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


def steps(events):
    return [e for e in events if e["event"] == "progress" and e["status"] == "processing"]


def test_the_join_reports_how_far_it_is_about_once_a_second(tmp_path):
    events = run("download", "https://fake.test/video?delay=0&join=0.15", dest=str(tmp_path), preset="best")
    assert last(events)["event"] == "done", last(events)
    join = [e for e in steps(events) if e["what"] == "join"]
    assert len(join) >= 3  # the join takes about 3 seconds
    assert {e["total"] for e in join} == {3_400_000}  # the sum of the parts
    written = [e["downloaded"] for e in join]
    assert written == sorted(written) and 0 < written[-1] <= 3_400_000
    assert any(e["speed"] for e in join) and any(e["eta"] is not None for e in join)
    assert {e["what"] for e in steps(events)} == {"tags", "join"}  # the audio part's tags, then the join


def test_the_mp3_step_reports_too(tmp_path):
    events = run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="mp3")
    assert last(events)["event"] == "done", last(events)
    assert [e["what"] for e in steps(events)][:1] == ["mp3"]
    assert steps(events)[0]["total"] == 95 * 128_000 // 8  # its usual size: the duration at 128 kbit/s


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


def best_detail(url, ffmpeg=True):
    [entry] = last(run("probe", url, ffmpeg=ffmpeg))["entries"]
    assert all(p["detail"] is None for p in entry["presets"][1:])
    return entry["presets"][0]["detail"]


def test_a_video_offers_best_its_real_heights_and_audio_with_size_estimates():
    sizes, order = presets("https://fake.test/video")
    assert order == ["best", "720", "480", "360", "audio", "mp3"]  # 1080 is what Best gives: offered once
    assert best_detail("https://fake.test/video") == "1080p"
    # yt-dlp's rule: largest short side, then H.264, then a known size: at 1080 the H.264 file outranks the H.264
    # HLS stream, which has a higher bitrate but no size; the AAC m4a outranks opus; at 360 the combined stream
    # has its own sound
    assert sizes == {
        "best": 3_000_000 + 400_000,
        "720": 1_500_000 + 400_000,
        "480": 800_000 + 400_000,
        "360": 600_000,  # the combined stream: the engine takes no second sound
        "audio": 400_000,
        "mp3": 95 * 128_000 // 8,  # the duration at the usual 128 kbit/s
    }


def test_without_ffmpeg_only_streams_with_picture_and_sound_are_offered():
    sizes, order = presets("https://fake.test/video", ffmpeg=False)
    assert order == ["best", "audio"]  # Best is the one combined 360p stream
    assert best_detail("https://fake.test/video", ffmpeg=False) == "360p"
    assert sizes["best"] == 600_000
    assert sizes["audio"] == 400_000


def test_an_audio_only_link_offers_no_heights():
    sizes, order = presets("https://fake.test/audio")
    assert order == ["best", "audio", "mp3"]
    assert best_detail("https://fake.test/audio") is None
    assert sizes["audio"] == 500_000


def test_a_playlist_is_read_flat_with_the_generic_presets():
    info = last(run("probe", "https://fake.test/playlist"))
    assert info["kind"] == "playlist"
    assert info["title"] == "Fake playlist"
    assert [e["url"] for e in info["entries"]] == [f"https://fake.test/video/{n}" for n in (1, 2, 3)]
    first = info["entries"][0]
    assert first["thumbnail"].startswith("data:image/svg+xml")
    assert first["duration"] == 95
    assert [(p["id"], p["detail"], p["size"]) for p in first["presets"]] == [
        ("best", None, None),
        ("1080", None, None),
        ("720", None, None),
        ("480", None, None),
        ("audio", None, None),
        ("mp3", None, None),
    ]


def test_a_live_entry_is_flagged():
    [entry] = last(run("probe", "https://fake.test/live"))["entries"]
    assert entry["live"] is True


def test_mp3_is_extracted_when_asked(tmp_path):
    done = last(run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="mp3"))
    assert done["path"] == str(tmp_path / "Fake video 1 [vid-1].mp3")


def test_a_size_is_estimated_from_the_bitrate_when_the_site_gives_none():
    sizes, order = presets("https://fake.test/hls")
    assert order == ["best", "audio", "mp3"]
    assert best_detail("https://fake.test/hls") == "720p"
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


def logged(events, start):
    return [e["line"] for e in events if e["event"] == "log" and e["line"].startswith(start)]


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
@pytest.mark.parametrize("preset", ["best", "audio", "mp3"])
def test_chapters_and_metadata_are_embedded_when_ffmpeg_is_there(tmp_path, engine, preset):
    events = run("download", "https://fake.test/video?delay=0", engine=engine, dest=str(tmp_path), preset=preset)
    assert last(events)["event"] == "done", last(events)
    assert logged(events, "[fake] details in the file: ") == ["[fake] details in the file: True"]
    assert ran(events)[-1] == "Capture"  # the final-path capture still runs last


def test_a_join_writes_the_big_file_once_taking_the_details_from_the_audio_part(tmp_path):
    events = run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="best")
    assert last(events)["event"] == "done", last(events)
    assert logged(events, "[Metadata] ") == ['[Metadata] Adding metadata to "Fake video 1 [vid-1].f140.m4a"']
    assert logged(events, "[fake] details in the file: ") == ["[fake] details in the file: True"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Fake video 1 [vid-1].mp4"]


@pytest.mark.parametrize(
    "url, preset, final",
    [
        (
            "video?fail=tags&delay=0",
            "best",
            "Fake video 1 [vid-1].mp4",
        ),  # the part could not be tagged: two passes, as before
        ("video?delay=0", "audio", "Fake video 1 [vid-1].m4a"),  # one part: nothing to join
    ],
)
def test_otherwise_the_details_are_written_after(tmp_path, url, preset, final):
    events = run("download", f"https://fake.test/{url}", dest=str(tmp_path), preset=preset)
    assert last(events)["event"] == "done", last(events)
    assert logged(events, "[Metadata] ")[-1] == f'[Metadata] Adding metadata to "{final}"'
    assert logged(events, "[fake] details in the file: ") == ["[fake] details in the file: True"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [final]


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
def test_without_ffmpeg_nothing_is_embedded(tmp_path, engine):
    events = run(
        "download", "https://fake.test/video?delay=0", engine=engine, dest=str(tmp_path), preset="best", ffmpeg=False
    )
    assert ran(events) == ["Capture"]


# Quality classes: a format's class is its short side


def test_a_vertical_video_offers_its_classes_with_labels():
    [entry] = last(run("probe", "https://fake.test/vertical"))["entries"]
    assert [(p["id"], p["label"], p["detail"]) for p in entry["presets"]] == [
        ("best", "Best", "1080p"),
        ("720", "720p", None),
        ("audio", "Audio", None),
        ("mp3", "MP3", None),
    ]
    sizes = {p["id"]: p["size"] for p in entry["presets"]}
    assert sizes["best"] == 2_800_000 + 400_000  # the 1080x1920 stream, not a lower one
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
    assert order == ["best", "360", "audio", "mp3"]
    assert best_detail("https://fake.test/four-three") == "480p"
    assert sizes["360"] == 300_000 + 400_000  # the 480x360 stream, not the 640x480 one


def test_the_top_class_wins_even_when_only_a_lower_class_is_h264():
    sizes, order = presets("https://fake.test/vp9-top")
    assert sizes["best"] == 2_500_000 + 400_000  # VP9 1080p, not H.264 720p
    assert best_detail("https://fake.test/vp9-top") == "1080p"
    assert sizes["720"] == 1_500_000 + 400_000


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot delete a process's current folder, so this race cannot happen there and is not covered",
)
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


def test_a_wide_screen_video_gets_the_class_above_its_short_side():
    """1920x800 is what sites call 1080p: a format's class is the smallest standard one that holds its short side."""
    from spool import worker

    video = [
        {"format_id": f"v{h}", "width": w, "height": h, "vcodec": "avc1", "acodec": "none", "filesize": 100}
        for w, h in ((1920, 800), (1280, 534))
    ]
    sound = {"format_id": "a", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 10}
    entry = worker.entry({"id": "x", "title": "Wide", "formats": [*video, sound]}, ffmpeg=True)
    assert [p["id"] for p in entry["presets"]] == ["best", "720", "audio", "mp3"]
    assert entry["presets"][0]["detail"] == "1080p"


# Progress across parts (yt-dlp's hooks name only the part they are on)


def test_progress_rises_across_the_video_and_the_audio_part(tmp_path):
    events = run("download", "https://fake.test/video?delay=0", dest=str(tmp_path), preset="best")
    progress = [(e["part"], e["progress"]) for e in events if e["event"] == "progress" and e["progress"] is not None]
    values = [p for _, p in progress]
    assert values == sorted(values), "progress went backwards"
    assert max(p for part, p in progress if part == 1) < 0.95  # the video part is not the whole job
    assert values[-1] == 1.0


def test_a_part_number_past_the_list_does_not_crash():
    from spool.worker import where

    hook = {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2, "info_dict": {"format_id": "x"}}
    progress, _ = where(hook, 3, {"preset": "best", "ffmpeg": True}, [{"format_id": "a"}, {"format_id": "b"}])
    assert 0 <= progress <= 1


# Every file the job writes is named, so Remove can clean it


def test_a_merge_cut_short_leaves_only_files_the_job_named(tmp_path):
    events = run("download", "https://fake.test/video?delay=0&fail=merge", dest=str(tmp_path), preset="best")
    assert last(events)["event"] == "error"
    named = {e["path"] for e in events if e["event"] == "file"}
    left = {str(p) for p in tmp_path.iterdir()}
    assert any(p.endswith(".temp.mp4") for p in left)
    assert left <= named


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
def test_names_keep_the_destination_as_given_through_a_symbolic_link(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    dest = str(tmp_path / "link")
    events = run("download", "https://fake.test/video?delay=0", dest=dest, preset="best")
    paths = [e["path"] for e in events if e["event"] in ("file", "done")]
    assert last(events)["event"] == "done"
    assert {os.path.dirname(p) for p in paths} == {dest}


# File names


def test_a_title_of_characters_the_engine_widens_still_fits(tmp_path):
    done = last(run("download", "https://fake.test/pipes?delay=0", dest=str(tmp_path), preset="best"))
    assert done["event"] == "done", done
    assert len(os.path.basename(done["path"]).encode()) + len(".f137.mp4.part") < 255


def test_a_class_file_is_named_for_the_class_it_got(tmp_path):
    """A playlist offers 720 for every entry; a video whose top is 480 is saved, and named, as 480p."""
    done = last(run("download", "https://fake.test/four-three?delay=0", dest=str(tmp_path), preset="720"))
    assert os.path.basename(done["path"]) == "Fake 4:3 video [43-1] 480p.mp4".replace(":", "：")


# What is offered can be saved


@pytest.mark.parametrize("ffmpeg", [True, False])
@pytest.mark.parametrize("url", ["https://fake.test/video", "https://fake.test/no-combined", "https://fake.test/audio"])
def test_every_quality_offered_downloads(tmp_path, url, ffmpeg):
    _, order = presets(url, ffmpeg=ffmpeg)
    for preset in order:
        if preset == "mp3":
            continue  # its file is whatever the audio part gave
        dest = tmp_path / preset
        dest.mkdir()
        done = last(run("download", url + "?delay=0", dest=str(dest), preset=preset, ffmpeg=ffmpeg))
        assert done["event"] == "done", (preset, done)


def test_without_ffmpeg_a_playlist_offers_no_classes():
    entry = last(run("probe", "https://fake.test/playlist", ffmpeg=False))["entries"][0]
    assert [p["id"] for p in entry["presets"]] == ["best", "audio"]


# Lists


def test_a_link_that_is_a_list_of_videos_fails_as_a_playlist(tmp_path):
    error = last(run("download", "https://fake.test/channel?delay=0", dest=str(tmp_path), preset="best"))
    assert (error["event"], error["kind"]) == ("error", "playlist")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("engine", ["yt-dlp", "youtube-dl"])
def test_a_playlist_link_without_an_item_saves_nothing(tmp_path, engine):
    error = last(
        run("download", "https://fake.test/playlist?delay=0", engine=engine, dest=str(tmp_path), preset="best")
    )
    assert error.get("kind") == "playlist"
    assert list(tmp_path.iterdir()) == []


# Kinds the worker knows


def test_a_live_stream_fails_with_its_kind(tmp_path):
    error = last(run("download", "https://fake.test/live", dest=str(tmp_path), preset="best"))
    assert (error["event"], error["kind"]) == ("error", "live")


def test_a_missing_javascript_runtime_is_named_only_for_what_it_causes(tmp_path):
    error = last(run("download", "https://fake.test/needs-js", dest=str(tmp_path), preset="best"))
    assert error["kind"] == "no_js"
    error = last(run("download", "https://fake.test/needs-js-403?delay=0", dest=str(tmp_path), preset="best"))
    assert "kind" not in error
    assert "javascript" not in error["message"].lower()


def test_a_relative_engine_path_still_loads_after_moving_into_the_destination(tmp_path):
    from serve_fake import fake_engine_zip

    (tmp_path / "engine").write_bytes(fake_engine_zip())
    (tmp_path / "out").mkdir()
    spec = {"engine": "yt-dlp", "engine_path": "engine", "mode": "download", "url": "https://fake.test/video?delay=0"}
    spec |= {"ffmpeg": True, "dest": str(tmp_path / "out"), "preset": "best"}
    out = subprocess.run(
        [sys.executable, "-I", str(WORKER)], input=json.dumps(spec), capture_output=True, text=True, cwd=tmp_path
    ).stdout
    assert last([json.loads(line) for line in out.splitlines()])["event"] == "done"


def test_the_fake_deletes_a_folder_only_inside_the_tests_own(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "other").mkdir()  # in the system temp folder, but not this test's folder
    code = "import sys; sys.path.insert(0, sys.argv[1]); import _fake; _fake.YoutubeDL({'socket_timeout': 1}).remove_test_folder(sys.argv[2])"
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(FAKE), str(tmp_path / "other")],
        cwd=tmp_path / "home",
        capture_output=True,
    )
    assert out.returncode != 0
    assert (tmp_path / "other").exists()
