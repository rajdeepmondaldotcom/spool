"""spool [--port N] [--no-open]: start the local server and open it in the browser."""

import argparse
import http.client
import json
import os
import secrets
import signal
import sys
import webbrowser

from . import server, system
from .engines import Engines
from .jobs import Jobs

PORT = 7766  # fixed by default, so a tab or bookmark survives a restart


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spool", description="A local web app for yt-dlp and youtube-dl.")
    parser.add_argument("--port", type=int, help=f"port on 127.0.0.1 (default: {PORT}, else a free one)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    data = system.data_dir()
    lock = data / "spool.lock"
    token = load_token(data / "token")
    url = running(lock, token)
    if url:
        print(f"Spool is already running at {url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return

    engines = Engines(data / "engines")
    jobs = Jobs(data, engines)
    try:
        httpd = server.make_server(jobs, engines, token, PORT if args.port is None else args.port)
    except OSError as exc:
        if args.port is not None:
            jobs.close()
            print(f"Spool could not use port {args.port}: {exc.strerror}", flush=True)
            sys.exit(1)
        httpd = server.make_server(jobs, engines, token, 0)  # something else holds the default port
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={token}"  # a fragment never leaves the browser
    write_private(lock, json.dumps({"port": port, "pid": os.getpid()}))
    signal.signal(signal.SIGTERM, interrupt)
    engines.start()
    print(f"Spool is running at {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        jobs.close()  # kills every worker's process group; their jobs read as interrupted next time
        lock.unlink(missing_ok=True)


def load_token(path):
    """One token per data folder, kept across launches so an open tab or bookmark keeps working."""
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        token = ""
    if len(token) < 20:
        token = secrets.token_urlsafe(24)
        write_private(path, token)
    return token


def running(lock, token):
    """The link of the Spool that owns this lockfile, if it still answers to our token."""
    # shortcut: two launches in the same instant can both start; a real lock (flock) if that ever bites
    try:
        port = int(json.loads(lock.read_text())["port"])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{port}", "X-Spool-Token": token})
        answered = conn.getresponse().status == 200
        conn.close()
        if answered:
            return f"http://127.0.0.1:{port}/#t={token}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_private(path, text):
    """Only this user may read it: the token in it opens Spool."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def interrupt(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
