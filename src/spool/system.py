"""What Spool needs from the machine: the data folder, destination checks, tools, child processes."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def data_dir():
    if os.environ.get("SPOOL_DATA_DIR"):
        path = Path(os.environ["SPOOL_DATA_DIR"])
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "spool"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "spool"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "spool"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def default_dest():
    return Path.home() / "Downloads"


CHECK_TIMEOUT = 3.0  # a sleeping drive or a macOS permission prompt can block a write test for ever
MOUNTS = ("/Volumes", "/mnt", "/media", "/run/media")
_checks = {}  # path -> the one check thread running for it, and when it started
_checks_lock = threading.Lock()


def check_dest(path):
    """The destination's problem, or none. Waits CHECK_TIMEOUT at most; one check per path runs at a time."""
    path = str(path)
    with _checks_lock:
        flight = _checks.get(path)
        if flight is None or not flight[0].is_alive():
            result = {}
            thread = threading.Thread(target=lambda: result.update(probe_dest(path)), daemon=True)
            flight = _checks[path] = (thread, time.monotonic(), result)
            thread.start()
    thread, started, result = flight
    thread.join(max(0.0, started + CHECK_TIMEOUT - time.monotonic()))
    if thread.is_alive():
        p = Path(path)
        return {"path": str(p), "display": display(p), "ok": False, "problem": "not_responding", "free_bytes": None}
    return result


def probe_dest(path):
    """Existence and a real write test. Never creates anything that is missing."""
    p = Path(path)
    problem = None
    if not p.exists():
        problem = "unplugged" if unplugged(p) else "missing"
    elif not p.is_dir():
        problem = "not_a_folder"
    else:
        try:
            with tempfile.TemporaryFile(dir=p, prefix=".spool-write-test-"):
                pass
        except OSError:
            problem = "not_writable"
    free = shutil.disk_usage(p).free if problem in (None, "not_writable") else None
    return {"path": str(p), "display": display(p), "ok": problem is None, "problem": problem, "free_bytes": free}


def unplugged(p):
    """Whether the drive itself is absent: a missing drive letter, or a missing mount point under /Volumes and co."""
    if sys.platform == "win32":
        return bool(p.drive) and not os.path.exists(p.drive + "\\")
    top = next(a for a in [p, *p.parents] if a.parent.exists())  # the highest missing folder
    return str(top.parent) in MOUNTS or str(top.parent.parent) in ("/media", "/run/media")


def display(path):
    """The folder's short label: its last two names, "Movies › Spool", "T7 › Spool". Home and /Volumes are not names."""
    p, home = Path(path), Path.home()
    if p.is_relative_to(home) and p != home:  # first: on Windows home sits on a drive too
        parts = p.relative_to(home).parts
    elif sys.platform == "darwin" and p.parts[1:2] == ("Volumes",):
        parts = p.parts[2:]
    else:
        parts = p.parts[1:]
    return " › ".join(parts[-2:]) or p.name or str(p)


def tools():
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "js_runtime": next((name for name in JS_RUNTIMES if shutil.which(name)), None),
        "platform": sys.platform,
        "can_choose_folder": sys.platform == "darwin",
    }


def choose_folder():
    """The native folder dialog. Returns a path, or None if the user cancelled. NotImplementedError off macOS."""
    if sys.platform != "darwin":
        raise NotImplementedError
    script = 'POSIX path of (choose folder with prompt "Choose where Spool saves downloads")'
    # without activating first, the dialog can open behind the browser
    result = subprocess.run(
        ["osascript", "-e", "tell me to activate", "-e", script], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None  # the user pressed Cancel (error -128)
    return result.stdout.strip().rstrip("/") or "/"


def reveal(path):
    """Show the file in the file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", f"/select,{path}"], check=False)
    else:  # shortcut: opens the folder without selecting the file; Linux file managers have no common select flag
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)


def open_folder(path):
    """Open a folder in the file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def spawn(args, **kwargs):
    """Start a child in its own process group, so killing it also kills what it started (ffmpeg)."""
    if sys.platform == "win32":
        return subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **kwargs)
    return subprocess.Popen(args, start_new_session=True, **kwargs)


def kill(proc, grace=3.0):
    """Stop a child's whole process group: politely first, by force after grace seconds."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def kill_leftovers(proc):
    """After a worker exits, stop anything it left running in its group."""
    if sys.platform != "win32":  # shortcut: on Windows a stray grandchild outlives the worker; fix if one shows up
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_soon(proc):
    threading.Thread(target=kill, args=(proc,), daemon=True).start()
