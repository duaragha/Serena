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
        "claude-opus-5-5",
        "claude-opus-5-5",
        "high",
    )
    assert casual.lane == "fast"

    # Spoken work turns keep their work lane and their strength, but take the
    # streaming model in it: he is listening, and a codex model cannot speak
    # until the whole answer exists.
    expected = {
        "research the latest turn detection work": (
            "research",
            "claude-opus-5-5",
            "high",
        ),
        "implement the fix in the serena repo": ("coding", "claude-opus-5-5", "high"),
        "review the implementation and check the diff": (
            "review",
            "claude-opus-5-5",
            "high",
        ),
        "design the architecture for this system": (
            "planning",
            "claude-opus-5-5",
            "high",
        ),
        "make a polished proposal document": (
            "documents",
            "claude-opus-5-5",
            "high",
        ),
        "start Serena Fleet for this change": ("fleet", "claude-opus-5-5", "high"),
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
            "claude-opus-5-5",
            "high",
        )
        assert decision.fallback_reason == ""


def test_fast_voice_model_unavailability_restores_the_previous_route() -> None:
    """A missing Opus 5.5 entitlement must not cost Raghav the spoken turn."""

    decision = route_turn(
        {"protocol": "voice", "text": "how are you"},
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": True, "reason": "available"},
            "models": {
                "claude-opus-5-5": {
                    "usable": False,
                    "reason": "model is not available on this subscription",
                }
            },
        },
    )

    # Haiku is next in the fast lane and streams, so the spoken turn takes it.
    assert (decision.provider, decision.model, decision.runtime_model) == (
        "claude",
        "claude-haiku-4-5",
        "haiku",
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
    ) == ("casual", "claude-opus-5-5", "high")
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
        "gpt-6-astra",
        "high",
    )


def test_a_spoken_work_question_takes_a_model_that_streams() -> None:
    """She answered a spoken Fleet question on a non-streaming model and he
    heard the preamble, then silence, then the whole reply at once."""

    capacity = {
        "codex": {"usable": True, "reason": "available"},
        "claude": {"usable": True, "reason": "available"},
    }
    question = "What fleet is currently running?"

    spoken = route_turn({"protocol": "voice", "text": question}, capacity=capacity)
    typed = route_turn({"protocol": "frontdoor", "text": question}, capacity=capacity)

    assert spoken.provider == "claude"
    assert spoken.model == "claude-opus-5-5"
    # Typed surfaces keep the stronger model: nobody is waiting on a sentence.
    assert typed.provider == "codex"
    assert typed.model == "gpt-6-astra"
    assert spoken.lane == typed.lane == "complex"


def test_a_non_streaming_model_still_answers_when_it_is_all_that_is_left() -> None:
    """Preferring streaming must never turn into refusing to answer."""

    decision = route_turn(
        {"protocol": "voice", "text": "What fleet is currently running?"},
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": False, "reason": "Claude usage exhausted"},
            "muse": {"usable": False, "reason": "muse is offline"},
        },
    )

    assert decision.provider == "codex"
    assert "Claude usage exhausted" in decision.fallback_reason



def test_a_spoken_casual_turn_still_streams_on_claude() -> None:
    """Typed chat moved to GPT-6 Astra; a voice turn must not go silent for it."""

    decision = route_turn(
        {"protocol": "voice", "text": "can you walk me through what the fleet did today"},
        capacity={
            "codex": {"usable": True, "reason": "available"},
            "claude": {"usable": True, "reason": "available"},
        },
    )

    assert decision.provider == "claude"
