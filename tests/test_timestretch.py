"""Serena's speed slider has to change her rate without changing her voice.

Resampling would be trivial and wrong: it moves pitch with speed, so she comes
out a different person. These pin the two things that actually matter, that the
duration really changes and that the pitch really does not, plus the streaming
contract, since this runs on audio arriving clause by clause.
"""

from __future__ import annotations

import numpy as np
import pytest

from voice.call.timestretch import (
    StreamingTimeStretch,
    clamp_speed,
    stretch_pcm16,
)

RATE = 24_000


def _tone(hz: float, seconds: float, rate: int = RATE) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * hz * t) * 12_000).astype("<i2").tobytes()


def _dominant_hz(pcm: bytes, rate: int = RATE) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum))])


def _seconds(pcm: bytes, rate: int = RATE) -> float:
    return len(pcm) / 2 / rate


@pytest.mark.parametrize("speed,expected", [(0.75, 4 / 3), (1.5, 2 / 3), (2.0, 0.5)])
def test_duration_scales_with_the_requested_speed(speed, expected) -> None:
    out = stretch_pcm16(_tone(220, 2.0), RATE, speed)
    assert _seconds(out) == pytest.approx(2.0 * expected, rel=0.06)


@pytest.mark.parametrize("speed", [0.7, 1.4])
def test_pitch_is_unchanged(speed) -> None:
    """The whole reason for WSOLA over resampling."""
    out = stretch_pcm16(_tone(220, 2.0), RATE, speed)
    assert _dominant_hz(out) == pytest.approx(220, abs=8)


def test_speed_one_returns_the_audio_untouched() -> None:
    original = _tone(300, 0.5)
    assert stretch_pcm16(original, RATE, 1.0) == original


def test_streaming_in_pieces_matches_a_single_pass() -> None:
    """It runs on the streaming path, so chunk boundaries must not matter."""
    whole = _tone(200, 1.5)
    once = stretch_pcm16(whole, RATE, 1.3)

    stretcher = StreamingTimeStretch(RATE, 1.3)
    pieces = b""
    step = 2 * 1_000  # 1000 samples at a time
    for offset in range(0, len(whole), step):
        pieces += stretcher.feed(whole[offset : offset + step])
    pieces += stretcher.flush()

    assert _seconds(pieces) == pytest.approx(_seconds(once), rel=0.05)
    assert _dominant_hz(pieces) == pytest.approx(200, abs=8)


def test_output_stays_inside_pcm16() -> None:
    loud = (np.full(RATE, 32_000, dtype="<i2")).tobytes()
    out = np.frombuffer(stretch_pcm16(loud, RATE, 0.8), dtype="<i2")
    assert out.size
    assert out.min() >= -32_768 and out.max() <= 32_767


def test_the_stretch_does_not_gate_the_audio_out() -> None:
    """Overlap-add with mismatched windows can quietly annihilate the signal."""
    source = _tone(220, 1.0)
    for speed in (0.75, 1.25):
        out = np.frombuffer(stretch_pcm16(source, RATE, speed), dtype="<i2")
        original = np.frombuffer(source, dtype="<i2")
        assert out.std() > original.std() * 0.5


@pytest.mark.parametrize(
    "given,expected",
    [(0.1, 0.5), (9.0, 2.0), ("fast", 1.0), (float("nan"), 1.0), (None, 1.0)],
)
def test_absurd_speeds_are_clamped_to_something_she_can_still_be(given, expected):
    assert clamp_speed(given) == expected


def test_empty_input_is_handled() -> None:
    stretcher = StreamingTimeStretch(RATE, 1.5)
    assert stretcher.feed(b"") == b""
    assert stretcher.flush() == b""


def test_the_slider_setting_round_trips(tmp_path) -> None:
    from voice.call.voice_speed import read_voice_speed, write_voice_speed

    path = tmp_path / "voice_speed"
    assert read_voice_speed(path) == 1.0  # unset means her normal rate
    assert write_voice_speed(1.35, path) == 1.35
    assert read_voice_speed(path) == 1.35
    assert write_voice_speed(99.0, path) == 2.0  # clamped, still readable
    assert read_voice_speed(path) == 2.0


def test_a_corrupt_setting_never_costs_her_the_ability_to_speak(tmp_path) -> None:
    path = tmp_path / "voice_speed"
    path.write_text("wat", encoding="utf-8")
    from voice.call.voice_speed import read_voice_speed

    assert read_voice_speed(path) == 1.0


def test_the_wrapper_speeds_up_a_real_backend_stream(monkeypatch) -> None:
    """One slider has to move every voice surface, so it wraps the engine."""
    import asyncio

    from voice.call.tts import DeterministicTTSStub, PCMChunk, SpeedAdjustedTTSBackend

    class _Engine(DeterministicTTSStub):
        async def stream(self, sentence, *, generation):
            for _ in range(8):
                yield PCMChunk(_tone(220, 0.25), RATE)

    async def collect(speed):
        monkeypatch.setenv("SERENA_CALL_VOICE_RATE", str(speed))
        backend = SpeedAdjustedTTSBackend(_Engine())
        out = b""
        async for chunk in backend.stream("anything", generation=1):
            out += chunk.pcm
        return out

    normal = asyncio.run(collect(1.0))
    faster = asyncio.run(collect(1.5))
    slower = asyncio.run(collect(0.75))

    assert _seconds(faster) == pytest.approx(_seconds(normal) / 1.5, rel=0.08)
    assert _seconds(slower) == pytest.approx(_seconds(normal) / 0.75, rel=0.08)
    assert _dominant_hz(faster) == pytest.approx(220, abs=8)


@pytest.mark.parametrize("speed", [0.6, 0.75, 0.9, 1.1, 1.5, 1.9])
def test_every_speed_survives_chunked_input(speed) -> None:
    """Below 1.0 the synthesis hop is the larger one, and reserving only for
    the analysis hop ran the reference window off the end of the buffer."""
    source = _tone(180, 1.2)
    stretcher = StreamingTimeStretch(RATE, speed)
    out = b""
    step = 2 * 1_920  # 80 ms, the size real TTS chunks arrive in
    for offset in range(0, len(source), step):
        out += stretcher.feed(source[offset : offset + step])
    out += stretcher.flush()
    assert _seconds(out) == pytest.approx(1.2 / speed, rel=0.08)
    assert _dominant_hz(out) == pytest.approx(180, abs=8)
