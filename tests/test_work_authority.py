"""Serena decides when to start coding work; the broker keeps her honest.

The old design let a regex decide before she ever saw the words, so ordinary
asks silently never became jobs. These tests pin the new contract: her
judgement is the authority, and the broker only proves the request is
grounded in a real spoken turn.
"""

from __future__ import annotations

import pytest

from core.work_authority import WorkAuthorityResult, authority_denial, start_coding_work


class _Inbox:
    def __init__(self, *, lease: bool = True) -> None:
        self.lease = lease
        self.enqueued: list[tuple[str, str, str]] = []

    def resident_lease_active(self) -> bool:
        return self.lease

    def enqueue(self, request: str, *, call_id: str, turn_id: str):
        self.enqueued.append((request, call_id, turn_id))
        return type("Item", (), {"item_id": f"item-{len(self.enqueued)}"})()


def _turn(text: str, protocol: str = "voice") -> dict:
    return {
        "text": text,
        "protocol": protocol,
        "call_id": "desk-1",
        "turn_id": "desk-1:3",
    }


@pytest.mark.parametrize(
    "spoken",
    [
        "can you fix the phev tracker",
        "hey serena the phev tracker deep link is broken, sort it out",
        "i need the trash purge bug looked at",
        "keep going on the voice work",
        "the macros screen is a mess, clean it up when you get a chance",
        "actually go ahead and start on konpeki",
    ],
)
def test_ordinary_asks_are_hers_to_start(spoken: str, tmp_path) -> None:
    """No verb-order rules. If he asked for work, she may start it."""
    inbox = _Inbox()
    result = start_coding_work(
        "fix the phev tracker deep link in personal_projects/full_tracker",
        origin=_turn(spoken),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert result.allowed, result.reason
    assert result.item_id
    assert len(inbox.enqueued) == 1


@pytest.mark.parametrize(
    "spoken,fragment",
    [
        ("what's the status of the phev work?", "question"),
        ("why is the wake word so twitchy?", "question"),
        ("don't touch the phev tracker", "withdrew"),
        ("actually never mind, leave it", "withdrew"),
        ("how are you doing today?", "question"),
    ],
)
def test_broker_refuses_when_he_did_not_ask_for_work(
    spoken: str, fragment: str, tmp_path
) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "fix the phev tracker", origin=_turn(spoken), inbox=inbox, audit_path=tmp_path / "audit.jsonl"
    )
    assert not result.allowed
    assert fragment in result.reason
    assert inbox.enqueued == []


def test_she_cannot_invent_a_job_with_no_spoken_turn(tmp_path) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "refactor everything", origin={}, inbox=inbox, audit_path=tmp_path / "audit.jsonl"
    )
    assert not result.allowed
    assert "no originating spoken turn" in result.reason
    assert inbox.enqueued == []


def test_non_spoken_surfaces_cannot_start_work(tmp_path) -> None:
    """Front-door and plain turns keep the existing pane protocol."""
    inbox = _Inbox()
    result = start_coding_work(
        "fix the deep link",
        origin=_turn("fix the deep link", protocol="frontdoor"),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert "live spoken turn" in result.reason


def test_refusal_when_the_private_worker_is_down(tmp_path) -> None:
    inbox = _Inbox(lease=False)
    result = start_coding_work(
        "fix the deep link",
        origin=_turn("fix the deep link"),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert "coding runtime" in result.reason


def test_turn_identity_is_the_idempotency_key(tmp_path) -> None:
    """The legacy spoken fast path and this tool must not double-queue."""
    inbox = _Inbox()
    origin = _turn("fix the phev tracker")
    start_coding_work("fix it", origin=origin, inbox=inbox, audit_path=tmp_path / "audit.jsonl")
    assert inbox.enqueued[0][1:] == ("desk-1", "desk-1:3")


def test_empty_request_is_refused() -> None:
    assert authority_denial("", _turn("fix the phev tracker")) is not None


def test_audit_records_refusals_too(tmp_path) -> None:
    audit = tmp_path / "work-authority.jsonl"
    start_coding_work(
        "fix it",
        origin=_turn("what's the status?"),
        inbox=_Inbox(),
        audit_path=audit,
    )
    body = audit.read_text(encoding="utf-8").strip()
    assert body
    assert '"allowed":false' in body.replace(" ", "")
    # Raw speech is never persisted, only its digest.
    assert "what's the status" not in body


def test_result_shape_is_stable() -> None:
    result = WorkAuthorityResult(True, "queued", "fix it", "abc")
    assert (result.allowed, result.request, result.item_id) == (True, "fix it", "abc")
