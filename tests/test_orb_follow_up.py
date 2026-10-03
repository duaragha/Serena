"""A summoned orb closes only when she says a follow-up was not for her.

2026-09-23: he paused on "Um..." while telling her his birthday did not feel
happy. The lone hesitation was committed as a turn, she answered SILENT, and
the orb vanished on him mid-sentence.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "voice" / "orb"))

import bridge  # noqa: E402


@pytest.mark.parametrize("text", ["Um...", "um", "Uh, um.", "Hmm", "mm", "..."])
def test_a_lone_hesitation_is_not_a_turn(text):
    assert bridge._is_filler(text)


@pytest.mark.parametrize("text", ["Um, it's my birthday", "okay", "yeah", "no", "Uh huh sure"])
def test_real_words_are_a_turn(text):
    assert not bridge._is_filler(text)


def _answer(monkeypatch, *events, fail=False):
    class Brain:
        async def stream_turn(self, text, **_):
            if fail:
                raise RuntimeError("brain went away")
            for event in events:
                yield event

    class Voice:
        async def stream(self, text, **_):
            yield SimpleNamespace(pcm=b"\0\0", sample_rate=24_000)

    monkeypatch.setattr(bridge, "BrainClient", Brain)
    monkeypatch.setattr(bridge, "tts_backend", lambda: Voice())
    monkeypatch.setattr(bridge, "publish_state", lambda state: None)
    sent = []
    asyncio.run(bridge._answer(sent.append, "Um...", 2, follow_up=True))
    return sent


def test_her_silent_is_flagged_so_the_orb_closes(monkeypatch):
    sent = _answer(monkeypatch, SimpleNamespace(type="delta", delta="SILENT", say=None))
    assert sent[-1] == {"type": "speech_end", "silent": True}
    assert not [event for event in sent if event["type"] in ("reply", "audio")]


def test_a_failed_turn_does_not_read_as_her_saying_not_me(monkeypatch):
    sent = _answer(monkeypatch, fail=True)
    assert sent[0]["type"] == "error"
    assert sent[-1] == {"type": "speech_end", "silent": False}


def test_a_real_answer_is_spoken_and_not_flagged(monkeypatch):
    sent = _answer(monkeypatch, SimpleNamespace(
        type="delta", delta="take your time, i'm listening.", say=None))
    assert any(event["type"] == "audio" for event in sent)
    assert sent[-1] == {"type": "speech_end", "silent": False}


def test_the_mic_socket_holds_a_hesitation_and_sends_it_with_his_next_words(monkeypatch):
    """Drives the real /ws/mic handler: the first cut of the hold crashed it
    on his first sentence and the orb showed "bridge closed" mid-speech."""

    import json
    import threading

    heard = iter(["Um...", "it's my birthday"])

    class Session:
        partial = committed = ""

        def feed(self, pcm):
            pass

    class Sessions:
        failing = False
        current = None

        def __init__(self, factory):
            pass

        def for_audio(self):
            self.current = Session()
            return self.current

        def commit(self, timeout):
            self.current = None
            return next(heard)

        def close(self):
            pass

    class Socket:
        def __init__(self, messages):
            self.messages = list(messages)
            self.sent = []

        def receive(self):
            return self.messages.pop(0) if self.messages else None

        def send(self, raw):
            self.sent.append(json.loads(raw))

    answered = []
    done = threading.Event()

    async def answer(send, text, turn, *, follow_up=False, after=""):
        answered.append((text, follow_up))
        done.set()

    monkeypatch.setattr(bridge, "load_elevenlabs_key", lambda: "key")
    monkeypatch.setattr(bridge, "load_keyterms", lambda: [])
    monkeypatch.setattr(bridge, "publish_state", lambda state: None)
    monkeypatch.setattr(bridge, "UtteranceSessions", Sessions)
    monkeypatch.setattr(bridge, "_answer", answer)
    commit = json.dumps({"type": "commit", "follow_up": True})
    ws = Socket([b"\0\0", commit, b"\0\0", commit])

    bridge.app.view_functions["ws_mic"].__wrapped__(ws)

    assert done.wait(2)
    kinds = [event["type"] for event in ws.sent if event["type"] != "transcript"]
    assert kinds == ["ready", "hold", "final", "thinking"]
    assert ws.sent[kinds.index("hold")]["text"] == "Um..."
    assert answered == [("Um... it's my birthday", True)]


def test_the_follow_up_note_carries_what_she_just_said():
    note = bridge._follow_up_note("Not normally -- base game caps you at one enchantment per item.")
    assert "one enchantment per item" in note
    assert note.endswith(bridge.FOLLOW_UP_NOTE)
    assert bridge._follow_up_note("") == bridge.FOLLOW_UP_NOTE
    assert len(bridge._follow_up_note("word " * 400)) < len(bridge.FOLLOW_UP_NOTE) + 400


def test_her_last_answer_reaches_the_next_follow_up(monkeypatch):
    """2026-10-03: "um, do you know which mods are for this?" came straight after
    her enchantment answer and she still judged it not for her."""

    import json
    import threading

    heard = iter(["can you stack two enchantments", "do you know which mods are for this"])

    class Session:
        partial = committed = ""

        def feed(self, pcm):
            pass

    class Sessions:
        failing = False
        current = None

        def __init__(self, factory):
            pass

        def for_audio(self):
            self.current = Session()
            return self.current

        def commit(self, timeout):
            self.current = None
            return next(heard)

        def close(self):
            pass

    first_done = threading.Event()
    second_done = threading.Event()
    calls = []

    class Socket:
        def __init__(self):
            self.messages = [b"\0\0", json.dumps({"type": "commit", "follow_up": False})]
            self.second = [b"\0\0", json.dumps({"type": "commit", "follow_up": True})]
            self.sent = []

        def receive(self):
            if not self.messages and self.second:
                # He asks the follow-up once she has answered the first one.
                assert first_done.wait(2)
                self.messages, self.second = self.second, []
            return self.messages.pop(0) if self.messages else None

        def send(self, raw):
            self.sent.append(json.loads(raw))

    async def answer(send, text, turn, *, follow_up=False, after=""):
        calls.append((text, follow_up, after))
        if len(calls) == 1:
            send({"type": "reply", "text": "not normally, one enchantment per item."})
            first_done.set()
        else:
            second_done.set()

    monkeypatch.setattr(bridge, "load_elevenlabs_key", lambda: "key")
    monkeypatch.setattr(bridge, "load_keyterms", lambda: [])
    monkeypatch.setattr(bridge, "publish_state", lambda state: None)
    monkeypatch.setattr(bridge, "UtteranceSessions", Sessions)
    monkeypatch.setattr(bridge, "_answer", answer)

    bridge.app.view_functions["ws_mic"].__wrapped__(Socket())

    assert second_done.wait(2)
    assert calls[0] == ("can you stack two enchantments", False, "")
    assert calls[1] == ("do you know which mods are for this", True,
                        "not normally, one enchantment per item.")
