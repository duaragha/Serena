"""The orb's Scribe sessions: one per utterance, opened when he starts talking.

Measured against the live service on 2026-09-19: a realtime session left idle
for ten seconds is dead. It does not raise -- feed() drops into it and commit()
returns "" -- so the failure reaches the surface as "she did not hear me" with
nothing in any log.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "voice" / "orb"))

import bridge  # noqa: E402


class FakeSession:
    def __init__(self) -> None:
        self.failed = False
        self.partial = ""
        self.committed = ""
        self.fed: list[bytes] = []
        self.closed = False
        self.commits = 0

    def feed(self, pcm: bytes) -> None:
        if self.failed:
            return
        self.fed.append(pcm)

    def commit(self, timeout: float) -> str:
        self.commits += 1
        return "" if self.failed else "he said a thing"

    def close(self) -> None:
        self.closed = True


def _sessions():
    made: list[FakeSession] = []

    def factory() -> FakeSession:
        made.append(FakeSession())
        return made[-1]

    return bridge.UtteranceSessions(factory), made


def test_nothing_is_opened_until_he_speaks():
    """The bug: a session opened early spends her whole reply going stale."""

    sessions, made = _sessions()
    assert made == []
    assert sessions.current is None


def test_one_session_serves_one_utterance():
    sessions, made = _sessions()
    first = sessions.for_audio()
    sessions.for_audio()
    assert len(made) == 1, "a single utterance must not open a second socket"

    assert sessions.commit(1.0) == "he said a thing"
    assert first.closed is True
    assert sessions.current is None

    # His next utterance opens its own, and only when it arrives.
    assert len(made) == 1
    second = sessions.for_audio()
    assert len(made) == 2
    assert second is not first


def test_a_dead_session_is_replaced_rather_than_fed():
    """This is the whole failure: feed() into a failed session says nothing."""

    sessions, made = _sessions()
    first = sessions.for_audio()
    first.failed = True

    second = sessions.for_audio()
    assert second is not first
    assert first.closed is True
    assert len(made) == 2

    second.feed(b"\x01\x02")
    assert second.fed == [b"\x01\x02"]


def test_committing_nothing_is_not_an_error():
    """He can stop talking without ever having opened one."""

    sessions, _ = _sessions()
    assert sessions.commit(1.0) == ""


def test_closing_releases_whatever_is_open():
    sessions, made = _sessions()
    live = sessions.for_audio()
    sessions.close()
    assert live.closed is True
    assert sessions.current is None
