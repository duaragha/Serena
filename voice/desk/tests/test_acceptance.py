from __future__ import annotations

from datetime import datetime, timedelta, timezone

from voice.desk.acceptance import DeskAcceptancePolicy, analyze_desk_acceptance


def _row(event: str, generation: int, at: datetime, **values):
    return {
        "ts": at.isoformat(),
        "event": event,
        "call_id": "desk-test",
        "generation": generation,
        **values,
    }


def _complete_turn(generation: int, start: datetime):
    return [
        _row("endpoint.detected", generation, start),
        _row("stt.done", generation, start + timedelta(milliseconds=100)),
        _row("brain.first_delta", generation, start + timedelta(milliseconds=500)),
        _row(
            "desk.response_first_write",
            generation,
            start + timedelta(milliseconds=900),
        ),
        _row("audio.end", generation, start + timedelta(seconds=2)),
    ]


def test_complete_heard_call_and_wake_report_can_pass() -> None:
    started = datetime(2026, 7, 23, tzinfo=timezone.utc)
    rows = []
    for generation in range(1, 4):
        rows.extend(_complete_turn(generation, started + timedelta(seconds=generation * 5)))
    rows.extend(
        [
            _row("desk.barge_in", 3, started + timedelta(seconds=18)),
            _row("call.end", 3, started + timedelta(seconds=20), clean_hangup=True),
        ]
    )

    report = analyze_desk_acceptance(
        rows,
        wake_report={"acceptance_claim": True},
        heard_clean=True,
    )

    assert report["coverage"]["completed_generations"] == [1, 2, 3]
    assert report["coverage"]["eou_to_first_write_p90_ms"] == 900
    assert report["conversation_acceptance_claim"] is True
    assert report["acceptance_claim"] is True


def test_code_only_and_missing_wake_evidence_cannot_pass() -> None:
    report = analyze_desk_acceptance(
        [],
        wake_report={"acceptance_claim": False},
        heard_clean=False,
        policy=DeskAcceptancePolicy(
            minimum_completed_turns=0,
            require_barge_in=False,
            require_clean_hangup=False,
        ),
    )

    assert report["conversation_acceptance_claim"] is False
    assert report["wake_acceptance_claim"] is False
    assert report["acceptance_claim"] is False
