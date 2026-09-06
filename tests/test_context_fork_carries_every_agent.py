"""A context fork must carry every readable side of a linked thread.

The fork scanned members for Claude and Codex only, and refused a thread that
was not exactly those two -- so a Codex+Gemini thread could not be forked at
all. Worse, the renderer ran the Claude parser over every member regardless of
agent, which returns zero messages for an Antigravity transcript. Had the scan
let Gemini through unchanged, the fork would have shipped a bundle that named
the Gemini chat in a heading and carried none of its text.

Now the thread needs any two agents that can be read, and each side is parsed
by its own reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chats import context_fork


def _claude_transcript(path: Path, texts: list[tuple[str, str]]) -> Path:
    lines = []
    for role, text in texts:
        lines.append(json.dumps({
            "type": role,
            "timestamp": "2026-09-03T15:24:03.000Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _gemini_transcript(path: Path, texts: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for role, text in texts:
        if role == "user":
            lines.append(json.dumps({"type": "USER_INPUT", "created_at": "2026-09-03T15:24:03Z",
                                     "content": f"<USER_REQUEST>{text}</USER_REQUEST>"}))
        else:
            lines.append(json.dumps({"type": "PLANNER_RESPONSE",
                                     "created_at": "2026-09-03T15:24:09Z", "content": text}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def thread(tmp_path, monkeypatch):
    """A linked Codex+Gemini thread, wired through the module's lookups."""
    cid = "9984a527-b5fa-4226-a9e3-e55661c8d9f1"
    from core import gemini_scanner

    monkeypatch.setattr(gemini_scanner, "GEMINI_ROOT", tmp_path / "agy")
    _gemini_transcript(
        tmp_path / "agy" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl",
        [("user", "gemini side question"), ("assistant", "gemini side answer")],
    )
    indexed_db = tmp_path / "agy" / "conversations" / f"{cid}.db"
    indexed_db.parent.mkdir(parents=True, exist_ok=True)
    indexed_db.write_bytes(b"SQLite format 3\x00")

    codex_file = _claude_transcript(
        tmp_path / "codex.jsonl",
        [("user", "codex side question"), ("assistant", "codex side answer")],
    )

    members = {
        "codex-1": {"session_id": "codex-1", "agent": "codex", "file_path": str(codex_file),
                    "title": "Unified App Changes", "cwd": str(tmp_path),
                    "last_timestamp": "2026-09-03T16:00:00Z"},
        "gem-1": {"session_id": "gem-1", "agent": "gemini", "file_path": str(indexed_db),
                  "title": "Unified App Changes", "cwd": str(tmp_path),
                  "last_timestamp": "2026-09-03T15:00:00Z"},
    }
    monkeypatch.setattr(context_fork, "get_session", lambda sid: members.get(sid))
    monkeypatch.setattr(context_fork.metadata, "get_group", lambda sid: "g1" if sid in members else None)
    monkeypatch.setattr(context_fork.metadata, "list_group_members", lambda gid: list(members))
    monkeypatch.setattr(context_fork.metadata, "get_meta", lambda sid: {})
    return members


def test_a_codex_gemini_thread_forks_and_carries_both_sides(thread, tmp_path) -> None:
    result = context_fork.build_context_fork("codex-1", "claude", output_dir=tmp_path / "out")

    bundle = Path(result["context_path"]).read_text(encoding="utf-8")

    assert "codex side question" in bundle
    assert "gemini side question" in bundle, "the Gemini transcript was named but not carried"
    assert "gemini side answer" in bundle
    assert result["message_count"] == 4
    assert sorted(result["source_session_ids"]) == ["codex-1", "gem-1"]


def test_the_receiving_agent_is_told_which_chats_it_is_reading(thread, tmp_path) -> None:
    result = context_fork.build_context_fork("codex-1", "claude", output_dir=tmp_path / "out")

    assert "Codex and Gemini chats" in result["prompt"]
    assert "Claude and Codex chats" not in result["prompt"]


def test_a_thread_of_one_agent_is_refused_with_a_reason(tmp_path, monkeypatch) -> None:
    only = {"session_id": "c1", "agent": "claude", "file_path": str(
        _claude_transcript(tmp_path / "a.jsonl", [("user", "hi")])), "cwd": str(tmp_path)}
    monkeypatch.setattr(context_fork, "get_session", lambda sid: only if sid == "c1" else None)
    monkeypatch.setattr(context_fork.metadata, "get_group", lambda sid: "g1")
    monkeypatch.setattr(context_fork.metadata, "list_group_members", lambda gid: ["c1"])
    monkeypatch.setattr(context_fork.metadata, "get_meta", lambda sid: {})

    with pytest.raises(ValueError, match="two chats from different agents"):
        context_fork.build_context_fork("c1", "claude", output_dir=tmp_path / "out")


def test_gemini_is_read_by_its_own_reader_not_the_claude_one(tmp_path, monkeypatch) -> None:
    """The specific silent failure: parse_full returns nothing for Antigravity."""
    from core import gemini_scanner
    from core.parser import parse_full

    cid = "abc"
    monkeypatch.setattr(gemini_scanner, "GEMINI_ROOT", tmp_path)
    transcript = _gemini_transcript(
        tmp_path / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl",
        [("user", "only reachable by the gemini reader")],
    )

    assert list(parse_full(transcript)) == [], "premise changed: parse_full now reads Antigravity"
    assert context_fork._read_turns("gemini", transcript) == [
        ("user", "only reachable by the gemini reader", "2026-09-03T15:24:03Z")
    ]


def test_a_gemini_chat_with_no_transcript_contributes_nothing_rather_than_raising(
    tmp_path, monkeypatch
) -> None:
    from core import gemini_scanner

    monkeypatch.setattr(gemini_scanner, "GEMINI_ROOT", tmp_path)
    stale = tmp_path / "conversations" / "gone.db"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"SQLite format 3\x00")

    assert context_fork._read_turns("gemini", stale) == []
