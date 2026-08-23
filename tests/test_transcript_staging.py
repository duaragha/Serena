"""A chat must never resume against a half-written transcript.

On the PC, opening a chat showed it going live and then dying back to "Ready to
resume". `claude -r` was finding the session file under the cwd's slug empty and
exiting immediately. The transcripts are tens of megabytes and get rewritten to
their union after every turn, with open("w"), which truncates first: anything
launched during that window read nothing. A write that failed left the file at
zero bytes for good, and the staging step trusted mere existence, so it never
repaired one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import chat_daemon
from ui import web


def _record(uuid: str, ts: str) -> str:
    return json.dumps({"uuid": uuid, "timestamp": ts, "text": "x" * 200})


def _session(path: Path, records: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(chat_daemon, "_projects_root", lambda: root)
    return root


def test_a_failed_rewrite_leaves_the_transcript_intact(projects: Path, monkeypatch) -> None:
    """The old code truncated first, so a failure destroyed the session."""
    original = [_record("a", "1"), _record("b", "2")]
    target = _session(projects / "slug" / "s.jsonl", original)
    before = target.read_text(encoding="utf-8")

    real_open = Path.open

    def explode(self, *args, **kwargs):  # noqa: ANN001
        # Fail only the scratch file, so the assertions below can still read.
        if ".tmp-" in self.name:
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.open", explode)

    chat_daemon._atomic_write(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == before
    assert target.stat().st_size > 0


def test_no_session_file_is_ever_opened_for_truncation(projects: Path, monkeypatch) -> None:
    """The heart of it: a reader must never catch a transcript mid-rewrite.

    open("w") empties the file before the new bytes arrive. On a transcript
    measured in megabytes that window is wide enough for `claude -r` to launch,
    read nothing, and quit. Only a scratch file may be opened that way.
    """
    long = [_record(str(i), f"{i:03d}") for i in range(50)]
    _session(projects / "linux-slug" / "s.jsonl", long)
    _session(projects / "windows-slug" / "s.jsonl", long[:5])

    truncated: list[str] = []
    real_open = Path.open

    def watched_open(self, mode="r", *args, **kwargs):  # noqa: ANN001
        if "w" in mode and ".tmp-" not in self.name:
            truncated.append(str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.open", watched_open)

    chat_daemon._canonicalize_session("s")

    assert not truncated, f"these session files were truncated in place: {truncated}"


def test_canonicalizing_still_brings_a_stale_copy_up_to_the_union(projects: Path) -> None:
    long = [_record(str(i), f"{i:03d}") for i in range(50)]
    _session(projects / "linux-slug" / "s.jsonl", long)
    short = _session(projects / "windows-slug" / "s.jsonl", long[:5])

    chat_daemon._canonicalize_session("s")

    assert short.stat().st_size > 0
    assert len(short.read_text(encoding="utf-8").strip().splitlines()) == 50


def test_staging_repairs_an_empty_copy_instead_of_trusting_it(
    projects: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Presence is not completeness.

    Canonicalising usually heals an empty copy on its own, so this pins the
    fallback: with no other copy to union against, an empty staged file must
    still be replaced rather than accepted because it exists.
    """
    import re

    records = [_record(str(i), f"{i:03d}") for i in range(20)]
    _session(projects / "linux-slug" / "s.jsonl", records)
    monkeypatch.setattr(chat_daemon, "_canonicalize_session", lambda _sid: None)

    cwd = tmp_path / "work"
    cwd.mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    empty = projects / slug / "s.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")

    web._ensure_resumable("s", str(cwd))

    assert empty.stat().st_size > 0, "an empty staged copy must be re-staged, not trusted"
    assert len(empty.read_text(encoding="utf-8").strip().splitlines()) == 20


def test_staging_leaves_a_complete_copy_alone(
    projects: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-copying a megabyte transcript on every open would be its own bug."""
    import re

    records = [_record(str(i), f"{i:03d}") for i in range(20)]
    _session(projects / "linux-slug" / "s.jsonl", records)
    monkeypatch.setattr(chat_daemon, "_canonicalize_session", lambda _sid: None)

    cwd = tmp_path / "work"
    cwd.mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    staged = _session(projects / slug / "s.jsonl", records)
    stamp = staged.stat().st_mtime_ns

    web._ensure_resumable("s", str(cwd))

    assert staged.stat().st_mtime_ns == stamp, "a complete copy must not be rewritten"
