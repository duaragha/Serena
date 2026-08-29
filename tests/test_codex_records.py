"""Codex changed how it records a turn, and Serena only knew the old shape.

Up to 0.146 each turn produced an ``event_msg`` whose payload type was
``user_message`` or ``agent_message``. From 0.150 those are gone and the turn is
a ``response_item`` message with a role, so every new Codex chat indexed as zero
messages, opened as an empty transcript, and hung the ask-codex bridge waiting
for an event that was never coming.

Two traps live here. Older rollouts carry BOTH shapes for one turn, so accepting
both doubles every historical chat. And the new shape exposes the AGENTS.md and
environment blocks Codex injects into the user slot, which the old events never
showed and which would title every chat "AGENTS.md instructions".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import codex_records
from core.chat_daemon import _codex_messages
from core.codex_scanner import parse_codex_metadata
from core.parser import _parse_codex_full


def _event(kind: str, text: str, ts: str = "2026-08-29T08:00:00.000Z") -> dict:
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": kind, "message": text}}


def _item(role: str, text: str, ts: str = "2026-08-29T08:00:00.000Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    }


def _meta(version: str = "0.150.1") -> dict:
    return {
        "timestamp": "2026-08-29T08:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "session_id": "01a04c88-cab5-7b43-b8b0-d4a9467b7236",
            "timestamp": "2026-08-29T08:00:00.000Z",
            "cwd": "/home/raghav",
            "originator": "codex-tui",
            "cli_version": version,
        },
    }


def _rollout(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "rollout-2026-08-29T08-00-00-01a04c88-cab5-7b43-b8b0-d4a9467b7236.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


AGENTS_BLOB = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nlots of setup\n</INSTRUCTIONS>"


def test_the_new_format_is_read(tmp_path: Path) -> None:
    path = _rollout(tmp_path, [_meta(), _item("user", "look into Hermes"), _item("assistant", "on it")])

    assert codex_records.read_messages(path) == [
        ("user", "look into Hermes", "2026-08-29T08:00:00.000Z"),
        ("assistant", "on it", "2026-08-29T08:00:00.000Z"),
    ]


def test_a_rollout_carrying_both_shapes_counts_each_turn_once(tmp_path: Path) -> None:
    """0.146 wrote the event and the item for the same turn."""
    path = _rollout(tmp_path, [
        _meta("0.146.0"),
        _event("user_message", "hello"),
        _item("user", "hello"),
        _event("agent_message", "hi back"),
        _item("assistant", "hi back"),
    ])

    messages = codex_records.read_messages(path)

    assert len(messages) == 2, f"every turn counted twice: {messages}"
    assert [role for role, _t, _ts in messages] == ["user", "assistant"]


@pytest.mark.parametrize("blob", [
    AGENTS_BLOB,
    "# Files mentioned by the user:\n\n## codex-clipboard.png",
    "<environment_context>\n  <current_date>2026-08-29</current_date>\n</environment_context>",
    '<in-app-browser-context source="ambient-ui-state">stuff</in-app-browser-context>',
    "<memory-context>\n{}\n</memory-context>",
])
def test_context_codex_injects_is_not_a_turn(tmp_path: Path, blob: str) -> None:
    path = _rollout(tmp_path, [_meta(), _item("user", blob), _item("user", "the real question")])

    assert [text for _r, text, _ts in codex_records.read_messages(path)] == ["the real question"]


def test_a_message_that_merely_starts_with_a_bracket_is_still_a_turn(tmp_path: Path) -> None:
    """The filter keys on a tag block, not on the first character."""
    typed = "<- that arrow is part of what I typed"
    path = _rollout(tmp_path, [_meta(), _item("user", typed)])

    assert [text for _r, text, _ts in codex_records.read_messages(path)] == [typed]


def test_codex_scaffolding_is_not_a_turn(tmp_path: Path) -> None:
    """developer-role records are skills, permissions and aborted-turn notices."""
    path = _rollout(tmp_path, [
        _meta(),
        _item("developer", "<skills_instructions>...</skills_instructions>"),
        _item("user", "actual question"),
    ])

    assert [role for role, _t, _ts in codex_records.read_messages(path)] == ["user"]


def test_the_title_comes_from_what_was_typed_not_what_was_injected(tmp_path: Path) -> None:
    path = _rollout(tmp_path, [
        _meta(),
        _item("user", AGENTS_BLOB),
        _item("user", "look into Hermes"),
        _item("assistant", "on it"),
    ])

    meta = parse_codex_metadata(path)
    data = meta if isinstance(meta, dict) else vars(meta)

    assert data["message_count"] == 2
    assert data["first_message"] == "look into Hermes"


def test_every_reader_agrees_on_the_same_rollout(tmp_path: Path) -> None:
    """The transcript, the phone and the index must not disagree about a chat."""
    path = _rollout(tmp_path, [
        _meta(),
        _item("user", AGENTS_BLOB),
        _item("user", "look into Hermes"),
        _item("assistant", "on it"),
    ])

    meta = parse_codex_metadata(path)
    data = meta if isinstance(meta, dict) else vars(meta)

    assert len(_parse_codex_full(path)) == 2
    assert len(_codex_messages(path, "sid")) == 2
    assert data["message_count"] == 2


def test_an_empty_or_unparseable_rollout_does_not_raise(tmp_path: Path) -> None:
    broken = tmp_path / "rollout-2026-08-29T08-00-00-01a04c88-cab5-7b43-b8b0-d4a9467b7236.jsonl"
    broken.write_text("not json\n\n{\"type\":\"event_msg\"}\n", encoding="utf-8")

    assert codex_records.read_messages(broken) == []
    assert _parse_codex_full(broken) == []
