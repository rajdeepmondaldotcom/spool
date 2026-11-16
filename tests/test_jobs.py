import os
import time

import pytest
from serve_fake import start

from spool.jobs import classify


def add(app, url, preset="best", **extra):
    entries = app.json("POST", "/api/probe", {"url": url})[1]["entries"]
    entries[0]["url"] = url  # keep the scripted delay
    status, body = app.json("POST", "/api/jobs", {"entries": entries, "preset": preset, **extra})
    assert status == 201, body
    return body["jobs"][0]["id"]


def wait_for(events, job_id, match):
    return events.until(lambda e, d: e == "job" and d["id"] == job_id and match(d))[-1][1]


def downloading(d):
    return d["state"] == "running" and d["stage"] == "downloading"


def gone(pid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):  # macOS answers EPERM when only zombies are left
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def events(app):
    events = app.events()
    events.next()
    yield events
    events.close()


# Cancel


def test_cancel_stops_the_whole_process_group_and_keeps_the_partial_file(app, events):
    job_id = add(app, "https://fake.test/slow?delay=0.3")
    wait_for(events, job_id, downloading)
    pid = app.jobs.procs[job_id].pid
    status, job = app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert (status, job["state"], job["stage"]) == (200, "cancelled", None)
    assert gone(pid)  # the worker and the helper it started
    assert [p.name for p in app.dest.iterdir()] == ["Fake slow video [slow-1].mp4.part"]


def test_cancel_a_queued_job(app, events):
    ids = [add(app, f"https://fake.test/slow/{n}?delay=0.3") for n in (1, 2, 3)]
    status, job = app.json("POST", f"/api/jobs/{ids[2]}/cancel")
    assert (status, job["state"]) == (200, "cancelled")


def test_cancel_only_what_is_waiting_or_running(app, events):
    job_id = add(app, "https://fake.test/video?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "done")
    assert app.json("POST", f"/api/jobs/{job_id}/cancel")[0] == 409
    assert app.json("POST", "/api/jobs/nope/cancel")[0] == 404


# Retry


def test_retry_resumes_the_partial_file(app, events):
    job_id = add(app, "https://fake.test/slow?delay=0.1")
    wait_for(events, job_id, downloading)
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert gone(app.jobs.procs[job_id].pid) if job_id in app.jobs.procs else True
    status, job = app.json("POST", f"/api/jobs/{job_id}/retry")
    assert (status, job["state"], job["error"]) == (200, "queued", None)
    done = wait_for(events, job_id, lambda d: d["state"] == "done")
    assert done["size"] == 4_000_000
    log = app.request("GET", f"/api/jobs/{job_id}/log")[1].decode()
    assert "Resuming download at byte" in log


def test_retry_can_switch_the_engine(app, events):
    job_id = add(app, "https://fake.test/403?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    status, job = app.json("POST", f"/api/jobs/{job_id}/retry", {"engine": "youtube-dl"})
    assert (status, job["engine"]) == (200, "youtube-dl")


def test_retry_refuses_a_job_that_is_still_going(app, events):
    job_id = add(app, "https://fake.test/slow?delay=0.3")
    assert app.json("POST", f"/api/jobs/{job_id}/retry")[0] == 409
    assert app.json("POST", f"/api/jobs/{job_id}/retry", {"engine": "nope"})[0] in (409, 422)
    assert app.json("POST", "/api/jobs/nope/retry")[0] == 404


# Remove


def test_remove_cleans_partial_leftovers(app, events):
    job_id = add(app, "https://fake.test/403?delay=0")
    failed = wait_for(events, job_id, lambda d: d["state"] == "failed")
    assert failed["error"]["kind"] == "blocked"
    assert [p.name for p in app.dest.iterdir()] == ["Fake refused video [refused-1].f137.mp4.part"]
    (app.dest / "Other [x].mp4").write_text("someone else's")
    status, _, _ = app.request("DELETE", f"/api/jobs/{job_id}")
    assert status == 204
    assert events.until(lambda e, d: e == "job_removed")[-1][1] == {"id": job_id}
    assert [p.name for p in app.dest.iterdir()] == ["Other [x].mp4"]
    assert all(j["id"] != job_id for j in app.jobs.snapshot()["jobs"])


def test_remove_never_touches_the_finished_file(app, events):
    job_id = add(app, "https://fake.test/video?delay=0")
    done = wait_for(events, job_id, lambda d: d["state"] == "done")
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert os.path.exists(done["path"])


def test_remove_refuses_a_job_that_is_still_going(app, events):
    job_id = add(app, "https://fake.test/slow?delay=0.3")
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 409
    assert app.request("DELETE", "/api/jobs/nope")[0] == 404


# Log


def test_the_log_is_the_engines_own_words(app, events):
    job_id = add(app, "https://fake.test/403?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    status, raw, headers = app.request("GET", f"/api/jobs/{job_id}/log")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert "HTTP Error 403: Forbidden" in raw.decode()
    assert app.request("GET", "/api/jobs/nope/log")[0] == 404


# Persistence and restart


def test_jobs_survive_a_restart_and_a_job_caught_mid_download_becomes_interrupted(tmp_path):
    httpd, url, jobs, engines = start(tmp_path)
    done = jobs.add([{"url": "https://fake.test/video?delay=0", "id": "vid-1", "title": "Done"}], "best")[0]
    end = time.time() + 10
    while jobs.snapshot()["jobs"][0]["state"] != "done":
        assert time.time() < end
        time.sleep(0.05)
    cut = jobs.add([{"url": "https://fake.test/slow?delay=0.3", "id": "slow-1", "title": "Cut"}], "best")[0]
    time.sleep(0.5)
    httpd.shutdown()
    jobs.close()

    httpd, url, jobs, engines = start(tmp_path)
    try:
        by_id = {j["id"]: j for j in jobs.snapshot()["jobs"]}
        assert by_id[done["id"]]["state"] == "done"
        assert by_id[done["id"]]["file_exists"] is True
        assert by_id[cut["id"]]["state"] == "failed"
        assert by_id[cut["id"]]["error"]["kind"] == "interrupted"
    finally:
        httpd.shutdown()
        jobs.close()


# Plain-word errors


def test_a_youtube_failure_without_a_javascript_runtime_says_so(app, events):
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/needs-js"})
    assert (status, body["error"]["kind"], body["error"]["hint"]) == (422, "engine", "install_js_runtime")
    assert "JavaScript" in body["error"]["message"]
    assert body["error"]["message"].endswith("The engine said: Requested format is not available.")
    job = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/needs-js"}], "preset": "best"})
    failed = wait_for(events, job[1]["jobs"][0]["id"], lambda d: d["state"] == "failed")
    assert (failed["error"]["kind"], failed["error"]["hint"]) == ("engine", "install_js_runtime")


def test_a_probe_that_fails_in_the_engine_carries_its_last_error_line_cleaned(app):
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/broken"})
    assert (status, body["error"]["kind"], body["error"]["hint"]) == (422, "engine", "update_engine")
    assert body["error"]["message"] == "The engine failed: Something new broke."


def test_probe_errors_have_a_kind_and_plain_words(app):
    for url, kind in (
        ("https://fake.test/unavailable", "unavailable"),
        ("https://fake.test/unsupported", "unsupported"),
    ):
        status, body = app.json("POST", "/api/probe", {"url": url})
        assert (status, body["error"]["kind"]) == (422, kind)
        assert "ERROR" not in body["error"]["message"]


@pytest.mark.parametrize(
    "message, kind, hint",
    [
        (
            "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser",
            "blocked",
            "update_engine",
        ),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "blocked", "update_engine"),
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
            "unavailable",
            None,
        ),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable", None),
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
            "unavailable",
            None,
        ),
        ("ERROR: Unsupported URL: https://example.com/", "unsupported", "try_other_engine"),
        (
            "ERROR: [generic] Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided, or not known>",
            "network",
            None,
        ),
        (
            "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location",
            "no_ffmpeg",
            "install_ffmpeg",
        ),
        (
            "ERROR: unable to open for writing: [Errno 13] Permission denied: '/x/T [a].mp4.part'",
            "dest_denied",
            "check_dest",
        ),
        ("ERROR: [youtube] abc: Some new breakage", "engine", "update_engine"),
        ("ERROR: unable to write data: [Errno 28] No space left on device", "dest_full", "check_dest"),
        ("ERROR: [Errno 30] Read-only file system: '/Volumes/T7/Spool/x.part'", "dest_denied", "check_dest"),
        ("[Errno 63] File name too long: '/x/a very long name.mp4'", "engine", None),
        ("ERROR: unable to open for writing: [Errno 2] No such file or directory: '/x/T.part'", "engine", None),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 61] Connection refused>", "network", None),
        # from the tester's runs and the engines' own source
        ("ERROR: [youtube] abc: This video is unavailable", "unavailable", None),
        ("ERROR: [youtube] abc: Video unavailable", "unavailable", None),
        (
            "ERROR: [vimeo] 123: This video is only available for registered users. Use --cookies, "
            "--cookies-from-browser, --username and --password, --netrc-cmd, or --netrc (vimeo) to provide account credentials",
            "unavailable",
            None,
        ),
        (
            "ERROR: [instagram] abc: Requested content is not available, rate-limit reached or login required",
            "unavailable",
            None,
        ),
        ("ERROR: [twitter] 1: You need to log in to view this content", "unavailable", None),
        ("ERROR: [patreon] 1: You need to be logged-in to access this", "unavailable", None),
        (
            "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video, and other exclusive perks.",
            "unavailable",
            None,
        ),
        (
            "No supported JavaScript runtime could be found. "
            "ERROR: [youtube] KXItezz-BhA: Requested format is not available. Use --list-formats for a list of available formats",
            "engine",
            "install_js_runtime",
        ),
        (
            "ERROR: Unable to rename file: [Errno 2] No such file or directory: '/x/out/race/Me at the zoo [jNQXAC9IVRw].m4a.part'",
            "engine",
            None,
        ),
    ],
)
def test_engine_messages_map_to_kinds_and_hints(message, kind, hint):
    got_kind, words, got_hint = classify(message)
    assert (got_kind, got_hint) == (kind, hint)
    assert words and "ERROR:" not in words


# Details a job learns while downloading


def test_a_job_added_without_a_probe_fills_in_its_details_from_the_engine(app, events):
    status, body = app.json(
        "POST", "/api/jobs", {"entries": [{"url": "https://fake.test/video?delay=0"}], "preset": "best"}
    )
    job = body["jobs"][0]
    assert job["title"] == "https://fake.test/video?delay=0"
    done = wait_for(events, job["id"], lambda d: d["state"] == "done")
    assert (done["title"], done["uploader"], done["duration"]) == ("Fake video 1", "Fake Channel", 95)
    assert done["thumbnail"].startswith("data:image/svg+xml")


def test_details_the_entry_supplied_are_never_overwritten(app, events):
    entry = {"url": "https://fake.test/video?delay=0", "title": "My name for it", "duration": 7}
    job = app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})[1]["jobs"][0]
    done = wait_for(events, job["id"], lambda d: d["state"] == "done")
    assert (done["title"], done["duration"], done["uploader"]) == ("My name for it", 7, "Fake Channel")


# Files and names


def test_two_presets_of_one_video_write_two_files(app, events):
    ids = [add(app, "https://fake.test/video?delay=0", preset) for preset in ("best", "720", "audio")]
    done = [wait_for(events, i, lambda d: d["state"] == "done") for i in ids]
    names = [os.path.basename(d["path"]) for d in done]
    assert names == ["Fake video 1 [vid-1].mp4", "Fake video 1 [vid-1] 720p.mp4", "Fake video 1 [vid-1].m4a"]


def test_a_job_identical_to_one_still_going_is_that_job(app, events):
    first = add(app, "https://fake.test/slow?delay=0.3")
    assert add(app, "https://fake.test/slow?delay=0.3") == first
    assert add(app, "https://fake.test/slow?delay=0.3", "720") != first
    assert len(app.jobs.snapshot()["jobs"]) == 2


def test_remove_cleans_what_the_engine_wrote_even_with_a_slash_in_the_id(app, events):
    job_id = add(app, "https://fake.test/403/a%2Fb?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    assert [p.name for p in app.dest.iterdir()] == ["Fake refused video [refused-a_b].f137.mp4.part"]
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert list(app.dest.iterdir()) == []


def test_remove_cleans_a_job_added_from_a_pasted_link(app, events):
    job = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/403?delay=0"}], "preset": "best"})
    job_id = job[1]["jobs"][0]["id"]
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert list(app.dest.iterdir()) == []


def test_remove_cleans_a_half_written_mp3(app, events):
    job_id = add(app, "https://fake.test/video?delay=0.1", "mp3")
    wait_for(events, job_id, lambda d: d["stage"] == "processing")
    time.sleep(0.2)  # the fake is writing the .mp3 now
    assert "Fake video 1 [vid-1].mp3" in [p.name for p in app.dest.iterdir()]
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert app.request("DELETE", f"/api/jobs/{job_id}")[0] == 204
    assert list(app.dest.iterdir()) == []


def test_remove_never_touches_another_jobs_finished_file(app, events):
    done = wait_for(events, add(app, "https://fake.test/audio?delay=0", "audio"), lambda d: d["state"] == "done")
    failed = add(app, "https://fake.test/403?delay=0")
    wait_for(events, failed, lambda d: d["state"] == "failed")
    assert app.request("DELETE", f"/api/jobs/{failed}")[0] == 204
    assert [p.name for p in app.dest.iterdir()] == [os.path.basename(done["path"])]


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
def test_a_live_stream_is_refused_while_it_is_live(app, events, engine):
    job = app.json(
        "POST", "/api/jobs", {"entries": [{"url": "https://fake.test/live"}], "preset": "best", "engine": engine}
    )
    failed = wait_for(events, job[1]["jobs"][0]["id"], lambda d: d["state"] in ("done", "failed"))
    assert failed["error"] == {"kind": "live", "message": "This is live now. Download it after it ends.", "hint": None}
    assert list(app.dest.iterdir()) == []


def test_a_job_for_a_playlist_item_downloads_that_item_only(app, events):
    entries = app.json("POST", "/api/probe", {"url": "https://fake.test/shared-playlist"})[1]["entries"]
    job = app.json("POST", "/api/jobs", {"entries": [entries[1]], "preset": "best"})[1]["jobs"][0]
    assert (job["url"], job["item"]) == ("https://fake.test/shared-playlist", 2)
    done = wait_for(events, job["id"], lambda d: d["state"] == "done")
    assert [p.name for p in app.dest.iterdir()] == [os.path.basename(done["path"])] == ["Shared item 2 [shared-2].mp4"]
    other = app.json("POST", "/api/jobs", {"entries": [entries[0]], "preset": "best"})[1]["jobs"][0]
    assert other["id"] != job["id"]
