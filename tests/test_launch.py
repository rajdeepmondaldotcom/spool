import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

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


LAUNCHED = []


@pytest.fixture(autouse=True)
def stop_every_launch():
    """Whatever a test does or fails to do, no Spool it started outlives it."""
    yield
    while LAUNCHED:
        stop(LAUNCHED.pop())


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
    LAUNCHED.append(proc)
    line = proc.stdout.readline()
    return proc, line


def url_in(line):
    return re.search(r"http://127\.0\.0\.1:\d+/#t=\S+", line).group(0)


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    proc.stdout.close()


def test_launch_prints_the_link_with_the_token_in_the_fragment(data):
    proc, line = launch(data)
    try:
        url = url_in(line)
        token = (data / "token").read_text().strip()
        lock = json.loads((data / "spool.lock").read_text())
        assert lock == {"port": lock["port"], "pid": proc.pid}
        assert url == f"http://127.0.0.1:{lock['port']}/#t={token}"
        assert stat.S_IMODE((data / "token").stat().st_mode) == 0o600
        client = Client(url.split("/#")[0], token)
        assert client.request("GET", "/api/jobs/x/log")[0] == 404  # past the token check
    finally:
        stop(proc)


def test_the_token_and_the_default_port_survive_a_restart(data):
    first, line = launch(data)
    stop(first)
    second, again = launch(data)
    try:
        assert url_in(again).split("#")[1] == url_in(line).split("#")[1]  # the same token
        if ":7766/" in line:  # unless something else on this machine holds the default port
            assert url_in(again) == url_in(line)
    finally:
        stop(second)


def test_the_default_port_taken_by_something_else_falls_back_to_a_free_one(data):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # the previous test's Spool may leave TIME_WAIT
        try:
            s.bind(("127.0.0.1", 7766))
        except OSError:
            pytest.skip("7766 is in use on this machine")
        s.listen()
        proc, line = launch(data)
        try:
            assert "Spool is running at" in line and ":7766/" not in line
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
    (data / "spool.lock").write_text(json.dumps({"port": 9, "pid": 999999}))
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
    client = Client(url.split("/#")[0], url.split("#t=")[1])
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


def test_tests_never_run_from_inside_the_repo(tmp_path):
    here = Path.cwd().resolve()
    assert here == tmp_path.resolve()
    assert Path(__file__).resolve().parents[1] not in (here, *here.parents)


def test_a_damaged_state_file_does_not_stop_launch(data):
    (data / "state.json").write_text('{"settings": {"dest": ')
    proc, line = launch(data)
    assert "started fresh" in line and "Traceback" not in line
    assert "Spool is running at" in proc.stdout.readline()


def test_two_launches_at_the_same_instant_both_come_up_cleanly(data):
    (data / "state.json").unlink()
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "spool", "--no-open", "--port", "0"],
            env={**os.environ, "SPOOL_DATA_DIR": str(data), "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": ""},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    LAUNCHED.extend(procs)
    for proc in procs:
        assert "Spool is" in proc.stdout.readline()
    for proc in procs:
        proc.send_signal(signal.SIGTERM)
        assert "Traceback" not in proc.communicate(timeout=10)[0]
