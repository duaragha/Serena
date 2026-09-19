"""The one notice Serena starts herself.

A nudge that fires every poll is a nag, and a nudge that never fires leaves his
answer sitting in a queue he cannot see. Both failures are tested here.
"""

from __future__ import annotations

import json

import pytest

from core import scheduler_actions


@pytest.fixture
def line(monkeypatch, tmp_path):
    """An available phone line, a captured notice, and a private state file."""

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key: bool(sent.append((text, key))) or True)
    monkeypatch.setattr(scheduler_actions, "_nudge_state_path",
                        lambda: tmp_path / "phone-nudge.json")
    from core import phone_line

    monkeypatch.setattr(phone_line, "available", lambda: True)
    return sent


def _tasks(monkeypatch, rows):
    from memory import store

    monkeypatch.setattr(store, "tasks_in_state",
                        lambda *states: [row for row in rows if row["state"] in states])


def _task(task_id, state, content="fix the locket sync", **extra):
    row = {"id": task_id, "state": state, "content": content, "asked_at": "",
           "snooze_until": ""}
    row.update(extra)
    return row


def test_a_blocked_task_makes_her_speak_first(line, monkeypatch):
    _tasks(monkeypatch, [_task(9, "blocked")])
    outcome = scheduler_actions.nudge_phone_line({})
    assert outcome.ok and outcome.output == {"task_id": 9, "state": "blocked", "sent": True}
    text, key = line[0]
    assert "#9 is still stuck" in text and "retry #9" in text
    assert key == "nudge:9:blocked"


def test_she_says_it_once_a_shift_not_once_a_poll(line, monkeypatch):
    _tasks(monkeypatch, [_task(9, "blocked")])
    assert scheduler_actions.nudge_phone_line({}).ok
    for _ in range(3):
        outcome = scheduler_actions.nudge_phone_line({})
        assert outcome.ok and "spoke recently" in outcome.detail
    assert len(line) == 1


def test_an_unanswered_question_is_chased_only_after_it_has_aged(line, monkeypatch):
    import time

    now = time.time()
    fresh = _task(11, "needs_triage", asked_at=str(int(now - 60)))
    _tasks(monkeypatch, [fresh])
    assert scheduler_actions.nudge_phone_line({}).detail == "nothing is waiting on him"
    fresh["asked_at"] = str(int(now - scheduler_actions.NUDGE_ASKED_AGE_SECONDS - 60))
    outcome = scheduler_actions.nudge_phone_line({})
    assert outcome.output["task_id"] == 11
    assert "reply \"#11 <details>\"" in line[0][0]


def test_a_brief_she_never_asked_about_is_not_hers_to_chase(line, monkeypatch):
    _tasks(monkeypatch, [_task(12, "needs_triage")])
    assert scheduler_actions.nudge_phone_line({}).detail == "nothing is waiting on him"
    assert line == []


def test_a_snoozed_task_stays_quiet(line, monkeypatch):
    from memory import store

    monkeypatch.setattr(store, "_is_snoozed", lambda task: True)
    _tasks(monkeypatch, [_task(13, "blocked")])
    assert scheduler_actions.nudge_phone_line({}).detail == "nothing is waiting on him"
    assert line == []


def test_a_held_notice_does_not_count_as_spoken(monkeypatch, tmp_path):
    from core import phone_line

    monkeypatch.setattr(phone_line, "available", lambda: True)
    monkeypatch.setattr(scheduler_actions, "_notify_phone", lambda text, key: False)
    state = tmp_path / "phone-nudge.json"
    monkeypatch.setattr(scheduler_actions, "_nudge_state_path", lambda: state)
    _tasks(monkeypatch, [_task(14, "blocked")])
    outcome = scheduler_actions.nudge_phone_line({})
    assert outcome.ok and outcome.output["sent"] is False
    # Quiet hours held it, so the next pass must be free to try again.
    assert not state.exists()


def test_the_action_takes_no_payload_and_needs_a_line(monkeypatch, tmp_path):
    from core import phone_line

    monkeypatch.setattr(phone_line, "available", lambda: True)
    assert scheduler_actions.nudge_phone_line({"task_id": 9}).ok is False
    monkeypatch.setattr(phone_line, "available", lambda: False)
    assert scheduler_actions.nudge_phone_line({}).detail == (
        "phone line is not configured on this machine")


def test_a_corrupt_state_file_does_not_silence_her(line, monkeypatch, tmp_path):
    (tmp_path / "phone-nudge.json").write_text("{not json", encoding="utf-8")
    _tasks(monkeypatch, [_task(15, "blocked")])
    assert scheduler_actions.nudge_phone_line({}).output["task_id"] == 15
    assert json.loads((tmp_path / "phone-nudge.json").read_text(encoding="utf-8"))["task_id"] == 15
