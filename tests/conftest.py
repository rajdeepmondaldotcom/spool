import pytest
from serve_fake import INSTALLED, FakeGitHub, start
from support import Client


@pytest.fixture(autouse=True)
def away_from_the_repo(tmp_path, monkeypatch):
    """Every test runs from its own temp folder, so nothing that deletes "here" can reach the repo.

    A fake engine once ran shutil.rmtree(os.getcwd()) with the repo as the current folder and deleted it.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def no_home_downloads(tmp_path, monkeypatch):
    """A Jobs built with no saved folder checks tmp_path, never the real Downloads folder."""
    monkeypatch.setattr("spool.system.default_dest", lambda: tmp_path)


@pytest.fixture
def github():
    """Nothing newer is published, so the engines Spool updates at launch stay as installed."""
    return FakeGitHub(latest=INSTALLED)


@pytest.fixture
def app(tmp_path, github):
    httpd, url, jobs, engines = start(tmp_path / "data", github=github)
    client = Client(url.split("/#")[0])
    client.httpd, client.jobs, client.engines, client.data = httpd, jobs, engines, tmp_path / "data"
    client.dest = tmp_path / "data" / "downloads"
    yield client
    httpd.shutdown()
    httpd.server_close()
    jobs.close()
