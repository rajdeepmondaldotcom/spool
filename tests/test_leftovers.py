"""Remove cleans what a job left half-written, and never a finished file (R13)."""

import os
import sys
import time

import pytest
from serve_fake import start


@pytest.fixture
def events(app):
    events = app.events()
    events.next()
    yield events
    events.close()


def add(app, url, preset="best"):
    status, body = app.json("POST", "/api/jobs", {"entries": [{"url": url}], "preset": preset})
    assert status == 201, body
    return body["jobs"][0]["id"]


def wait_for(events, job_id, match):
    return events.until(lambda e, d: e == "job" and d["id"] == job_id and match(d))[-1][1]


def names(folder):
    return sorted(p.name for p in folder.iterdir() if not p.name.startswith(".fake-"))


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder that refuses writes")
def test_remove_after_a_failed_download_again_keeps_the_finished_file(app, events):
    job_id = add(app, "https://fake.test/audio?delay=0", "audio")
    done = wait_for(events, job_id, lambda d: d["state"] == "done")
    app.dest.chmod(0o555)  # the next run fails on the folder check
    try:
        app.json("POST", f"/api/jobs/{job_id}/retry")
        assert wait_for(events, job_id, lambda d: d["state"] == "failed")["error"]["kind"] == "dest_denied"
    finally:
        app.dest.chmod(0o755)
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert os.path.exists(done["path"])


def test_removing_a_cancelled_twin_after_the_finished_one_keeps_the_file(app, events):
    first = add(app, "https://fake.test/audio?delay=0.1", "audio")
    wait_for(events, first, lambda d: d["stage"] == "downloading")
    app.json("POST", f"/api/jobs/{first}/cancel")
    second = add(app, "https://fake.test/audio?delay=0", "audio")
    assert second != first
    done = wait_for(events, second, lambda d: d["state"] == "done")
    assert app.request("DELETE", f"/api/jobs/{second}")[0] == 204
    assert app.request("DELETE", f"/api/jobs/{first}")[0] == 204
    assert names(app.dest) == [os.path.basename(done["path"])]


def test_remove_keeps_the_partial_file_a_running_twin_is_writing(app, events):
    first = add(app, "https://fake.test/slow?delay=0.1")
    wait_for(events, first, lambda d: d["stage"] == "downloading")
    app.json("POST", f"/api/jobs/{first}/cancel")
    second = add(app, "https://fake.test/slow?delay=0.1")
    wait_for(events, second, lambda d: d["stage"] == "downloading")
    assert app.request("DELETE", f"/api/jobs/{first}")[0] == 204
    assert names(app.dest) == ["Fake slow video [slow-1].mp4.part"]
    assert wait_for(events, second, lambda d: d["state"] == "done")["size"] == 4_000_000


def test_remove_cleans_the_partial_file_after_a_restart(tmp_path):
    httpd, _, jobs, _ = start(tmp_path)
    try:
        job = jobs.add([{"url": "https://fake.test/slow?delay=0.1"}], "best")[0][0]
        end = time.time() + 10
        while not jobs.snapshot()["jobs"][0]["files"]:
            assert time.time() < end
            time.sleep(0.05)
    finally:
        httpd.shutdown()
        jobs.close()
    httpd, _, jobs, _ = start(tmp_path)
    try:
        jobs.remove(job["id"])
        assert names(tmp_path / "downloads") == []
    finally:
        httpd.shutdown()
        jobs.close()


def test_remove_cleans_up_in_a_folder_reached_through_a_link(app, events, tmp_path):
    real = tmp_path / "Real"
    real.mkdir()
    (tmp_path / "Link").symlink_to(real, target_is_directory=True)
    app.json("PUT", "/api/settings", {"dest": str(tmp_path / "Link")})
    job_id = add(app, "https://fake.test/403?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    assert names(real) == ["Fake refused video [refused-1].f137.mp4.part"]
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert names(real) == []


def test_remove_keeps_an_mp3_an_earlier_run_of_the_job_finished(tmp_path):
    from spool.jobs import remove_leftovers

    mp3 = tmp_path / "Song [a].mp3"
    mp3.write_bytes(b"finished")
    job = {"files": [str(tmp_path / "Song [a].webm")], "preset": "mp3", "state": "cancelled", "dest": str(tmp_path)}
    for started in (None, time.time() + 60):  # finished, then Download again, then Cancel: waiting, or just begun
        remove_leftovers({**job, "started": started}, set())
        assert mp3.exists()  # written before this run began: the earlier run's finished file
    job["started"] = time.time() - 30
    remove_leftovers(job, set())
    assert not mp3.exists()  # written during this run: the half-written file a cancel leaves


def test_remove_cleans_the_pieces_of_a_stream_fetched_several_at_once(tmp_path):
    from spool.jobs import remove_leftovers

    dest = tmp_path / "Spool"
    dest.mkdir()
    name = dest / "Clip [1-2] [ab].f137.mp4"  # a title glob would read as patterns
    pieces = [f"{name.name}.part-Frag1", f"{name.name}.part-Frag12.part", f"{name.name}.ytdl", f"{name.name}.part"]
    kept = [
        "Clip [1-2] [ab].f137.mp4.part-Frag",  # no number
        "Clip [1-2] [ab].f137.mp4.part-Frag1x",
        "Clip [1-2] [ab].f137.mp4.part-Frag1.part.mp4",
        "Clip [1-2] [ab].f137.mp4.part-Frag1.mp4",
        "Other [x].mp4.part-Frag1",  # another job's
        "Clip [a].f137.mp4.part-Frag1",
    ]
    for f in pieces + kept:
        (dest / f).write_bytes(b"x")
    (dest / "Clip [1-2] [ab].f137.mp4.part-Frag2").mkdir()  # never a folder
    outside = tmp_path / f"{name.name}.part-Frag1"  # beside the folder, not in it
    outside.write_bytes(b"x")
    job = {"files": [str(name)], "preset": "best", "state": "cancelled", "dest": str(dest), "started": None}
    remove_leftovers(job, set())
    assert names(dest) == sorted(kept + ["Clip [1-2] [ab].f137.mp4.part-Frag2"])
    assert outside.exists()


def test_remove_keeps_the_pieces_a_running_twin_is_writing(tmp_path):
    from spool.jobs import remove_leftovers

    name = tmp_path / "Clip [a].mp4"
    (tmp_path / "Clip [a].mp4.part-Frag3").write_bytes(b"x")
    job = {"files": [str(name)], "preset": "best", "state": "cancelled", "dest": str(tmp_path), "started": None}
    remove_leftovers(job, {str(name)})
    assert names(tmp_path) == ["Clip [a].mp4.part-Frag3"]


def test_the_finished_file_is_never_a_piece(tmp_path):
    from spool.jobs import remove_leftovers

    name = tmp_path / "Clip [a].mp4"
    finished = tmp_path / "Clip [a].mp4.part-Frag1"  # a title that happens to end so
    finished.write_bytes(b"done")
    job = {"files": [str(name)], "preset": "best", "state": "done", "dest": str(tmp_path), "started": None}
    remove_leftovers(job, {str(finished)})
    assert finished.exists()
