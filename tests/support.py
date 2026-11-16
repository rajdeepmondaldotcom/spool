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
    """Talks to a running Spool the way the page does: cookie, same Origin, JSON."""

    def __init__(self, base, token="fake-token"):
        self.base, self.token = base, token
        self.headers = {"Cookie": f"spool={token}", "Origin": base}

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
