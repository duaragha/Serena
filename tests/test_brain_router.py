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
