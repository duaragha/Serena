from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from voice.call.wakeword import WakeGate
from voice.desk.wake_listener import (
    PhraseVerification,
    WakeOnlyListener,
    normalize_wake_phrase,
    wake_phrase_matches,
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
        self.failure_reason: str | None = None

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


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


@pytest.mark.parametrize(
    "transcript",
    [
        "Hey, Serena!",
        "hey serina",
        "hay sirena",
        "hi sarina",
        "okay, hey Serena, are you awake",
    ],
)
def test_wake_phrase_matcher_accepts_live_asr_variants(transcript: str) -> None:
    assert wake_phrase_matches(transcript) is True


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "Serena",
        "hey Siri",
        "hey Sabrina",
        "hey there",
        "open the window",
    ],
)
def test_wake_phrase_matcher_rejects_unrelated_speech(transcript: str) -> None:
    assert wake_phrase_matches(transcript) is False


def test_structured_wake_events_never_persist_the_transcript() -> None:
    microphone = _Microphone([b"\0" * 2_560])
    events = []
    listener = WakeOnlyListener(
        _Scorer([0.9]),
        WakeGate(0.5, patience_frames=1, cooldown_seconds=0),
        microphone,
        lambda: None,
        phrase_verifier=_Verifier(
            [PhraseVerification(True, "Hey Serena, private trailing words")]
        ),
        phrase_post_roll_frames=0,
        event_sink=events.append,
    )
    assert listener.run() is True
    candidate = next(
        event for event in events if event["event"] == "wake.phrase_candidate"
    )
    assert candidate["accepted"] is True
    assert candidate["recognized_words"] == 5
    assert "transcript" not in candidate


def test_listener_fails_closed_when_pipewire_frames_stall() -> None:
    microphone = _Microphone([])
    clock = _Clock()
    events = []

    def stall(*, timeout: float):
        assert timeout == 0.25
        clock.value = 1.1
        raise queue.Empty

    microphone.frames.get = stall
    listener = WakeOnlyListener(
        _Scorer([]),
        WakeGate(0.5, patience_frames=1, cooldown_seconds=0),
        microphone,
        lambda: None,
        phrase_verifier=_Verifier([]),
        event_sink=events.append,
        microphone_stall_seconds=1.0,
        monotonic=clock,
    )

    with pytest.raises(RuntimeError, match="produced no audio frames"):
        listener.run()

    assert microphone.closed is True
    assert [event["event"] for event in events] == [
        "wake.listener_started",
        "wake.microphone_stalled",
        "wake.listener_stopped",
    ]
    assert events[1]["reason"] == "frame_timeout"


def test_listener_exits_immediately_when_pipewire_capture_fails() -> None:
    microphone = _Microphone([])
    microphone.failure_reason = "PipeWire capture exited with status 1"
    events = []
    listener = WakeOnlyListener(
        _Scorer([]),
        WakeGate(0.5, patience_frames=1, cooldown_seconds=0),
        microphone,
        lambda: None,
        phrase_verifier=_Verifier([]),
        event_sink=events.append,
    )

    with pytest.raises(RuntimeError, match="status 1"):
        listener.run()

    stalled = next(
        event for event in events if event["event"] == "wake.microphone_stalled"
    )
    assert stalled["reason"] == "capture_failed"
    assert microphone.closed is True


def test_wake_service_is_bound_to_wireplumber_and_native_capture() -> None:
    repo = Path(__file__).resolve().parents[3]
    unit = (repo / "systemd" / "serena-wake-listener.service").read_text(
        encoding="utf-8"
    )

    assert "After=wireplumber.service" in unit
    assert "BindsTo=wireplumber.service" in unit
    assert "PartOf=wireplumber.service" in unit
    assert "WantedBy=wireplumber.service" in unit
    assert "WantedBy=default.target" not in unit
    assert "--pipewire-target dmic_dc_filtered" in unit
    assert "TimeoutStopSec=2" in unit
    assert "KillMode=control-group" in unit
    assert "LogRateLimitBurst=100" in unit
    assert "StartLimitIntervalSec=180" in unit
    assert "ExecStartPre=" not in unit
    assert "RestartSec=5" in unit
