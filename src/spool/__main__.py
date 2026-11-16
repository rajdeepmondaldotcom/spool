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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spool", description="A local web app for yt-dlp and youtube-dl.")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    data = system.data_dir()
    lock = data / "spool.lock"
    url = running(lock)
    if url:
        print(f"Spool is already running at {url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return

    engines = Engines(data / "engines")
    jobs = Jobs(data, engines)
    token = secrets.token_urlsafe(24)
    try:
        httpd = server.make_server(jobs, engines, token, args.port)
    except OSError as exc:
        jobs.close()
        print(f"Spool could not use port {args.port}: {exc.strerror}", flush=True)
        sys.exit(1)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={token}"
    write_private(lock, json.dumps({"port": port, "pid": os.getpid(), "token": token}))
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


def running(lock):
    """The link of the Spool that owns this lockfile, if it still answers to its token."""
    # shortcut: two launches in the same instant can both start; a real lock (flock) if that ever bites
    try:
        info = json.loads(lock.read_text())
        conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=2)
        conn.request("GET", f"/?t={info['token']}", headers={"Host": f"127.0.0.1:{info['port']}"})
        if conn.getresponse().status == 303:
            return f"http://127.0.0.1:{info['port']}/?t={info['token']}"
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
