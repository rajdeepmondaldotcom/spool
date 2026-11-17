import json
import os
import subprocess
import sys
import time

import pytest
from serve_fake import start
from support import Held, WriteTests, until

from spool.jobs import classify, error


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


def gone(proc, timeout=5):
    """The worker and, off Windows, everything in its process group have exited."""
    if sys.platform == "win32":  # no process groups to probe: only the worker itself is checked there
        try:
            proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
    pid = proc.pid
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


def post(app, url, id="v", preset="best"):
    status, body = app.json("POST", "/api/jobs", {"entries": [{"url": url, "id": id}], "preset": preset})
    assert status == 201, body
    return body["jobs"][0]["id"]


def test_cancel_stops_the_whole_process_group_and_keeps_the_partial_file(app, monkeypatch, tmp_path):
    worker, checks = Held(monkeypatch, tmp_path), WriteTests(monkeypatch)
    job_id = post(app, "https://fake.test/hold/child")
    worker.held()  # it started a helper of its own and is mid-file
    proc = app.jobs.procs[job_id]
    checks.arm()  # from here, any write test Spool makes on the folder leaves its file there
    status, job = app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert (status, job["state"], job["stage"]) == (200, "cancelled", None)
    assert gone(proc)  # the worker and the helper it started
    until(lambda: job_id not in app.jobs.procs)  # Spool is done with the job: nothing of its is in flight
    assert [p.name for p in app.dest.iterdir()] == ["Same video [v].mp4.part"]


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


def test_retry_resumes_the_partial_file(app, events, monkeypatch, tmp_path):
    worker = Held(monkeypatch, tmp_path)
    job_id = post(app, "https://fake.test/hold")
    worker.held()
    proc = app.jobs.procs[job_id]
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert gone(proc)  # the retry resumes what the first run left, not a file still being written
    worker.go()
    status, job = app.json("POST", f"/api/jobs/{job_id}/retry")
    assert (status, job["state"], job["error"]) == (200, "queued", None)
    done = wait_for(events, job_id, lambda d: d["state"] == "done")
    assert done["size"] == 100 + 300  # the first run's bytes, then the whole of the second
    log = app.request("GET", f"/api/jobs/{job_id}/log")[1].decode()
    assert "Resuming download at byte 100" in log


def test_retry_can_switch_the_engine(app, events):
    job_id = add(app, "https://fake.test/403?delay=0")
    wait_for(events, job_id, lambda d: d["state"] == "failed")
    status, job = app.json("POST", f"/api/jobs/{job_id}/retry", {"engine": "youtube-dl"})
    assert (status, job["engine"]) == (200, "youtube-dl")


def test_retry_refuses_a_job_that_is_still_going(app, events):
    job_id = add(app, "https://fake.test/slow?delay=0.3")
    assert app.json("POST", f"/api/jobs/{job_id}/retry")[0] == 409
    assert app.json("POST", f"/api/jobs/{job_id}/retry", {"engine": "nope"})[0] == 409
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
    done = jobs.add([{"url": "https://fake.test/video?delay=0", "id": "vid-1", "title": "Done"}], "best")[0][0]
    end = time.time() + 10
    while jobs.snapshot()["jobs"][0]["state"] != "done":
        assert time.time() < end
        time.sleep(0.05)
    cut = jobs.add([{"url": "https://fake.test/slow?delay=0.3", "id": "slow-1", "title": "Cut"}], "best")[0][0]
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


def test_a_youtube_failure_without_a_javascript_runtime_is_no_js(app, events):
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/needs-js"})
    assert (status, body["error"]["kind"], body["error"]["hint"]) == (422, "no_js", "retry")
    job = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/needs-js"}], "preset": "best"})
    failed = wait_for(events, job[1]["jobs"][0]["id"], lambda d: d["state"] == "failed")
    assert (failed["error"]["kind"], failed["error"]["hint"]) == ("no_js", "retry")


def test_a_probe_that_fails_in_the_engine_asks_to_try_again(app):
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/broken"})
    assert (status, body["error"]) == (
        422,
        {"kind": "engine", "message": "Spool hit an error it doesn't recognise. Try again.", "hint": "retry"},
    )


def test_probe_errors_have_a_kind_and_plain_words(app):
    for url, kind in (
        ("https://fake.test/unavailable", "unavailable"),
        ("https://fake.test/unsupported", "unsupported"),
    ):
        status, body = app.json("POST", "/api/probe", {"url": url})
        assert (status, body["error"]["kind"]) == (422, kind)
        assert "ERROR" not in body["error"]["message"]


@pytest.mark.parametrize(
    "message, kind",
    [
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", "limited"),
        ("ERROR: [youtube] abc: Sign in to confirm you're not a bot. This helps protect our community.", "limited"),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "limited"),
        ("ERROR: [instagram] abc: Too Many Requests", "limited"),
        ("ERROR: [youtube] abc: This content isn't available, try again later.", "limited"),
        ("ERROR: [youtube] abc: This content isn’t available, try again later.", "limited"),
        ("ERROR: [youtube] abc: This content isn\ufffdt available, try again later.", "limited"),  # a mark lost
        ("ERROR: [youtube] abc: Sign in to confirm you\ufffdre not a bot.", "limited"),
        ("ERROR: [youtube] abc: Sign in to confirm you`re not a bot.", "limited"),
        ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate", "signin"),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "signin"),
        ("ERROR: [youtube] abc: This video is available to this channel's members-only content", "signin"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "blocked"),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video", "signin"),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable"),
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
            "signin",
        ),
        ("ERROR: Unsupported URL: https://example.com/", "unsupported"),
        (
            "ERROR: [generic] Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided, or not known>",
            "network",
        ),
        (
            "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location",
            "no_ffmpeg",
        ),
        ("ERROR: unable to open for writing: [Errno 13] Permission denied: '/x/T [a].mp4.part'", "dest_denied"),
        ("ERROR: [youtube] abc: Some new breakage", "engine"),
        ("ERROR: unable to write data: [Errno 28] No space left on device", "dest_full"),
        ("ERROR: [Errno 30] Read-only file system: '/Volumes/T7/Spool/x.part'", "dest_denied"),
        ("[Errno 63] File name too long: '/x/a very long name.mp4'", "engine"),
        ("ERROR: unable to open for writing: [Errno 2] No such file or directory: '/x/T.part'", "engine"),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 61] Connection refused>", "network"),
        ("ERROR: [youtube] abc: This video is unavailable", "unavailable"),
        ("ERROR: [youtube] abc: Video unavailable", "unavailable"),
        (
            "ERROR: [vimeo] 123: This video is only available for registered users. Use --cookies, --cookies-from-browser, --username and --password, --netrc-cmd, or --netrc (vimeo) to provide account credentials",
            "signin",
        ),
        (
            "ERROR: [instagram] abc: Requested content is not available, rate-limit reached or login required",
            "signin",
        ),
        ("ERROR: [twitter] 1: You need to log in to view this content", "signin"),
        ("ERROR: [patreon] 1: You need to be logged-in to access this", "signin"),
        (
            "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video, and other exclusive perks.",
            "signin",
        ),
        (
            "No supported JavaScript runtime could be found. ERROR: [youtube] KXItezz-BhA: Requested format is not available. Use --list-formats for a list of available formats",
            "no_js",
        ),
        (
            "ERROR: Unable to rename file: [Errno 2] No such file or directory: '/x/out/race/Me at the zoo [jNQXAC9IVRw].m4a.part'",
            "engine",
        ),
        (  # a title in a path is not the engine's verdict
            "ERROR: unable to open for writing: [Errno 13] Permission denied: '/x/Why this video is unavailable [a].mp4.part'",
            "dest_denied",
        ),
        (
            "ERROR: Unable to rename file: [Errno 2] No such file or directory: '/x/HTTP Error 403 explained [a].m4a.part' -> '/x/HTTP Error 403 explained [a].m4a'",
            "engine",
        ),
        (
            'ERROR: [Errno 30] Read-only file system: "/Volumes/T7/Don\'t sign in [a].mp4.part"',
            "dest_denied",
        ),
        (
            "ERROR: [vimeo] 1: Requested format is not available. Use --list-formats for a list of available formats",
            "engine",
        ),
    ],
)
def test_engine_messages_map_to_kinds(message, kind):
    assert classify(message) == kind


def test_a_kind_the_worker_names_is_used_as_is(app, events, monkeypatch, tmp_path):
    import spool.jobs

    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        'print(\'{"event": "error", "message": "HTTP Error 403 in a list", "kind": "playlist"}\')\n'
    )
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    job_id = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/playlist"}], "preset": "best"})[1]
    failed = wait_for(events, job_id["jobs"][0]["id"], lambda d: d["state"] in ("done", "failed"))
    assert failed["error"] == {
        "kind": "playlist",
        "message": "This link is a list of videos. Paste it in the link field to choose from it.",
        "hint": None,
    }
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/playlist"})
    assert (status, body["error"]["kind"]) == (422, "playlist")


@pytest.mark.parametrize(
    "kind, name, message, hint",
    [  # the error table in ux/plan.md, word for word
        ("invalid_link", None, "That doesn't look like a link. Paste a video's web address.", None),
        ("too_long", None, "That link is too long for Spool.", None),
        ("unsupported", None, "Spool can't read this link.", None),
        ("empty", None, "Spool found no video at this link.", None),
        ("unavailable", None, "This video isn't available. It may be private or removed.", None),
        ("signin", None, "This video needs a signed-in account. Choose your browser in Settings.", "signin"),
        (
            "signin_unread",
            "Safari",
            "Spool couldn't read the sign-in from Safari. Choose another browser in Settings, or try again.",
            "signin",
        ),
        ("live", None, "This is live now. Download it after it ends.", None),
        ("blocked", None, "The site refused the download.", "retry"),
        (
            "limited",
            None,
            "The site is limiting this connection for now. Wait an hour or so, then try again.",
            "retry",
        ),
        ("network", None, "Spool couldn't reach the site. Check your internet connection.", "retry"),
        ("no_ffmpeg", None, "This quality needs a free helper to join video and audio.", "install_ffmpeg"),
        ("no_js", None, "Spool couldn't set up what it needs for YouTube. Check your internet connection.", "retry"),
        ("interrupted", None, "Spool was closed before this download finished.", "resume"),
        (
            "dest_unplugged",
            "T7 › Spool",
            "T7 › Spool isn't connected. Plug it in, or choose another folder.",
            "check_dest",
        ),
        ("dest_missing", "Movies › Spool", "Movies › Spool doesn't exist. Choose another folder.", "check_dest"),
        ("dest_not_folder", "a › b", "a › b is a file, not a folder. Choose another folder.", "check_dest"),
        ("dest_denied", "a › b", "Spool isn't allowed to save to a › b. Choose another folder.", "check_dest"),
        ("dest_full", "T7", "T7 is full. Free some space, or choose another folder.", "check_dest"),
        (
            "dest_not_responding",
            "T7",
            "T7 isn't answering. If macOS is asking whether Spool may use it, choose Allow.",
            "check_dest",
        ),
        ("engine", None, "The download failed.", "show_log"),
        ("engine_not_ready", None, "Spool is still setting up. Try again in a few seconds.", None),
        ("not_found", None, "Spool can't find that download.", None),
        ("gone", None, "Spool can't find the file. It was moved or deleted.", None),
        ("playlist", None, "This link is a list of videos. Paste it in the link field to choose from it.", None),
    ],
)
def test_every_error_is_the_sentence_in_the_plan(kind, name, message, hint):
    assert error(kind, name) == {"kind": kind, "message": message, "hint": hint}


def test_an_engine_error_in_a_probe_asks_to_try_again():
    assert error("engine", probe=True) == {
        "kind": "engine",
        "message": "Spool hit an error it doesn't recognise. Try again.",
        "hint": "retry",
    }


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
    done = {}  # two run at once, so they may finish in either order: wait for all in one pass
    events.until(lambda e, d: e == "job" and d["state"] == "done" and not done.update({d["id"]: d}) and len(done) == 3)
    names = [os.path.basename(done[i]["path"]) for i in ids]
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


def test_remove_cleans_a_half_written_mp3(app, monkeypatch, tmp_path):
    worker, checks = Held(monkeypatch, tmp_path), WriteTests(monkeypatch)
    job_id = post(app, "https://fake.test/hold/mp3", preset="mp3")
    worker.held()  # the .mp3 is half made
    assert "Same video [v].mp3" in [p.name for p in app.dest.iterdir()]
    checks.arm()
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    until(lambda: job_id not in app.jobs.procs)
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


# Queue


def workers():
    """This process's worker children that are still alive (zombies are done)."""
    out = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,stat=,command="], capture_output=True, text=True).stdout
    rows = [line.split(None, 3) for line in out.splitlines()]
    return [int(r[0]) for r in rows if r[1] == str(os.getpid()) and not r[2].startswith("Z") and "worker.py" in r[3]]


@pytest.mark.skipif(sys.platform == "win32", reason="ps")
def test_a_job_cancelled_as_it_starts_never_downloads_and_two_at_most_run(app, events, monkeypatch):
    from spool import system

    spawn, spawned, most = system.spawn, [], []

    def counted(*args, **kwargs):  # every worker Spool starts, and how many of its workers are alive then
        most.append(1 + sum(p.poll() is None for p in spawned))
        spawned.append(spawn(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(system, "spawn", counted)
    entry = {"url": "https://fake.test/slow?delay=0.2", "id": "slow-1", "title": "Slow"}
    for n in range(25):
        job = app.json(
            "POST",
            "/api/jobs",
            {"entries": [dict(entry, url=f"https://fake.test/slow/{n}?delay=0.2", id=f"slow-{n}")], "preset": "best"},
        )
        assert app.json("POST", f"/api/jobs/{job[1]['jobs'][0]['id']}/cancel")[1]["state"] == "cancelled"
    until(lambda: not workers())
    assert max(most, default=0) <= 2
    assert [j["state"] for j in app.jobs.snapshot()["jobs"]] == ["cancelled"] * 25


# Trying again by itself: test_waiting.py


# A damaged state file


@pytest.mark.parametrize(
    "text", ['{"settings": {"dest": "/x"}, "jo', "", "[1, 2]", '{"settings": 7, "jobs": {}}', '{"jobs": [1]}x']
)
def test_a_damaged_state_file_is_moved_aside_and_spool_starts_fresh(tmp_path, capsys, text):
    from spool.engines import Engines
    from spool.jobs import Jobs

    (tmp_path / "state.json").write_text(text)
    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"))
    try:
        assert jobs.snapshot()["jobs"] == []
        [aside] = tmp_path.glob("state.json.broken-*")
        assert aside.read_text() == text
        out = capsys.readouterr().out
        assert out.count("\n") == 1 and aside.name in out
    finally:
        jobs.close()


def test_a_malformed_job_record_is_skipped(tmp_path):
    from spool.engines import Engines
    from spool.jobs import Jobs, new_job

    good = new_job({"url": "https://fake.test/video", "title": "Good"}, "best", "yt-dlp", str(tmp_path))
    good["state"] = "done"
    missing_key = {k: v for k, v in good.items() if k != "state"}
    state = {"settings": {"dest": 7, "engine": ["x"], "preset": {}}, "jobs": [good, missing_key, "junk", None]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"))
    try:
        assert [j["title"] for j in jobs.snapshot()["jobs"]] == ["Good"]
        assert jobs.settings["engine"] == "yt-dlp" and isinstance(jobs.settings["dest"], str)
    finally:
        jobs.close()


# A job's thread never dies quietly


def test_a_worker_that_fails_to_start_fails_its_job_and_frees_its_slot(app, events, monkeypatch):
    from spool import system

    spawn = system.spawn
    calls = []

    def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(24, "Too many open files")
        return spawn(*args, **kwargs)

    monkeypatch.setattr(system, "spawn", once)
    first = add(app, "https://fake.test/video?delay=0")
    failed = wait_for(events, first, lambda d: d["state"] in ("done", "failed"))
    assert (failed["state"], failed["error"]["kind"]) == ("failed", "engine")
    assert "Too many open files" in app.request("GET", f"/api/jobs/{first}/log")[1].decode()
    second = add(app, "https://fake.test/video/2?delay=0")
    assert wait_for(events, second, lambda d: d["state"] in ("done", "failed"))["state"] == "done"
    assert app.jobs.procs == {}


def test_a_byte_that_is_not_utf8_from_the_worker_is_only_logged(app, events, monkeypatch, tmp_path):
    import spool.jobs

    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe ffmpeg noise\\n')\n"
        'sys.stdout.buffer.write(b\'{"event": "error", "message": "ERROR: Unsupported URL: x"}\\n\')\n'
    )
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/video"})
    assert (status, body["error"]["kind"]) == (422, "unsupported")
    job = app.json("POST", "/api/jobs", {"entries": [{"url": "https://fake.test/video"}], "preset": "best"})[1]
    failed = wait_for(events, job["jobs"][0]["id"], lambda d: d["state"] in ("done", "failed"))
    assert (failed["state"], failed["error"]["kind"]) == ("failed", "unsupported")
    assert app.jobs.procs == {}


# Twins and the queue


def test_retry_refuses_while_the_same_download_runs_in_another_row(app, events, monkeypatch, tmp_path):
    worker = Held(monkeypatch, tmp_path)
    first = post(app, "https://fake.test/hold")
    worker.held()
    app.json("POST", f"/api/jobs/{first}/cancel")
    second = post(app, "https://fake.test/hold")  # the same download again, in a row of its own
    assert second != first
    status, body = app.json("POST", f"/api/jobs/{first}/retry")
    assert (status, body["error"]["kind"]) == (409, "conflict")


def test_two_run_at_once_and_the_rest_wait(app, events):
    for n in (1, 2, 3):
        add(app, f"https://fake.test/slow/{n}?delay=0.3")
    assert sorted(j["state"] for j in app.jobs.snapshot()["jobs"]) == ["queued", "running", "running"]


def test_adding_many_videos_saves_the_list_once(app, events, monkeypatch):
    saves = []
    save = app.jobs._save
    monkeypatch.setattr(app.jobs, "_save", lambda: saves.append(1) or save())
    entries = [{"url": f"https://fake.test/slow/{n}?delay=0.3"} for n in range(100)]
    assert app.json("POST", "/api/jobs", {"entries": entries, "preset": "best"})[0] == 201
    assert len(saves) <= 1 + 2  # the list, then the two jobs that start


def test_progress_never_goes_back(tmp_path):
    from spool.engines import Engines
    from spool.jobs import Jobs, new_job

    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"))
    try:
        job = dict(new_job({"url": "https://fake.test/video"}, "best", "yt-dlp", str(tmp_path)), state="running")
        for p in (0.5, 0.3):
            jobs._progress(
                job,
                {
                    "status": "downloading",
                    "progress": p,
                    "part": 1,
                    "downloaded": 1,
                    "total": 2,
                    "speed": None,
                    "eta": None,
                },
            )
        assert job["progress"] == 0.5
    finally:
        jobs.close()


# Reading and watching never wait on the disk under the lock


def test_a_snapshot_checks_files_outside_the_lock(tmp_path, monkeypatch):
    import pathlib

    from spool.engines import Engines
    from spool.jobs import Jobs, new_job

    path = str(tmp_path / "v.mp4")
    done = dict(new_job({"url": "https://fake.test/video"}, "best", "yt-dlp", str(tmp_path)), state="done", path=path)
    (tmp_path / "state.json").write_text(json.dumps({"jobs": [done]}))
    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"))
    held = []
    exists, path_exists = os.path.exists, pathlib.Path.exists
    monkeypatch.setattr(
        os.path, "exists", lambda p: (str(p) != path or not held.append(jobs.lock._is_owned())) and exists(p)
    )
    monkeypatch.setattr(
        pathlib.Path, "exists", lambda p: (str(p) != path or not held.append(jobs.lock._is_owned())) and path_exists(p)
    )
    try:
        jobs.snapshot()
        jobs.subscribe()
        jobs._snapshot_changed()
        assert held and not any(held)
    finally:
        jobs.close()


def test_with_no_page_open_nothing_checks_the_folder(tmp_path, monkeypatch):
    import types

    import spool.jobs
    from spool import system
    from spool.engines import Engines
    from spool.jobs import Jobs

    watched = []  # each second the watcher has slept through
    clock = types.SimpleNamespace(
        time=time.time,
        monotonic=time.monotonic,
        strftime=time.strftime,
        sleep=lambda s: time.sleep(s) or (s == 1 and watched.append(s)),
    )
    monkeypatch.setattr(spool.jobs, "time", clock)
    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"))
    checks = []
    check = system.check_dest
    monkeypatch.setattr(system, "check_dest", lambda p, **k: checks.append(p) or check(p, **k))
    try:
        jobs._snapshot_changed()
        until(lambda: len(watched) >= 2)  # the watcher went round at least once, whole
        assert checks == []
        q = jobs.subscribe()
        jobs._snapshot_changed()
        assert q.get(timeout=1)[0] == q.get(timeout=1)[0] == "snapshot"
    finally:
        jobs.close()


# Links


@pytest.mark.parametrize(
    "url",
    [
        "ftp://fake.test/video",
        "file:///etc/passwd",
        "javascript://fake.test/%0aalert(1)",
        "https://user:pw@youtube.com@evil.test/x",
        "https://me@fake.test/video",
        "http://[::1/x",
    ],
)
def test_a_link_must_be_http_or_https_with_a_plain_host(app, url):
    status, body = app.json("POST", "/api/probe", {"url": url})
    assert (status, body["error"]["kind"]) == (422, "invalid_link")


# One file, one writer


def downloading_together(app, monkeypatch):
    """Every set of jobs seen downloading at once, at every progress event the workers send."""
    together, progress = [], app.jobs._progress

    def record(job, event):
        progress(job, event)
        with app.jobs.lock:
            together.append({j["id"] for j in app.jobs.jobs if j["state"] == "running" and j["stage"] == "downloading"})

    monkeypatch.setattr(app.jobs, "_progress", record)
    return together


@pytest.mark.parametrize("with_id", [True, False])
def test_two_links_to_one_video_never_write_its_file_at_once(app, events, monkeypatch, with_id):
    together = downloading_together(app, monkeypatch)
    ids = []
    for url in ("https://fake.test/slow?delay=0.05", "https://fake.test/slow/1?delay=0.05"):  # one video, two links
        entry = {"url": url, **({"id": "slow-1"} if with_id else {})}
        ids.append(app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})[1]["jobs"][0]["id"])
    ended = {}
    events.until(
        lambda e, d: (
            e == "job"
            and d["id"] in ids
            and d["state"] in ("done", "failed")
            and not ended.update({d["id"]: d})
            and len(ended) == 2
        ),
        timeout=30,
    )
    assert [ended[i]["state"] for i in ids] == ["done", "done"]
    assert {ended[i]["size"] for i in ids} == {4_000_000}
    assert together and max(len(s) for s in together) == 1


def test_audio_and_mp3_of_one_video_take_turns(app, events, monkeypatch):
    together = downloading_together(app, monkeypatch)
    entry = {"url": "https://fake.test/audio?delay=0.05", "id": "aud-1"}
    ids = [
        app.json("POST", "/api/jobs", {"entries": [entry], "preset": p})[1]["jobs"][0]["id"] for p in ("audio", "mp3")
    ]
    ended = {}
    events.until(
        lambda e, d: (
            e == "job"
            and d["id"] in ids
            and d["state"] in ("done", "failed")
            and not ended.update({d["id"]: d})
            and len(ended) == 2
        ),
        timeout=30,
    )
    assert [ended[i]["state"] for i in ids] == ["done", "done"]
    assert max(len(s) for s in together) == 1


def test_a_site_limiting_the_connection_never_offers_an_engine_update(tmp_path):
    from spool.engines import Engines
    from spool.jobs import Jobs

    engines = Engines(tmp_path / "engines")
    engines.status = lambda: {"yt-dlp": {"latest": "2", "version": "1"}}
    jobs = Jobs(tmp_path, engines)
    try:
        assert jobs._error("blocked", "yt-dlp")["hint"] == "update_engine"
        assert jobs._error("limited", "yt-dlp")["hint"] == "retry"
    finally:
        jobs.close()


def test_different_videos_with_ids_still_run_two_at_once(app, events):
    for n in (1, 2):
        entry = {"url": f"https://fake.test/slow/{n}?delay=0.3", "id": f"slow-{n}"}
        app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})
    assert [j["state"] for j in app.jobs.snapshot()["jobs"]] == ["running", "running"]


def test_the_watcher_sends_a_snapshot_only_when_the_folder_or_tools_change(tmp_path, monkeypatch):
    import queue
    import types

    import spool.jobs
    from spool.engines import Engines
    from spool.jobs import Jobs

    watched = []
    clock = types.SimpleNamespace(
        time=time.time,
        monotonic=time.monotonic,
        strftime=time.strftime,
        sleep=lambda s: time.sleep(s) or (s == 1 and watched.append(s)),
    )
    monkeypatch.setattr(spool.jobs, "time", clock)
    tools = {"ffmpeg": True, "js_runtime": "deno", "js_setup": None, "js_path": None, "platform": "x"}
    jobs = Jobs(tmp_path, Engines(tmp_path / "engines"), tools=lambda: dict(tools))
    try:
        q = jobs.subscribe()
        assert q.get(timeout=1)[0] == "snapshot"
        until(lambda: len(watched) >= 3)  # two whole rounds with the page open
        with pytest.raises(queue.Empty):
            q.get_nowait()  # nothing changed, so nothing was sent
        tools["ffmpeg"] = False  # the helper was uninstalled
        event, snap = q.get(timeout=10)
        assert (event, snap["tools"]["ffmpeg"]) == ("snapshot", False)
    finally:
        jobs.close()


def test_a_cancel_never_tests_the_folder(app, monkeypatch, tmp_path):
    """The write test after a run tells an unplugged drive from a plain failure; after Cancel nothing failed.
    (A real failure still runs it: test_a_destination_that_vanishes_mid_download_is_named_as_missing.)"""
    from spool import system

    worker = Held(monkeypatch, tmp_path)
    written, check = [], system.check_dest
    monkeypatch.setattr(system, "check_dest", lambda p, write=True: written.append(write) or check(p, write=write))
    job_id = post(app, "https://fake.test/hold")
    worker.held()
    before = written.count(True)  # the add and the start each test the folder
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    until(lambda: job_id not in app.jobs.procs)  # the job's run is over
    assert written.count(True) == before
