from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from voice.call.greeting import CallGreetingState


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_greeting_is_once_after_playback_and_retryable_before_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "call-state.json"
    clock = Clock(datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc))
    state = CallGreetingState(path, clock=clock)

    first = state.claim("call-one")
    assert first is not None
    assert first.first_call_today is True
    assert first.call_number_today == 1
    assert state.claim("call-one") is None

    state.release("call-one")
    restarted = CallGreetingState(path, clock=clock)
    retried = restarted.claim("call-one")
    assert retried == first
    assert restarted.complete("call-one") is True
    assert restarted.claim("call-one") is None

    second = restarted.claim("call-two")
    assert second is not None
    assert second.first_call_today is False
    assert second.call_number_today == 2
    assert restarted.complete("call-two") is True

    clock.value = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
    next_day = restarted.claim("call-three")
    assert next_day is not None
    assert next_day.first_call_today is True
    assert next_day.call_number_today == 1
    assert "first-call-today=\"true\"" in next_day.prompt()
    assert "never ask an open-ended question" in next_day.prompt()

    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_corrupt_or_unwritable_state_fails_soft(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json", encoding="utf-8")
    state = CallGreetingState(
        corrupt,
        clock=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )
    assert state.claim("call-corrupt") is not None

    blocked_parent = tmp_path / "plain-file"
    blocked_parent.write_text("blocked", encoding="utf-8")
    blocked = CallGreetingState(
        blocked_parent / "state.json",
        clock=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )
    assert blocked.claim("call-unwritable") is None
    assert blocked.claim("call-unwritable") is None


def test_invalid_counter_is_treated_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "call-state.json"
    path.write_text(
        '{"date":"2026-07-16","calls_today":"not-a-number"}',
        encoding="utf-8",
    )
    state = CallGreetingState(
        path,
        clock=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )

    context = state.claim("safe-counter")

    assert context is not None
    assert context.call_number_today == 1
