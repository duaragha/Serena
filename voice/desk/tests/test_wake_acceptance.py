from __future__ import annotations

from voice.desk.wake_acceptance import (
    WakeAcceptancePolicy,
    build_report,
    parse_journal_messages,
)
from voice.desk.wake_listener import WAKE_EVENT_PREFIX


def _event(name: str, wall_ns: int, **values):
    return {
        "event": name,
        "wall_ns": wall_ns,
        "listener_id": "listener",
        "phrase_model_sha256": "a" * 64,
        **values,
    }


def test_journal_parser_accepts_only_structured_transcript_free_events() -> None:
    lines = [
        "ordinary wake log",
        WAKE_EVENT_PREFIX
        + '{"event":"wake.phrase_candidate","accepted":false,'
        '"recognized_words":3,"wall_ns":1}',
        WAKE_EVENT_PREFIX + "not-json",
    ]
    events = parse_journal_messages(lines)
    assert len(events) == 1
    assert events[0]["recognized_words"] == 3
    assert "transcript" not in events[0]


def test_two_stage_acceptance_counts_liveness_attempts_and_false_accepts() -> None:
    second = 1_000_000_000
    events = [
        _event("wake.listener_started", 0 * second),
        _event("wake.heartbeat", 60 * second),
        _event("wake.heartbeat", 120 * second),
        _event("wake.phrase_candidate", 130 * second, accepted=True),
        _event("wake.accepted", 131 * second),
        _event("wake.listener_stopped", 132 * second),
    ]
    attempts = [
        {
            "attempt_id": "wanted",
            "start_wall_ns": 125 * second,
            "end_wall_ns": 135 * second,
        }
    ]
    policy = WakeAcceptancePolicy(
        minimum_background_hours=0.02,
        minimum_active_days=1,
        minimum_attempts=1,
        maximum_miss_rate=0,
        maximum_false_accepts=0,
    )
    report = build_report(
        events,
        attempts,
        current_phrase_model_sha256="a" * 64,
        policy=policy,
    )
    assert report["coverage"]["detected_attempts"] == 1
    assert report["coverage"]["false_accepts"] == 0
    assert report["acceptance_claim"] is True


def test_unmarked_final_accept_prevents_acceptance() -> None:
    second = 1_000_000_000
    report = build_report(
        [
            _event("wake.listener_started", 1 * second),
            _event("wake.heartbeat", 61 * second),
            _event("wake.accepted", 62 * second),
        ],
        [],
        current_phrase_model_sha256="a" * 64,
        policy=WakeAcceptancePolicy(
            minimum_background_hours=0,
            minimum_active_days=0,
            minimum_attempts=0,
            maximum_miss_rate=1,
            maximum_false_accepts=0,
        ),
    )
    assert report["coverage"]["false_accepts"] == 1
    assert report["acceptance_claim"] is False
