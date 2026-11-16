"""A fake engine that plays scripted scenarios chosen by the link, with no network.

Links look like https://fake.test/<scenario>[/<n>][?delay=<seconds per step>].
It implements only what spool's worker uses: YoutubeDL(params) as a context manager,
extract_info, download, add_post_processor, progress and post-processor hooks, the merger's
postprocessor_args, FFmpegMetadataPP and the logger.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import import_module
from urllib.parse import parse_qs, unquote, urlsplit

THUMB = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 9'%3E"
    "%3Crect width='16' height='9' fill='%23888'/%3E%3C/svg%3E"
)
VIDEO_FORMATS = [
    {
        "format_id": "hls-1080",
        "ext": "mp4",
        "height": 1080,
        "width": 1920,
        "vcodec": "avc1.640028",
        "acodec": "none",
        "tbr": 9000,
        "protocol": "m3u8_native",
    },
    {
        "format_id": "137",
        "ext": "mp4",
        "height": 1080,
        "width": 1920,
        "vcodec": "avc1.640028",
        "acodec": "none",
        "filesize": 3_000_000,
    },
    {
        "format_id": "248",
        "ext": "webm",
        "height": 1080,
        "width": 1920,
        "vcodec": "vp9",
        "acodec": "none",
        "filesize": 2_500_000,
    },
    {
        "format_id": "136",
        "ext": "mp4",
        "height": 720,
        "width": 1280,
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "filesize": 1_500_000,
    },
    {
        "format_id": "135",
        "ext": "mp4",
        "height": 480,
        "width": 854,
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "filesize": 800_000,
    },
    {
        "format_id": "18",
        "ext": "mp4",
        "height": 360,
        "width": 640,
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "filesize": 600_000,
    },
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129, "filesize": 400_000},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135, "filesize": 380_000},
]
VERTICAL_FORMATS = [  # a phone video: the class is the short side
    {
        "format_id": "v1080",
        "ext": "mp4",
        "width": 1080,
        "height": 1920,
        "vcodec": "avc1.640028",
        "acodec": "none",
        "filesize": 2_800_000,
    },
    {
        "format_id": "v720",
        "ext": "mp4",
        "width": 720,
        "height": 1280,
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "filesize": 1_300_000,
    },
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129, "filesize": 400_000},
]
FOUR_THREE_FORMATS = [  # an old 4:3 video: 640x480 is class 480, 480x360 is class 360
    {
        "format_id": "v480",
        "ext": "mp4",
        "width": 640,
        "height": 480,
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "filesize": 500_000,
    },
    {
        "format_id": "v360",
        "ext": "mp4",
        "width": 480,
        "height": 360,
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "filesize": 300_000,
    },
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129, "filesize": 400_000},
]
VP9_TOP_FORMATS = [f for f in VIDEO_FORMATS if f["format_id"] not in ("hls-1080", "137")]  # 1080 only in VP9
AUDIO_FORMATS = [
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129, "filesize": 500_000},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135, "filesize": 480_000},
]
NO_COMBINED_FORMATS = [f for f in VIDEO_FORMATS if f["format_id"] != "18"]  # every stream is picture or sound
SLOW_FORMATS = [
    {
        "format_id": "18",
        "ext": "mp4",
        "height": 360,
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "filesize": 4_000_000,
    },
]


TAGGED = set()  # files holding chapters and details: written by a metadata step, or carried over by a join
HOME = os.path.realpath(os.getcwd())  # the engine is imported before the worker moves: the test's own tmp_path


class Named:
    """An engine post-processor the fake only names (FFmpegMetadata, FFmpegExtractAudio)."""

    def __init__(self, key):
        self.key = key

    def run(self, information):
        if self.key == "FFmpegMetadata":
            TAGGED.add(information["filepath"])
        return [], information


class DownloadError(Exception):
    pass


class PostProcessor:
    def __init__(self, downloader=None):
        self._downloader = downloader

    def run(self, information):
        return [], information


class FFmpegMetadataPP(PostProcessor):
    """yt-dlp's chapters-and-details step: it rewrites the whole file it is given, through <name>.temp.<ext>."""

    def __init__(self, downloader, add_metadata=True, add_chapters=True, add_infojson="if_exists"):
        super().__init__(downloader)

    def run(self, info):
        ydl, path = self._downloader, info["filepath"]
        ydl.pp_hook("started", "Metadata", info)
        ydl.log(f'[Metadata] Adding metadata to "{path}"')
        if info.get("_fail_tags") and "requested_formats" not in info:  # one part's, not the joined file's
            raise DownloadError("ERROR: Postprocessing: Conversion failed!")
        temp = "{}.temp{}".format(*os.path.splitext(path))
        shutil.copyfile(path, temp)
        os.replace(temp, path)
        TAGGED.add(path)
        ydl.pp_hook("finished", "Metadata", info)
        return [], info


def video(url, n, title=None, formats=VIDEO_FORMATS, **extra):
    vid = f"vid-{n}"
    return {
        "id": vid,
        "title": title or f"Fake video {n}",
        "uploader": "Fake Channel",
        "duration": 95,
        "thumbnail": THUMB,
        "webpage_url": url,
        "formats": formats,
        "is_live": False,
        "timestamp": 1_100_000_000,  # uploaded in 2004: engines stamp files with this unless updatetime is off
        **extra,
    }


def flat(n):
    return {
        "_type": "url",
        "id": f"vid-{n}",
        "url": f"https://fake.test/video/{n}",
        "title": f"Fake video {n}",
        "duration": 95,
        "uploader": "Fake Channel",
        "thumbnails": [{"url": THUMB}],
    }


def scenario(url):
    parts = urlsplit(url)
    path = [p for p in parts.path.split("/") if p]
    name = path[0] if parts.hostname == "fake.test" and path else ""
    n = unquote(path[1]) if len(path) > 1 else "1"
    delay = parse_qs(parts.query).get("delay")
    delay = float(delay[0]) if delay else None
    if name == "video":
        fail = parse_qs(parts.query).get("fail")  # merge: the join breaks half way; tags: a part can't be tagged
        join = float(parse_qs(parts.query).get("join", ["0"])[0])  # seconds per tenth of the join
        extra = {"_fail_merge": fail == ["merge"], "_fail_tags": fail == ["tags"], "_join": join}
        return {**video(url, n), **extra}, delay if delay is not None else 0.02
    if name == "audio":
        return {
            **video(url, n, "Fake audio track", AUDIO_FORMATS),
            "id": f"aud-{n}",
        }, delay if delay is not None else 0.1
    if name == "slow":
        return {
            **video(url, n, "Fake slow video", SLOW_FORMATS),
            "id": f"slow-{n}",
        }, delay if delay is not None else 0.5
    if name == "ytdl-only":
        return {**video(url, n, "Fake video only youtube-dl reads"), "id": f"only-{n}"}, delay or 0
    if name == "vanish":  # the destination is deleted after Spool checked it and before the engine writes
        return {**video(url, n), "_rm": parse_qs(parts.query)["rm"][0]}, delay or 0
    if name in ("403-start", "403-once"):  # refused before any bytes: always, or only the first time
        return {
            **video(url, n, "Fake refused video"),
            "id": f"refused-{n}",
            "_refuse_at": 0,
            "_once": name == "403-once",
        }, 0
    if name == "403":
        return {**video(url, n, "Fake refused video"), "id": f"refused-{n}", "_refuse": True}, delay or 0
    if name == "live":
        return {**video(url, n, "Fake live stream"), "id": f"live-{n}", "is_live": True, "live_status": "is_live"}, 0
    if name == "long-title":
        title = "Ünïcödé 日本語 a very long title that keeps going " * 9 + "and ends here"
        return {**video(url, n, title), "id": f"long-{n}"}, delay if delay is not None else 0.15
    if name == "vertical":
        return {**video(url, n, "Fake vertical video", VERTICAL_FORMATS), "id": f"vert-{n}"}, delay or 0
    if name == "four-three":
        return {**video(url, n, "Fake 4:3 video", FOUR_THREE_FORMATS), "id": f"43-{n}"}, delay or 0
    if name == "vp9-top":
        return {**video(url, n, "Fake VP9 video", VP9_TOP_FORMATS), "id": f"vp9-{n}"}, delay or 0
    if name == "hls":  # streams with a bitrate but no file size
        hls = [{"format_id": "hls-720", "ext": "mp4", "height": 720, "tbr": 800, "protocol": "m3u8_native"}]
        return {**video(url, n, "Fake streamed video", hls), "id": f"hls-{n}"}, 0
    if name == "no-combined":  # a site that never offers picture and sound in one stream
        return {**video(url, n, "Fake split video", NO_COMBINED_FORMATS), "id": f"split-{n}"}, delay or 0
    if name == "pipes":  # each of these characters becomes a 3-byte one in a yt-dlp file name
        return {**video(url, n, "a|" * 150), "id": f"pipes-{n}"}, delay or 0
    if name == "channel":  # a channel's entries are its tabs, each a list of videos
        tabs = [
            {"_type": "url", "id": f"tab-{t}", "url": f"https://fake.test/playlist/{t}", "title": t}
            for t in ("Videos", "Shorts")
        ]
        return {"_type": "playlist", "id": "chan", "title": "Fake channel", "entries": tabs}, delay or 0
    if name == "playlist":
        return {"_type": "playlist", "id": "pl-1", "title": "Fake playlist", "entries": [flat(i) for i in (1, 2, 3)]}, 0
    if name == "shared-playlist":  # archive.org multi-file items: every entry carries the item's own link
        entries = [{**flat(i), "id": f"shared-{i}", "title": f"Shared item {i}", "url": url} for i in (1, 2, 3)]
        return {"_type": "playlist", "id": "shared", "title": "Fake shared item", "entries": entries}, delay or 0
    if name == "ytdl-ids":  # youtube-dl lists YouTube playlist entries by bare id
        entries = [
            {"_type": "url", "ie_key": "Youtube", "id": f"abcdefghij{i}", "url": f"abcdefghij{i}", "title": f"Y {i}"}
            for i in (1, 2)
        ]
        return {"_type": "playlist", "id": "yt", "title": "Fake YouTube playlist", "entries": entries}, 0
    if name == "empty-playlist":
        return {"_type": "playlist", "id": "pl-0", "title": "Fake empty playlist", "entries": []}, 0
    if name == "broken":  # an engine bug, with the engines' own report-this-bug tail
        raise DownloadError(
            f"WARNING: [fake] something odd\nERROR: [fake] broken-{n}: Something new broke; please report this issue on "
            " https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. "
            "Confirm you are on the latest version using  yt-dlp -U"
        )
    if name == "needs-js":  # YouTube without deno, node, bun or quickjs, as the tester saw it
        return {**video(url, n, "Fake video needing JavaScript"), "id": f"js-{n}", "_needs_js": True}, 0
    if name == "needs-js-403":  # the JavaScript warning, then a failure it did not cause
        return {
            **video(url, n, "Fake refused video"),
            "id": f"js-{n}",
            "_needs_js": "warn",
            "_refuse_at": 0,
            "_once": False,
        }, 0
    if name == "unavailable":
        raise DownloadError(f"ERROR: [fake] {n}: Video unavailable. This video has been removed by the uploader")
    raise DownloadError(f"ERROR: Unsupported URL: {url}")


FULL_WIDTH = {**{c: chr(ord(c) + 0xFEE0) for c in '"*:<>?|'}, "/": "⧸", "\\": "⧹"}  # yt-dlp's file names


def has(f, codec):
    return f.get(codec) != "none"


class YoutubeDL:
    STEPS = 10
    HOOK_INFO = False  # youtube-dl's progress hooks carry no info_dict; yt-dlp's do
    NAME = "youtube-dl"

    def scenario(self, url):
        if self.NAME == "yt-dlp" and "/ytdl-only" in url:  # a site only the second engine reads
            raise DownloadError(f"ERROR: Unsupported URL: {url}")
        return scenario(url)

    METADATA_OPTIONS = set()  # what the engine's FFmpegMetadata post-processor accepts
    WHEN = {"post_process": 1}

    def __init__(self, params=None):
        self.params = params or {}
        if "socket_timeout" not in self.params:  # youtube-dl would wait 10 minutes for a host that stops answering
            raise TypeError("the fake engine wants a socket_timeout")
        self.pps = []  # (when rank, post-processor) in the order the engine runs them
        for p in self.params.get("postprocessors", []):
            options = set(p) - {"key", "when"}
            if p["key"] == "FFmpegMetadata" and not options <= self.METADATA_OPTIONS:
                raise TypeError(f"FFmpegMetadataPP got unexpected options {sorted(options - self.METADATA_OPTIONS)}")
            self.pps.append((1, Named(p["key"])))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, pp):
        self.pps.append((1, pp))

    def pp_hook(self, status, key, info):
        for h in self.params.get("postprocessor_hooks", []):
            h({"status": status, "postprocessor": key, "info_dict": info})

    def log(self, msg):
        if self.params.get("logger"):
            self.params["logger"].debug(msg)

    def fail(self, msg):
        if self.params.get("logger"):
            self.params["logger"].error(msg)
        raise DownloadError(msg)

    def extract_info(self, url, download=True):
        self.log(f"[fake] Extracting URL: {url}")
        try:
            info, _ = self.scenario(url)
        except DownloadError as exc:
            self.fail(str(exc))
        self.needs_js(info)
        return info

    def needs_js(self, info):
        if info.get("_needs_js"):
            self.params["logger"].warning(
                "[youtube] No supported JavaScript runtime could be found. Only deno is enabled by default; to use "
                "another runtime add  --js-runtimes RUNTIME[:PATH]  to your command/config. YouTube extraction without "
                "a JS runtime has been deprecated, and some formats may be missing. See  "
                "https://github.com/yt-dlp/yt-dlp/wiki/EJS  for details on installing one"
            )
            if info["_needs_js"] == "warn":
                return
            self.fail(
                f"ERROR: [youtube] {info['id']}: Requested format is not available. Use --list-formats for a list of available formats"
            )

    def skipped(self, info):
        refused = self.params.get("match_filter") and self.params["match_filter"](info, incomplete=False)
        if refused:
            self.log(f"[download] {refused}, skipping ..")  # the engines skip quietly; no error
        return refused

    def download(self, urls):
        import_module(self.NAME.replace("-", "_") + ".downloader")  # the engines load their parts as they need them
        for url in urls:
            try:
                info, delay = self.scenario(url)
            except DownloadError as exc:
                self.fail(str(exc))
            self.needs_js(info)
            if info.get("_type") != "playlist":
                self.download_one(info, delay)
                continue
            if self.NAME == "yt-dlp" and self.skipped(
                {"playlist_count": len(info["entries"]), "playlist_id": info["id"]}
            ):
                continue  # yt-dlp asks about the list itself first
            wanted = self.params.get("playlist_items")  # without it, an engine downloads every entry
            for i, e in enumerate(info["entries"], 1):
                if wanted is None or str(i) == wanted:
                    self.download_one({**video(url, str(i), e["title"]), "id": e["id"], "playlist_index": i}, delay)
        return 0

    def evaluate_outtmpl(self, tmpl, info, sanitize=False):
        """The engines' template fields: %(a,b|default).N(s|B). yt-dlp cuts a value and then cleans it for a file
        name; youtube-dl cleans and then cuts."""

        def field(m):
            names, default, n, conv = m.group(1).split(","), m.group(2), m.group(3), m.group(4)
            value = str(next((info[k] for k in names if info.get(k) is not None), "NA" if default is None else default))
            clean = (lambda v: v) if not sanitize else self.clean_id if names == ["id"] else self.clean
            if self.NAME == "youtube-dl":
                value = clean(value)
            if n and conv == "B":
                value = value.encode()[: int(n)].decode(errors="ignore")
            elif n:
                value = value[: int(n)]
            return (value if self.NAME == "youtube-dl" else clean(value)).replace("%", "%%")

        return re.sub(r"%\(([\w,]+)(?:\|([^)]*))?\)(?:\.(\d+))?([sB])", field, tmpl) % ()

    def clean(self, value):
        if self.NAME == "youtube-dl":
            return re.sub(r"[\\/|*<>]", "_", value).replace("?", "").replace(":", " -").replace('"', "'")
        return "".join(FULL_WIDTH.get(c, c) for c in value)

    def clean_id(self, value):
        return re.sub(r"[\\/|*<>]", "_", value)

    def prepare_filename(self, info):
        return self.name(info, info["ext"])

    def name(self, info, ext):
        name = self.evaluate_outtmpl(self.params["outtmpl"], {**info, "ext": ext}, sanitize=True)
        if len(os.path.basename(name + ".part").encode()) > 255:
            raise OSError(63, "File name too long", name)
        return name

    def remove_test_folder(self, path):
        """Delete the one folder a test named, and only inside that test's own folder."""
        path = os.path.realpath(path)
        if os.path.commonpath([path, HOME]) != HOME or path == HOME:
            raise RuntimeError(f"refusing to delete {path}: not inside {HOME}")
        if os.path.commonpath([HOME, os.path.realpath(tempfile.gettempdir())]) != os.path.realpath(
            tempfile.gettempdir()
        ):
            raise RuntimeError(f"refusing to delete {path}: the test's folder {HOME} is not a temp folder")
        shutil.rmtree(path)

    def open(self, name, mode):
        folder = os.path.dirname(name)
        if folder:
            os.makedirs(folder, exist_ok=True)  # as the engines do: a full path recreates a deleted folder
        return open(name, mode)

    def select(self, info, fmt):
        """The streams for a format selector, as yt-dlp chooses them; fails as it does when nothing fits."""
        formats = info["formats"]
        sort = self.params.get("format_sort") or []
        n = next((int(s[4:]) for s in sort if s.startswith("res:")), None)

        def rank(f):  # shortcut: res, H.264 and the file size only; a stream with no file size ranks last
            side = min(f["width"], f["height"]) if f.get("width") and f.get("height") else f.get("height") or 0
            return (
                side if n is None or side <= n else -side,
                str(f.get("vcodec")).startswith("avc1"),
                f.get("filesize") or 0,
            )

        def pick(atom):
            kind, ext = atom.split("[")[0], re.search(r"\[ext=(\w+)\]", atom)
            if kind == "bv*":
                found = [f for f in formats if has(f, "vcodec")]
            elif kind in ("ba", "bestaudio"):
                found = [f for f in formats if has(f, "acodec") and not has(f, "vcodec")]
            else:  # b, best: picture and sound, unless every stream has only one of them
                found = [f for f in formats if has(f, "vcodec") and has(f, "acodec")]
                if not found and (all(has(f, "vcodec") for f in formats) or all(has(f, "acodec") for f in formats)):
                    found = formats
            found = [f for f in found if not ext or f["ext"] == ext.group(1)]
            return max(found, key=rank, default=None)

        for alternative in fmt.split("/"):
            parts = [pick(atom) for atom in alternative.split("+")]
            if all(parts):
                return parts[:1] if len(parts) == 2 and has(parts[0], "acodec") else parts  # never a second sound
        self.fail(
            f"ERROR: [fake] {info['id']}: Requested format is not available. Use --list-formats for a list of available formats"
        )

    def chosen(self, info):
        fmt = self.params.get("format", "best")
        if self.NAME == "yt-dlp":
            return self.select(info, fmt)
        formats = {f["format_id"]: f for f in info["formats"]}  # shortcut: youtube-dl's selectors, by format id
        if fmt.startswith("bestaudio") or "137" not in formats:
            return [formats["140"] if "140" in formats else info["formats"][0]]
        return [formats["137"], formats["140"]] if "+" in fmt else [formats["18"]]

    def download_one(self, info, delay):
        if self.skipped(info):
            return
        if info.get("_rm"):
            self.remove_test_folder(info["_rm"])
        mp3 = any(p.get("key") == "FFmpegExtractAudio" for p in self.params.get("postprocessors", []))
        parts = self.chosen(info)
        if len(parts) == 2:
            info = {
                **info,
                "requested_formats": parts,
                "format_id": "+".join(f["format_id"] for f in parts),
                "ext": "mp4",
            }
        else:
            info = {**info, **parts[0]}
        for _, pp in sorted((p for p in self.pps if p[0] == self.WHEN.get("video")), key=lambda x: x[0]):
            _, info = pp.run(info)  # yt-dlp's step after choosing the streams, before naming the file
        final = self.name(info, "mp3" if mp3 else info["ext"])
        if os.path.exists(final):
            self.log(f"[download] {final} has already been downloaded")
            self.hook(
                status="finished",
                downloaded_bytes=os.path.getsize(final),
                total_bytes=os.path.getsize(final),
                filename=final,
            )
        else:
            files = [self.fetch(info, f, delay, parts) for f in parts]
            if len(files) == 2:
                self.merge(info, parts, files, final, delay)
            elif mp3:  # ffmpeg writes the .mp3 as it goes, so a cut leaves a short file that looks finished
                about = {**info, "filepath": files[0]}
                self.pp_hook("started", "ExtractAudio", about)
                self.log(f"[ExtractAudio] Destination: {final}")
                with self.open(final, "wb") as out:
                    out.write(b"\0" * 1000)
                    out.flush()
                    time.sleep(delay * 10)
                    out.write(b"\0" * 1000)
                os.remove(files[0])
                self.pp_hook("finished", "ExtractAudio", about)
            else:
                os.replace(files[0], final)
        order = [pp for when, pp in sorted(self.pps, key=lambda x: x[0]) if when != self.WHEN.get("video")]
        self.log("[fake] ran: " + ", ".join(getattr(pp, "key", type(pp).__name__) for pp in order))
        if self.params.get("updatetime", True) and info.get("timestamp"):
            os.utime(final, (info["timestamp"], info["timestamp"]))
        for pp in order:
            pp.run({**info, "filepath": final})
        self.log(f"[fake] details in the file: {final in TAGGED}")

    def merge(self, info, parts, files, final, delay):
        """yt-dlp's join: chapters and details come from the input its postprocessor_args name, else none."""
        about = {
            **info,
            "filepath": final,
            "requested_formats": [{**f, "filepath": n} for f, n in zip(parts, files, strict=True)],
        }
        self.pp_hook("started", "Merger", about)
        self.log(f'[Merger] Merging formats into "{final}"')
        args = (self.params.get("postprocessor_args") or {}).get("merger+ffmpeg_o", [])
        temp = os.path.splitext(final)[0] + ".temp.mp4"
        time.sleep(delay)
        with self.open(temp, "wb") as out:
            for f in files:
                with open(f, "rb") as src:
                    data = src.read()
                step = len(data) // self.STEPS + 1
                for k in range(0, len(data), step):  # ffmpeg writes as it goes
                    out.write(data[k : k + step])
                    out.flush()
                    time.sleep(info.get("_join", 0))
                if info.get("_fail_merge"):
                    self.fail("ERROR: Postprocessing: Conversion failed!")
        os.replace(temp, final)
        if "-map_metadata" in args:
            i = args[args.index("-map_metadata") + 1]
            assert args[args.index("-map_chapters") + 1] == i, args
            if files[int(i)] in TAGGED:
                TAGGED.add(final)
        for f in files:
            os.remove(f)
        self.pp_hook("finished", "Merger", about)

    def fetch(self, info, f, delay, parts):
        split = len(parts) > 1
        name = self.name(info, f"f{f['format_id']}.{f['ext']}" if split else f["ext"])
        about = {k: v for k, v in {**info, **f}.items() if k != "requested_formats"}  # yt-dlp's hooks see one part
        part = name + ".part"
        total = f.get("filesize") or 1_000_000  # a stream with only a bitrate: any size will do
        done = os.path.getsize(part) if os.path.exists(part) else 0
        if done:
            self.log(f"[download] Resuming download at byte {done}")
        step = total // self.STEPS
        if info["id"].startswith("slow"):  # a child of our own, as ffmpeg would be, for cancel to kill
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
        with self.open(part, "ab") as out:
            while done < total:
                if info.get("_refuse") and done >= step * 3:
                    self.fail("ERROR: unable to download video data: HTTP Error 403: Forbidden")
                if info.get("_refuse_at") == done:
                    marker = f".fake-refused-{info['id']}"  # in the destination, the engine's current folder
                    if not (info["_once"] and os.path.exists(marker)):
                        open(marker, "w").close()
                        self.fail("ERROR: unable to download video data: HTTP Error 403: Forbidden")
                chunk = min(step, total - done)
                out.write(b"\0" * chunk)
                out.flush()
                done += chunk
                self.hook(
                    **({"info_dict": about} if self.HOOK_INFO else {}),
                    status="downloading",
                    downloaded_bytes=done,
                    total_bytes=total,
                    speed=step / delay if delay else None,
                    eta=int((total - done) / step * delay),
                    filename=name,
                    tmpfilename=part,
                )
                time.sleep(delay)
        os.replace(part, name)
        self.hook(
            **({"info_dict": about} if self.HOOK_INFO else {}),
            status="finished",
            downloaded_bytes=total,
            total_bytes=total,
            filename=name,
        )
        return name

    def hook(self, **d):
        for h in self.params.get("progress_hooks", []):
            h(d)
