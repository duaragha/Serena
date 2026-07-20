"""Wake-only Serena bootstrap with no network or conversational runtime."""

from __future__ import annotations

import argparse
import queue
import re
import signal
import subprocess
import threading
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from voice.call.wakeword import OpenWakeWordScorer, WakeGate
from voice.desk.client import (
    DEFAULT_MANIFEST_PATH,
    WakeScorer,
    _device_selector,
    load_manifest_wake_config,
)
from voice.desk.io import SoundDeviceMicrophone

WAKE_FRAME_BYTES = 2_560
FULL_VOICE_UNIT = "serena-dot-overlay.service"
DEFAULT_PHRASE_MODEL = (
    Path(__file__).resolve().parents[1] / "models" / "faster-whisper-tiny.en"
)
TARGET_WAKE_PHRASE = "hey serena"
PHRASE_WINDOW_FRAMES = 20
PHRASE_POST_ROLL_FRAMES = 2


def normalize_wake_phrase(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


@dataclass(frozen=True, slots=True)
class PhraseVerification:
    accepted: bool
    transcript: str


class WakePhraseVerifier(Protocol):
    def verify(self, pcm16: bytes) -> PhraseVerification: ...


class FasterWhisperWakePhraseVerifier:
    """Confirm a wake candidate with a second, fully local speech model."""

    def __init__(
        self,
        model_path: str | Path = DEFAULT_PHRASE_MODEL,
        *,
        target_phrase: str = TARGET_WAKE_PHRASE,
    ) -> None:
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"local wake phrase verifier is missing at {path}")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is unavailable to the wake listener") from exc
        self.target_phrase = normalize_wake_phrase(target_phrase)
        if not self.target_phrase:
            raise ValueError("wake target phrase cannot be empty")
        self._model = WhisperModel(
            str(path),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        silence = np.zeros(16_000, dtype=np.float32)
        segments, _ = self._model.transcribe(
            silence,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        list(segments)

    def verify(self, pcm16: bytes) -> PhraseVerification:
        if len(pcm16) % 2:
            return PhraseVerification(False, "")
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
        audio /= 32768.0
        segments, _ = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        transcript = " ".join(
            segment.text.strip() for segment in segments if segment.text.strip()
        ).strip()
        accepted = normalize_wake_phrase(transcript) == self.target_phrase
        return PhraseVerification(accepted, transcript)


def launch_full_voice_app() -> None:
    subprocess.run(
        [
            "systemctl",
            "--user",
            "--no-block",
            "start",
            FULL_VOICE_UNIT,
        ],
        check=True,
    )


class WakeOnlyListener:
    """Listen locally for one phrase, then hand off and exit."""

    def __init__(
        self,
        scorer: WakeScorer,
        gate: WakeGate,
        microphone: SoundDeviceMicrophone,
        launcher: Callable[[], None] = launch_full_voice_app,
        *,
        phrase_verifier: WakePhraseVerifier,
        phrase_window_frames: int = PHRASE_WINDOW_FRAMES,
        phrase_post_roll_frames: int = PHRASE_POST_ROLL_FRAMES,
    ) -> None:
        if phrase_window_frames < 1:
            raise ValueError("phrase_window_frames must be at least 1")
        if phrase_post_roll_frames < 0:
            raise ValueError("phrase_post_roll_frames cannot be negative")
        self.scorer = scorer
        self.gate = gate
        self.microphone = microphone
        self.launcher = launcher
        self.phrase_verifier = phrase_verifier
        self.phrase_window_frames = phrase_window_frames
        self.phrase_post_roll_frames = phrase_post_roll_frames

    def run(self, stop: threading.Event | None = None) -> bool:
        stop = stop or threading.Event()
        recent_frames: deque[bytes] = deque(maxlen=self.phrase_window_frames)
        self.microphone.start()
        try:
            while not stop.is_set():
                try:
                    pcm = self.microphone.frames.get(timeout=0.25)
                except queue.Empty:
                    continue
                if len(pcm) != WAKE_FRAME_BYTES:
                    continue
                recent_frames.append(pcm)
                score = self.scorer.score_frame(np.frombuffer(pcm, dtype="<i2"))
                if not self.gate.observe(score):
                    continue
                candidate = list(recent_frames)
                for _ in range(self.phrase_post_roll_frames):
                    try:
                        trailing = self.microphone.frames.get(timeout=0.12)
                    except queue.Empty:
                        break
                    if len(trailing) != WAKE_FRAME_BYTES:
                        continue
                    recent_frames.append(trailing)
                    candidate.append(trailing)
                verification = self.phrase_verifier.verify(b"".join(candidate))
                print(
                    "[wake-listener] phrase candidate "
                    f"score={score:.4f} accepted={verification.accepted} "
                    f"transcript={verification.transcript!r}",
                    flush=True,
                )
                if not verification.accepted:
                    self.scorer.reset()
                    self.gate.reset()
                    recent_frames.clear()
                    continue
                self.microphone.close()
                self.launcher()
                return True
            return False
        finally:
            self.microphone.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--input-device")
    parser.add_argument("--phrase-model", type=Path, default=DEFAULT_PHRASE_MODEL)
    parser.add_argument("--phrase", default=TARGET_WAKE_PHRASE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    requested_device = _device_selector(args.input_device)
    frozen = load_manifest_wake_config(
        args.manifest,
        requested_device=requested_device,
    )
    listener = WakeOnlyListener(
        OpenWakeWordScorer(frozen.spec),
        frozen.gate,
        SoundDeviceMicrophone(device=frozen.input_device),
        phrase_verifier=FasterWhisperWakePhraseVerifier(
            args.phrase_model,
            target_phrase=args.phrase,
        ),
    )
    listener.run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
