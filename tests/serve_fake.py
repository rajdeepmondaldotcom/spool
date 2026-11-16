"""Run the real Spool server against the fake engine, with no network.

    uv run python tests/serve_fake.py --port 8765 [--fresh] [--no-ffmpeg]

Prints the link, with the token in its fragment (#t=fake-token); API calls send it as X-Spool-Token. Links like https://fake.test/video drive every UI state; see fake_engine/_fake.py.
--fresh starts with no engines installed, so the first-run install is visible (it takes a few seconds).
--no-ffmpeg runs as if ffmpeg were missing. Tools never come from this machine's PATH: by default ffmpeg and a
JavaScript runtime (deno) are there, so every machine offers the same qualities.
--dest-hangs makes every destination check hang, as a sleeping drive does, so the page shows "not_responding".
Other fake links: /live (fails as "live"), /needs-js (hint "install_js_runtime"), /shared-playlist (entries that
share one link, downloaded as items), /broken (an engine bug). A /Volumes/<no such drive>/... destination is "unplugged".
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

FAKE = Path(__file__).with_name("fake_engine")
INSTALLED = "2026.01.01"
LATEST = "2026.02.01"
NAMES = {"yt-dlp": "yt-dlp", "youtube-dl": "youtube-dl"}


def install_fake_engines(data_dir):
    for name in NAMES:
        folder = Path(data_dir) / "engines" / name
        shutil.copytree(FAKE, folder / INSTALLED, ignore=shutil.ignore_patterns("__pycache__"))
        (folder / "current").write_text(INSTALLED)


def fake_engine_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in FAKE.rglob("*.py"):
            z.write(f, f.relative_to(FAKE).as_posix())
    return buf.getvalue()


class FakeGitHub:
    """Stands in for github.com: the latest tag is LATEST and each release holds the fake engine as a zip."""

    def __init__(self, delay=0.0, corrupt=False, offline=False):
        self.delay, self.corrupt, self.offline = delay, corrupt, offline
        self.asset = fake_engine_zip()
        self.fetched = []

    def check(self, url):
        time.sleep(self.delay)
        if self.offline:
            raise OSError("<urlopen error [Errno 8] nodename nor servname provided, or not known>")
        self.fetched.append(url)

    def resolve(self, url):
        self.check(url)
        assert url.endswith("/releases/latest"), url
        return url.replace("/latest", f"/tag/{LATEST}")

    def fetch(self, url):
        self.check(url)
        if url.endswith("/SHA2-256SUMS"):
            digest = hashlib.sha256(self.asset).hexdigest()
            return "".join(f"{digest}  {asset}\n" for asset in NAMES.values()).encode()
        return self.asset + (b"corrupted" if self.corrupt else b"")


def start(data_dir, port=0, fresh=False, github=None, ffmpeg=True, js_runtime="deno"):
    """Start Spool on the fake engine in data_dir. Returns (httpd, url, jobs, engines).

    The tools are what the caller says, never what this machine's PATH has, so tests run the same everywhere."""
    os.environ["SPOOL_DATA_DIR"] = str(data_dir)
    from spool import server, system
    from spool.engines import Engines
    from spool.jobs import Jobs

    data = system.data_dir()
    if not fresh and not (data / "engines").exists():
        install_fake_engines(data)
    github = github or FakeGitHub(delay=2.0 if fresh else 0.0)
    engines = Engines(data / "engines", fetch=github.fetch, resolve=github.resolve)
    tools = {"ffmpeg": ffmpeg, "js_runtime": js_runtime, "platform": sys.platform, "can_choose_folder": False}
    jobs = Jobs(data, engines, tools=lambda: dict(tools))
    downloads = data / "downloads"
    if not (data / "state.json").exists() or not downloads.exists():
        downloads.mkdir(exist_ok=True)
        jobs.update_settings({"dest": str(downloads)})
    httpd = server.make_server(jobs, engines, token="fake-token", port=port)
    engines.start()
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/#t=fake-token", jobs, engines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--no-ffmpeg", action="store_true")
    parser.add_argument("--dest-hangs", action="store_true")
    parser.add_argument("--data-dir", help="reuse a data folder (default: a new temp folder)")
    args = parser.parse_args()
    data = args.data_dir or tempfile.mkdtemp(prefix="spool-fake-")
    httpd, url, jobs, _ = start(data, args.port, args.fresh, ffmpeg=not args.no_ffmpeg)
    if args.dest_hangs:
        from spool import system

        system.probe_dest = lambda path: threading.Event().wait()  # never answers
    print(f"Data folder: {data}", flush=True)
    print(url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        jobs.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
