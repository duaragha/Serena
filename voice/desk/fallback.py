"""Cold local desk reply when the home voice host cannot be reached."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from voice.call.wakeword import rms_dbfs
from voice.desk.greetings import GreetingAudio

DEFAULT_WHISPER_MODEL = (
    Path(__file__).resolve().parents[1] / "models" / "faster-whisper-tiny.en"
)
DEFAULT_KOKORO_MODEL = (
    Path(__file__).resolve().parents[1] / "models" / "kokoro-v1.0.int8.onnx"
)
DEFAULT_KOKORO_VOICES = (
    Path(__file__).resolve().parents[1] / "models" / "voices-v1.0.bin"
)
DEFAULT_CLAUDE = Path.home() / ".local" / "bin" / "claude"
FALLBACK_CWD = Path.home() / ".cache" / "serena" / "desk-fallback"


class LocalColdResponder:
    """One-process local ears and voice around a tool-free cold Claude turn."""

    def __init__(
        self,
        microphone,
        playback,
        overlay,
        metrics,
        *,
        whisper_model: Path = DEFAULT_WHISPER_MODEL,
        kokoro_model: Path = DEFAULT_KOKORO_MODEL,
        kokoro_voices: Path = DEFAULT_KOKORO_VOICES,
        voice: str = "af_heart",
        speed: float = 1.2,
        voice_activity_dbfs: float = -45.0,
        idle_seconds: float = 12.0,
        max_utterance_seconds: float = 30.0,
        trailing_silence_ms: int = 800,
        transcriber: Callable[[bytes], str] | None = None,
        brain: Callable[[str], str] | None = None,
        synthesizer: Callable[[str], GreetingAudio] | None = None,
    ) -> None:
        self.microphone = microphone
        self.playback = playback
        self.overlay = overlay
        self.metrics = metrics
        self.whisper_model = Path(whisper_model).expanduser()
        self.kokoro_model = Path(kokoro_model).expanduser()
        self.kokoro_voices = Path(kokoro_voices).expanduser()
        self.voice = voice
        self.speed = speed
        self.voice_activity_dbfs = voice_activity_dbfs
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.max_utterance_seconds = max(
            self.idle_seconds, float(max_utterance_seconds)
        )
        self.trailing_silence_frames = max(1, int(trailing_silence_ms / 80))
        self._transcriber_override = transcriber
        self._brain_override = brain
        self._synthesizer_override = synthesizer
        self._whisper: Any = None
        self._kokoro: Any = None

    def available(self) -> tuple[bool, str]:
        if self._transcriber_override is None and not self.whisper_model.is_dir():
            return False, f"local Whisper model is missing at {self.whisper_model}"
        if self._synthesizer_override is None and (
            not self.kokoro_model.is_file() or not self.kokoro_voices.is_file()
        ):
            return False, "local Kokoro assets are missing"
        if self._brain_override is None and not self._claude_path().is_file():
            return False, f"Claude CLI is missing at {self._claude_path()}"
        return True, "ready"

    def run(
        self,
        stop: threading.Event,
        *,
        max_turns: int | None = None,
    ) -> int:
        ready, reason = self.available()
        if not ready:
            self.metrics.record("desk.local_fallback_unavailable", error=reason)
            return 0
        completed = 0
        self.metrics.record("desk.local_fallback_started")
        while not stop.is_set():
            self.microphone.drain()
            self.overlay.set_state("listening")
            pcm = self._capture_utterance(stop)
            if not pcm:
                break
            started_ns = time.monotonic_ns()
            try:
                self.overlay.set_state("thinking")
                text = self._transcribe(pcm)
                if not text:
                    self.metrics.record("desk.local_fallback_no_speech")
                    break
                answer = self._answer(text)
                audio = self._synthesize(answer)
                self.overlay.set_state("speaking")
                first = self.playback.play(audio)
            except Exception as exc:
                self.metrics.record(
                    "desk.local_fallback_failed",
                    stage="turn",
                    error=f"{type(exc).__name__}: {exc}",
                )
                break
            completed += 1
            self.metrics.record(
                "desk.local_fallback_reply",
                input_chars=len(text),
                output_chars=len(answer),
                elapsed_ms=round((time.monotonic_ns() - started_ns) / 1_000_000, 3),
                first_write_ns=first.monotonic_ns,
                acoustic_acceptance_claim=False,
            )
            if max_turns is not None and completed >= max_turns:
                break
        self.overlay.release_state()
        self.metrics.record("desk.local_fallback_stopped", turns=completed)
        return completed

    def _capture_utterance(self, stop: threading.Event) -> bytes:
        started_ns = time.monotonic_ns()
        pre_roll: deque[bytes] = deque(maxlen=5)
        captured: list[bytes] = []
        speech_frames = 0
        silence_frames = 0
        while not stop.is_set():
            elapsed = (time.monotonic_ns() - started_ns) / 1_000_000_000
            if elapsed >= self.max_utterance_seconds:
                break
            if not captured and elapsed >= self.idle_seconds:
                return b""
            try:
                pcm = self.microphone.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if len(pcm) != 2_560:
                continue
            active = rms_dbfs(np.frombuffer(pcm, dtype="<i2")) >= (
                self.voice_activity_dbfs
            )
            if not captured:
                pre_roll.append(pcm)
                if not active:
                    continue
                captured.extend(pre_roll)
                speech_frames = 1
                silence_frames = 0
                continue
            captured.append(pcm)
            if active:
                speech_frames += 1
                silence_frames = 0
            else:
                silence_frames += 1
                if speech_frames >= 2 and silence_frames >= self.trailing_silence_frames:
                    break
        return b"".join(captured) if speech_frames >= 2 else b""

    def _transcribe(self, pcm: bytes) -> str:
        if self._transcriber_override is not None:
            return self._transcriber_override(pcm).strip()
        if self._whisper is None:
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(
                str(self.whisper_model),
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, _info = self._whisper.transcribe(
            samples,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _answer(self, text: str) -> str:
        if self._brain_override is not None:
            return self._brain_override(text).strip()
        from core.config import read_agent_context
        from core.daypart import TimeContext

        state = ""
        try:
            from core.brain_state import compact_active

            state = compact_active()
        except Exception:
            pass
        moment = TimeContext.now()
        system = "\n\n".join(
            part
            for part in (
                read_agent_context(),
                state,
                f"<current-time {moment.attributes()}>\n"
                "This is the live local system clock. Use it when the hour or "
                "the part of day matters, in your own words.\n"
                "</current-time>",
                "# Degraded desk voice\n"
                "The home brain is unreachable. Answer this transcribed desk turn "
                "as Serena in one to three short spoken sentences. Plain prose only. "
                "No markdown, lists, tools, stage directions, or mention of this "
                "fallback unless the user asks. Do not claim you checked live state.",
            )
            if part.strip()
        )
        FALLBACK_CWD.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        env["SERENA_FRONTDOOR"] = "1"
        result = subprocess.run(
            [
                str(self._claude_path()),
                "-p",
                text[:4_000],
                "--model",
                os.environ.get("SERENA_DESK_FALLBACK_MODEL", "sonnet"),
                "--effort",
                "low",
                "--system-prompt",
                system,
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--no-session-persistence",
                "--safe-mode",
            ],
            cwd=FALLBACK_CWD,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            error = " ".join(result.stderr.split())[:500]
            raise RuntimeError(f"cold Claude turn exited {result.returncode}: {error}")
        answer = _speech_text(result.stdout)
        if not answer:
            raise RuntimeError("cold Claude turn returned no spoken text")
        return answer[:2_000]

    def _synthesize(self, text: str) -> GreetingAudio:
        if self._synthesizer_override is not None:
            return self._synthesizer_override(text)
        if self._kokoro is None:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(
                str(self.kokoro_model), str(self.kokoro_voices)
            )
        samples, sample_rate = self._kokoro.create(
            text, voice=self.voice, speed=self.speed, lang="en-us"
        )
        pcm = (
            np.clip(np.asarray(samples), -1.0, 1.0) * 32767.0
        ).astype("<i2").tobytes()
        if not pcm:
            raise RuntimeError("local Kokoro returned no audio")
        return GreetingAudio(
            f"local-cold-{uuid.uuid4().hex}",
            int(sample_rate),
            pcm,
            text,
            time.time(),
        )

    @staticmethod
    def _claude_path() -> Path:
        configured = os.environ.get("SERENA_DESK_FALLBACK_CLAUDE", "").strip()
        if configured:
            return Path(configured).expanduser()
        discovered = shutil.which("claude")
        return Path(discovered) if discovered else DEFAULT_CLAUDE


def _speech_text(raw: str) -> str:
    text = re.sub(r"```.*?```", " ", raw or "", flags=re.DOTALL)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    text = re.sub(
        r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+)",
        "",
        text,
        flags=re.MULTILINE,
    )
    return " ".join(text.split()).strip()
