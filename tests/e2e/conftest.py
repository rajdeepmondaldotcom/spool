"""Browser tests: the real server on the fake engine, driven through Chromium. No network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import serve_fake  # noqa: E402


@pytest.fixture
def spool(tmp_path, request):
    """Starts Spool in a fresh data folder. Yields (url, jobs). Parametrize it indirectly to pass start()'s options,
    e.g. {"ffmpeg": False}."""
    httpd, url, jobs, _ = serve_fake.start(tmp_path, **getattr(request, "param", {}))
    yield url, jobs
    httpd.shutdown()
    jobs.close()


NATIVE = ("**/api/choose-folder", "**/api/dest/reveal", "**/api/jobs/*/reveal")


def guard_native(page):
    """Calls that would open a native folder dialog or the file manager never reach the server in a test.
    Returns the list of (path, request) they made; a test sets page.chosen to answer choose-folder."""
    made = []

    def fake(route):
        made.append(route.request.url.split("/api/", 1)[1])
        chosen = getattr(page, "chosen", None)
        if made[-1] == "choose-folder" and chosen:
            return route.fulfill(json={"path": chosen})
        route.fulfill(status=204)

    for pattern in NATIVE:
        page.route(pattern, fake)
    return made


@pytest.fixture(autouse=True)
def no_native_windows(page):
    page.native = guard_native(page)


@pytest.fixture
def app(page, spool):
    """The page, opened with the launch token and connected to the event stream.
    Fails the test on any script error or Content-Security-Policy violation."""
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text)
    )
    page.goto(spool[0])
    page.get_by_label("Link to download").wait_for()
    yield page
    assert errors == []
