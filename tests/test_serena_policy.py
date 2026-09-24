from __future__ import annotations

import json

import pytest

from core.serena_policy import (
    SerenaPolicyError,
    classify_risk,
    fleet_policy_reference,
    load_policy,
    model_options,
    resolve_policy,
    validate_frozen_decision,
    validate_policy,
)


def _capacity(*, codex: bool = True, claude: bool = True) -> dict:
    return {
        "codex": {
            "usable": codex,
            "reason": "available" if codex else "Codex usage exhausted",
        },
        "claude": {
            "usable": claude,
            "reason": "available" if claude else "Claude usage exhausted",
        },
    }


def test_checked_in_policy_is_valid_and_fleet_stays_separate() -> None:
    policy = load_policy()

    assert policy["schema_version"] == 1
    assert set(policy["profiles"]) >= {"brain", "coding", "research", "documents", "fleet"}
    assert fleet_policy_reference(policy) == "core.fleet_policy.PHASE_MODEL_POLICY"
    with pytest.raises(SerenaPolicyError, match="belongs to core.fleet_policy"):
        resolve_policy("fleet", policy=policy)


def test_invalid_model_reference_fails_validation() -> None:
    policy = json.loads(json.dumps(load_policy()))
    policy["profiles"]["brain"]["lanes"]["casual"]["execute"][0]["model"] = "missing"

    with pytest.raises(SerenaPolicyError, match="unknown model"):
        validate_policy(policy)


def test_brain_casual_streams_first_and_capacity_falls_back_truthfully() -> None:
    typed = resolve_policy("brain", activity="chat", capacity=_capacity())
    spoken = resolve_policy(
        "brain", activity="chat", capacity=_capacity(), require_streaming=True
    )
    fallback = resolve_policy(
        "brain", activity="chat", capacity=_capacity(codex=False)
    )

    # Raghav moved typed chat to GPT-6 Astra on 2026-09-24. A spoken turn
    # still needs a model that streams: Astra emits no deltas, so on a voice
    # turn it waits behind the streaming model rather than leading.
    assert (typed.provider, typed.model, typed.effort) == ("codex", "gpt-6-astra", "high")
    assert (spoken.provider, spoken.model, spoken.effort) == (
        "claude",
        "claude-sonnet-5",
        "high",
    )
    assert (fallback.provider, fallback.model) == ("claude", "claude-sonnet-5")
    assert fallback.fallback_reason


def test_brain_voice_chat_prefers_haiku_but_respects_model_health() -> None:
    """Provider health alone hid an unavailable fast model on 2026-08-12."""

    automatic = resolve_policy(
        "brain",
        activity="voice_chat",
        capacity=_capacity(),
    )
    capacity = _capacity()
    capacity["models"] = {
        "claude-haiku-4-5": {
            "usable": False,
            "reason": "Haiku is unavailable",
        }
    }
    fallback = resolve_policy(
        "brain",
        activity="voice_chat",
        capacity=capacity,
    )

    assert (automatic.lane, automatic.model, automatic.effort) == (
        "fast",
        "claude-haiku-4-5",
        "high",
    )
    assert (fallback.model, fallback.effort) == ("gpt-5.6-terra", "high")
    assert "Haiku is unavailable" in fallback.fallback_reason


def test_coding_lanes_apply_routine_normal_and_hard_floors() -> None:
    routine = resolve_policy(
        "coding",
        activity="implement",
        complexity="routine",
        role="implement",
        capacity=_capacity(),
    )
    normal = resolve_policy(
        "coding",
        activity="implement",
        complexity="normal",
        role="implement",
        capacity=_capacity(),
    )
    hard = resolve_policy(
        "coding",
        activity="implement",
        complexity="hard",
        role="implement",
        capacity=_capacity(),
    )

    assert (routine.lane, routine.model, routine.effort) == (
        "routine",
        "gpt-5.6-terra",
        "high",
    )
    assert (normal.lane, normal.model, normal.effort) == (
        "normal",
        "gpt-6-astra",
        "medium",
    )
    assert (hard.lane, hard.model, hard.effort) == (
        "hard",
        "gpt-6-astra",
        "high",
    )


def test_high_risk_overrides_a_weak_manual_model_but_keeps_tool_authority_external() -> None:
    risk, reason = classify_risk("restart the systemd voice brain service")
    decision = resolve_policy(
        "brain",
        activity="laptop",
        risk=risk,
        manual_override="terra",
        capacity=_capacity(),
    )

    assert risk == "high"
    assert "systemd" in reason
    assert decision.model == "gpt-6-astra"
    assert "below the high safety floor" in decision.fallback_reason
    assert "permission" not in decision.as_dict()
    assert "tools" not in decision.as_dict()


def test_coding_options_come_from_policy_and_frozen_decisions_revalidate() -> None:
    options = model_options("coding")
    values = [item["value"] for item in options]
    decision = resolve_policy(
        "coding",
        activity="review",
        complexity="normal",
        role="review",
        capacity=_capacity(),
    )

    assert values == [
        "auto",
        "gpt-5.6-terra",
        "gpt-6-astra",
        "claude-sonnet-5",
        "claude-opus-5",
        "muse-spark",
    ]
    frozen = validate_frozen_decision(
        decision.as_dict(),
        profile="coding",
        role="review",
    )
    assert frozen["model"] == "gpt-5.6-luna"
