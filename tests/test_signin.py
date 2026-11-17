"""Signing in, for private and age-restricted videos: the queue's side (contract section 4)."""

import json

import pytest
from serve_fake import start

import spool.jobs
from spool.jobs import classify

AGE = "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users."
ROBOT = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"

# Each run does what the next line of `plan` says: an error event (message, and the kind the worker names, if
# any), or "ok": a finished file for a download, a video's details for a probe. Every spec is kept in `specs`.
STUB = """
import json, pathlib, sys
spec = json.loads(sys.stdin.readline())
here = pathlib.Path(sys.argv[0]).parent
with open(here / "specs", "a", encoding="utf-8") as f:
    f.write(json.dumps(spec) + "\\n")
plan = json.loads((here / "plan").read_text(encoding="utf-8"))
step = plan[min(len((here / "specs").read_text().splitlines()), len(plan)) - 1]
def say(**event):
    print(json.dumps(event), flush=True)
if step != "ok":
    say(event="error", **step)
elif spec["mode"] == "probe":
    say(event="info", kind="video", title="Signed", entries=[{"url": spec["url"], "id": "s1", "presets": []}])
else:
    path = pathlib.Path(spec["dest"]) / "Signed [s1].mp4"
    path.write_bytes(b"x")
    say(event="done", path=str(path), size=1)
"""


@pytest.fixture
def worker(monkeypatch, tmp_path):
    folder = tmp_path / "signin-stub"
    folder.mkdir()
    (folder / "worker.py").write_text(STUB, encoding="utf-8")
    monkeypatch.setattr(spool.jobs, "WORKER", folder / "worker.py")

    def plan(*steps):
        (folder / "plan").write_text(json.dumps(steps), encoding="utf-8")
        return lambda: [json.loads(line) for line in (folder / "specs").read_text().splitlines()]

    return plan


@pytest.fixture
def events(app):
    events = app.events()
    events.next()
    yield events
    events.close()


def download(app, events):
    status, body = app.json(
        "POST", "/api/jobs", {"entries": [{"url": "https://fake.test/x", "id": "s1"}], "preset": "best"}
    )
    assert status == 201, body
    job_id = body["jobs"][0]["id"]
    return events.until(lambda e, d: e == "job" and d["id"] == job_id and d["state"] in ("done", "failed"))[-1][1]


# The setting


def test_sign_in_is_off_until_a_browser_is_chosen_and_is_saved(app, tmp_path):
    assert app.jobs.snapshot()["settings"]["signin"] is None
    status, body = app.json("PUT", "/api/settings", {"signin": "safari"})
    assert (status, body["settings"]["signin"]) == (200, "safari")
    assert json.loads((app.data / "state.json").read_text())["settings"]["signin"] == "safari"
    assert app.json("PUT", "/api/settings", {"signin": None})[1]["settings"]["signin"] is None


@pytest.mark.parametrize("value", ["netscape", "Safari", 7, ["chrome"], ""])
def test_a_browser_spool_cannot_read_is_refused(app, value):
    status, body = app.json("PUT", "/api/settings", {"signin": value})
    assert (status, body["error"]) == (
        422,
        {"kind": "invalid_signin", "message": "Spool can't sign in with that browser.", "hint": None},
    )
    assert app.jobs.snapshot()["settings"]["signin"] is None


def test_a_saved_browser_survives_a_restart_and_a_bad_one_is_dropped(tmp_path):
    for saved, loaded in (("firefox", "firefox"), ("netscape", None)):
        data = tmp_path / saved
        httpd, _, jobs, _ = start(data)
        jobs.update_settings({"signin": "firefox"})
        httpd.shutdown()
        jobs.close()
        state = json.loads((data / "state.json").read_text())
        state["settings"]["signin"] = saved
        (data / "state.json").write_text(json.dumps(state))
        httpd, _, jobs, _ = start(data)
        try:
            assert jobs.snapshot()["settings"]["signin"] == loaded
        finally:
            httpd.shutdown()
            jobs.close()


# The kinds


@pytest.mark.parametrize(
    "message",
    [
        AGE,
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
        "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video.",
        "ERROR: [vimeo] 123: This video is only available for registered users. Use --cookies",
        "ERROR: [twitter] 1: You need to log in to view this content",
        "ERROR: [patreon] 1: You need to be logged-in to access this",
        "ERROR: [instagram] abc: Requested content is not available, rate-limit reached or login required",
    ],
)
def test_a_video_that_asks_for_an_account_is_signin(message):
    assert classify(message) == "signin"


@pytest.mark.parametrize(
    "message, kind",
    [
        (ROBOT, "limited"),  # the robot check says "sign in" too, and is matched first
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable"),
        ("ERROR: [youtube] abc: The uploader has not made this video available in your country", "unavailable"),
    ],
)
def test_what_is_not_about_an_account_stays_as_it_was(message, kind):
    assert classify(message) == kind


# Downloads


def test_with_no_browser_chosen_it_says_where_to_choose_one(app, events, worker):
    specs = worker({"message": AGE})
    failed = download(app, events)
    assert failed["error"] == {
        "kind": "signin",
        "message": "This video needs a signed-in account. Choose your browser in Settings.",
        "hint": "signin",
    }
    assert [s.get("signin") for s in specs()] == [None]


def test_a_video_that_asks_is_tried_once_more_signed_in(app, events, worker):
    specs = worker({"message": AGE}, "ok")
    app.json("PUT", "/api/settings", {"signin": "chrome"})
    done = download(app, events)
    assert done["state"] == "done", done["error"]
    assert [s.get("signin") for s in specs()] == [None, "chrome"]
    assert "signin" not in specs()[0]  # present only on the try that uses it


def test_signed_in_and_still_refused_says_the_account_cannot_open_it(app, events, worker):
    specs = worker({"message": AGE})
    app.json("PUT", "/api/settings", {"signin": "safari"})
    failed = download(app, events)
    assert failed["error"] == {
        "kind": "signin",
        "message": "The account in Safari can't open this video.",
        "hint": None,
    }
    assert [s.get("signin") for s in specs()] == [None, "safari"]


def test_a_sign_in_that_cannot_be_read_says_so(app, events, worker):
    specs = worker({"message": AGE}, {"message": "ERROR: could not copy the cookie database", "kind": "signin_unread"})
    app.json("PUT", "/api/settings", {"signin": "chrome"})
    failed = download(app, events)
    assert failed["error"] == {
        "kind": "signin_unread",
        "message": "Spool couldn't read the sign-in from Chrome. Choose another browser in Settings, or try again.",
        "hint": "signin",
    }
    assert len(specs()) == 2


def test_the_robot_check_is_never_tried_signed_in(app, events, worker):
    specs = worker({"message": ROBOT})
    app.json("PUT", "/api/settings", {"signin": "chrome"})
    assert download(app, events)["error"]["kind"] == "limited"
    assert len(specs()) == 1


def test_a_retry_by_hand_starts_without_the_sign_in_again(app, events, worker):
    specs = worker({"message": AGE})
    app.json("PUT", "/api/settings", {"signin": "chrome"})
    failed = download(app, events)
    assert app.json("POST", f"/api/jobs/{failed['id']}/retry")[0] == 200
    events.until(lambda e, d: e == "job" and d["id"] == failed["id"] and d["state"] == "failed")
    assert [s.get("signin") for s in specs()] == [None, "chrome", None, "chrome"]


# Getting details


def test_a_probe_is_tried_once_more_signed_in(app, worker):
    specs = worker({"message": AGE}, "ok")
    app.json("PUT", "/api/settings", {"signin": "firefox"})
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/x"})
    assert (status, body["title"]) == (200, "Signed")
    assert [s.get("signin") for s in specs()] == [None, "firefox"]


@pytest.mark.parametrize(
    "chosen, steps, message, hint",
    [
        (None, [{"message": AGE}], "This video needs a signed-in account. Choose your browser in Settings.", "signin"),
        ("edge", [{"message": AGE}], "The account in Edge can't open this video.", None),
        (
            "edge",
            [{"message": AGE}, {"message": "x", "kind": "signin_unread"}],
            "Spool couldn't read the sign-in from Edge. Choose another browser in Settings, or try again.",
            "signin",
        ),
    ],
)
def test_a_probe_that_needs_an_account_says_why(app, worker, chosen, steps, message, hint):
    worker(*steps)
    app.json("PUT", "/api/settings", {"signin": chosen})
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/x"})
    assert (status, body["error"]["message"], body["error"]["hint"]) == (422, message, hint)


# The second engine cannot sign in

ELSEWHERE = {
    "kind": "signin",
    "message": "This video needs a signed-in account, and Spool can't sign in for this link.",
    "hint": None,
}


def test_a_download_on_the_second_engine_is_not_tried_signed_in(app, events, worker):
    specs = worker({"message": AGE})
    app.json("PUT", "/api/settings", {"signin": "chrome", "engine": "youtube-dl"})
    assert download(app, events)["error"] == ELSEWHERE
    assert [(s["engine"], s.get("signin")) for s in specs()] == [("youtube-dl", None)]


def test_a_probe_on_the_second_engine_is_not_tried_signed_in(app, worker):
    specs = worker({"message": AGE})
    app.json("PUT", "/api/settings", {"signin": "chrome"})
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/x", "engine": "youtube-dl"})
    assert (status, body["error"]) == (422, ELSEWHERE)
    assert [(s["engine"], s.get("signin")) for s in specs()] == [("youtube-dl", None)]
