"""Worker: a child process that runs one engine call and reports JSON lines on stdout.

Spec on stdin: {engine, engine_path, mode: "probe" | "download", url, ffmpeg, js_runtime?, dest?, preset?, item?}.
Events: {"event": "log" | "progress" | "meta" | "info" | "done" | "error", ...}. Standard library only; run with python -I.
"""

import json
import os
import sys
from importlib import import_module

OUT = sys.stdout
LIVE = "This is live now."  # jobs.py maps these words to the error kind "live"


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
TITLE = {
    "yt_dlp": "%(title).150B",
    "youtube_dl": "%(title).50s",
}  # youtube-dl has no byte precision; 50 chars <= 200 bytes
HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240)
FLAT_PRESETS = ("best", "1080", "720", "480", "audio")  # a flat playlist listing has no formats to read
LABELS = {"best": "Best", "audio": "Audio", "mp3": "MP3"}
CLASS = {str(h): f" {h}p" for h in HEIGHTS}  # two classes of one video must not share a file name


def preset(pid, size=None):
    return {"id": pid, "label": LABELS.get(pid, f"{pid}p"), "size": size}


def short(f):
    """A format's class: its short side, or its height when the site gives no width."""
    return min(f["width"], f["height"]) if f.get("width") and f.get("height") else (f.get("height") or 0)


def has(f, codec):
    return f.get(codec) != "none"  # no codec info means the stream carries it


def bytes_of(f, duration):
    """A stream's bytes; from the bitrate when the site gives no size, as yt-dlp estimates it."""
    return f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * (duration or 0)


def size(duration, *formats):
    sizes = [f and bytes_of(f, duration) for f in formats]
    return int(sum(sizes)) if all(sizes) else None


def ranking(n, duration):
    """yt-dlp's order for format_sort [res:N, fps, vcodec:h264, acodec:aac]: the site's own preference first,
    then the largest short side up to N (above N the smallest), then frame rate, H.264, AAC, size, bitrate."""

    def key(f):
        side = short(f)
        return (
            -1 if f.get("preference") is None else f["preference"],
            side if n is None or side <= n else -side,
            f.get("fps") or 0,
            str(f.get("vcodec")).startswith(("avc1", "h264")),
            str(f.get("acodec")).startswith(("mp4a", "aac")),
            bytes_of(f, duration),
            f.get("tbr") or 0,
        )

    return key


def estimate(formats, preset, ffmpeg, duration):
    """Estimated bytes for a preset, choosing streams by the rule yt-dlp uses. None when unknown."""
    audio = [f for f in formats if has(f, "acodec") and not has(f, "vcodec")]
    both = [f for f in formats if has(f, "acodec") and has(f, "vcodec")]
    if preset == "mp3":
        return None
    if preset == "audio":
        m4a = [f for f in audio if f.get("ext") == "m4a"]
        return size(duration, max(m4a or audio or both, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None))
    key = ranking(None if preset == "best" else int(preset), duration)
    video = [f for f in formats if has(f, "vcodec")]
    if ffmpeg and video and audio:  # bv*+ba
        return size(duration, max(video, key=key), max(audio, key=key))
    return size(duration, max(both or formats, key=key, default=None))  # b


def entry(info, ffmpeg):
    if info.get("_type") in ("url", "url_transparent"):
        presets = [preset(p) for p in FLAT_PRESETS]
    else:
        formats = info.get("formats") or [info]  # a site with one stream puts it on the info itself
        playable = formats if ffmpeg else [f for f in formats if has(f, "acodec")]  # no merging without ffmpeg
        classes = {short(f) for f in playable if has(f, "vcodec")}
        ids = ["best", *(str(h) for h in HEIGHTS if h in classes), "audio"]
        presets = [preset(p, estimate(formats, p, ffmpeg, info.get("duration"))) for p in ids]
    if ffmpeg:
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

    class Capture(PostProcessor):
        def run(self, information):
            path = information.get("filepath")
            final["path"] = path and os.path.abspath(path)
            details(information)  # youtube-dl's hooks carry no info, so this is where it tells us
            return [], information

    part = {"n": 0, "last": None}
    seen = set()

    def hook(d):
        if d.get("status") == "downloading":  # only what this job writes; "already downloaded" is someone's file
            for name in {d.get("filename"), d.get("tmpfilename")} - seen - {None}:
                seen.add(name)
                emit(event="file", path=os.path.abspath(name))
        if part["n"] == 0 and d.get("info_dict"):
            details(d["info_dict"])  # yt-dlp tells us as soon as the first part starts
        if d.get("status") == "downloading" and part["last"] != "downloading":
            part["n"] += 1
        part["last"] = d.get("status")
        emit(
            event="progress",
            status=d.get("status"),
            part=part["n"],
            downloaded=d.get("downloaded_bytes"),
            total=d.get("total_bytes") or d.get("total_bytes_estimate"),
            speed=d.get("speed"),
            eta=d.get("eta"),
        )

    live = []

    def not_live(info, incomplete=False):
        """The engines' match_filter: a live stream would record without end, so skip it and say why."""
        if info.get("is_live") or info.get("live_status") == "is_live":
            live.append(True)
            return "Spool does not record live streams"
        return None

    params.update(
        match_filter=not_live,
        **format_params(spec["preset"], spec["ffmpeg"], module),
        outtmpl=TITLE[module] + " [%(id)s]" + CLASS.get(spec["preset"], "") + ".%(ext)s",  # relative to dest
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
    if spec["ffmpeg"]:  # chapters and the video's details go into the file; youtube-dl's takes no options
        params["postprocessors"].append(
            {"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True}
            if module == "yt_dlp"
            else {"key": "FFmpegMetadata"}
        )
    try:  # from inside dest, an engine cannot recreate it: a path relative to a deleted folder goes nowhere
        os.chdir(spec["dest"])
    except OSError:
        raise RuntimeError("The destination folder is missing.") from None
    with engine.YoutubeDL(params) as ydl:
        if module == "yt_dlp":
            ydl.add_post_processor(Capture(), when="after_move")
        else:
            ydl.add_post_processor(Capture())
        ydl.download([spec["url"]])
    if live:
        raise RuntimeError(LIVE)
    path = final.get("path")
    if not path or not os.path.exists(path):
        raise RuntimeError("The engine finished but Spool could not find the file it saved.")
    emit(event="done", path=path, size=os.path.getsize(path))


def main():
    spec = json.load(sys.stdin)
    module = "yt_dlp" if spec["engine"] == "yt-dlp" else "youtube_dl"
    sys.path.insert(0, spec["engine_path"])
    engine = import_module(module)
    params = {"logger": Log(), "noprogress": True}
    if module == "yt_dlp" and spec.get("js_runtime") not in (None, "deno"):
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
        message = str(exc) or type(exc).__name__
        if Log.no_js:  # the real cause of what failed after the warning
            message = "No supported JavaScript runtime could be found.\n" + message
        emit(event="error", message=message)
        sys.exit(1)
