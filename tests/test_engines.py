import json
import subprocess
import sys
import time

import pytest
from serve_fake import INSTALLED, LATEST, FakeGitHub, install_fake_engines

from spool.engines import Busy, Engines
from spool.jobs import WORKER


def settle(engines, timeout=10):
    end = time.time() + timeout
    while any(s["busy"] for s in engines.status().values()):
        assert time.time() < end, engines.status()
        time.sleep(0.02)
    time.sleep(0.05)  # let the latest-version checks land too
    return engines.status()


def probe_with(path, engine):
    spec = {"engine": engine, "engine_path": path, "mode": "probe", "url": "https://fake.test/video", "ffmpeg": True}
    out = subprocess.run([sys.executable, "-I", str(WORKER)], input=json.dumps(spec), capture_output=True, text=True)
    return json.loads(out.stdout.splitlines()[-1])


def test_first_run_installs_both_engines_in_the_background(tmp_path):
    github = FakeGitHub(delay=0.2)
    engines = Engines(tmp_path, fetch=github.fetch, resolve=github.resolve)
    engines.start()
    assert {s["busy"] for s in engines.status().values()} == {"installing"}
    status = settle(engines)
    for name in ("yt-dlp", "youtube-dl"):
        assert status[name] == {"installed": True, "version": LATEST, "latest": LATEST, "busy": None, "error": None}
        assert probe_with(engines.path(name), name)["event"] == "info"  # the installed file is a working engine


def test_installs_come_from_the_tagged_release(tmp_path):
    github = FakeGitHub()
    engines = Engines(tmp_path, fetch=github.fetch, resolve=github.resolve)
    engines.update("youtube-dl")
    settle(engines)
    base = "https://github.com/ytdl-org/ytdl-nightly/releases"
    assert github.fetched == [
        f"{base}/latest",
        f"{base}/download/{LATEST}/SHA2-256SUMS",
        f"{base}/download/{LATEST}/youtube-dl",
    ]


def test_an_installed_engine_learns_the_latest_version_and_updates_beside_the_old_file(tmp_path):
    install_fake_engines(tmp_path)
    github = FakeGitHub()
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)
    engines.start()
    status = settle(engines)
    assert (status["yt-dlp"]["version"], status["yt-dlp"]["latest"]) == (INSTALLED, LATEST)
    engines.update("yt-dlp")
    assert engines.status()["yt-dlp"]["busy"] == "updating"
    assert settle(engines)["yt-dlp"]["version"] == LATEST
    assert (tmp_path / "engines" / "yt-dlp" / INSTALLED).exists()


def test_a_checksum_mismatch_is_refused_and_the_old_engine_kept(tmp_path):
    install_fake_engines(tmp_path)
    engines = Engines(tmp_path / "engines", fetch=FakeGitHub(corrupt=True).fetch, resolve=FakeGitHub().resolve)
    old = engines.path("yt-dlp")
    engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert status["version"] == INSTALLED
    assert "checksum" in status["error"]
    assert engines.path("yt-dlp") == old
    assert not (tmp_path / "engines" / "yt-dlp" / LATEST).exists()


def test_offline_leaves_latest_unknown_and_says_so_on_update(tmp_path):
    install_fake_engines(tmp_path)
    offline = FakeGitHub(offline=True)
    engines = Engines(tmp_path / "engines", fetch=offline.fetch, resolve=offline.resolve)
    engines.start()
    assert settle(engines)["yt-dlp"]["latest"] is None
    engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert status["version"] == INSTALLED
    assert "internet" in status["error"]


def test_one_update_at_a_time_and_only_known_engines(tmp_path):
    github = FakeGitHub(delay=0.3)
    engines = Engines(tmp_path, fetch=github.fetch, resolve=github.resolve)
    engines.update("yt-dlp")
    with pytest.raises(Busy):
        engines.update("yt-dlp")
    with pytest.raises(KeyError):
        engines.update("ffmpeg")
    settle(engines)


# Through the server


@pytest.fixture
def github():
    return FakeGitHub(delay=0.3)


def test_update_is_accepted_and_progress_arrives_in_snapshots(app):
    settle(app.engines)
    events = app.events()
    events.next()
    status, _ = app.json("POST", "/api/engines/yt-dlp/update")
    assert status == 202
    assert app.json("POST", "/api/engines/yt-dlp/update")[0] == 409
    seen = events.until(lambda e, d: e == "snapshot" and d["engines"]["yt-dlp"]["version"] == LATEST)
    assert any(e == "snapshot" and d["engines"]["yt-dlp"]["busy"] == "updating" for e, d in seen)
    events.close()


def test_update_of_an_unknown_engine_is_404(app):
    assert app.json("POST", "/api/engines/nope/update")[0] == 404


def test_until_the_first_install_finishes_probe_says_the_engine_is_not_ready(tmp_path):
    from serve_fake import start
    from support import Client

    httpd, url, jobs, engines = start(tmp_path, fresh=True, github=FakeGitHub(delay=0.3))
    client = Client(url.split("/#")[0])
    try:
        status, body = client.json("POST", "/api/probe", {"url": "https://fake.test/video"})
        assert (status, body["error"]["kind"]) == (503, "engine_not_ready")
        settle(engines)
        assert client.json("POST", "/api/probe", {"url": "https://fake.test/video"})[0] == 200
    finally:
        httpd.shutdown()
        jobs.close()


def test_an_update_right_after_launch_still_reaches_the_browser(tmp_path):
    from serve_fake import start

    httpd, url, jobs, engines = start(tmp_path, github=FakeGitHub())
    try:
        q = jobs.subscribe()
        q.get()
        engines.update("yt-dlp")  # finishes within milliseconds, before any polling would look
        end = time.time() + 3
        while True:
            event, data = q.get(timeout=max(0.01, end - time.time()))
            if event == "snapshot" and data["engines"]["yt-dlp"]["version"] == LATEST:
                break
    finally:
        httpd.shutdown()
        jobs.close()
