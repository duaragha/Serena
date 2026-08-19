from __future__ import annotations

import asyncio
import json

import pytest

from voice import brain_bridge


class _Socket:
    def __init__(self, incoming: list[str] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"type":"state_change","value":0.5}',
        '{"type":"amplitude","value":true}',
        '{"type":"amplitude","value":-0.1}',
        '{"type":"amplitude","value":1.1}',
        '{"type":"amplitude","value":0.5,"extra":1}',
    ],
)
def test_amplitude_message_rejects_invalid_input(raw: str) -> None:
    assert brain_bridge.parse_client_message(raw) is None


def test_amplitude_message_is_normalized() -> None:
    parsed = brain_bridge.parse_client_message(
        '{"type":"amplitude","value":0.73}'
    )
    assert json.loads(parsed or "null") == {"type": "amplitude", "value": 0.73}


@pytest.mark.parametrize(
    "message",
    [
        {"type": "code_start", "project": "serena"},
        {"type": "code_start", "project": "coding", "status": "ready"},
        {"type": "code_hide"},
        {"type": "code_done", "summary": "tests passed"},
        {
            "type": "code_event",
            "item_id": "job-123",
            "event": {"kind": "bash", "summary": "pytest -q", "detail": "passed"},
        },
        {
            "type": "fleet_notice",
            "run_id": "12503b47-ac94-4110-b546-27944b41c3c7",
            "state": "completed",
            "token": "completed:123.000000",
            "text": "Fleet finished successfully.",
        },
    ],
)
def test_local_code_events_are_validated(message: dict) -> None:
    parsed = brain_bridge.parse_local_event(json.dumps(message).encode())
    assert json.loads(parsed or "null") == message


@pytest.mark.parametrize(
    "message",
    [
        {"type": "code_start", "project": ""},
        {"type": "code_done", "summary": 42},
        {"type": "code_event", "event": {"kind": "secret", "summary": "no"}},
        {"type": "code_event", "event": {"kind": "text", "extra": "no"}},
        {"type": "code_event", "item_id": "bad/job", "event": {"kind": "text"}},
        {"type": "code_done", "summary": "done", "snapshot": {"state": "failed"}},
        {
            "type": "fleet_notice",
            "run_id": "not/a/run",
            "state": "completed",
            "token": "completed:123.000000",
            "text": "do not speak",
        },
    ],
)
def test_local_code_events_reject_invalid_payloads(message: dict) -> None:
    assert brain_bridge.parse_local_event(json.dumps(message).encode()) is None


def test_coding_snapshot_keeps_brief_project_evidence_and_controls() -> None:
    snapshot = {
        "item_id": "job-1",
        "state": "working",
        "project": "serena",
        "project_root": "/tmp/serena",
        "brief": {"request": "fix it"},
        "model": {"requested": "gpt-5.6-sol", "effort": "xhigh"},
        "progress": {"attempt": 1},
        "changes": ["core/runtime.py"],
        "tests": [{"command": "pytest -q", "exit_code": 0}],
        "live_proof": [],
        "evidence": {"complete": True},
        "review": {"state": "completed"},
        "controls": {"can_cancel": True, "can_steer": True, "can_resume": False},
        "summary": "",
    }
    message = {"type": "code_snapshot", "snapshot": snapshot}

    parsed = brain_bridge.parse_local_event(json.dumps(message).encode())

    assert json.loads(parsed or "null") == message


@pytest.mark.parametrize("action", ["status", "cancel", "resume"])
def test_overlay_job_controls_are_validated(action: str) -> None:
    parsed = brain_bridge.parse_client_message(
        json.dumps({"type": "code_control", "item_id": "job-1", "action": action})
    )
    assert json.loads(parsed or "null") == {
        "type": "code_control",
        "item_id": "job-1",
        "action": action,
    }


def test_overlay_steering_requires_bounded_text() -> None:
    assert brain_bridge.parse_client_message(
        '{"type":"code_control","item_id":"job-1","action":"steer","text":""}'
    ) is None
    parsed = brain_bridge.parse_client_message(
        '{"type":"code_control","item_id":"job-1","action":"steer",'
        '"text":"keep the API stable"}'
    )
    assert json.loads(parsed or "null")["text"] == "keep the API stable"


def test_idle_state_is_promoted_to_working_while_job_marker_exists(
    tmp_path,
    monkeypatch,
) -> None:
    state = tmp_path / "voice_state"
    working = tmp_path / "voice_working"
    state.write_text("idle\n", encoding="utf-8")
    working.write_text("working\n", encoding="utf-8")
    monkeypatch.setattr(brain_bridge, "STATE_FILE", state)
    monkeypatch.setattr(brain_bridge, "WORKING_FILE", working)

    assert brain_bridge.read_state() == "working"
    state.write_text("speaking\n", encoding="utf-8")
    assert brain_bridge.read_state() == "speaking"


def test_local_publisher_broadcasts_amplitude_without_echo(monkeypatch) -> None:
    monkeypatch.setattr(brain_bridge, "current_durable_job_snapshot", lambda: None)
    publisher = _Socket(['{"type":"amplitude","value":0.42}'])
    observer = _Socket()
    brain_bridge.clients.clear()
    brain_bridge.clients.add(observer)
    try:
        asyncio.run(brain_bridge.handler(publisher))
    finally:
        brain_bridge.clients.clear()

    assert json.loads(publisher.sent[0]) == {
        "type": "state_change",
        "state": brain_bridge.state,
    }
    assert publisher.sent == [publisher.sent[0]]
    assert [json.loads(message) for message in observer.sent] == [
        {"type": "amplitude", "value": 0.42}
    ]


def test_new_overlay_connection_receives_the_active_durable_snapshot(monkeypatch) -> None:
    snapshot = {
        "item_id": "job-1",
        "state": "working",
        "project": "serena",
        "project_root": "/tmp/serena",
    }
    monkeypatch.setattr(
        brain_bridge,
        "current_durable_job_snapshot",
        lambda: snapshot,
    )
    client = _Socket()
    brain_bridge.clients.clear()
    try:
        asyncio.run(brain_bridge.handler(client))
    finally:
        brain_bridge.clients.clear()

    assert [json.loads(message) for message in client.sent] == [
        {"type": "state_change", "state": brain_bridge.state},
        {"type": "code_snapshot", "snapshot": snapshot},
    ]


def test_oversized_done_snapshot_does_not_hide_the_terminal_event() -> None:
    message = {
        "type": "code_done",
        "summary": "failed truthfully",
        "snapshot": {
            "item_id": "job-1",
            "state": "failed",
            "project": "serena",
            "brief": {"request": "x" * 56_000},
        },
    }

    parsed = brain_bridge.parse_local_event(json.dumps(message).encode())

    assert json.loads(parsed or "null") == {
        "type": "code_done",
        "summary": "failed truthfully",
    }


def test_broadcast_uses_snapshot_when_client_set_changes_during_send() -> None:
    class _MutatingSocket(_Socket):
        async def send(self, message: str) -> None:
            self.sent.append(message)
            brain_bridge.clients.add(_Socket())

    first = _MutatingSocket()
    brain_bridge.clients.clear()
    brain_bridge.clients.add(first)
    try:
        asyncio.run(brain_bridge.broadcast('{"type":"amplitude","value":0.1}'))
    finally:
        brain_bridge.clients.clear()

    assert first.sent == ['{"type":"amplitude","value":0.1}']


def test_fleet_notice_is_spoken_and_durably_acknowledged(monkeypatch) -> None:
    """A terminal Fleet event must become local speech, not only a visual card."""

    from voice.desk import say

    states = []
    spoken = []
    recorded = []

    async def fake_speak_stream(sentences) -> None:
        while True:
            sentence = await sentences.get()
            if sentence is None:
                return
            spoken.append(sentence)

    monkeypatch.setattr(brain_bridge, "read_state", lambda: "idle")
    monkeypatch.setattr(brain_bridge, "_record_fleet_notice", lambda *args, **kwargs: recorded.append((args, kwargs)))
    monkeypatch.setattr(say, "set_state", states.append)
    monkeypatch.setattr(say, "speak_stream", fake_speak_stream)
    notice = {
        "type": "fleet_notice",
        "run_id": "12503b47-ac94-4110-b546-27944b41c3c7",
        "state": "completed",
        "token": "completed:123.000000",
        "text": "Fleet finished successfully.",
    }

    asyncio.run(brain_bridge.speak_fleet_notice(notice))

    assert spoken == ["Fleet finished successfully."]
    assert states == ["speaking", "idle"]
    assert recorded == [
        ((notice, "run.notification.delivered"), {}),
    ]
