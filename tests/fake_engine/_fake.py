"""A fake engine that plays scripted scenarios chosen by the link, with no network.

Links look like https://fake.test/<scenario>[/<n>][?delay=<seconds per step>].
It implements only what spool's worker uses: YoutubeDL(params) as a context manager,
extract_info, process_video_result, download, add_post_processor, progress and post-processor hooks,
the merger's postprocessor_args, FFmpegMetadataPP and the logger. https://www.youtube.com/@<name>[/<tab>]
is a fake channel. A stream's bytes are its offsets mod 256, so a file joined wrongly shows.
"""

import json
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
V2160 = {"format_id": "v2160", "ext": "webm", "width": 3840, "height": 2160, "vcodec": "vp9", "filesize": 6_000_000}
V1440 = {
    "format_id": "v1440",
    "ext": "mp4",
    "width": 2560,
    "height": 1440,
    "vcodec": "avc1.640032",
    "filesize": 4_000_000,
}
H264_TALL_FORMATS = [  # H.264 up to 1440p, and a 2160p only in VP9
    {**V2160, "acodec": "none"},
    {**V1440, "acodec": "none"},
    *(f for f in VIDEO_FORMATS if f["format_id"] in ("137", "140", "251")),
]
NO_H264_FORMATS = [  # VP9 and Opus only: nothing plays everywhere
    {**V1440, "ext": "webm", "vcodec": "vp9", "acodec": "none"},
    *(f for f in VIDEO_FORMATS if f["format_id"] in ("248", "251")),
]
AUDIO_FORMATS = [
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129, "filesize": 500_000},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135, "filesize": 480_000},
]
NO_COMBINED_FORMATS = [f for f in VIDEO_FORMATS if f["format_id"] != "18"]  # every stream is picture or sound
TUBE_FORMATS = [  # YouTube's direct streams, which it lets a downloader fetch in pieces by byte range
    {**f, "protocol": "https", "url": f"https://fake.test/stream/{f['format_id']}"}
    for f in VIDEO_FORMATS
    if f["format_id"] in ("136", "140")
]
TUBE_FORMATS[0] = {**TUBE_FORMATS[0], "filesize": 25_000_000}  # three pieces of 10 MiB
TUBE_FORMATS += [  # a direct stream YouTube gives no size for, and a streamed one
    {**VIDEO_FORMATS[5], "protocol": "https", "url": "https://fake.test/stream/18", "filesize": None},
    {**VIDEO_FORMATS[0], "height": 480, "width": 854, "format_id": "hls-480", "filesize": None},
]
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


class ReExtractInfo(Exception):
    """yt-dlp's signal to read the video again (fresh stream links) and go on from what is on disk."""


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
        "formats": [dict(f) for f in formats],  # a new extraction gives new dicts, as the engines' do
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


def data(start, end):
    """A stream's bytes from start to end: each is its offset mod 256."""
    first = start % 256
    return (bytes(range(first, 256)) + bytes(range(256)) * ((end - start) // 256 + 1))[: end - start]


def channel(url, path):
    """A YouTube channel: its home lists its tabs, each tab its videos, newest first."""
    name = "Fake Channel"
    if path[0].startswith("@"):
        path = [None, *path]
    tab = path[2] if len(path) > 2 else None
    if tab is None:
        tabs = [
            {"_type": "url", "url": f"{url.rstrip('/')}/{t}", "title": f"{name} - {t.title()}"}
            for t in ("videos", "shorts")
        ]
        return {"_type": "playlist", "id": "UCfake", "title": name, "channel": name, "entries": tabs}, 0
    count = {"videos": 150, "shorts": 3}[tab]
    entries = [
        {**flat(n), "id": f"{tab}-{n}", "url": f"https://www.youtube.com/watch?v={tab}-{n}", "ie_key": "Youtube"}
        for n in range(count, 0, -1)
    ]
    return {
        "_type": "playlist",
        "id": "UCfake",
        "title": f"{name} - {tab.title()}",
        "channel": name,
        "entries": entries,
    }, 0


def scenario(url):
    parts = urlsplit(url)
    path = [p for p in parts.path.split("/") if p]
    if parts.hostname == "www.youtube.com" and path:
        return channel(url, path)
    name = path[0] if parts.hostname == "fake.test" and path else ""
    n = unquote(path[1]) if len(path) > 1 else "1"
    delay = parse_qs(parts.query).get("delay")
    delay = float(delay[0]) if delay else None
    if name == "video":
        fail = parse_qs(parts.query).get("fail")  # merge: the join breaks half way; tags: a part can't be tagged;
        # join-args: the join breaks only when told to take chapters and details from a part
        join = float(parse_qs(parts.query).get("join", ["0"])[0])  # seconds per tenth of the join
        extra = {"_fail_merge": fail == ["merge"], "_fail_tags": fail == ["tags"], "_join": join}
        extra["_fail_join_args"] = fail == ["join-args"]
        return {**video(url, n), **extra}, delay if delay is not None else 0.02
    if name == "audio":
        return {
            **video(url, n, "Fake audio track", AUDIO_FORMATS),
            "id": f"aud-{n}",
        }, delay if delay is not None else 0.1
    if name == "tube":  # YouTube, with faults chosen by the query (see pieces())
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        extra = {"extractor_key": "Youtube", "_q": q, "_stop": int(q.get("stop", 0))}
        extra["_cut_old"] = q.get("cut") == "old"  # the worker dies half way through a stream fetched whole
        extra["_fail_merge"] = q.get("fail") == "merge"
        formats = TUBE_FORMATS
        if q.get("size") == "small":  # the site says the video stream is 5 MB shorter than it is
            formats = [{**f, "filesize": f["filesize"] - 5_000_000, "_bytes": f["filesize"]} for f in formats[:1]]
            formats += TUBE_FORMATS[1:]
        return {**video(url, n, "Fake tube video", formats), "id": f"tube-{n}", **extra}, delay or 0
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
    if name == "h264-tall":
        return {**video(url, n, "Fake tall video", H264_TALL_FORMATS), "id": f"tall-{n}"}, delay or 0
    if name == "no-h264":
        return {**video(url, n, "Fake VP9 only video", NO_H264_FORMATS), "id": f"nh-{n}"}, delay or 0
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


def merged_ext(parts):
    """The engine's container for joined streams: MP4 or WebM when both fit it, else Matroska."""
    exts = {f["ext"] for f in parts}
    return "mp4" if exts <= {"mp4", "m4a"} else "webm" if exts == {"webm"} else "mkv"


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
        if self.params.get("logger"):
            self.params["logger"].debug("[fake] a new engine object")  # each costs time, and may cost requests
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
        if info.get("_type") == "playlist" and self.params.get("playlistend"):
            info = {**info, "entries": info["entries"][: self.params["playlistend"]]}
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
                for reads in range(1, 100):  # the engine reads the video again on every ReExtractInfo, without limit
                    try:
                        self.download_one(info, delay)
                        break
                    except ReExtractInfo:
                        self.log("[fake] throttled: reading the video again")
                        info = self.scenario(url)[0]
                    if reads == 5:
                        self.fail("ERROR: [fake] read the video again 5 times and would go on")
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
        for _, pp in (p for p in self.pps if p[0] == self.WHEN.get("pre_process")):
            _, info = pp.run(info)  # yt-dlp's step before it chooses the streams
        parts = self.chosen(info)
        self.log("[fake] chose " + "+".join(f["format_id"] for f in parts))
        if len(parts) == 2:
            info = {
                **info,
                "requested_formats": parts,
                "format_id": "+".join(f["format_id"] for f in parts),
                "ext": merged_ext(parts),
            }
        else:
            info = {**info, **parts[0]}
        for _, pp in sorted((p for p in self.pps if p[0] == self.WHEN.get("video")), key=lambda x: x[0]):
            _, info = pp.run(info)  # yt-dlp's step after choosing the streams, before naming the file
        parts = info.get("requested_formats") or [info]  # as that step left them
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
        before = (self.WHEN.get("pre_process"), self.WHEN.get("video"))
        order = [pp for when, pp in sorted(self.pps, key=lambda x: x[0]) if when not in before]
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
        temp = "{}.temp{}".format(*os.path.splitext(final))
        time.sleep(delay)
        with self.open(temp, "wb") as out:
            if info.get("_fail_join_args") and args:
                self.fail("ERROR: Postprocessing: Conversion failed!")  # before deleting the parts, as yt-dlp does
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
        if split and os.path.exists(name):  # a part a failed join left: yt-dlp keeps it
            self.log(f"[download] {name} has already been downloaded")
            self.hook(
                **({"info_dict": about} if self.HOOK_INFO else {}),
                status="finished",
                total_bytes=os.path.getsize(name),
                filename=name,
            )
            return name
        self.log(f"[download] Destination: {name}")
        if f.get("protocol") == "http_dash_segments":
            return self.pieces(info, f, name, about)
        if f.get("protocol") == "m3u8_native":
            self.log(f"[fake] pieces at once: {self.params.get('concurrent_fragment_downloads', 1)}")
        part = name + ".part"
        total = f.get("_bytes") or f.get("filesize") or 1_000_000  # only a bitrate: any size will do
        if self.params.get("throttledratelimit"):
            self.log(f"[fake] a stream fetched whole with a floor of {self.params['throttledratelimit']} B/s")
        done = os.path.getsize(part) if os.path.exists(part) else 0
        if done:
            self.log(f"[download] Resuming download at byte {done}")
        step = total // self.STEPS
        if info["id"].startswith("slow"):  # a child of our own, as ffmpeg would be, for cancel to kill
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
        with self.open(part, "ab") as out:
            while done < total:
                if info.get("_cut_old") and done >= total // 2:
                    os._exit(9)
                if info.get("_refuse") and done >= step * 3:
                    self.fail("ERROR: unable to download video data: HTTP Error 403: Forbidden")
                if info.get("_refuse_at") == done:
                    marker = f".fake-refused-{info['id']}"  # in the destination, the engine's current folder
                    if not (info["_once"] and os.path.exists(marker)):
                        open(marker, "w").close()
                        self.fail("ERROR: unable to download video data: HTTP Error 403: Forbidden")
                chunk = min(step, total - done)
                out.write(data(done, done + chunk))
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

    def pieces(self, info, f, name, about):
        """yt-dlp's DASH downloader: pieces by byte range, several at once, appended in order to <name>.part;
        <name>.ytdl counts the pieces appended. A .part with no .ytdl is appended to from piece 0, as the engine does,
        and nothing checks the size. Faults, from the link's query: throttle=piece (the second piece trickles, once),
        throttle=line (every piece is slow), refuse=pieces|limit (the second piece is refused, or limited),
        ignore=range (every piece is the whole stream), stop=<n> (the worker dies at piece n; with between=1 after
        appending it and before counting it)."""
        q, part, ytdl = info["_q"], name + ".part", name + ".ytdl"
        at_once, real = self.params.get("concurrent_fragment_downloads", 1), f.get("_bytes") or f["filesize"]
        tries = self.params.get("fragment_retries")
        pause = (self.params.get("retry_sleep_functions") or {}).get("fragment")
        self.log(f"[dashsegments] Total fragments: {len(f['fragments'])}; pieces at once: {at_once}")
        self.log(
            f"[fake] tries a piece {tries} more times, pausing {[pause(n=k) for k in range(tries)] if pause else 0}"
        )
        start = json.load(open(ytdl))["downloader"]["current_fragment"]["index"] if os.path.exists(ytdl) else 0
        skip = self.params.get("skip_unavailable_fragments", True)
        with self.open(part, "ab") as out:
            for i in range(start, len(f["fragments"])):
                if i == start == 0:
                    json.dump({"downloader": {"current_fragment": {"index": 0}}}, open(ytdl, "w"))
                a, b = map(int, re.search(r"[?&]range=(\d+)-(\d+)", f["fragments"][i]["url"]).groups())
                frag = f"{part}-Frag{i + 1}"
                with open(frag + ".part", "wb") as piece:
                    piece.write(data(0, real) if q.get("ignore") == "range" else data(a, min(b + 1, real)))
                os.replace(frag + ".part", frag)
                self.hook(  # bytes received count, appended or not; no total until the end, and the guess swings
                    info_dict=about,
                    status="downloading",
                    downloaded_bytes=out.tell() + os.path.getsize(frag),
                    total_bytes_estimate=f["filesize"] * (3 if i % 2 else 0.5),
                    fragment_index=i + 1,
                    fragment_count=len(f["fragments"]),
                    filename=name,
                    tmpfilename=part,
                )
                slow = q.get("throttle") == "line" or (
                    q.get("throttle") == "piece" and i == 1 and not os.path.exists(".fake-throttled")
                )
                if slow:
                    open(".fake-throttled", "w").close()  # in the destination, the engine's current folder
                    os.remove(frag)
                    if self.params.get("throttledratelimit"):  # below it for 3 seconds: read the video again
                        raise ReExtractInfo()
                    if q.get("throttle") == "piece":
                        self.fail("ERROR: [fake] a piece came at 1 KB/s for minutes")
                    continue  # a slow line: it comes, slowly
                if info.get("_stop") == i + 1 and not q.get("between"):
                    os._exit(9)  # killed, as a cancel or a crash would: the pieces stay on disk
                if q.get("refuse") and i == 1:
                    os.remove(frag)
                    if i + 2 <= len(f["fragments"]):  # the piece another connection fetched meanwhile stays
                        open(f"{part}-Frag{i + 2}", "wb").write(data(0, 1000))
                    if skip:
                        self.log(f"[download] Skipping fragment {i + 1} ...")  # a hole in the file, and success
                        continue
                    if q["refuse"] == "limit":
                        self.fail("ERROR: [download] Got error: HTTP Error 429: Too Many Requests")
                    self.fail(f"ERROR: fragment {i + 1} not found, unable to continue")
                with open(frag, "rb") as piece:
                    out.write(piece.read())
                    out.flush()
                os.remove(frag)
                if info.get("_stop") == i + 1:
                    os._exit(9)  # killed between appending the piece and counting it
                json.dump({"downloader": {"current_fragment": {"index": i + 1}}}, open(ytdl, "w"))
        os.remove(ytdl)
        os.replace(part, name)
        size = os.path.getsize(name)
        self.hook(info_dict=about, status="finished", downloaded_bytes=size, total_bytes=size, filename=name)
        return name

    def hook(self, **d):
        for h in self.params.get("progress_hooks", []):
            h(d)
