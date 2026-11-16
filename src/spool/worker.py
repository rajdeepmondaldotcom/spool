"""Worker: a child process that runs one engine call and reports JSON lines on stdout.

Spec on stdin: {engine, engine_path, mode: "probe" | "download", url, ffmpeg, js_runtime?, dest?, preset?, item?}.
Events: {"event": "log" | "progress" | "meta" | "file" | "info" | "done" | "error", ...}; an error carries "kind"
("live", "no_js" or "playlist") when the worker knows it. Standard library only; run with python -I.
"""

import json
import os
import sys
from importlib import import_module

OUT = sys.stdout
LIVE = "This is live now."  # jobs.py maps these words to the error kind "live"
NO_JS_FAILS = ("requested format is not available", "only images are available")  # what a missing runtime causes


class Failure(Exception):
    def __init__(self, message, kind=None):
        super().__init__(message)
        self.kind = kind


def emit(**event):
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
    return {"id": pid, "label": LABELS.get(pid, f"{pid}p"), "detail": detail, "size": size}


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


def estimate(formats, preset, ffmpeg, duration):
    """Estimated bytes for a preset, choosing streams by the rule yt-dlp uses. None when unknown."""
    audio = [f for f in formats if has(f, "acodec") and not has(f, "vcodec")]
    both = [f for f in formats if has(f, "acodec") and has(f, "vcodec")]
    if preset == "mp3":
        return int(duration * MP3_RATE) if duration else None
    if preset == "audio":
        m4a = [f for f in audio if f.get("ext") == "m4a"]
        return size(duration, max(m4a or audio or both, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None))
    return size(duration, *chosen(formats, None if preset == "best" else int(preset), ffmpeg))


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


def entry(info, ffmpeg):
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
    done, total = d.get("downloaded_bytes"), d.get("total_bytes") or d.get("total_bytes_estimate")
    frac = 1.0 if d.get("status") == "finished" else min(1.0, done / total) if done is not None and total else None
    sizes = [bytes_of(f, (info or {}).get("duration")) for f in parts]
    if frac is None:
        return (i / len(parts) or None), what
    if all(sizes):
        return (sum(sizes[:i]) + frac * sizes[i]) / sum(sizes), what
    return (i + frac) / len(parts), what


def probe(engine, params, spec):
    params.update(extract_flat="in_playlist", skip_download=True)
    with engine.YoutubeDL(params) as ydl:
        info = ydl.extract_info(spec["url"], download=False)
    if info.get("_type") == "playlist":
        found = [(n, entry(e, spec["ffmpeg"])) for n, e in enumerate(info.get("entries") or [], 1) if e]
        urls = [e["url"] for _, e in found]
        for n, e in found:  # no link of its own: download it as item n of the probed link
            if (
                not str(e["url"]).startswith(("http://", "https://"))
                or urls.count(e["url"]) > 1
                or e["url"] == spec["url"]
            ):
                e.update(url=spec["url"], item=n)
        emit(event="info", kind="playlist", title=info.get("title"), entries=[e for _, e in found])
    else:
        emit(event="info", kind="video", title=info.get("title"), entries=[entry(info, spec["ffmpeg"])])


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

    plan = []
    seen = set()

    def wrote(name):
        if name not in seen:
            seen.add(name)
            emit(event="file", path=here(name))

    class Name(PostProcessor):
        """yt-dlp's step between choosing the streams and naming the file."""

        def run(self, information):
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
            return [], information

    joined = []  # the join takes chapters and details from the audio part: no second pass over the joined file

    def tag_audio(d):
        """Tag the small audio part and have the join copy its tags, instead of rewriting the joined file after.

        Any failure leaves today's two passes: the Details step then writes them into the joined file."""
        info = d["info_dict"]
        i = next((k for k, f in enumerate(plan) if f.get("format_id") == info.get("format_id")), None)
        if i is None or len(plan) < 2 or has(info, "vcodec") or not has(info, "acodec"):
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

    part = {"n": 0, "last": None}

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
        emit(
            event="progress",
            status=d.get("status"),
            part=part["n"],
            progress=progress,
            what=what,
            downloaded=d.get("downloaded_bytes"),
            total=d.get("total_bytes") or d.get("total_bytes_estimate"),
            speed=d.get("speed"),
            eta=d.get("eta"),
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
        ydl.download([spec["url"]])
    if refused:
        raise refused[0]
    path = final.get("path")
    if not path and not seen:
        raise RuntimeError("Spool found no video at this link.")  # an empty playlist, or an item past its end
    if not path or not os.path.exists(path):
        raise RuntimeError("The engine finished but Spool could not find the file it saved.")
    emit(event="done", path=path, size=os.path.getsize(path))


def main():
    spec = json.load(sys.stdin)
    module = "yt_dlp" if spec["engine"] == "yt-dlp" else "youtube_dl"
    sys.path.insert(0, os.path.abspath(spec["engine_path"]))  # download() moves into dest; engines import late
    engine = import_module(module)
    params = {"logger": Log(), "noprogress": True, "socket_timeout": 30}  # youtube-dl would wait 10 minutes
    if module == "yt_dlp" and spec.get("js_path"):  # Spool's own deno, outside PATH
        params["js_runtimes"] = {"deno": {"path": os.path.abspath(spec["js_path"])}}
    elif module == "yt_dlp" and spec.get("js_runtime") not in (None, "deno"):
        params["js_runtimes"] = {spec["js_runtime"]: {}}  # yt-dlp enables deno by itself
    if spec["mode"] == "probe":
        probe(engine, params, spec)
    else:
        download(engine, module, params, spec)


if __name__ == "__main__":
    sys.stdout = sys.stderr  # an engine that prints must not corrupt the event stream
    try:
        main()
    except BaseException as exc:  # report anything, including the engine's own exits
        message, kind = str(exc) or type(exc).__name__, getattr(exc, "kind", None)
        if Log.no_js and any(p in message.lower() for p in NO_JS_FAILS):  # the real cause of what failed
            message, kind = "No supported JavaScript runtime could be found.\n" + message, "no_js"
        emit(event="error", message=message, **({"kind": kind} if kind else {}))
        sys.exit(1)
