"""yt-dlp's container rule for joined streams, which the worker asks for each preset's file type."""

from _fake import merged_ext


def get_compatible_ext(*, vcodecs, acodecs, vexts, aexts, preferences=None):
    return merged_ext([{"ext": e} for e in vexts + aexts])
