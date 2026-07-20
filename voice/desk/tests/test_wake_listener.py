from __future__ import annotations

import queue
import threading

from voice.call.wakeword import WakeGate
from voice.desk.wake_listener import (
    PhraseVerification,
    WakeOnlyListener,
    normalize_wake_phrase,
)


class _Scorer:
    def __init__(self, scores: list[float]) -> None:
        self.scores = iter(scores)

    def score_frame(self, _frame) -> float:
        return next(self.scores)

    def reset(self) -> None:
        self.resets = getattr(self, "resets", 0) + 1


class _Verifier:
    def __init__(self, results: list[PhraseVerification]) -> None:
        self.results = iter(results)
        self.audio: list[bytes] = []

    def verify(self, pcm16: bytes) -> PhraseVerification:
        self.audio.append(pcm16)
        return next(self.results)


class _Microphone:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames: queue.Queue[bytes] = queue.Queue()
        for frame in frames:
            self.frames.put(frame)
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


def test_background_audio_only_scores_local_wake_frames() -> None:
    stop = threading.Event()
    microphone = _Microphone([b"\0" * 2_560, b"\0" * 2_560])
    launches = 0

    def launch() -> None:
        nonlocal launches
        launches += 1

    listener = WakeOnlyListener(
        _Scorer([0.1, 0.2]),
        WakeGate(0.5, patience_frames=1, cooldown_seconds=0),
        microphone,
        launch,
        phrase_verifier=_Verifier([]),
    )

    original_get = microphone.frames.get

    def stop_after_scores(*, timeout: float):
        if microphone.frames.empty():
            stop.set()
            raise queue.Empty
        return original_get(timeout=timeout)

    microphone.frames.get = stop_after_scores
    assert listener.run(stop) is False
    assert launches == 0
    assert microphone.started is True
    assert microphone.closed is True


def test_valid_wake_launches_full_app_once_and_releases_microphone() -> None:
    microphone = _Microphone([b"\0" * 2_560, b"\0" * 2_560])
    launches = 0

    def launch() -> None:
        nonlocal launches
        launches += 1

    listener = WakeOnlyListener(
        _Scorer([0.7, 0.8]),
        WakeGate(0.5, patience_frames=2, cooldown_seconds=0),
        microphone,
        launch,
        phrase_verifier=_Verifier([PhraseVerification(True, "Hey, Serena.")]),
        phrase_post_roll_frames=0,
    )

    assert listener.run() is True
    assert launches == 1
    assert microphone.closed is True


def test_false_model_candidate_stays_asleep_without_exact_phrase() -> None:
    stop = threading.Event()
    microphone = _Microphone([b"\0" * 2_560, b"\0" * 2_560])
    scorer = _Scorer([0.99, 0.1])
    verifier = _Verifier([PhraseVerification(False, "open the window")])
    launches = 0

    def launch() -> None:
        nonlocal launches
        launches += 1

    listener = WakeOnlyListener(
        scorer,
        WakeGate(0.5, patience_frames=1, cooldown_seconds=0),
        microphone,
        launch,
        phrase_verifier=verifier,
        phrase_post_roll_frames=0,
    )
    original_get = microphone.frames.get

    def stop_when_empty(*, timeout: float):
        if microphone.frames.empty():
            stop.set()
            raise queue.Empty
        return original_get(timeout=timeout)

    microphone.frames.get = stop_when_empty

    assert listener.run(stop) is False
    assert launches == 0
    assert scorer.resets == 1
    assert len(verifier.audio) == 1


def test_wake_phrase_normalization_is_exact_after_punctuation() -> None:
    assert normalize_wake_phrase("  Hey, SERENA! ") == "hey serena"
    assert normalize_wake_phrase("hey serena open coding") != "hey serena"
