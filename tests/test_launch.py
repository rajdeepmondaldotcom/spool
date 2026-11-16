import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from serve_fake import install_fake_engines
from support import Client

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="stops Spool with SIGTERM and checks process groups; launch and shutdown are not covered on Windows",
)


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


def env(data):
    """An unreachable proxy, in both spellings (lowercase wins), keeps the latest-version check off the network."""
    proxy = {k: v for k in ("https_proxy", "HTTPS_PROXY") for v in ["http://127.0.0.1:9"]}
    blank = {k: "" for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY")}
    return {**os.environ, "SPOOL_DATA_DIR": str(data), **proxy, **blank}


def start(data, *args):
    proc = subprocess.Popen(
        [sys.executable, "-m", "spool", "--no-open", *args],
        env=env(data),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    LAUNCHED.append(proc)
    proc.lines = queue.Queue()
    threading.Thread(
        target=lambda: [proc.lines.put(x) for x in proc.stdout] + [proc.lines.put("")], daemon=True
    ).start()
    return proc


def readline(proc, timeout=15):
    """The next line Spool prints; a launch that blocks fails the test instead of hanging the suite."""
    try:
        return proc.lines.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"Spool printed nothing for {timeout} s")


def launch(data, *args):
    proc = start(data, *args)
    return proc, readline(proc)


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


def test_launch_prints_the_link_with_the_token_in_the_fragment(data):
    proc, line = launch(data)
    try:
        assert line.startswith("Spool is running at http://127.0.0.1:")
        assert readline(proc) == "Press Ctrl+C to stop it.\n"
        url = url_in(line)
        lock = json.loads((data / "spool.lock").read_text())
        assert lock == {"port": lock["port"], "pid": proc.pid, "token": lock["token"]}
        assert url == f"http://127.0.0.1:{lock['port']}/#t={lock['token']}"
        assert stat.S_IMODE((data / "spool.lock").stat().st_mode) == 0o600
        assert not (data / "token").exists()  # no token outlives its launch
        client = Client(url.split("/#")[0], lock["token"])
        assert client.request("GET", "/api/jobs/x/log")[0] == 404  # past the token check
    finally:
        stop(proc)


def test_every_launch_has_a_new_token_and_the_default_port_survives(data):
    first, line = launch(data)
    stop(first)
    second, again = launch(data)
    try:
        assert url_in(again).split("#")[1] != url_in(line).split("#")[1]  # an old tab's token opens nothing
        if ":7766/" in line:
            assert url_in(again).split("#")[0] == url_in(line).split("#")[0]
    finally:
        stop(second)


def test_the_default_port_survives_a_restart(data):
    first, line = launch(data)
    stop(first)
    second, again = launch(data)
    try:
        if ":7766/" in line:  # unless something else on this machine holds the default port
            assert url_in(again).split("#")[0] == url_in(line).split("#")[0]
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


def dead_pid():
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return gone.pid


def test_a_stale_lockfile_is_ignored(data):
    (data / "spool.lock").write_text(json.dumps({"port": 9, "pid": dead_pid()}))
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
        assert f"Spool couldn't use port {port}" in line
    proc, line = launch(data, "--port", str(port))
    try:
        assert f"127.0.0.1:{port}/" in line
    finally:
        stop(proc)


def downloading(data):
    """A Spool with a download running, and its workers' pids."""
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
    return proc, workers


def stopped_cleanly(proc, data, workers):
    proc.wait(10)
    assert proc.returncode == 0
    assert not (data / "spool.lock").exists()
    for pid in workers:  # each worker's whole group, the helper it started included
        with pytest.raises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)


def test_shutdown_stops_running_downloads_and_removes_the_lockfile(data):
    proc, workers = downloading(data)
    stop(proc)
    stopped_cleanly(proc, data, workers)


def test_closing_the_terminal_window_shuts_down_the_same_way(data):
    proc, workers = downloading(data)
    proc.send_signal(signal.SIGHUP)
    stopped_cleanly(proc, data, workers)


def test_tests_never_run_from_inside_the_repo(tmp_path):
    here = Path.cwd().resolve()
    assert here == tmp_path.resolve()
    assert Path(__file__).resolve().parents[1] not in (here, *here.parents)


def test_a_damaged_state_file_does_not_stop_launch(data):
    (data / "state.json").write_text('{"settings": {"dest": ')
    proc, line = launch(data)
    assert line.startswith("Spool couldn't read its saved list, so it started a new one.") and "Traceback" not in line
    assert "Spool is running at" in readline(proc)


def test_two_launches_at_the_same_instant_never_crash(data):
    """A known limit, not a promise: both may start (the shortcut in __main__.running); neither may crash."""
    (data / "state.json").unlink()
    procs = [start(data, "--port", "0") for _ in range(2)]
    for proc in procs:
        assert "Spool is" in readline(proc)
    for proc in procs:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
        printed = []
        while (line := readline(proc)) != "":
            printed.append(line)
        assert "Traceback" not in "".join(printed)


def test_links_on_the_command_line_ride_in_the_fragment():
    from spool.__main__ import carrying

    assert carrying([]) == ""
    assert carrying(["https://youtu.be/x?t=1", "https://v.test/a b"]) == (
        "&url=https%3A%2F%2Fyoutu.be%2Fx%3Ft%3D1%20https%3A%2F%2Fv.test%2Fa%20b"
    )
