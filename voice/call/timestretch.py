"""Change how fast Serena talks without changing what she sounds like.

Pocket TTS has no speaking-rate control, and resampling would move her pitch
with the speed, which is a different voice rather than the same person talking
faster. So the audio is time-scaled with WSOLA: overlapping windows are laid
back down at a different spacing, each one shifted to wherever it correlates
best with what is already there, which keeps pitch and timbre intact.

It runs on the streaming path, so it has to work on audio arriving in pieces
without waiting for the end of the sentence. State carries across feeds, and
only fully overlap-added samples are released.
"""

from __future__ import annotations

import numpy as np

SAMPLE_DTYPE = "<i2"
MIN_SPEED = 0.5
MAX_SPEED = 2.0
# 40 ms windows at 24 kHz: long enough to span a pitch period of a low voice,
# short enough that a window never straddles two different phonemes.
WINDOW_MS = 40.0
# How far WSOLA may slide a window to find the best splice. Roughly one pitch
# period; without the search this degrades to plain OLA and sounds warbly.
SEARCH_MS = 12.0


def clamp_speed(speed: float) -> float:
    """Keep the rate inside what still sounds like her."""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(value):
        return 1.0
    return float(min(max(value, MIN_SPEED), MAX_SPEED))


class StreamingTimeStretch:
    """Time-scale PCM16 as it arrives. speed > 1 is faster, < 1 is slower."""

    def __init__(self, sample_rate: int, speed: float) -> None:
        self.sample_rate = int(sample_rate)
        self.speed = clamp_speed(speed)
        self.window = max(64, int(self.sample_rate * WINDOW_MS / 1000.0))
        # Even window, 50% overlap: the Hann pairs then sum to exactly one.
        self.window -= self.window % 2
        self.synthesis_hop = self.window // 2
        self.analysis_hop = max(1, int(round(self.synthesis_hop * self.speed)))
        self.search = max(0, int(self.sample_rate * SEARCH_MS / 1000.0))
        self._hann = np.hanning(self.window + 1)[:-1].astype(np.float64)
        self._input = np.zeros(0, dtype=np.float64)
        self._consumed = 0  # absolute read position within the stream
        self._output = np.zeros(0, dtype=np.float64)
        self._emitted = 0  # samples of _output already handed back
        self._reference = np.zeros(self.window, dtype=np.float64)
        self._primed = False

    @property
    def passthrough(self) -> bool:
        return self.analysis_hop == self.synthesis_hop

    def feed(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        if self.passthrough:
            return pcm
        samples = np.frombuffer(pcm, dtype=SAMPLE_DTYPE).astype(np.float64)
        self._input = np.concatenate((self._input, samples))
        self._process()
        return self._drain(final=False)

    def flush(self) -> bytes:
        if self.passthrough:
            return b""
        self._process()
        return self._drain(final=True)

    # --- internals ---

    def _needed(self) -> int:
        """Input we must hold before a window can be placed and searched.

        The larger of the two hops, not just the analysis one: below 1.0 the
        synthesis hop is the bigger of the pair, and reserving for the smaller
        left the reference window running off the end of the buffer.
        """
        return (
            self._consumed
            + self.window
            + max(self.analysis_hop, self.synthesis_hop)
            + self.search
        )

    def _process(self) -> None:
        while self._input.size >= self._needed():
            start = self._consumed
            if self._primed and self.search:
                start = self._best_offset(start)
            segment = self._input[start : start + self.window] * self._hann
            self._overlap_add(segment)
            self._reference = self._input[
                start + self.synthesis_hop : start + self.synthesis_hop + self.window
            ].copy()
            self._primed = True
            self._consumed += self.analysis_hop
            self._compact()

    def _best_offset(self, centre: int) -> int:
        """Slide the window to where it splices most smoothly.

        Correlating against what the previous window's tail predicted is the
        whole difference between WSOLA and plain overlap-add, which phase-
        cancels and gives speech a hollow, warbling edge.
        """
        if self._reference.size != self.window:
            return max(0, min(centre, max(0, self._input.size - self.window)))
        low = max(0, centre - self.search)
        high = min(self._input.size - self.window, centre + self.search)
        if high <= low:
            return max(0, min(centre, max(0, self._input.size - self.window)))
        window = np.lib.stride_tricks.sliding_window_view(
            self._input[low : high + self.window], self.window
        )
        scores = window @ self._reference
        return int(low + int(np.argmax(scores)))

    def _overlap_add(self, segment: np.ndarray) -> None:
        end = len(self._output) if self._primed else 0
        start = max(0, end - self.synthesis_hop) if self._primed else 0
        required = start + self.window
        if required > len(self._output):
            self._output = np.concatenate(
                (self._output, np.zeros(required - len(self._output), dtype=np.float64))
            )
        self._output[start : start + self.window] += segment

    def _compact(self) -> None:
        """Drop input we can no longer look back at, keeping indices honest."""
        keep_from = max(0, self._consumed - self.search - self.window)
        if keep_from > self.sample_rate:  # only worth doing in bulk
            self._input = self._input[keep_from:]
            self._consumed -= keep_from

    def _drain(self, *, final: bool) -> bytes:
        # A tail of one window is still being summed into, so it is not final
        # until no more windows are coming.
        limit = len(self._output) if final else max(0, len(self._output) - self.window)
        if limit <= self._emitted:
            return b""
        block = self._output[self._emitted : limit]
        self._emitted = limit
        if final:
            self._output = np.zeros(0, dtype=np.float64)
            self._emitted = 0
        return np.clip(np.rint(block), -32768, 32767).astype(SAMPLE_DTYPE).tobytes()


def stretch_pcm16(pcm: bytes, sample_rate: int, speed: float) -> bytes:
    """One-shot convenience for callers that already hold a whole utterance."""
    stretcher = StreamingTimeStretch(sample_rate, speed)
    if stretcher.passthrough:
        return pcm
    return stretcher.feed(pcm) + stretcher.flush()
