"""ux/plan.md section 9: what the page relies on."""

import os
import time

import pytest
from serve_fake import FakeGitHub
from support import job_state

from spool import system


@pytest.fixture
def events(app):
    events = app.events()
    events.next()
    yield events
    events.close()


def add(app, url, preset="best", entry=None):
    status, body = app.json("POST", "/api/jobs", {"entries": [entry or {"url": url}], "preset": preset})
    assert status == 201, body
    return body


def finished(events, job_id):
    return events.until(lambda e, d: e == "job" and d["id"] == job_id and d["state"] in ("done", "failed"))


# One percentage


def test_progress_is_one_rising_number_across_video_and_audio(app, events):
    job = add(app, "https://fake.test/video?delay=0.08")["jobs"][0]  # each part spans several 0.25 s ticks
    seen = finished(events, job["id"])
    running = [d for e, d in seen if e == "job" and d["id"] == job["id"] and d["progress"] is not None]
    progress = [d["progress"] for d in running]
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert seen[-1][1]["state"] == "done"
    whats = [d["what"] for d in running if d["state"] == "running" and d["stage"] == "downloading"]
    assert whats[0] == "video" and whats[-1] == "audio"
    by_what = {d["what"]: d["progress"] for d in running if d["stage"] == "downloading"}
    assert by_what["video"] < 0.9  # the 3 MB video is most of the 3.4 MB job, but not all of it


def test_an_audio_job_says_audio(app, events):
    job = add(app, "https://fake.test/audio?delay=0.05", "audio")["jobs"][0]
    seen = finished(events, job["id"])
    assert {d["what"] for e, d in seen if e == "job" and d["stage"] == "downloading"} == {"audio"}


def test_youtube_dl_progress_rises_too(app, events):
    app.json("PUT", "/api/settings", {"engine": "youtube-dl"})
    job = add(app, "https://fake.test/video?delay=0.02")["jobs"][0]
    seen = finished(events, job["id"])
    progress = [d["progress"] for e, d in seen if e == "job" and d["progress"] is not None]
    assert progress == sorted(progress) and progress[-1] == 1.0


def test_the_join_says_how_far_it_is(app, events):
    job = add(app, "https://fake.test/video?delay=0.02&join=0.15")["jobs"][0]
    seen = finished(events, job["id"])
    assert seen[-1][1]["state"] == "done"
    mine = [d for e, d in seen if e == "job" and d["id"] == job["id"]]
    join = [d for d in mine if d["stage"] == "processing" and d["what"] == "join"]
    assert len(join) >= 2, [(d["stage"], d["what"]) for d in mine]
    assert {d["total"] for d in join} == {3_400_000}
    written = [d["downloaded"] for d in join]
    assert written == sorted(written) and written[-1] > 0
    assert any(d["speed"] for d in join)
    progress = [d["progress"] for d in mine if d["progress"] is not None]
    assert progress == sorted(progress)
    assert all(d["what"] in ("join", "tags", "mp3", None) for d in mine if d["stage"] == "processing")


def test_a_join_that_fails_with_the_tags_is_tried_once_more_the_plain_way(app, events):
    job = add(app, "https://fake.test/video?delay=0&fail=join-args")["jobs"][0]
    done = finished(events, job["id"])[-1][1]
    assert done["state"] == "done", done["error"]
    assert os.path.exists(done["path"])
    log = app.request("GET", f"/api/jobs/{job['id']}/log")[1].decode().splitlines()
    assert len([line for line in log if line.startswith("[download] Destination: ")]) == 2  # each part fetched once
    assert len([line for line in log if line.startswith("[spool] ")]) == 1  # the quick way failed, the plain way ran
    name = os.path.basename(done["path"])
    assert f'[Metadata] Adding metadata to "{name}"' in log  # the second pass wrote the details
    assert "[fake] details in the file: True" in log


# Engines without choices


def test_a_probe_falls_back_to_the_other_engine(app):
    status, probe = app.json("POST", "/api/probe", {"url": "https://fake.test/ytdl-only"})
    assert (status, probe["engine"]) == (200, "youtube-dl")


def test_a_job_added_without_a_probe_falls_back_once(app, events):
    job = add(app, "https://fake.test/ytdl-only?delay=0")["jobs"][0]
    assert job["engine"] == "yt-dlp"
    done = finished(events, job["id"])[-1][1]
    assert (done["state"], done["engine"]) == ("done", "youtube-dl")


def test_a_link_neither_engine_reads_fails_once_on_each(app, events):
    job = add(app, "https://fake.test/unsupported")["jobs"][0]
    failed = finished(events, job["id"])[-1][1]
    assert failed["error"] == {"kind": "unsupported", "message": "Spool can't read this link.", "hint": None}
    assert app.request("GET", f"/api/jobs/{job['id']}/log")[1].decode().count("--- ") == 2


@pytest.fixture
def github():  # a newer engine is published but fails its checksum, so it is never installed
    return FakeGitHub(corrupt=True)


def test_a_refusal_offers_the_update_when_a_newer_engine_exists(app, events):
    end = time.time() + 10
    while app.engines.status()["yt-dlp"]["busy"]:
        assert time.time() < end
        time.sleep(0.05)
    job = add(app, "https://fake.test/403?delay=0")["jobs"][0]
    failed = finished(events, job["id"])[-1][1]
    assert failed["error"] == {"kind": "blocked", "message": "The site refused the download.", "hint": "update_engine"}


# The list


def test_adding_what_is_already_in_the_list_adds_nothing(app, events):
    entry = {"url": "https://fake.test/video?delay=0", "id": "vid-1", "title": "V"}
    first = add(app, None, entry=entry)
    assert first["existing"] == 0
    finished(events, first["jobs"][0]["id"])
    again = add(app, None, entry=entry)
    assert (again["existing"], again["jobs"][0]["id"]) == (1, first["jobs"][0]["id"])
    other = add(app, None, "720", entry=entry)
    assert other["existing"] == 0
    os.remove(app.jobs.snapshot()["jobs"][1]["path"])  # the first file is gone: adding it again is new
    assert add(app, None, entry=entry)["existing"] == 0


def test_clear_takes_finished_rows_off_the_list_and_keeps_their_files(app, events):
    done = finished(events, add(app, "https://fake.test/video?delay=0")["jobs"][0]["id"])[-1][1]
    later = finished(events, add(app, "https://fake.test/video/2?delay=0")["jobs"][0]["id"])[-1][1]
    failed = finished(events, add(app, "https://fake.test/unsupported")["jobs"][0]["id"])[-1][1]
    assert app.json("POST", "/api/jobs/clear", {"ids": [done["id"], failed["id"], "nope"]})[0] == 204
    assert events.until(lambda e, d: e == "job_removed")[-1][1] == {"id": done["id"]}
    assert [j["id"] for j in app.jobs.snapshot()["jobs"]] == [failed["id"], later["id"]]
    assert os.path.exists(done["path"])
    assert app.json("POST", "/api/jobs/clear", {"state": "done"})[0] == 422


# The folder


def test_the_folder_opens_in_the_file_manager(app, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(system, "open_folder", opened.append)  # never a real window
    assert app.request("POST", "/api/dest/reveal")[0] == 204
    assert opened == [str(app.dest)]
    app.json("PUT", "/api/settings", {"dest": str(tmp_path / "gone")})
    status, body = app.json("POST", "/api/dest/reveal")
    assert (status, body["error"]["kind"]) == (409, "dest_missing")
    assert opened == [str(app.dest)]


def test_a_missing_folder_can_be_created_one_level_deep(app, tmp_path):
    status, body = app.json("PUT", "/api/settings", {"dest": str(tmp_path / "New"), "create": True})
    assert (status, body["dest"]["ok"]) == (200, True)
    assert (tmp_path / "New").is_dir()
    status, body = app.json("PUT", "/api/settings", {"dest": str(tmp_path / "a" / "b"), "create": True})
    assert (status, body["dest"]["problem"]) == (200, "missing")
    assert not (tmp_path / "a").exists()
    assert app.json("PUT", "/api/settings", {"dest": str(tmp_path), "create": "yes"})[0] == 422


def test_a_folder_problem_carries_its_sentence(app, tmp_path):
    body = app.json("PUT", "/api/settings", {"dest": str(tmp_path / "Gone")})[1]
    assert body["dest"]["message"] == f"{system.display(tmp_path / 'Gone')} doesn't exist. Choose another folder."
    events = app.events()
    snap = events.until(lambda e, d: e == "snapshot")[-1][1]
    events.close()
    assert snap["dest"]["message"] == body["dest"]["message"]


def test_the_interrupted_job_can_resume(tmp_path):
    from serve_fake import start

    httpd, _, jobs, _ = start(tmp_path)
    job = jobs.add([{"url": "https://fake.test/slow?delay=0.1"}], "best")[0][0]
    end = time.time() + 10
    while not jobs.snapshot()["jobs"][0]["downloaded"]:
        assert time.time() < end
        time.sleep(0.05)
    httpd.shutdown()
    jobs.close()
    httpd, _, jobs, _ = start(tmp_path)
    try:
        assert jobs.snapshot()["jobs"][0]["error"] == {
            "kind": "interrupted",
            "message": "Spool was closed before this download finished.",
            "hint": "resume",
        }
        jobs.retry(job["id"])
        end = time.time() + 15
        while jobs.snapshot()["jobs"][0]["state"] != "done":
            assert time.time() < end
            time.sleep(0.05)
        assert "Resuming download at byte" in jobs.log(job["id"])
    finally:
        httpd.shutdown()
        jobs.close()


def test_a_finished_job_reports_done_with_full_progress(app, events):
    done = events.until(job_state("done")) if add(app, "https://fake.test/audio?delay=0", "audio") else None
    assert done[-1][1]["progress"] == 1.0


# What the worker adds rides through unchanged


def test_a_presets_file_type_and_a_lists_truncated_reach_the_page(app, monkeypatch, tmp_path):
    import json

    import spool.jobs

    preset = {"id": "best", "label": "Best", "detail": "1080p", "size": 3_400_000, "ext": "mkv"}
    entry = {"url": "https://fake.test/v/1", "id": "v1", "title": "One", "presets": [preset]}
    info = {"event": "info", "kind": "playlist", "title": "A channel", "entries": [entry], "truncated": True}
    worker = tmp_path / "worker.py"
    worker.write_text(f"import sys\nsys.stdin.read()\nprint({json.dumps(json.dumps(info))})\n")
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/channel"})
    assert status == 200
    assert (body["kind"], body["title"], body["truncated"]) == ("playlist", "A channel", True)
    assert body["entries"] == [entry]
    info["truncated"] = False
    worker.write_text(f"import sys\nsys.stdin.read()\nprint({json.dumps(json.dumps(info))})\n")
    assert app.json("POST", "/api/probe", {"url": "https://fake.test/channel"})[1]["truncated"] is False


def test_a_probe_from_a_worker_that_says_nothing_of_truncation_is_not_truncated(app):
    assert app.json("POST", "/api/probe", {"url": "https://fake.test/video"})[1]["truncated"] is False
