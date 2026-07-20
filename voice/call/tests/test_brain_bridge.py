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
            "event": {"kind": "bash", "summary": "pytest -q", "detail": "passed"},
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
    ],
)
def test_local_code_events_reject_invalid_payloads(message: dict) -> None:
    assert brain_bridge.parse_local_event(json.dumps(message).encode()) is None


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


def test_local_publisher_broadcasts_amplitude_without_echo() -> None:
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
