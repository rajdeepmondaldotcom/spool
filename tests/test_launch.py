import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest
from serve_fake import install_fake_engines
from support import Client

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def data(tmp_path):
    install_fake_engines(tmp_path)
    (tmp_path / "downloads").mkdir()
    (tmp_path / "state.json").write_text(json.dumps({"settings": {"dest": str(tmp_path / "downloads")}}))
    return tmp_path


def launch(data, *args):
    # an unreachable proxy keeps the latest-version check off the network
    env = {**os.environ, "SPOOL_DATA_DIR": str(data), "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "spool", "--no-open", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    line = proc.stdout.readline()
    return proc, line


def url_in(line):
    return re.search(r"http://127\.0\.0\.1:\d+/\?t=\S+", line).group(0)


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def test_launch_prints_the_tokened_link_and_writes_a_private_lockfile(data):
    proc, line = launch(data)
    try:
        url = url_in(line)
        lock = json.loads((data / "spool.lock").read_text())
        assert lock["pid"] == proc.pid
        assert url == f"http://127.0.0.1:{lock['port']}/?t={lock['token']}"
        assert stat.S_IMODE((data / "spool.lock").stat().st_mode) == 0o600
        client = Client(url.split("/?")[0], lock["token"])
        assert client.request("GET", "/")[0] == 200
    finally:
        stop(proc)


def test_a_second_launch_points_at_the_running_one_and_exits(data):
    first, line = launch(data)
    try:
        second, again = launch(data)
        assert second.wait(10) == 0
        assert "already running" in again
        assert url_in(again) == url_in(line)
    finally:
        stop(first)


def test_a_stale_lockfile_is_ignored(data):
    (data / "spool.lock").write_text(json.dumps({"port": 9, "pid": 999999, "token": "old"}))
    proc, line = launch(data)
    try:
        assert "already running" not in line
        assert json.loads((data / "spool.lock").read_text())["pid"] == proc.pid
    finally:
        stop(proc)


def test_a_chosen_port_is_used_and_a_taken_one_is_a_plain_error(data):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen()
        proc, line = launch(data, "--port", str(port))
        assert proc.wait(10) == 1
        assert f"could not use port {port}" in line
    proc, line = launch(data, "--port", str(port))
    try:
        assert f"127.0.0.1:{port}/" in line
    finally:
        stop(proc)


def test_shutdown_stops_running_downloads_and_removes_the_lockfile(data):
    proc, line = launch(data)
    url = url_in(line)
    client = Client(url.split("/?")[0], url.split("t=")[1])
    entry = {"url": "https://fake.test/slow?delay=0.3", "id": "slow-1", "title": "Slow"}
    assert client.json("POST", "/api/jobs", {"entries": [entry], "preset": "best"})[0] == 201
    deadline = time.time() + 10
    while not list((data / "downloads").glob("*.part")):
        assert time.time() < deadline
        time.sleep(0.05)
    workers = [
        int(p) for p in subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
    ]
    assert workers
    stop(proc)
    assert proc.returncode == 0
    assert not (data / "spool.lock").exists()
    for pid in workers:  # each worker's whole group, the helper it started included
        with pytest.raises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)
