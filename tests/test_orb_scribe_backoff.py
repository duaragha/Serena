"""A refused Scribe connection must not be retried sixteen times a second."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "voice" / "orb"))

import bridge  # noqa: E402


class _Session:
    failed = False

    def close(self):
        pass


def test_a_refused_connection_backs_off_instead_of_retrying_every_frame():
    now = {"t": 0.0}
    attempts = []

    def refuse():
        attempts.append(now["t"])
        return None

    sessions = bridge.UtteranceSessions(refuse, clock=lambda: now["t"])
    for frame in range(40):            # 40 frames of him talking, 64ms apart
        now["t"] = frame * 0.064
        assert sessions.for_audio() is None
    assert len(attempts) == 2, attempts   # t=0, then once after the 1s backoff
    assert sessions.failing


def test_it_recovers_the_moment_a_connection_succeeds():
    now = {"t": 0.0}
    results = [None, _Session()]
    sessions = bridge.UtteranceSessions(lambda: results.pop(0), clock=lambda: now["t"])
    assert sessions.for_audio() is None
    now["t"] = 1.5
    assert isinstance(sessions.for_audio(), _Session)
    assert not sessions.failing


def test_a_follow_up_tells_her_it_may_not_be_for_her(monkeypatch):
    """The first summoned orb answered his phone call out loud."""

    import asyncio

    seen = {}

    class Brain:
        async def stream_turn(self, text, **kw):
            seen["text"] = text
            if False:
                yield None

    monkeypatch.setattr(bridge, "BrainClient", Brain)
    monkeypatch.setattr(bridge, "publish_state", lambda *_: None)
    asyncio.run(bridge._answer(lambda e: None, "and then he said", 2, follow_up=True))
    assert seen["text"].startswith(bridge.FOLLOW_UP_NOTE)
    asyncio.run(bridge._answer(lambda e: None, "what time is it", 1))
    assert seen["text"] == "what time is it"



def _fake_brain(monkeypatch, pieces):
    class Event:
        def __init__(self, delta):
            self.type, self.delta, self.say = "delta", delta, ""

    class Brain:
        async def stream_turn(self, text, **kw):
            for piece in pieces:
                yield Event(piece)

    class Backend:
        async def stream(self, text, **kw):
            class Chunk:
                pcm, sample_rate = b"\x00\x01" * 10, 24000
            yield Chunk()

    monkeypatch.setattr(bridge, "BrainClient", Brain)
    monkeypatch.setattr(bridge, "tts_backend", lambda: Backend())
    monkeypatch.setattr(bridge, "publish_state", lambda *_: None)


def test_her_not_for_me_is_never_spoken_or_shown(monkeypatch):
    import asyncio

    _fake_brain(monkeypatch, ["*(no ", "response — not ", "directed at me)*"])
    sent = []
    asyncio.run(bridge._answer(sent.append, "and then he said", 2, follow_up=True))
    assert [e["type"] for e in sent] == ["speech_end"]


def test_a_real_follow_up_is_still_answered(monkeypatch):
    import asyncio

    _fake_brain(monkeypatch, ["Yeah, rain ", "again tomorrow, bring a jacket."])
    sent = []
    asyncio.run(bridge._answer(sent.append, "is it gonna rain tomorrow too", 2, follow_up=True))
    assert "audio" in [e["type"] for e in sent]
    assert sent[-2]["type"] in ("reply", "audio")
