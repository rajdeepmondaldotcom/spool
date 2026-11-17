"""Worker: a child process that runs one engine call and reports JSON lines on stdout.

Spec on stdin, one line: {engine, engine_path, mode: "probe" | "download", url, ffmpeg, js_runtime?, dest?, preset?,
item?, hold?}. A download first tells the video's id, before it writes any file; with hold it then reads one more line
and goes on only at "go".
Events: {"event": "log" | "progress" | "meta" | "id" | "file" | "info" | "done" | "error", ...}; an error carries "kind"
("live", "no_js" or "playlist") when the worker knows it. A progress event's status is the engine's ("downloading",
"finished"), or "processing" for how far a join, tags or MP3 step has written its file. An info event of a list
carries "truncated": a channel has more videos than it lists. Each preset carries "ext", the type of file it saves.
Standard library only; run with python -I.
"""

import json
import os
import re
import sys
import threading
import time
from importlib import import_module

OUT = sys.stdout
OUT_LOCK = threading.Lock()  # a step's progress is reported from a thread of its own
LIVE = "This is live now."  # jobs.py maps these words to the error kind "live"
NO_JS_FAILS = ("requested format is not available", "only images are available")  # what a missing runtime causes


class Failure(Exception):
    def __init__(self, message, kind=None):
        super().__init__(message)
        self.kind = kind


def emit(**event):
    with OUT_LOCK:
        OUT.write(json.dumps(event, default=str) + "\n")
        OUT.flush()


class Log:
    no_js = False  # yt-dlp warned that YouTube has no JavaScript runtime to solve its challenge

    def debug(self, msg):
        Log.no_js = Log.no_js or "No supported JavaScript runtime" in msg
        emit(event="log", line=msg)

    info = warning = error = debug


AUDIO = {"audio": "bestaudio[ext=m4a]/bestaudio/best", "mp3": "bestaudio/best"}
LONG = {2160: 3840, 1440: 2560, 1080: 1920, 720: 1280, 480: 854, 360: 640, 240: 426}  # the 16:9 long side


def format_params(preset, ffmpeg, module):
    """The engine params that choose a preset's streams."""
    if preset in AUDIO:
        return {"format": AUDIO[preset]}
    if module == "yt_dlp":  # its sorter's res is the short side; resolution first, H.264 + AAC only break ties
        res = "res" if preset == "best" else f"res:{preset}"
        return {"format": "bv*+ba/b" if ffmpeg else "b", "format_sort": [res, "fps", "vcodec:h264", "acodec:aac"]}
    return {"format": ladder(preset, ffmpeg)}


def ladder(preset, ffmpeg):
    """youtube-dl's selector: it has no format sorting, so fallbacks stand in for it.

    shortcut: youtube-dl cannot rank by the short side or break ties by codec, so a 4:3 or 3:4 video's class N can
    give a format one class up, and H.264 is preferred only at exactly N; Best takes the engine's own top format.
    Upgrade if youtube-dl gains format sorting, or drop the ladder if yt-dlp becomes the only engine."""
    if preset == "best":
        return "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best" if ffmpeg else "best"
    n = int(preset)
    fit, tall = f"[height<={LONG[n]}][width<={LONG[n]}]", f"[height<={n}]"
    if not ffmpeg:
        return f"best{fit}/best{tall}/best"
    return (  # H.264 only when the class itself has it, landscape or portrait; then the best that fits
        f"bestvideo[height={n}][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[width={n}][vcodec^=avc1]+bestaudio[ext=m4a]"
        f"/bestvideo{fit}+bestaudio/bestvideo{tall}+bestaudio/best{fit}/best{tall}/best"
    )


# A file name holds 255 bytes (macOS) or 255 characters (exFAT), and the engines add " [id]" and ".f137.mp4.part".
NAME = {  # yt-dlp's spool_* fields are set by the Name step in download(); youtube-dl has no byte precision: 50 chars
    "yt_dlp": "%(spool_title,title).150B [%(id)s]%(spool_class|)s",  # <= 200 bytes
    "youtube_dl": "%(title).50s [%(id)s]",
}
HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240)
FLAT_PRESETS = ("best", "1080", "720", "480", "audio")  # a flat playlist listing has no formats to read
LABELS = {"best": "Best", "audio": "Audio", "mp3": "MP3"}
CLASS = {str(h): f" {h}p" for h in HEIGHTS}  # two classes of one video must not share a file name


MP3_RATE = 128_000 // 8  # bytes a second: the engines' default MP3 quality averages about 128 kbit/s


def preset(pid, size=None, detail=None):
    return {"id": pid, "label": LABELS.get(pid, f"{pid}p"), "detail": detail, "size": size, "ext": None}


def file_type(streams, compatible):
    """The type of file a preset's streams save: one stream keeps its own; two are joined into the container the
    engine picks for them (yt-dlp's get_compatible_ext, as its join calls it). None when unknown."""
    if None in streams or (len(streams) > 1 and not compatible):
        return None  # shortcut: youtube-dl has no such rule to ask; upgrade if it matters there
    if len(streams) == 1:
        return streams[0].get("ext")
    video, audio = streams
    return compatible(
        vcodecs=[video.get("vcodec")], acodecs=[audio.get("acodec")], vexts=[video["ext"]], aexts=[audio["ext"]]
    )


def short(f):
    """A format's class: its short side, or its height when the site gives no width."""
    return min(f["width"], f["height"]) if f.get("width") and f.get("height") else (f.get("height") or 0)


def klass(f):
    """The smallest standard class that holds a format's short side: 1920x800 is class 1080, as sites label it."""
    side = short(f)
    return next((h for h in reversed(HEIGHTS) if side and side <= h), None)


def has(f, codec):
    return f.get(codec) != "none"  # no codec info means the stream carries it


def bytes_of(f, duration):
    """A stream's bytes; from the bitrate when the site gives no size, as yt-dlp estimates it."""
    return f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * (duration or 0)


def size(duration, *formats):
    sizes = [f and bytes_of(f, duration) for f in formats]
    return int(sum(sizes)) if all(sizes) else None


NEWER_CODECS = (("hev", "hvc", "h265"), ("vp09.02", "vp9.2"), ("vp9", "vp09"), ("av01", "av1"))


def codec_rank(f):
    """yt-dlp's vcodec:h264: H.264 first, then older codecs, then newer ones, the nearest first (H.265, VP9,
    VP9 HDR, AV1)."""
    codec = str(f.get("vcodec"))
    if codec.startswith(("avc", "h264")):
        return 0
    newer = next((i for i, names in enumerate(NEWER_CODECS) if codec.startswith(names)), None)
    return -1 if newer is None else -(2, 4, 3, 5)[newer]


def ranking(n):
    """yt-dlp's order for format_sort [res:N, fps, vcodec:h264, acodec:aac]: the site's own preference first,
    then the largest short side up to N (above N the smallest), frame rate, H.264, AAC, and then the engine's own
    defaults: the original language, the site's quality mark, HDR, a known size (none loses), bitrate."""

    def known(f, field):
        return -1 if f.get(field) is None else f[field]

    def key(f):
        side = short(f)
        return (
            known(f, "preference"),
            side if n is None or side <= n else -side,
            f.get("fps") or 0,
            codec_rank(f),
            str(f.get("acodec")).startswith(("mp4a", "aac")),
            known(f, "language_preference"),
            known(f, "quality"),
            f.get("dynamic_range") not in (None, "SDR"),
            f.get("filesize") or f.get("filesize_approx") or -1,
            f.get("tbr") or 0,
        )

    return key


def picked(formats, preset, ffmpeg):
    """The streams a preset downloads, by the rule yt-dlp uses; (None,) if none fit."""
    if preset in AUDIO:
        audio = [f for f in formats if has(f, "acodec") and not has(f, "vcodec")]
        both = [f for f in formats if has(f, "acodec") and has(f, "vcodec")]
        m4a = [f for f in audio if f.get("ext") == "m4a"]
        return (max(m4a or audio or both, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None),)
    return chosen(formats, None if preset == "best" else int(preset), ffmpeg)


def estimate(formats, preset, ffmpeg, duration):
    """Estimated bytes for a preset, choosing streams by the rule yt-dlp uses. None when unknown."""
    if preset == "mp3":
        return int(duration * MP3_RATE) if duration else None
    return size(duration, *picked(formats, preset, ffmpeg))


def chosen(formats, n, ffmpeg):
    """The streams yt-dlp's rule picks for class n (None for Best): bv*+ba with ffmpeg, else b. (None,) if none fit."""
    key = ranking(n)
    audio = [f for f in formats if has(f, "acodec") and not has(f, "vcodec")]
    video = [f for f in formats if has(f, "vcodec")]
    if ffmpeg and video and audio:
        top = max(video, key=key)
        return (top,) if has(top, "acodec") else (top, max(audio, key=key))  # the engine takes no second sound
    both = [f for f in formats if has(f, "acodec") and has(f, "vcodec")]
    whole = all(has(f, "vcodec") for f in formats) or all(has(f, "acodec") for f in formats)  # b's own fallback
    return (max(both or (formats if whole else []), key=key, default=None),)


def entry(info, ffmpeg, compatible=None):
    if info.get("_type") in ("url", "url_transparent"):  # without ffmpeg a class is whatever one stream a site has
        presets = [preset(p) for p in (FLAT_PRESETS if ffmpeg else ("best", "audio"))]
    else:
        formats = info.get("formats") or [info]  # a site with one stream puts it on the info itself
        playable = formats if ffmpeg else [f for f in formats if has(f, "acodec")]  # no merging without ffmpeg
        classes = {klass(f) for f in playable if has(f, "vcodec")}
        top = chosen(formats, None, ffmpeg)[0]
        best = klass(top) if top and has(top, "vcodec") else None  # Best's own class: offered once, as its detail
        heights = [str(h) for h in HEIGHTS if h in classes and h != best]
        ids = ["best"] * bool(top) + heights + ["audio"] + ["mp3"] * bool(ffmpeg)  # no Best when nothing fits
        presets = [preset(p, estimate(formats, p, ffmpeg, info.get("duration"))) for p in ids]
        for p in presets:  # the MP3 step decides its type, whatever stream it starts from
            p["ext"] = "mp3" if p["id"] == "mp3" else file_type(picked(formats, p["id"], ffmpeg), compatible)
        if top:
            presets[0]["detail"] = best and f"{best}p"
    if ffmpeg and presets[-1]["id"] != "mp3":
        presets.append(preset("mp3"))
    thumbnails = info.get("thumbnails") or [{}]
    url = info.get("webpage_url") or info.get("url")
    if info.get("ie_key") == "Youtube" and not str(url).startswith(("http://", "https://")):
        url = f"https://www.youtube.com/watch?v={info.get('id') or url}"  # youtube-dl lists bare ids
    return {
        "url": url,
        "item": None,
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or thumbnails[-1].get("url"),
        "live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
        "presets": presets,
    }


def where(d, n, spec, plan=()):
    """(progress across all parts, 0 to 1 or None; what the current part carries) for one progress hook call.

    plan: the parts yt-dlp chose, as its Name step saw them; each part's hook names only that part."""
    info = d.get("info_dict")
    if info:
        parts = info.get("requested_formats") or plan or [info]
        i = next((k for k, f in enumerate(parts) if f.get("format_id") == info.get("format_id")), max(n, 1) - 1)
        i = min(i, len(parts) - 1)
        what = "video" if has(parts[i], "vcodec") else "audio" if has(parts[i], "acodec") else None
    else:  # shortcut: youtube-dl's hooks carry no info, so the preset's usual parts stand in; fine until a surprise
        parts = [{}] * (1 if spec["preset"] in AUDIO or not spec["ffmpeg"] else 2)
        i = min(max(n, 1), len(parts)) - 1
        what = "audio" if spec["preset"] in AUDIO or i == 1 else "video"
    done, total = d.get("downloaded_bytes"), total_of(d)
    frac = 1.0 if d.get("status") == "finished" else min(1.0, done / total) if done is not None and total else None
    sizes = [bytes_of(f, (info or {}).get("duration")) for f in parts]
    if frac is None:
        return (i / len(parts) or None), what
    if all(sizes):
        return (sum(sizes[:i]) + frac * sizes[i]) / sum(sizes), what
    return (i + frac) / len(parts), what


def total_of(d):
    """A progress hook's total bytes. In pieces the engine gives none until the end and its guess swings, so the
    stream's own size comes first."""
    return d.get("total_bytes") or (d.get("info_dict") or {}).get("filesize") or d.get("total_bytes_estimate")


# shortcut: YouTube streams come whole, one connection, as in 0.1.2. In pieces (AT_ONCE > 1, 4 measured best on a
# home line) a 239 MB film came at 9.7 MB/s against 7.9 whole in one run; but one run stalled six minutes on one piece
# and one got a 403 on the data, while one connection fetched 35 GB and 3.4 GB without a failure. Raise it only after
# many clean real-site runs in pieces, with no stall, no refusal, and a measured gain. Tests set it where they need it.
AT_ONCE = 1
THROTTLED = 20_000  # bytes a second: a piece slower for 3 seconds makes the engine read the video again and go on
# with fresh links. Pieces join in order, so one throttled piece (seen: 1 KB/s for minutes) holds up the rest. Only on
# the first read: the engine's re-reads have no limit, and on a slow line every piece is below it
PIECE = 10 << 20  # bytes a piece: the size yt-dlp's own pieces mode (formats=dashy) asks YouTube for
PIECE_TRIES = 2  # tries again for a piece, after 1 s then 2 s; then the stream comes whole, and a site that limits
# is not sent four connections' worth of quick tries
NOT_PIECES = ("http error 429", "too many requests", "not a bot", "no space left", "errno 28")  # pieces would not help


class Wrong(Exception):
    """A stream fetched in pieces is not the size the site gave."""


def in_pieces(f):
    """Fetch a YouTube stream as byte ranges, several at once, as the engine's DASH downloader does. Each piece asks
    for its full width: a stream longer than the site said then comes out too big, and Wrong catches it."""
    url = f["url"] + ("&" if "?" in f["url"] else "?")
    f["fragments"] = [{"url": f"{url}range={a}-{a + PIECE - 1}"} for a in range(0, f["filesize"], PIECE)]
    f["protocol"] = "http_dash_segments"
    f.pop("downloader_options", None)  # its whole-file chunking


def dropped(name):
    """Remove a stream fetched in pieces from the destination: its file, .part, .ytdl and pieces, so it comes whole."""
    folder, base = os.path.split(name)
    for n in os.listdir(folder or "."):
        if n in (base, base + ".part", base + ".ytdl") or n.startswith(base + ".part-Frag"):
            os.remove(os.path.join(folder, n))


STEPS = {"Merger": "join", "Metadata": "tags", "ExtractAudio": "mp3"}  # yt-dlp's steps that write a whole file
EVERY = 1.0  # seconds between a step's reports


def output(what, info):
    """(the file a step writes, its expected bytes or None), from the info its started hook carries."""
    path = info["filepath"]
    base, ext = os.path.splitext(path)
    if what == "mp3":
        return base + ".mp3", info.get("duration") and int(info["duration"] * MP3_RATE)  # its usual size
    inputs = [f["filepath"] for f in info["requested_formats"]] if what == "join" else [path]
    try:
        total = sum(os.path.getsize(f) for f in inputs)
    except OSError:
        total = None
    return base + ".temp" + ext, total


def watch(what, out, total, part, stop):
    """Report how much of out is written, now and then every EVERY seconds until stop is set."""
    then, before = None, 0
    while True:
        try:
            done = max(before, os.path.getsize(out))  # never backwards: the file is renamed when the step ends
        except OSError:
            done = before
        now = time.monotonic()
        speed = (done - before) / (now - then) if then is not None and now > then else None
        eta = int((total - done) / speed) if speed and total and total > done else None
        emit(
            event="progress",
            status="processing",
            part=part,
            progress=None,  # the job's progress is the download's; a step's own is downloaded / total
            what=what,
            downloaded=done,
            total=total,
            speed=speed,
            eta=eta,
        )
        then, before = now, done
        if stop.wait(EVERY):
            return


CHANNEL = re.compile(  # a YouTube channel's home, or one of its tabs of videos
    r"(?P<home>https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|(?:channel|c|user)/[^/?#]+))"
    r"(?:/(?P<tab>videos|shorts|streams))?/?(?:[?#].*)?"
)
LISTED = 100  # a channel lists its newest videos, at most this many


def probe(engine, module, params, spec):
    url, channel = spec["url"], CHANNEL.fullmatch(spec["url"])
    if channel:  # its home lists its tabs; its uploads are the Videos tab
        url = f"{channel['home']}/{channel['tab'] or 'videos'}"
        params["playlistend"] = LISTED + 1  # one more tells that there are more; the engine reads no further
    params.update(extract_flat="in_playlist", skip_download=True)
    with engine.YoutubeDL(params) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        listed = list(info.get("entries") or [])
        truncated = bool(channel) and len(listed) > LISTED
        found = [(n, entry(e, spec["ffmpeg"])) for n, e in enumerate(listed[:LISTED] if channel else listed, 1) if e]
        urls = [e["url"] for _, e in found]
        for n, e in found:  # no link of its own: download it as item n of the probed link
            if (
                not str(e["url"]).startswith(("http://", "https://"))
                or urls.count(e["url"]) > 1
                or e["url"] == spec["url"]
            ):
                e.update(url=spec["url"], item=n)
        title = (info.get("channel") or info.get("uploader")) if channel else None
        emit(
            event="info",
            kind="playlist",
            title=title or info.get("title"),
            entries=[e for _, e in found],
            truncated=truncated,
        )
    else:
        compatible = getattr(import_module(module + ".utils"), "get_compatible_ext", None)
        emit(event="info", kind="video", title=info.get("title"), entries=[entry(info, spec["ffmpeg"], compatible)])


def download(engine, module, params, spec):
    final = {}
    PostProcessor = import_module(module + ".postprocessor.common").PostProcessor

    def details(info):
        e = entry(info, spec["ffmpeg"])
        emit(event="meta", **{k: e[k] for k in ("title", "uploader", "thumbnail", "duration")})

    def here(name):  # the engine writes into dest, its current folder; getcwd() would resolve links and case
        return os.path.join(spec["dest"], os.path.basename(name))

    class Capture(PostProcessor):
        def run(self, information):
            path = information.get("filepath")
            final["path"] = path and here(path)
            details(information)  # youtube-dl's hooks carry no info, so this is where it tells us
            return [], information

    told = []

    def tell(vid):
        """The video's id, once, before any file is written; held, go on only at the queue's go."""
        if told:
            return
        told.append(vid)
        emit(event="id", id=vid)
        if spec.get("hold") and sys.stdin.readline().strip() != "go":
            os._exit(0)  # the queue said stop: nothing is written, nothing more to say

    plan = []
    seen = set()
    pieces = [module == "yt_dlp"]  # off once pieces failed: then the job goes on the old way, one connection
    used = {}  # this run's streams in pieces: format_id -> the file it is fetched to
    finished = set()  # this run's streams finished, by format_id
    reads = [0]  # times this job read the video in pieces mode

    def wrote(name):
        if name not in seen:
            seen.add(name)
            emit(event="file", path=here(name))

    class Name(PostProcessor):
        """yt-dlp's step between choosing the streams and naming the file."""

        def run(self, information):
            tell(information.get("id"))
            plan[:] = information.get("requested_formats") or [information]
            title = ydl.evaluate_outtmpl("%(title)s", information, sanitize=True)  # cleaned first: "|" takes 3 bytes
            video = next((f for f in plan if has(f, "vcodec")), None)
            got = video and klass(video)  # a class preset gives the best below it: name the file for what it got
            information["spool_title"] = title.encode()[:150].decode(errors="ignore")
            information["spool_class"] = (
                f" {got}p" if got and spec["preset"] in CLASS else CLASS.get(spec["preset"], "")
            )
            base, ext = os.path.splitext(ydl.prepare_filename(information))
            for e in {ext, ".mp3" if spec["preset"] == "mp3" else ext}:  # the merge and the metadata step write here
                wrote(base + ".temp" + e)
            split = information.get("requested_formats")  # the engine's names for each stream's file
            fast = AT_ONCE > 1 and pieces[0] and information.get("extractor_key") == "Youtube"
            reads[0] += fast
            used.clear()
            finished.clear()
            for f in plan:
                name = f"{base}.f{f['format_id']}.{f['ext']}" if split else base + ext
                fresh = not os.path.exists(name + ".part") or os.path.exists(name + ".ytdl")
                if fast and f.get("protocol") == "https" and f.get("filesize") and fresh:
                    in_pieces(f)  # a .part the old way left goes on the old way: in pieces it is appended from 0
                    used[f["format_id"]] = here(name)
                elif f.get("protocol") == "https" and os.path.exists(name + ".ytdl"):
                    os.remove(name + ".ytdl")  # once the old way adds to a .part, pieces must never resume it
            ydl.params.update(  # the old way's values otherwise, as in 0.1.2
                concurrent_fragment_downloads=AT_ONCE if used else 1,
                skip_unavailable_fragments=not used,  # a piece skipped is a hole in the file
                # a stream fetched whole has no floor, as in 0.1.2: it applies to every download of the run
                throttledratelimit=THROTTLED if reads[0] == 1 and len(used) == len(plan) else None,
                fragment_retries=PIECE_TRIES if used else 10,
                retry_sleep_functions={"fragment": lambda n: 2**n} if used else {},
            )
            return [], information

    joined = []  # the join takes chapters and details from the audio part: no second pass over the joined file
    plain = []  # that join failed: the second try joins the plain way and Details writes the tags after
    merging = []  # the join started and has not finished

    def tag_audio(d):
        """Tag the small audio part and have the join copy its tags, instead of rewriting the joined file after.

        Any failure leaves today's two passes: the Details step then writes them into the joined file."""
        info = d["info_dict"]
        i = next((k for k, f in enumerate(plan) if f.get("format_id") == info.get("format_id")), None)
        if plain or i is None or len(plan) < 2 or has(info, "vcodec") or not has(info, "acodec"):
            return
        try:
            wrote("{}.temp{}".format(*os.path.splitext(d["filename"])))
            tags(dict(info, filepath=d["filename"]))
        except Exception as exc:  # the engine's error, or a change in its insides
            emit(event="log", line=f"[spool] details go in after the join: {exc!r}")
            return
        args = ydl.params.setdefault("postprocessor_args", {})
        args["merger+ffmpeg_o"] = ["-map_metadata", str(i), "-map_chapters", str(i)]
        joined.append(i)

    def tags(info):
        Metadata = import_module("yt_dlp.postprocessor.ffmpeg").FFmpegMetadataPP
        return Metadata(ydl, add_metadata=True, add_chapters=True).run(info)

    class Details(PostProcessor):
        """yt-dlp's chapters-and-details step, unless the join already carried them."""

        def run(self, information):
            return ([], information) if joined else tags(information)

    part = {"n": 0, "last": None, "top": None}
    stepping = [threading.Event()]

    def step(d):
        """yt-dlp's postprocessor_hooks: one step runs at a time, from its started hook to its finished one."""
        stepping[0].set()
        what = STEPS.get(d.get("postprocessor"))
        if what == "join":
            merging[:] = [True] if d.get("status") == "started" else []
        if d.get("status") == "started" and what:
            try:
                out, total = output(what, d["info_dict"])
            except (KeyError, TypeError):  # a change in the engine's insides: no number, the job goes on
                return
            stepping[0] = threading.Event()
            threading.Thread(target=watch, args=(what, out, total, part["n"], stepping[0]), daemon=True).start()

    def hook(d):
        if d.get("status") == "downloading":  # only what this job writes; "already downloaded" is someone's file
            for name in {d.get("filename"), d.get("tmpfilename")} - {None}:
                wrote(name)
        if part["n"] == 0 and d.get("info_dict"):
            details(d["info_dict"])  # yt-dlp tells us as soon as the first part starts
        if d.get("status") == "downloading" and part["last"] != "downloading":
            part["n"] += 1
        part["last"] = d.get("status")
        progress, what = where(d, part["n"], spec, plan)
        if progress is not None:  # never backwards: pieces finish out of order, and the old way may take over
            part["top"] = progress = max(progress, part["top"] or 0)
        emit(
            event="progress",
            status=d.get("status"),
            part=part["n"],
            progress=progress,
            what=what,
            downloaded=d.get("downloaded_bytes"),
            total=total_of(d),
            speed=d.get("speed"),
            eta=d.get("eta"),
        )
        fid = (d.get("info_dict") or {}).get("format_id")
        if d.get("status") == "finished" and fid in used:
            finished.add(fid)
            if os.path.getsize(d["filename"]) != d["info_dict"]["filesize"]:  # a piece came twice, or too much
                raise Wrong(
                    f"{d['filename']} is {os.path.getsize(d['filename'])} bytes, not {d['info_dict']['filesize']}"
                )
        if module == "yt_dlp" and spec["ffmpeg"] and d.get("status") == "finished" and d.get("info_dict"):
            tag_audio(d)

    refused = []

    def wanted(info, incomplete=False):
        """The engines' match_filter: one video per job, and no live stream, which would record without end."""
        if not spec.get("item") and (info.get("playlist_index") is not None or "playlist_count" in info):
            refused.append(Failure("This link is a list of videos.", "playlist"))  # a channel's tab, say
            return "Spool saves one video per download"
        if info.get("is_live") or info.get("live_status") == "is_live":
            refused.append(Failure(LIVE, "live"))
            return "Spool does not record live streams"
        if module != "yt_dlp":  # youtube-dl asks only once the streams are chosen, before it writes
            tell(info.get("id"))
        return None

    # shortcut: youtube-dl has no step between choosing and naming, so its class is the preset's, even when a
    # playlist entry has no such class, and its merge's .temp file goes unreported; drop with youtube-dl
    cls = "" if module == "yt_dlp" else CLASS.get(spec["preset"], "")
    params.update(
        match_filter=wanted,
        **format_params(spec["preset"], spec["ffmpeg"], module),
        outtmpl=NAME[module] + cls + ".%(ext)s",  # relative to dest
        updatetime=False,  # stamp files with the download time, not the upload time
        progress_hooks=[hook],
        **({"postprocessor_hooks": [step]} if module == "yt_dlp" else {}),
        retries=10,
        fragment_retries=10,
    )
    if spec.get("item"):
        params["playlist_items"] = str(spec["item"])
    else:
        params["noplaylist"] = True
    params["postprocessors"] = []
    if spec["preset"] == "mp3":
        params["postprocessors"].append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3"})
    if spec["ffmpeg"] and module != "yt_dlp":  # chapters and the video's details go into the file; yt-dlp's: Details
        params["postprocessors"].append({"key": "FFmpegMetadata"})
    try:  # from inside dest, an engine cannot recreate it: a path relative to a deleted folder goes nowhere
        os.chdir(spec["dest"])
    except OSError:
        raise RuntimeError("The destination folder is missing.") from None
    with engine.YoutubeDL(params) as ydl:
        if module == "yt_dlp":
            ydl.add_post_processor(Name(), when="video")
            if spec["ffmpeg"]:  # after the MP3 step, which params added first
                ydl.add_post_processor(Details(), when="post_process")
            ydl.add_post_processor(Capture(), when="after_move")
        else:
            ydl.add_post_processor(Capture())

        def fetch():
            try:
                ydl.download([spec["url"]])
            except Exception as exc:
                if not (joined and merging):
                    raise
                # the parts stay on disk, so the engine finds them and only joins again
                emit(
                    event="log", line=f"[spool] the join with the details failed, so Spool joined the plain way: {exc}"
                )
                ydl.params["postprocessor_args"].pop("merger+ffmpeg_o")
                joined.clear()
                plain.append(True)
                ydl.download([spec["url"]])

        try:
            fetch()
        except Exception as exc:
            now = next((f["format_id"] for f in plan if f["format_id"] not in finished), None)
            if not (isinstance(exc, Wrong) or now in used) or any(w in str(exc).lower() for w in NOT_PIECES):
                raise  # only a failure to fetch pieces goes on the old way
            emit(event="log", line=f"[spool] fetching in pieces failed, so Spool fetches it whole: {exc}")
            for name in used.values():
                dropped(name)  # what came in pieces is not trusted: it comes again whole
            pieces[0] = False
            part.update(n=0, last=None)
            fetch()
    if refused:
        raise refused[0]
    path = final.get("path")
    if not path and not seen:
        raise RuntimeError("Spool found no video at this link.")  # an empty playlist, or an item past its end
    if not path or not os.path.exists(path):
        raise RuntimeError("The engine finished but Spool could not find the file it saved.")
    emit(event="done", path=path, size=os.path.getsize(path))


def main():
    spec = json.loads(sys.stdin.readline())  # one line: with hold, the go follows on stdin
    module = "yt_dlp" if spec["engine"] == "yt-dlp" else "youtube_dl"
    sys.path.insert(0, os.path.abspath(spec["engine_path"]))  # download() moves into dest; engines import late
    engine = import_module(module)
    params = {"logger": Log(), "noprogress": True, "socket_timeout": 30}  # youtube-dl would wait 10 minutes
    if module == "yt_dlp" and spec.get("js_path"):  # Spool's own deno, outside PATH
        params["js_runtimes"] = {"deno": {"path": os.path.abspath(spec["js_path"])}}
    elif module == "yt_dlp" and spec.get("js_runtime") not in (None, "deno"):
        params["js_runtimes"] = {spec["js_runtime"]: {}}  # yt-dlp enables deno by itself
    if spec["mode"] == "probe":
        probe(engine, module, params, spec)
    else:
        download(engine, module, params, spec)


def run():
    sys.stdout = sys.stderr  # an engine that prints must not corrupt the event stream
    try:
        main()
    except BaseException as exc:  # report anything, including the engine's own exits
        message, kind = str(exc) or type(exc).__name__, getattr(exc, "kind", None)
        if Log.no_js and any(p in message.lower() for p in NO_JS_FAILS):  # the real cause of what failed
            message, kind = "No supported JavaScript runtime could be found.\n" + message, "no_js"
        emit(event="error", message=message, **({"kind": kind} if kind else {}))
        sys.exit(1)


if __name__ == "__main__":
    run()
