"""Wake-only Serena bootstrap with no network or conversational runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
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
WAKE_EVENT_PREFIX = "SERENA_WAKE_EVENT "


def journal_wake_event(payload: dict[str, object]) -> None:
    """Emit one transcript-free structured event to the user journal."""

    event = {
        **payload,
        "wall_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    print(
        WAKE_EVENT_PREFIX
        + json.dumps(event, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def normalize_wake_phrase(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def phrase_model_sha256(path: str | Path) -> str:
    """Hash a local verifier directory as one stable tree identity."""

    root = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    if not root.is_dir():
        raise FileNotFoundError(f"wake phrase model is missing at {root}")
    files = sorted(item for item in root.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"wake phrase model has no files at {root}")
    for item in files:
        digest.update(str(item.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


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
        event_sink: Callable[[dict[str, object]], None] = journal_wake_event,
        event_context: dict[str, object] | None = None,
        heartbeat_seconds: float | None = None,
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
        self.event_sink = event_sink
        self.event_context = dict(event_context or {})
        self.heartbeat_seconds = max(
            5.0,
            float(
                heartbeat_seconds
                if heartbeat_seconds is not None
                else os.environ.get("SERENA_WAKE_HEARTBEAT_SECONDS", "60")
            ),
        )

    def _event(self, name: str, **values: object) -> None:
        self.event_sink({"event": name, **self.event_context, **values})

    def run(self, stop: threading.Event | None = None) -> bool:
        stop = stop or threading.Event()
        recent_frames: deque[bytes] = deque(maxlen=self.phrase_window_frames)
        listener_id = uuid.uuid4().hex
        self.event_context.setdefault("listener_id", listener_id)
        last_heartbeat = time.monotonic()
        self.microphone.start()
        self._event("wake.listener_started")
        try:
            while not stop.is_set():
                try:
                    pcm = self.microphone.frames.get(timeout=0.25)
                except queue.Empty:
                    pcm = None
                now = time.monotonic()
                if now - last_heartbeat >= self.heartbeat_seconds:
                    self._event("wake.heartbeat")
                    last_heartbeat = now
                if pcm is None:
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
                normalized = normalize_wake_phrase(verification.transcript)
                self._event(
                    "wake.phrase_candidate",
                    score=round(score, 6),
                    accepted=verification.accepted,
                    recognized_words=len(normalized.split()),
                )
                if not verification.accepted:
                    self.scorer.reset()
                    self.gate.reset()
                    recent_frames.clear()
                    continue
                self.microphone.close()
                self._event("wake.accepted", score=round(score, 6))
                self.launcher()
                return True
            return False
        finally:
            self.microphone.close()
            self._event("wake.listener_stopped")


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
        event_context={
            "phrase": normalize_wake_phrase(args.phrase),
            "phrase_model_sha256": phrase_model_sha256(args.phrase_model),
        },
    )
    listener.run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
