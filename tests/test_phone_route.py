"""His text line gets its own model, because it is not a spoken turn.

Every brain turn -- voice, front door and the phone -- was routed by the same
legacy branch to one model:

    daemon constants: MODEL='sonnet' VOICE_MODEL='sonnet' REFLEX_MODEL='sonnet'
    phone turn -> sonnet | provider claude | lane legacy

Voice buys its 500-800ms first token by thinking as little as possible, and
the phone inherited that because they shared a model. The whole lane/policy
system was unreachable from the daemon, and `complexity` in the payload was
ignored, which is what made the bot feel two-dimensional.
"""

from core.brain_router import route_turn

LEGACY = {"conversation_model": "sonnet", "voice_model": "sonnet", "reflex_model": "sonnet"}


def _route(protocol, **kwargs):
    return route_turn({"protocol": protocol, "text": "hows the workout task going"},
                      **LEGACY, **kwargs)


def test_an_unconfigured_phone_route_behaves_exactly_as_before():
    """The default must change nothing, so this can ship dark."""

    decision = _route("phone")
    assert decision.model == "sonnet"
    assert decision.lane == "legacy"


def test_a_configured_phone_route_reaches_the_reasoning_model():
    decision = _route("phone", phone_model="gpt-6-astra", phone_effort="high")

    assert decision.model == "gpt-6-astra"
    assert decision.runtime_model == "gpt-6-astra"
    # The provider has to follow the model, or the daemon serves it on the
    # wrong client and the route quietly does nothing.
    assert decision.provider == "codex"
    assert decision.effort == "high"
    assert decision.lane == "phone"


def test_voice_keeps_its_own_model_when_the_phone_is_configured():
    """Voice latency is the reason the phone needed its own route at all."""

    decision = _route("voice", phone_model="gpt-6-astra", phone_effort="high")

    assert decision.model == "sonnet"
    assert decision.provider == "claude"
    assert decision.lane == "legacy"


def test_the_front_door_is_unaffected():
    decision = _route("frontdoor", phone_model="gpt-6-astra")
    assert decision.model == "sonnet" and decision.lane == "legacy"


def test_effort_defaults_rather_than_arriving_empty():
    decision = _route("phone", phone_model="gpt-6-astra", phone_effort="")
    assert decision.effort == "high"


def test_a_blank_phone_model_is_not_treated_as_configured():
    """An unset env var arrives as an empty string, not None."""

    decision = _route("phone", phone_model="   ")
    assert decision.model == "sonnet" and decision.lane == "legacy"


def test_the_phone_line_asks_for_the_phone_protocol():
    """phone_intent and the daemon have to agree on the name."""

    import json
    from unittest.mock import patch

    from core import phone_intent

    sent = {}

    def _post(url, payload, token):
        sent.update(payload)
        return {"ok": True, "say": "#1 is still blocked."}

    with patch.object(phone_intent, "_endpoint", lambda: ("http://x/turn", "t")), \
         patch.object(phone_intent, "_post", _post):
        assert phone_intent.read("hows it going") == ("say", "#1 is still blocked.")

    assert sent["protocol"] == "phone"
    json.dumps(sent)  # the payload has to survive the wire


def test_health_exposes_whether_the_phone_route_is_configured():
    """Without this there is no way to tell the route is live from outside.

    /health reported model, voice_model and reflex_model, so a phone line
    still answering on the voice model looked identical to one that was not.
    """

    import re
    from pathlib import Path

    source = Path("core/brain_daemon.py").read_text(encoding="utf-8")
    block = source[source.index('"reflex_model": REFLEX_MODEL,'):][:400]
    assert '"phone_model"' in block
    assert '"phone_effort"' in block
    assert '"phone_route_configured"' in block
    # It reports the effective model, so an unconfigured route is not blank.
    assert "PHONE_MODEL or MODEL" in block
