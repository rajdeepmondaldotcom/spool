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
from spool.worker import selector

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
    ids = [p["id"] for p in entry["presets"]]
    assert ids[0] == "best" and "240" in ids and ids[-2:] == ["audio", "mp3"]
    assert next(p for p in entry["presets"] if p["id"] == "best")["size"]


@pytest.mark.parametrize("engine, preset", [("yt-dlp", "best"), ("yt-dlp", "audio"), ("youtube-dl", "best")])
def test_a_real_download_lands_with_its_final_path(jobs, engine, preset):
    [job] = jobs.add([{"url": ZOO}], preset, engine)
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
formats, spec = json.load(sys.stdin)
with engine.YoutubeDL({"quiet": True}) as ydl:
    ctx = {"formats": formats, "incomplete_formats": False, "has_merged_format": False}
    print(json.dumps([f["format_id"] for f in ydl.build_format_selector(spec)(ctx)]))
"""


def fmt(fid, height, width=None, vcodec="avc1.640028", acodec="none", ext="mp4"):
    f = {
        "format_id": fid,
        "format": fid,
        "url": f"https://x.test/{fid}",
        "protocol": "https",
        "ext": ext,
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
    }
    return {**f, "width": width} if width else f


AUDIO = fmt("a140", None, vcodec="none", acodec="mp4a.40.2", ext="m4a")
LANDSCAPE = [fmt("v360", 360, 640), fmt("v1080", 1080, 1920), fmt("v1440", 1440, 2560, "vp9", ext="webm"), AUDIO]
VERTICAL = [fmt("v720", 1280, 720), fmt("v1080", 1920, 1080), fmt("v1440", 2560, 1440, "vp9", ext="webm"), AUDIO]
NO_WIDTHS = [fmt("v720", 720), fmt("v1080", 1080), fmt("v1440", 1440, vcodec="vp9", ext="webm"), AUDIO]


@pytest.mark.parametrize("engine, module", [("yt-dlp", "yt_dlp"), ("youtube-dl", "youtube_dl")])
@pytest.mark.parametrize(
    "formats, preset, chosen",
    [
        (LANDSCAPE, "1080", "v1080+a140"),
        (VERTICAL, "1080", "v1080+a140"),
        (NO_WIDTHS, "1080", "v1080+a140"),
        (VERTICAL, "720", "v720+a140"),
        (VERTICAL, "best", "v1440+a140"),
        (LANDSCAPE, "best", "v1440+a140"),
    ],
)
def test_the_real_engines_pick_the_class_in_either_orientation(jobs, engine, module, formats, preset, chosen):
    spec = json.dumps([formats, selector(preset, True)])
    out = subprocess.run(
        [sys.executable, "-I", "-c", PICK, jobs.engines.path(engine), module],
        input=spec,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out.splitlines()[-1]) == [chosen]
