"""Voice Activity Detection module.

Uses Silero VAD to detect when the user starts and stops speaking.
After the wake word triggers, this module collects audio frames during
speech and returns the complete utterance once silence is detected.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import torch

logger = logging.getLogger(__name__)

# Silero VAD at 16kHz requires exactly 512 samples per call
_VAD_CHUNK_SIZE = 512
_SAMPLE_RATE = 16000


class VoiceActivityDetector:
    """Collects a spoken utterance using Silero VAD for endpoint detection.

    Feed audio chunks via process(). Once speech starts and then silence is
    detected for the configured duration, the complete utterance is returned
    as a numpy array.

    Usage:
        vad = VoiceActivityDetector()
        vad.start_listening()
        # In audio callback or loop:
        utterance = vad.process(chunk)
        if utterance is not None:
            transcribe(utterance)  # numpy int16 array of full utterance
    """

    def __init__(
        self,
        silence_threshold_ms: int = 700,
        vad_threshold: float = 0.4,
        max_utterance_seconds: float = 30.0,
        pre_speech_padding_ms: int = 600,
    ) -> None:
        """
        Args:
            silence_threshold_ms: Milliseconds of silence after speech to consider
                                  the utterance complete. Default 800ms.
            vad_threshold: Silero VAD probability threshold. Frames above this
                           are considered speech. Default 0.5.
            max_utterance_seconds: Maximum utterance length before forced cutoff.
                                   Prevents unbounded buffer growth.
            pre_speech_padding_ms: Audio to keep before speech onset for natural
                                    sounding capture. Default 300ms.
        """
        self._silence_threshold_ms = silence_threshold_ms
        self._vad_threshold = vad_threshold
        self._max_samples = int(max_utterance_seconds * _SAMPLE_RATE)
        self._pre_speech_samples = int(pre_speech_padding_ms / 1000 * _SAMPLE_RATE)

        # Load Silero VAD model
        self._model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            trust_repo=True,
        )
        self._model.eval()
        logger.info(
            "Silero VAD loaded (silence=%dms, threshold=%.2f)",
            silence_threshold_ms,
            vad_threshold,
        )

        # State
        self._listening = False
        self._speech_detected = False
        self._silence_start: float | None = None
        self._audio_buffer: list[np.ndarray] = []
        self._pre_speech_buffer: list[np.ndarray] = []
        # Accumulator for sub-512 chunks
        self._leftover = np.array([], dtype=np.int16)

    @property
    def is_listening(self) -> bool:
        return self._listening

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    def start_listening(self) -> None:
        """Begin listening for speech. Call after wake word detection."""
        self._listening = True
        self._speech_detected = False
        self._silence_start = None
        self._audio_buffer.clear()
        self._pre_speech_buffer.clear()
        self._leftover = np.array([], dtype=np.int16)
        self._reset_model()
        logger.debug("VAD: started listening for speech")

    def stop_listening(self) -> None:
        """Stop listening and discard any buffered audio."""
        self._listening = False
        self._speech_detected = False
        self._silence_start = None
        self._audio_buffer.clear()
        self._pre_speech_buffer.clear()
        self._leftover = np.array([], dtype=np.int16)

    def process(self, chunk: np.ndarray) -> np.ndarray | None:
        """Process an audio chunk through VAD.

        Args:
            chunk: Audio data as numpy int16 array at 16kHz. Any length is
                   accepted -- it will be sliced into 512-sample windows
                   internally as required by Silero.

        Returns:
            Complete utterance as numpy int16 array once silence is detected
            after speech, or None if still collecting.
        """
        if not self._listening:
            return None

        # Prepend any leftover samples from previous call
        if len(self._leftover) > 0:
            chunk = np.concatenate([self._leftover, chunk])
            self._leftover = np.array([], dtype=np.int16)

        # Process in 512-sample windows
        offset = 0
        while offset + _VAD_CHUNK_SIZE <= len(chunk):
            window = chunk[offset : offset + _VAD_CHUNK_SIZE]
            offset += _VAD_CHUNK_SIZE

            result = self._process_window(window)
            if result is not None:
                return result

        # Save leftover samples for next call
        if offset < len(chunk):
            self._leftover = chunk[offset:]

        return None

    def _process_window(self, window: np.ndarray) -> np.ndarray | None:
        """Process a single 512-sample window through Silero VAD."""
        # Get speech probability
        tensor = torch.from_numpy(window.astype(np.float32) / 32768.0)
        speech_prob = self._model(tensor, _SAMPLE_RATE).item()
        is_speech = speech_prob >= self._vad_threshold

        if not self._speech_detected:
            # Keep a rolling pre-speech buffer so we don't clip the start
            self._pre_speech_buffer.append(window.copy())
            max_pre_chunks = self._pre_speech_samples // _VAD_CHUNK_SIZE + 1
            if len(self._pre_speech_buffer) > max_pre_chunks:
                self._pre_speech_buffer.pop(0)

            if is_speech:
                # Speech just started -- include pre-speech padding
                self._speech_detected = True
                self._silence_start = None
                self._audio_buffer.extend(self._pre_speech_buffer)
                self._pre_speech_buffer.clear()
                logger.debug("VAD: speech started (prob=%.3f)", speech_prob)
        else:
            # Already in speech -- accumulate audio
            self._audio_buffer.append(window.copy())

            if is_speech:
                self._silence_start = None
            else:
                # Silence during speech -- start/continue silence timer
                if self._silence_start is None:
                    self._silence_start = time.monotonic()
                else:
                    silence_ms = (time.monotonic() - self._silence_start) * 1000
                    if silence_ms >= self._silence_threshold_ms:
                        return self._finalize_utterance()

            # Safety cutoff for very long utterances
            total_samples = sum(len(b) for b in self._audio_buffer)
            if total_samples >= self._max_samples:
                logger.warning("VAD: max utterance length reached, forcing cutoff")
                return self._finalize_utterance()

        return None

    def _finalize_utterance(self) -> np.ndarray:
        """Concatenate the buffered audio into a single utterance array."""
        utterance = np.concatenate(self._audio_buffer)
        duration_s = len(utterance) / _SAMPLE_RATE
        logger.info("VAD: utterance complete (%.1fs, %d samples)", duration_s, len(utterance))

        # Reset state for next utterance
        self._listening = False
        self._speech_detected = False
        self._silence_start = None
        self._audio_buffer.clear()
        self._pre_speech_buffer.clear()
        self._leftover = np.array([], dtype=np.int16)

        return utterance

    def _reset_model(self) -> None:
        """Reset Silero VAD internal state."""
        self._model.reset_states()
