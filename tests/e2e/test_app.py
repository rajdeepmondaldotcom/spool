"""The main flows a person uses, through the real server and the fake engine."""

import re

import pytest
import serve_fake  # on the path through conftest
from playwright.sync_api import expect

VIDEO = "https://fake.test/video"


def look_up(page, link):
    field = page.get_by_label("Link to download")
    field.fill(link)
    field.press("Enter")


def test_look_up_a_link_then_download_it_to_finished(app):
    look_up(app, VIDEO)
    tray = app.get_by_role("region", name="Fake video 1")
    expect(tray.get_by_text("Fake Channel")).to_be_visible()
    expect(tray.get_by_text("1:35")).to_be_visible()
    expect(tray.get_by_role("group", name="Quality").get_by_role("radio", name=re.compile("^Best"))).to_be_checked()

    tray.get_by_role("button", name="Download").click()

    expect(tray).to_be_hidden()
    expect(app.get_by_label("Link to download")).to_be_focused()
    expect(app.get_by_label("Link to download")).to_have_value("")
    finished = app.get_by_role("region", name="Finished")
    expect(finished.get_by_text("Fake video 1")).to_be_visible(timeout=15000)
    expect(finished.get_by_role("button", name=re.compile("Show in (Finder|folder)"))).to_be_visible()


def test_pick_two_of_a_playlist_and_download_them(app):
    look_up(app, "https://fake.test/playlist")
    tray = app.get_by_role("region", name="Fake playlist")
    expect(tray.get_by_text("3 of 3 selected")).to_be_visible()
    tray.get_by_role("button", name="Select none").click()
    expect(tray.get_by_role("button", name="Download 0")).to_be_disabled()
    tray.get_by_role("checkbox", name=re.compile("Fake video 1")).check()
    tray.get_by_role("checkbox", name=re.compile("Fake video 3")).check()
    expect(tray.get_by_text("2 of 3 selected")).to_be_visible()

    tray.get_by_role("button", name="Download 2").click()

    expect(app.get_by_role("status").filter(has_text="Added 2 downloads.")).to_be_visible()
    finished = app.get_by_role("region", name="Finished")
    expect(finished.get_by_role("listitem")).to_have_count(2, timeout=15000)
    expect(finished.get_by_text("Fake video 2")).to_have_count(0)


def test_a_refused_download_offers_the_engine_update_when_one_exists(app):
    look_up(app, "https://fake.test/403")
    app.get_by_role("region", name="Fake refused video").get_by_role("button", name="Download").click()

    stopped = app.get_by_role("region", name="Stopped")
    expect(stopped.get_by_text("The site refused this download. It often works on a second try.")).to_be_visible(
        timeout=15000
    )
    expect(stopped.get_by_role("button", name="Update engine and retry")).to_have_class("fill")
    expect(stopped.get_by_role("button", name="Retry", exact=True)).to_have_class("secondary")
    expect(stopped.get_by_role("button", name="Show log")).to_be_visible()


def test_a_refused_download_with_the_latest_engine_offers_retry(app, spool):
    engines = spool[1].engines
    engines.update("yt-dlp")
    expect(app.get_by_role("contentinfo").get_by_text("Up to date")).to_be_visible(timeout=15000)
    look_up(app, "https://fake.test/403")
    app.get_by_role("region", name="Fake refused video").get_by_role("button", name="Download").click()

    stopped = app.get_by_role("region", name="Stopped")
    expect(stopped.get_by_role("button", name="Retry", exact=True)).to_have_class("fill", timeout=15000)
    expect(stopped.get_by_role("button", name="Update engine and retry")).to_have_count(0)


def test_a_link_the_engine_cannot_read_offers_the_other_engine(app):
    look_up(app, "https://fake.test/unsupported")
    error = app.get_by_role("alert")
    expect(error).to_contain_text("yt-dlp can't read this link.")
    expect(error.get_by_role("button", name="Try with youtube-dl")).to_be_visible()
    field = app.get_by_label("Link to download")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_accessible_description(re.compile("yt-dlp can't read this link."))


def test_a_live_stream_cannot_be_downloaded_yet(app):
    look_up(app, "https://fake.test/live")
    tray = app.get_by_role("region", name="Fake live stream")
    expect(tray.get_by_text("This is live now. Download it after it ends.")).to_be_visible()
    expect(tray.get_by_role("button", name="Download")).to_be_disabled()


def test_escape_closes_the_tray_and_returns_to_the_field(app):
    look_up(app, VIDEO)
    tray = app.get_by_role("region", name="Fake video 1")
    tray.get_by_role("radio", name=re.compile("^720p")).focus()
    app.keyboard.press("Escape")
    expect(tray).to_be_hidden()
    expect(app.get_by_label("Link to download")).to_be_focused()


def test_keyboard_only_main_flow(app):
    expect(app.get_by_label("Link to download")).to_be_focused()
    app.keyboard.type(VIDEO)
    app.keyboard.press("Enter")
    tray = app.get_by_role("region", name="Fake video 1")
    expect(tray).to_be_visible()
    # Tab into the quality group, move to the next choice with an arrow, then on to Download.
    radio = tray.get_by_role("radio", name=re.compile("^Best"))
    while not radio.evaluate("e => e === document.activeElement"):
        app.keyboard.press("Tab")
    app.keyboard.press("ArrowRight")
    expect(tray.get_by_role("radio", name=re.compile("^1080p"))).to_be_checked()
    app.keyboard.press("Tab")
    expect(tray.get_by_role("button", name="Download")).to_be_focused()
    app.keyboard.press("Enter")
    expect(app.get_by_label("Link to download")).to_be_focused()
    expect(app.get_by_role("region", name="Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)


def test_cmd_enter_downloads_straight_away(app):
    field = app.get_by_label("Link to download")
    field.fill(VIDEO)
    field.press("ControlOrMeta+Enter")
    expect(app.get_by_role("region", name="Finished").get_by_text("Fake video 1")).to_be_visible(timeout=15000)


def test_pasting_several_links_queues_them_all(app):
    app.locator(".queue").click()
    app.evaluate(
        """links => { const d = new DataTransfer(); d.setData('text/plain', links);
        document.body.dispatchEvent(new ClipboardEvent('paste', {clipboardData: d, bubbles: true})); }""",
        "https://fake.test/video/1\nhttps://fake.test/video/2 https://fake.test/audio",
    )
    expect(app.get_by_role("status").filter(has_text="Added 3 downloads.")).to_be_visible()
    expect(app.get_by_role("region", name="Finished").get_by_role("listitem")).to_have_count(3, timeout=15000)


def test_a_missing_folder_blocks_download_and_says_how_to_fix_it(app, spool, tmp_path):
    spool[1].update_settings({"dest": str(tmp_path / "gone")})
    problem = app.get_by_role("alert").filter(has_text="gone doesn't exist. Choose another folder.")
    expect(problem).to_be_visible()
    expect(problem.get_by_role("button", name="Try again")).to_be_visible()
    expect(problem.get_by_role("button", name="Change folder")).to_be_visible()

    look_up(app, VIDEO)
    download = app.get_by_role("region", name="Fake video 1").get_by_role("button", name="Download")
    expect(download).to_be_disabled()
    expect(download).to_have_accessible_description(re.compile("gone doesn't exist"))


def test_a_file_chosen_as_the_folder_says_so(app, spool, tmp_path):
    (tmp_path / "notes.txt").write_text("not a folder")
    spool[1].update_settings({"dest": str(tmp_path / "notes.txt")})
    expect(app.get_by_role("alert").filter(has_text="is a file, not a folder.")).to_be_visible()


def test_cancel_a_running_download_then_resume_it(app):
    look_up(app, "https://fake.test/slow")
    app.get_by_role("region", name="Fake slow video").get_by_role("button", name="Download").click()
    active = app.get_by_role("region", name="Downloading")
    expect(active.get_by_role("progressbar", name="Progress of Fake slow video")).to_be_visible(timeout=10000)
    expect(active.get_by_text("Downloading video")).to_be_visible()
    active.get_by_role("button", name="Cancel").click()

    stopped = app.get_by_role("region", name="Stopped")
    expect(stopped.get_by_text("Cancelled")).to_be_visible()
    expect(stopped.get_by_role("button", name="Resume")).to_be_focused()
    stopped.get_by_role("button", name="Resume").click()
    expect(app.get_by_role("region", name="Downloading").get_by_text("Fake slow video")).to_be_visible()


def test_show_log_then_remove_a_failed_download(app):
    look_up(app, "https://fake.test/403")
    app.get_by_role("region", name="Fake refused video").get_by_role("button", name="Download").click()
    stopped = app.get_by_role("region", name="Stopped")
    stopped.get_by_role("button", name="Show log").click(timeout=15000)
    expect(stopped.get_by_label("Engine log")).to_contain_text("403")
    stopped.get_by_role("button", name="Remove").click()
    expect(app.get_by_text("Nothing here yet. Paste a link above.")).to_be_visible()


def test_remove_a_finished_download_from_its_menu(app):
    look_up(app, VIDEO)
    app.get_by_role("region", name="Fake video 1").get_by_role("button", name="Download").click()
    finished = app.get_by_role("region", name="Finished")
    finished.get_by_role("button", name="More actions for Fake video 1").click(timeout=15000)
    expect(finished.get_by_role("button", name="Download again")).to_be_visible()
    finished.get_by_role("button", name="Remove from list").click()
    expect(app.get_by_text("Nothing here yet. Paste a link above.")).to_be_visible()
    expect(app.get_by_label("Link to download")).to_be_focused()


def test_update_the_engine_from_the_status_bar(app):
    status = app.get_by_role("contentinfo")
    status.get_by_role("button", name="Update to 2026.02.01 for yt-dlp").click()
    expect(status.get_by_text("Up to date")).to_be_visible(timeout=15000)
    expect(status.get_by_text("2026.02.01")).to_be_visible()


def test_the_status_bar_says_when_the_event_stream_drops(app, spool):
    spool[1].close()  # ends every event stream
    expect(app.get_by_role("contentinfo").get_by_text("Reconnecting…")).to_be_visible()


def test_finishing_is_announced_and_the_tab_title_shows_progress(app):
    look_up(app, "https://fake.test/slow?delay=0.3")
    app.get_by_role("region", name="Fake slow video").get_by_role("button", name="Download").click()
    expect(app).to_have_title(re.compile(r"^\d+% Spool$"))
    expect(app.get_by_role("status").filter(has_text="Finished: Fake slow video")).to_be_attached(timeout=15000)
    expect(app).to_have_title("Spool")


def test_settings_save_a_typed_folder_and_default_quality(app, tmp_path):
    app.get_by_role("button", name="Settings").click()
    dialog = app.get_by_role("dialog", name="Settings")
    dialog.get_by_label("Default quality").select_option("720")
    folder = dialog.get_by_label("Folder")
    folder.fill(str(tmp_path / "missing"))
    dialog.get_by_role("button", name="Save folder").click()
    expect(dialog.get_by_text("missing doesn't exist. Choose another folder.")).to_be_visible()
    app.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(app.get_by_role("alert").filter(has_text="missing doesn't exist")).to_be_visible()
    expect(
        app.get_by_text("Cmd+Enter downloads at 720p").or_(app.get_by_text("Ctrl+Enter downloads at 720p"))
    ).to_be_visible()


@pytest.mark.parametrize("width,height", [(1440, 420), (375, 560)])
def test_the_status_bar_never_covers_the_last_row(app, spool, width, height):
    app.set_viewport_size({"width": width, "height": height})
    spool[1].add([{"url": "https://fake.test/403", "title": "Fake refused video"}], "best")
    remove = app.get_by_role("region", name="Stopped").get_by_role("button", name="Remove")
    expect(remove).to_be_visible(timeout=15000)
    app.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    bar = app.get_by_role("contentinfo").bounding_box()
    button = remove.bounding_box()
    assert button["y"] + button["height"] <= bar["y"]
    remove.click()
    expect(app.get_by_text("Nothing here yet. Paste a link above.")).to_be_visible()


def test_a_failed_row_fills_only_its_suggested_fix(app):
    look_up(app, "https://fake.test/403")
    app.get_by_role("region", name="Fake refused video").get_by_role("button", name="Download").click()
    stopped = app.get_by_role("region", name="Stopped")
    fix = stopped.get_by_role("button", name="Update engine and retry")
    expect(fix).to_have_class("fill", timeout=15000)  # a newer engine exists in the fake
    expect(stopped.get_by_role("button", name="Retry", exact=True)).to_have_class("secondary")
    expect(stopped.get_by_role("button", name="Show log")).to_have_class("quiet")
    expect(stopped.get_by_role("button", name="Remove")).to_have_class("quiet")


def test_a_download_bigger_than_the_free_space_is_flagged(app):
    def huge(route):
        response = route.fetch()
        body = response.json()
        for p in body["entries"][0]["presets"]:
            if p["size"] is not None:
                p["size"] = 10**15
        route.fulfill(response=response, json=body)

    app.route("**/api/probe", huge)
    field = app.get_by_label("Link to download")
    field.fill(VIDEO)
    field.press("Enter")
    expect(app.get_by_text("This needs about")).to_be_visible()
    # A warning, not a block: the estimate can be wrong and the choice is the user's.
    expect(app.get_by_role("button", name="Download", exact=True)).to_be_enabled()


def test_ticking_one_entry_of_a_shared_link_item_leaves_the_others(app):
    # A multi-file item: every entry shares one link and differs only by its position.
    def shared(route):
        data = route.fetch().json()
        for n, entry in enumerate(data["entries"], 1):
            entry.update(url="https://fake.test/playlist", item=n)
        route.fulfill(json=data)

    app.route("**/api/probe", shared)
    look_up(app, "https://fake.test/playlist")
    tray = app.get_by_role("region", name="Fake playlist")
    tray.get_by_role("checkbox", name=re.compile("Fake video 2")).uncheck()
    expect(tray.get_by_text("2 of 3 selected")).to_be_visible()
    expect(tray.get_by_role("checkbox", name=re.compile("Fake video 1"))).to_be_checked()

    with app.expect_request("**/api/jobs") as sent:
        tray.get_by_role("button", name="Download 2").click()
    assert [e["item"] for e in sent.value.post_data_json["entries"]] == [1, 3]


def test_a_probe_engine_error_shows_the_engines_own_words(app):
    app.route(
        "**/api/probe",
        lambda route: route.fulfill(
            status=422, json={"error": {"kind": "engine", "message": "ERROR: [fake] boom", "hint": None}}
        ),
    )
    look_up(app, VIDEO)
    error = app.get_by_role("alert").filter(has_text="ERROR: [fake] boom")
    expect(error).to_be_visible()
    expect(error).not_to_contain_text("log")


def test_a_file_saved_to_another_folder_names_that_folder(app, spool, tmp_path):
    look_up(app, VIDEO)
    app.get_by_role("region", name="Fake video 1").get_by_role("button", name="Download").click()
    finished = app.get_by_role("region", name="Finished")
    expect(finished.get_by_role("listitem")).to_have_count(1, timeout=15000)
    expect(finished.locator(".folder")).to_have_count(0)

    (tmp_path / "Elsewhere").mkdir()
    spool[1].update_settings({"dest": str(tmp_path / "Elsewhere")})
    expect(finished.get_by_role("listitem").get_by_text("downloads")).to_be_visible()


def test_a_tab_with_an_unknown_token_says_how_to_sign_in(page, spool):
    page.goto(spool[0].replace("#t=fake-token", "#t=wrong"))
    expect(page.get_by_text("This tab isn't signed in to Spool.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/$"))  # the token left the address bar


def test_a_tab_reconnects_after_spool_restarts(page, tmp_path):
    httpd, url, jobs, _ = serve_fake.start(tmp_path)
    port = httpd.server_address[1]
    page.goto(url)
    page.get_by_label("Link to download").wait_for()
    httpd.shutdown()
    httpd.server_close()
    jobs.close()
    expect(page.get_by_text("Reconnecting…")).to_be_visible()

    httpd, url, jobs, _ = serve_fake.start(tmp_path, port=port)
    try:
        expect(page.get_by_text("Reconnecting…")).to_be_hidden(timeout=10000)
        look_up(page, VIDEO)  # the token kept from the first load still works
        expect(page.get_by_role("region", name="Fake video 1")).to_be_visible()
    finally:
        httpd.shutdown()
        httpd.server_close()
        jobs.close()
