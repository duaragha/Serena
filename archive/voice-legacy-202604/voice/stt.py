"""Speech-to-text — Groq Whisper (cloud, fast) or local faster-whisper."""

from __future__ import annotations

import io
import logging
import os
import time
import wave

import numpy as np

from serena.config import STTConfig

logger = logging.getLogger(__name__)


class SpeechToText:
    """Transcribes audio. Uses Groq Whisper API by default for speed + accuracy.

    Falls back to local faster-whisper if Groq isn't available or configured.
    """

    def __init__(self, config: STTConfig) -> None:
        self._config = config
        self._engine = config.engine
        self._groq_client = None
        self._local_model = None

        if self._engine == "groq":
            try:
                from groq import Groq
                # Try multiple env var sources
                api_key = (
                    os.environ.get("GROQ_API_KEY")
                    or self._load_groq_key_from_claude_config()
                )
                if not api_key:
                    logger.warning("GROQ_API_KEY not found, falling back to local STT")
                    self._engine = "local"
                else:
                    self._groq_client = Groq(api_key=api_key)
                    logger.info("STT initialized — engine=groq, model=%s", config.model)
            except Exception:
                logger.exception("Groq init failed, falling back to local STT")
                self._engine = "local"

        if self._engine == "local":
            from faster_whisper import WhisperModel
            local_model = config.model if config.model.endswith(".en") else "base.en"
            logger.info("Loading whisper model '%s' on device '%s'", local_model, config.device)
            compute_type = "int8" if config.device == "cpu" else "float16"
            self._local_model = WhisperModel(
                local_model,
                device=config.device,
                compute_type=compute_type,
            )
            logger.info("Whisper model loaded, warming up...")
            warmup_start = time.perf_counter()
            warmup_audio = np.zeros(16000, dtype=np.float32)
            segments, _ = self._local_model.transcribe(
                warmup_audio, language=config.language, beam_size=1, vad_filter=False,
            )
            list(segments)
            logger.info("Whisper warmed up in %.0f ms", (time.perf_counter() - warmup_start) * 1000)

    @staticmethod
    def _load_groq_key_from_claude_config() -> str | None:
        """Try to find a Groq API key in ~/.claude.json MCP config."""
        try:
            import json
            from pathlib import Path
            cfg_path = Path.home() / ".claude.json"
            if not cfg_path.exists():
                return None
            cfg = json.loads(cfg_path.read_text())
            for server_cfg in cfg.get("mcpServers", {}).values():
                env = server_cfg.get("env", {})
                key = env.get("GROQ_API_KEY")
                if key:
                    return key
        except Exception:
            pass
        return None

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe a float32 audio array to text."""
        if audio.size == 0:
            return ""

        # Reject silence early
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < 0.003:
            logger.debug("Audio RMS %.5f below silence threshold, skipping", rms)
            return ""

        if self._engine == "groq":
            return self._transcribe_groq(audio, sample_rate)
        return self._transcribe_local(audio, sample_rate)

    def _transcribe_groq(self, audio: np.ndarray, sample_rate: int) -> str:
        """Send audio to Groq Whisper API as a WAV blob."""
        # Convert float32 [-1,1] to int16 PCM
        if audio.dtype == np.float32:
            int_audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        else:
            int_audio = audio.astype(np.int16)

        # Build a WAV file in memory
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int_audio.tobytes())
        buf.seek(0)

        start = time.perf_counter()
        try:
            result = self._groq_client.audio.transcriptions.create(
                file=("audio.wav", buf.read(), "audio/wav"),
                model=self._config.model,
                language=self._config.language,
                response_format="json",
            )
            text = result.text.strip()
        except Exception as exc:
            logger.warning("Groq STT failed: %s — falling back to silence", exc)
            return ""

        elapsed_ms = (time.perf_counter() - start) * 1000
        duration_ms = (audio.size / sample_rate) * 1000
        logger.info(
            "STT (groq): %.0f ms audio in %.0f ms -> '%s'",
            duration_ms, elapsed_ms, text[:80],
        )
        return text

    def _transcribe_local(self, audio: np.ndarray, sample_rate: int) -> str:
        """Local faster-whisper transcription."""
        start = time.perf_counter()
        # vad_filter=False because Silero already filters; Whisper's VAD clips utterances
        segments, _ = self._local_model.transcribe(
            audio,
            language=self._config.language,
            beam_size=5,
            vad_filter=False,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()

        elapsed_ms = (time.perf_counter() - start) * 1000
        duration_ms = (audio.size / sample_rate) * 1000
        logger.info(
            "STT (local): %.0f ms audio in %.0f ms (%.1fx realtime) -> '%s'",
            duration_ms, elapsed_ms,
            duration_ms / elapsed_ms if elapsed_ms > 0 else 0,
            text[:80],
        )
        return text
