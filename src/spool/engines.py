"""Engines are files Spool manages: fetched from the official release, checksum-verified, versioned, switched last."""

import functools
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

RELEASES = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp/releases",
    "youtube-dl": "https://github.com/ytdl-org/ytdl-nightly/releases",
}
MODULE = {"yt-dlp": "yt_dlp", "youtube-dl": "youtube_dl"}
TAG = re.compile(r"v?\d[\w.\-]{0,62}", re.ASCII)  # a version: never ".", "..", "current" or "latest"
LIMIT = 300 << 20  # bytes in one fetched or unpacked file; deno's program, the largest, is about 80 MB


def http_get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"{url} is larger than {LIMIT} bytes")
    return data


def log(what, exc):
    print(f"Spool couldn't {what}: {exc!r}", file=sys.stderr, flush=True)  # the cause, for whoever reads the terminal


def final_url(url):
    """Where a redirect ends. The latest version is the tag in the redirect from releases/latest."""
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
        return r.geturl()


class Busy(Exception):
    pass


class Engines:
    def __init__(self, root, fetch=http_get, resolve=final_url):
        self.root, self.fetch, self.resolve = Path(root), fetch, resolve
        self.lock = threading.Lock()
        self.busy = dict.fromkeys(RELEASES)
        self.error = dict.fromkeys(RELEASES)
        self.latest = dict.fromkeys(RELEASES)
        self.on_change = lambda: None  # called, outside the lock, whenever status() would answer differently

    def version(self, name):
        """The current version, while its file is there: a deleted file is installed again."""
        current = self.root / name / "current"
        tag = current.read_text().strip() if current.exists() else None
        return tag if tag and (self.root / name / tag).exists() else None

    def path(self, name):
        """The installed engine file, or None."""
        version = self.version(name)
        return str(self.root / name / version) if version else None

    def status(self):
        with self.lock:
            return {
                name: {
                    "installed": self.version(name) is not None,
                    "version": self.version(name),
                    "latest": self.latest[name],
                    "busy": self.busy[name],
                    "error": self.error[name],
                }
                for name in RELEASES
            }

    def start(self):
        """Delete older engine files (no worker can be using one yet), then in the background install or update
        each engine to the latest release."""
        for name in RELEASES:
            folder, keep = self.root / name, {self.version(name), "current"}
            for f in folder.iterdir() if folder.is_dir() else ():
                if f.name not in keep and f.is_file():
                    f.unlink()
        for name in RELEASES:  # at launch no job runs, so an engine can update itself
            self.update(name)

    def update(self, name):
        """Install or update in the background. Raises KeyError for an unknown engine, Busy if one is running."""
        RELEASES[name]
        with self.lock:
            if self.busy[name]:
                raise Busy(name)
            self.busy[name] = "updating" if self.version(name) else "installing"
            self.error[name] = None
        self.on_change()
        threading.Thread(target=self._update, args=(name,), daemon=True).start()

    def _latest(self, name):
        tag = self.resolve(f"{RELEASES[name]}/latest").rstrip("/").rsplit("/", 1)[-1]
        if not TAG.fullmatch(tag):
            raise ValueError(f"unexpected release tag {tag!r}")
        with self.lock:
            self.latest[name] = tag
        self.on_change()
        return tag

    def _update(self, name):
        error = None
        try:
            tag = self._latest(name)
            if tag != self.version(name):
                self._install(name, tag)
        except Exception as exc:
            log(f"update {name}", exc)
            error = (
                "The engine Spool downloaded didn't match its published checksum, so Spool kept the one it had."
                if isinstance(exc, ChecksumMismatch)
                else "Spool couldn't update its engine. Check your internet connection."
                if isinstance(exc, OSError)
                else "Spool couldn't update its engine."
            )
        with self.lock:
            self.busy[name] = None
            self.error[name] = error
        self.on_change()

    def _install(self, name, tag):
        base, asset = f"{RELEASES[name]}/download/{tag}", name  # each release names its file after the engine
        sums = self.fetch(f"{base}/SHA2-256SUMS").decode()
        expected = next((line.split()[0] for line in sums.splitlines() if line.split()[1:] == [asset]), None)
        if expected is None:
            raise ValueError(f"the release has no checksum for {asset}")
        data = self.fetch(f"{base}/{asset}")
        if hashlib.sha256(data).hexdigest() != expected.lower():
            raise ChecksumMismatch()
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        # the old file stays until the next start: a running worker may still be reading it
        write_atomic(folder / tag, data)
        loads = False
        try:  # a release that cannot load on this Python never becomes current
            code = "import sys; sys.path.insert(0, sys.argv[1]); import " + MODULE[name]
            check = subprocess.run(
                [sys.executable, "-I", "-c", code, str(folder / tag)], capture_output=True, timeout=120
            )
            loads = check.returncode == 0
        finally:
            if not loads:
                (folder / tag).unlink()
        if not loads:
            raise RuntimeError(f"{tag} does not load: {check.stderr.decode(errors='replace').strip()[-500:]}")
        write_atomic(folder / "current", tag.encode())


class ChecksumMismatch(Exception):
    pass


DENO = "https://github.com/denoland/deno/releases"
DENO_TARGETS = {  # (sys.platform, machine) -> the release's target name
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("win32", "AMD64"): "x86_64-pc-windows-msvc",
}
JS_ON_PATH = ("deno", "node", "bun")
JS_FLOORS = {"deno": (2, 3, 0), "node": (22, 0, 0), "bun": (1, 2, 11)}  # yt-dlp's own, in yt_dlp/utils/_jsruntime.py
SHA256 = re.compile(r"\b[0-9a-fA-F]{64}\b")  # deno's .sha256sum is "<hex>  <name>", or PowerShell's list on Windows


@functools.cache  # shortcut: a runtime upgraded in place is seen at the next launch; fine for a local app
def new_enough(name, path):
    """Whether a runtime on PATH is one the engine will use: an older one is skipped and YouTube fails."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return bool(found) and tuple(map(int, found.groups())) >= JS_FLOORS[name]


class JsRuntime:
    """YouTube's JavaScript runtime: one on PATH if the machine has it, else deno fetched from its official release
    the way the engines are (temp file, published SHA-256, switched last)."""

    def __init__(self, root, fetch=http_get, resolve=final_url, which=shutil.which):
        self.root, self.fetch, self.resolve, self.which = Path(root) / "deno", fetch, resolve, which
        self.lock = threading.Lock()
        self.setup = None  # None, "installing" or "failed"
        self.busy = self.checked = False  # a fetch is running; the latest release was looked up this run

    def tools(self):
        """{js_runtime, js_setup, js_path}: js_path is Spool's own deno, None for a runtime on PATH."""
        on_path = next((n for n in JS_ON_PATH if (path := self.which(n)) and new_enough(n, path)), None)
        current = self.root / "current"
        own = self.root / current.read_text().strip() / exe() if not on_path and current.exists() else None
        own = own if own and own.exists() else None
        with self.lock:
            return {"js_runtime": on_path or (own and "deno"), "js_setup": self.setup, "js_path": own and str(own)}

    def start(self):
        """Fetch deno in the background if nothing on PATH will do, and a newer one once a run. Safe to call often:
        one fetch at a time, and a failed first fetch is tried again."""
        tools = self.tools()
        with self.lock:
            if self.busy or (tools["js_runtime"] and (not tools["js_path"] or self.checked)):
                return
            self.busy = True
            if not tools["js_path"]:  # while an older own deno serves, nothing is being set up
                self.setup = "installing"
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        setup = None
        try:
            tag = self.resolve(f"{DENO}/latest").rstrip("/").rsplit("/", 1)[-1]
            if not TAG.fullmatch(tag):
                raise ValueError(f"unexpected release tag {tag!r}")
            if not (self.root / tag / exe()).exists():
                self._fetch(tag)
            write_atomic(self.root / "current", tag.encode())
            for old in self.root.iterdir():  # older versions; one a worker still runs keeps going on POSIX
                if old.is_dir() and old.name != tag and TAG.fullmatch(old.name):
                    shutil.rmtree(old, ignore_errors=True)
        except Exception as exc:  # offline, a bad download, an unknown platform: say so, and a later start tries again
            log("fetch YouTube's helper", exc)
            setup = None if self.tools()["js_path"] else "failed"
        with self.lock:
            self.setup, self.busy, self.checked = setup, False, True

    def _fetch(self, tag):
        asset = f"deno-{DENO_TARGETS[(sys.platform, platform.machine())]}.zip"
        expected = SHA256.search(self.fetch(f"{DENO}/download/{tag}/{asset}.sha256sum").decode())
        if not expected:
            raise ValueError(f"no checksum for {asset}")
        data = self.fetch(f"{DENO}/download/{tag}/{asset}")
        if hashlib.sha256(data).hexdigest() != expected.group().lower():
            raise ChecksumMismatch()
        archive = zipfile.ZipFile(io.BytesIO(data))
        if archive.getinfo(exe()).file_size > LIMIT:
            raise ValueError(f"{exe()} in {asset} is larger than {LIMIT} bytes")
        program = archive.read(exe())  # the one file, never a path from the archive
        folder = self.root / tag
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(folder / exe(), program)
        os.chmod(folder / exe(), 0o700)


def exe():
    return "deno.exe" if sys.platform == "win32" else "deno"


_replacing = threading.Lock()


def write_atomic(path, data):
    """Replace path with data in one step. The temp name is unique, so two writers never share it; mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with _replacing:  # our own threads take turns; the retries below are for another Spool process
            for wait in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, None):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:  # Windows refuses while another writer is replacing the same file
                    if wait is None or sys.platform != "win32":
                        raise
                    time.sleep(wait)
    except BaseException:
        os.unlink(tmp)  # our own temp file, never anything else
        raise
