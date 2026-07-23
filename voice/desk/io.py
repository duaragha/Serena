"""Local microphone, playback, overlay, and greeting-cache adapters."""

from __future__ import annotations

import array
import json
import math
import os
import queue
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from core.brain_lifetime import secure_directory, write_json_atomic
from voice.desk.greetings import MAX_GREETING_BYTES, GreetingAudio

MIC_SAMPLE_RATE = 16_000
WAKE_FRAME_SAMPLES = 1_280
WAKE_FRAME_BYTES = WAKE_FRAME_SAMPLES * 2
DEFAULT_STATE_PATH = Path.home() / ".config" / "serena" / "voice_state"
DEFAULT_FALLBACK_PATH = (
    Path.home() / ".cache" / "serena" / "desk-last-greeting.json"
)


def pcm_visual_level(pcm: bytes) -> float:
    """Map little-endian PCM16 RMS to a stable 0..1 visual level."""

    if not pcm or len(pcm) % 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    mean_square = sum(float(sample) * sample for sample in samples) / len(samples)
    if mean_square <= 1.0:
        return 0.0
    dbfs = 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)
    return max(0.0, min(1.0, (dbfs + 52.0) / 42.0))


class OverlayPublisher:
    """Drive the existing state file and low-latency amplitude websocket."""

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        websocket_url: str = "ws://127.0.0.1:8765",
    ) -> None:
        self.state_path = Path(state_path).expanduser()
        self.websocket_url = websocket_url
        self._socket = None
        self._lock = threading.Lock()
        self._last_state = ""
        self._closed = threading.Event()
        self._reconnect_wake = threading.Event()
        self._reconnect_thread: threading.Thread | None = None

    def open(self) -> None:
        with self._lock:
            if self._closed.is_set() or (
                self._reconnect_thread is not None
                and self._reconnect_thread.is_alive()
            ):
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                name="serena-overlay-publisher",
                daemon=True,
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        import websocket

        while not self._closed.is_set():
            with self._lock:
                connected = self._socket is not None
            if connected:
                self._reconnect_wake.wait(1.0)
                self._reconnect_wake.clear()
                continue
            try:
                socket = websocket.create_connection(
                    self.websocket_url,
                    timeout=0.4,
                    enable_multithread=True,
                )
                socket.settimeout(0.4)
            except Exception:
                self._reconnect_wake.wait(0.5)
                self._reconnect_wake.clear()
                continue
            with self._lock:
                if self._closed.is_set():
                    with suppress(Exception):
                        socket.close()
                    return
                self._socket = socket

    def set_state(self, state: str) -> None:
        if state not in {"idle", "listening", "thinking", "speaking", "working"}:
            raise ValueError(f"invalid voice state {state!r}")
        if state == self._last_state:
            return
        parent = secure_directory(self.state_path.parent)
        temporary = parent / f".{self.state_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(state + "\n", encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.state_path)
            self._last_state = state
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def set_amplitude(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        self.send_event({"type": "amplitude", "value": value})

    def send_event(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 60_000:
            raise ValueError("overlay event is too large")
        with self._lock:
            if self._socket is None:
                return
            try:
                self._socket.send(payload)
            except Exception:
                with suppress(Exception):
                    self._socket.close()
                self._socket = None
                self._reconnect_wake.set()

    def close(self) -> None:
        self.set_amplitude(0.0)
        self._closed.set()
        self._reconnect_wake.set()
        with self._lock:
            socket = self._socket
            self._socket = None
            reconnect_thread = self._reconnect_thread
            self._reconnect_thread = None
        if socket is not None:
            with suppress(Exception):
                socket.close()
        if (
            reconnect_thread is not None
            and reconnect_thread is not threading.current_thread()
        ):
            reconnect_thread.join(timeout=1.0)


class GreetingFetcher:
    """Fetch one hot server greeting after wake, with a local last-good fallback."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        fallback_path: Path = DEFAULT_FALLBACK_PATH,
        timeout: float = 1.2,
    ) -> None:
        self.url = url
        self.token = token
        self.fallback_path = Path(fallback_path).expanduser()
        self.timeout = max(0.2, float(timeout))

    def fetch(self) -> tuple[GreetingAudio, str]:
        request = Request(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                pcm = response.read(MAX_GREETING_BYTES + 1)
                if len(pcm) > MAX_GREETING_BYTES:
                    raise RuntimeError("desk greeting response was too large")
                sample_rate = int(response.headers["X-Serena-Sample-Rate"])
                greeting_id = response.headers["X-Serena-Greeting-Id"]
                greeting = GreetingAudio(
                    greeting_id,
                    sample_rate,
                    pcm,
                    "server-prefetched desk greeting",
                    time.time(),
                )
                if GreetingAudio.from_json(greeting.to_json()) is None:
                    raise RuntimeError("desk greeting response was invalid")
                write_json_atomic(
                    self.fallback_path,
                    {"version": 1, "greeting": greeting.to_json()},
                )
                return greeting, "server-cache"
        except Exception:
            fallback = self.load_fallback()
            if fallback is not None:
                return fallback, "local-fallback"
            return fallback_tone(), "tone-fallback"

    def load_fallback(self) -> GreetingAudio | None:
        try:
            payload = json.loads(self.fallback_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        return GreetingAudio.from_json(payload.get("greeting"))


def fallback_tone() -> GreetingAudio:
    sample_rate = 24_000
    duration_seconds = 0.22
    total_samples = int(sample_rate * duration_seconds)
    samples = array.array("h")
    for index in range(total_samples):
        time_s = index / sample_rate
        envelope = min(1.0, index / 240, (total_samples - index) / 480)
        value = math.sin(2 * math.pi * 523.25 * time_s)
        value += 0.45 * math.sin(2 * math.pi * 659.25 * time_s)
        samples.append(int(5_500 * envelope * value / 1.45))
    if sys.byteorder != "little":
        samples.byteswap()
    return GreetingAudio(
        "tone-fallback",
        sample_rate,
        samples.tobytes(),
        "local connection cue",
        time.time(),
    )


class SoundDeviceMicrophone:
    """Continuous local 16 kHz source. Frames never leave until the loop wakes."""

    def __init__(self, *, device: str | int | None = None, max_frames: int = 64):
        self.device = device
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self.dropped_frames = 0
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        def callback(indata, frames, _time_info, status) -> None:
            if status or frames != WAKE_FRAME_SAMPLES:
                self.dropped_frames += 1
            payload = bytes(indata)
            if len(payload) != WAKE_FRAME_BYTES:
                self.dropped_frames += 1
                return
            try:
                self.frames.put_nowait(payload)
            except queue.Full:
                self.dropped_frames += 1
                with suppress(queue.Empty):
                    self.frames.get_nowait()
                with suppress(queue.Full):
                    self.frames.put_nowait(payload)

        self._stream = sd.RawInputStream(
            samplerate=MIC_SAMPLE_RATE,
            blocksize=WAKE_FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self.device,
            latency="low",
            callback=callback,
        )
        self._stream.start()

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()


@dataclass(frozen=True, slots=True)
class PlaybackStart:
    monotonic_ns: int
    underflow: bool


class SoundDevicePlayback:
    """Low-latency PCM16 output that publishes each real chunk's level."""

    def __init__(
        self,
        overlay: OverlayPublisher,
        *,
        device: str | int | None = None,
    ) -> None:
        self.overlay = overlay
        self.device = device
        self.last_amplitude = 0.0
        self._stream = None
        self._sample_rate = 0
        self._first_write: PlaybackStart | None = None

    def start(self, sample_rate: int) -> None:
        self.finish()
        import sounddevice as sd

        self._stream = sd.RawOutputStream(
            samplerate=sample_rate,
            blocksize=0,
            channels=1,
            dtype="int16",
            device=self.device,
            latency="low",
        )
        self._stream.start()
        self._sample_rate = sample_rate
        self._first_write = None

    def write(self, pcm: bytes) -> PlaybackStart | None:
        if self._stream is None:
            raise RuntimeError("desk playback has not started")
        if not pcm or len(pcm) % 2:
            raise ValueError("desk playback requires whole PCM16 samples")
        underflow = bool(self._stream.write(pcm))
        self.last_amplitude = pcm_visual_level(pcm)
        self.overlay.set_amplitude(self.last_amplitude)
        if self._first_write is None:
            self._first_write = PlaybackStart(time.monotonic_ns(), underflow)
            return self._first_write
        return None

    def play(self, greeting: GreetingAudio, *, chunk_ms: int = 40) -> PlaybackStart:
        self.start(greeting.sample_rate)
        frame_bytes = max(2, greeting.sample_rate * 2 * chunk_ms // 1000)
        for offset in range(0, len(greeting.pcm), frame_bytes):
            self.write(greeting.pcm[offset : offset + frame_bytes])
        first = self._first_write
        self.finish()
        if first is None:
            raise RuntimeError("desk greeting produced no playback")
        return first

    def finish(self) -> None:
        stream = self._stream
        self._stream = None
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        finally:
            self.last_amplitude = 0.0
            self.overlay.set_amplitude(0.0)
            self._sample_rate = 0

    def close(self) -> None:
        self.finish()
