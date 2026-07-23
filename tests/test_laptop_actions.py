from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.laptop_actions import execute_laptop_action


def _origin(text: str) -> dict[str, str]:
    return {
        "protocol": "voice",
        "call_id": "desk-test",
        "turn_id": "desk-test:1",
        "text": text,
    }


def test_direct_reversible_action_executes_and_writes_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "core.laptop_actions._command_for",
        lambda action, target: ["/trusted/pactl", action, target],
    )
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    audit = tmp_path / "actions.jsonl"
    result = execute_laptop_action(
        "volume_down",
        "",
        origin=_origin("volume down"),
        audit_path=audit,
        runner=runner,
    )

    assert result.ok is True
    assert calls == [["/trusted/pactl", "volume_down", ""]]
    receipt = json.loads(audit.read_text(encoding="utf-8"))
    assert receipt["status"] == "completed"
    assert receipt["origin_sha256"]
    assert "volume down" not in audit.read_text(encoding="utf-8")


def test_question_negation_and_remote_call_never_execute(tmp_path: Path) -> None:
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("denied action reached the runner")

    origins = [
        _origin("can you mute the laptop"),
        _origin("do not mute the laptop"),
        {**_origin("mute the laptop"), "call_id": "phone-call"},
    ]
    for index, origin in enumerate(origins):
        result = execute_laptop_action(
            "mute",
            "",
            origin=origin,
            audit_path=tmp_path / f"audit-{index}.jsonl",
            runner=runner,
        )
        assert result.ok is False
        assert result.status == "denied"
    assert calls == []


def test_arbitrary_typing_is_not_representable(tmp_path: Path) -> None:
    result = execute_laptop_action(
        "type_text",
        "send this",
        origin=_origin("type this message"),
        audit_path=tmp_path / "actions.jsonl",
    )
    assert result.ok is False
    assert "outside" in result.message


def test_open_app_requires_named_allowlisted_target(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "core.laptop_actions._command_for",
        lambda action, target: ["/trusted/gtk-launch", target],
    )
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    denied = execute_laptop_action(
        "open_app",
        "terminal",
        origin=_origin("open the browser"),
        audit_path=tmp_path / "denied.jsonl",
        runner=runner,
    )
    allowed = execute_laptop_action(
        "open_app",
        "terminal",
        origin=_origin("open terminal"),
        audit_path=tmp_path / "allowed.jsonl",
        runner=runner,
    )
    assert denied.ok is False
    assert allowed.ok is True
