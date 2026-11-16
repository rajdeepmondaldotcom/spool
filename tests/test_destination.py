import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from support import job_state

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
    assert body["settings"] == {"dest": str(folder), "engine": "youtube-dl", "preset": "720"}
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
def test_a_destination_that_vanishes_mid_download_is_named_as_missing(app, tmp_path):
    folder = tmp_path / "Drive"
    folder.mkdir()
    app.json("PUT", "/api/settings", {"dest": str(folder)})
    events = app.events()
    events.next()
    job = app.json("POST", "/api/jobs", {"entries": [entry("https://fake.test/slow?delay=0.1")], "preset": "best"})
    job_id = job[1]["jobs"][0]["id"]
    events.until(lambda e, d: e == "job" and d["id"] == job_id and d["stage"] == "downloading")
    shutil.rmtree(folder)
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
    """The first write to the drive blocks forever, as a sleeping drive or a macOS permission prompt does."""
    import threading

    monkeypatch.setattr(system, "CHECK_TIMEOUT", 0.3)
    release, started, real = threading.Event(), [], system.probe_dest

    def stuck(path):
        started.append(path)
        release.wait()
        return real(path)

    monkeypatch.setattr(system, "probe_dest", stuck)
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
    assert hanging_drive[0].count(str(folder)) == 1  # one stuck check, however often Spool asks


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
