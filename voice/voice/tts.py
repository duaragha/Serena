"""Text-to-speech module — Kokoro (local, natural) with Piper fallback."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from serena.config import TTSConfig

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_KOKORO_MODEL = _PROJECT_ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _PROJECT_ROOT / "models" / "kokoro" / "voices-v1.0.bin"


class TextToSpeech:
    """Synthesizes speech using Kokoro (local, natural sounding).

    Falls back to Piper if Kokoro model files aren't present.
    """

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._kokoro = None
        self._engine = "kokoro"

        if _KOKORO_MODEL.exists() and _KOKORO_VOICES.exists():
            try:
                from kokoro_onnx import Kokoro
                self._kokoro = Kokoro(str(_KOKORO_MODEL), str(_KOKORO_VOICES))
                voices = self._kokoro.get_voices()
                logger.info(
                    "TTS initialized — engine=kokoro, voice=af_heart, %d voices available, speed=%.2f",
                    len(voices), config.speed,
                )
                # Warm up to avoid cold-start latency on first real call
                warmup_start = time.perf_counter()
                self._kokoro.create("ready.", voice="af_heart", speed=1.0, lang="en-us")
                logger.info("Kokoro warmed up in %.0f ms", (time.perf_counter() - warmup_start) * 1000)
            except Exception:
                logger.exception("Kokoro failed to load, falling back to piper")
                self._engine = "piper"
        else:
            logger.info("Kokoro model not found, using piper")
            self._engine = "piper"

        if self._engine == "piper":
            self._length_scale = 1.0 / config.speed if config.speed > 0 else 1.0
            logger.info("TTS initialized — engine=piper, model=%s", config.piper_model)

    async def synthesize(self, text: str) -> str:
        """Convert text to speech and save as a WAV file.

        Returns path to the generated file. Caller owns cleanup
        unless using speak() which handles it.
        """
        if self._kokoro:
            return await self._synthesize_kokoro(text)
        return await self._synthesize_piper(text)

    async def _synthesize_kokoro(self, text: str) -> str:
        """Synthesize via Kokoro — local, natural sounding, ~1-2s on CPU."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = tmp.name
        tmp.close()

        start = time.perf_counter()

        loop = asyncio.get_event_loop()
        audio, sample_rate = await loop.run_in_executor(
            None,
            lambda: self._kokoro.create(
                text,
                voice="af_heart",
                speed=self._config.speed,
                lang="en-us",
            ),
        )

        await loop.run_in_executor(None, lambda: sf.write(output_path, audio, sample_rate))

        elapsed_ms = (time.perf_counter() - start) * 1000
        duration_s = len(audio) / sample_rate
        logger.info(
            "TTS (kokoro): %.1fs audio in %.0f ms (%.1fx realtime) — %d chars",
            duration_s, elapsed_ms, duration_s * 1000 / elapsed_ms if elapsed_ms > 0 else 0,
            len(text),
        )
        return output_path

    async def _synthesize_piper(self, text: str) -> str:
        """Synthesize via Piper TTS — local, fast, less natural."""
        from piper.download_voices import download_voice

        model_dir = _PROJECT_ROOT / "models" / "piper"
        model_path = model_dir / f"{self._config.piper_model}.onnx"
        config_path = model_dir / f"{self._config.piper_model}.onnx.json"

        if not model_path.exists() or not config_path.exists():
            logger.info("Downloading piper voice model '%s'...", self._config.piper_model)
            model_dir.mkdir(parents=True, exist_ok=True)
            download_voice(self._config.piper_model, model_dir)

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = tmp.name
        tmp.close()

        cmd = [
            "piper",
            "--model", str(model_path),
            "--output_file", output_path,
            "--length_scale", f"{self._length_scale:.2f}",
            "--sentence_silence", "0.15",
        ]

        start = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=text.encode("utf-8"))
        elapsed_ms = (time.perf_counter() - start) * 1000

        if proc.returncode != 0:
            Path(output_path).unlink(missing_ok=True)
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Piper failed (exit {proc.returncode}): {error_msg}")

        logger.info("TTS (piper): synthesized %d chars in %.0f ms", len(text), elapsed_ms)
        return output_path

    async def speak(self, text: str) -> None:
        """Synthesize text and play it through speakers."""
        wav_path = await self.synthesize(text)

        try:
            proc = await asyncio.create_subprocess_exec(
                "mpv", "--no-video", "--really-quiet", wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                logger.warning("mpv exited with code %d", proc.returncode)
        finally:
            Path(wav_path).unlink(missing_ok=True)
