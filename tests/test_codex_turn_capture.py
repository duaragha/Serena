"""Regression coverage for evidence read from a reused Codex chat."""

from __future__ import annotations

import hashlib
import json
import time

from core import codex_bridge
from core.codex_turn_capture import capture_turn, wait_for_turn


def _write_rollout(tmp_path, sid: str, payloads: list[dict]):
    root = tmp_path / "sessions" / "2026" / "08" / "03"
    root.mkdir(parents=True)
    path = root / f"rollout-2026-08-03T00-00-00-{sid}.jsonl"
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in payloads),
        encoding="utf-8",
    )
    return path


def test_reused_turn_capture_requires_the_exact_committed_prompt(tmp_path, monkeypatch):
    """A neighbouring manual turn must never be charged to the voice job."""

    sid = "019fcaaa-1111-7222-8333-123456789abc"
    prompt = "fix only the selected project"
    _write_rollout(
        tmp_path,
        sid,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "manual"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "max"},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "exec-1",
                    "input": (
                        'const result = await tools.exec_command({cmd:".venv/bin/pytest -q"}); '
                        "text(JSON.stringify(result));"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "exec-1",
                    "output": '{"exit_code":0,"output":"2 passed"}',
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "implemented and tested"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "implemented and tested",
                },
            },
        ],
    )
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")

    turn = capture_turn(
        sid,
        start_offset=0,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    )

    assert turn.saw_prompt is True
    assert turn.completed is True
    assert turn.message == "implemented and tested"
    assert (turn.model, turn.effort) == ("gpt-5.6-sol", "max")
    assert turn.commands == [
        {
            "command": ".venv/bin/pytest -q",
            "exit_code": 0,
            "output": '{"exit_code":0,"output":"2 passed"}',
        }
    ]


def test_reused_turn_capture_ignores_commands_without_real_exit_receipts(
    tmp_path, monkeypatch
):
    """Worker prose or bare output must not become fake passing evidence."""

    sid = "019fcaaa-1111-7222-8333-abcdef123456"
    prompt = "run the tests"
    _write_rollout(
        tmp_path,
        sid,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "exec-2",
                    "input": 'const result = await tools.exec_command({cmd:"pytest -q"}); text(result.output);',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "exec-2",
                    "output": "Script completed\nOutput:\nlooks fine",
                },
            },
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ],
    )
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")

    turn = capture_turn(
        sid,
        start_offset=0,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    )

    assert turn.completed is True
    assert turn.commands == []


def test_missing_reused_transcript_is_an_immediate_unavailable_result(
    tmp_path, monkeypatch
):
    sid = "019fcaaa-1111-7222-8333-feedface1234"
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "missing")

    started = time.monotonic()
    turn = wait_for_turn(
        sid,
        start_offset=3902,
        prompt_sha256="committed-digest",
        timeout=60,
        poll_interval=0.05,
    )

    assert time.monotonic() - started < 1
    assert turn.source_available is False
    assert turn.completed is False
    assert "no Codex rollout" in turn.source_error


def test_waiting_reused_turn_stops_when_exact_owner_cannot_progress(
    tmp_path, monkeypatch
):
    sid = "019fcaaa-1111-7222-8333-deadfeed1234"
    _write_rollout(tmp_path, sid, [{"type": "session_meta", "payload": {"id": sid}}])
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")

    turn = wait_for_turn(
        sid,
        start_offset=1,
        prompt_sha256="committed-digest",
        timeout=60,
        poll_interval=0.05,
        progress_probe=lambda: "the exact Codex owner died",
        progress_probe_interval=0.05,
    )

    assert turn.source_available is True
    assert turn.completed is False
    assert turn.progress_error == "the exact Codex owner died"
