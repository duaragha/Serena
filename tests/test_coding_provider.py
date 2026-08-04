"""Coding work routes to whoever actually has capacity.

2026-08-04: Codex reported 100% of its rate limit used. Raghav asked Serena to
change the coding workflow so it would stop depending on Codex, and that job
was dispatched to Codex, which could not run it. It sat at "working" with no
log line until he cancelled it by hand. read_fleet_capacity() had the answer
the whole time and nothing asked it.
"""

from __future__ import annotations

import pytest

from core.coding_provider import choose_providers


def _capacity(codex: bool | None, claude: bool | None) -> dict:
    out = {}
    for name, state in (("codex", codex), ("claude", claude)):
        if state is None:
            out[name] = {"provider": name, "status": "unknown",
                         "usable": True, "reason": f"{name} usage is unavailable"}
        else:
            out[name] = {
                "provider": name,
                "status": "available" if state else "unavailable",
                "usable": state,
                "reason": "below limit" if state else f"{name} reported rate_limit_reached",
            }
    return out


def test_both_healthy_keeps_one_model_writing_and_another_reviewing() -> None:
    """A reviewer sharing the author's blind spots is decoration."""
    plan = choose_providers(_capacity(True, True))
    assert plan.usable
    assert plan.implement_provider == "codex"
    assert plan.review_provider == "claude"
    assert not plan.same_provider_reviews_itself


def test_the_live_situation_routes_everything_to_claude() -> None:
    """Codex at 100%, Claude at 43%: exactly what the machine read that day."""
    plan = choose_providers(_capacity(False, True))
    assert plan.usable
    assert plan.implement_provider == "claude"
    assert plan.review_provider == "claude"
    assert "codex is out of capacity" in plan.reason


def test_the_mirror_case_routes_everything_to_codex() -> None:
    plan = choose_providers(_capacity(True, False))
    assert plan.usable
    assert plan.implement_provider == "codex"
    assert plan.review_provider == "codex"


def test_one_provider_reviewing_itself_is_recorded_as_such() -> None:
    """Self review is weaker, so the job has to admit when it happened."""
    plan = choose_providers(_capacity(False, True))
    assert plan.same_provider_reviews_itself
    assert plan.to_dict()["self_review"] is True


def test_nothing_is_dispatched_when_both_are_out() -> None:
    """Better to say so than to hang at 'working' with no log line."""
    plan = choose_providers(_capacity(False, False))
    assert not plan.usable
    assert not plan.implement_provider
    assert "rate_limit_reached" in plan.reason
    assert "codex" in plan.reason and "claude" in plan.reason


@pytest.mark.parametrize("codex,claude", [(None, None), (None, True), (True, None)])
def test_an_unknown_reading_never_benches_a_provider(codex, claude) -> None:
    """fleet_capacity's own rule: only a positive, unexpired exhaustion signal
    counts. Being unsure is not the same as being out."""
    plan = choose_providers(_capacity(codex, claude))
    assert plan.usable
    assert plan.implement_provider and plan.review_provider


def test_a_missing_provider_entry_does_not_crash_the_dispatch() -> None:
    plan = choose_providers({})
    assert plan.usable


def test_the_choice_carries_its_evidence() -> None:
    """He should be able to see why it picked, not just what it picked."""
    plan = choose_providers(_capacity(False, True))
    payload = plan.to_dict()
    assert payload["capacity"]["codex"]["usable"] is False
    assert payload["capacity"]["claude"]["usable"] is True
    assert payload["implement"]["model"] and payload["review"]["model"]


def test_the_preference_can_be_flipped() -> None:
    plan = choose_providers(_capacity(True, True), preferred_implementer="claude")
    assert plan.implement_provider == "claude"
    assert plan.review_provider == "codex"


def test_the_supervisor_asks_capacity_before_spending_a_turn() -> None:
    """The whole failure was a dispatch that never consulted the reading.

    A job is now refused immediately with the provider's own reason instead of
    sitting at "working" forever, which is the difference between a wait he
    can understand and a hang he has to debug.
    """
    from pathlib import Path

    source = Path("core/voice_work_supervisor.py").read_text(encoding="utf-8")
    body = source.split("def _run_item", 1)[1]
    gate = body.split("validate_repository_root", 1)[0]
    assert "self.provider_chooser()" in gate
    assert "assignment.usable" in gate
    assert "provider_assignment" in gate
    # And the choice is refused loudly, not silently downgraded.
    assert "raise RuntimeError(assignment.reason)" in gate
