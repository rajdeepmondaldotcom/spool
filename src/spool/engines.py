"""Engines are files Spool manages: fetched from the official release, checksum-verified, versioned, switched last."""

import hashlib
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

RELEASES = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp/releases",
    "youtube-dl": "https://github.com/ytdl-org/ytdl-nightly/releases",
}
TAG = re.compile(r"[\w.\-]{1,64}")


def http_get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


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
        current = self.root / name / "current"
        return current.read_text().strip() if current.exists() else None

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
        except ChecksumMismatch:
            error = "The engine Spool downloaded didn't match its published checksum, so Spool kept the one it had."
        except OSError:
            error = "Spool couldn't update its engine. Check your internet connection."
        except Exception:
            error = "Spool couldn't update its engine."
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
        write_atomic(folder / "current", tag.encode())


class ChecksumMismatch(Exception):
    pass


def write_atomic(path, data):
    """Replace path with data in one step. The temp name is unique, so two writers never share it; mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
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
