"""The queue: jobs and their states, settings, the one state file, live events and plain-word errors."""

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import system
from .engines import RELEASES, write_atomic

WORKER = Path(__file__).with_name("worker.py")
PRESETS = ("best", "2160", "1440", "1080", "720", "480", "360", "240", "audio", "mp3")
AT_ONCE = 2
TICK = 0.25  # job progress goes out at most once a tick: four a second


class Problem(Exception):
    """An error the browser sees: an HTTP status and {kind, message, hint}."""

    def __init__(self, status, kind, message, hint=None):
        super().__init__(message)
        self.status, self.kind, self.message, self.hint = status, kind, message, hint


class Jobs:
    def __init__(self, data_dir, engines):
        self.data, self.engines = Path(data_dir), engines
        self.state_file, self.logs = self.data / "state.json", self.data / "logs"
        self.logs.mkdir(exist_ok=True)
        saved = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.settings = {
            "dest": str(system.default_dest()),
            "engine": "yt-dlp",
            "preset": "best",
            **saved.get("settings", {}),
        }
        self.jobs = saved.get("jobs", [])  # newest first
        self.lock = threading.RLock()
        self.procs = {}  # job id -> the worker running it
        self.dirty = set()
        self.subscribers = []
        self.closed = False
        for job in self.jobs:
            if job["state"] in ("queued", "running"):
                message = "Spool was closed before this download finished. Retry to resume it."
                job.update(state="failed", stage=None, error={"kind": "interrupted", "message": message, "hint": None})
        self._save()
        engines.on_change = self._snapshot_changed  # the event stream lives here, so engines report to it
        threading.Thread(target=self._tick, daemon=True).start()

    # Reading

    def snapshot(self):
        dest = system.check_dest(self.settings["dest"])
        with self.lock:
            jobs = [dict(j, file_exists=bool(j["path"]) and Path(j["path"]).exists()) for j in self.jobs]
            return {
                "settings": dict(self.settings),
                "dest": dest,
                "engines": self.engines.status(),
                "tools": system.tools(),
                "jobs": jobs,
            }

    def subscribe(self):
        """A queue of (event, data), starting with a snapshot. (None, None) means Spool is closing."""
        q = queue.Queue()
        with self.lock:
            q.put(("snapshot", self.snapshot()))
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # Probe and add

    def probe(self, url, engine=None):
        url, engine = link(url), self._engine(engine)
        events = self._call({"mode": "probe", "url": url, "engine": engine})
        result = next((e for e in reversed(events) if e.get("event") in ("info", "error")), None)
        if result is None:
            raise Problem(422, "engine", "The engine stopped without an answer.", "update_engine")
        if result["event"] == "error":
            kind, message, hint = classify(result["message"])
            raise Problem(422, kind, message, hint)
        if not result["entries"]:
            other = "youtube-dl" if engine == "yt-dlp" else "yt-dlp"
            raise Problem(422, "empty", f"{engine} found no videos at this link. Try {other}.", "try_other_engine")
        return {"engine": engine, "kind": result["kind"], "title": result["title"], "entries": result["entries"]}

    def add(self, entries, preset, engine=None):
        engine = self._engine(engine)
        if preset not in PRESETS:
            raise Problem(422, "invalid_preset", "That quality is not one Spool knows.")
        if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
            raise Problem(422, "invalid_entries", "Choose at least one video to download.")
        dest = self.settings["dest"]
        dest_problem(system.check_dest(dest), status=409)
        new = [new_job(e, preset, engine, dest) for e in entries]
        with self.lock:
            added = []
            for job in new:
                same = next((j for j in self.jobs if j["state"] in ("queued", "running") and key(j) == key(job)), None)
                if same is None:
                    self.jobs.insert(0, job)
                    self._changed(job)
                added.append(dict(same or job))
            self._pump()
        return added

    # Job control

    def cancel(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job["state"] not in ("queued", "running"):
                raise Problem(409, "conflict", "Only a waiting or running download can be cancelled.")
            job.update(state="cancelled", stage=None, speed=None, eta=None, finished=time.time())
            self._changed(job)
            proc = self.procs.get(job_id)
            self._pump()
        if proc:
            system.kill_soon(proc)
        return dict(job)

    def retry(self, job_id, engine=None):
        """Queue a finished, failed or cancelled job again; the engine resumes a partial file."""
        with self.lock:
            job = self._job(job_id)
            if job["state"] in ("queued", "running"):
                raise Problem(409, "conflict", "This download is still going.")
            engine = self._engine(engine or job["engine"])
            job.update(
                engine=engine,
                state="queued",
                stage=None,
                part=0,
                downloaded=None,
                total=None,
                speed=None,
                eta=None,
                path=None,
                size=None,
                file_exists=False,
                error=None,
                finished=None,
            )
            self._changed(job)
            copy = dict(job)
            self._pump()
        return copy

    def remove(self, job_id):
        """Forget a job and delete its partial leftovers. The finished file is never touched."""
        with self.lock:
            job = self._job(job_id)
            if job["state"] in ("queued", "running"):
                raise Problem(409, "conflict", "Cancel this download before removing it.")
            proc = self.procs.get(job_id)
        if proc:
            try:
                proc.wait(5)  # a cancelled worker may still be writing its partial file
            except subprocess.TimeoutExpired:
                pass
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
            self._publish("job_removed", {"id": job_id})
            self._save()
            keep = {job["path"]} | {j["path"] for j in self.jobs}
            keep |= {f for j in self.jobs if j["state"] in ("queued", "running") for f in j["files"]}
        (self.logs / f"{job_id}.log").unlink(missing_ok=True)
        remove_leftovers(job, keep)

    def reveal(self, job_id):
        with self.lock:
            path = self._job(job_id)["path"]
        if not path or not os.path.exists(path):
            raise Problem(410, "gone", "The file is no longer there. It may have been moved or deleted.")
        system.reveal(path)

    def log(self, job_id):
        with self.lock:
            self._job(job_id)
        path = self.logs / f"{job_id}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    # Settings

    def update_settings(self, changes):
        """Save settings. A destination with a problem is still saved: the drive may come back."""
        changes = dict(changes)
        for key in changes.keys() - {"dest", "engine", "preset"}:
            raise Problem(422, "invalid_setting", f"Spool has no setting called {key}.")
        if "dest" in changes:
            dest = changes["dest"]
            dest = os.path.expanduser(dest) if isinstance(dest, str) else ""
            if not os.path.isabs(dest):
                raise Problem(422, "invalid_dest", "The destination must be a full folder path.")
            changes["dest"] = os.path.normpath(dest)
        if "engine" in changes and changes["engine"] not in RELEASES:
            raise Problem(422, "invalid_engine", "That engine is not one Spool knows.")
        if "preset" in changes and changes["preset"] not in PRESETS:
            raise Problem(422, "invalid_preset", "That quality is not one Spool knows.")
        with self.lock:
            self.settings.update(changes)
            self._save()
            self._publish("snapshot", self.snapshot())
        return {"settings": dict(self.settings), "dest": system.check_dest(self.settings["dest"])}

    def close(self):
        with self.lock:
            self.closed = True
            procs = list(self.procs.values())
            for q in self.subscribers:
                q.put((None, None))
        for proc in procs:
            system.kill(proc)

    # Inside

    def _job(self, job_id):
        job = next((j for j in self.jobs if j["id"] == job_id), None)
        if job is None:
            raise Problem(404, "not_found", "Spool has no download with that id.")
        return job

    def _engine(self, engine):
        engine = engine or self.settings["engine"]
        if engine not in RELEASES:
            raise Problem(422, "invalid_engine", "That engine is not one Spool knows.")
        if not self.engines.path(engine):
            raise Problem(503, "engine_not_ready", f"{engine} is not installed yet.")
        return engine

    def _spec(self, engine, **spec):
        tools = system.tools()
        return {
            "engine": engine,
            "engine_path": self.engines.path(engine),
            "ffmpeg": tools["ffmpeg"],
            "js_runtime": tools["js_runtime"],
            **spec,
        }

    def _call(self, spec, timeout=120):
        try:
            out = subprocess.run(
                [sys.executable, "-I", str(WORKER)],
                input=json.dumps(self._spec(**spec)),
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout
        except subprocess.TimeoutExpired:
            raise Problem(422, "network", "The site took too long to answer.") from None
        return [e for e in map(parse, out.splitlines()) if e]

    def _pump(self):
        """Start queued jobs, oldest first, while fewer than AT_ONCE run."""
        running = sum(j["state"] == "running" for j in self.jobs)
        for job in reversed(self.jobs):
            if running >= AT_ONCE or self.closed:
                return
            if job["state"] == "queued" and job["id"] not in self.procs:
                job.update(state="running", stage="starting")
                self._changed(job)
                running += 1
                threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job):
        try:
            dest_problem(system.check_dest(job["dest"]))
        except Problem as p:
            with self.lock:
                if job["state"] == "running":
                    job.update(
                        state="failed",
                        stage=None,
                        finished=time.time(),
                        error={"kind": p.kind, "message": p.message, "hint": p.hint},
                    )
                    self._changed(job)
                    self._pump()
            return
        spec = self._spec(job["engine"], mode="download", url=job["url"], dest=job["dest"], preset=job["preset"])
        result = None
        with open(self.logs / f"{job['id']}.log", "a", encoding="utf-8") as log:
            log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {job['engine']} {job['preset']} {job['url']}\n")
            log.flush()
            with self.lock:
                if self.closed:
                    return
                proc = system.spawn(
                    [sys.executable, "-I", str(WORKER)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    encoding="utf-8",
                )
                self.procs[job["id"]] = proc
            proc.stdin.write(json.dumps(spec))
            proc.stdin.close()
            for line in proc.stdout:
                event = parse(line)
                if not event or event.get("event") == "log":
                    log.write((event or {}).get("line", line.rstrip("\n")) + "\n")
                elif event["event"] == "progress":
                    self._progress(job, event)
                elif event["event"] == "meta":
                    self._details(job, event)
                elif event["event"] == "file":
                    with self.lock:
                        job["files"].append(event["path"])
                else:
                    result = event
                    break  # a child the engine left behind may hold the pipe open; this was the last word
            proc.wait()
            system.kill_leftovers(proc)
        with self.lock:
            self.procs.pop(job["id"], None)
            if self.closed:
                return
            if job["state"] != "running":  # cancelled while it ran, and perhaps retried since
                self._pump()
                return
            if result and result["event"] == "done":
                job.update(state="done", path=result["path"], size=result["size"], file_exists=True)
            else:
                kind, message, hint = classify(result["message"] if result else "")
                try:
                    dest_problem(system.check_dest(job["dest"]))  # a drive unplugged mid-download says so
                except Problem as p:
                    kind, message, hint = p.kind, p.message, p.hint
                job.update(state="failed", error={"kind": kind, "message": message, "hint": hint})
            job.update(stage=None, speed=None, eta=None, finished=time.time())
            self._changed(job)
            self._pump()

    def _progress(self, job, event):
        with self.lock:
            if job["state"] != "running":
                return
            if event["status"] == "downloading":
                job.update(
                    stage="downloading",
                    part=event["part"],
                    downloaded=event["downloaded"],
                    total=event["total"],
                    speed=event["speed"],
                    eta=event["eta"],
                )
            elif event["status"] == "finished":
                job.update(stage="processing", downloaded=event["total"], speed=None, eta=None)
            self.dirty.add(job["id"])

    def _details(self, job, event):
        """Fill in what the entry did not supply; never overwrite what it did."""
        with self.lock:
            fresh = new_job({**event, "url": job["url"]}, job["preset"], job["engine"], job["dest"])
            for key in ("title", "uploader", "thumbnail", "duration"):
                if (job[key] is None or (key == "title" and job[key] == job["url"])) and fresh[key] is not None:
                    job[key] = fresh[key]
            self.dirty.add(job["id"])

    def _changed(self, job):
        """A state change: sent now, never dropped, and saved."""
        self.dirty.discard(job["id"])
        self._publish("job", dict(job))
        self._save()

    def _publish(self, event, data):
        for q in self.subscribers:
            q.put((event, data))

    def _save(self):
        write_atomic(self.state_file, json.dumps({"settings": self.settings, "jobs": self.jobs}).encode())

    def _snapshot_changed(self):
        with self.lock:
            if not self.closed:
                self._publish("snapshot", self.snapshot())

    def _machine(self):
        return dict(system.check_dest(self.settings["dest"]), free_bytes=None), system.tools()

    def _tick(self):
        """Flush throttled job progress. Destination and tools have no events, so poll them once a second."""
        last, n = self._machine(), 0
        while not self.closed:
            time.sleep(TICK)
            with self.lock:
                for job in self.jobs:
                    if job["id"] in self.dirty:
                        self._publish("job", dict(job))
                self.dirty.clear()
            n += 1
            if n % 4 == 0:  # the destination write test runs once a second, not every tick
                now = self._machine()
                if now != last:
                    self._snapshot_changed()
                last = now


def dest_problem(check, status=None):
    """Raise the plain-word problem for a destination check, if it has one."""
    if check["problem"] in ("missing", "not_a_folder"):
        raise Problem(
            status,
            "dest_missing",
            f"The destination {check['display']} is not there. Is the drive plugged in?",
            "check_dest",
        )
    if check["problem"] == "not_writable":
        raise Problem(status, "dest_denied", f"Spool cannot write to {check['display']}.", "check_dest")


def key(job):
    """Jobs with the same key would write the same file."""
    return job["url"], job["preset"], job["dest"]


def parse(line):
    try:
        event = json.loads(line)
    except ValueError:
        return None  # engines may print Python warnings; only JSON lines are events
    return event if isinstance(event, dict) else None


def link(url):
    """A link must be http or https with a host; it reaches the engine as data only."""
    parts = urlsplit(url) if isinstance(url, str) and len(url) <= 4096 else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        raise Problem(422, "invalid_link", "That is not a link Spool can use. Links start with http:// or https://.")
    return url


def text(value, limit=1000):
    return str(value)[:limit] if value is not None else None


def new_job(entry, preset, engine, dest):
    thumb = entry.get("thumbnail")
    if not (isinstance(thumb, str) and thumb.startswith(("https://", "http://", "data:image/"))):
        thumb = None
    duration = entry.get("duration")
    return {
        "id": secrets.token_hex(4),
        "url": link(entry.get("url")),
        "entry_id": text(entry.get("id"), 200),
        "title": text(entry.get("title")) or entry["url"],
        "uploader": text(entry.get("uploader")),
        "thumbnail": thumb,
        "duration": duration if isinstance(duration, (int, float)) else None,
        "preset": preset,
        "engine": engine,
        "dest": dest,
        "state": "queued",
        "stage": None,
        "part": 0,
        "downloaded": None,
        "total": None,
        "speed": None,
        "eta": None,
        "path": None,
        "size": None,
        "file_exists": False,
        "files": [],  # every name the engine wrote, temporary ones included: what Remove cleans up
        "error": None,
        "created": time.time(),
        "finished": None,
    }


ERRORS = [  # (kind, hint, plain words, lowercase pieces of engine messages); the first match wins
    ("unsupported", "try_other_engine", "The engine cannot read this link.", ("unsupported url",)),
    (
        "no_ffmpeg",
        "install_ffmpeg",
        "This quality needs ffmpeg, which is not installed.",
        (
            "ffmpeg not found",
            "ffprobe and ffmpeg not found",
            "ffmpeg is not installed",
            "ffprobe/avprobe and ffmpeg/avconv",
        ),
    ),
    (
        "unavailable",
        None,
        "This video is not available. It may be private, removed, age-restricted or blocked where you are.",
        (
            "video unavailable",
            "private video",
            "is not available",
            "has been removed",
            "members-only",
            "confirm your age",
            "age-restricted",
            "in your country",
            "geo restrict",
            "http error 404",
            "does not exist",
        ),
    ),
    (
        "blocked",
        "update_engine",
        "The site refused the download. Updating the engine and retrying usually fixes this.",
        ("http error 403", "sign in to confirm", "not a bot", "http error 429", "too many requests"),
    ),
    (
        "dest_missing",
        "check_dest",
        "The destination folder is not there. Is the drive plugged in?",
        ("destination folder is missing",),
    ),
    ("dest_full", "check_dest", "The destination is full. Free some space and retry.", ("no space left", "errno 28")),
    (
        "dest_denied",
        "check_dest",
        "Spool cannot write to the destination folder.",
        ("permission denied", "read-only file system", "errno 13", "errno 30"),
    ),
    (
        "network",
        None,
        "Spool could not reach the site. Check your internet connection.",
        (
            "unable to download webpage",
            "urlopen error",
            "timed out",
            "connection reset",
            "connection refused",
            "nodename nor servname",
            "name or service not known",
            "getaddrinfo",
            "network is unreachable",
            "temporary failure in name resolution",
            "connection aborted",
        ),
    ),
    (
        "engine",
        "install_js_runtime",
        "The engine needs a JavaScript runtime such as deno for this site.",
        ("javascript runtime", "js runtime"),
    ),
    # any other local file-system error: updating the engine would not help
    ("engine", None, "Spool could not save the file. Show the log for the details.", ("[errno ", "winerror")),
]


def classify(message):
    """An engine's message -> (kind, plain words, hint). The engine's own words stay in the job's log."""
    low = message.lower()
    for kind, hint, words, pieces in ERRORS:
        if any(p in low for p in pieces):
            return kind, words, hint
    first = message.removeprefix("ERROR: ").strip().splitlines()[0] if message.strip() else ""
    return "engine", f"The engine failed: {first}" if first else "The engine stopped unexpectedly.", "update_engine"


def remove_leftovers(job, keep):
    """Delete what the engine wrote for this job, except any finished file and what a running job still uses."""
    names = set()
    for f in job["files"]:
        names |= {f, f + ".part", f + ".ytdl"}
        if job["preset"] == "mp3" and job["state"] != "done":  # ffmpeg's half-written .mp3 looks finished
            mp3 = os.path.splitext(f.removesuffix(".part"))[0] + ".mp3"
            if os.path.exists(mp3) and os.path.getmtime(mp3) >= job["created"]:  # never an older job's mp3
                names.add(mp3)
    for name in names - keep:
        if os.path.dirname(name) == job["dest"] and os.path.isfile(name):
            os.unlink(name)
