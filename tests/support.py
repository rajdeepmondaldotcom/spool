"""Helpers the backend tests share. (Not conftest: tests/e2e has its own conftest module.)"""

import http.client
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request


class Client:
    """Talks to a running Spool the way the page does: token header, same Origin, JSON."""

    def __init__(self, base, token="fake-token"):
        self.base, self.token = base, token
        self.headers = {"X-Spool-Token": token, "Origin": base}

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {**self.headers, **({"Content-Type": "application/json"} if data else {}), **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, method, path, body=None, headers=None):
        status, raw, _ = self.request(method, path, body, headers)
        return status, json.loads(raw) if raw and raw[:1] in b"{[" else raw

    def events(self):
        return Events(self.base, self.headers)


class Events:
    """Reads /api/events in a thread; next() returns (event, data)."""

    def __init__(self, base, headers):
        conn = http.client.HTTPConnection(base.split("//")[1], timeout=30)
        conn.connect()
        self.sock = conn.sock
        conn.request("GET", "/api/events", headers=headers)
        self.resp = conn.getresponse()
        assert self.resp.status == 200, self.resp.status
        self.q = queue.Queue()
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        event = None
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    self.q.put((event, json.loads(line[6:])))
        except Exception:
            pass

    def next(self, timeout=10):
        return self.q.get(timeout=timeout)

    def until(self, match, timeout=15):
        """Return every event up to and including the first one that match(event, data) accepts."""
        seen, end = [], time.time() + timeout
        while True:
            item = self.next(max(0.01, end - time.time()))
            seen.append(item)
            if match(*item):
                return seen

    def close(self):
        self.sock.shutdown(socket.SHUT_RDWR)


def job_state(state):
    return lambda event, data: event == "job" and data["state"] == state


# A worker that a test holds in place, so a moment in a download is there to be seen however fast or slow the
# machine is. Its link says what it does: https://fake.test/<words>?name=<file name>, where the words may hold
#   hold   wait after the first 100 bytes until the test calls go()
#   child  start a helper process first, as an engine starts ffmpeg (a cancel must stop it too)
#   burst  send 200 progress events at once before holding
#   mp3    also write a half-made .mp3 beside the stream, as ffmpeg does
# Without hold it writes 300 bytes and finishes. A .part already there is continued, and the log says so.
HELD = r"""
import json, os, pathlib, subprocess, sys, time
from urllib.parse import parse_qs, urlsplit
spec = json.loads(sys.stdin.read())
here = pathlib.Path(sys.argv[0]).parent
url = urlsplit(spec["url"])
words = url.path
title = parse_qs(url.query).get("name", ["Same video"])[0]
ext = "webm" if "mp3" in words else "mp4"
name = os.path.join(spec["dest"], f"{title} [v].{ext}")
def say(**event):
    print(json.dumps(event), flush=True)
def progress(done):
    say(event="progress", status="downloading", part=1, downloaded=done, total=300, speed=None, eta=None,
        progress=done / 300, what="video")
def write(n):
    with open(name + ".part", "ab") as f:
        f.write(b"x" * n)
    return os.path.getsize(name + ".part")
if "child" in words:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
say(event="file", path=name)
if os.path.exists(name + ".part"):
    say(event="log", line=f"[download] Resuming download at byte {os.path.getsize(name + '.part')}")
progress(write(100))
if "mp3" in words:
    pathlib.Path(name[: -len(ext)] + "mp3").write_bytes(b"half")
if "burst" in words:
    for n in range(1, 201):
        progress(100 + n)
    say(event="log", line="[spool] burst sent")
if "hold" in words:
    (here / f"held-{title}").touch()
    end = time.time() + 60
    while not (here / "go").exists() and time.time() < end:
        time.sleep(0.02)
try:
    progress(write(100))
    progress(write(100))
except OSError as exc:
    say(event="error", message=f"ERROR: unable to write data: {exc}")
    sys.exit()
os.replace(name + ".part", name)
say(event="done", path=name, size=os.path.getsize(name))
"""


class Held:
    """The held worker in place of Spool's, for a test. held(title) waits until that file's job is held."""

    def __init__(self, monkeypatch, folder):
        import spool.jobs

        self.dir = folder / "held-worker"
        self.dir.mkdir(parents=True)
        (self.dir / "worker.py").write_text(HELD, encoding="utf-8")
        monkeypatch.setattr(spool.jobs, "WORKER", self.dir / "worker.py")

    def held(self, title="Same video", timeout=20):
        until(lambda: (self.dir / f"held-{title}").exists(), timeout)

    def go(self):
        (self.dir / "go").touch()


def until(test, timeout=20):
    """Wait for something that will happen; the timeout only stops a broken test from hanging."""
    end = time.time() + timeout
    while not test():
        assert time.time() < end, "it never happened"
        time.sleep(0.02)
