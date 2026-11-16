"""The main flows a person uses, through the real server and the fake engine."""

import re
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
    """Add jobs straight through the server, as another tab would. Returns the jobs."""
    res = spool[1].add([{"url": u, "title": t} for u, t in links], preset)
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
    held[0].continue_()
    app.wait_for_timeout(1000)
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


def test_running_rows_say_reconnecting_when_spool_drops(app, spool):
    download(app, "https://fake.test/slow?delay=2", "Fake slow video")
    row = group(app, "Downloading").get_by_role("listitem")
    expect(row.get_by_text("Downloading video")).to_be_visible(timeout=10000)
    spool[1].close()  # ends every event stream
    expect(row.get_by_text("Reconnecting…")).to_be_visible()


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
    undo = app.get_by_role("button", name="Undo")
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
    expect(app.get_by_role("button", name="Undo")).to_be_focused()


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
    expect(app.get_by_role("button", name="Undo")).to_be_focused()
    expect(finished).to_have_count(0)
    app.get_by_role("button", name="Undo").click()
    expect(group(app, "Finished").get_by_role("listitem")).to_have_count(12)

    group(app, "Finished").get_by_role("button", name="Clear list").click()
    expect(app.get_by_role("button", name="Undo")).to_be_hidden(timeout=7000)
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
    expect(app.get_by_role("status").filter(has_text="already in the list")).to_be_visible()
    finished = group(app, "Finished").get_by_role("listitem")
    expect(finished).to_have_count(1)
    expect(finished).to_have_class(re.compile(r"\bflash\b"))


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
    name.click()
    app.wait_for_timeout(200)
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


def test_an_unplugged_drive_says_to_plug_it_in(app, spool):
    spool[1].update_settings({"dest": "/Volumes/NoSuchDrive/Spool"})
    expect(
        app.get_by_role("alert").filter(
            has_text=re.compile("NoSuchDrive.* isn't connected. Plug it in, or choose another folder.")
        )
    ).to_be_visible()


def test_a_drive_that_does_not_answer_blocks_download(app, monkeypatch):
    from spool import system

    release = threading.Event()
    real = system.probe_dest
    monkeypatch.setattr(system, "probe_dest", lambda path: (release.wait(), real(path))[1])  # asleep until released
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
    page.wait_for_timeout(500)
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
        expect(strip).to_contain_text("Spool couldn't set up its engine.", timeout=15000)
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
    jobs.close()

    httpd, url, jobs, _ = serve_fake.start(tmp_path, port=port)
    try:
        page.wait_for_timeout(2500)
        look_up(page, VIDEO)  # the token kept from the first load still works
        expect(page.get_by_role("region", name="Fake video 1")).to_be_visible()
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()
