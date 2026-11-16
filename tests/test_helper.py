"""YouTube's free helper: Spool fetches deno when the machine has no JavaScript runtime (ux/plan.md section 9)."""

import hashlib
import io
import platform
import sys
import time
import zipfile

import pytest

from spool.engines import DENO, JsRuntime

EXE = "deno.exe" if sys.platform == "win32" else "deno"


class FakeDenoRelease:
    """Stands in for github.com/denoland/deno: one tag, one zip per target and its .sha256sum (PowerShell's output
    for the Windows zip, as deno publishes it)."""

    def __init__(self, corrupt=False, offline=False, tag="v2.0.0"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(EXE, f"#!/bin/sh\necho deno {tag[1:]}\n".encode())
        self.zip, self.corrupt, self.offline, self.tag, self.fetched = buf.getvalue(), corrupt, offline, tag, []

    def resolve(self, url):
        if self.offline:
            raise OSError("offline")
        assert url == f"{DENO}/latest"
        return f"{DENO}/tag/{self.tag}"

    def fetch(self, url):
        if self.offline:
            raise OSError("offline")
        self.fetched.append(url)
        assert url.startswith(f"{DENO}/download/{self.tag}/deno-")
        if url.endswith(".sha256sum"):
            digest, name = hashlib.sha256(self.zip).hexdigest(), url.rsplit("/", 1)[1][:-10]
            if "windows" in name:
                return (
                    f"\r\nAlgorithm : SHA256\r\nHash      : {digest.upper()}\r\nPath      : C:\\a\\{name}\r\n".encode()
                )
            return f"{digest}  {name}\n".encode()
        return self.zip + (b"x" if self.corrupt else b"")


def settled(helper, timeout=10):
    end = time.time() + timeout
    while helper.tools()["js_setup"] == "installing":
        assert time.time() < end
        time.sleep(0.02)
    return helper.tools()


def test_with_no_runtime_on_path_spool_fetches_deno_and_uses_its_own_copy(tmp_path):
    release = FakeDenoRelease()
    helper = JsRuntime(tmp_path, fetch=release.fetch, resolve=release.resolve, which=lambda name: None)
    assert helper.tools()["js_runtime"] is None
    helper.start()
    tools = settled(helper)
    assert (tools["js_runtime"], tools["js_setup"]) == ("deno", None)
    assert tools["js_path"] == str(tmp_path / "deno" / "v2.0.0" / EXE)
    assert (tmp_path / "deno" / "v2.0.0" / EXE).read_bytes().startswith(b"#!/bin/sh")
    assert len(release.fetched) == 2
    helper.start()  # already there: nothing more is fetched
    assert len(settled(helper)) and len(release.fetched) == 2


def program(tmp_path, name, says):
    """A runtime on PATH that answers --version."""
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{says}'\n")
    path.chmod(0o700)
    return str(path)


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in runtimes are shell scripts")


@unix_only
@pytest.mark.parametrize("on_path, says", [("deno", "deno 2.5.6 (stable)"), ("node", "v22.1.0"), ("bun", "1.2.11")])
def test_a_runtime_on_path_needs_nothing_fetched(tmp_path, on_path, says):
    release, path = FakeDenoRelease(), program(tmp_path, on_path, says)
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda n: path if n == on_path else None)
    helper.start()
    assert settled(helper) == {"js_runtime": on_path, "js_setup": None, "js_path": None}
    assert release.fetched == []


@unix_only
@pytest.mark.parametrize("on_path, says", [("deno", "deno 2.2.0 (stable)"), ("node", "v20.11.0"), ("bun", "1.1.0")])
def test_a_runtime_on_path_older_than_the_engine_accepts_does_not_count(tmp_path, on_path, says):
    release, path = FakeDenoRelease(), program(tmp_path, on_path, says)
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda n: path if n == on_path else None)
    helper.start()
    assert settled(helper)["js_path"] == str(tmp_path / "deno" / "v2.0.0" / EXE)


def test_the_windows_checksum_file_is_read_too(tmp_path, monkeypatch):
    from spool import engines

    here = (sys.platform, platform.machine())
    monkeypatch.setitem(engines.DENO_TARGETS, here, "x86_64-pc-windows-msvc")  # this machine fetches that zip
    release = FakeDenoRelease()
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)
    helper.start()
    assert settled(helper)["js_setup"] is None
    assert release.fetched[0].endswith("deno-x86_64-pc-windows-msvc.zip.sha256sum")


def test_spools_own_deno_is_updated_once_a_run(tmp_path):
    old = FakeDenoRelease()
    settled(start(JsRuntime(tmp_path, old.fetch, old.resolve, which=lambda name: None)))
    new = FakeDenoRelease(tag="v2.1.0")
    helper = start(JsRuntime(tmp_path, new.fetch, new.resolve, which=lambda name: None))
    assert helper.tools()["js_setup"] is None  # the old one serves while the new one comes
    done(helper)
    assert helper.tools()["js_path"] == str(tmp_path / "deno" / "v2.1.0" / EXE)
    assert sorted(p.name for p in (tmp_path / "deno").iterdir()) == ["current", "v2.1.0"]
    helper.start()  # checked already this run
    done(helper)
    assert len(new.fetched) == 2


def test_a_missing_deno_file_is_fetched_again(tmp_path):
    release = FakeDenoRelease()
    settled(start(JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)))
    (tmp_path / "deno" / "v2.0.0" / EXE).unlink()
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)
    assert helper.tools()["js_runtime"] is None
    assert settled(start(helper))["js_path"] == str(tmp_path / "deno" / "v2.0.0" / EXE)


def test_an_archive_member_larger_than_the_limit_is_refused(tmp_path, monkeypatch):
    from spool import engines

    monkeypatch.setattr(engines, "LIMIT", 10)
    release = FakeDenoRelease()
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)
    assert settled(start(helper))["js_setup"] == "failed"


def start(helper):
    helper.start()
    return helper


def done(helper, timeout=10):
    end = time.time() + timeout
    while helper.busy:
        assert time.time() < end
        time.sleep(0.02)


@pytest.mark.parametrize("release", [FakeDenoRelease(corrupt=True), FakeDenoRelease(offline=True)])
def test_a_failed_fetch_leaves_a_clear_state_and_nothing_half_installed(tmp_path, release):
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)
    helper.start()
    assert settled(helper) == {"js_runtime": None, "js_setup": "failed", "js_path": None}
    assert not (tmp_path / "deno" / "current").exists()
