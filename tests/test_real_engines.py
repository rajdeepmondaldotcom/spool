"""The contract with the real engines, over the internet. Run with: uv run pytest -m network"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from spool.engines import Engines
from spool.jobs import WORKER, Jobs
from spool.worker import format_params

pytestmark = pytest.mark.network
ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 seconds


@pytest.fixture(scope="module")
def jobs(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    engines = Engines(data / "engines")
    engines.start()
    end = time.time() + 180
    while any(s["busy"] for s in engines.status().values()):
        assert time.time() < end
        time.sleep(0.5)
    status = engines.status()
    assert all(s["installed"] and s["error"] is None for s in status.values()), status
    jobs = Jobs(data, engines)
    (data / "out").mkdir()
    jobs.update_settings({"dest": str(data / "out")})
    yield jobs
    jobs.close()


def finish(jobs, job_id, timeout=180):
    end = time.time() + timeout
    while True:
        job = next(j for j in jobs.snapshot()["jobs"] if j["id"] == job_id)
        if job["state"] in ("done", "failed"):
            return job
        assert time.time() < end, job
        time.sleep(0.5)


def test_engines_are_installed_at_the_latest_version(jobs):
    for status in jobs.engines.status().values():
        assert status["version"] == status["latest"]


def test_yt_dlp_probe_reads_real_heights_and_sizes(jobs):
    probe = jobs.probe(ZOO, "yt-dlp")
    [entry] = probe["entries"]
    assert (entry["id"], entry["title"], entry["duration"]) == ("jNQXAC9IVRw", "Me at the zoo", 19)
    assert [(p["id"], p["detail"]) for p in entry["presets"]] == [
        ("best", "240p"),  # its top class, so 240 is not offered again
        ("audio", None),
        ("mp3", None),
    ]
    assert all(p["size"] for p in entry["presets"])


@pytest.mark.parametrize("engine, preset", [("yt-dlp", "best"), ("yt-dlp", "audio"), ("youtube-dl", "best")])
def test_a_real_download_lands_with_its_final_path(jobs, engine, preset):
    [job], _ = jobs.add([{"url": ZOO}], preset, engine)
    done = finish(jobs, job["id"])
    assert done["state"] == "done", (done["error"], jobs.log(job["id"])[-2000:])
    assert done["title"] == "Me at the zoo"  # filled in from the engine
    assert done["size"] > 100_000
    assert done["file_exists"]
    if shutil.which("ffprobe"):  # chapters and details were embedded
        tags = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=title", "-of", "json", done["path"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert json.loads(tags)["format"]["tags"]["title"] == "Me at the zoo"
    jobs.remove(job["id"])
    os.remove(done["path"])  # the next engine must really download, not find the file


def test_a_real_playlist_is_read_flat(jobs):
    probe = jobs.probe("https://www.youtube.com/playlist?list=PLzMcBGfZo4-mP7qA9cagf68V06sko5otr", "yt-dlp")
    assert probe["kind"] == "playlist"
    assert len(probe["entries"]) > 1
    assert all(e["url"].startswith("https://") for e in probe["entries"])


PICK = """
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[3])
engine = __import__(sys.argv[2])
import worker
formats, params, preset = json.load(sys.stdin)
info = {"id": "x", "title": "x", "formats": formats, "extractor": "test", "extractor_key": "Test",
        "webpage_url": "https://x.test/"}
with engine.YoutubeDL({"quiet": True, "simulate": True, **params}) as ydl:
    if preset == "compatible" and sys.argv[2] == "yt_dlp":  # the worker's own step before the engine chooses
        from yt_dlp.postprocessor.common import PostProcessor
        class Narrow(PostProcessor):
            def run(self, information):
                worker.narrow(ydl, information, True)
                return [], information
        ydl.add_post_processor(Narrow(), when="pre_process")
    chosen = ydl.process_video_result(info, download=False)  # the engine's own sorting and selection
print(json.dumps("+".join(f["format_id"] for f in chosen.get("requested_formats") or [chosen])))
"""


def fmt(fid, height, width=None, vcodec="avc1.640028", acodec="none", ext="mp4", **extra):
    f = {
        "format_id": fid,
        "format": fid,
        "url": f"https://x.test/{fid}",
        "protocol": "https",
        "ext": ext,
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        **extra,
    }
    return {**f, "width": width} if width else f


AUDIO = fmt("a140", None, vcodec="none", acodec="mp4a.40.2", ext="m4a", abr=129)
OPUS = fmt("a251", None, vcodec="none", acodec="opus", ext="webm", abr=135)
VP9 = {"vcodec": "vp9", "ext": "webm"}
LANDSCAPE = [fmt("v360", 360, 640), fmt("v1080", 1080, 1920), fmt("v1440", 1440, 2560, **VP9), AUDIO, OPUS]
VERTICAL = [fmt("v720", 1280, 720), fmt("v1080", 1920, 1080), fmt("v1440", 2560, 1440, **VP9), AUDIO, OPUS]
NO_WIDTHS = [fmt("v720", 720), fmt("v1080", 1080), fmt("v1440", 1440, **VP9), AUDIO, OPUS]
FOUR_THREE = [fmt("v240", 240, 320), fmt("v360", 360, 480), fmt("v480", 480, 640), AUDIO, OPUS]
VP9_TOP = [fmt("v720", 720, 1280), fmt("v1080", 1080, 1920, **VP9), AUDIO, OPUS]  # 1080 exists only in VP9
TIE = [fmt("v1080vp9", 1080, 1920, **VP9), fmt("v1080", 1080, 1920), AUDIO, OPUS]  # H.264 breaks the tie
WITH_SOUND = [fmt("v360", 360, 640, acodec="mp4a.40.2"), AUDIO]  # the top stream has its own sound


def pick(jobs, engine, module, formats, preset):
    spec = json.dumps([formats, format_params(preset, True, module), preset])
    out = subprocess.run(
        [sys.executable, "-I", "-c", PICK, jobs.engines.path(engine), module, str(WORKER.parent)],
        input=spec,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out.splitlines()[-1])


# Streams shaped like the ones that fooled the size estimate on a real video.
A140 = {"vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "abr": 129}
NO_SIZE = [  # a stream with no size loses to one with a size, whatever its bitrate; then the site's quality decides
    fmt("hls720", 720, 1280, tbr=1564, protocol="m3u8_native"),
    fmt("v720", 720, 1280, tbr=618, filesize=11_586_906),
    fmt("a140-drc", None, **A140, filesize=2_430_993, quality=2.5),
    fmt("a140", None, **A140, filesize=2_430_774, quality=3),
]
NEWER = [  # with no H.264 at the top, the codec nearest above it wins: VP9, then VP9 HDR, then AV1
    fmt("av1", 2160, 3840, vcodec="av01.0.12M.08", filesize=9_000_000),
    fmt("vp9hdr", 2160, 3840, vcodec="vp09.02.51.10", ext="webm", filesize=8_000_000, dynamic_range="HDR10"),
    fmt("vp9", 2160, 3840, **VP9, filesize=5_000_000),
    fmt("v1080", 1080, 1920, filesize=1_000_000),
    AUDIO,
]
OLD_AND_NEW = [fmt("vp8", 720, 1280, vcodec="vp8", ext="webm"), fmt("vp9", 720, 1280, **VP9, filesize=9_000_000), AUDIO]
TALL = [  # H.264 above 1080: plays everywhere takes the largest, and VP9 the top
    fmt("v2160", 2160, 3840, **VP9, filesize=9_000_000),
    fmt("v1440", 1440, 2560, filesize=6_000_000),
    fmt("v1080", 1080, 1920, filesize=3_000_000),
    AUDIO,
    OPUS,
]
NO_H264 = [  # nothing plays everywhere: what class 1080 saves
    fmt("v1440", 1440, 2560, **VP9, filesize=6_000_000),
    fmt("v1080", 1080, 1920, **VP9, filesize=3_000_000),
    fmt("v720", 720, 1280, vcodec="av01.0.08M.08", filesize=1_000_000),
    OPUS,
]
SOUND_ONLY_IN_OPUS = [fmt("v1080", 1080, 1920, filesize=3_000_000), OPUS]  # H.264 with no AAC to join
H264_IN_WEBM = [fmt("v1080", 1080, 1920, ext="webm"), fmt("v720", 720, 1280, filesize=1), AUDIO]  # not for MP4
ONE_STREAM = [fmt("v360", 360, 640, acodec="mp4a.40.2"), fmt("v1080", 1080, 1920, **VP9), OPUS]  # both in one
ORIGINAL = [  # the original language beats a dubbed track, before quality or size
    fmt("v720", 720, 1280),
    fmt("dub", None, **A140, filesize=3_000_000, language_preference=-1),
    fmt("orig", None, **A140, filesize=2_000_000, language_preference=10),
]


@pytest.mark.parametrize(
    "formats",
    [NO_SIZE, NEWER, OLD_AND_NEW, ORIGINAL, LANDSCAPE, VERTICAL, VP9_TOP, TIE, WITH_SOUND]
    + [TALL, NO_H264, SOUND_ONLY_IN_OPUS, H264_IN_WEBM, ONE_STREAM],
)
@pytest.mark.parametrize("preset", ["best", "compatible", "1080", "720"])
def test_the_size_estimate_counts_the_streams_the_engine_picks(jobs, formats, preset):
    from spool.worker import picked

    ours = picked(formats, preset, True)
    assert "+".join(f["format_id"] for f in ours) == pick(jobs, "yt-dlp", "yt_dlp", formats, preset)


@pytest.mark.parametrize(
    "formats, preset, chosen",
    [
        (LANDSCAPE, "1080", "v1080+a140"),
        (LANDSCAPE, "720", "v360+a140"),
        (LANDSCAPE, "best", "v1440+a140"),
        (VERTICAL, "1080", "v1080+a140"),
        (VERTICAL, "720", "v720+a140"),
        (VERTICAL, "best", "v1440+a140"),
        (NO_WIDTHS, "1080", "v1080+a140"),
        (FOUR_THREE, "360", "v360+a140"),
        (FOUR_THREE, "best", "v480+a140"),
        (VP9_TOP, "best", "v1080+a140"),
        (VP9_TOP, "1080", "v1080+a140"),
        (TIE, "best", "v1080+a140"),
        (WITH_SOUND, "best", "v360"),  # no second sound, so the size estimate counts one stream
        (TALL, "compatible", "v1440+a140"),  # the largest H.264 with AAC
        (VP9_TOP, "compatible", "v720+a140"),
        (NO_H264, "compatible", "v1080+a251"),  # none: what class 1080 saves
        (SOUND_ONLY_IN_OPUS, "compatible", "v1080+a251"),
        (ONE_STREAM, "compatible", "v360"),
    ],
)
def test_yt_dlp_picks_the_class_by_its_short_side_with_h264_only_as_a_tie_break(jobs, formats, preset, chosen):
    assert pick(jobs, "yt-dlp", "yt_dlp", formats, preset) == chosen


@pytest.mark.parametrize(
    "formats, preset, chosen",
    [  # youtube-dl relies on the site's order (best last), as its extractors give it
        (LANDSCAPE, "1080", "v1080+a140"),
        (VERTICAL, "1080", "v1080+a140"),
        (NO_WIDTHS, "1080", "v1080+a140"),
        (VERTICAL, "720", "v720+a140"),
        (VERTICAL, "best", "v1440+a140"),
        (LANDSCAPE, "best", "v1440+a140"),
        (VP9_TOP, "1080", "v1080+a251"),  # the top class in VP9, with the site's best audio
    ],
)
def test_youtube_dl_ladders_pick_the_class(jobs, formats, preset, chosen):
    assert pick(jobs, "youtube-dl", "youtube_dl", formats, preset) == chosen


def test_spool_fetches_a_working_deno_from_its_official_release(tmp_path):
    from spool.engines import JsRuntime

    helper = JsRuntime(tmp_path, which=lambda name: None)  # as if the machine had no runtime
    helper.start()
    end = time.time() + 300
    while helper.tools()["js_setup"] == "installing":
        assert time.time() < end
        time.sleep(0.5)
    tools = helper.tools()
    assert (tools["js_runtime"], tools["js_setup"]) == ("deno", None), tools
    out = subprocess.run([tools["js_path"], "--version"], capture_output=True, text=True, check=True).stdout
    assert out.startswith("deno ")


# The worker against the real engine


IN_PIECES = (
    "import sys; sys.path.insert(0, sys.argv[1]); import worker; worker.AT_ONCE = int(sys.argv[2]); worker.run()"
)


def worker_cmd(at_once=None):
    """The worker, as Spool ships it, or fetching YouTube streams in at_once pieces at once (shipped off)."""
    if at_once:
        return [sys.executable, "-I", "-c", IN_PIECES, str(WORKER.parent), str(at_once)]
    return [sys.executable, "-I", str(WORKER)]


def worker(jobs, dest, url, preset="best", at_once=None):
    spec = jobs._spec("yt-dlp", mode="download", url=url, dest=str(dest), preset=preset, item=None)
    out = subprocess.run(
        worker_cmd(at_once), input=json.dumps(spec), capture_output=True, text=True, timeout=300
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_a_real_two_part_download_rises_across_both_parts(jobs, tmp_path):
    events = worker(jobs, tmp_path, ZOO)
    assert events[-1]["event"] == "done", events[-1]
    progress = [(e["part"], e["progress"]) for e in events if e["event"] == "progress" and e["progress"] is not None]
    values = [p for _, p in progress]
    assert values == sorted(values)
    if {part for part, _ in progress} == {1, 2}:  # with ffmpeg: video, then audio
        assert max(p for part, p in progress if part == 1) < 0.99
        assert any(e["path"].endswith(".temp.mp4") for e in events if e["event"] == "file")  # the merge's file


def test_a_real_channel_link_fails_as_a_list_and_saves_nothing(jobs, tmp_path):
    events = worker(jobs, tmp_path, "https://www.youtube.com/@jawed")
    assert (events[-1]["event"], events[-1].get("kind")) == ("error", "playlist"), events[-1]
    assert list(tmp_path.iterdir()) == []


FILM = "https://www.youtube.com/watch?v=SkVqJ1SGeL0"  # a short open-licence film, with no chapters of its own
CHAPTERS = """
import runpy, sys
sys.path.insert(0, sys.argv[1])
import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

class Chapters(PostProcessor):
    def run(self, info):  # before the streams are chosen, as an extractor gives them
        half = info["duration"] / 2
        info["chapters"] = [
            {"start_time": 0, "end_time": half, "title": "One"},
            {"start_time": half, "end_time": info["duration"], "title": "Two"},
        ]
        return [], info

init = yt_dlp.YoutubeDL.__init__
def with_chapters(self, *args, **kwargs):
    init(self, *args, **kwargs)
    self.add_post_processor(Chapters(), when="pre_process")
yt_dlp.YoutubeDL.__init__ = with_chapters
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="joins need ffmpeg")
def test_a_real_join_writes_the_big_file_once_with_chapters_and_details(jobs, tmp_path):
    spec = jobs._spec("yt-dlp", mode="download", url=FILM, dest=str(tmp_path), preset="240", item=None)
    out = subprocess.run(
        [sys.executable, "-I", "-c", CHAPTERS, jobs.engines.path("yt-dlp"), str(WORKER)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=300,
    ).stdout
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert events[-1]["event"] == "done", events[-1]
    name = os.path.basename(events[-1]["path"])
    logs = [e["line"] for e in events if e["event"] == "log"]
    assert any(line.startswith("[Merger] Merging formats into") and name in line for line in logs), logs
    tagged = [line for line in logs if line.startswith("[Metadata] Adding metadata to")]
    assert len(tagged) == 1 and name not in tagged[0], tagged  # only the small audio part, never the joined file
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_entries", "format_tags", "-of", "json", name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    probe = json.loads(probe)
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["One", "Two"]
    tags = {k.lower(): v for k, v in probe["format"]["tags"].items()}
    assert tags["title"] and tags["artist"] and tags["date"], tags
    steps = [e for e in events if e["event"] == "progress" and e["status"] == "processing"]
    assert [e["what"] for e in steps][:1] == ["tags"] and "join" in [e["what"] for e in steps]  # the engine's hooks
    parts = sum(e["total"] for e in events if e["event"] == "progress" and e["status"] == "finished")
    [join] = {e["total"] for e in steps if e["what"] == "join"}
    assert 0 <= join - parts < 64_000  # the join's expected size: its parts, the audio part with its new tags


# YouTube in pieces, file types and channels, on the real engine

FILM_ID = "SkVqJ1SGeL0"
FILM_720 = "Caminandes 3： Llamigos [SkVqJ1SGeL0] 720p"  # Spool's name for it: yt-dlp widens the colon
PICKS = """
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[3])
import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
import worker
presets = json.load(sys.stdin)
with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
    info = ydl.extract_info(sys.argv[2], download=False, process=False)
picks = {}
for preset, params in presets.items():  # the engine's own choice for each preset, as a download makes it
    with yt_dlp.YoutubeDL({"quiet": True, "simulate": True, **params}) as ydl:
        class Narrow(PostProcessor):  # the worker's own step before the engine chooses
            def run(self, information):
                worker.narrow(ydl, information, True)
                return [], information
        if preset == "compatible":
            ydl.add_post_processor(Narrow(), when="pre_process")
        chosen = ydl.process_ie_result(dict(info, formats=[dict(f) for f in info["formats"]]), download=False)
    picks[preset] = "+".join(f["format_id"] for f in chosen.get("requested_formats") or [chosen])
print(json.dumps({"formats": info["formats"], "picks": picks}, default=str))
"""
FILM_PRESETS = ["best", "compatible", "1080", "720", "480", "360", "240"]


@pytest.fixture(scope="module")
def film(jobs):
    presets = {p: format_params(p, True, "yt_dlp") for p in FILM_PRESETS}
    out = subprocess.run(
        [sys.executable, "-I", "-c", PICKS, jobs.engines.path("yt-dlp"), FILM, str(WORKER.parent)],
        input=json.dumps(presets),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    return json.loads(out.splitlines()[-1])


@pytest.mark.parametrize("preset", FILM_PRESETS)
def test_on_a_real_video_spool_counts_the_streams_the_engine_picks_for_every_preset(film, preset):
    from spool.worker import picked

    ours = picked(film["formats"], preset, True)
    assert "+".join(f["format_id"] for f in ours) == film["picks"][preset]


def logs(events, start):
    return [e["line"] for e in events if e["event"] == "log" and e["line"].startswith(start)]


def tags(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k.lower(): v for k, v in json.loads(out)["format"]["tags"].items()}


def plays(path):
    return subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"]).returncode == 0


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


@pytest.fixture(scope="module")
def film_720(jobs, tmp_path_factory):
    """The film at 720p, fetched in pieces once: its events."""
    events = worker(jobs, tmp_path_factory.mktemp("film"), FILM, "720", at_once=4)
    assert events[-1]["event"] == "done", events[-1]
    return events


@needs_ffmpeg
def test_pieces_a_real_youtube_download_comes_in_pieces_with_the_streams_of_today(film, film_720):
    assert logs(film_720, f"[info] {FILM_ID}: Downloading 1 format(s): ") == [
        f"[info] {FILM_ID}: Downloading 1 format(s): {film['picks']['720']}"
    ]
    assert len(logs(film_720, "[dashsegments] Total fragments: ")) == 2  # the video, then the audio
    path = film_720[-1]["path"]
    assert os.path.basename(path) == FILM_720 + ".mp4"
    assert plays(path)
    found = tags(path)
    assert found["title"] == "Caminandes 3: Llamigos" and found["artist"] and found["date"], found
    progress = [e["progress"] for e in film_720 if e["event"] == "progress" and e["progress"] is not None]
    assert progress == sorted(progress) and progress[-1] == 1.0


def look(jobs, url):
    """The worker's lookup event for a link."""
    spec = jobs._spec("yt-dlp", mode="probe", url=url)
    out = subprocess.run(
        [sys.executable, "-I", str(WORKER)], input=json.dumps(spec), capture_output=True, text=True, timeout=120
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")][-1]


@pytest.fixture(scope="module")
def film_types(jobs):
    return {p["id"]: p["ext"] for p in look(jobs, FILM)["entries"][0]["presets"]}


@pytest.mark.parametrize("preset", ["720", "audio", "mp3"])
def test_each_quality_saves_the_file_type_the_lookup_named(jobs, tmp_path, film_types, preset):
    assert (film_types["best"], film_types["audio"], film_types["mp3"]) == ("mp4", "m4a", "mp3"), film_types
    events = worker(jobs, tmp_path, FILM, preset)
    assert events[-1]["event"] == "done", events[-1]
    assert events[-1]["path"].endswith("." + film_types[preset])


@needs_ffmpeg
def test_pieces_a_real_download_killed_half_way_resumes_in_pieces(jobs, tmp_path, film_720):
    spec = jobs._spec("yt-dlp", mode="download", url=FILM, dest=str(tmp_path), preset="720", item=None)
    child = subprocess.Popen(worker_cmd(4), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    child.stdin.write(json.dumps(spec))
    child.stdin.close()
    for line in child.stdout:
        e = json.loads(line)
        if e["event"] == "progress" and (e["progress"] or 0) >= 0.3:
            break
    child.kill()  # our own child
    child.wait()
    assert any(p.name.endswith(".ytdl") for p in tmp_path.iterdir())  # the pieces' own record of where they got
    events = worker(jobs, tmp_path, FILM, "720", at_once=4)
    assert events[-1]["event"] == "done", events[-1]
    assert abs(events[-1]["size"] - film_720[-1]["size"]) < 4096  # the same file, give or take its date tags
    assert plays(events[-1]["path"])


ENGINE_PIECES = """
import sys
sys.path.insert(0, sys.argv[1])
import yt_dlp
params = {"quiet": True, "format": "136", "outtmpl": sys.argv[3]}
if sys.argv[4] == "pieces":
    params["extractor_args"] = {"youtube": {"formats": ["dashy"], "skip": ["hls"]}}
with yt_dlp.YoutubeDL(params) as ydl:
    sys.exit(ydl.download([sys.argv[2]]))
"""


def engine_fetch(jobs, out, mode):
    subprocess.run(
        [sys.executable, "-I", "-c", ENGINE_PIECES, jobs.engines.path("yt-dlp"), FILM, str(out), mode],
        check=True,
        timeout=300,
    )
    return out.read_bytes()


@needs_ffmpeg
def test_pieces_a_part_left_by_one_connection_is_never_resumed_in_pieces(jobs, tmp_path, film_720):
    whole = engine_fetch(jobs, tmp_path / "whole.mp4", "whole")  # the 720p video stream, one connection
    # The danger: the engine's own pieces mode appends to such a .part from byte 0 and calls it a success
    (tmp_path / "cut.mp4.part").write_bytes(whole[:3_000_000])
    joined = engine_fetch(jobs, tmp_path / "cut.mp4", "pieces")
    assert len(joined) == 3_000_000 + len(whole) and joined != whole
    # Spool finishes it the old way instead
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / f"{FILM_720}.f136.mp4.part").write_bytes(whole[:3_000_000])  # no .ytdl beside it
    events = worker(jobs, dest, FILM, "720", at_once=4)
    assert events[-1]["event"] == "done", events[-1]
    assert len(logs(events, "[dashsegments] Total fragments: ")) == 1  # only the audio came in pieces
    assert abs(events[-1]["size"] - film_720[-1]["size"]) < 4096
    assert plays(events[-1]["path"])


def test_a_real_channel_lists_its_newest_100_uploads(jobs):
    start = time.monotonic()
    probe = look(jobs, "https://www.youtube.com/@BlenderOfficial")
    assert time.monotonic() - start < 30
    assert (probe["kind"], probe["title"], probe["truncated"]) == ("playlist", "Blender", True), probe
    entries = probe["entries"]
    assert len(entries) == 100 and len({e["id"] for e in entries}) == 100
    assert all(e["url"] == f"https://www.youtube.com/watch?v={e['id']}" and e["item"] is None for e in entries)
    assert all(e["title"] and e["duration"] for e in entries)


STALL_SIZE = 25 * 1024 * 1024 + 12345  # three pieces
STALL_BODY = bytes(i % 251 for i in range(STALL_SIZE))
STALL_WORKER = """
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import types
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
import worker
worker.AT_ONCE = 4  # pieces, which Spool ships off

reads = []

class YoutubeIE(InfoExtractor):  # a stand-in for YouTube's page: one stream with its size, from the local server
    _VALID_URL = r"http://127\\.0\\.0\\.1:\\d+/watch"

    def _real_extract(self, url):
        reads.append(1)
        stream = {"format_id": "140", "url": f"{url.rsplit('/', 1)[0]}/stream?read={len(reads)}", "ext": "m4a",
                  "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https", "filesize": int(sys.argv[3])}
        return {"id": "abc", "title": "T", "formats": [stream]}

class YDL(yt_dlp.YoutubeDL):
    def __init__(self, params=None):
        super().__init__(params, auto_init=False)
        self.add_info_extractor(YoutubeIE())

spec = json.loads(sys.argv[4])
params = {"logger": worker.Log(), "noprogress": True, "socket_timeout": 30}
worker.download(types.SimpleNamespace(YoutubeDL=YDL), "yt_dlp", params, spec)
"""


@pytest.fixture
def stalling_site():
    """A local stand-in for YouTube's stream host: on the first read of the video, the second piece comes at
    1 KB/s, as one piece did on a real run that stalled for six minutes. Only 127.0.0.1 is contacted."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    reads = []

    class Site(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            reads.append(q.get("read"))
            a, b = map(int, q["range"].split("-")) if "range" in q else (0, STALL_SIZE - 1)  # or the whole stream
            body = STALL_BODY[a : b + 1]
            start = int(self.headers["Range"].split("=")[1].split("-")[0]) if self.headers.get("Range") else 0
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            slow = q.get("read") == "1" and a == 10 * 1024 * 1024
            try:
                for i in range(start, len(body), 1024 if slow else 1 << 20):
                    self.wfile.write(body[i : i + (1024 if slow else 1 << 20)])
                    time.sleep(1 if slow else 0)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", reads
    server.shutdown()


def test_pieces_a_stalled_piece_makes_spool_read_the_video_again_and_finish(jobs, tmp_path, stalling_site):
    base, reads = stalling_site
    spec = {"url": f"{base}/watch", "dest": str(tmp_path), "preset": "audio", "ffmpeg": None}
    try:
        out = subprocess.run(
            [sys.executable, "-I", "-c", STALL_WORKER, jobs.engines.path("yt-dlp"), str(WORKER.parent), str(STALL_SIZE)]
            + [json.dumps(spec)],
            capture_output=True,
            text=True,
            timeout=90,  # without the guard the piece takes about three hours
        ).stdout
    except subprocess.TimeoutExpired:
        pytest.fail("the stalled piece held the download")
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert events[-1]["event"] == "done", events[-1]
    assert sorted(set(reads) - {None}) == ["1", "2"]  # read once more, not again and again
    assert open(events[-1]["path"], "rb").read() == STALL_BODY


# Signing in: the real engine reads a made-up Firefox store in the test's own home, never a browser's

HOMES = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")  # where the engine looks
NOWHERE = "http://127.0.0.1:9/video"  # the sign-in is read before any request; this one goes nowhere


def firefox_store(home):
    """A Firefox profile with one cookie, where the engine looks for Firefox on this system, under home."""
    import sqlite3

    root = {"darwin": "Library/Application Support/Firefox/Profiles", "win32": "Mozilla/Firefox/Profiles"}
    profile = home / root.get(sys.platform, "mozilla/firefox") / "test.default"
    profile.mkdir(parents=True)
    db = sqlite3.connect(profile / "cookies.sqlite")
    db.execute("CREATE TABLE moz_cookies (host, name, value, path, expiry, isSecure, originAttributes)")
    db.execute("INSERT INTO moz_cookies VALUES ('.x.test', 'session', 'made-up', '/', 4102444800000, 1, '')")
    db.execute("PRAGMA user_version = 17")
    db.commit()
    db.close()
    return profile / "cookies.sqlite"


@pytest.mark.parametrize("problem", [None, "missing", "no rights"])
def test_the_real_engine_reads_a_made_up_firefox_sign_in_and_names_what_it_cannot_read(jobs, tmp_path, problem):
    if problem == "no rights" and (sys.platform == "win32" or os.geteuid() == 0):
        pytest.skip("a file's read right cannot be taken away here")
    if problem != "missing":
        db = firefox_store(tmp_path)
        if problem:
            db.chmod(0)
    spec = jobs._spec("yt-dlp", mode="probe", url=NOWHERE, signin="firefox")
    env = {**os.environ, **dict.fromkeys(HOMES, str(tmp_path))}
    out = subprocess.run(
        [sys.executable, "-I", str(WORKER)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    ).stdout
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    read = [e["line"] for e in events if e["event"] == "log" and e["line"].startswith("Extracted ")]
    if problem:
        assert (events[-1]["event"], events[-1].get("kind")) == ("error", "signin_unread"), events[-1]
        assert read == []
    else:
        assert read == ["Extracted 1 cookies from firefox"]
        assert events[-1]["event"] == "error" and events[-1].get("kind") != "signin_unread", events[-1]
