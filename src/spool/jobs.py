"""The queue: jobs and their states, settings, the one state file, live events and plain-word errors."""

import json
import os
import queue
import re
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
LINK_LIMIT = 8192  # signed CDN links run to a few thousand characters
RETRY_AFTER = 3  # seconds before the one automatic retry of a download refused before any bytes
TICK = 0.25  # job progress goes out at most once a tick: four a second


class Problem(Exception):
    """An error the browser sees: an HTTP status and {kind, message, hint}."""

    def __init__(self, status, kind, message, hint=None):
        super().__init__(message)
        self.status, self.kind, self.message, self.hint = status, kind, message, hint


class Jobs:
    def __init__(self, data_dir, engines, tools=system.tools):
        self.data, self.engines, self.tools = Path(data_dir), engines, tools  # tools: what the machine has
        self.state_file, self.logs = self.data / "state.json", self.data / "logs"
        self.logs.mkdir(exist_ok=True)
        saved = self._load()
        self.settings = {"dest": str(system.default_dest()), "engine": "yt-dlp", "preset": "best"}
        valid = {
            "dest": lambda v: isinstance(v, str) and os.path.isabs(v),
            "engine": lambda v: isinstance(v, str) and v in RELEASES,
            "preset": lambda v: isinstance(v, str) and v in PRESETS,
        }
        self.settings.update({k: v for k, v in saved.get("settings", {}).items() if k in valid and valid[k](v)})
        keys = set(new_job({"url": "https://x.test"}, "best", "yt-dlp", "/")) - {"files", "item"}  # added later
        self.jobs = [  # newest first; a malformed record is dropped, not fatal
            {"files": [], "item": None, **j} for j in saved.get("jobs", []) if isinstance(j, dict) and keys <= set(j)
        ]
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
        threading.Thread(target=self._watch, daemon=True).start()

    def _load(self):
        """The saved state. A damaged file is moved aside, not fatal: Spool starts fresh and says so."""
        try:
            saved = json.loads(self.state_file.read_bytes()) if self.state_file.exists() else {}
            if not (
                isinstance(saved, dict)
                and isinstance(saved.get("settings", {}), dict)
                and isinstance(saved.get("jobs", []), list)
            ):
                raise ValueError("not the shape Spool saves")
            return saved
        except ValueError:  # bad JSON, bad UTF-8 and the wrong shape
            aside = self.state_file.with_name(f"state.json.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(self.state_file, aside)
            print(f"Spool could not read its saved state, so it started fresh; the old file is {aside}", flush=True)
            return {}

    # Reading

    def snapshot(self, dest=None):
        """Pass dest when holding the lock: checking a destination can take CHECK_TIMEOUT, so never under it."""
        if dest is None or dest["path"] != self.settings["dest"]:  # the setting changed since dest was checked
            dest = system.check_dest(self.settings["dest"])
        with self.lock:
            jobs = [dict(j, file_exists=bool(j["path"]) and Path(j["path"]).exists()) for j in self.jobs]
            return {
                "settings": dict(self.settings),
                "dest": dest,
                "engines": self.engines.status(),
                "tools": self.tools(),
                "jobs": jobs,
            }

    def subscribe(self):
        """A queue of (event, data), starting with a snapshot. (None, None) means Spool is closing."""
        q, dest = queue.Queue(), system.check_dest(self.settings["dest"])
        with self.lock:
            q.put(("snapshot", self.snapshot(dest)))
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
            line = last_error(result["message"])
            if kind == "engine" and line and line not in message:  # a probe has no log to show, so say it here
                message += f" The engine said: {line}"
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
        if "engine" in changes and not (isinstance(changes["engine"], str) and changes["engine"] in RELEASES):
            raise Problem(422, "invalid_engine", "That engine is not one Spool knows.")
        if "preset" in changes and not (isinstance(changes["preset"], str) and changes["preset"] in PRESETS):
            raise Problem(422, "invalid_preset", "That quality is not one Spool knows.")
        with self.lock:
            self.settings.update(changes)
            self._save()
        self._snapshot_changed()
        return {"settings": dict(self.settings), "dest": system.check_dest(self.settings["dest"])}

    def close(self):
        with self.lock:
            self.closed = True
            procs = [p for p in self.procs.values() if p]
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
        if not (isinstance(engine, str) and engine in RELEASES):
            raise Problem(422, "invalid_engine", "That engine is not one Spool knows.")
        if not self.engines.path(engine):
            raise Problem(503, "engine_not_ready", f"{engine} is not installed yet.")
        return engine

    def _spec(self, engine, **spec):
        tools = self.tools()
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
        """Start queued jobs, oldest first, while fewer than AT_ONCE slots are taken.

        A slot is an entry in procs: taken here, freed when the job's thread ends, so a cancelled worker that is
        still dying keeps its slot."""
        for job in reversed(self.jobs):
            if len(self.procs) >= AT_ONCE or self.closed:
                return
            if job["state"] == "queued" and job["id"] not in self.procs:
                job.update(state="running", stage="starting")
                self._changed(job)
                self.procs[job["id"]] = None  # no worker yet
                threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job):
        """Run a job in the slot _pump reserved for it; the slot is freed only when its worker is gone."""
        try:
            error = None
            for attempt in (1, 2):
                dest_problem(system.check_dest(job["dest"]))
                result = self._attempt(job)
                if result is None or result["event"] == "done":
                    break
                kind, message, hint = classify(result["message"])
                try:
                    dest_problem(system.check_dest(job["dest"]))  # a drive unplugged mid-download says so
                except Problem as p:
                    kind, message, hint = p.kind, p.message, p.hint
                error = {"kind": kind, "message": message, "hint": hint}
                if not (attempt == 1 and kind == "blocked" and not job["downloaded"]):
                    break
                time.sleep(RETRY_AFTER)  # a refusal before any bytes usually passes
        except Problem as p:
            result, error = {"event": "error"}, {"kind": p.kind, "message": p.message, "hint": p.hint}
        with self.lock:
            self.procs.pop(job["id"], None)
            if self.closed:
                return
            if job["state"] == "running" and result is not None:  # else cancelled, and perhaps retried since
                if result["event"] == "done":
                    job.update(state="done", path=result["path"], size=result["size"], file_exists=True)
                else:
                    job.update(state="failed", error=error)
                job.update(stage=None, speed=None, eta=None, finished=time.time())
                self._changed(job)
            self._pump()

    def _attempt(self, job):
        """One worker run: its last event, or None if the job was cancelled before it could start."""
        spec = self._spec(
            job["engine"], mode="download", url=job["url"], dest=job["dest"], preset=job["preset"], item=job["item"]
        )
        result = {"event": "error", "message": ""}
        with open(self.logs / f"{job['id']}.log", "a", encoding="utf-8") as log:
            with self.lock:  # cancel takes this lock too: it either sees the worker, or the worker never starts
                if self.closed or job["state"] != "running":
                    return None
                log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {job['engine']} {job['preset']} {job['url']}\n")
                log.flush()
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
        return result

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
        dest = system.check_dest(self.settings["dest"])
        with self.lock:
            if not self.closed:
                self._publish("snapshot", self.snapshot(dest))

    def _machine(self):
        return dict(system.check_dest(self.settings["dest"]), free_bytes=None), self.tools()

    def _tick(self):
        """Flush throttled job progress."""
        while not self.closed:
            time.sleep(TICK)
            with self.lock:
                for job in self.jobs:
                    if job["id"] in self.dirty:
                        self._publish("job", dict(job))
                self.dirty.clear()

    def _watch(self):
        """Destination and tools have no events, so poll them once a second, apart from progress: a check can hang."""
        last = self._machine()
        while not self.closed:
            time.sleep(1)
            now = self._machine()
            if now != last:
                self._snapshot_changed()
            last = now


DEST_PROBLEMS = {  # dest.problem -> (job error kind, plain words)
    "unplugged": ("dest_unplugged", "The drive for {} is not plugged in."),
    "missing": ("dest_missing", "The folder {} is not there."),
    "not_a_folder": ("dest_not_folder", "{} is a file, not a folder."),
    "not_writable": ("dest_denied", "Spool cannot write to {}."),
    "not_responding": (
        "dest_not_responding",
        "The drive for {} is not answering. It may be asleep, or the system may be asking for permission.",
    ),
}


def dest_problem(check, status=None):
    """Raise the plain-word problem for a destination check, if it has one."""
    if check["problem"]:
        kind, words = DEST_PROBLEMS[check["problem"]]
        raise Problem(status, kind, words.format(check["display"]), "check_dest")


def key(job):
    """Jobs with the same key would write the same file."""
    return job["url"], job["preset"], job["dest"], job["item"]


def parse(line):
    try:
        event = json.loads(line)
    except ValueError:
        return None  # engines may print Python warnings; only JSON lines are events
    return event if isinstance(event, dict) else None


def link(url):
    """A link must be http or https with a host; it reaches the engine as data only."""
    if isinstance(url, str) and len(url) > LINK_LIMIT:
        raise Problem(
            422, "invalid_link", f"That link is too long. Spool takes links of up to {LINK_LIMIT:,} characters."
        )
    parts = urlsplit(url) if isinstance(url, str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        raise Problem(422, "invalid_link", "That is not a link Spool can use. Links start with http:// or https://.")
    return url


def text(value, limit=1000):
    return str(value)[:limit] if value is not None else None


def new_job(entry, preset, engine, dest):
    thumb = entry.get("thumbnail")
    if not (isinstance(thumb, str) and thumb.startswith(("https://", "http://", "data:image/"))):
        thumb = None
    duration, item = entry.get("duration"), entry.get("item")
    return {
        "id": secrets.token_hex(4),
        "url": link(entry.get("url")),
        "item": item if type(item) is int and item > 0 else None,  # a playlist item of url, 1-based
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
    ("live", None, "This is live now. Download it after it ends.", ("this is live now.",)),
    (  # yt-dlp warns first; whatever fails after it on YouTube fails for this reason
        "engine",
        "install_js_runtime",
        "YouTube needs a JavaScript runtime such as deno, and none is installed. Install deno, then retry.",
        ("no supported javascript runtime",),
    ),
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
    (  # before "unavailable": the bot check says "sign in" too, and updating the engine is its fix
        "blocked",
        "update_engine",
        "The site refused the download. Updating the engine and retrying usually fixes this.",
        ("http error 403", "not a bot", "http error 429", "too many requests"),
    ),
    (
        "unavailable",
        None,
        "This video is not available. It may be private, removed, members-only, age-restricted or blocked where you are.",
        (
            "video unavailable",
            "is unavailable",
            "private video",
            "is not available",
            "has been removed",
            "members-only",
            "registered users",
            "logged-in",
            "log in",
            "sign in",
            "login required",
            "confirm your age",
            "age-restricted",
            "in your country",
            "geo restrict",
            "http error 404",
            "does not exist",
        ),
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
    # any other local file-system error: updating the engine would not help
    ("engine", None, "Spool could not save the file. Show the log for the details.", ("[errno ", "winerror")),
]


def classify(message):
    """An engine's message -> (kind, plain words, hint). The engine's own words stay in the job's log."""
    low = message.lower()
    for kind, hint, words, pieces in ERRORS:
        if any(p in low for p in pieces):
            return kind, words, hint
    line = last_error(message)
    return "engine", f"The engine failed: {line}" if line else "The engine stopped unexpectedly.", "update_engine"


def last_error(message):
    """The engine's last error line without its prefixes and command-line advice."""
    lines = [x.strip() for x in message.strip().splitlines() if x.strip()]
    line = next((x for x in reversed(lines) if x.startswith("ERROR:")), lines[-1] if lines else "")
    line = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]+:\s+)?", "", line)
    line = re.split(r";? please report this issue|\s+Use --|\s+See\s+https?://", line)[0].strip()
    return line if line.endswith(".") or not line else line + "."


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
