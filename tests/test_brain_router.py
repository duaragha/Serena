from __future__ import annotations

from core.brain_router import classify_turn, route_turn


def test_short_local_control_uses_reflex_model() -> None:
    decision = route_turn(
        {"protocol": "voice", "text": "volume down"},
        conversation_model="sonnet",
        voice_model="sonnet",
        reflex_model="haiku",
    )

    assert decision.route_class == "reflex"
    assert decision.model == "haiku"
    assert "local control" in decision.reason


def test_complex_voice_routes_keep_the_strong_model() -> None:
    for text, expected in (
        ("research the latest turn detection work", "research"),
        ("implement the fix in the serena repo", "coding"),
        ("review the implementation and check the diff", "review"),
    ):
        decision = route_turn(
            {"protocol": "voice", "text": text},
            conversation_model="sonnet",
            voice_model="haiku",
            reflex_model="haiku",
        )
        assert decision.route_class == expected
        assert decision.model == "sonnet"


def test_ordinary_voice_stays_on_the_conversation_model() -> None:
    assert classify_turn(
        {
            "protocol": "voice",
            "text": "i've been thinking about why this project matters to me",
        }
    )[0] == "conversation"


def test_non_voice_surface_does_not_get_fast_voice_model() -> None:
    decision = route_turn(
        {"protocol": "frontdoor", "text": "hello"},
        conversation_model="sonnet",
        voice_model="haiku",
        reflex_model="haiku",
    )
    assert decision.model == "sonnet"


def test_how_are_you_uses_haiku_without_weakening_work_turns() -> None:
    """The 2026-08-12 trace found casual voice still paying Terra latency."""

    capacity = {
        "codex": {"usable": True, "reason": "available"},
        "claude": {"usable": True, "reason": "available"},
    }

    casual = route_turn(
        {"protocol": "voice", "text": "how are you"},
        capacity=capacity,
    )
    assert (casual.activity, casual.model, casual.runtime_model, casual.effort) == (
        "voice_chat",
        "claude-haiku-4-5",
        "haiku",
        "high",
    )
    assert casual.lane == "fast"

    expected = {
        "research the latest turn detection work": (
            "research",
            "gpt-5.6-terra",
            "high",
        ),
        "implement the fix in the serena repo": ("coding", "gpt-5.6-sol", "high"),
        "review the implementation and check the diff": (
            "review",
            "gpt-5.6-sol",
            "high",
        ),
        "design the architecture for this system": ("planning", "gpt-5.6-sol", "high"),
        "make a polished proposal document": ("documents", "gpt-5.6-terra", "high"),
        "start Serena Fleet for this change": ("fleet", "gpt-5.6-sol", "high"),
    }
    for text, wanted in expected.items():
        decision = route_turn({"protocol": "voice", "text": text}, capacity=capacity)
        assert (decision.activity, decision.model, decision.effort) == wanted


def test_substantive_and_explicit_conversation_stay_on_the_strong_model() -> None:
    """The fast lane once weakened every unclassified spoken conversation."""

    capacity = {
        "codex": {"usable": True, "reason": "available"},
        "claude": {"usable": True, "reason": "available"},
    }

    for payload in (
        {
            "protocol": "voice",
            "text": "i've been thinking about why this project matters to me",
        },
        {
            "protocol": "voice",
            "route_class": "conversation",
            "text": "how are you",
        },
    ):
        decision = route_turn(payload, capacity=capacity)
        assert (decision.activity, decision.lane, decision.model, decision.effort) == (
            "chat",
            "casual",
            "gpt-5.6-terra",
            "high",
        )
        assert decision.fallback_reason == ""


def test_fast_voice_model_unavailability_restores_the_previous_route() -> None:
    """A missing Haiku entitlement must not cost Raghav the spoken turn."""

    decision = route_turn(
        {"protocol": "voice", "text": "how are you"},
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": True, "reason": "available"},
            "models": {
                "claude-haiku-4-5": {
                    "usable": False,
                    "reason": "model is not available on this subscription",
                }
            },
        },
    )

    assert (decision.provider, decision.model, decision.runtime_model) == (
        "codex",
        "gpt-5.6-terra",
        "gpt-5.6-terra",
    )
    assert "not available on this subscription" in decision.fallback_reason

    runtime_fallback = route_turn(
        {
            "protocol": "voice",
            "text": "how are you",
            "_fast_model_available": False,
        },
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": True, "reason": "available"},
        },
    )
    assert (
        runtime_fallback.lane,
        runtime_fallback.model,
        runtime_fallback.effort,
    ) == ("casual", "gpt-5.6-terra", "high")
    assert "restored the standard chat lane" in runtime_fallback.fallback_reason


def test_non_voice_chat_keeps_the_previous_casual_route() -> None:
    """The latency lane is for speech, not every surface that says hello."""

    decision = route_turn(
        {"protocol": "frontdoor", "text": "tell me something funny"},
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": True, "reason": "available"},
        },
    )

    assert (decision.activity, decision.lane, decision.model, decision.effort) == (
        "chat",
        "casual",
        "gpt-5.6-terra",
        "high",
    )
