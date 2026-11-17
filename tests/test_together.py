"""Two pasted links download at once: a job added without its id learns it from its worker (contract section 1)."""

import json
import time

import pytest
from serve_fake import start
from support import Held, until

import spool.jobs
from spool import system


@pytest.fixture
def events(app):
    events = app.events()
    events.next()
    yield events
    events.close()


@pytest.fixture
def worker(monkeypatch, tmp_path):
    return Held(monkeypatch, tmp_path)


def add(app, url, id=None):
    entry = {"url": url, **({"id": id} if id else {})}
    status, body = app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})
    assert status == 201, body
    return body["jobs"][0]["id"]


def job(app, job_id):
    return next(j for j in app.jobs.snapshot()["jobs"] if j["id"] == job_id)


def ended(events, *ids, timeout=20):
    """Each job's last state, once every one of ids has finished or failed."""
    last = {}
    events.until(
        lambda e, d: (
            e == "job"
            and d["id"] in ids
            and d["state"] in ("done", "failed", "cancelled")
            and not last.update({d["id"]: d})
            and len(last) == len(ids)
        ),
        timeout,
    )
    return last


def held_at_go(app, job_id, vid="v"):
    """A job whose worker told its id and now waits for the queue's go. It stays so while what it waits for is held."""
    until(lambda: job(app, job_id)["entry_id"] == vid)
    j = job(app, job_id)
    assert (j["state"], j["stage"], j["files"], j["progress"]) == ("running", "starting", [], None)


def test_two_links_without_ids_for_different_videos_download_at_once(app, events, worker):
    first = add(app, "https://fake.test/hold?name=A&id=a")
    worker.held("A")  # mid-download
    second = add(app, "https://fake.test/plain?name=B&id=b")
    assert ended(events, second)[second]["state"] == "done"
    assert job(app, first)["state"] == "running"
    worker.go()
    assert ended(events, first)[first]["state"] == "done"
    assert (job(app, first)["entry_id"], job(app, second)["entry_id"]) == ("a", "b")  # kept, for a later Retry
    saved = {j["id"]: j["entry_id"] for j in json.loads((app.data / "state.json").read_text())["jobs"]}
    assert saved == {first: "a", second: "b"}


def test_the_second_link_to_the_same_video_goes_once_the_first_ones_worker_is_gone(app, events, worker):
    first = add(app, "https://fake.test/hold?name=Same&id=v")
    worker.held("Same")
    second = add(app, "https://fake.test/plain?name=Same&id=v")
    held_at_go(app, second)
    worker.go()
    last = ended(events, first, second)
    assert {d["state"] for d in last.values()} == {"done"}
    assert "Resuming" not in app.jobs.log(second)  # it never picked up the first one's half-written file


def test_a_link_without_an_id_waits_for_a_job_that_turns_out_to_be_the_same_video(app, events, worker):
    first = add(app, "https://fake.test/hold?name=Same&id=v", id="v")
    worker.held("Same")
    second = add(app, "https://fake.test/plain?name=Same&id=v")
    held_at_go(app, second)
    worker.go()
    assert {d["state"] for d in ended(events, first, second).values()} == {"done"}


def test_a_job_with_an_id_waits_in_the_queue_for_a_link_that_turned_out_to_be_it(app, events, worker):
    first = add(app, "https://fake.test/hold?name=Same&id=v")
    worker.held("Same")
    second = add(app, "https://fake.test/plain?name=Same&id=v", id="v")
    assert job(app, second)["state"] == "queued"  # a free slot, and the same video is being written
    worker.go()
    assert {d["state"] for d in ended(events, first, second).values()} == {"done"}


def test_a_held_job_still_takes_a_slot(app, events, worker):
    first = add(app, "https://fake.test/hold?name=Same&id=v", id="v")
    worker.held("Same")
    second = add(app, "https://fake.test/plain?name=Same&id=v")
    held_at_go(app, second)
    third = add(app, "https://fake.test/plain?name=C&id=c", id="c")
    assert job(app, third)["state"] == "queued"  # both slots are taken
    worker.go()
    assert {d["state"] for d in ended(events, first, second, third).values()} == {"done"}


def test_cancelling_a_held_job_stops_its_worker_frees_its_slot_and_writes_nothing(app, events, worker):
    first = add(app, "https://fake.test/hold?name=Same&id=v", id="v")
    worker.held("Same")
    second = add(app, "https://fake.test/plain?name=Same&id=v")
    held_at_go(app, second)
    status, body = app.json("POST", f"/api/jobs/{second}/cancel")
    assert (status, body["state"]) == (200, "cancelled")
    until(lambda: second not in app.jobs.procs)  # its worker is gone, and its slot with it
    third = add(app, "https://fake.test/plain?name=C&id=c", id="c")
    assert ended(events, third)[third]["state"] == "done"  # while the first still holds its slot
    assert job(app, second)["files"] == []
    assert "Resuming" not in app.jobs.log(second)
    worker.go()
    assert ended(events, first)[first]["state"] == "done"
    assert job(app, second)["state"] == "cancelled"


def test_quitting_while_a_job_is_held_leaves_it_interrupted_with_its_id(tmp_path, monkeypatch):
    worker = Held(monkeypatch, tmp_path)
    httpd, _, jobs, _ = start(tmp_path / "data")
    jobs.add([{"url": "https://fake.test/hold?name=Same&id=v", "id": "v"}], "best")
    worker.held("Same")
    [second], _ = jobs.add([{"url": "https://fake.test/plain?name=Same&id=v"}], "best")
    until(lambda: next(j for j in jobs.snapshot()["jobs"] if j["id"] == second["id"])["entry_id"] == "v")
    httpd.shutdown()
    began = time.monotonic()
    jobs.close()
    assert time.monotonic() - began < 10
    httpd, _, jobs, _ = start(tmp_path / "data")
    try:
        again = next(j for j in jobs.snapshot()["jobs"] if j["id"] == second["id"])
        assert (again["state"], again["error"]["kind"], again["entry_id"], again["files"]) == (
            "failed",
            "interrupted",
            "v",
            [],
        )
    finally:
        httpd.shutdown()
        jobs.close()


def stub(monkeypatch, tmp_path, code):
    worker = tmp_path / "stub" / "worker.py"
    worker.parent.mkdir()
    worker.write_text(code, encoding="utf-8")
    monkeypatch.setattr(spool.jobs, "WORKER", worker)
    return worker.parent


@pytest.mark.parametrize(
    "said, kind",
    [
        ('print(\'{"event": "error", "message": "ERROR: [Errno 54] Connection reset by peer"}\')', "network"),
        ("", "engine"),
    ],
)
def test_a_worker_that_dies_before_its_id_fails_as_before_and_frees_its_slot(
    app, events, monkeypatch, tmp_path, said, kind
):
    stub(monkeypatch, tmp_path, f"import sys\nsys.stdin.readline()\n{said}\n")
    first, second = add(app, "https://fake.test/one"), add(app, "https://fake.test/two")
    third = add(app, "https://fake.test/three")
    last = ended(events, first, second, third)
    assert {d["error"]["kind"] for d in last.values()} == {kind}


AGAIN = """
import json, pathlib, sys
spec = json.loads(sys.stdin.readline())
here = pathlib.Path(sys.argv[0]).parent
with open(here / "specs", "a") as f:
    f.write(json.dumps(spec) + "\\n")
def say(**event):
    print(json.dumps(event), flush=True)
say(event="id", id="r")
if spec.get("hold") and sys.stdin.readline().strip() != "go":
    sys.exit()
if len((here / "specs").read_text().splitlines()) == 1:
    say(event="error", message="ERROR: [Errno 54] Connection reset by peer")
else:
    path = pathlib.Path(spec["dest"]) / "Again [r].mp4"
    path.write_bytes(b"x")
    say(event="done", path=str(path), size=1)
"""


def test_a_try_again_after_the_id_is_known_needs_no_hold(app, events, monkeypatch, tmp_path):
    folder = stub(monkeypatch, tmp_path, AGAIN)
    app.jobs.waits = (0.05,)
    job_id = add(app, "https://fake.test/again")
    assert ended(events, job_id)[job_id]["state"] == "done"
    specs = [json.loads(line) for line in (folder / "specs").read_text().splitlines()]
    assert [s.get("hold") for s in specs] == [True, None]
    assert job(app, job_id)["entry_id"] == "r"


def test_a_held_job_is_never_said_to_be_not_answering(app, events, worker, monkeypatch):
    """Its folder check is vouched for by the job it waits for; while held, nothing checks it again."""
    first = add(app, "https://fake.test/hold?name=Same&id=v", id="v")
    worker.held("Same")
    until(lambda: job(app, first)["stage"] == "downloading")  # Spool knows it is writing
    monkeypatch.setattr(system, "CHECK_TIMEOUT", 0.3)
    real = system.probe
    monkeypatch.setattr(system, "probe", lambda path, write: time.sleep(1) or real(path, write))  # past the limit
    second = add(app, "https://fake.test/plain?name=Same&id=v")
    held_at_go(app, second)
    worker.go()
    last = ended(events, first, second)
    assert {d["state"] for d in last.values()} == {"done"}, last[second]["error"]


def test_two_links_without_ids_download_at_once_with_the_real_worker(app, events):
    ids = [add(app, f"https://fake.test/video/{n}?delay=0") for n in (1, 2)]
    last = ended(events, *ids, timeout=30)
    assert {d["state"] for d in last.values()} == {"done"}
    assert all(job(app, i)["entry_id"] for i in ids)
