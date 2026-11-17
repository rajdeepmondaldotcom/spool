"""No sentence Spool sends to the page or prints names a program (ux/plan.md section 1)."""

import ast
import re
from pathlib import Path

import spool
from spool.jobs import MESSAGES, NOTES

SOURCE = Path(spool.__file__).parent
NAMES = re.compile(r"yt-dlp|youtube-dl|ffmpeg|ffprobe|deno", re.IGNORECASE)
INTERPOLATED_OK = {"url", "aside", "args.port", "exc.strerror"}  # links, paths and the OS's own words


def sentences():
    """(where, text) for every sentence the API can return or Spool can print, read from the source."""
    for file in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(file.read_text())):
            said = None
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Problem" and len(node.args) > 2:
                said = node.args[2]
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print" and node.args:
                said = node.args[0]
            elif isinstance(node, ast.Assign) and file.name == "engines.py":
                if any(getattr(t, "id", None) == "error" for t in node.targets):
                    said = node.value
            if said is None:
                continue
            parts = said.values if isinstance(said, ast.JoinedStr) else [said]
            for part in parts:
                where = f"{file.name}:{node.lineno}"
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield where, part.value
                elif isinstance(part, ast.FormattedValue):
                    yield where, "{" + ast.unparse(part.value) + "}"
    for kind, (words, _) in MESSAGES.items():
        yield f"MESSAGES[{kind}]", words
    for kind, words in NOTES.items():
        yield f"NOTES[{kind}]", words


def test_no_message_or_printed_line_names_a_program():
    found = list(sentences())
    assert len(found) > 40  # the scan reaches the messages
    bad = [(w, t) for w, t in found if NAMES.search(t)]
    bad += [(w, t) for w, t in found if t.startswith("{") and t.endswith("}") and t[1:-1] not in INTERPOLATED_OK]
    assert bad == []
