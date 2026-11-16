import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

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


def test_an_installed_engine_updates_itself_at_launch_beside_the_old_file(tmp_path):
    install_fake_engines(tmp_path)
    github = FakeGitHub(delay=0.2)
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)
    engines.start()
    assert engines.status()["yt-dlp"]["busy"] == "updating"
    status = settle(engines)
    assert (status["yt-dlp"]["version"], status["yt-dlp"]["latest"], status["yt-dlp"]["error"]) == (
        LATEST,
        LATEST,
        None,
    )
    assert (tmp_path / "engines" / "yt-dlp" / INSTALLED).exists()  # until the next launch deletes it
    engines.update("yt-dlp")  # already the latest: nothing to fetch
    assert settle(engines)["yt-dlp"]["version"] == LATEST


def test_a_failed_update_at_launch_keeps_the_old_engine_and_says_so_without_names(tmp_path):
    install_fake_engines(tmp_path)
    offline = FakeGitHub(offline=True)
    engines = Engines(tmp_path / "engines", fetch=offline.fetch, resolve=offline.resolve)
    engines.start()
    status = settle(engines)["yt-dlp"]
    assert (status["version"], status["error"]) == (
        INSTALLED,
        "Spool couldn't update its engine. Check your internet connection.",
    )


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


def test_update_is_accepted_and_progress_arrives_in_snapshots(app, github):
    settle(app.engines)  # the update Spool starts by itself at launch has finished
    github.delay = 0.3  # long enough for the second request to find the engine busy
    events = app.events()
    events.next()
    status, _ = app.json("POST", "/api/engines/yt-dlp/update")
    assert status == 202
    assert app.json("POST", "/api/engines/yt-dlp/update")[0] == 409
    events.until(lambda e, d: e == "snapshot" and d["engines"]["yt-dlp"]["busy"] == "updating")
    events.until(lambda e, d: e == "snapshot" and not d["engines"]["yt-dlp"]["busy"])
    assert app.engines.status()["yt-dlp"]["version"] == LATEST
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
        settle(engines)  # the update at launch
        q = jobs.subscribe()
        q.get()
        engines.update("youtube-dl")  # already the latest: done within milliseconds, before any polling would look

        end = time.time() + 3
        while True:
            event, data = q.get(timeout=max(0.01, end - time.time()))
            if event == "snapshot" and data["engines"]["youtube-dl"]["busy"] is None:
                assert data["engines"]["yt-dlp"]["version"] == LATEST
                break
    finally:
        httpd.shutdown()
        jobs.close()


def test_writes_at_the_same_instant_never_collide(tmp_path):
    import threading

    from spool.engines import write_atomic

    errors = []

    def write(n):
        try:
            for _ in range(50):
                write_atomic(tmp_path / "state.json", str(n).encode())
        except OSError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_older_engine_files_are_deleted_at_startup(tmp_path):
    install_fake_engines(tmp_path)
    folder = tmp_path / "engines" / "yt-dlp"
    (folder / "2025.01.01").write_bytes(b"old engine")
    (folder / "current.abc123.tmp").write_bytes(b"cut short")
    github = FakeGitHub(latest=INSTALLED)  # nothing newer, so no install races the listing below
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)
    engines.start()
    assert sorted(p.name for p in folder.iterdir()) == [INSTALLED, "current"]
    settle(engines)


# A new release is checked before it becomes current


def broken_engine_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for module in ("yt_dlp", "youtube_dl"):
            z.writestr(f"{module}/__init__.py", "raise ImportError('this release needs a newer Python')\n")
    return buf.getvalue()


def test_a_release_that_cannot_load_is_never_made_current(tmp_path):
    install_fake_engines(tmp_path)
    github = FakeGitHub()
    github.asset = broken_engine_zip()  # its checksum matches: only running it shows the fault
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)
    engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert (status["version"], status["error"]) == (INSTALLED, "Spool couldn't update its engine.")
    assert not (tmp_path / "engines" / "yt-dlp" / LATEST).exists()
    assert probe_with(engines.path("yt-dlp"), "yt-dlp")["event"] == "info"
    assert "newer Python" in (tmp_path / "engines" / "update.log").read_text()  # the cause is kept somewhere


def test_a_deleted_engine_file_is_installed_again(tmp_path):
    install_fake_engines(tmp_path)
    shutil.rmtree(tmp_path / "engines" / "yt-dlp" / INSTALLED)
    github = FakeGitHub(latest=INSTALLED)
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)
    assert engines.status()["yt-dlp"]["installed"] is False
    engines.start()
    assert settle(engines)["yt-dlp"]["installed"] is True
    assert probe_with(engines.path("yt-dlp"), "yt-dlp")["event"] == "info"


@pytest.mark.parametrize("tag", [".", "..", "current", "latest", "\u0662\u0660\u0662\u0666.01.01"])
def test_a_release_tag_that_is_not_a_version_is_refused(tmp_path, tag):
    github = FakeGitHub(latest=tag)
    engines = Engines(tmp_path, fetch=github.fetch, resolve=github.resolve)
    engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert (status["installed"], status["error"]) == (False, "Spool couldn't update its engine.")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["update.log"]  # nothing installed, the cause kept


def test_a_fetch_larger_than_the_limit_is_refused(monkeypatch):
    from spool import engines

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engines, "LIMIT", 10)
    monkeypatch.setattr(engines.urllib.request, "urlopen", lambda url, timeout: Response(b"x" * 11))
    with pytest.raises(ValueError):
        engines.http_get("https://example.test/big")
    monkeypatch.setattr(engines.urllib.request, "urlopen", lambda url, timeout: Response(b"x" * 10))
    assert engines.http_get("https://example.test/small") == b"x" * 10


# The package carries the licences of what it ships


def test_the_wheel_carries_every_licence(tmp_path):
    import spool

    root = Path(spool.__file__).parents[2]
    if not (root / "pyproject.toml").exists() or not shutil.which("uv"):
        pytest.skip("needs the source tree and uv")
    build = ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(root)]
    subprocess.run(build, check=True, capture_output=True, timeout=120)
    [wheel] = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    for wanted in ("licenses/LICENSE", "licenses/NOTICE.md", "vendor/LICENSE-preact.txt", "vendor/LICENSE-htm.txt"):
        assert any(n.endswith(wanted) for n in names), wanted
