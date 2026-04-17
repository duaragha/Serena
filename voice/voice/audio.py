"""Audio capture and playback module.

Uses sounddevice for mic capture at 16kHz/16-bit/mono (STT-ready format).
Provides callback-based streaming that yields audio chunks to consumers.
Playback via ffplay subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Audio format constants matching STT model expectations
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = np.int16
# 80ms chunks = 1280 samples. This is what openwakeword expects,
# and it divides evenly into Silero VAD's 512-sample requirement.
CHUNK_SAMPLES = 1280
BLOCKSIZE = CHUNK_SAMPLES


class AudioCapture:
    """Continuous microphone capture at 16kHz/16-bit/mono.

    Audio is streamed via an asyncio queue so consumers can iterate over
    chunks in an async context. The actual recording runs in a sounddevice
    callback thread.

    Usage:
        capture = AudioCapture()
        capture.start()
        async for chunk in capture.stream():
            process(chunk)  # numpy int16 array, shape (1280,)
        capture.stop()
    """

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = SAMPLE_RATE,
        chunk_samples: int = CHUNK_SAMPLES,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._chunk_samples = chunk_samples
        self._stream: sd.InputStream | None = None
        self._running = False
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sync_callbacks: list[Callable[[np.ndarray], None]] = []

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def chunk_samples(self) -> int:
        return self._chunk_samples

    @property
    def is_running(self) -> bool:
        return self._running

    def add_callback(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register a synchronous callback that receives each audio chunk.

        Callbacks run in the sounddevice thread -- keep them fast.
        """
        self._sync_callbacks.append(callback)

    def remove_callback(self, callback: Callable[[np.ndarray], None]) -> None:
        self._sync_callbacks.remove(callback)

    def start(self) -> None:
        """Start capturing audio from the microphone."""
        if self._running:
            logger.warning("AudioCapture already running")
            return

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        self._queue = asyncio.Queue(maxsize=100)
        self._running = True

        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            blocksize=self._chunk_samples,
            device=self._device,
            channels=CHANNELS,
            dtype="int16",
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info(
            "Audio capture started: %dHz, %d-sample chunks, device=%s",
            self._sample_rate,
            self._chunk_samples,
            self._device or "default",
        )

    def stop(self) -> None:
        """Stop capturing audio."""
        if not self._running:
            return

        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("Audio capture stopped")

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """sounddevice callback -- runs in a separate thread."""
        if status:
            logger.warning("Audio callback status: %s", status)

        # indata shape is (frames, channels) -- flatten to 1D int16
        chunk = indata[:, 0].copy()

        # Fire synchronous callbacks (wake word, VAD, etc.)
        for cb in self._sync_callbacks:
            try:
                cb(chunk)
            except Exception:
                logger.exception("Error in audio callback")

        # Async queue disabled — all consumers use sync callbacks.
        # Keeping the queue caused QueueFull spam when nothing drained it.

    async def stream(self) -> AsyncIterator[np.ndarray]:
        """Async iterator yielding audio chunks as numpy int16 arrays.

        Each chunk is shape (chunk_samples,) at the configured sample rate.
        """
        if self._queue is None:
            raise RuntimeError("AudioCapture not started -- call start() first")

        while self._running:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                yield chunk
            except asyncio.TimeoutError:
                continue

    def __del__(self) -> None:
        self.stop()


def play_audio(path: str | Path) -> None:
    """Play an audio file via ffplay (blocking).

    Uses ffplay from ffmpeg -- supports WAV, MP3, OGG, etc.
    Blocks until playback completes.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    try:
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ffplay not found -- install ffmpeg") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffplay exited with code {e.returncode}") from e


async def play_audio_async(path: str | Path) -> None:
    """Non-blocking version of play_audio for use in async contexts."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, play_audio, path)
