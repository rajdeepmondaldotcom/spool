"""spool [--port N] [--no-open]: start the local server and open it in the browser."""

import argparse
import http.client
import json
import os
import secrets
import signal
import sys
import webbrowser
from urllib.parse import quote

from . import server, system
from .engines import Engines, JsRuntime, write_atomic
from .jobs import Jobs

PORT = 7766  # fixed by default, so a tab or bookmark survives a restart


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="spool", description="Save any video to your own disk. Runs on this computer and opens in your browser."
    )
    parser.add_argument("--port", type=int, help=f"port on 127.0.0.1 (default: {PORT}, else a free one)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    parser.add_argument("links", nargs="*", metavar="link", help="links to put in Spool's field")
    args = parser.parse_args(argv)

    data = system.data_dir()
    lock = data / "spool.lock"
    url = running(lock)
    if url:
        print(f"Spool is already running at {url}", flush=True)
        if not args.no_open:
            webbrowser.open(url + carrying(args.links))
        return

    engines, js = Engines(data / "engines"), JsRuntime(data / "tools")
    jobs = Jobs(data, engines, tools=lambda: system.tools(js), setup=js.start)
    # A new token every launch: an old tab's, or one a stranger on this port collected, opens nothing.
    token = secrets.token_urlsafe(24)
    try:
        httpd = server.make_server(jobs, engines, token, PORT if args.port is None else args.port)
    except OSError as exc:
        if args.port is not None:
            jobs.close()
            print(f"Spool couldn't use port {args.port}: {exc.strerror}", flush=True)
            sys.exit(1)
        httpd = server.make_server(jobs, engines, token, 0)  # something else holds the default port
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={token}"  # a fragment never leaves the browser
    write_private(lock, json.dumps({"port": port, "pid": os.getpid(), "token": token}))
    stops = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
    for stop in stops[1:]:  # SIGHUP: the terminal window was closed
        signal.signal(stop, interrupt)
    try:  # from here a stop (Ctrl+C, SIGTERM, SIGHUP) at any moment shuts down quietly
        engines.start()
        js.start()
        print(f"Spool is running at {url}", flush=True)
        print("Press Ctrl+C to stop it.", flush=True)
        if not args.no_open:
            # shortcut: on Linux the browser's command line, which `ps` shows other users, carries this launch's
            # token while that browser runs; a 0600 redirect file would hide it, but a confined (snap) browser
            # can't read Spool's data folder. Upgrade if Spool is used on shared Linux machines.
            webbrowser.open(url + carrying(args.links))
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for stop in stops:  # a second Ctrl+C must not cut the clean-up short and leave workers running
            signal.signal(stop, signal.SIG_IGN)
        httpd.server_close()
        jobs.close()  # kills every worker's process group; their jobs read as interrupted next time
        lock.unlink(missing_ok=True)


def carrying(links):
    """The fragment part that puts links in the page's field; the page waits for Enter before reading them."""
    return "&url=" + quote(" ".join(links), safe="") if links else ""


def running(lock):
    """The link of the Spool that wrote this lockfile, if its process is alive and it answers to its token."""
    # shortcut: two launches in the same instant can both start; a real lock (flock) if that ever bites
    try:
        saved = json.loads(lock.read_text())
        port, pid, token = int(saved["port"]), int(saved["pid"]), str(saved["token"])
        os.kill(pid, 0)  # a dead Spool's port may belong to anything now: never send it a token
        # the event stream answers after a destination check, which can take CHECK_TIMEOUT on a sleeping drive
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=system.CHECK_TIMEOUT + 2)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{port}", "X-Spool-Token": token})
        reply = conn.getresponse()
        answered = reply.status == 200 and reply.getheader("Server", "").startswith("spool")
        conn.close()
        if answered:
            return f"http://127.0.0.1:{port}/#t={token}"
    except (OSError, ValueError, KeyError, TypeError):  # PermissionError: that pid is another user's now
        pass
    return None


def write_private(path, text):
    """Only this user may read it: the token in it opens Spool. (write_atomic creates files with mode 0600.)"""
    write_atomic(path, text.encode())


def interrupt(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
