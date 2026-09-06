"""Handing off FROM a Gemini chat.

The briefing refused this direction, on the grounds that Antigravity stores a
conversation as protobuf in a SQLite table with no published schema. That is
true of the ``.db``, but Antigravity also writes the same turns as plain JSONL
under ``brain/<id>/.system_generated/logs/transcript.jsonl``. The conversation
is indexed by the ``.db`` when both exist -- that is the file whose mtime
tracks the conversation -- so reading one means resolving the id to the
transcript rather than opening the indexed path.
"""

from __future__ import annotations

import json
from pathlib import Path

from chats import handoff


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def _transcript(tmp_path: Path, records: list[dict]) -> Path:
    return _write(tmp_path / "transcript.jsonl", records)


def test_the_prompt_is_read_without_its_metadata_wrapper(tmp_path: Path) -> None:
    """Antigravity wraps the typed text in tags and appends session chatter."""
    path = _transcript(tmp_path, [
        {
            "type": "USER_INPUT",
            "content": (
                "<USER_REQUEST>\nfix the sidebar filter\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-03T11:24:03-04:00.\n"
                "</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
                "`Model Selection` from None to Gemini 3.8 Flash (High).\n</USER_SETTINGS_CHANGE>"
            ),
        },
    ])

    messages = handoff._parse_gemini(path)

    assert [m.text for m in messages] == ["fix the sidebar filter"]
    assert messages[0].role == "user"


def test_the_model_turn_carries_its_prose_and_each_tool_call(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [
        {
            "type": "PLANNER_RESPONSE",
            "content": "Looking at the fold now.",
            "thinking": "internal",
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": "grep -n fold web.py"}},
                {"name": "view_file", "args": {"Path": "ui/web.py"}},
            ],
        },
    ])

    messages = handoff._parse_gemini(path)

    assert [m.role for m in messages] == ["assistant"] * 3
    assert messages[0].text == "Looking at the fold now."
    assert [m.tool_name for m in messages[1:]] == ["run_command", "view_file"]
    assert "grep -n fold web.py" in messages[1].tool_input


def test_tool_output_is_not_mistaken_for_conversation(tmp_path: Path) -> None:
    """GENERIC steps echo command output -- including this transcript's own
    lines, when a command happens to cat it. Quoting them back would have the
    briefing recite the log to itself."""
    path = _transcript(tmp_path, [
        {"type": "USER_INPUT", "content": "<USER_REQUEST>go</USER_REQUEST>"},
        {"type": "GENERIC", "content": 'The command exited with code 0.\nOutput:\n{"type":"USER_INPUT"}'},
        {"type": "SYSTEM_MESSAGE", "content": "<SYSTEM_MESSAGE>task canceled</SYSTEM_MESSAGE>"},
        {"type": "CHECKPOINT", "content": "{{ CHECKPOINT 0 }} earlier parts truncated"},
        {"type": "PLANNER_RESPONSE", "content": "done"},
    ])

    messages = handoff._parse_gemini(path)

    assert [(m.role, m.text) for m in messages] == [("user", "go"), ("assistant", "done")]


def test_a_damaged_line_does_not_lose_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>one</USER_REQUEST>"})
        + "\n{ this is not json\n\n[]\n"
        + json.dumps({"type": "PLANNER_RESPONSE", "content": "two"})
        + "\n",
        encoding="utf-8",
    )

    assert [m.text for m in handoff._parse_gemini(path)] == ["one", "two"]


def test_the_db_path_resolves_to_the_transcript_beside_it(tmp_path: Path, monkeypatch) -> None:
    """The indexed path is the unreadable one; the id is the way through."""
    from core import gemini_scanner

    monkeypatch.setattr(gemini_scanner, "GEMINI_ROOT", tmp_path)
    cid = "9984a527-b5fa-4226-a9e3-e55661c8d9f1"
    wanted = _write(
        tmp_path / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl",
        [{"type": "USER_INPUT", "content": "<USER_REQUEST>hi</USER_REQUEST>"}],
    )
    indexed = tmp_path / "conversations" / f"{cid}.db"
    indexed.parent.mkdir(parents=True, exist_ok=True)
    indexed.write_bytes(b"SQLite format 3\x00")

    assert handoff._gemini_transcript(indexed) == wanted
    assert handoff._gemini_transcript(wanted) == wanted
    assert handoff._gemini_transcript(tmp_path / "conversations" / "unknown-id.db") is None


def test_a_conversation_with_no_transcript_says_so_and_names_the_way_out(
    tmp_path: Path, monkeypatch
) -> None:
    """Conversations predating brain/ still cannot be read; that must stay a
    clear message rather than an empty briefing."""
    from core import gemini_scanner

    monkeypatch.setattr(gemini_scanner, "GEMINI_ROOT", tmp_path)
    stale = tmp_path / "conversations" / "090baaeb-feed-4a15-b3a4-176ca5eb2481.db"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"SQLite format 3\x00")

    monkeypatch.setattr(
        handoff,
        "get_session",
        lambda sid: {"agent": "gemini", "file_path": str(stale), "title": "old chat", "cwd": ""},
    )

    result = handoff.build_handoff_briefing("090baaeb-feed-4a15-b3a4-176ca5eb2481")

    assert result["ok"] is False
    assert "no readable transcript" in result["error"]
    assert "Claude or Codex" in result["error"]


def test_a_real_gemini_chat_produces_a_briefing() -> None:
    """The end the user actually hits, over whatever is on this machine."""
    import sqlite3

    from core.config import DB_PATH

    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "select session_id from sessions where agent='gemini' order by last_timestamp desc"
    ).fetchall()
    conn.close()
    if not rows:
        import pytest

        pytest.skip("no Gemini conversations on this machine")

    results = [handoff.build_handoff_briefing(sid) for (sid,) in rows]
    briefable = [r for r in results if r.get("ok")]

    assert briefable, f"every Gemini chat refused: {[r.get('error') for r in results]}"
    for result in briefable:
        assert result["agent"] == "gemini"
        assert "# Handoff Briefing" in result["briefing"]
        assert "Gemini transcripts cannot be read" not in result["briefing"]
