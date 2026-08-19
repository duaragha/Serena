"""Full-duplex barge-in gating for the desk conversation loop.

The microphone is always open; half-duplex was purely a consumption gate in
``DeskClient._run_conversation``. This module decides, frame by frame, when
speech during Serena's thinking/speaking phases is a deliberate interruption
rather than silence, noise, or the client hearing its own playback.

Energy plus sustain only. Wake-word scoring is deliberately out of scope.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Mapping

import numpy as np

from voice.call.wakeword import rms_dbfs

__all__ = ["BargeInGate", "speech_level_dbfs"]


def speech_level_dbfs(frame: np.ndarray) -> float:
    """Loudness of a frame with any constant offset removed.

    This laptop's AMD DMIC wedges to a DC-heavy signal after a reboot: a
    measured offset of -8300 alone reads as -12 dBFS, so plain RMS said "he is
    talking" on every single frame. The barge-in gate then cancelled her reply
    the instant she started forming it, and she went quiet mid-thought.
    """
    if frame.size == 0:
        return rms_dbfs(frame)
    centred = frame.astype(np.float64)
    return rms_dbfs(centred - centred.mean())

ENV_ENABLED = "SERENA_DESK_BARGE_IN"
ENV_SUSTAIN_MS = "SERENA_DESK_BARGE_SUSTAIN_MS"
ENV_THINKING_SUSTAIN_MS = "SERENA_DESK_BARGE_THINKING_SUSTAIN_MS"
ENV_MARGIN_DB = "SERENA_DESK_BARGE_MARGIN_DB"
ENV_PLAYBACK_SCALE_DB = "SERENA_DESK_BARGE_PLAYBACK_SCALE_DB"

DEFAULT_SUSTAIN_MS = 320.0
DEFAULT_THINKING_SUSTAIN_MS = 480.0
DEFAULT_MARGIN_DB = 6.0
DEFAULT_PLAYBACK_SCALE_DB = 12.0
DEFAULT_AMPLITUDE_FLOOR_DBFS = -40.0
DEFAULT_FRAME_MS = 80.0
DEFAULT_PRE_ROLL_FRAMES = 2

_BARGE_PHASES = frozenset({"thinking", "speaking"})


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class BargeInGate:
    """Detect sustained live speech during thinking/speaking phases.

    Feed one native mic frame at a time. A frame is a barge candidate when its
    RMS level clears the phase threshold; a trigger requires an unbroken streak
    of candidates. While Serena is audibly speaking the threshold carries an
    extra margin plus a playback-correlated floor so her own voice reaching the
    microphone is not mistaken for Raghav interrupting.
    """

    def __init__(
        self,
        *,
        voice_activity_dbfs: float,
        frame_ms: float = DEFAULT_FRAME_MS,
        amplitude_floor_dbfs: float = DEFAULT_AMPLITUDE_FLOOR_DBFS,
        pre_roll_frames: int = DEFAULT_PRE_ROLL_FRAMES,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.enabled = str(env.get(ENV_ENABLED, "1")).strip() != "0"
        self.voice_activity_dbfs = float(voice_activity_dbfs)
        self.margin_db = _env_float(env, ENV_MARGIN_DB, DEFAULT_MARGIN_DB)
        self.playback_scale_db = _env_float(
            env, ENV_PLAYBACK_SCALE_DB, DEFAULT_PLAYBACK_SCALE_DB
        )
        self.amplitude_floor_dbfs = float(amplitude_floor_dbfs)
        frame_ms = max(1.0, float(frame_ms))
        self.speaking_sustain_frames = max(
            1, round(_env_float(env, ENV_SUSTAIN_MS, DEFAULT_SUSTAIN_MS) / frame_ms)
        )
        self.thinking_sustain_frames = max(
            1,
            round(
                _env_float(env, ENV_THINKING_SUSTAIN_MS, DEFAULT_THINKING_SUSTAIN_MS)
                / frame_ms
            ),
        )
        self.pre_roll_frames = max(0, int(pre_roll_frames))
        self._recent: deque[bytes] = deque(
            maxlen=max(self.speaking_sustain_frames, self.thinking_sustain_frames)
            + self.pre_roll_frames
        )
        self._streak = 0
        self._trigger_frames: tuple[bytes, ...] = ()

    @property
    def trigger_frames(self) -> tuple[bytes, ...]:
        """The sustained streak plus pre-roll captured at the last trigger."""

        return self._trigger_frames

    def feed(self, pcm: bytes, *, phase: str, playback_amplitude: float) -> bool:
        """Score one mic frame; return True when a barge-in has triggered."""

        if phase not in _BARGE_PHASES:
            raise ValueError(f"barge-in gate cannot score phase {phase!r}")
        if not pcm or len(pcm) % 2:
            raise ValueError("barge-in gate requires whole PCM16 samples")
        if not self.enabled:
            return False
        self._recent.append(pcm)
        level = speech_level_dbfs(np.frombuffer(pcm, dtype="<i2"))
        if phase == "speaking":
            amplitude = max(0.0, min(1.0, float(playback_amplitude)))
            threshold = self.voice_activity_dbfs + self.margin_db
            floor = self.amplitude_floor_dbfs + amplitude * self.playback_scale_db
            candidate = level >= threshold and level >= floor
            required = self.speaking_sustain_frames
        else:
            candidate = level >= self.voice_activity_dbfs
            required = self.thinking_sustain_frames
        if not candidate:
            self._streak = 0
            return False
        self._streak += 1
        if self._streak < required:
            return False
        frames = list(self._recent)
        self._trigger_frames = tuple(frames[-(required + self.pre_roll_frames) :])
        self._streak = 0
        return True

    def reset(self) -> None:
        self._streak = 0
        self._recent.clear()
        self._trigger_frames = ()
