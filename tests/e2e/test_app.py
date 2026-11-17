"""The main flows a person uses, through the real server and the fake engine."""

import json
import re
import sys
import threading
import time

import pytest
import serve_fake  # on the path through conftest
from playwright.sync_api import expect

VIDEO = "https://fake.test/video"


def look_up(page, link):
    field = page.get_by_label("Link to download")
    field.fill(link)
    field.press("Enter")


def tray(page, title):
    return page.get_by_role("region", name=title)


def download(page, link, title):
    look_up(page, link)
    tray(page, title).get_by_role("button", name="Download", exact=True).click()


def group(page, name):
    return page.get_by_role("region", name=name)


def add(spool, links, preset="best"):
    """Add jobs straight through the server, as another tab would after a lookup: each with its video's id (here its
    link), so different videos in one folder run together. Returns the jobs."""
    res = spool[1].add([{"url": u, "id": u, "title": t} for u, t in links], preset)
    return res[0] if isinstance(res, tuple) else res


def server_job(spool, title, state, timeout=15):
    """The job as the server holds it, once it reaches the state."""
    end = time.time() + timeout
    while time.time() < end:
        for j in spool[1].snapshot()["jobs"]:
            if j["title"] == title and j["state"] == state:
                return j
        time.sleep(0.1)
    raise AssertionError(f"{title} never became {state}")


def paste(page, text):
    page.evaluate(
        """text => { const d = new DataTransfer(); d.setData('text/plain', text);
        document.body.dispatchEvent(new ClipboardEvent('paste', {clipboardData: d, bubbles: true})); }""",
        text,
    )


def test_paste_then_enter_is_the_whole_flow(app):
    paste(app, "Watch this: <https://fake.test/video>.")
    tray = app.get_by_role("region", name="Fake video 1")
    download = tray.get_by_role("button", name="Download", exact=True)
    expect(download).to_be_focused()
    expect(app.get_by_label("Link to download")).to_have_value("https://fake.test/video")
    with app.expect_request("**/api/jobs") as sent:
        app.keyboard.press("Enter")
    assert sent.value.post_data_json["preset"] == "best"
    expect(app.get_by_role("region", name="Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)


def test_a_bare_host_is_read_as_a_link(app):
    with app.expect_request("**/api/probe") as sent:
        look_up(app, "fake.test/video")
    assert sent.value.post_data_json["url"] == "https://fake.test/video"
    expect(app.get_by_role("region", name="Fake video 1")).to_be_visible()


@pytest.mark.parametrize("how", ["enter", "paste"])
def test_text_with_no_link_says_so_under_the_field(app, how):
    field = app.get_by_label("Link to download")
    if how == "enter":
        look_up(app, "cats playing piano")
    else:
        app.locator(".queue").click()
        paste(app, "cats playing piano")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_accessible_description("That doesn't look like a link. Paste a video's web address.")
    expect(app.get_by_role("alert").get_by_role("button")).to_have_count(0)


def test_escape_cancels_a_lookup_and_ignores_the_late_answer(app):
    held = []
    app.route("**/api/probe", lambda route: held.append(route))
    look_up(app, VIDEO)
    expect(app.get_by_role("button", name="Getting details…")).to_be_disabled()
    app.keyboard.press("Escape")
    expect(app.get_by_role("button", name="Get details")).to_be_enabled()
    expect(app.get_by_label("Link to download")).to_be_focused()
    with app.expect_response("**/api/probe") as late:
        held[0].continue_()
    late.value.finished()
    expect(app.get_by_role("region", name="Fake video 1")).to_have_count(0)


def test_cmd_enter_in_the_preview_downloads_the_selected_quality(app):
    look_up(app, VIDEO)
    tray = app.get_by_role("region", name="Fake video 1")
    tray.get_by_role("radio", name=re.compile("^720p")).check()
    with app.expect_request("**/api/jobs") as sent:
        app.keyboard.press("ControlOrMeta+Enter")
    assert sent.value.post_data_json["preset"] == "720"


def test_enter_on_a_quality_choice_downloads_it(app):
    look_up(app, VIDEO)
    radio = app.get_by_role("region", name="Fake video 1").get_by_role("radio", name=re.compile("^720p"))
    radio.check()
    radio.focus()
    with app.expect_request("**/api/jobs") as sent:
        app.keyboard.press("Enter")
    assert sent.value.post_data_json["preset"] == "720"


def test_a_link_pasted_during_setup_is_looked_up_when_setup_ends(page, tmp_path):
    httpd, url, jobs, _ = serve_fake.start(tmp_path, fresh=True)
    try:
        page.goto(url)
        expect(page.get_by_text("Setting up Spool. This takes a few seconds.")).to_be_visible()
        paste(page, VIDEO)
        expect(page.get_by_label("Link to download")).to_have_value(VIDEO)
        expect(page.get_by_role("region", name="Fake video 1")).to_be_visible(timeout=20000)
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()


def test_look_up_a_link_then_download_it_to_finished(app):
    look_up(app, VIDEO)
    t = tray(app, "Fake video 1")
    expect(t.get_by_text("Fake Channel")).to_be_visible()
    expect(t.get_by_text("1:35")).to_be_visible()
    expect(t.get_by_role("group", name="Quality").get_by_role("radio", name=re.compile("^Best"))).to_be_checked()

    t.get_by_role("button", name="Download", exact=True).click()

    expect(t).to_be_hidden()
    expect(app.get_by_label("Link to download")).to_be_focused()
    expect(app.get_by_label("Link to download")).to_have_value("")
    row = group(app, "Finished").get_by_role("listitem").filter(has_text="Fake video 1")
    expect(row).to_be_visible(timeout=15000)
    expect(row.get_by_role("button", name=re.compile("Show in (Finder|folder)"))).to_be_visible()
    expect(row.get_by_text("Best", exact=True)).to_be_visible()  # its quality and size, no "Done" and no tick
    expect(row.get_by_text(re.compile(r"^\d+(\.\d)? (KB|MB)$"))).to_be_visible()
    expect(row.get_by_text("Done")).to_have_count(0)
    expect(row.get_by_role("heading")).to_have_attribute("dir", "auto")


def test_keyboard_only_main_flow(app):
    expect(app.get_by_label("Link to download")).to_be_focused()
    app.keyboard.type(VIDEO)
    app.keyboard.press("Enter")
    t = tray(app, "Fake video 1")
    expect(t.get_by_role("button", name="Download", exact=True)).to_be_focused()
    # Back into the quality group, move to the next choice with an arrow, then Enter downloads it.
    best = t.get_by_role("radio", name=re.compile("^Best"))
    while not best.evaluate("e => e === document.activeElement"):
        app.keyboard.press("Shift+Tab")
    app.keyboard.press("ArrowRight")
    expect(best).not_to_be_checked()
    with app.expect_request("**/api/jobs") as sent:
        app.keyboard.press("Enter")
    assert sent.value.post_data_json["preset"] != "best"
    expect(app.get_by_label("Link to download")).to_be_focused()
    expect(group(app, "Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)


def test_cmd_enter_downloads_straight_away(app):
    field = app.get_by_label("Link to download")
    field.fill(VIDEO)
    field.press("ControlOrMeta+Enter")
    expect(group(app, "Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)


def test_escape_closes_the_tray_and_returns_to_the_field(app):
    look_up(app, VIDEO)
    t = tray(app, "Fake video 1")
    t.get_by_role("radio", name=re.compile("^Best")).focus()
    app.keyboard.press("Escape")
    expect(t).to_be_hidden()
    expect(app.get_by_label("Link to download")).to_be_focused()


def test_escape_in_the_instant_the_preview_opens_still_returns_to_the_field(app):
    # Escape arrives as the preview is drawn, before the browser paints it: no later focus move may undo the close.
    app.evaluate(
        """() => new MutationObserver((_, watch) => {
            if (!document.querySelector('section.tray')) return;
            watch.disconnect();
            document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
        }).observe(document.body, {childList: true, subtree: true})"""
    )
    look_up(app, VIDEO)
    expect(app.locator("section.tray")).to_have_count(0)
    expect(app.get_by_label("Link to download")).to_be_focused()
    app.wait_for_timeout(300)  # past the moment a late focus move would have run
    expect(app.get_by_label("Link to download")).to_be_focused()


def test_pasting_several_links_queues_them_all(app):
    app.locator(".queue").click()
    paste(app, "https://fake.test/video/1\nhttps://fake.test/video/2, https://fake.test/audio")
    expect(app.get_by_role("status").filter(has_text="Added 3 downloads.")).to_be_visible()
    expect(group(app, "Finished").get_by_role("listitem")).to_have_count(3, timeout=15000)
    expect(app.get_by_text("Added 3 downloads.")).to_be_hidden(timeout=7000)  # gone after 5 seconds


@pytest.mark.parametrize("width", [1440, 375])
def test_the_skeleton_is_as_tall_as_the_tray_it_stands_in_for(app, width):
    app.set_viewport_size({"width": width, "height": 900})
    held = []
    app.route("**/api/probe", lambda route: held.append(route))
    look_up(app, VIDEO)
    waiting = app.locator(".tray.skeleton").bounding_box()["height"]
    held[0].continue_()
    expect(tray(app, "Fake video 1")).to_be_visible()
    assert abs(tray(app, "Fake video 1").bounding_box()["height"] - waiting) < 2


def test_one_yellow_button_at_a_time(app):
    look_up(app, VIDEO)
    expect(tray(app, "Fake video 1")).to_be_visible()
    expect(app.locator("button.primary:visible")).to_have_count(1)
    expect(app.get_by_role("button", name="Get details")).not_to_have_class("primary")


# Words: the server's sentence, and the button its hint names


def probe_answers(page, kind, hint):
    page.route(
        "**/api/probe",
        lambda route: route.fulfill(
            status=422, json={"error": {"kind": kind, "message": f"The server's words for {kind}.", "hint": hint}}
        ),
    )


@pytest.mark.parametrize(
    "kind,hint,button",
    [
        ("network", "retry", "Try again"),
        ("blocked", "update_engine", "Update and try again"),
        ("no_ffmpeg", "install_ffmpeg", "Copy command"),
        ("unavailable", None, None),
        ("engine", "try_other_engine", None),  # a hint the page doesn't know gets no button
    ],
)
def test_a_lookup_error_shows_the_servers_words_and_the_hints_button(app, kind, hint, button):
    probe_answers(app, kind, hint)
    look_up(app, VIDEO)
    error = app.get_by_role("alert").filter(has_text=f"The server's words for {kind}.")
    expect(error).to_be_visible()
    field = app.get_by_label("Link to download")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_accessible_description(f"The server's words for {kind}.")
    if button:
        expect(error.get_by_role("button")).to_have_text(button)
    else:
        expect(error.get_by_role("button")).to_have_count(0)


def test_try_again_looks_the_link_up_again(app):
    probe_answers(app, "network", "retry")
    look_up(app, VIDEO)
    app.unroute("**/api/probe")
    app.get_by_role("button", name="Try again").click()
    expect(tray(app, "Fake video 1")).to_be_visible()


def test_a_link_only_the_second_engine_reads_just_works(app):
    look_up(app, "https://fake.test/ytdl-only")
    expect(tray(app, "Fake video only youtube-dl reads")).to_be_visible()
    expect(app.get_by_role("alert")).to_have_count(0)


def test_a_link_spool_cannot_read_shows_only_the_sentence(app):
    look_up(app, "https://fake.test/unsupported")
    error = app.get_by_role("alert")
    expect(error).to_contain_text("Spool can't read this link.")
    expect(error.get_by_role("button")).to_have_count(0)


# A newer engine exists, but installing it at launch failed, so the update is still on offer.
@pytest.mark.parametrize(
    "spool", [{"github": serve_fake.FakeGitHub(latest=serve_fake.LATEST, corrupt=True)}], indirect=True
)
def test_a_refused_download_shows_the_servers_words_and_offers_the_update(app, spool):
    download(app, "https://fake.test/403", "Fake refused video")
    job = server_job(spool, "Fake refused video", "failed")
    stopped = group(app, "Stopped")
    expect(stopped.get_by_text(job["error"]["message"])).to_be_visible()
    expect(stopped.get_by_role("button", name="Update and try again")).to_have_class("fill")
    expect(stopped.get_by_role("button", name="Remove from list")).to_have_class("quiet")
    expect(stopped.get_by_role("button", name="Show log")).to_have_count(0)


def test_a_refused_download_with_the_latest_engine_offers_try_again(app, spool):
    download(app, "https://fake.test/403", "Fake refused video")
    stopped = group(app, "Stopped")
    expect(stopped.get_by_role("button", name="Try again")).to_have_class("fill", timeout=15000)
    expect(stopped.get_by_role("button", name="Update and try again")).to_have_count(0)


def test_an_engine_failure_offers_try_again_and_the_log(app, spool):
    add(spool, [("https://fake.test/broken", "Fake broken video")])
    stopped = group(app, "Stopped")
    expect(stopped.get_by_role("button", name="Try again")).to_have_class("fill", timeout=15000)
    stopped.get_by_role("button", name="Show log").click()
    expect(stopped.get_by_label("Download log")).to_contain_text("broken")
    expect(stopped.get_by_role("button", name="Hide log")).to_have_attribute("aria-expanded", "true")


def test_a_queued_live_stream_says_to_wait_and_offers_only_remove(app, spool):
    add(spool, [("https://fake.test/live", "Fake live stream")])
    stopped = group(app, "Stopped")
    expect(stopped.get_by_text("This is live now. Download it after it ends.")).to_be_visible(timeout=15000)
    expect(stopped.get_by_role("button")).to_have_count(1)
    expect(stopped.get_by_role("button", name="Remove from list")).to_be_visible()


def test_a_live_stream_cannot_be_downloaded_yet(app):
    look_up(app, "https://fake.test/live")
    t = tray(app, "Fake live stream")
    expect(t.get_by_text("This is live now. Download it after it ends.")).to_be_visible()
    expect(t.get_by_role("button", name="Download", exact=True)).to_be_disabled()


def test_youtube_without_its_helper_offers_try_again(app):
    look_up(app, "https://fake.test/needs-js")
    error = app.get_by_role("alert")
    expect(error).to_contain_text("Spool couldn't set up what it needs for YouTube. Check your internet connection.")
    expect(error.get_by_role("button")).to_have_text("Try again")


NAMES = re.compile(r"yt[-_]dlp|youtube[-_]dl|ffmpeg|deno|(?-i:\bMain\b|\bBackup\b)", re.I)


def assert_no_program_names(page, state):
    """Program names may appear only inside an install command."""
    shown = page.evaluate(
        """() => { const b = document.body.cloneNode(true); b.querySelectorAll('code').forEach(c => c.remove());
        return b.innerText + ' ' + [...document.querySelectorAll('[title], [aria-label]')]
          .map(e => e.title + ' ' + (e.getAttribute('aria-label') || '')).join(' '); }"""
    )
    shown += page.locator("body").aria_snapshot()
    found = NAMES.search(re.sub(r"- code: .*", "", shown))
    assert not found, f"{state}: {found.group()} is on the page"


def test_the_page_never_names_the_programs_underneath(app, spool):
    assert_no_program_names(app, "idle")
    look_up(app, "https://fake.test/slow?delay=1")
    expect(tray(app, "Fake slow video")).to_be_visible()
    assert_no_program_names(app, "preview")
    tray(app, "Fake slow video").get_by_role("button", name="Download", exact=True).click()
    expect(app.get_by_role("progressbar")).to_be_visible(timeout=10000)
    assert_no_program_names(app, "running")
    add(spool, [("https://fake.test/403", "Fake refused video"), ("https://fake.test/broken", "Fake broken video")])
    expect(group(app, "Stopped").get_by_role("listitem")).to_have_count(2, timeout=15000)
    assert_no_program_names(app, "failed")
    look_up(app, "https://fake.test/unsupported")
    expect(app.get_by_role("alert")).to_be_visible()
    assert_no_program_names(app, "lookup failed")
    look_up(app, "https://fake.test/needs-js")
    expect(app.get_by_role("alert")).to_be_visible()
    assert_no_program_names(app, "youtube helper")
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_role("radio")).to_have_count(0)  # engines are not a choice
    assert_no_program_names(app, "settings")


@pytest.mark.parametrize("spool", [{"ffmpeg": False}], indirect=True)
def test_the_helper_strip_names_the_helper_only_in_its_command(app):
    strip = app.get_by_role("complementary", name="Free helper")
    expect(strip).to_contain_text("The top qualities and MP3 need a free helper. Run this in your terminal:")
    expect(strip.locator("code")).to_contain_text("ffmpeg")
    expect(strip.get_by_role("button", name="Copy command")).to_be_visible()
    assert_no_program_names(app, "helper strip")


# Choosing a quality


def test_the_preview_offers_four_tiles_and_the_rest_under_more_qualities(app):
    with app.expect_response("**/api/probe") as answer:
        look_up(app, VIDEO)
    presets = answer.value.json()["entries"][0]["presets"]
    quality = tray(app, "Fake video 1").get_by_role("group", name="Quality")
    expect(quality.get_by_role("radio")).to_have_count(4)
    expect(quality.get_by_role("radio", name=re.compile("^Audio"))).to_be_visible()
    quality.get_by_role("button", name="More qualities").click()
    expect(quality.get_by_role("radio")).to_have_count(len(presets))
    expect(quality.get_by_role("radio", name=re.compile(r"^MP3 \d"))).to_be_visible()  # MP3 has a size too


def test_best_says_its_real_quality_and_is_not_offered_twice(app):
    look_up(app, VIDEO)
    quality = tray(app, "Fake video 1").get_by_role("group", name="Quality")
    expect(quality.get_by_role("radio", name=re.compile("^Best 1080p"))).to_be_visible()
    quality.get_by_role("button", name="More qualities").click()
    expect(quality.get_by_role("radio", name=re.compile("^1080p"))).to_have_count(0)


def test_the_quality_picked_becomes_the_remembered_one(app):
    look_up(app, VIDEO)
    quality = tray(app, "Fake video 1").get_by_role("group", name="Quality")
    quality.get_by_role("button", name="More qualities").click()
    quality.get_by_role("radio", name=re.compile("^360p")).check()
    with app.expect_request("**/api/settings") as saved:
        app.keyboard.press("ControlOrMeta+Enter")
    assert saved.value.post_data_json == {"preset": "360"}
    expect(app.get_by_text(re.compile(r"(Cmd|Ctrl)\+Enter downloads at 360p\."))).to_be_visible()
    look_up(app, "https://fake.test/video/2")
    quality = tray(app, "Fake video 2").get_by_role("group", name="Quality")
    expect(quality.get_by_role("radio", name=re.compile("^360p"))).to_be_checked()  # More qualities starts open


def test_a_video_without_the_remembered_quality_selects_the_nearest_lower(app, spool):
    spool[1].update_settings({"preset": "720"})
    expect(app.get_by_text(re.compile(r"downloads at 720p"))).to_be_visible()
    look_up(app, "https://fake.test/four-three")
    t = tray(app, "Fake 4:3 video")
    expect(t.get_by_role("radio", name=re.compile("^Best 480p"))).to_be_checked()  # Best is this video's 480p
    with app.expect_request("**/api/jobs") as sent:
        t.get_by_role("button", name="Download", exact=True).click()
    assert sent.value.post_data_json["preset"] == "best"
    app.wait_for_timeout(300)
    assert spool[1].snapshot()["settings"]["preset"] == "720"  # a fallback is not a choice


def test_pick_two_of_a_playlist_and_download_them(app):
    look_up(app, "https://fake.test/playlist")
    t = tray(app, "Fake playlist")
    every = t.get_by_role("checkbox", name=re.compile("^Select all"))
    expect(every).to_be_checked()
    expect(every).to_have_accessible_name(re.compile("3 of 3"))
    expect(t.get_by_role("radio")).to_have_count(4)
    expect(t.get_by_text(re.compile(r"\d+(\.\d)? (KB|MB)"))).to_have_count(0)  # no sizes for a playlist
    every.uncheck()
    expect(t.get_by_role("button", name="Download 0")).to_be_disabled()
    t.get_by_role("checkbox", name=re.compile("Fake video 1")).check()
    t.get_by_role("checkbox", name=re.compile("Fake video 3")).check()
    expect(every).to_have_accessible_name(re.compile("2 of 3"))
    expect(every).to_have_js_property("indeterminate", True)

    t.get_by_role("button", name="Download 2").click()

    expect(app.get_by_role("status").filter(has_text="Added 2 downloads.")).to_be_visible()
    finished = group(app, "Finished")
    expect(finished.get_by_role("listitem")).to_have_count(2, timeout=15000)
    expect(finished.get_by_text("Fake video 2")).to_have_count(0)


def test_ticking_one_entry_of_a_shared_link_item_leaves_the_others(app):
    # A multi-file item: every entry shares one link and differs only by its position.
    look_up(app, "https://fake.test/shared-playlist")
    t = tray(app, "Fake shared item")
    boxes = t.get_by_role("list").get_by_role("checkbox")
    expect(boxes.first).to_be_checked()
    total = boxes.count()
    boxes.nth(1).uncheck()
    expect(t.get_by_text(f"{total - 1} of {total}")).to_be_visible()
    expect(boxes.first).to_be_checked()

    with app.expect_request("**/api/jobs") as sent:
        t.get_by_role("button", name=f"Download {total - 1}").click()
    items = [e["item"] for e in sent.value.post_data_json["entries"]]
    assert items == [n for n in range(1, total + 1) if n != 2]


def test_a_download_bigger_than_the_free_space_is_flagged(app):
    def huge(route):
        response = route.fetch()
        body = response.json()
        for p in body["entries"][0]["presets"]:
            if p["size"] is not None:
                p["size"] = 10**15
        route.fulfill(response=response, json=body)

    app.route("**/api/probe", huge)
    look_up(app, VIDEO)
    expect(app.get_by_text("This needs about")).to_be_visible()
    # A warning, not a block: the estimate can be wrong and the choice is the user's.
    expect(app.get_by_role("button", name="Download", exact=True)).to_be_enabled()


# While it downloads


def test_one_percentage_drives_the_counter_the_bar_and_the_tab_title(app):
    download(app, "https://fake.test/slow?delay=0.3", "Fake slow video")
    expect(app).to_have_title(re.compile(r"^\d+% - Spool$"), timeout=10000)
    row = group(app, "Downloading").get_by_role("listitem")
    bar = row.get_by_role("progressbar")
    expect(bar).to_have_attribute("value", re.compile(r"^[1-9]\d*$"), timeout=10000)
    value = bar.get_attribute("value")
    assert row.locator(".counter").inner_text().replace("\n", "") in (
        f"{value}%",
        f"{int(value) + 1}%",
        f"{int(value) - 1}%",
    )
    expect(app.get_by_role("status").filter(has_text="Finished: Fake slow video")).to_be_attached(timeout=15000)
    expect(app).to_have_title("Spool")


def test_running_rows_come_before_waiting_ones_then_stopped_then_finished(app, spool):
    add(spool, [("https://fake.test/video/1", "Fake video 1")])
    server_job(spool, "Fake video 1", "done")
    add(spool, [("https://fake.test/403", "Fake refused video")])
    server_job(spool, "Fake refused video", "failed")
    for n in (1, 2, 3):
        add(spool, [(f"https://fake.test/slow/{n}?delay=1", f"Slow {n}")])
    active = group(app, "Downloading").get_by_role("listitem")
    expect(active).to_have_count(3, timeout=10000)
    expect(active.nth(2)).to_contain_text("Waiting")
    expect(active.nth(2)).to_contain_text("Slow 3")  # the next to start, and the only one waiting
    headings = app.locator(".queue h2").all_inner_texts()
    assert headings == ["Downloading", "Stopped", "Finished"]


def vanish(jobs):
    """Every event stream ends with no word, as when Spool crashes or the connection is lost."""
    with jobs.lock:
        for q in jobs.subscribers:
            q.put((None, None))
    end = time.time() + 5
    while jobs.subscribers:
        assert time.time() < end, "a stream did not end"
        time.sleep(0.02)


def test_running_rows_say_reconnecting_when_spool_drops(app, spool):
    download(app, "https://fake.test/slow?delay=2", "Fake slow video")
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.get_by_text("Downloading video")).to_be_visible(timeout=10000)
    vanish(spool[1])
    expect(row.get_by_text("Reconnecting…")).to_be_visible()
    expect(row).to_contain_text("Downloading video", timeout=5000)  # the next try finds Spool again


def processing(spool, title, **step):
    """Moves the running job into its processing step with these values, as the server would report them. The fake
    sends one progress line and then sleeps a minute, so nothing overwrites them during the test."""
    jobs = spool[1]
    end = time.time() + 15
    while not any(j["title"] == title and j.get("stage") == "downloading" for j in jobs.snapshot()["jobs"]):
        assert time.time() < end, f"{title} never started downloading"
        time.sleep(0.1)
    with jobs.lock:
        job = next(j for j in jobs.jobs if j["title"] == title)
        job.update({"stage": "processing", "speed": None, "eta": None, **step})
        jobs._changed(job)


def test_a_processing_step_shows_its_words_percentage_speed_and_time_left(app, spool):
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    processing(spool, "Fake slow video", what="join", downloaded=5_300_000, total=10_000_000, speed=9_100_000, eta=1860)
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.locator(".meta")).to_have_text("Joining video and audio53%9.1 MB/s31 min left")
    pct = row.get_by_role("progressbar").get_attribute("value")
    assert row.locator(".counter").inner_text().replace("\n", "") == f"{pct}%"  # still the whole job's progress


def test_a_processing_step_from_an_older_server_looks_as_before(app, spool):
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    processing(spool, "Fake slow video", part=2, what="video")  # the download's own numbers stay behind
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.locator(".meta")).to_have_text("Joining video and audio")


def test_a_tab_seen_again_asks_for_a_fresh_snapshot(app):
    with app.expect_request("**/api/events"):
        app.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def test_cancel_a_running_download_then_resume_it(app):
    download(app, "https://fake.test/slow", "Fake slow video")
    active = group(app, "Downloading")
    expect(active.get_by_role("progressbar", name="Progress of Fake slow video")).to_be_visible(timeout=10000)
    expect(active.get_by_text("Downloading video")).to_be_visible()
    active.get_by_role("button", name="Cancel").click()

    stopped = group(app, "Stopped")
    expect(stopped.get_by_text("Cancelled")).to_be_visible()
    expect(stopped.get_by_role("button", name="Resume")).to_be_focused()
    stopped.get_by_role("button", name="Resume").click()
    expect(group(app, "Downloading").get_by_text("Fake slow video")).to_be_visible()


# The finish

HIDDEN_TAB = """
window.__notes = []; window.__hidden = false;
Object.defineProperty(Document.prototype, 'hidden', { get() { return window.__hidden; }, configurable: true });
window.Notification = class { constructor(title, o) { window.__notes.push([title, o && o.body]); } };
Notification.permission = 'default';
Notification.requestPermission = async () => { Notification.permission = 'granted'; return 'granted'; };
"""


def with_hidden_tab(page):
    page.add_init_script(HIDDEN_TAB)
    page.reload()
    page.get_by_label("Link to download").wait_for()


def test_the_row_that_just_finished_is_tinted_for_two_seconds(app):
    download(app, VIDEO, "Fake video 1")
    row = group(app, "Finished").get_by_role("listitem")
    expect(row).to_have_class(re.compile(r"\bflash\b"), timeout=15000)
    expect(row).not_to_have_class(re.compile(r"\bflash\b"), timeout=4000)


def test_a_download_that_ends_in_a_hidden_tab_says_finished_and_notifies(app):
    with_hidden_tab(app)
    expect(app.get_by_text("Get a notification when downloads finish.")).to_have_count(0)  # never on load
    download(app, "https://fake.test/slow?delay=0.3", "Fake slow video")
    ask = app.locator(".ask")
    expect(ask).to_contain_text("Get a notification when downloads finish.")
    ask.get_by_role("button", name="Turn on").click()
    expect(ask).to_be_hidden()
    app.evaluate("window.__hidden = true")
    expect(app).to_have_title("Finished - Spool", timeout=15000)
    assert app.evaluate("window.__notes") == [["Finished", "Fake slow video"]]
    app.evaluate("window.__hidden = false; document.dispatchEvent(new Event('visibilitychange'))")
    expect(app).to_have_title("Spool")


def test_no_thanks_is_remembered(app):
    with_hidden_tab(app)
    download(app, VIDEO, "Fake video 1")
    app.locator(".ask").get_by_role("button", name="No thanks").click()
    expect(app.locator(".ask")).to_have_count(0)
    download(app, "https://fake.test/video/2", "Fake video 2")
    app.wait_for_timeout(300)
    expect(app.locator(".ask")).to_have_count(0)
    app.get_by_role("button", name="Settings", exact=True).click()
    expect(app.get_by_role("checkbox", name="Get a notification when downloads finish")).not_to_be_checked()


# The list


def test_remove_takes_the_row_off_at_once_and_undo_puts_it_back(app, spool):
    add(spool, [("https://fake.test/403", "Fake refused video")])
    stopped = group(app, "Stopped")
    stopped.get_by_role("button", name="Remove from list").click(timeout=15000)
    undo = app.get_by_role("button", name="Undo", exact=True)
    expect(undo).to_be_focused()
    expect(app.get_by_role("status").filter(has_text="Removed.")).to_be_visible()
    expect(app.get_by_text("Downloads show up here. Paste a link above to start.")).to_be_visible()
    assert any(j["title"] == "Fake refused video" for j in spool[1].snapshot()["jobs"])  # not sent yet
    undo.click()
    expect(stopped.get_by_text("Fake refused video")).to_be_visible()

    stopped.get_by_role("button", name="Remove from list").click()
    expect(undo).to_be_hidden(timeout=7000)
    expect(app.get_by_label("Link to download")).to_be_focused()
    assert not any(j["title"] == "Fake refused video" for j in spool[1].snapshot()["jobs"])


def test_remove_a_finished_download_from_its_menu(app):
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished")
    finished.get_by_role("button", name="More actions for Fake video 1").click(timeout=15000)
    expect(finished.get_by_role("button", name="Download again")).to_be_visible()
    finished.get_by_role("button", name="Remove from list").click()
    expect(app.get_by_text("Downloads show up here. Paste a link above to start.")).to_be_visible()
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_focused()


def test_finished_shows_ten_then_show_all_and_clear_list_keeps_the_files(app, spool, tmp_path):
    add(spool, [(f"https://fake.test/video/{n}", f"Fake video {n}") for n in range(1, 13)])
    finished = group(app, "Finished")
    expect(finished.get_by_role("button", name="Show all 12")).to_be_visible(timeout=30000)
    expect(finished.get_by_role("listitem")).to_have_count(10)
    finished.get_by_role("button", name="Show all 12").click()
    expect(finished.get_by_role("listitem")).to_have_count(12)

    files = sorted(p.name for p in (tmp_path / "downloads").iterdir())
    finished.get_by_role("button", name="Clear list").click()
    expect(app.get_by_role("status").filter(has_text="Cleared 12.")).to_be_visible()
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_focused()
    expect(finished).to_have_count(0)
    app.get_by_role("button", name="Undo", exact=True).click()
    expect(group(app, "Finished").get_by_role("listitem")).to_have_count(12)

    group(app, "Finished").get_by_role("button", name="Clear list").click()
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_hidden(timeout=7000)
    expect(app.get_by_text("Downloads show up here. Paste a link above to start.")).to_be_visible()
    assert not [j for j in spool[1].snapshot()["jobs"] if j["state"] == "done"]
    assert sorted(p.name for p in (tmp_path / "downloads").iterdir()) == files


def test_resume_all_when_more_than_one_row_can_resume(app, spool):
    jobs = add(spool, [(f"https://fake.test/slow/{n}?delay=2", f"Slow {n}") for n in (1, 2)])
    for j in jobs:
        spool[1].cancel(j["id"])
    stopped = group(app, "Stopped")
    stopped.get_by_role("button", name="Resume all").click(timeout=10000)
    expect(group(app, "Downloading").get_by_role("listitem")).to_have_count(2)


def test_adding_a_video_already_in_the_list_tints_its_row(app, spool):
    download(app, VIDEO, "Fake video 1")
    server_job(spool, "Fake video 1", "done")
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished").get_by_role("listitem")
    expect(finished).to_have_class(re.compile(r"\bflash\b"))  # the tint lasts two seconds, so look at it first
    expect(app.get_by_role("status").filter(has_text="already in the list")).to_be_visible()
    expect(finished).to_have_count(1)


def test_several_links_say_how_many_were_already_in_the_list(app, spool):
    download(app, VIDEO, "Fake video 1")
    server_job(spool, "Fake video 1", "done")
    app.locator(".queue").click()
    paste(app, f"{VIDEO} https://fake.test/video/2 https://fake.test/video/3")
    expect(app.get_by_role("status").filter(has_text="Added 2 downloads. 1 was already in the list.")).to_be_visible()


# The folder


def test_the_folder_line_opens_the_folder_and_changes_it(app, spool, tmp_path):
    dest = spool[1].snapshot()["dest"]
    line = app.locator(".dest")
    expect(line).to_contain_text("Saving to")
    expect(line).to_contain_text("free")
    name = line.get_by_role("button", name=dest["display"])
    expect(name).to_have_attribute("title", dest["path"])
    with app.expect_request("**/api/dest/reveal"):
        name.click()
    assert app.native == ["dest/reveal"]

    line.get_by_role("button", name="Change").click()  # the test server has no native folder dialog
    expect(app.get_by_role("dialog", name="Settings").get_by_label("Folder")).to_be_focused()


def test_a_missing_folder_blocks_download_and_says_how_to_fix_it(app, spool, tmp_path):
    spool[1].update_settings({"dest": str(tmp_path / "gone")})
    problem = app.get_by_role("alert").filter(has_text="gone doesn't exist. Choose another folder.")
    expect(problem).to_be_visible()
    expect(problem.get_by_role("button", name="Try again")).to_be_visible()
    expect(problem.get_by_role("button", name="Change folder")).to_be_visible()

    look_up(app, VIDEO)
    button = tray(app, "Fake video 1").get_by_role("button", name="Download", exact=True)
    expect(button).to_be_disabled()
    expect(button).to_have_accessible_description(re.compile("gone doesn't exist"))


def test_a_file_chosen_as_the_folder_says_so(app, spool, tmp_path):
    (tmp_path / "notes.txt").write_text("not a folder")
    spool[1].update_settings({"dest": str(tmp_path / "notes.txt")})
    expect(app.get_by_role("alert").filter(has_text="is a file, not a folder.")).to_be_visible()


# A folder on a drive that is not there, in each system's own form.
UNPLUGGED = {"darwin": "/Volumes/NoSuchDrive/Spool", "win32": "Q:\\Spool"}.get(
    sys.platform, "/media/nobody-here/NoSuchDrive/Spool"
)


def test_an_unplugged_drive_says_to_plug_it_in(app, spool):
    spool[1].update_settings({"dest": UNPLUGGED})
    expect(
        app.get_by_role("alert").filter(
            has_text=re.compile(r"isn't connected\. Plug it in, or choose another folder\.")
        )
    ).to_be_visible()


def test_a_drive_that_does_not_answer_blocks_download(app, monkeypatch):
    from spool import system

    release = threading.Event()
    real = system.probe
    monkeypatch.setattr(system, "probe", lambda path, write: (release.wait(), real(path, write))[1])  # asleep till then
    try:
        problem = app.get_by_role("alert").filter(
            has_text="isn't answering. If macOS is asking whether Spool may use it"
        )
        expect(problem).to_be_visible(timeout=15000)
        expect(problem.get_by_role("button", name="Try again")).to_be_visible()
        look_up(app, VIDEO)
        expect(tray(app, "Fake video 1").get_by_role("button", name="Download", exact=True)).to_be_disabled()
    finally:
        release.set()


def test_a_file_saved_to_another_folder_names_that_folder(app, spool, tmp_path):
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished")
    expect(finished.get_by_role("listitem")).to_have_count(1, timeout=15000)
    expect(finished.locator(".folder")).to_have_count(0)

    (tmp_path / "Elsewhere").mkdir()
    spool[1].update_settings({"dest": str(tmp_path / "Elsewhere")})
    expect(finished.get_by_role("listitem").get_by_text("downloads")).to_be_visible()


@pytest.mark.parametrize("size", [{"width": 320, "height": 568}, {"width": 720, "height": 450}])
def test_download_is_reachable_on_a_small_or_zoomed_screen(app, size):
    app.set_viewport_size(size)  # 720 x 450 is a 1440 x 900 window at 200% zoom
    look_up(app, VIDEO)
    expect(tray(app, "Fake video 1").get_by_role("button", name="Download", exact=True)).to_be_in_viewport()
    assert app.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


# Settings


def test_settings_save_a_typed_folder_and_create_it(app, tmp_path):
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_role("button", name=re.compile("Save"))).to_have_count(0)
    expect(dialog.get_by_label("Default quality")).to_have_count(0)
    folder = dialog.get_by_label("Folder")
    folder.fill(str(tmp_path / "missing"))
    folder.press("Tab")  # saves when the field loses focus
    expect(dialog.get_by_text("missing doesn't exist. Choose another folder.")).to_be_visible()
    dialog.get_by_role("button", name="Create this folder").click()
    expect(dialog.get_by_text(re.compile(r"missing, .* free"))).to_be_visible()
    assert (tmp_path / "missing").is_dir()

    folder.fill(str(tmp_path / "other"))
    folder.press("Enter")
    expect(dialog.get_by_text("other doesn't exist. Choose another folder.")).to_be_visible()
    app.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(app.get_by_role("alert").filter(has_text="other doesn't exist")).to_be_visible()


def test_settings_holds_the_bookmark_button_and_the_version(app, spool):
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_text("Save from any page. Drag this to your bookmarks bar:")).to_be_visible()
    link = dialog.get_by_role("link", name="Save with Spool")
    href = link.get_attribute("href")
    assert href.startswith("javascript:") and spool[0].split("/#")[0] + "/#url=" in href
    expect(dialog.get_by_text(re.compile(r"^Spool \d"))).to_be_visible()
    dialog.get_by_role("button", name="Check for updates").click()
    expect(dialog.get_by_text(re.compile("Checking…|Up to date"))).to_be_visible()
    expect(dialog.get_by_text("Up to date")).to_be_visible(timeout=15000)


def test_there_is_no_status_bar(app):
    expect(app.get_by_role("contentinfo")).to_have_count(0)
    expect(app.locator(".deck").get_by_role("button", name="Settings", exact=True)).to_be_visible()


# A link in the address


def test_a_link_in_the_address_waits_in_the_field(page, spool):
    probes = []
    page.on("request", lambda r: "/api/probe" in r.url and probes.append(r))
    page.goto(spool[0] + "&url=" + "https%3A%2F%2Ffake.test%2Fvideo")
    field = page.get_by_label("Link to download")
    expect(field).to_have_value(VIDEO)
    expect(field).to_be_focused()
    expect(page).to_have_url(re.compile(r"/$"))
    with page.expect_request("**/api/settings"):  # a request sent after any lookup would have been
        page.evaluate("fetch('/api/settings', {method: 'PUT', headers: {'X-Spool-Token': 'fake-token'}})")
    assert probes == []  # any page can open this address, so Spool never fetches the link by itself
    page.evaluate("location.hash = '#url=' + encodeURIComponent('https://fake.test/video/2 https://fake.test/audio')")
    expect(field).to_have_value("https://fake.test/video/2 https://fake.test/audio")
    field.press("Enter")
    expect(page.get_by_role("status").filter(has_text="Added 2 downloads.")).to_be_visible()


# Signed out, first run and reconnecting


def test_a_tab_with_an_unknown_token_says_how_to_sign_in(page, spool):
    page.goto(spool[0].replace("#t=fake-token", "#t=wrong"))
    expect(
        page.get_by_text("This tab lost its link to Spool. Run spool in your terminal to open a new one.")
    ).to_be_visible()
    expect(page).to_have_url(re.compile(r"/$"))  # the token left the address bar


def test_a_first_run_that_cannot_fetch_the_engine_says_so(page, tmp_path):
    httpd, url, jobs, _ = serve_fake.start(tmp_path, fresh=True, github=serve_fake.FakeGitHub(offline=True))
    try:
        page.goto(url)
        strip = page.get_by_role("complementary", name="Engine")
        expect(strip.get_by_role("alert")).to_be_visible(timeout=15000)
        errors = {e["error"] for e in jobs.snapshot()["engines"].values()} - {None}
        expect(strip.get_by_role("alert")).to_have_text(errors.pop())  # the server's sentence
        expect(strip.get_by_role("button", name="Try again")).to_be_visible()
        expect(page.get_by_text("Setting up Spool.")).to_have_count(0)
        assert_no_program_names(page, "engine strip")
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()


def test_a_tab_reconnects_after_spool_restarts(page, tmp_path):
    httpd, url, jobs, _ = serve_fake.start(tmp_path)
    port = httpd.server_address[1]
    page.goto(url)
    page.get_by_label("Link to download").wait_for()
    httpd.shutdown()
    httpd.server_close()
    vanish(jobs)  # gone without a word, as from a crash
    jobs.close()

    httpd, url, jobs, _ = serve_fake.start(tmp_path, port=port)
    try:
        jobs.add([{"url": VIDEO, "title": "Fake video 1"}], "best")  # reaches the page only through a new stream
        expect(group(page, "Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)
        look_up(page, "https://fake.test/video/2")  # the token kept from the first load still works
        expect(page.get_by_role("region", name="Fake video 2")).to_be_visible()
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()


# Review round: each test below went red on the page as it was at 93de052


def clear_requests(page):
    """The ids each Clear sent to the server, as they go out."""
    sent = []
    page.on("request", lambda r: sent.append(r.post_data_json["ids"]) if r.url.endswith("/api/jobs/clear") else None)
    return sent


def test_a_large_playlist_is_added_in_one_go(app):
    def many(route):
        response = route.fetch()
        body = response.json()
        body["entries"] = [dict(body["entries"][0], url=f"https://fake.test/video/{n}", id=f"v{n}") for n in range(150)]
        route.fulfill(response=response, json=body)

    app.route("**/api/probe", many)
    look_up(app, "https://fake.test/playlist")
    with app.expect_request("**/api/jobs") as sent:
        tray(app, "Fake playlist").get_by_role("button", name="Download 150").click()
    assert "presets" not in sent.value.post_data
    expect(app.get_by_role("status").filter(has_text="Added 150 downloads.")).to_be_visible()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot delete a folder holding an open file")
def test_a_download_stopped_by_an_unplugged_drive_can_be_tried_again(app, spool, tmp_path):
    import shutil

    drive = tmp_path / "Drive"
    drive.mkdir()
    spool[1].update_settings({"dest": str(drive)})
    jobs = add(spool, [(f"https://fake.test/slow/{n}?delay=0.2", f"Slow {n}") for n in (1, 2)])
    server_job(spool, "Slow 1", "running")
    server_job(spool, "Slow 2", "running")
    shutil.rmtree(drive)  # the drive is unplugged mid-download
    stopped = group(app, "Stopped")
    expect(stopped.get_by_role("listitem")).to_have_count(2, timeout=15000)
    assert {j["error"]["hint"] for j in spool[1].snapshot()["jobs"]} == {"check_dest"}
    expect(stopped.get_by_role("button", name="Change folder")).to_have_count(2)
    drive.mkdir()  # plugged back in
    stopped.get_by_role("button", name="Try again").first.click()
    expect(group(app, "Downloading").get_by_role("listitem")).to_have_count(1)
    group(app, "Stopped").get_by_role("button", name="Try again").click()
    expect(group(app, "Finished").get_by_role("listitem")).to_have_count(2, timeout=30000)
    assert len(jobs) == 2


def test_eight_tabs_all_keep_working_and_a_hidden_one_hears_the_finish(page, spool):
    tabs = [page] + [page.context.new_page() for _ in range(7)]
    for tab in tabs:
        tab.goto(spool[0])
        expect(tab.get_by_label("Link to download")).to_be_visible()
    look_up(tabs[-1], VIDEO)
    expect(tray(tabs[-1], "Fake video 1")).to_be_visible()
    add(spool, [("https://fake.test/video/5", "Fake video 5")])
    for tab in tabs:
        expect(group(tab, "Finished").get_by_text("Fake video 5")).to_be_visible(timeout=15000)
    tabs[0].close()  # the tab holding the stream goes; another takes over
    add(spool, [("https://fake.test/video/6", "Fake video 6")])
    for tab in tabs[1:]:
        expect(group(tab, "Finished").get_by_text("Fake video 6")).to_be_visible(timeout=15000)


def test_a_refused_token_never_replaces_the_kept_one(page, spool):
    origin = spool[0].split("#")[0]
    page.goto(spool[0])
    expect(page.get_by_label("Link to download")).to_be_visible()
    page.goto("about:blank")  # a new load, not a fragment change
    page.goto(origin + "#t=x")  # any website can open this address
    expect(page.get_by_text("This tab lost its link to Spool.", exact=False)).to_be_visible()
    page.goto("about:blank")
    page.goto(origin)
    expect(page.get_by_label("Link to download")).to_be_visible()


def test_a_second_tab_with_a_token_spool_refuses_says_so_too(page, context, spool):
    origin = spool[0].split("#")[0]
    page.goto(origin + "#t=stale")
    expect(page.get_by_text("This tab lost its link to Spool.", exact=False)).to_be_visible()
    second = context.new_page()  # joins the first tab's stream, which has already been refused
    second.goto(origin + "#t=stale")
    expect(second.get_by_text("This tab lost its link to Spool.", exact=False)).to_be_visible()


def test_a_tab_from_an_earlier_launch_works_again_once_a_new_tab_is_let_in(page, context, spool):
    origin = spool[0].split("#")[0]
    page.goto(origin + "#t=stale")  # as a tab left open across a restart: its token opens nothing now
    expect(page.get_by_text("This tab lost its link to Spool.", exact=False)).to_be_visible()
    newer = context.new_page()
    newer.goto(spool[0])  # the tab the new launch opens
    expect(newer.get_by_label("Link to download")).to_be_visible()
    expect(page.get_by_label("Link to download")).to_be_visible()


def test_clear_sends_only_the_rows_it_took_off(app, spool):
    sent = clear_requests(app)
    add(spool, [("https://fake.test/video/1", "Fake video 1")])
    finished = group(app, "Finished")
    expect(finished.get_by_role("listitem")).to_have_count(1, timeout=15000)
    cleared = [j["id"] for j in spool[1].snapshot()["jobs"]]
    finished.get_by_role("button", name="Clear list").click()
    add(spool, [("https://fake.test/video/2", "Fake video 2")])
    server_job(spool, "Fake video 2", "done")  # finishes inside Undo's five seconds
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_hidden(timeout=7000)
    expect(group(app, "Finished").get_by_text("Fake video 2")).to_be_visible()
    assert sent == [cleared]
    assert [j["title"] for j in spool[1].snapshot()["jobs"]] == ["Fake video 2"]


def test_a_removed_row_that_another_tab_starts_again_comes_back(app, spool):
    add(spool, [("https://fake.test/slow?delay=0.3", "Fake slow video")])
    finished = group(app, "Finished")
    finished.get_by_role("button", name="More actions for Fake slow video").click(timeout=15000)
    finished.get_by_role("button", name="Remove from list").click()
    job = server_job(spool, "Fake slow video", "done")
    spool[1].retry(job["id"])  # another tab chose Download again
    expect(app.get_by_role("listitem").filter(has_text="Fake slow video")).to_be_visible()  # running, or done again
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_hidden()


def test_focus_left_on_the_page_stays_there(app, spool):
    add(spool, [("https://fake.test/broken", "Fake broken video")])
    stopped = group(app, "Stopped")
    stopped.get_by_role("button", name="Show log").click(timeout=15000)
    app.mouse.click(5, app.viewport_size["height"] - 5)  # blank space
    assert app.evaluate("document.activeElement === document.body")
    add(spool, [("https://fake.test/video/3", "Fake video 3")])
    expect(group(app, "Finished").get_by_text("Fake video 3")).to_be_visible(timeout=15000)
    assert app.evaluate("document.activeElement === document.body")


def test_a_link_from_the_address_is_selected_so_a_paste_replaces_it(page, spool):
    page.goto(spool[0] + "&url=" + "https%3A%2F%2Ffake.test%2Fvideo%2F7")
    field = page.get_by_label("Link to download")
    expect(field).to_have_value("https://fake.test/video/7")
    expect(field).to_be_focused()
    assert field.evaluate("e => [e.selectionStart, e.selectionEnd]") == [0, len("https://fake.test/video/7")]
    with page.expect_request("**/api/probe") as sent:
        page.keyboard.insert_text("https://fake.test/video/2")  # what a paste puts in the field
        page.evaluate("document.getElementById('link').dispatchEvent(new Event('paste', {bubbles: true}))")
    assert sent.value.post_data_json["url"] == "https://fake.test/video/2"


def test_cmd_enter_on_a_playlist_opens_the_preview(app):
    requests = []
    app.on("request", lambda r: r.url.endswith("/api/jobs") and requests.append(r))
    field = app.get_by_label("Link to download")
    field.fill("https://fake.test/playlist")
    field.press("ControlOrMeta+Enter")
    expect(tray(app, "Fake playlist")).to_be_visible()
    assert requests == []


def test_a_row_action_that_fails_says_why_on_the_row(app, spool):
    add(spool, [("https://fake.test/broken", "Fake broken video")])
    stopped = group(app, "Stopped")
    app.route(
        "**/api/jobs/*/retry",
        lambda route: route.fulfill(
            status=409, json={"error": {"kind": "engine_not_ready", "message": "The server's words.", "hint": None}}
        ),
    )
    stopped.get_by_role("button", name="Try again").click(timeout=15000)
    expect(stopped.get_by_role("listitem").get_by_text("The server's words.")).to_be_visible()


@pytest.mark.parametrize("spool", [{"github": serve_fake.FakeGitHub(offline=True)}], indirect=True)
def test_the_engine_strip_shows_the_servers_sentence(app, spool):
    strip = app.get_by_role("complementary", name="Engine")
    expect(strip).to_be_visible(timeout=15000)
    errors = {e["error"] for e in spool[1].snapshot()["engines"].values()} - {None}
    expect(strip.get_by_role("alert")).to_have_text(errors.pop())


def test_a_stalled_stream_shows_reconnecting(app, spool, monkeypatch):
    from spool import server

    app.clock.install()
    download(app, "https://fake.test/slow?delay=2", "Fake slow video")
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.get_by_text("Downloading video")).to_be_visible(timeout=10000)
    monkeypatch.setattr(server, "PING", 3600)
    with spool[1].lock:
        spool[1].subscribers.clear()  # Spool is suspended: the stream stays open and says nothing
    app.clock.run_for(60_000)
    expect(row.get_by_text("Reconnecting…")).to_be_visible()


BLOCKED_NOTIFICATIONS = """
window.Notification = class {};
Notification.permission = 'default';
Notification.requestPermission = async () => { Notification.permission = 'denied'; return 'denied'; };
"""


def test_notifications_the_browser_blocks_say_so(app):
    app.add_init_script(BLOCKED_NOTIFICATIONS)
    app.reload()
    app.get_by_role("button", name="Settings", exact=True).click()
    switch = app.get_by_role("checkbox", name="Get a notification when downloads finish")
    switch.click()
    expect(switch).not_to_be_checked()
    expect(switch).to_be_disabled()
    expect(app.get_by_role("dialog").get_by_text("Your browser blocks notifications from Spool.")).to_be_visible()


def test_update_and_try_again_runs_the_lookup_after_the_update(app):
    probe_answers(app, "blocked", "update_engine")
    look_up(app, VIDEO)
    app.unroute("**/api/probe")
    with app.expect_request("**/api/probe"):
        app.get_by_role("button", name="Update and try again").click()
    expect(tray(app, "Fake video 1")).to_be_visible()


def test_two_removes_each_keep_their_five_seconds(app, spool):
    add(spool, [("https://fake.test/403", "Fake refused video"), ("https://fake.test/broken", "Fake broken video")])
    stopped = group(app, "Stopped")
    expect(stopped.get_by_role("listitem")).to_have_count(2, timeout=15000)
    started = time.time()
    stopped.get_by_role("button", name="Remove from list").first.click()
    app.wait_for_timeout(1000)
    with app.expect_request(lambda r: r.method == "DELETE", timeout=7000):
        stopped.get_by_role("button", name="Remove from list").click()
    assert time.time() - started > 4.5


def test_a_page_hidden_with_undo_showing_sends_and_drops_the_undo(app, spool):
    add(spool, [("https://fake.test/403", "Fake refused video")])
    with app.expect_request(lambda r: r.method == "DELETE"):
        group(app, "Stopped").get_by_role("button", name="Remove from list").click(timeout=15000)
        app.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    expect(app.get_by_role("button", name="Undo", exact=True)).to_be_hidden()


def test_the_bookmark_link_leaves_the_opening_site_no_handle(page, spool, context):
    page.goto(spool[0])
    page.get_by_role("button", name="Settings", exact=True).click()
    code = page.get_by_role("link", name="Save with Spool").get_attribute("href").removeprefix("javascript:")
    other = context.new_page()
    other.goto(spool[0].replace("127.0.0.1", "localhost").split("#")[0] + "icon.svg")  # another origin
    with context.expect_page() as opened:
        handle = other.evaluate(f"() => {{ {code}; return window.__last ?? null }}")
    assert handle is None
    assert opened.value.evaluate("window.opener") is None


def test_the_notice_and_undo_words_reach_a_screen_reader(app, spool):
    assert app.locator(".notice").evaluate("e => getComputedStyle(e).display") != "none"
    add(spool, [("https://fake.test/403", "Fake refused video")])
    group(app, "Stopped").get_by_role("button", name="Remove from list").click(timeout=15000)
    expect(app.locator("[aria-live=polite]")).to_have_text("Removed.")


@pytest.mark.parametrize("spool", [{"ffmpeg": False}], indirect=True)
def test_a_copy_the_browser_refuses_selects_the_command(app):
    app.add_init_script("navigator.clipboard.writeText = () => Promise.reject(new Error('denied'))")
    app.reload()
    strip = app.get_by_role("complementary", name="Free helper")
    strip.get_by_role("button", name="Copy command").click()
    expect(strip.get_by_role("button", name=re.compile(r"^Press (Cmd|Ctrl)\+C$"))).to_be_visible()
    assert app.evaluate("getSelection().toString()") == strip.locator("code").inner_text()


# Spool 0.2.0


def fresh_snapshot(page):
    with page.expect_request("**/api/events"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def newer_spool(page, spool, version):
    """Spool's look at the releases found a newer one."""
    spool[1].newer = version
    fresh_snapshot(page)


def older_server(page, spool):
    """The page as Spool 0.1 serves it: no `update` in the snapshot, no `ext` on a preset."""
    jobs = spool[1]
    plain = jobs.snapshot
    jobs.snapshot = lambda *a, **k: {k: v for k, v in plain(*a, **k).items() if k != "update"}

    def no_ext(route):
        response = route.fetch()
        body = response.json()
        for e in body["entries"]:
            for p in e.get("presets") or []:
                p.pop("ext", None)
        route.fulfill(response=response, json=body)

    page.route("**/api/probe", no_ext)
    fresh_snapshot(page)


def with_ext(route):
    response = route.fetch()
    body = response.json()
    for p in body["entries"][0]["presets"]:
        p["ext"] = None if p["id"] == "audio" else "mkv" if p["id"] == "best" else "mp4"
    route.fulfill(response=response, json=body)


def test_each_quality_says_the_type_of_file_it_saves(app):
    app.route("**/api/probe", with_ext)
    look_up(app, VIDEO)
    quality = tray(app, "Fake video 1").get_by_role("group", name="Quality")
    best = quality.locator(".tile").filter(has=app.get_by_role("radio", name=re.compile("^Best")))
    expect(best.locator(".tile-size")).to_have_text(re.compile(r"^\d+(\.\d)? (KB|MB|GB) MKV$"))
    expect(quality.get_by_role("radio", name=re.compile(r"^720p .* MP4$"))).to_be_visible()
    expect(quality.locator(".tile").filter(has_text="Audio").locator(".tile-size")).to_have_text(
        re.compile(r"^\d+(\.\d)? (KB|MB)$")  # no type known: the size alone, as before
    )


def test_a_tile_from_an_older_server_shows_only_its_size(app, spool):
    older_server(app, spool)
    look_up(app, VIDEO)
    expect(tray(app, "Fake video 1")).to_be_visible()
    sizes = tray(app, "Fake video 1").locator(".tile-size").all_inner_texts()
    assert sizes and all(re.fullmatch(r"\d+(\.\d)? (KB|MB|GB)", s) for s in sizes), sizes


def test_a_download_trying_again_by_itself_says_why_and_when(app, spool):
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.get_by_role("progressbar")).to_have_attribute("value", re.compile(r"^\d+$"), timeout=10000)
    processing(spool, "Fake slow video", stage="waiting", note="The connection dropped.", eta=28, attempt=2, attempts=5)
    expect(row.locator(".meta")).to_have_text("The connection dropped.Trying again in 28 s")
    pct = row.get_by_role("progressbar").get_attribute("value")
    assert row.locator(".counter").inner_text().replace("\n", "") == f"{pct}%"  # the job's progress, kept
    expect(row.get_by_role("alert")).to_have_count(0)
    assert "danger" not in row.evaluate("e => e.innerHTML")
    expect(row.get_by_role("button", name="Cancel")).to_be_visible()
    expect(app).to_have_title(f"{pct}% - Spool")


@pytest.mark.parametrize("spool", [{"waits": (30,)}], indirect=True)
def test_a_refused_download_waits_to_try_again_and_can_be_cancelled(app, spool):
    add(spool, [("https://fake.test/403", "Fake refused video")])
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.locator(".meta")).to_have_text(re.compile(r"^The site refused the download\.Trying again in \d+ s$"))
    expect(row.get_by_role("alert")).to_have_count(0)
    row.get_by_role("button", name="Cancel").click()
    expect(group(app, "Stopped").get_by_text("Cancelled")).to_be_visible()


def test_a_running_row_from_an_older_server_looks_as_before(app, spool):
    older_server(app, spool)
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    processing(spool, "Fake slow video", stage="downloading", downloaded=4_000_000, speed=2_000_000, eta=12)
    expect(group(app, "Downloading").locator(".meta")).to_have_text("Downloading video2.0 MB/s12 s left")


def test_a_channel_with_more_videos_says_it_shows_the_newest(app):
    def channel(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "truncated": True})

    app.route("**/api/probe", channel)
    look_up(app, "https://fake.test/playlist")
    t = tray(app, "Fake playlist")
    expect(t.get_by_text("Showing the newest 3.")).to_have_count(1)
    app.unroute("**/api/probe")
    app.keyboard.press("Escape")
    look_up(app, "https://fake.test/playlist")
    expect(tray(app, "Fake playlist").get_by_text(re.compile("^Showing"))).to_have_count(0)


def test_settings_offers_a_newer_spool_with_its_command(app, spool):
    newer_spool(app, spool, "0.2.1")
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_text("Spool 0.2.1 is ready.")).to_be_visible()
    expect(dialog.locator("code")).to_have_text("uv tool upgrade spool-dl")
    expect(dialog.get_by_role("button", name="Copy command")).to_be_visible()
    expect(dialog.get_by_text("When it finishes, quit Spool and start it again.")).to_be_visible()


def test_settings_from_an_older_server_look_as_before(app, spool):
    older_server(app, spool)
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_role("button", name="Done")).to_be_visible()
    expect(dialog.get_by_text(re.compile("is ready"))).to_have_count(0)
    expect(dialog.get_by_role("button", name="Quit Spool")).to_have_count(0)
    expect(dialog.locator("code")).to_have_count(0)


def test_quit_with_nothing_running_quits_at_once(app, spool):
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_text(re.compile("is ready"))).to_have_count(0)
    with app.expect_request("**/api/quit"):
        dialog.get_by_role("button", name="Quit Spool").click()
    expect(dialog).to_be_hidden()
    assert app.native == ["quit"]


def test_quit_while_downloading_asks_first_in_the_page(app, spool):
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    expect(group(app, "Downloading").get_by_role("progressbar")).to_be_visible(timeout=10000)
    app.on("dialog", lambda d: pytest.fail(f"a browser dialog: {d.message}"))
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    dialog.get_by_role("button", name="Quit Spool").click()
    expect(dialog.get_by_text("Downloads running now stop. You can resume them next time.")).to_be_visible()
    expect(dialog.get_by_role("button", name="Quit now")).to_be_focused()
    dialog.get_by_role("button", name="Keep running").click()
    expect(dialog.get_by_text("Downloads running now stop.", exact=False)).to_have_count(0)
    expect(dialog.get_by_role("button", name="Quit Spool")).to_be_focused()
    assert app.native == []
    dialog.get_by_role("button", name="Quit Spool").click()
    with app.expect_request("**/api/quit"):
        dialog.get_by_role("button", name="Quit now").click()
    expect(dialog).to_be_hidden()


def test_open_a_finished_file_from_its_menu(app, spool):
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished")
    finished.get_by_role("button", name="More actions for Fake video 1").click(timeout=15000)
    menu = finished.locator(".menu")
    expect(menu.get_by_role("button").first).to_have_text("Open")
    job = server_job(spool, "Fake video 1", "done")
    menu.get_by_role("button", name="Open", exact=True).click()
    expect(menu).to_be_hidden()
    assert app.native == [f"jobs/{job['id']}/open"]


def test_a_file_that_will_not_open_says_why_on_the_row(app, spool):
    app.route(
        "**/api/jobs/*/open",
        lambda route: route.fulfill(
            status=410, json={"error": {"kind": "gone", "message": "The server's words.", "hint": None}}
        ),
    )
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished")
    finished.get_by_role("button", name="More actions for Fake video 1").click(timeout=15000)
    finished.get_by_role("button", name="Open", exact=True).click()
    expect(finished.get_by_role("listitem").get_by_role("alert")).to_have_text("The server's words.")


def test_a_finished_row_from_an_older_server_has_no_open(app, spool):
    older_server(app, spool)
    download(app, VIDEO, "Fake video 1")
    finished = group(app, "Finished")
    finished.get_by_role("button", name="More actions for Fake video 1").click(timeout=15000)
    expect(finished.locator(".menu").get_by_role("button")).to_have_text(["Download again", "Remove from list"])


def test_after_quit_the_tab_says_spool_stopped_and_how_to_start_it(app, spool):
    add(spool, [("https://fake.test/slow?delay=60", "Fake slow video")])
    expect(group(app, "Downloading").get_by_role("progressbar")).to_be_visible(timeout=10000)
    app.get_by_role("button", name="Settings", exact=True).click()
    dialog = app.get_by_role("dialog", name="Settings")
    dialog.get_by_role("button", name="Quit Spool").click()
    dialog.get_by_role("button", name="Quit now").click()
    expect(
        app.get_by_text("Spool has stopped. To start it again, run spool in your terminal. It opens a new tab.")
    ).to_be_visible()
    spool[1].close()  # the stream ends, as it does when Spool stops
    app.wait_for_timeout(500)
    expect(app.get_by_text("Reconnecting…")).to_have_count(0)
    expect(app.get_by_label("Link to download")).to_have_count(0)
    expect(app.get_by_role("button")).to_have_count(0)


STOPPED = "Spool has stopped. To start it again, run spool in your terminal. It opens a new tab."


def ending_stream(page, spool):
    """Every request for the event stream gets a snapshot, then the stream ends with no word. Returns the requests."""
    made = []

    def answer(route):
        made.append(route.request.url)
        route.fulfill(
            content_type="text/event-stream", body=f"event: snapshot\ndata: {json.dumps(spool[1].snapshot())}\n\n"
        )

    page.context.route("**/api/events", answer)
    fresh_snapshot(page)
    return made


def test_a_clean_stop_stops_every_tab_reconnecting(app, spool):
    made = []
    app.context.on("request", lambda r: "/api/events" in r.url and made.append(r))
    other = app.context.new_page()
    other.goto(spool[0])
    other.get_by_label("Link to download").wait_for()
    spool[1].close()  # a clean stop: every stream hears `stopped`
    expect(app.get_by_text(STOPPED)).to_be_visible()
    expect(other.get_by_text(STOPPED)).to_be_visible()  # it does not read the stream itself
    asked = len(made)
    app.close()  # the other tab takes the lock, and must not open the stream again
    other.wait_for_timeout(2500)
    assert len(made) == asked
    expect(other.get_by_text("Reconnecting…")).to_have_count(0)


def test_a_stopped_tab_follows_the_tab_of_the_next_launch(page, context, tmp_path, monkeypatch):
    from spool import server

    httpd, url, jobs, _ = serve_fake.start(tmp_path)
    port = httpd.server_address[1]
    page.goto(url)
    page.get_by_label("Link to download").wait_for()
    jobs.close()
    httpd.shutdown()
    httpd.server_close()
    expect(page.get_by_text(STOPPED)).to_be_visible()

    made = []
    page.on("request", lambda r: "/api/events" in r.url and made.append(r))
    real = server.make_server
    monkeypatch.setattr(server, "make_server", lambda *a, **k: real(*a, **{**k, "token": "next-launch"}))
    httpd, url, jobs, _ = serve_fake.start(tmp_path, port=port)  # each launch has a new token
    try:
        page.wait_for_timeout(2500)
        assert made == []  # a stopped tab never tries by itself
        newer = context.new_page()
        newer.goto(url.replace("fake-token", "next-launch"))
        expect(newer.get_by_label("Link to download")).to_be_visible()
        expect(page.get_by_label("Link to download")).to_be_visible()
        jobs.add([{"url": VIDEO, "title": "Fake video 1"}], "best")
        expect(group(page, "Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()


def test_a_stream_that_just_ends_keeps_reconnecting(app, spool):
    made = ending_stream(app, spool)
    app.wait_for_timeout(2500)
    assert len(made) >= 2
    expect(app.get_by_text(STOPPED)).to_have_count(0)
    expect(app.get_by_label("Link to download")).to_be_visible()


def test_after_quit_the_stream_is_not_asked_for_again(app, spool):
    made = []
    app.on("request", lambda r: "/api/events" in r.url and made.append(r))
    app.get_by_role("button", name="Settings", exact=True).click()
    app.get_by_role("dialog", name="Settings").get_by_role("button", name="Quit Spool").click()
    expect(app.get_by_text(STOPPED)).to_be_visible()
    asked = len(made)
    vanish(spool[1])  # the stream ends without a word, as from a server that stopped before saying so
    app.wait_for_timeout(2500)
    assert len(made) == asked


def test_two_videos_added_from_the_page_download_at_the_same_time(app, spool):
    def slow(route):
        response = route.fetch()
        body = response.json()
        for n, e in enumerate(body["entries"], 1):
            e.update(url=f"https://fake.test/slow/{n}?delay=0.5", id=f"slow-{n}")
        route.fulfill(response=response, json=body)

    app.route("**/api/probe", slow)
    look_up(app, "https://fake.test/playlist")
    t = tray(app, "Fake playlist")
    t.get_by_role("checkbox", name=re.compile("Fake video 3")).uncheck()
    t.get_by_role("button", name="Download 2").click()
    end = time.time() + 15
    while sum(j["state"] == "running" for j in spool[1].snapshot()["jobs"]) < 2:
        assert time.time() < end, "the two never ran together"
        time.sleep(0.05)
    expect(group(app, "Downloading").get_by_role("progressbar")).to_have_count(2)
