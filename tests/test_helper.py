"""YouTube's free helper: Spool fetches deno when the machine has no JavaScript runtime (ux/plan.md section 9)."""

import hashlib
import io
import sys
import time
import zipfile

import pytest

from spool.engines import DENO, JsRuntime

EXE = "deno.exe" if sys.platform == "win32" else "deno"


class FakeDenoRelease:
    """Stands in for github.com/denoland/deno: tag v2.0.0, one zip per target and its .sha256sum."""

    def __init__(self, corrupt=False, offline=False):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(EXE, b"#!/bin/sh\necho deno 2.0.0\n")
        self.zip, self.corrupt, self.offline, self.fetched = buf.getvalue(), corrupt, offline, []

    def resolve(self, url):
        if self.offline:
            raise OSError("offline")
        assert url == f"{DENO}/latest"
        return f"{DENO}/tag/v2.0.0"

    def fetch(self, url):
        if self.offline:
            raise OSError("offline")
        self.fetched.append(url)
        assert url.startswith(f"{DENO}/download/v2.0.0/deno-")
        if url.endswith(".sha256sum"):
            return f"{hashlib.sha256(self.zip).hexdigest()}  {url.rsplit('/', 1)[1][:-10]}\n".encode()
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


@pytest.mark.parametrize("on_path", ["deno", "node", "bun"])
def test_a_runtime_on_path_needs_nothing_fetched(tmp_path, on_path):
    release = FakeDenoRelease()
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda n: f"/bin/{n}" if n == on_path else None)
    helper.start()
    assert settled(helper) == {"js_runtime": on_path, "js_setup": None, "js_path": None}
    assert release.fetched == []


@pytest.mark.parametrize("release", [FakeDenoRelease(corrupt=True), FakeDenoRelease(offline=True)])
def test_a_failed_fetch_leaves_a_clear_state_and_nothing_half_installed(tmp_path, release):
    helper = JsRuntime(tmp_path, release.fetch, release.resolve, which=lambda name: None)
    helper.start()
    assert settled(helper) == {"js_runtime": None, "js_setup": "failed", "js_path": None}
    assert not (tmp_path / "deno" / "current").exists()
