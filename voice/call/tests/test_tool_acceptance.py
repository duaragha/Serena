from __future__ import annotations

import json

import psutil
import pytest

from voice.call import tool_acceptance


def _tool_rows(name: str, *, include_result: bool = True) -> list[dict]:
    rows = [
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": name,
                        "input": {"repo": "serena"},
                    }
                ]
            }
        }
    ]
    if include_result:
        rows.append(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "result",
                        }
                    ]
                }
            }
        )
    return rows


def test_tool_evidence_requires_allowlisted_completed_call() -> None:
    result = tool_acceptance._tool_evidence(
        _tool_rows("mcp__serena-ro__git_latest"),
        "mcp__serena-ro__git_latest",
    )

    assert result == {
        "expected": "mcp__serena-ro__git_latest",
        "used": ["mcp__serena-ro__git_latest"],
        "completed": 1,
    }

    with pytest.raises(tool_acceptance.AcceptanceError, match="outside"):
        tool_acceptance._tool_evidence(
            _tool_rows("Bash"),
            "mcp__serena-ro__git_latest",
        )
    with pytest.raises(tool_acceptance.AcceptanceError, match="did not return"):
        tool_acceptance._tool_evidence(
            _tool_rows("mcp__serena-ro__git_latest", include_result=False),
            "mcp__serena-ro__git_latest",
        )


def test_assistant_process_census_matches_only_current_user_clis() -> None:
    username = psutil.Process().username()

    class Candidate:
        def __init__(self, pid: int, command: list[str], owner: str) -> None:
            self.info = {
                "pid": pid,
                "cmdline": command,
                "create_time": float(pid),
                "status": psutil.STATUS_RUNNING,
                "username": owner,
            }

    candidates = [
        Candidate(1, ["/usr/bin/codex", "exec"], username),
        Candidate(2, ["/opt/claude", "--resume"], username),
        Candidate(3, ["python", "-m", "voice.call.process_worker"], username),
        Candidate(4, ["/usr/bin/codex", "exec"], "different-user"),
    ]

    result = tool_acceptance._assistant_cli_processes(
        process_iter=lambda **_kwargs: candidates
    )

    assert result == {1: 1.0, 2: 2.0}


def test_assistant_process_census_fails_closed_on_same_user_unreadable() -> None:
    candidate = type(
        "Candidate",
        (),
        {
            "info": {
                "pid": 7,
                "cmdline": None,
                "create_time": 1.0,
                "status": psutil.STATUS_RUNNING,
                "username": psutil.Process().username(),
            }
        },
    )()

    with pytest.raises(tool_acceptance.AcceptanceError, match="no inspectable"):
        tool_acceptance._assistant_cli_processes(
            process_iter=lambda **_kwargs: [candidate]
        )


def test_turn_evidence_binds_tool_and_answer_to_exact_voice_marker() -> None:
    call_id = "v25c-12345678"
    turn_id = f"{call_id}:1"
    answer = "main is current and the newest commit is test."
    context = json.dumps(
        {"call_id": call_id, "turn_id": turn_id}, separators=(",", ":")
    )
    rows = [
        *_tool_rows("mcp__serena-ro__git_latest"),
        {
            "type": "user",
            "message": {
                "content": (
                    f"<voice-turn-context>{context}</voice-turn-context>\n"
                    "fresh-check marker 12345678"
                )
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "bound-tool",
                        "name": "mcp__serena-ro__git_latest",
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "bound-tool"}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": answer}]},
        },
    ]

    result = tool_acceptance._turn_evidence(
        rows,
        expected_tool="mcp__serena-ro__git_latest",
        call_id=call_id,
        turn_id=turn_id,
        response=answer,
    )

    assert result["call_id_bound"] is True
    assert result["turn_id_bound"] is True
    with pytest.raises(tool_acceptance.AcceptanceError, match="returned voice answer"):
        tool_acceptance._turn_evidence(
            rows,
            expected_tool="mcp__serena-ro__git_latest",
            call_id=call_id,
            turn_id=turn_id,
            response="a different concurrent answer",
        )


def test_pane_guard_retains_transient_cli_identity(monkeypatch) -> None:
    samples = iter(({1: 1.0}, {1: 1.0, 2: 2.0}, {1: 1.0}))
    monkeypatch.setattr(
        tool_acceptance,
        "_assistant_cli_processes",
        lambda: next(samples),
    )
    monkeypatch.setattr(tool_acceptance, "_jsonl_files", lambda _root: set())
    monkeypatch.setattr(tool_acceptance, "_metadata_manifest", lambda: {})
    guard = tool_acceptance._PaneGuard()

    guard._sample_processes()
    guard._sample_processes()

    assert guard._new_processes == {(2, 2.0)}


def test_cli_reports_unexpected_acceptance_failure_as_json(monkeypatch, capsys) -> None:
    async def fail(_args):
        raise RuntimeError("model runtime unavailable")

    monkeypatch.setattr(tool_acceptance, "run_acceptance", fail)

    assert tool_acceptance.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "acceptance": "v2.5c",
        "error": "model runtime unavailable",
    }
