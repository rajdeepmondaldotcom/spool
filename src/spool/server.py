"""HTTP: access checks, routing, live events and static files. Holds no state of its own."""

import hmac
import json
import mimetypes
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import system
from .engines import Busy
from .jobs import Problem

STATIC = Path(__file__).with_name("static")
CSP = (
    "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
MAX_BODY = 1_000_000
mimetypes.add_type("font/woff2", ".woff2")  # not in every platform's table, and nosniff needs it right
PING = 15


def make_server(jobs, engines, token, port=0):
    """A server bound to 127.0.0.1. Call serve_forever() on it."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.jobs, httpd.engines, httpd.token = jobs, engines, token
    return httpd


class Handler(BaseHTTPRequestHandler):
    server_version = "spool"
    sys_version = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def handle_request(self, method):
        try:
            url = urlsplit(self.path)
            self.check_host()
            self.check_access(method, url.path)
            route, args = self.route(method, url.path)
            route(*args)
        except Problem as p:
            self.send_json(p.status, {"error": {"kind": p.kind, "message": p.message, "hint": p.hint}})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Access

    def check_host(self):
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):  # DNS rebinding
            raise Problem(403, "forbidden", "This address is not Spool's.")

    def check_access(self, method, path):
        # The token travels in a header: a cookie would reach every server on 127.0.0.1, whatever its port.
        given = self.headers.get("X-Spool-Token", "")
        if path.startswith("/api/") and not hmac.compare_digest(given.encode(), self.server.token.encode()):
            raise Problem(401, "unauthorized", "Open Spool from the link it printed when it started.")
        if method != "GET":
            if self.headers.get("Origin") != f"http://{self.headers['Host']}":  # other websites
                raise Problem(403, "forbidden", "Spool only takes changes from its own page.")
            length = self.length()
            if length and self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise Problem(415, "bad_request", "Spool only takes JSON.")
            if length > MAX_BODY:
                raise Problem(413, "bad_request", "That request is too large.")

    def length(self):
        value = self.headers.get("Content-Length") or "0"
        if not re.fullmatch(r"[0-9]{1,12}", value):
            raise Problem(400, "bad_request", "Spool could not read that request.")
        return int(value)

    def body(self):
        length = self.length()
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except ValueError:
            raise Problem(400, "bad_request", "Spool could not read that request.") from None
        if not isinstance(data, dict):
            raise Problem(400, "bad_request", "Spool could not read that request.")
        return data

    # Routing

    def route(self, method, path):
        for m, pattern, name in ROUTES:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                return getattr(self, name), match.groups()
        if method == "GET" and not path.startswith("/api/"):
            return self.static, (path,)
        if any(re.fullmatch(pattern, path) for _, pattern, _ in ROUTES):
            raise Problem(405, "bad_request", "That method is not allowed here.")
        raise Problem(404, "not_found", "There is nothing here.")

    def events(self):
        q = self.server.jobs.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            while True:
                try:
                    event, data = q.get(timeout=PING)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    continue
                if event is None:
                    return
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        finally:
            self.server.jobs.unsubscribe(q)

    def probe(self):
        body = self.body()
        self.send_json(200, self.server.jobs.probe(body.get("url"), body.get("engine")))

    def add(self):
        body = self.body()
        self.send_json(201, {"jobs": self.server.jobs.add(body.get("entries"), body.get("preset"), body.get("engine"))})

    def cancel(self, job_id):
        self.send_json(200, self.server.jobs.cancel(job_id))

    def retry(self, job_id):
        self.send_json(200, self.server.jobs.retry(job_id, self.body().get("engine")))

    def remove(self, job_id):
        self.server.jobs.remove(job_id)
        self.send_bytes(204, b"", "application/json")

    def log(self, job_id):
        self.send_bytes(200, self.server.jobs.log(job_id).encode(), "text/plain")

    def reveal(self, job_id):
        self.server.jobs.reveal(job_id)
        self.send_bytes(204, b"", "application/json")

    def settings(self):
        self.send_json(200, self.server.jobs.update_settings(self.body()))

    def choose_folder(self):
        try:
            path = system.choose_folder()
        except NotImplementedError:
            raise Problem(
                501, "not_supported", "Type the folder path instead; this system has no folder dialog."
            ) from None
        if path is None:
            self.send_bytes(204, b"", "application/json")
        else:
            self.send_json(200, {"path": path})

    def update_engine(self, name):
        try:
            self.server.engines.update(name)
        except KeyError:
            raise Problem(404, "not_found", "Spool has no engine by that name.") from None
        except Busy:
            raise Problem(409, "busy", f"{name} is already being installed or updated.") from None
        self.send_bytes(202, b"", "application/json")

    def static(self, path):
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (STATIC / name).resolve()
        if not re.fullmatch(r"[\w.\-/]+", name) or not file.is_relative_to(STATIC.resolve()) or not file.is_file():
            raise Problem(404, "not_found", "There is nothing here.")
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_bytes(200, file.read_bytes(), "text/javascript" if file.suffix == ".js" else kind)

    # Responses

    def send_json(self, status, data):
        self.send_bytes(status, json.dumps(data).encode(), "application/json")

    def send_bytes(self, status, data, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")  # thumbnail requests must not carry Spool's address
        self.end_headers()
        self.wfile.write(data)


ROUTES = [
    ("GET", r"/api/events", "events"),
    ("POST", r"/api/probe", "probe"),
    ("POST", r"/api/jobs", "add"),
    ("POST", r"/api/jobs/(\w+)/cancel", "cancel"),
    ("POST", r"/api/jobs/(\w+)/retry", "retry"),
    ("DELETE", r"/api/jobs/(\w+)", "remove"),
    ("POST", r"/api/jobs/(\w+)/reveal", "reveal"),
    ("GET", r"/api/jobs/(\w+)/log", "log"),
    ("PUT", r"/api/settings", "settings"),
    ("POST", r"/api/choose-folder", "choose_folder"),
    ("POST", r"/api/engines/([\w\-]+)/update", "update_engine"),
]
