"""Unit tests for the full-duplex barge-in gate (task 3)."""

from __future__ import annotations

from voice.desk.duplex import BargeInGate

FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz


def _frame(value: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * FRAME_SAMPLES


LOUD = _frame(8_000)   # ~-12 dBFS, clearly above every threshold
MID = _frame(823)      # ~-32 dBFS: above speaking threshold, below hot floor
QUIET = _frame(20)     # ~-64 dBFS, silence


def _gate(**env) -> BargeInGate:
    return BargeInGate(voice_activity_dbfs=-45.0, environ=env)


def test_sustained_speech_during_speaking_triggers_after_four_frames():
    gate = _gate()
    results = [
        gate.feed(LOUD, phase="speaking", playback_amplitude=0.0)
        for _ in range(4)
    ]
    assert results == [False, False, False, True]


def test_short_burst_does_not_trigger_and_quiet_resets_streak():
    gate = _gate()
    for _ in range(3):
        gate.feed(LOUD, phase="speaking", playback_amplitude=0.0)
    assert gate.feed(QUIET, phase="speaking", playback_amplitude=0.0) is False
    # Streak reset: three more loud frames still must not trigger.
    results = [
        gate.feed(LOUD, phase="speaking", playback_amplitude=0.0)
        for _ in range(3)
    ]
    assert True not in results


def test_playback_amplitude_raises_the_self_hear_floor():
    # -32 dBFS clears the speaking threshold (-39) but with playback at full
    # amplitude the floor moves to -28, so her own voice level is rejected...
    hot = _gate()
    assert not any(
        hot.feed(MID, phase="speaking", playback_amplitude=1.0) for _ in range(8)
    )
    # ...while the same level with silent playback is a real interruption.
    cold = _gate()
    results = [
        cold.feed(MID, phase="speaking", playback_amplitude=0.0)
        for _ in range(4)
    ]
    assert results[-1] is True


def test_thinking_needs_only_the_short_sustain():
    gate = _gate()
    results = [
        gate.feed(LOUD, phase="thinking", playback_amplitude=0.0)
        for _ in range(2)
    ]
    assert results == [False, True]


def test_kill_switch_disables_everything():
    gate = _gate(SERENA_DESK_BARGE_IN="0")
    assert gate.enabled is False
    assert not any(
        gate.feed(LOUD, phase="speaking", playback_amplitude=0.0)
        for _ in range(10)
    )


def test_trigger_frames_carry_streak_plus_pre_roll():
    gate = _gate()
    gate.feed(QUIET, phase="speaking", playback_amplitude=0.0)
    gate.feed(QUIET, phase="speaking", playback_amplitude=0.0)
    for _ in range(3):
        assert gate.feed(LOUD, phase="speaking", playback_amplitude=0.0) is False
    assert gate.feed(LOUD, phase="speaking", playback_amplitude=0.0) is True
    frames = gate.trigger_frames
    # 4 sustained + 2 pre-roll frames, so the barged speech onset survives.
    assert len(frames) == 6
    assert frames[-4:] == (LOUD, LOUD, LOUD, LOUD)
    gate.reset()
    assert gate.trigger_frames == ()
