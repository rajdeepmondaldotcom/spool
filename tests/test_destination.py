import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from support import Held, job_state, until

from spool import system


def entry(url="https://fake.test/video?delay=0"):
    return {"url": url, "id": "vid-1", "title": "Fake video 1"}


# Settings


def test_settings_change_the_destination_engine_and_preset(app, tmp_path):
    folder = tmp_path / "Videos"
    folder.mkdir()
    events = app.events()
    events.next()
    status, body = app.json("PUT", "/api/settings", {"dest": str(folder), "engine": "youtube-dl", "preset": "720"})
    assert status == 200
    assert body["settings"] == {"dest": str(folder), "engine": "youtube-dl", "preset": "720", "signin": None}
    assert (body["dest"]["ok"], body["dest"]["problem"]) == (True, None)
    assert body["dest"]["free_bytes"] > 0
    snap = events.until(lambda e, d: e == "snapshot" and d["settings"]["dest"] == str(folder))[-1][1]
    assert snap["dest"]["path"] == str(folder)
    events.close()


def test_a_missing_destination_is_saved_with_its_problem_and_never_created(app, tmp_path):
    gone = tmp_path / "Unplugged" / "Spool"
    status, body = app.json("PUT", "/api/settings", {"dest": str(gone)})
    assert status == 200
    assert (body["dest"]["ok"], body["dest"]["problem"], body["dest"]["free_bytes"]) == (False, "missing", None)
    assert not gone.parent.exists()
    assert app.jobs.snapshot()["settings"]["dest"] == str(gone)


def test_a_file_is_not_a_folder(app, tmp_path):
    (tmp_path / "file").write_text("x")
    assert app.json("PUT", "/api/settings", {"dest": str(tmp_path / "file")})[1]["dest"]["problem"] == "not_a_folder"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="permissions differ")
def test_a_read_only_folder_is_not_writable(app, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert app.json("PUT", "/api/settings", {"dest": str(locked)})[1]["dest"]["problem"] == "not_writable"
        status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
        assert (status, body["error"]["kind"], body["error"]["hint"]) == (409, "dest_denied", "check_dest")
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(
    "body, kind",
    [
        ({"dest": "relative/path"}, "invalid_dest"),
        ({"dest": 7}, "invalid_dest"),
        ({"engine": "ffmpeg"}, "invalid_engine"),
        ({"preset": "4k"}, "invalid_preset"),
        ({"signin": "netscape"}, "invalid_signin"),
        ({"colour": "red"}, "invalid_setting"),
    ],
)
def test_bad_settings_are_refused_with_the_reason(app, body, kind):
    status, reply = app.json("PUT", "/api/settings", body)
    assert (status, reply["error"]["kind"]) == (422, kind)


# Checks before downloading


def test_a_download_to_a_missing_destination_is_refused_up_front(app, tmp_path):
    app.json("PUT", "/api/settings", {"dest": str(tmp_path / "nowhere")})
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert (status, body["error"]["kind"], body["error"]["hint"]) == (409, "dest_missing", "check_dest")
    assert app.jobs.snapshot()["jobs"] == []


def test_the_destination_is_checked_again_when_the_job_starts(app, tmp_path):
    folder = tmp_path / "Drive"
    folder.mkdir()
    app.json("PUT", "/api/settings", {"dest": str(folder)})
    events = app.events()
    events.next()
    # two slow jobs fill both slots, so the third waits while its drive is unplugged
    app.json(
        "POST",
        "/api/jobs",
        {"entries": [entry(f"https://fake.test/slow/{n}?delay=0.1") for n in (1, 2)], "preset": "best"},
    )
    third = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})[1]["jobs"][0]
    shutil.rmtree(folder)
    seen = events.until(lambda e, d: e == "job" and d["id"] == third["id"] and d["state"] == "failed", timeout=20)
    events.close()
    assert seen[-1][1]["error"]["kind"] == "dest_missing"
    assert not folder.exists()


# Display label


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS volumes")
def test_a_drive_is_labelled_by_its_name():
    assert system.display("/Volumes/T7/Spool") == "T7 › Spool"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS volumes")
def test_a_deep_folder_on_a_drive_is_its_last_two_names():
    assert system.display("/Volumes/T7/Media/Video/2026/Spool") == "2026 › Spool"
    assert system.display("/Volumes/T7") == "T7"


def test_a_folder_in_home_is_labelled_without_the_home_part():
    assert system.display(Path.home() / "Downloads") == "Downloads"
    assert system.display(Path.home() / "Movies" / "Clips") == "Movies › Clips"
    assert system.display(Path.home() / "a" / "b" / "c" / "d") == "c › d"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_anything_else_shows_its_parent_and_folder():
    assert system.display("/var/folders/m3/9gfmvp/T/downloads") == "T › downloads"
    assert system.display("/") == "/"
    assert system.display("/srv/videos") == "srv › videos"


# Folder chooser and reveal


def test_choose_folder_is_501_without_a_native_dialog(app, monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "linux")
    assert app.json("POST", "/api/choose-folder")[0] == 501


def test_reveal_needs_a_finished_file_that_still_exists(app):
    events = app.events()
    events.next()
    job = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})[1]["jobs"][0]
    done = events.until(job_state("done"))[-1][1]
    events.close()
    os.remove(done["path"])
    assert app.json("POST", f"/api/jobs/{job['id']}/reveal")[0] == 410
    assert app.json("POST", "/api/jobs/nope/reveal")[0] == 404
    assert app.jobs.snapshot()["jobs"][0]["file_exists"] is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot delete a folder holding an open file, so a vanishing folder is not covered there",
)
def test_a_destination_that_vanishes_mid_download_is_named_as_missing(app, tmp_path, monkeypatch):
    worker = Held(monkeypatch, tmp_path)
    folder = tmp_path / "Drive"
    folder.mkdir()
    app.json("PUT", "/api/settings", {"dest": str(folder)})
    events = app.events()
    events.next()
    job = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/hold", "id": "v"}], "preset": "best"})
    job_id = job[1]["jobs"][0]["id"]
    worker.held()  # mid-download
    shutil.rmtree(folder)  # the drive is unplugged
    worker.go()  # its next write fails
    failed = events.until(lambda e, d: e == "job" and d["id"] == job_id and d["state"] == "failed")[-1][1]
    events.close()
    assert (failed["error"]["kind"], failed["error"]["hint"]) == ("dest_missing", "check_dest")


# Truer words for a destination problem


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS volumes")
def test_a_drive_that_is_not_plugged_in_is_unplugged_not_missing(app):
    status, body = app.json("PUT", "/api/settings", {"dest": "/Volumes/NoSuchDrive/Spool"})
    assert (status, body["dest"]["problem"]) == (200, "unplugged")
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert (status, body["error"]["kind"], body["error"]["hint"]) == (409, "dest_unplugged", "check_dest")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux mount points")
@pytest.mark.parametrize(
    "path", ["/media/nobody-here/T7/Spool", "/run/media/nobody-here/T7/Spool", "/mnt/no-such-drive/x"]
)
def test_an_absent_mount_point_is_unplugged(path):
    assert system.check_dest(path)["problem"] == "unplugged"


def test_a_missing_folder_on_a_present_drive_is_missing(app, tmp_path):
    app.json("PUT", "/api/settings", {"dest": str(tmp_path / "gone")})
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert (status, body["error"]["kind"]) == (409, "dest_missing")


def test_a_file_as_destination_fails_as_not_a_folder(app, tmp_path):
    (tmp_path / "file").write_text("x")
    app.json("PUT", "/api/settings", {"dest": str(tmp_path / "file")})
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert (status, body["error"]["kind"]) == (409, "dest_not_folder")


# A destination check that never answers


@pytest.fixture
def hanging_drive(monkeypatch):
    """Every look at the drive blocks, as a sleeping drive or a macOS permission prompt does."""
    import threading

    monkeypatch.setattr(system, "CHECK_TIMEOUT", 0.3)
    release, started, real = threading.Event(), [], system.probe

    def stuck(path, write):
        started.append(path)
        release.wait()
        return real(path, write)

    monkeypatch.setattr(system, "probe", stuck)
    yield started, release
    release.set()


def test_a_destination_that_never_answers_is_not_responding_and_nothing_freezes(app, tmp_path, hanging_drive):
    folder = tmp_path / "Asleep"
    folder.mkdir()
    t = time.time()
    status, body = app.json("PUT", "/api/settings", {"dest": str(folder)})
    assert (status, body["dest"]["problem"], body["dest"]["ok"]) == (200, "not_responding", False)
    events = app.events()  # a new event stream still opens
    assert events.next()[1]["dest"]["problem"] == "not_responding"
    events.close()
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert (status, body["error"]["kind"]) == (409, "dest_not_responding")
    assert time.time() - t < 5
    assert hanging_drive[0].count(str(folder)) == 2  # one stuck check of each kind, however often Spool asks


def test_a_snapshot_goes_out_when_the_stuck_check_finally_answers(app, tmp_path, hanging_drive):
    folder = tmp_path / "Asleep"
    folder.mkdir()
    app.json("PUT", "/api/settings", {"dest": str(folder)})
    events = app.events()
    events.next()
    hanging_drive[1].set()  # the drive wakes up
    seen = events.until(lambda e, d: e == "snapshot" and d["dest"]["ok"], timeout=10)
    events.close()
    assert seen[-1][1]["dest"]["problem"] is None


def test_a_drive_busy_with_spools_own_download_is_answering(app, monkeypatch, tmp_path):
    """A slow drive queues the write test behind Spool's own writes: the running job is the proof it works."""
    worker = Held(monkeypatch, tmp_path)
    events = app.events()
    events.next()
    first = app.json(
        "POST", "/api/jobs", {"entries": [{"url": "https://fake.test/hold?name=First", "id": "a"}], "preset": "best"}
    )[1]["jobs"][0]
    worker.held("First")
    until(lambda: app.jobs.snapshot()["jobs"][0]["stage"] == "downloading")  # Spool knows it is writing
    monkeypatch.setattr(system, "CHECK_TIMEOUT", 0.3)
    real = system.probe
    monkeypatch.setattr(system, "probe", lambda path, write: time.sleep(1) or real(path, write))  # past the limit

    status, body = app.json("PUT", "/api/settings", {"dest": str(app.dest)})
    assert (status, body["dest"]["ok"], body["dest"]["problem"]) == (200, True, None)
    assert app.jobs.snapshot()["dest"]["ok"] is True  # the page leaves Download enabled
    entry = {"url": "https://fake.test/plain?name=Second", "id": "b"}
    status, body = app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})
    assert status == 201, body
    second = events.until(
        lambda e, d: e == "job" and d["id"] == body["jobs"][0]["id"] and d["state"] in ("done", "failed")
    )
    assert second[-1][1]["state"] == "done", second[-1][1]["error"]

    worker.go()
    events.until(lambda e, d: e == "job" and d["id"] == first["id"] and d["state"] == "done", timeout=20)
    events.close()
    status, body = app.json("PUT", "/api/settings", {"dest": str(app.dest)})  # no job of Spool's there now
    assert body["dest"]["problem"] == "not_responding"


def writes_wait(monkeypatch, seconds):
    """Our write test waits in line seconds (None: for ever) while a look answers at once, as on a drive another
    program writes to flat out."""
    monkeypatch.setattr(system, "CHECK_TIMEOUT", 0.3)
    release, real = threading.Event(), system.probe
    monkeypatch.setattr(system, "probe", lambda path, write: (write and release.wait(seconds), real(path, write))[1])
    return release


@pytest.fixture
def slow_writes(monkeypatch):
    release = writes_wait(monkeypatch, 1.5)  # well past CHECK_TIMEOUT, as the 11.5 s measured against its 3
    yield
    release.set()


@pytest.fixture
def stuck_writes(monkeypatch):
    release = writes_wait(monkeypatch, None)
    yield
    release.set()


def test_a_drive_slow_to_take_a_write_starts_the_download_at_the_first_click(app, slow_writes):
    events = app.events()
    events.next()
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert status == 201, body
    seen = events.until(lambda e, d: e == "job" and d["state"] in ("done", "failed"), timeout=20)
    events.close()
    assert seen[-1][1]["state"] == "done", seen[-1][1]["error"]


def test_a_write_test_that_never_finishes_fails_the_job_after_a_while(app, monkeypatch, stuck_writes):
    monkeypatch.setattr(system, "START_TIMEOUT", 0.6)
    events = app.events()
    events.next()
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert status == 201, body
    seen = events.until(lambda e, d: e == "job" and d["state"] in ("done", "failed"), timeout=20)
    events.close()
    error = seen[-1][1]["error"]
    assert (seen[-1][1]["state"], error["kind"], error["hint"]) == ("failed", "dest_not_responding", "check_dest")


def test_a_job_waiting_on_a_write_test_can_be_cancelled_and_frees_its_slot(app, monkeypatch, stuck_writes):
    monkeypatch.setattr(system, "START_TIMEOUT", 1.0)
    status, body = app.json("POST", "/api/jobs", {"entries": [entry()], "preset": "best"})
    assert status == 201, body
    job = body["jobs"][0]
    until(lambda: app.jobs.snapshot()["jobs"][0]["stage"] == "starting")  # its worker waits on the write test
    status, body = app.json("POST", f"/api/jobs/{job['id']}/cancel")
    assert (status, body["state"]) == (200, "cancelled")
    until(lambda: not app.jobs.procs, timeout=10)  # the slot is free once the wait is over
    assert app.jobs.snapshot()["jobs"][0]["state"] == "cancelled"


# A check never raises, and a watcher's check writes nothing


def test_a_folder_spool_may_not_enter_is_denied_not_a_crash(tmp_path, monkeypatch):
    inside = tmp_path / "Private" / "Spool"
    real = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda p, **k: (_ for _ in ()).throw(PermissionError(13, "denied")) if p == inside else real(p, **k),
    )
    check = system.check_dest(inside)
    assert (check["ok"], check["problem"], check["path"]) == (False, "not_writable", str(inside))


def test_a_share_that_drops_is_not_responding_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(system.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError(5, "Input/output error")))
    check = system.check_dest(tmp_path)
    assert (check["ok"], check["problem"], check["free_bytes"]) == (False, "not_responding", None)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the failure is the test
def test_a_check_that_fails_in_a_way_nobody_foresaw_still_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "probe_dest", lambda path: 1 / 0)
    assert system.check_dest(tmp_path)["problem"] == "not_responding"


def test_the_quick_check_writes_nothing_and_still_sees_a_folder_it_cannot_write(tmp_path, monkeypatch):
    monkeypatch.setattr(system.tempfile, "TemporaryFile", lambda **k: pytest.fail("the quick check wrote a file"))
    assert system.check_dest(tmp_path, write=False)["ok"] is True
    monkeypatch.setattr(system.os, "access", lambda p, mode: False)
    assert system.check_dest(tmp_path / ".", write=False)["problem"] == "not_writable"


# Tools, the folder dialog and the file manager (never run for real: subprocess.run is replaced)


def test_tools_with_spools_runtime_reads_path_once_and_agrees_with_it(monkeypatch):
    asked = []
    monkeypatch.setattr(system.shutil, "which", lambda name: asked.append(name) or (name == "quickjs") or None)

    class Js:
        def tools(self):
            return {"js_runtime": "deno", "js_setup": None, "js_path": "/x/deno"}

    assert system.tools(Js())["js_runtime"] == "deno"
    assert asked == ["ffmpeg"]
    assert system.tools()["js_runtime"] is None  # a runtime the engines can't use is no runtime


class Ran:
    def __init__(self, monkeypatch, returncode=0, stdout="", stderr="", missing=False):
        self.calls, self.result = [], (returncode, stdout, stderr)
        self.missing = missing
        monkeypatch.setattr(system.subprocess, "run", self)

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.missing:
            raise FileNotFoundError(2, "No such file or directory")
        code, out, err = self.result
        return system.subprocess.CompletedProcess(args, code, out, err)


def test_the_folder_dialog_tells_cancel_from_failure(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    Ran(monkeypatch, stdout="/Volumes/T7/Spool/\n")
    assert system.choose_folder() == "/Volumes/T7/Spool"
    Ran(monkeypatch, 1, stderr="execution error: User canceled. (-128)")
    assert system.choose_folder() is None
    Ran(monkeypatch, 1, stderr="execution error: Not authorized to send Apple events to System Events. (-1743)")
    with pytest.raises(NotImplementedError):
        system.choose_folder()


def test_reveal_on_windows_quotes_only_the_path(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "win32")
    ran = Ran(monkeypatch)
    system.reveal(r"C:\Users\a\Downloads\My video [x].mp4")
    assert ran.calls == [r'explorer /select,"C:\Users\a\Downloads\My video [x].mp4"']


def test_no_file_manager_is_not_supported_not_a_crash(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "linux")
    Ran(monkeypatch, missing=True)
    for call in (system.reveal, system.open_folder):
        with pytest.raises(NotImplementedError):
            call("/srv/videos/a.mp4")


# Process groups


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_leftovers_are_killed_where_python_cannot_wait_without_reaping(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)  # as on macOS before Python 3.13
    marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys; "
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(str(c.pid))"
    )
    proc = system.spawn([sys.executable, "-c", script])
    while not marker.exists():
        time.sleep(0.02)
    system.kill_leftovers(proc)
    assert proc.returncode == 0
    child, deadline = int(marker.read_text()), time.time() + 5
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        assert time.time() < deadline, "the grandchild outlived the worker"
        time.sleep(0.05)


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs os.waitid: not on Windows, nor macOS before Python 3.13")
def test_leftovers_are_killed_before_the_worker_is_reaped(tmp_path):
    marker = tmp_path / "child.pid"
    script = (
        "import subprocess, sys; "
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(str(c.pid))"
    )
    proc = system.spawn([sys.executable, "-c", script])
    while not marker.exists():  # never poll(): that would reap the worker
        time.sleep(0.02)
    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)  # exited, not yet reaped
    child = int(marker.read_text())
    system.kill_leftovers(proc)
    assert proc.returncode == 0  # kill_leftovers reaps it, after the group is gone
    deadline = time.time() + 5
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        assert time.time() < deadline, "the grandchild outlived the worker"
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_kill_survives_a_group_it_may_not_signal(monkeypatch):
    proc = system.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with monkeypatch.context() as m:
            m.setattr(system.os, "killpg", lambda *a: (_ for _ in ()).throw(PermissionError(1, "not permitted")))
            system.kill(proc, grace=0.1)
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize(
    "platform, call", [("darwin", ["open", "/v/a [x].mp4"]), ("linux", ["xdg-open", "/v/a [x].mp4"])]
)
def test_open_file_uses_the_systems_default_program(monkeypatch, platform, call):
    monkeypatch.setattr(system.sys, "platform", platform)
    ran = Ran(monkeypatch)
    system.open_file("/v/a [x].mp4")
    assert ran.calls == [call]
    Ran(monkeypatch, missing=True)
    with pytest.raises(NotImplementedError):
        system.open_file("/v/a [x].mp4")


def test_open_file_on_windows_asks_windows(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "win32")
    started = []
    monkeypatch.setattr(system.os, "startfile", started.append, raising=False)
    system.open_file(r"C:\v\a.mp4")
    assert started == [r"C:\v\a.mp4"]

    def no_program(path):
        raise OSError("No application is associated with the specified file")

    monkeypatch.setattr(system.os, "startfile", no_program, raising=False)
    with pytest.raises(NotImplementedError):
        system.open_file(r"C:\v\a.mp4")
