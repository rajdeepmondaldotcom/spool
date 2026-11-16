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
