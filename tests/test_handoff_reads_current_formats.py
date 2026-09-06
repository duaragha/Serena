"""The handoff must read the rollout shapes Codex actually writes.

`chats/handoff.py` carried its own rollout reader that looked for `event_msg`
payloads named `user_message` / `agent_message`. Codex has since moved turns
into `response_item` records and wrapped the events in `item_completed`, so on
a current rollout that search matched nothing and the handoff failed with "No
messages to summarize" -- 64 of the 282 Codex chats on this machine.

`core/codex_records.py` already understood both shapes, having been fixed for
the sidebar. The duplicate reader is what went stale, so these tests pin the
handoff to the shared one and cover the shapes that broke it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chats import handoff
from core.config import DB_PATH


def _rollout(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "rollout-2026-09-06T00-00-00-test.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _item(role: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role,
                    "content": [{"type": "output_text", "text": text}]},
    }


def _event(kind: str, text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": kind, "message": text}}


def test_the_new_response_item_shape_is_read(tmp_path: Path) -> None:
    """The shape that produced "No messages to summarize"."""
    path = _rollout(tmp_path, [
        {"type": "session_meta", "payload": {"id": "x", "source": "cli"}},
        _item("user", "fix the handoff"),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
        _item("assistant", "on it"),
    ])

    messages = handoff._parse_codex(path)

    assert [(m.role, m.text) for m in messages] == [("user", "fix the handoff"), ("assistant", "on it")]


def test_the_legacy_event_shape_still_works(tmp_path: Path) -> None:
    path = _rollout(tmp_path, [_event("user_message", "hello"), _event("agent_message", "hi")])

    messages = handoff._parse_codex(path)

    assert [(m.role, m.text) for m in messages] == [("user", "hello"), ("assistant", "hi")]


def test_a_file_carrying_both_shapes_does_not_double_count(tmp_path: Path) -> None:
    """Rollouts exist with both envelopes; the events win, per codex_records."""
    path = _rollout(tmp_path, [
        _event("user_message", "hello"),
        _item("user", "hello"),
        _event("agent_message", "hi"),
        _item("assistant", "hi"),
    ])

    messages = [m for m in handoff._parse_codex(path) if not m.tool_name]

    assert [(m.role, m.text) for m in messages] == [("user", "hello"), ("assistant", "hi")]


def test_both_tool_call_shapes_survive_either_transcript(tmp_path: Path) -> None:
    """Tool calls are only ever response_item, so they must not be dropped
    when the event-shaped turns win."""
    call = {"type": "response_item",
            "payload": {"type": "function_call", "name": "sleep",
                        "arguments": '{"duration_ms":45000}'}}
    custom = {"type": "response_item",
              "payload": {"type": "custom_tool_call", "name": "shell",
                          "input": "grep -rn fold ui/web.py"}}

    for name in ("a", "b"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    with_events = handoff._parse_codex(
        _rollout(tmp_path / "a", [_event("user_message", "go"), call, custom]))
    with_items = handoff._parse_codex(
        _rollout(tmp_path / "b", [_item("user", "go"), call, custom]))

    for messages in (with_events, with_items):
        tools = [(m.tool_name, m.tool_input) for m in messages if m.tool_name]
        assert [t[0] for t in tools] == ["sleep", "shell"]
        assert "45000" in tools[0][1]
        assert "grep -rn fold" in tools[1][1]


def test_injected_context_is_not_treated_as_something_the_user_typed(tmp_path: Path) -> None:
    """codex_records filters these; going through it means the handoff does too."""
    path = _rollout(tmp_path, [
        _item("user", "<environment_context>cwd=/home/raghav</environment_context>"),
        _item("user", "the real question"),
    ])

    assert [m.text for m in handoff._parse_codex(path)] == ["the real question"]


def test_the_handoff_does_not_keep_its_own_rollout_reader() -> None:
    """Two readers drifting apart is the bug itself, not a detail of it."""
    source = Path("chats/handoff.py").read_text(encoding="utf-8")
    body = source[source.index("def _parse_codex("):source.index("def _render_briefing(")]

    code = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith("#")
    )
    code = code[:code.index('"""')] + code[code.index('"""', code.index('"""') + 3) + 3:]

    assert "codex_records.iter_records" in code
    assert "json.loads" not in code, "handoff is re-parsing rollout lines itself"
    assert ".open(" not in code, "handoff is re-opening the rollout itself"
    assert '"event_msg"' not in code, "handoff is matching rollout envelopes again"


@pytest.mark.parametrize("agent", ["codex", "gemini"])
def test_every_chat_of_this_agent_on_this_machine_can_be_handed_off(agent: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "select session_id, file_path from sessions where agent=? order by last_timestamp desc limit 40",
        (agent,),
    ).fetchall()
    conn.close()
    live = [sid for sid, path in rows if Path(path).exists()]
    if not live:
        pytest.skip(f"no {agent} chats on this machine")

    failures = []
    for sid in live:
        result = handoff.build_handoff_briefing(sid)
        if not result.get("ok"):
            failures.append((sid, result.get("error")))

    assert not failures, f"{len(failures)}/{len(live)} {agent} chats refuse handoff: {failures[:3]}"
