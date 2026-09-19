"""Text file I/O names its encoding, because Windows does not default to UTF-8.

Python opens text files in the locale encoding. On Linux that is UTF-8 and the
omission is invisible; on Windows it is cp1252, and every read of a file with a
single non-ASCII byte raises.

That is not hypothetical. The knowledge maintenance schedule ran on the PC,
read the knowledge notes with no encoding, and failed on the first note
containing a dash it did not recognise:

    'charmap' codec can't decode byte 0x90 in position 12284:
    character maps to <undefined>

It reported failure every run and was one of five strikes from switching itself
off permanently. The doctor caught it; this keeps it from coming back.

The scan is AST-based on purpose. A grep would trip over the `write_text` calls
that live inside generated-script string literals, which are that script's I/O
and not this file's.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEXT_IO = {"read_text", "write_text"}
# Third-party sources and anything vendored are not ours to hold to this.
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "site-packages", "dist", "build"}


def _sources() -> list[Path]:
    return [
        path
        for path in REPO.rglob("*.py")
        if not SKIP_PARTS.intersection(path.parts)
    ]


def _offenders(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in TEXT_IO:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        # Path.read_text(enc) positionally is also explicit.
        if func.attr == "read_text" and node.args:
            continue
        if func.attr == "write_text" and len(node.args) > 1:
            continue
        found.append((node.lineno, func.attr))
    return found


def test_every_text_read_and_write_names_its_encoding():
    offenders = []
    for path in _sources():
        for line, call in _offenders(path):
            offenders.append(f"{path.relative_to(REPO)}:{line} {call}()")

    assert not offenders, (
        "these default to the locale encoding, which is cp1252 on Windows:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_scan_actually_finds_something(tmp_path):
    """A guard that cannot fail is not a guard."""

    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "Path('a').read_text()\n"
        "Path('b').write_text('x')\n",
        encoding="utf-8",
    )

    assert [call for _line, call in _offenders(sample)] == ["read_text", "write_text"]


def test_an_explicit_encoding_is_accepted(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from pathlib import Path\n"
        "Path('a').read_text(encoding='utf-8')\n"
        "Path('b').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert _offenders(sample) == []


def test_a_string_literal_is_not_scanned(tmp_path):
    """Generated scripts carry their own write_text; it is not this file's I/O."""

    sample = tmp_path / "gen.py"
    sample.write_text(
        "from pathlib import Path\n"
        "Path('x').write_text('''\n"
        "Path('inner').write_text(str(1))\n"
        "''', encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert _offenders(sample) == []
