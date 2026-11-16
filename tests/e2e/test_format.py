"""The display formats in static/format.js, checked against the brief's worked examples."""

CASES = [
    ("bytes", [474478], "474 KB"),
    ("bytes", [3_100_000], "3.1 MB"),
    ("bytes", [47_340_000], "47.3 MB"),
    ("bytes", [507477491712], "507 GB"),
    ("speed", [3_100_000], "3.1 MB/s"),
    ("speed", [17_000], "0.02 MB/s"),
    ("duration", [19], "0:19"),
    ("duration", [629], "10:29"),
    ("duration", [3723], "1:02:03"),
    ("timeLeft", [12], "12 s left"),
    ("timeLeft", [170], "3 min left"),
    ("timeLeft", [3900], "1 h 5 min left"),
    ("presetName", ["1080"], "1080p"),
    ("engineName", ["yt-dlp"], "Main"),
    ("engineName", ["youtube-dl"], "Backup"),
    ("tryOther", ["youtube-dl"], "Try the backup engine"),
    ("tryOther", ["yt-dlp"], "Try the main engine"),
    ("plain", ["ERROR: see the yt-dlp wiki or youtube_dl docs"], "ERROR: see the the engine wiki or the engine docs"),
    ("stageWords", [{"stage": "downloading", "part": 1, "preset": "best"}], "Downloading video"),
    ("stageWords", [{"stage": "downloading", "part": 2, "preset": "best"}], "Downloading sound"),
    ("stageWords", [{"stage": "processing", "preset": "mp3"}], "Converting to MP3"),
    (
        "destProblem",
        [{"problem": "unplugged", "path": "/Volumes/T7/Spool", "display": "T7 › Spool"}],
        "T7 isn't connected. Plug it in, or choose another folder.",
    ),
    (
        "destProblem",
        [{"problem": "missing", "path": "/x/Old", "display": "x › Old"}],
        "Old doesn't exist. Choose another folder.",
    ),
    (
        "destProblem",
        [{"problem": "not_a_folder", "path": "/x/a.txt", "display": "a.txt"}],
        "a.txt is a file, not a folder.",
    ),
    (
        "destProblem",
        [{"problem": "not_responding", "path": "/Volumes/NAS/Films", "display": "NAS › Films"}],
        "NAS isn't answering. If macOS is asking whether Spool may use it, choose Allow.",
    ),
    ("links", ["https://a.test/1\n  http://b.test x ftp://c.test"], ["https://a.test/1", "http://b.test"]),
]


def test_formats_match_the_brief(app):
    got = app.evaluate(
        "async (cases) => { const f = await import('/format.js'); return cases.map(([fn, args]) => f[fn](...args)); }",
        [[fn, args] for fn, args, _ in CASES],
    )
    assert got == [want for *_, want in CASES]
