import pytest
from serve_fake import FakeGitHub, start
from support import Client


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def app(tmp_path, github):
    httpd, url, jobs, engines = start(tmp_path / "data", github=github)
    client = Client(url.split("/?")[0])
    client.httpd, client.jobs, client.engines, client.data = httpd, jobs, engines, tmp_path / "data"
    client.dest = tmp_path / "data" / "downloads"
    yield client
    httpd.shutdown()
    httpd.server_close()
    jobs.close()
