"""Browser tests: the real server on the fake engine, driven through Chromium. No network."""

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import serve_fake  # noqa: E402


@pytest.fixture
def spool(tmp_path):
    """Starts Spool in a fresh data folder. Yields (url, jobs)."""
    httpd, url, jobs, _ = serve_fake.start(tmp_path)
    yield url, jobs
    httpd.shutdown()
    jobs.close()


@pytest.fixture
def no_ffmpeg(monkeypatch):
    paths = os.environ.get("PATH", "").split(os.pathsep)
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in paths if not shutil.which("ffmpeg", path=p)))


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
