import _fake
from _fake import DownloadError  # noqa: F401

from . import version  # noqa: F401


class YoutubeDL(_fake.YoutubeDL):
    HOOK_INFO = True
    NAME = "yt-dlp"
    METADATA_OPTIONS = {"add_chapters", "add_metadata", "add_infojson"}
    WHEN = {"pre_process": 0, "video": 0.5, "post_process": 1, "after_move": 2}

    def add_post_processor(self, pp, when="post_process"):
        self.pps.append((self.WHEN[when], pp))
