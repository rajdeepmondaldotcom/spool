"""The contract with the real engines, over the internet. Run with: uv run pytest -m network"""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from spool.engines import Engines
from spool.jobs import Jobs
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
engine = __import__(sys.argv[2])
formats, params = json.load(sys.stdin)
info = {"id": "x", "title": "x", "formats": formats, "extractor": "test", "extractor_key": "Test",
        "webpage_url": "https://x.test/"}
with engine.YoutubeDL({"quiet": True, "simulate": True, **params}) as ydl:
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


def pick(jobs, engine, module, formats, preset):
    spec = json.dumps([formats, format_params(preset, True, module)])
    out = subprocess.run(
        [sys.executable, "-I", "-c", PICK, jobs.engines.path(engine), module],
        input=spec,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out.splitlines()[-1])


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
