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

from spool.engines import RELEASES, Busy, Engines
from spool.jobs import WORKER


def settle(engines, timeout=10):
    end = time.time() + timeout
    while any(s["busy"] for s in engines.status().values()):
        assert time.time() < end, engines.status()
        time.sleep(0.02)
    return engines.status()  # an update learns the latest version before it stops being busy


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
    assert (status["installed"], status["error"]) == (False, "Spool couldn't set up its engine.")
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


def test_reading_the_current_version_takes_its_turn_with_a_replace(tmp_path):
    """Windows refuses to open a file that is being replaced, so a read waits for the write to finish."""
    import threading

    from spool import engines as module

    install_fake_engines(tmp_path)
    engines, read = Engines(tmp_path / "engines"), threading.Event()
    with module._replacing:  # a replace is in flight
        threading.Thread(target=lambda: (engines.version("yt-dlp"), read.set()), daemon=True).start()
        assert not read.wait(0.2)
    assert read.wait(2)
    assert engines.version("yt-dlp") == INSTALLED


# A newer Spool


class Releases:
    def __init__(self, tag="v0.2.1", fail=False):
        self.tag, self.fail, self.asked = tag, fail, []

    def __call__(self, url):
        self.asked.append(url)
        if self.fail:
            raise OSError("offline")
        return f"https://github.com/rajdeepmondaldotcom/spool/releases/tag/{self.tag}"


@pytest.mark.parametrize(
    "tag, running, newer",
    [
        ("v0.2.1", "0.2.0", "0.2.1"),
        ("0.10.0", "0.9.3", "0.10.0"),
        ("v0.2.0", "0.2.0", None),
        ("v0.1.9", "0.2.0", None),
        ("releases", "0.2.0", None),  # no release yet: the page redirects to the list
        ("v0.3.0-rc1", "0.2.0", None),
        ("v0.2.1", None, None),  # run from a source tree
    ],
)
def test_a_newer_spool_is_a_release_tag_above_the_running_version(tmp_path, tag, running, newer):
    from spool.engines import newer_spool

    releases = Releases(tag)
    assert newer_spool(tmp_path, running, resolve=releases) == newer
    assert releases.asked == ["https://github.com/rajdeepmondaldotcom/spool/releases/latest"]  # never api.github.com


def test_the_release_page_is_asked_at_most_once_a_day_even_across_restarts(tmp_path):
    from spool.engines import newer_spool

    releases, now = Releases(), 1_000_000.0
    assert newer_spool(tmp_path, "0.2.0", resolve=releases, now=lambda: now) == "0.2.1"
    releases.tag = "v0.2.2"
    assert newer_spool(tmp_path, "0.2.0", resolve=releases, now=lambda: now + 86_000) == "0.2.1"
    assert len(releases.asked) == 1
    assert newer_spool(tmp_path, "0.2.0", resolve=releases, now=lambda: now + 86_500) == "0.2.2"
    assert len(releases.asked) == 2


def test_a_failed_look_says_nothing_and_asks_again_next_time(tmp_path):
    from spool.engines import newer_spool

    releases = Releases(fail=True)
    assert newer_spool(tmp_path, "0.2.0", resolve=releases) is None
    (tmp_path / "update.json").write_text("{broken")
    releases.fail = False
    assert newer_spool(tmp_path, "0.2.0", resolve=releases) == "0.2.1"
    assert len(releases.asked) == 2


# A newer engine by itself

NIGHTLY = "2026.02.03.232655"
DAILY = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases"


def installed(tmp_path, github):
    install_fake_engines(tmp_path)
    return Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)


def test_newer_installs_the_newest_build_of_every_source(tmp_path):
    github = FakeGitHub(nightly=NIGHTLY)
    engines = installed(tmp_path, github)
    assert engines.newer("yt-dlp") is True
    status = engines.status()["yt-dlp"]
    assert (status["version"], status["latest"], status["busy"], status["error"]) == (NIGHTLY, NIGHTLY, None, None)
    assert f"{DAILY}/download/{NIGHTLY}/yt-dlp" in github.fetched
    assert probe_with(engines.path("yt-dlp"), "yt-dlp")["event"] == "info"


def test_newer_asks_at_most_once_an_hour_per_engine(tmp_path):
    github = FakeGitHub(latest=INSTALLED)
    engines = installed(tmp_path, github)
    assert engines.newer("yt-dlp") is False
    asked = list(github.fetched)
    assert len(asked) == 2  # each source's latest, and nothing to fetch
    github.latest = LATEST
    assert engines.newer("yt-dlp") is False  # within the hour: not asked again
    assert github.fetched == asked
    assert engines.newer("youtube-dl") is True  # another engine has its own hour
    assert [u for u in github.fetched if u.endswith("/latest")][2:] == [f"{RELEASES['youtube-dl']}/latest"]
    engines.asked["yt-dlp"] -= 3601
    assert engines.newer("yt-dlp") is True


def test_newer_never_raises_and_says_false_when_offline(tmp_path):
    engines = installed(tmp_path, FakeGitHub(offline=True))
    assert engines.newer("yt-dlp") is False
    assert engines.status()["yt-dlp"]["version"] == INSTALLED


def test_one_source_offline_still_finds_the_other(tmp_path):
    github = FakeGitHub(nightly=NIGHTLY)
    resolve = github.resolve
    github.resolve = lambda url: resolve(url) if "nightly" in url else (_ for _ in ()).throw(OSError("down"))
    engines = installed(tmp_path, github)
    assert engines.newer("yt-dlp") is True
    assert engines.version("yt-dlp") == NIGHTLY


def test_newer_waits_for_an_update_already_running(tmp_path):
    import threading

    github, gate = FakeGitHub(), threading.Event()
    resolve = github.resolve
    github.resolve = lambda url: gate.wait(20) and resolve(url)  # the update is held until newer() waits for it
    engines = installed(tmp_path, github)
    waiting, wait = threading.Event(), engines.done.wait
    engines.done.wait = lambda *a: waiting.set() or wait(*a)
    engines.update("yt-dlp")
    answer = []
    looking = threading.Thread(target=lambda: answer.append(engines.newer("yt-dlp")))
    looking.start()
    assert waiting.wait(20)
    gate.set()
    looking.join(20)
    assert answer == [True]  # the running update installed LATEST
    assert engines.status()["yt-dlp"]["version"] == LATEST
    assert github.fetched.count(f"{RELEASES['yt-dlp']}/latest") == 1  # newer() did not look itself
    engines.update("youtube-dl")
    settle(engines)  # it has LATEST now: nothing is newer
    assert engines.newer("youtube-dl") is False


def test_a_failed_look_leaves_no_error_on_the_page_and_keeps_the_cause(tmp_path):
    engines = installed(tmp_path, FakeGitHub(offline=True))
    assert engines.newer("yt-dlp") is False
    assert engines.status()["yt-dlp"]["error"] is None
    assert "update yt-dlp" in (tmp_path / "engines" / "update.log").read_text()
    engines.error["yt-dlp"] = "an earlier update's words"
    engines.asked.clear()
    engines.newer("yt-dlp")
    assert engines.status()["yt-dlp"]["error"] == "an earlier update's words"  # left as it was


def test_a_daily_build_that_fails_to_install_never_offers_an_update_that_finds_nothing(tmp_path):
    github = FakeGitHub(latest=INSTALLED, nightly=NIGHTLY, corrupt=True)
    engines = installed(tmp_path, github)
    assert engines.newer("yt-dlp") is False
    assert engines.status()["yt-dlp"]["latest"] == INSTALLED  # what update() would install
    github.corrupt = False
    engines.asked.clear()
    assert engines.newer("yt-dlp") is True
    status = engines.status()["yt-dlp"]
    assert (status["version"], status["latest"]) == (NIGHTLY, NIGHTLY)  # never older than the version


def test_newer_never_raises(tmp_path, monkeypatch):
    engines = installed(tmp_path, FakeGitHub())
    monkeypatch.setattr(engines, "version", lambda name: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert engines.newer("yt-dlp") is False
    monkeypatch.undo()
    engines.on_change = lambda: (_ for _ in ()).throw(RuntimeError("a listener broke"))
    engines.newer("youtube-dl")  # answers, whatever it answers
    assert engines.busy["youtube-dl"] is None  # and no update is left looking busy


@pytest.mark.parametrize("launch", [True, False])
def test_no_update_replaces_the_engine_with_an_older_one(tmp_path, launch):
    github = FakeGitHub(nightly=NIGHTLY)
    engines = installed(tmp_path, github)
    assert engines.newer("yt-dlp") is True
    engines = Engines(tmp_path / "engines", fetch=github.fetch, resolve=github.resolve)  # a new launch
    if launch:
        engines.start()
    else:
        engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert (status["version"], status["latest"], status["error"]) == (NIGHTLY, NIGHTLY, None)
    assert not (tmp_path / "engines" / "yt-dlp" / LATEST).exists()


def test_latest_is_never_older_than_the_installed_version(tmp_path):
    github = FakeGitHub(latest="2025.06.01")
    engines = installed(tmp_path, github)
    engines.update("yt-dlp")
    status = settle(engines)["yt-dlp"]
    assert (status["version"], status["latest"]) == (INSTALLED, INSTALLED)


def test_the_second_engine_has_one_source(tmp_path):
    github = FakeGitHub(nightly=NIGHTLY)
    engines = installed(tmp_path, github)
    assert engines.newer("youtube-dl") is True
    assert engines.version("youtube-dl") == LATEST
    assert not [u for u in github.fetched if "yt-dlp" in u]
