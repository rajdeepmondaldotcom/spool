import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

import spool

WORKER = Path(spool.__file__).with_name("worker.py")
FAKE = Path(__file__).with_name("fake_engine")


def run(mode, url, engine="yt-dlp", **spec):
    spec = {"engine": engine, "engine_path": str(FAKE), "mode": mode, "url": url, "ffmpeg": True, **spec}
    out = subprocess.run(
        [sys.executable, "-I", str(WORKER)], input=json.dumps(spec), capture_output=True, text=True, timeout=30
    ).stdout
    return [json.loads(line) for line in out.splitlines()]


def last(events):
    return [e for e in events if e["event"] != "log" and e["event"] != "progress"][-1]


def test_probe_reads_one_video():
    info = last(run("probe", "https://fake.test/video"))
    assert info["event"] == "info"
    assert info["kind"] == "video"
    [entry] = info["entries"]
    assert entry["id"] == "vid-1"
    assert entry["title"] == "Fake video 1"
    assert entry["url"] == "https://fake.test/video"
    assert entry["live"] is False


def test_download_merges_two_parts_and_reports_the_final_file(tmp_path):
    events = run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="best")
    parts = {e["part"] for e in events if e["event"] == "progress"}
    assert parts == {1, 2}
    done = last(events)
    assert done["event"] == "done"
    assert done["path"] == str(tmp_path / "Fake video 1 [vid-1].mp4")
    assert done["size"] == 3_400_000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Fake video 1 [vid-1].mp4"]


def test_download_works_with_youtube_dl_too(tmp_path):
    done = last(
        run("download", "https://fake.test/video?delay=0", engine="youtube-dl", dest=str(tmp_path), preset="720")
    )
    assert done["event"] == "done"
    assert Path(done["path"]).exists()


def test_engine_failure_is_reported_as_an_error_event():
    error = last(run("probe", "https://fake.test/unavailable"))
    assert error["event"] == "error"
    assert "Video unavailable" in error["message"]


def presets(url, ffmpeg=True):
    [entry] = last(run("probe", url, ffmpeg=ffmpeg))["entries"]
    return {p["id"]: p["size"] for p in entry["presets"]}, [p["id"] for p in entry["presets"]]


def test_a_video_offers_best_its_real_heights_and_audio_with_size_estimates():
    sizes, order = presets("https://fake.test/video")
    assert order == ["best", "1080", "720", "480", "360", "audio", "mp3"]
    # yt-dlp's rule: largest short side, then H.264, then size: at 1080 the H.264 HLS stream (9000 kbit/s for 95 s)
    # outranks the smaller H.264 file; the AAC m4a outranks opus; at 360 the combined stream is merged with it too
    assert sizes == {
        "best": 9000 * 125 * 95 + 400_000,
        "1080": 9000 * 125 * 95 + 400_000,
        "720": 1_500_000 + 400_000,
        "480": 800_000 + 400_000,
        "360": 600_000 + 400_000,
        "audio": 400_000,
        "mp3": None,
    }


def test_without_ffmpeg_only_streams_with_picture_and_sound_are_offered():
    sizes, order = presets("https://fake.test/video", ffmpeg=False)
    assert order == ["best", "360", "audio"]
    assert sizes["best"] == 600_000
    assert sizes["audio"] == 400_000


def test_an_audio_only_link_offers_no_heights():
    sizes, order = presets("https://fake.test/audio")
    assert order == ["best", "audio", "mp3"]
    assert sizes["audio"] == 500_000


def test_a_playlist_is_read_flat_with_the_generic_presets():
    info = last(run("probe", "https://fake.test/playlist"))
    assert info["kind"] == "playlist"
    assert info["title"] == "Fake playlist"
    assert [e["url"] for e in info["entries"]] == [f"https://fake.test/video/{n}" for n in (1, 2, 3)]
    first = info["entries"][0]
    assert first["thumbnail"].startswith("data:image/svg+xml")
    assert first["duration"] == 95
    assert [(p["id"], p["size"]) for p in first["presets"]] == [
        ("best", None),
        ("1080", None),
        ("720", None),
        ("480", None),
        ("audio", None),
        ("mp3", None),
    ]


def test_a_live_entry_is_flagged():
    [entry] = last(run("probe", "https://fake.test/live"))["entries"]
    assert entry["live"] is True


def test_mp3_is_extracted_when_asked(tmp_path):
    done = last(run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="mp3"))
    assert done["path"] == str(tmp_path / "Fake video 1 [vid-1].mp3")


def test_a_size_is_estimated_from_the_bitrate_when_the_site_gives_none():
    sizes, order = presets("https://fake.test/hls")
    assert order == ["best", "720", "audio", "mp3"]
    assert sizes["best"] == 800 * 1000 // 8 * 95


def test_a_very_long_title_is_cut_so_the_file_name_fits(tmp_path):
    for engine in ("yt-dlp", "youtube-dl"):
        dest = tmp_path / engine
        dest.mkdir()
        done = last(
            run("download", "https://fake.test/long-title?delay=0", engine=engine, dest=str(dest), preset="best")
        )
        assert done["event"] == "done", done
        name = os.path.basename(done["path"])
        assert name.startswith("Ünïcödé 日本語") and name.endswith(" [long-1].mp4")
        assert len(name.encode()) + len(".f137.mp4.part") < 255


# Chapters and metadata


def ran(events):
    """The post-processors the fake engine ran, in order (it logs them)."""
    line = next(e["line"] for e in events if e["event"] == "log" and e["line"].startswith("[fake] ran: "))
    return line.removeprefix("[fake] ran: ").split(", ")


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
@pytest.mark.parametrize(
    "preset, with_ffmpeg",
    [
        ("best", ["FFmpegMetadata", "Capture"]),
        ("audio", ["FFmpegMetadata", "Capture"]),
        ("mp3", ["FFmpegExtractAudio", "FFmpegMetadata", "Capture"]),
    ],
)
def test_chapters_and_metadata_are_embedded_when_ffmpeg_is_there(tmp_path, engine, preset, with_ffmpeg):
    events = run("download", "https://fake.test/video?delay=0", engine=engine, dest=str(tmp_path), preset=preset)
    assert last(events)["event"] == "done", last(events)
    assert ran(events) == with_ffmpeg  # the final-path capture still runs last


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
def test_without_ffmpeg_nothing_is_embedded(tmp_path, engine):
    events = run(
        "download", "https://fake.test/video?delay=0", engine=engine, dest=str(tmp_path), preset="best", ffmpeg=False
    )
    assert ran(events) == ["Capture"]


# Quality classes: a format's class is its short side


def test_a_vertical_video_offers_its_classes_with_labels():
    [entry] = last(run("probe", "https://fake.test/vertical"))["entries"]
    assert [(p["id"], p["label"]) for p in entry["presets"]] == [
        ("best", "Best"),
        ("1080", "1080p"),
        ("720", "720p"),
        ("audio", "Audio"),
        ("mp3", "MP3"),
    ]
    sizes = {p["id"]: p["size"] for p in entry["presets"]}
    assert sizes["1080"] == 2_800_000 + 400_000  # the 1080x1920 stream, not a lower one
    assert sizes["720"] == 1_300_000 + 400_000


def test_playlist_presets_carry_labels_too():
    entry = last(run("probe", "https://fake.test/playlist"))["entries"][0]
    assert [p["label"] for p in entry["presets"]] == ["Best", "1080p", "720p", "480p", "Audio", "MP3"]


def test_yt_dlp_chooses_by_its_format_sorting_with_resolution_first():
    from spool.worker import format_params

    assert format_params("720", True, "yt_dlp") == {
        "format": "bv*+ba/b",
        "format_sort": ["res:720", "fps", "vcodec:h264", "acodec:aac"],
    }
    assert format_params("best", False, "yt_dlp") == {
        "format": "b",
        "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac"],
    }
    assert format_params("audio", True, "yt_dlp") == {"format": "bestaudio[ext=m4a]/bestaudio/best"}


def test_a_4_3_video_is_classed_by_its_short_side():
    sizes, order = presets("https://fake.test/four-three")
    assert order == ["best", "480", "360", "audio", "mp3"]
    assert sizes["360"] == 300_000 + 400_000  # the 480x360 stream, not the 640x480 one


def test_the_top_class_wins_even_when_only_a_lower_class_is_h264():
    sizes, order = presets("https://fake.test/vp9-top")
    assert sizes["best"] == sizes["1080"] == 2_500_000 + 400_000  # VP9 1080p, not H.264 720p
    assert sizes["720"] == 1_500_000 + 400_000


def test_a_destination_deleted_just_before_the_engine_starts_is_not_recreated(tmp_path):
    drive = tmp_path / "Drive"
    drive.mkdir()
    url = "https://fake.test/vanish?delay=0&rm=" + quote(str(drive))  # the fake deletes exactly this folder
    error = last(run("download", url, dest=str(drive), preset="best"))
    assert error["event"] == "error"
    assert not drive.exists()


def test_files_carry_the_download_time_not_the_upload_time(tmp_path):
    done = last(run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="best"))
    assert abs(os.path.getmtime(done["path"]) - time.time()) < 60


# Entries with no link of their own


def test_playlist_entries_that_share_one_link_are_items_of_the_probed_link():
    url = "https://fake.test/shared-playlist"
    entries = last(run("probe", url))["entries"]
    assert [(e["url"], e["item"]) for e in entries] == [(url, 1), (url, 2), (url, 3)]


def test_playlist_entries_with_their_own_links_are_not_items():
    entries = last(run("probe", "https://fake.test/playlist"))["entries"]
    assert [e["item"] for e in entries] == [None, None, None]


def test_youtube_dl_youtube_entries_with_a_bare_id_get_their_watch_link():
    entries = last(run("probe", "https://fake.test/ytdl-ids", engine="youtube-dl"))["entries"]
    assert [(e["url"], e["item"]) for e in entries] == [
        ("https://www.youtube.com/watch?v=abcdefghij1", None),
        ("https://www.youtube.com/watch?v=abcdefghij2", None),
    ]


def test_an_item_downloads_only_that_entry(tmp_path):
    events = run("download", "https://fake.test/shared-playlist?delay=0", dest=str(tmp_path), preset="best", item=2)
    assert last(events)["event"] == "done"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Shared item 2 [shared-2].mp4"]
