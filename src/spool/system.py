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

from .engines import JS_ON_PATH


def data_dir():
    if os.environ.get("SPOOL_DATA_DIR"):
        path = Path(os.environ["SPOOL_DATA_DIR"]).absolute()  # a worker moves into the destination folder
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


def check_dest(path, write=True):
    """The destination's problem, or none. Waits CHECK_TIMEOUT at most; one check per path runs at a time.

    write=False skips the real write test, for polling: it never wakes a sleeping drive or touches a synced folder."""
    path = str(path)
    with _checks_lock:
        flight = _checks.get((path, write))
        if flight is None or not flight[0].is_alive():
            result = {}
            probe = probe_dest if write else quick_dest
            thread = threading.Thread(target=lambda: result.update(probe(path)), daemon=True)
            flight = _checks[(path, write)] = (thread, time.monotonic(), result)
            thread.start()
    thread, started, result = flight
    thread.join(max(0.0, started + CHECK_TIMEOUT - time.monotonic()))
    if thread.is_alive() or not result:  # still stuck, or the check itself failed
        p = Path(path)
        return {"path": str(p), "display": display(p), "ok": False, "problem": "not_responding", "free_bytes": None}
    return result


def probe_dest(path):
    """Existence and a real write test. Never creates anything that is missing."""
    return probe(path, write=True)


def quick_dest(path):
    """Existence and the permission bits: nothing is written."""
    return probe(path, write=False)


def probe(path, write):
    p, problem, free = Path(path), None, None
    try:
        if not p.exists():
            problem = "unplugged" if unplugged(p) else "missing"
        elif not p.is_dir():
            problem = "not_a_folder"
        elif not write:
            problem = None if os.access(p, os.W_OK) else "not_writable"
        else:
            try:
                with tempfile.TemporaryFile(dir=p, prefix=".spool-write-test-"):
                    pass
            except OSError:
                problem = "not_writable"
        free = shutil.disk_usage(p).free if problem in (None, "not_writable") else None
    except PermissionError:  # a parent folder Spool may not enter
        problem = "not_writable"
    except OSError:  # a share that dropped answers with an input/output error
        problem, free = "not_responding", None
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


def tools(js=None):
    """What the machine has. js: a JsRuntime, which also knows about Spool's own deno and its setup."""
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        **(js.tools() if js else {"js_runtime": on_path(), "js_setup": None, "js_path": None}),
        "platform": sys.platform,
        "can_choose_folder": sys.platform == "darwin",
    }


def on_path():
    return next((name for name in JS_ON_PATH if shutil.which(name)), None)


def choose_folder():
    """The native folder dialog. Returns a path, or None if the user cancelled.

    NotImplementedError off macOS, and when the dialog can't open (over SSH, or automation not permitted)."""
    if sys.platform != "darwin":
        raise NotImplementedError
    script = 'POSIX path of (choose folder with prompt "Choose where Spool saves downloads")'
    # without activating first, the dialog can open behind the browser
    try:
        result = subprocess.run(
            ["osascript", "-e", "tell me to activate", "-e", script], capture_output=True, text=True, check=False
        )
    except OSError:
        raise NotImplementedError from None
    if result.returncode != 0:
        if "(-128)" in result.stderr:  # the user pressed Cancel
            return None
        raise NotImplementedError(result.stderr.strip())
    return result.stdout.strip().rstrip("/") or "/"


def reveal(path):
    """Show the file in the file manager. NotImplementedError when the machine has none (headless Linux, WSL)."""
    if sys.platform == "darwin":
        run(["open", "-R", path])
    elif sys.platform == "win32":
        # Explorer reads /select, itself: quoting the whole argument, as a list would, opens a default folder
        run(f'explorer /select,"{path}"')
    else:  # shortcut: opens the folder without selecting the file; Linux file managers have no common select flag
        run(["xdg-open", os.path.dirname(path)])


def open_folder(path):
    """Open a folder in the file manager. NotImplementedError when the machine has none."""
    run(["open" if sys.platform == "darwin" else "explorer" if sys.platform == "win32" else "xdg-open", path])


def open_file(path):
    """Open a file with the system's default program. NotImplementedError when the machine has none."""
    if sys.platform == "win32":
        try:
            os.startfile(path)
        except OSError:  # no program is set for this type
            raise NotImplementedError from None
    else:
        run(["open" if sys.platform == "darwin" else "xdg-open", path])


def run(args):
    try:
        subprocess.run(args, check=False)
    except OSError:
        raise NotImplementedError from None


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
    except (ProcessLookupError, PermissionError):  # gone, or only a zombie leader is left (macOS says EPERM)
        pass


def kill_leftovers(proc):
    """Wait for a worker to exit, stop anything it left running in its group, then reap it.

    Reaping last keeps the worker's pid, and so its group's number, from being reused by an unrelated process."""
    if sys.platform != "win32":  # shortcut: on Windows a stray grandchild outlives the worker; fix if one shows up
        if proc.returncode is None and hasattr(os, "waitid"):
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        else:  # shortcut: macOS before Python 3.13 has no waitid, so there the worker is reaped first and its
            proc.wait()  # group's number could, for an instant, belong to something else; upgrade when 3.13 is the floor
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.wait()


def kill_soon(proc):
    threading.Thread(target=kill, args=(proc,), daemon=True).start()
