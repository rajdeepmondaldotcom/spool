"""The queue: jobs and their states, settings, the one state file, live events and plain-word errors."""

import glob
import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from . import system
from .engines import RELEASES, write_atomic

WORKER = Path(__file__).with_name("worker.py")
PRESETS = ("best", "2160", "1440", "1080", "720", "480", "360", "240", "audio", "mp3")
AT_ONCE = 2
try:
    VERSION = metadata.version("spool-dl")
except metadata.PackageNotFoundError:  # run from a source tree that was never installed
    VERSION = None
LINK_LIMIT = 8192  # signed CDN links run to a few thousand characters
WAITS = (10, 30, 60, 120, 300)  # seconds before each try again of a download cut short; then it fails
TICK = 0.25  # job progress goes out at most once a tick: four a second


class Problem(Exception):
    """An error the browser sees: an HTTP status and {kind, message, hint}."""

    def __init__(self, status, kind, message, hint=None):
        super().__init__(message)
        self.status, self.kind, self.message, self.hint = status, kind, message, hint


class Jobs:
    def __init__(self, data_dir, engines, tools=system.tools, setup=lambda: None):
        # tools: what the machine has; setup: fetch YouTube's helper again if the last try failed
        self.data, self.engines, self.tools, self.setup = Path(data_dir), engines, tools, setup
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
        # keys added after v0.1's first saves
        later = {"files": [], "item": None, "progress": None, "what": None, "started": None}
        later |= {"note": None, "attempt": None, "attempts": None}
        keys = set(new_job({"url": "https://x.test"}, "best", "yt-dlp", "/")) - set(later)
        self.jobs = [  # newest first; a malformed record is dropped, not fatal
            {**later, **j} for j in saved.get("jobs", []) if isinstance(j, dict) and keys <= set(j)
        ]
        self.lock = threading.RLock()
        self.procs = {}  # job id -> the worker running it
        self.dirty = set()
        self.subscribers = []
        self.closed = False
        self.waits = WAITS
        self.wake = threading.Condition(self.lock)  # a job waiting to try again wakes for a cancel or a quit
        self.newer = None  # a newer Spool's version, once a look at the releases finds one
        for job in self.jobs:
            if job["state"] in ("queued", "running"):
                job.update(state="failed", stage=None, note=None, eta=None, error=error("interrupted"))
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
            print(f"Spool couldn't read its saved list, so it started a new one. The old file is {aside}", flush=True)
            return {}

    # Reading

    def snapshot(self, dest=None, there=None):
        """Pass dest and there (from _there) when holding the lock: disk checks can hang, so never under it."""
        if dest is None or dest["path"] != self.settings["dest"]:  # the setting changed since dest was checked
            dest = self._check(self.settings["dest"], write=False)
        there = self._there() if there is None else there
        with self.lock:  # a job finished since there was taken keeps the file_exists it finished with
            jobs = [dict(j, file_exists=there.get(j["path"], j["file_exists"])) for j in self.jobs]
            return {
                "settings": dict(self.settings),
                "dest": dest_message(dest),
                "engines": self.engines.status(),
                "tools": {k: v for k, v in self.tools().items() if k != "js_path"},
                "version": VERSION,
                "update": self.newer and {"version": self.newer},
                "jobs": jobs,
            }

    def subscribe(self):
        """A queue of (event, data), starting with a snapshot. (None, None) means Spool is closing."""
        q, dest, there = queue.Queue(), self._check(self.settings["dest"], write=False), self._there()
        with self.lock:
            q.put(("snapshot", self.snapshot(dest, there)))
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # Probe and add

    def probe(self, url, engine=None):
        """Read a link with the engine in settings, then the other when the first can't read it or finds nothing."""
        url, first = link(url), self._engine(engine)
        self.setup()
        order = [first] if engine else [first, other(first)]
        for n, name in enumerate(order):
            try:
                return self._probe(url, name)
            except Problem as p:
                last = n == len(order) - 1 or not self.engines.path(order[n + 1])
                if p.kind not in ("unsupported", "empty") or last:
                    raise

    def _probe(self, url, engine):
        events = self._call({"mode": "probe", "url": url, "engine": engine})
        result = next((e for e in reversed(events) if e.get("event") in ("info", "error")), None)
        if result is None:
            kind = "engine"
        elif result["event"] == "error":
            kind = result.get("kind") if result.get("kind") in KINDS else classify(result["message"])
        else:
            kind = None
        if kind is None and not result["entries"]:
            kind = "empty"
        if kind:
            e = self._error(kind, engine, probe=True)
            raise Problem(422, kind, e["message"], e["hint"])
        return {
            "engine": engine,
            "kind": result["kind"],
            "title": result["title"],
            "entries": result["entries"],  # whole: each preset's ext rides along
            "truncated": result.get("truncated", False),  # a channel with more videos than it lists
        }

    def add(self, entries, preset, engine=None):
        """Add a job per entry. Returns (jobs, existing): an entry matching a job already in the list adds nothing."""
        engine = self._engine(engine)
        if preset not in PRESETS:
            raise Problem(422, "invalid_preset", "Spool doesn't know that quality.")
        if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
            raise Problem(422, "invalid_entries", "Choose at least one video to download.")
        dest = self.settings["dest"]
        dest_problem(self._check(dest), status=409)
        new = [new_job(e, preset, engine, dest) for e in entries]
        with self.lock:
            done = [(j, j["path"]) for j in self.jobs if j["state"] == "done" and j["path"]]
        there = {id(j) for j, path in done if os.path.exists(path)}  # never a file-system call under the lock
        with self.lock:
            added, existing = [], 0
            for job in new:
                live = (j for j in self.jobs if j["state"] in ("queued", "running") or id(j) in there)
                same = next((j for j in live if key(j) == key(job)), None)
                if same is None:
                    self.jobs.insert(0, job)
                    self._publish("job", dict(job))
                existing += same is not None
                added.append(dict(same or job))
            if existing < len(new):
                self._save()  # once for the lot: the list is rewritten whole
            self._pump()
        return added, existing

    # Job control

    def cancel(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job["state"] not in ("queued", "running"):
                raise Problem(409, "conflict", "Only a waiting or running download can be cancelled.")
            job.update(state="cancelled", stage=None, note=None, speed=None, eta=None, finished=time.time())
            self._changed(job)
            self.wake.notify_all()
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
            if any(j["state"] in ("queued", "running") and key(j) == key(job) for j in self.jobs):
                raise Problem(409, "conflict", "The same download is already going.")  # one partial file
            engine = self._engine(engine or job["engine"])
            self.setup()
            job.update(RUN, engine=engine)
            self._changed(job)
            copy = dict(job)
            self._pump()
        return copy

    def remove(self, job_id):
        """Forget a job and delete its partial leftovers. The finished file is never touched."""
        with self.lock:
            job = self._job(job_id)
            if job["state"] in ("queued", "running"):
                raise Problem(409, "conflict", "Cancel this download before removing it from the list.")
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

    def clear(self, ids):
        """Take the finished jobs among ids off the list; their files stay. Other ids are ignored.

        The page names the rows it showed as cleared, so a job that finished during its Undo stays."""
        if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            raise Problem(422, "invalid_ids", "Choose the finished downloads to clear.")
        with self.lock:
            gone = [j for j in self.jobs if j["state"] == "done" and j["id"] in ids]
            self.jobs = [j for j in self.jobs if j not in gone]
            for job in gone:
                self._publish("job_removed", {"id": job["id"]})
            self._save()
        for job in gone:
            (self.logs / f"{job['id']}.log").unlink(missing_ok=True)

    def reveal(self, job_id):
        with self.lock:
            path = self._job(job_id)["path"]
        if not path or not os.path.exists(path):
            raise problem(410, "gone")
        system.reveal(path)

    def open(self, job_id):
        with self.lock:
            job = self._job(job_id)
            state, path = job["state"], job["path"]
        if state != "done":
            raise Problem(409, "conflict", "Only a finished download can be opened.")
        if not path or not os.path.exists(path):
            raise problem(410, "gone")
        system.open_file(path)

    def reveal_dest(self):
        dest = self.settings["dest"]
        dest_problem(self._check(dest), status=409)
        system.open_folder(dest)

    def log(self, job_id):
        with self.lock:
            self._job(job_id)
        path = self.logs / f"{job_id}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    # Settings

    def update_settings(self, changes):
        """Save settings. A destination with a problem is still saved: the drive may come back."""
        changes = dict(changes)
        create = changes.pop("create", False)
        if changes.keys() - {"dest", "engine", "preset"}:
            raise Problem(422, "invalid_setting", "Spool has no setting by that name.")
        if not isinstance(create, bool):
            raise Problem(422, "invalid_setting", "Create is true or false.")
        if "dest" in changes:
            dest = changes["dest"]
            dest = os.path.expanduser(dest) if isinstance(dest, str) else ""
            if not os.path.isabs(dest):
                raise Problem(422, "invalid_dest", "Type the folder's full path.")
            changes["dest"] = os.path.normpath(dest)
            if create:
                make_folder(changes["dest"])
        if "engine" in changes and not (isinstance(changes["engine"], str) and changes["engine"] in RELEASES):
            raise Problem(422, "invalid_engine", "Spool doesn't know that engine.")
        if "preset" in changes and not (isinstance(changes["preset"], str) and changes["preset"] in PRESETS):
            raise Problem(422, "invalid_preset", "Spool doesn't know that quality.")
        with self.lock:
            self.settings.update(changes)
            self._save()
        self._snapshot_changed()
        return {
            "settings": dict(self.settings),
            "dest": dest_message(self._check(self.settings["dest"], write=False)),
        }

    def close(self):
        with self.lock:
            self.closed = True
            self.wake.notify_all()
            procs = [p for p in self.procs.values() if p]
            for q in self.subscribers:
                q.put((None, None))
        for proc in procs:
            system.kill(proc)

    # Inside

    def _job(self, job_id):
        job = next((j for j in self.jobs if j["id"] == job_id), None)
        if job is None:
            raise problem(404, "not_found")
        return job

    def _check(self, path, write=True, but=None):
        """system.check_dest, except that a folder a job of ours is writing to is never "not answering": on a slow
        drive the write test waits behind that job's own writes. but: a job that cannot vouch for itself."""
        check = system.check_dest(path, write=write)
        if check["problem"] == "not_responding":
            with self.lock:
                writing = any(
                    j is not but and j["state"] == "running" and j["stage"] != "starting" and j["dest"] == str(path)
                    for j in self.jobs
                )
            if writing:
                return dict(check, ok=True, problem=None)
        return check

    def _engine(self, engine):
        engine = engine or self.settings["engine"]
        if not (isinstance(engine, str) and engine in RELEASES):
            raise Problem(422, "invalid_engine", "Spool doesn't know that engine.")
        if not self.engines.path(engine):
            raise problem(503, "engine_not_ready")
        return engine

    def _error(self, kind, engine, dest=None, probe=False):
        """The error for a kind, with the folder's name and, for a refusal, whether an engine update may help."""
        e = error(kind, dest and system.display(dest), probe)
        status = self.engines.status()[engine]
        if kind == "blocked" and status["latest"] and status["latest"] != status["version"]:
            e["hint"] = "update_engine"
        return e

    def _spec(self, engine, **spec):
        tools = self.tools()
        return {
            "engine": engine,
            "engine_path": self.engines.path(engine),
            "ffmpeg": tools["ffmpeg"],
            "js_runtime": tools["js_runtime"],
            "js_path": tools.get("js_path"),
            **spec,
        }

    def _call(self, spec, timeout=120):
        try:
            out = subprocess.run(
                [sys.executable, "-I", str(WORKER)],
                input=json.dumps(self._spec(**spec)),
                capture_output=True,
                text=True,
                errors="replace",  # ffmpeg shares the pipe and may write any bytes
                timeout=timeout,
            ).stdout
        except subprocess.TimeoutExpired:
            raise problem(422, "network") from None
        return [e for e in map(parse, out.splitlines()) if e]

    def _pump(self):
        """Start queued jobs, oldest first, while fewer than AT_ONCE slots are taken.

        A slot is an entry in procs: taken here, freed when the job's thread ends, so a cancelled worker that is
        still dying keeps its slot."""
        for job in reversed(self.jobs):
            if len(self.procs) >= AT_ONCE or self.closed:
                return
            if job["state"] == "queued" and job["id"] not in self.procs:
                job.update(state="running", stage="starting", started=time.time())
                self._changed(job)
                self.procs[job["id"]] = None  # no worker yet
                threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job):
        """Run a job in the slot _pump reserved for it; the slot is freed only when its worker is gone."""
        try:
            failure, switched, waited, furthest = None, False, 0, None
            while True:
                dest_problem(self._check(job["dest"], but=job))
                result = self._attempt(job)
                if result is None or result["event"] == "done":
                    break
                kind = result.get("kind") if result.get("kind") in KINDS else classify(result["message"])
                try:
                    dest_problem(self._check(job["dest"], but=job))  # a drive unplugged mid-download says so
                except Problem as p:
                    kind = p.kind
                failure = self._error(kind, job["engine"], job["dest"])
                if kind in ("unsupported", "empty") and not switched and self.engines.path(other(job["engine"])):
                    with self.lock:  # the other engine may read what this one can't
                        job["engine"], switched = other(job["engine"]), True
                    continue
                reached = (job["part"], job["downloaded"] or 0)
                if furthest is not None and reached > furthest:
                    waited = 0  # this try got further: a line that drops now and then still finishes
                furthest = max(furthest or reached, reached)
                if kind not in NOTES or waited == len(self.waits) or not self._wait(job, kind, waited):
                    break
                waited += 1
        except Problem as p:
            result, failure = {"event": "error"}, {"kind": p.kind, "message": p.message, "hint": p.hint}
        except Exception:  # a spawn or write that fails must not leave the job "starting" and its slot taken
            result, failure = {"event": "error"}, error("engine")
            if self.procs.get(job["id"]):
                system.kill(self.procs[job["id"]])
            try:
                with open(self.logs / f"{job['id']}.log", "a", encoding="utf-8") as log:
                    log.write(traceback.format_exc())
            except OSError:
                pass
        with self.lock:
            self.procs.pop(job["id"], None)
            if self.closed:
                return
            if job["state"] == "running" and result is not None:  # else cancelled, and perhaps retried since
                if result["event"] == "done":
                    job.update(state="done", path=result["path"], size=result["size"], file_exists=True, progress=1.0)
                    for j in self.jobs:  # a finished file is nobody's leftover, whichever job wrote under its name
                        j["files"] = [f for f in j["files"] if result["path"] not in (f, mp3_of(j, f))]
                else:
                    job.update(state="failed", error=failure)
                job.update(stage=None, note=None, speed=None, eta=None, finished=time.time())
                self._changed(job)
            self._pump()

    def _wait(self, job, kind, waited):
        """Hold the job's slot until its next try. False when it was cancelled or Spool quit meanwhile."""
        end = time.monotonic() + self.waits[waited]
        with self.lock:
            if self.closed or job["state"] != "running":
                return False
            note = NOTES[kind]
            job.update(stage="waiting", note=note, attempt=waited + 2, attempts=len(self.waits) + 1, speed=None)
            job["eta"] = math.ceil(self.waits[waited])
            self._changed(job)
            while (left := end - time.monotonic()) > 0:
                self.wake.wait(min(1.0, left))
                if self.closed or job["state"] != "running":
                    return False
                job["eta"] = max(0, math.ceil(end - time.monotonic()))
                self.dirty.add(job["id"])
            job.update(stage="starting", note=None, eta=None)
            self._changed(job)
        return True

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
                    errors="replace",  # ffmpeg shares the pipe and may write any bytes
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
                        self._save()  # Remove needs these after a quit too
                else:
                    result = event
                    break  # a child the engine left behind may hold the pipe open; this was the last word
            system.kill_leftovers(proc)  # it waits for the worker itself, and reaps it last
        return result

    def _progress(self, job, event):
        with self.lock:
            if job["state"] != "running":
                return
            if event.get("progress") is not None:  # never backwards, whatever the engine reports
                job["progress"] = max(job["progress"] or 0.0, min(1.0, event["progress"]))
            job["what"] = event.get("what")
            if event["status"] in ("downloading", "processing"):  # a part, or a join, tags or MP3 step on a file
                job.update(
                    stage=event["status"],
                    part=event["part"],
                    downloaded=event["downloaded"],
                    total=event["total"],
                    speed=event["speed"],
                    eta=event["eta"],
                )
            elif event["status"] == "finished":  # a part is done; what comes next is not known yet
                job.update(stage="processing", what=None, downloaded=event["total"], speed=None, eta=None)
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

    def _there(self):
        """Whether each finished job's file is still on disk, checked outside the lock."""
        with self.lock:
            paths = [j["path"] for j in self.jobs if j["path"]]
        return {p: os.path.exists(p) for p in paths}

    def _snapshot_changed(self):
        if not self.subscribers:  # no page open: nobody to tell, and a subscriber starts with a snapshot
            return
        dest, there = self._check(self.settings["dest"], write=False), self._there()
        with self.lock:
            if not self.closed:
                self._publish("snapshot", self.snapshot(dest, there))

    def _machine(self):
        return dict(self._check(self.settings["dest"], write=False), free_bytes=None), self.tools()

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
        last = None  # checked first when a page opens: a write test is not free
        while not self.closed:
            time.sleep(1)
            if self.closed or not self.subscribers:
                continue
            now = self._machine()
            if now != last:
                self._snapshot_changed()
            last = now


MESSAGES = {  # kind -> (the sentence people read, hint). Every sentence the server sends; none names a program.
    "invalid_link": ("That doesn't look like a link. Paste a video's web address.", None),
    "too_long": ("That link is too long for Spool.", None),
    "unsupported": ("Spool can't read this link.", None),
    "empty": ("Spool found no video at this link.", None),
    "unavailable": ("This video is private, removed, or needs a sign-in.", None),
    "live": ("This is live now. Download it after it ends.", None),
    "playlist": ("This link is a list of videos. Paste it in the link field to choose from it.", None),
    "blocked": ("The site refused the download.", "retry"),  # update_engine when a newer engine exists
    "network": ("Spool couldn't reach the site. Check your internet connection.", "retry"),
    "no_ffmpeg": ("This quality needs a free helper to join video and audio.", "install_ffmpeg"),
    "no_js": ("Spool couldn't set up what it needs for YouTube. Check your internet connection.", "retry"),
    "interrupted": ("Spool was closed before this download finished.", "resume"),
    "dest_unplugged": ("{name} isn't connected. Plug it in, or choose another folder.", "check_dest"),
    "dest_missing": ("{name} doesn't exist. Choose another folder.", "check_dest"),
    "dest_not_folder": ("{name} is a file, not a folder. Choose another folder.", "check_dest"),
    "dest_denied": ("Spool isn't allowed to save to {name}. Choose another folder.", "check_dest"),
    "dest_full": ("{name} is full. Free some space, or choose another folder.", "check_dest"),
    "dest_not_responding": (
        "{name} isn't answering. If macOS is asking whether Spool may use it, choose Allow.",
        "check_dest",
    ),
    "engine": ("The download failed.", "show_log"),
    "engine_probe": ("Spool hit an error it doesn't recognise. Try again.", "retry"),
    "engine_not_ready": ("Spool is still setting up. Try again in a few seconds.", None),
    "not_found": ("Spool can't find that download.", None),
    "gone": ("Spool can't find the file. It was moved or deleted.", None),
}
NOTES = {  # kind -> what a job waiting to try again says happened. Only these kinds are tried again.
    "network": "The connection dropped.",
    "blocked": "The site refused the download.",
}
KINDS = ("live", "no_js", "playlist")  # the worker names these itself; its message is then not read
DEST_KINDS = {
    "unplugged": "dest_unplugged",
    "missing": "dest_missing",
    "not_a_folder": "dest_not_folder",
    "not_writable": "dest_denied",
    "not_responding": "dest_not_responding",
}


def error(kind, name=None, probe=False):
    """{kind, message, hint} for a kind. name is the folder's short label, for the destination kinds."""
    words, hint = MESSAGES["engine_probe" if probe and kind == "engine" else kind]
    return {"kind": kind, "message": words.format(name=name), "hint": hint}


def problem(status, kind, name=None, probe=False):
    e = error(kind, name, probe)
    return Problem(status, kind, e["message"], e["hint"])


def dest_message(check):
    """The snapshot's dest, with the sentence for its problem."""
    kind = DEST_KINDS.get(check["problem"])
    return dict(check, message=kind and error(kind, check["display"])["message"])


def dest_problem(check, status=None):
    """Raise the problem for a destination check, if it has one."""
    if check["problem"]:
        raise problem(status, DEST_KINDS[check["problem"]], check["display"])


def other(engine):
    return next(e for e in RELEASES if e != engine)


def make_folder(path):
    """Create path when only its last name is missing and its parent takes writes. Otherwise leave it to the check."""
    parent = os.path.dirname(path)
    if not os.path.exists(path) and os.path.isdir(parent) and os.access(parent, os.W_OK):
        try:
            os.mkdir(path)
        except OSError:
            pass  # the destination check says what is wrong


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
        raise problem(422, "too_long")
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
    except ValueError:  # a broken [IPv6] host
        parts = None
    if (
        not parts
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc  # a user name lets a link show one host and fetch another
        or any(c.isspace() for c in url)
    ):
        raise problem(422, "invalid_link")
    return url


def text(value, limit=1000):
    return str(value)[:limit] if value is not None else None


RUN = {  # a job's fields for one run, as it is before the run starts; Retry sets them back
    "state": "queued",
    "started": None,  # when this run began; Remove tells this run's half-written file from an earlier run's
    "stage": None,
    "part": 0,
    "progress": None,  # 0 to 1 across all parts, never decreasing
    "what": None,  # downloading: "video" or "audio", what the part carries; processing: "join", "tags", "mp3" or None
    "downloaded": None,
    "total": None,
    "speed": None,
    "eta": None,
    "path": None,
    "size": None,
    "file_exists": False,
    "error": None,
    "finished": None,
    "note": None,  # waiting: what happened, in NOTES's words
    "attempt": None,  # waiting: the number of the try that comes next, out of attempts
    "attempts": None,
}


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
        **RUN,
        "files": [],  # every name the engine wrote, temporary ones included: what Remove cleans up
        "created": time.time(),
    }


OS_ERROR = re.compile(r"(\[errno \d+\] [^:]*):.*")  # "[Errno 13] Permission denied: '<file name>'"
ERRORS = [  # (kind, lowercase pieces of engine messages); the first match wins. The words people read are MESSAGES.
    ("live", ("this is live now.",)),
    ("no_js", ("no supported javascript runtime",)),  # yt-dlp warns first; what fails after it fails for this
    ("unsupported", ("unsupported url",)),
    ("empty", ("found no video at this link",)),
    ("no_ffmpeg", ("ffmpeg not found", "ffprobe and ffmpeg not found", "ffmpeg is not installed", "ffprobe/avprobe")),
    ("engine", ("requested format is not available",)),  # the video is there; the log says what was asked
    ("blocked", ("http error 403", "not a bot", "http error 429", "too many requests")),  # the bot check says sign in
    (
        "unavailable",
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
    ("dest_missing", ("destination folder is missing",)),
    ("dest_full", ("no space left", "errno 28")),
    ("dest_denied", ("permission denied", "read-only file system", "errno 13", "errno 30")),
    (
        "network",
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
]


def classify(message):
    """An engine's message -> an error kind. The engine's own words stay in the job's log.

    File names carry the video's title, so an OS error's file names are not read."""
    low = OS_ERROR.sub(r"\1", message.lower())
    return next((kind for kind, pieces in ERRORS if any(p in low for p in pieces)), "engine")


def mp3_of(job, f):
    """The .mp3 ffmpeg makes from a reported name f, for an MP3 job."""
    return os.path.splitext(f.removesuffix(".part"))[0] + ".mp3" if job["preset"] == "mp3" else None


PIECE = re.compile(r"\.part-Frag\d+(\.part)?")  # one of several pieces of a stream fetched at once


def remove_leftovers(job, keep):
    """Delete what the engine wrote for this job, except any finished file and what a running job still uses."""
    names = set()
    for f in job["files"]:
        names |= {f, f + ".part", f + ".ytdl"}
        if f not in keep:  # a running twin's pieces are its own
            names |= {p for p in glob.glob(glob.escape(f) + ".part-Frag*") if PIECE.fullmatch(p[len(f) :])}
        mp3 = mp3_of(job, f)
        if mp3 and job["state"] != "done" and job["started"]:  # ffmpeg's half-written .mp3 looks finished
            if os.path.exists(mp3) and os.path.getmtime(mp3) >= job["started"]:  # never an earlier run's mp3
                names.add(mp3)
    for name in names - keep:
        try:  # the worker reports names under the folder's real path, which the typed one may not spell
            if os.path.isfile(name) and os.path.samefile(os.path.dirname(name), job["dest"]):
                os.unlink(name)
        except OSError:
            pass
