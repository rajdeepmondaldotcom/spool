"""A download the connection or the site cut short tries again by itself (contract point 3)."""

import json
import time

import pytest
from serve_fake import start

import spool.jobs


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


def wait_for(events, job_id, match, timeout=15):
    return events.until(lambda e, d: e == "job" and d["id"] == job_id and match(d), timeout)


def runs(app, job_id):
    return app.request("GET", f"/api/jobs/{job_id}/log")[1].decode().count("--- ")


def waiting(d):
    return d["state"] == "running" and d["stage"] == "waiting"


def test_the_waits_grow_and_end_after_five_minutes():
    assert spool.jobs.WAITS == (10, 30, 60, 120, 300)


def test_a_refusal_waits_then_tries_again_by_itself(app, events):
    app.jobs.waits = (2.5,) * 5
    job_id = add(app, "https://fake.test/403-once?delay=0")
    seen = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))
    held = [d for e, d in seen if e == "job" and d["id"] == job_id and waiting(d)]
    assert held, [d["stage"] for _, d in seen]
    assert {(d["note"], d["attempt"], d["attempts"]) for d in held} == {("The site refused the download.", 2, 6)}
    etas = [d["eta"] for d in held]
    assert etas[0] == 3 and len(set(etas)) >= 2 and etas == sorted(etas, reverse=True)  # about once a second
    assert seen[-1][1]["state"] == "done"
    assert (seen[-1][1]["note"], seen[-1][1]["stage"]) == (None, None)
    assert runs(app, job_id) == 2


def test_a_refusal_that_persists_fails_as_today_after_the_last_wait(app, events):
    app.jobs.waits = (0.05,) * 5
    job_id = add(app, "https://fake.test/403-start?delay=0")
    failed = wait_for(events, job_id, lambda d: d["state"] == "failed")[-1][1]
    assert failed["error"] == {"kind": "blocked", "message": "The site refused the download.", "hint": "retry"}
    assert (failed["stage"], failed["note"], failed["eta"]) == (None, None, None)
    assert runs(app, job_id) == 6


def test_a_refusal_mid_download_continues_the_partial_file(app, events):
    app.jobs.waits = (0.05,) * 5
    job_id = add(app, "https://fake.test/403?delay=0")
    seen = wait_for(events, job_id, lambda d: d["state"] == "failed")
    progress = [d["progress"] for e, d in seen if e == "job" and d["id"] == job_id and d["progress"] is not None]
    assert progress == sorted(progress) and progress[-1] > 0  # never backwards while it waits
    assert runs(app, job_id) == 6  # each try after the first got no further
    assert "Resuming download at byte" in app.request("GET", f"/api/jobs/{job_id}/log")[1].decode()


STUB = """
import json, pathlib, sys
spec = json.loads(sys.stdin.read())
count = pathlib.Path(sys.argv[0]).with_name("runs")
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
def say(**event):
    print(json.dumps(event), flush=True)
say(event="progress", status="downloading", part=1, downloaded=n * FURTHER, total=1000, speed=None, eta=None,
    progress=n * FURTHER / 1000, what="video")
if n < RUNS:
    say(event="error", message=MESSAGE, **KIND)
else:
    path = pathlib.Path(spec["dest"]) / "Stub [s].mp4"
    path.write_bytes(b"x")
    say(event="done", path=str(path), size=1)
"""


def stub(monkeypatch, tmp_path, message, runs=99, further=100, kind=None):
    worker = tmp_path / "stub" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(
        encoding="utf-8",
        data=STUB.replace("FURTHER", str(further))
        .replace("RUNS", str(runs))
        .replace("MESSAGE", repr(message))
        .replace("KIND", repr({"kind": kind} if kind else {})),
    )
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    return worker.with_name("runs")


def test_a_try_that_got_further_starts_the_count_again(app, events, monkeypatch, tmp_path):
    """A line that drops every few minutes still finishes a large download."""
    count = stub(monkeypatch, tmp_path, "ERROR: [Errno 54] Connection reset by peer", runs=9)
    app.jobs.waits = (0.05, 0.05)  # two waits without getting further, then it fails
    job_id = add(app, "https://fake.test/big")
    seen = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))
    assert seen[-1][1]["state"] == "done", seen[-1][1]["error"]
    assert count.read_text() == "9"
    notes = {d["note"] for e, d in seen if e == "job" and d["id"] == job_id and waiting(d)}
    assert notes == {"The connection dropped."}
    assert {d["attempt"] for e, d in seen if e == "job" and d["id"] == job_id and waiting(d)} == {2}


def test_a_try_that_got_no_further_counts(app, events, monkeypatch, tmp_path):
    count = stub(monkeypatch, tmp_path, "ERROR: Unable to download webpage: The read operation timed out", further=0)
    app.jobs.waits = (0.05, 0.05)
    job_id = add(app, "https://fake.test/big")
    failed = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))[-1][1]
    assert failed["error"]["kind"] == "network"
    assert count.read_text() == "3"


@pytest.mark.parametrize(
    "message, kind",
    [
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", None),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video", None),
        ("This is live now.", "live"),
        ("ERROR: unable to write data: [Errno 28] No space left on device", None),
        ("ERROR: unable to open for writing: [Errno 13] Permission denied: '/x/T [a].mp4.part'", None),
        ("ERROR: destination folder is missing", None),
        ("ERROR: Postprocessing: ffprobe and ffmpeg not found.", None),
        ("a list", "playlist"),
        ("ERROR: [youtube] abc: Some new breakage", None),
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", None),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", None),
    ],
)
def test_what_another_try_cannot_fix_is_not_tried_again(app, events, monkeypatch, tmp_path, message, kind):
    count = stub(monkeypatch, tmp_path, message, kind=kind)
    app.jobs.waits = (0.05,) * 5
    job_id = add(app, "https://fake.test/big")
    failed = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))
    assert not [d for e, d in failed if e == "job" and d["id"] == job_id and waiting(d)]
    assert count.read_text() == "1"


def test_a_waiting_job_holds_its_slot_and_cancel_works_at_once(app, events):
    app.jobs.waits = (30,) * 5
    first, second = (add(app, f"https://fake.test/403-start/{n}?delay=0") for n in (1, 2))
    wait_for(events, first, waiting)
    third = add(app, "https://fake.test/video?delay=0")
    assert app.jobs.snapshot()["jobs"][0]["state"] == "queued"  # both slots are held by jobs that wait
    status, job = app.json("POST", f"/api/jobs/{first}/cancel")
    assert (status, job["state"], job["stage"], job["note"]) == (200, "cancelled", None, None)
    assert wait_for(events, third, lambda d: d["state"] == "done", timeout=5)[-1][1]["state"] == "done"
    assert runs(app, first) == 1


def test_cancel_then_retry_while_waiting_runs_once(app, events):
    app.jobs.waits = (30,) * 5
    job_id = add(app, "https://fake.test/403-once?delay=0")
    wait_for(events, job_id, waiting)
    app.json("POST", f"/api/jobs/{job_id}/cancel")
    assert app.json("POST", f"/api/jobs/{job_id}/retry")[0] == 200
    assert wait_for(events, job_id, lambda d: d["state"] == "done", timeout=5)[-1][1]["state"] == "done"
    assert runs(app, job_id) == 2


def test_quitting_while_waiting_leaves_the_job_interrupted(tmp_path):
    httpd, _, jobs, _ = start(tmp_path)
    jobs.waits = (30,) * 5
    job = jobs.add([{"url": "https://fake.test/403-start?delay=0"}], "best")[0][0]
    end = time.time() + 10
    while jobs.snapshot()["jobs"][0]["stage"] != "waiting":
        assert time.time() < end
        time.sleep(0.05)
    httpd.shutdown()
    jobs.close()
    assert json.loads((tmp_path / "state.json").read_text())["jobs"][0]["stage"] == "waiting"
    httpd, _, jobs, _ = start(tmp_path)
    try:
        [again] = jobs.snapshot()["jobs"]
        assert (again["id"], again["state"], again["error"]["kind"]) == (job["id"], "failed", "interrupted")
        assert (again["stage"], again["note"], again["eta"]) == (None, None, None)
    finally:
        httpd.shutdown()
        jobs.close()


def test_a_site_limiting_the_connection_fails_at_once_with_a_way_to_try_by_hand(app, events, monkeypatch, tmp_path):
    count = stub(monkeypatch, tmp_path, "ERROR: [youtube] abc: Sign in to confirm you’re not a bot.")
    app.jobs.waits = (0.05,) * 5
    job_id = add(app, "https://fake.test/big")
    failed = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))[-1][1]
    assert failed["error"] == {
        "kind": "limited",
        "message": "The site is limiting this connection for now. Wait an hour or so, then try again.",
        "hint": "retry",
    }
    assert count.read_text() == "1"


# Pacing a long list


def starts_and_ends(events, ids):
    """(time, "start" | "end", id) for each job, until all have finished."""
    seen, ended = [], set()

    def note(e, d):
        if e == "job" and d["id"] in ids:
            if d["state"] == "running" and d["stage"] == "starting":
                seen.append((time.monotonic(), "start", d["id"]))
            elif d["state"] in ("done", "failed") and d["id"] not in ended:
                ended.add(d["id"])
                seen.append((time.monotonic(), "end", d["id"]))
        return len(ended) == len(ids)

    events.until(note, timeout=30)
    return seen


def test_with_many_waiting_the_next_starts_a_few_seconds_after_one_finishes(app, events):
    app.jobs.pace = 1.0
    app.jobs.pace_from = 3  # queued jobs at which the pause begins
    ids = {add(app, f"https://fake.test/video/{n}?delay=0") for n in range(6)}
    seen = starts_and_ends(events, ids)
    starts = [t for t, what, _ in seen if what == "start"]
    assert len(starts) == 6
    for began in starts[2:4]:  # 4, then 3 were still queued when these began
        last_end = max(t for t, what, _ in seen if what == "end" and t < began)
        assert began - last_end >= 0.9


def test_a_handful_of_downloads_is_not_slowed(app, events):
    app.jobs.pace = 30
    began = time.monotonic()
    ids = {add(app, f"https://fake.test/video/{n}?delay=0") for n in range(4)}
    starts_and_ends(events, ids)
    assert time.monotonic() - began < 15


def test_the_pace_is_kept_in_one_place():
    assert (spool.jobs.PACE, spool.jobs.PACE_FROM) == (8, 5)


def test_the_test_server_paces_nothing_unless_a_test_asks(tmp_path):
    httpd, _, jobs, _ = start(tmp_path)
    try:
        assert jobs.pace == 0
    finally:
        httpd.shutdown()
        jobs.close()
    httpd, _, jobs, _ = start(tmp_path / "paced", pace=3)
    try:
        assert jobs.pace == 3
    finally:
        httpd.shutdown()
        jobs.close()


PARTS = """
import json, pathlib, sys
sys.stdin.read()
count = pathlib.Path(sys.argv[0]).with_name("runs")
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
def say(**event):
    print(json.dumps(event), flush=True)
def progress(part, what, downloaded, total, progress):
    say(event="progress", status="downloading", part=part, downloaded=downloaded, total=total, speed=None,
        eta=None, progress=progress, what=what)
if n == 1:  # the video stream whole, then the audio drops early
    progress(1, "video", 900, 900, 0.9)
    say(event="progress", status="finished", part=1, downloaded=900, total=900, speed=None, eta=None,
        progress=0.9, what="video")
    progress(2, "audio", 5, 100, 0.905)
else:  # the video is on disk: the audio is this run's first stream, and gets further up to run 8
    m = min(n, 8)
    progress(1, "audio", 5 + m * 10, 100, 0.9 + (5 + m * 10) / 1000)
say(event="error", message="ERROR: [Errno 54] Connection reset by peer")
"""


def test_progress_on_a_later_stream_starts_the_count_again(app, events, monkeypatch, tmp_path):
    worker = tmp_path / "parts" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(PARTS, encoding="utf-8")
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    app.jobs.waits = (0.05, 0.05)
    job_id = add(app, "https://fake.test/big")
    wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))
    assert (
        int(worker.with_name("runs").read_text()) == 10
    )  # up to run 8 each got further; then two waits, and two tries that did not


UNICODE = """
import json, sys
sys.stdin.read()
said = {"utf8": sys.flags.utf8_mode, "encoding": sys.stdout.encoding}
print(json.dumps({"event": "meta", "title": "Ünïcödé 日本語 " + json.dumps(said), "uploader": "Zoë", "thumbnail": None,
                  "duration": 1}, ensure_ascii=False), flush=True)
print(json.dumps({"event": "error", "message": "ERROR: [youtube] abc: Sign in to confirm you’re not a bot."},
                 ensure_ascii=False), flush=True)
"""


def test_text_from_a_worker_arrives_intact_whatever_the_systems_code_page(app, events, monkeypatch, tmp_path):
    worker = tmp_path / "unicode" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(UNICODE, encoding="utf-8")
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    job_id = add(app, "https://fake.test/big")
    failed = wait_for(events, job_id, lambda d: d["state"] in ("done", "failed"))[-1][1]
    assert failed["title"].startswith("Ünïcödé 日本語 ") and failed["uploader"] == "Zoë"
    assert json.loads(failed["title"].split(" ", 2)[2]) == {"utf8": 1, "encoding": "utf-8"}
    assert failed["error"]["kind"] == "limited"
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/big"})  # the probe's path too
    assert (status, body["error"]["kind"]) == (422, "limited")
