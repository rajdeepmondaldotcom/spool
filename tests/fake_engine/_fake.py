"""A fake engine that plays scripted scenarios chosen by the link, with no network.

Links look like https://fake.test/<scenario>[/<n>][?delay=<seconds per step>].
It implements only what spool's worker uses: YoutubeDL(params) as a context manager,
extract_info, download, add_post_processor, progress hooks and the logger.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


class Named:
    """An engine post-processor the fake only names (FFmpegMetadata, FFmpegExtractAudio)."""

    def __init__(self, key):
        self.key = key

    def run(self, information):
        return [], information


class DownloadError(Exception):
    pass


class PostProcessor:
    def __init__(self, downloader=None):
        self._downloader = downloader

    def run(self, information):
        return [], information


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
        return video(url, n), delay if delay is not None else 0.15
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
    if name == "unavailable":
        raise DownloadError(f"ERROR: [fake] {n}: Video unavailable. This video has been removed by the uploader")
    raise DownloadError(f"ERROR: Unsupported URL: {url}")


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
            self.fail(
                f"ERROR: [youtube] {info['id']}: Requested format is not available. Use --list-formats for a list of available formats"
            )

    def download(self, urls):
        for url in urls:
            try:
                info, delay = self.scenario(url)
            except DownloadError as exc:
                self.fail(str(exc))
            self.needs_js(info)
            if info.get("_type") != "playlist":
                self.download_one(info, delay)
                continue
            wanted = self.params.get("playlist_items")  # without it, an engine downloads every entry
            for i, e in enumerate(info["entries"], 1):
                if wanted is None or str(i) == wanted:
                    self.download_one({**video(url, str(i), e["title"]), "id": e["id"]}, delay)
        return 0

    def name(self, info, ext):
        """The engines' template: %(title)s, %(title).Ns (characters) or %(title).NB (bytes, yt-dlp only)."""
        title = info["title"].replace("/", "_")
        tmpl = self.params["outtmpl"]
        cut = re.search(r"%\(title\)\.(\d+)([sB])", tmpl)
        if cut and cut.group(2) == "B":
            title = title.encode()[: int(cut.group(1))].decode(errors="ignore")
        elif cut:
            title = title[: int(cut.group(1))]
        tmpl = re.sub(r"%\(title\)(\.\d+)?[sB]", lambda m: title.replace("%", "%%"), tmpl)
        name = tmpl % {"id": info["id"].replace("/", "_"), "ext": ext}  # the engines clean ids for file names too
        if len(os.path.basename(name + ".part").encode()) > 255:
            raise OSError(63, "File name too long", name)
        return name

    def remove_test_folder(self, path):
        """Delete the one folder a test named, and only inside the system temp folder (never "here")."""
        path, temp = os.path.realpath(path), os.path.realpath(tempfile.gettempdir())
        if os.path.commonpath([path, temp]) != temp or len(path) <= len(temp) + 1:
            raise RuntimeError(f"refusing to delete {path}: not inside {temp}")
        shutil.rmtree(path)

    def open(self, name, mode):
        folder = os.path.dirname(name)
        if folder:
            os.makedirs(folder, exist_ok=True)  # as the engines do: a full path recreates a deleted folder
        return open(name, mode)

    def download_one(self, info, delay):
        refused = self.params.get("match_filter") and self.params["match_filter"](info, incomplete=False)
        if refused:
            self.log(f"[download] {refused}, skipping ..")  # the engines skip quietly; no error
            return
        if info.get("_rm"):
            self.remove_test_folder(info["_rm"])
        fmt = self.params.get("format", "best")
        mp3 = any(p.get("key") == "FFmpegExtractAudio" for p in self.params.get("postprocessors", []))
        formats = {f["format_id"]: f for f in info["formats"]}
        if fmt.startswith("bestaudio") or "137" not in formats:
            parts = [formats["140"] if "140" in formats else info["formats"][0]]
        elif "+" in fmt:
            parts = [formats["137"], formats["140"]]
        else:
            parts = [formats["18"]]
        final_ext = "mp3" if mp3 else ("mp4" if len(parts) == 2 else parts[0]["ext"])
        final = self.name(info, final_ext)
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
                self.log(f'[Merger] Merging formats into "{final}"')
                time.sleep(delay)
                with self.open(final, "wb") as out:
                    for f in files:
                        with open(f, "rb") as src:
                            out.write(src.read())
                        os.remove(f)
            elif mp3:  # ffmpeg writes the .mp3 as it goes, so a cut leaves a short file that looks finished
                self.log(f"[ExtractAudio] Destination: {final}")
                with self.open(final, "wb") as out:
                    out.write(b"\0" * 1000)
                    out.flush()
                    time.sleep(delay * 10)
                    out.write(b"\0" * 1000)
                os.remove(files[0])
            else:
                os.replace(files[0], final)
        order = [pp for _, pp in sorted(self.pps, key=lambda x: x[0])]
        self.log("[fake] ran: " + ", ".join(getattr(pp, "key", type(pp).__name__) for pp in order))
        if self.params.get("updatetime", True) and info.get("timestamp"):
            os.utime(final, (info["timestamp"], info["timestamp"]))
        for pp in order:
            pp.run({**info, "filepath": final})

    def fetch(self, info, f, delay, parts):
        split = len(parts) > 1
        name = self.name(info, f"f{f['format_id']}.{f['ext']}" if split else f["ext"])
        about = {**info, **f, "requested_formats": parts} if split else {**info, **f}  # as yt-dlp's hooks carry
        part = name + ".part"
        total = f["filesize"]
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
        self.hook(status="finished", downloaded_bytes=total, total_bytes=total, filename=name)
        return name

    def hook(self, **d):
        for h in self.params.get("progress_hooks", []):
            h(d)
