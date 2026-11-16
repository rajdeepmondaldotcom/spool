import http.client
import json
import socket
import time
from pathlib import Path

import pytest
from support import job_state

from spool import server, system


def raw(app, method, path, headers):
    """A request with exactly these headers (urllib always sends its own Host)."""
    host, port = app.httpd.server_address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    stream = r.getheader("Content-Type") == "text/event-stream"  # never ends: a broken check must fail, not hang
    return r.status, r.getheaders(), b"" if stream else r.read()


def host(app):
    return f"127.0.0.1:{app.httpd.server_address[1]}"


# Access checks


TOKEN = {"X-Spool-Token": "fake-token"}


def test_a_foreign_host_is_refused(app):
    status, _, _ = raw(app, "GET", "/api/events", {"Host": "evil.test", **TOKEN})
    assert status == 403
    assert raw(app, "GET", "/", {"Host": "evil.test"})[0] == 403


def test_localhost_by_name_is_accepted(app):
    port = app.httpd.server_address[1]
    status, _, _ = raw(app, "GET", "/", {"Host": f"localhost:{port}"})
    assert status == 200


def test_the_page_and_its_files_need_no_token(app):
    for path in ("/", "/index.html"):
        assert raw(app, "GET", path, {"Host": host(app)})[0] == 200, path


def test_every_api_request_needs_the_token_header(app):
    for method, path in (("GET", "/api/events"), ("GET", "/api/jobs/x/log"), ("POST", "/api/probe")):
        for headers in ({}, {"X-Spool-Token": "guess"}, {"Cookie": "spool=fake-token"}):
            status, _, body = raw(app, method, path, {"Host": host(app), "Origin": f"http://{host(app)}", **headers})
            assert status == 401, (method, path, headers)
            assert json.loads(body)["error"]["kind"] == "unauthorized"


def test_spool_sets_no_cookies_and_sends_no_referrer(app):
    for path in ("/", "/api/jobs/x/log"):
        _, headers, _ = raw(app, "GET", path, {"Host": host(app), **TOKEN})
        headers = dict(headers)
        assert "Set-Cookie" not in headers
        assert headers["Referrer-Policy"] == "no-referrer"


def test_writes_need_our_own_origin(app):
    body = {"url": "https://fake.test/video"}
    assert app.json("POST", "/api/probe", body, {"Origin": "https://evil.test"})[0] == 403
    status, _, _ = raw(app, "POST", "/api/probe", {"Host": host(app), **TOKEN})
    assert status == 403


def test_writes_with_a_body_must_be_json(app):
    status, _, _ = app.request("POST", "/api/probe", None, {"Content-Type": "text/plain", "Content-Length": "2"})
    assert status == 415


def test_the_page_is_served_with_a_strict_content_security_policy(app):
    status, raw_body, headers = app.request("GET", "/")
    assert status == 200
    assert b"<html" in raw_body.lower()
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert "Access-Control-Allow-Origin" not in headers


def test_static_files_cannot_escape_the_static_folder(app):
    assert app.request("GET", "/../__init__.py")[0] == 404
    assert app.request("GET", "/%2e%2e/__init__.py")[0] == 404


def test_errors_are_json_with_a_kind_and_plain_words(app):
    status, body = app.json("GET", "/api/nothing-here")
    assert status == 404
    assert body["error"]["kind"] == "bad_request"
    assert body["error"]["message"]


# Live events, probe and download


def test_events_start_with_a_snapshot(app):
    events = app.events()
    event, snap = events.next()
    events.close()
    assert event == "snapshot"
    assert snap["settings"] == {"dest": str(app.dest), "engine": "yt-dlp", "preset": "best"}
    assert snap["dest"]["ok"] is True
    assert snap["engines"]["yt-dlp"]["installed"] is True
    assert snap["engines"]["yt-dlp"]["version"] == "2026.01.01"
    assert set(snap["tools"]) == {"ffmpeg", "js_runtime", "js_setup", "platform", "can_choose_folder"}
    assert snap["dest"]["message"] is None
    assert snap["version"] == "0.1.0"
    assert snap["jobs"] == []


def test_probe_returns_the_entries(app):
    status, probe = app.json("POST", "/api/probe", {"url": "https://fake.test/video"})
    assert status == 200
    assert probe["engine"] == "yt-dlp"
    assert probe["kind"] == "video"
    assert probe["entries"][0]["id"] == "vid-1"


def test_probe_of_an_empty_playlist_is_empty_after_both_engines(app):
    status, body = app.json("POST", "/api/probe", {"url": "https://fake.test/empty-playlist"})
    assert (status, body["error"]) == (
        422,
        {"kind": "empty", "message": "Spool found no video at this link.", "hint": None},
    )


def test_probe_of_a_playlist_lists_its_entries(app):
    status, probe = app.json("POST", "/api/probe", {"url": "https://fake.test/playlist", "engine": "youtube-dl"})
    assert status == 200
    assert probe["engine"] == "youtube-dl"
    assert (probe["kind"], probe["title"], len(probe["entries"])) == ("playlist", "Fake playlist", 3)


def test_probe_refuses_a_link_that_is_not_http(app):
    for url in ("file:///etc/passwd", "javascript:alert(1)", "https://", "not a link", 42):
        status, body = app.json("POST", "/api/probe", {"url": url})
        assert status == 422, url
        assert body["error"]["kind"] == "invalid_link"


def test_a_job_downloads_with_live_progress_to_the_destination(app):
    events = app.events()
    events.next()
    status, probe = app.json("POST", "/api/probe", {"url": "https://fake.test/video?delay=0.02"})
    status, body = app.json("POST", "/api/jobs", {"entries": probe["entries"], "preset": "best"})
    assert status == 201
    [job] = body["jobs"]
    assert job["state"] == "queued"
    seen = events.until(job_state("done"))
    events.close()
    states = [d["state"] for e, d in seen if e == "job"]
    assert states[0] == "queued" and "running" in states and states[-1] == "done"
    assert any(d.get("stage") == "downloading" and d["downloaded"] for e, d in seen if e == "job")
    done = seen[-1][1]
    assert done["path"] == str(app.dest / "Fake video 1 [vid-1].mp4")
    assert done["size"] == 3_400_000
    assert done["file_exists"] is True
    assert Path(done["path"]).exists()


def test_job_events_are_throttled_to_four_a_second(app):
    events = app.events()
    events.next()
    # 20 progress steps over about one second
    entry = {"url": "https://fake.test/video?delay=0.05", "id": "vid-1", "title": "Fake video 1"}
    began = time.monotonic()
    app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})
    seen = events.until(job_state("done"))
    took = time.monotonic() - began  # about a second here, several on a loaded machine
    events.close()
    progress = [d for e, d in seen if e == "job" and d["state"] == "running"]
    assert 2 <= len(progress) <= 4 * took + 4  # four a second, plus the stage changes, which go out at once


def test_two_jobs_run_at_once_and_the_third_waits(app):
    events = app.events()
    events.next()
    entries = [
        {"url": f"https://fake.test/slow/{n}?delay=0.2", "id": f"slow-{n}", "title": f"Slow {n}"} for n in (1, 2, 3)
    ]
    app.json("POST", "/api/jobs", {"entries": entries, "preset": "best"})
    events.until(lambda e, d: e == "job" and d["state"] == "running" and d["stage"] == "downloading")
    states = {j["title"]: j["state"] for j in app.jobs.snapshot()["jobs"]}
    assert states == {"Slow 1": "running", "Slow 2": "running", "Slow 3": "queued"}
    events.close()


def test_a_long_link_is_accepted_and_a_too_long_one_says_so(app):
    long = "https://fake.test/video?x=" + "a" * 5000
    assert app.json("POST", "/api/probe", {"url": long})[0] == 200
    status, body = app.json("POST", "/api/probe", {"url": long + "a" * 10_000})
    assert (status, body["error"]["kind"], body["error"]["message"]) == (
        422,
        "too_long",
        "That link is too long for Spool.",
    )


def test_a_bad_content_length_is_400(app):
    for length in ("abc", "-5", "1e3"):
        headers = {"Host": host(app), "Origin": f"http://{host(app)}", "Content-Type": "application/json", **TOKEN}
        status, _, body = raw(app, "POST", "/api/probe", {**headers, "Content-Length": length})
        assert status == 400, length
        assert json.loads(body)["error"]["kind"] == "bad_request"


def test_settings_of_the_wrong_type_are_422(app):
    for body in ({"engine": ["yt-dlp"]}, {"preset": {"a": 1}}, {"dest": None}, {"engine": None}):
        status, reply = app.json("PUT", "/api/settings", body)
        assert status == 422, body
        assert reply["error"]["kind"].startswith("invalid_")
    assert app.json("POST", "/api/probe", {"url": "https://fake.test/video", "engine": ["x"]})[0] == 422


# Every request is answered, and an idle connection is let go


def test_an_idle_connection_is_closed(app, monkeypatch):
    assert 0 < server.Handler.timeout <= 30
    monkeypatch.setattr(server.Handler, "timeout", 0.3)
    with socket.create_connection(app.httpd.server_address, timeout=5) as s:
        t = time.time()
        assert s.recv(1) == b""  # closed by Spool, not by our 5 s timeout
        assert time.time() - t < 3


def test_a_bug_in_a_route_is_a_plain_sentence(app, monkeypatch):
    monkeypatch.setattr(app.jobs, "log", lambda job_id: 1 / 0)
    status, body = app.json("GET", "/api/jobs/x/log")
    assert (status, body["error"]["kind"]) == (500, "internal")
    assert body["error"]["message"]


def test_a_tab_that_closes_mid_request_is_quiet(app, monkeypatch, capfd):
    def aborted(job_id):
        raise ConnectionAbortedError(10053, "An established connection was aborted")  # what Windows raises

    monkeypatch.setattr(app.jobs, "log", aborted)
    try:
        app.request("GET", "/api/jobs/x/log")
    except http.client.RemoteDisconnected:
        pass
    time.sleep(0.2)
    assert "Traceback" not in capfd.readouterr().err


def test_http_thumbnails_are_allowed(app):
    img = next(d for d in server.CSP.split(";") if d.strip().startswith("img-src")).split()
    assert {"'self'", "https:", "http:", "data:"} <= set(img)


def test_the_folder_dialog_answers_a_path_a_cancel_or_a_plain_refusal(app, monkeypatch):
    answers = iter(["/Volumes/T7/Spool", None])
    monkeypatch.setattr(system, "choose_folder", lambda: next(answers))
    assert app.json("POST", "/api/choose-folder") == (200, {"path": "/Volumes/T7/Spool"})
    assert app.request("POST", "/api/choose-folder")[0] == 204

    def refused():
        raise NotImplementedError("Not authorized to send Apple events (-1743)")

    monkeypatch.setattr(system, "choose_folder", refused)
    status, body = app.json("POST", "/api/choose-folder")
    assert (status, body["error"]["kind"]) == (501, "not_supported")


def test_reveal_shows_the_finished_file_or_says_the_system_cannot(app, monkeypatch):
    shown = []
    monkeypatch.setattr(system, "reveal", shown.append)
    monkeypatch.setattr(system, "open_folder", shown.append)
    entry = {"url": "https://fake.test/video?delay=0", "id": "vid-1", "title": "Fake video 1"}
    events = app.events()
    events.next()
    [job] = app.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})[1]["jobs"]
    done = events.until(job_state("done"))[-1][1]
    events.close()
    assert app.request("POST", f"/api/jobs/{job['id']}/reveal")[0] == 204
    assert app.request("POST", "/api/dest/reveal")[0] == 204
    assert shown == [done["path"], str(app.dest)]

    def none(path):
        raise NotImplementedError

    monkeypatch.setattr(system, "reveal", none)
    monkeypatch.setattr(system, "open_folder", none)
    for path in (f"/api/jobs/{job['id']}/reveal", "/api/dest/reveal"):
        status, body = app.json("POST", path)
        assert (status, body["error"]["kind"]) == (501, "not_supported"), path


def test_starting_the_server_never_looks_up_this_machines_name(monkeypatch):
    """On some networks that lookup takes many seconds, and Spool only ever serves 127.0.0.1."""
    monkeypatch.setattr(socket, "getfqdn", lambda *a: pytest.fail("the server looked up the machine's name"))
    httpd = server.make_server(None, None, "token")
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()
