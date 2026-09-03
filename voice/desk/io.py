"""Local microphone, playback, overlay, and greeting-cache adapters."""

from __future__ import annotations

import array
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from core.brain_lifetime import secure_directory, write_json_atomic
from core.daypart import current_daypart
from voice.desk.greetings import MAX_GREETING_BYTES, GreetingAudio
from voice.desk.output_mute import read_voice_output_muted

MIC_SAMPLE_RATE = 16_000
WAKE_FRAME_SAMPLES = 1_280
WAKE_FRAME_BYTES = WAKE_FRAME_SAMPLES * 2
DEFAULT_STATE_PATH = Path.home() / ".config" / "serena" / "voice_state"
# One daypart. Past that the cached line is describing a different evening.
FALLBACK_MAX_AGE_SECONDS = 6 * 60 * 60

DEFAULT_FALLBACK_PATH = (
    Path.home() / ".cache" / "serena" / "desk-last-greeting.json"
)
FALLBACK_SCHEMA_VERSION = 2
WAKE_GREETING_HANDOFF_SCHEMA_VERSION = 1
DEFAULT_PIPEWIRE_WAKE_TARGET = "dmic_dc_filtered"
PIPEWIRE_CAPTURE_EXECUTABLE = "/usr/bin/pw-cat"
DEFAULT_WAKE_GREETING_HANDOFF_PATH = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "serena-wake-greeting-handoff.json"
)


@dataclass(frozen=True, slots=True)
class PlaybackStart:
    monotonic_ns: int
    underflow: bool


@dataclass(frozen=True, slots=True)
class WakeGreetingHandoff:
    greeting_id: str
    playback_end_monotonic_ns: int
    source: str


def record_wake_greeting_handoff(
    greeting: GreetingAudio,
    first_write: PlaybackStart,
    source: str,
    *,
    path: Path = DEFAULT_WAKE_GREETING_HANDOFF_PATH,
) -> WakeGreetingHandoff:
    duration_ns = round(
        len(greeting.pcm) / 2 / greeting.sample_rate * 1_000_000_000
    )
    handoff = WakeGreetingHandoff(
        greeting.greeting_id,
        first_write.monotonic_ns + duration_ns,
        source,
    )
    write_json_atomic(
        Path(path),
        {
            "version": WAKE_GREETING_HANDOFF_SCHEMA_VERSION,
            "greeting_id": handoff.greeting_id,
            "playback_end_monotonic_ns": handoff.playback_end_monotonic_ns,
            "source": handoff.source,
        },
    )
    return handoff


def consume_wake_greeting_handoff(
    *,
    path: Path = DEFAULT_WAKE_GREETING_HANDOFF_PATH,
    max_future_seconds: float = 15.0,
) -> WakeGreetingHandoff | None:
    marker = Path(path)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    finally:
        with suppress(OSError):
            marker.unlink()
    if (
        not isinstance(payload, dict)
        or payload.get("version") != WAKE_GREETING_HANDOFF_SCHEMA_VERSION
    ):
        return None
    try:
        handoff = WakeGreetingHandoff(
            str(payload["greeting_id"]),
            int(payload["playback_end_monotonic_ns"]),
            str(payload["source"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    now_ns = time.monotonic_ns()
    max_future_ns = now_ns + round(max(1.0, max_future_seconds) * 1_000_000_000)
    if (
        not handoff.greeting_id
        or not handoff.source
        or handoff.playback_end_monotonic_ns < now_ns - 2_000_000_000
        or handoff.playback_end_monotonic_ns > max_future_ns
    ):
        return None
    return handoff


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

    def adopt_state(self, *, stale_after: float = 90.0) -> str:
        """Take the overlay over without stomping a turn already in progress.

        The supervisor puts the microphone back on the wake word by starting a
        brand new client, which used to announce itself by writing "idle". If
        Raghav had typed to her in the meantime, that write landed while she
        was still mid-sentence and the dot field went dark on him. A new
        process knows nothing about the overlay, so it inherits whatever is
        there rather than overruling it. A state left behind by something that
        died is not inherited forever: after stale_after it is cleared.
        """
        try:
            current = self.state_path.read_text(encoding="utf-8").strip()
            age = time.time() - self.state_path.stat().st_mtime
        except OSError:
            current, age = "", 0.0
        busy = {"listening", "thinking", "speaking", "working"}
        if current in busy and age < stale_after:
            self._last_state = current
            return current
        self.set_state("idle")
        return "idle"

    def release_state(self) -> None:
        """Hand the overlay back, but only if it is still ours to hand back.

        A spoken conversation ending is not the machine going quiet. A typed
        turn can be mid-sentence on the same dot field, and this process
        writing "idle" on its way out blanked the dot under her while she was
        still talking. You may only clear the state you last wrote.
        """
        try:
            current = self.state_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current and current != self._last_state:
            self._last_state = current
            return
        self.set_state("idle")

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
                    response.headers.get("X-Serena-Greeting-Daypart", "") or "",
                )
                if GreetingAudio.from_json(greeting.to_json()) is None:
                    raise RuntimeError("desk greeting response was invalid")
                write_json_atomic(
                    self.fallback_path,
                    {
                        "version": FALLBACK_SCHEMA_VERSION,
                        "greeting": greeting.to_json(),
                    },
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
        if (
            not isinstance(payload, dict)
            or payload.get("version") != FALLBACK_SCHEMA_VERSION
        ):
            return None
        greeting = GreetingAudio.from_json(payload.get("greeting"))
        # A last-good line that was written for another part of day would greet
        # him with the wrong half of the clock.  The tone is better than that.
        if greeting is not None and greeting.daypart not in ("", current_daypart()):
            return None
        # And a last-good line has an expiry. The pool held one greeting, so
        # every wake after the first 503'd and replayed this file, which is how
        # a single odd greeting ("handsome weasel", 2026-08-21) became the thing
        # she said every time. Stale enough is worse than the plain tone.
        if greeting is not None:
            age = time.time() - float(greeting.created_at or 0.0)
            if age > FALLBACK_MAX_AGE_SECONDS:
                return None
        return greeting


MIC_OFF_CLIP_PATH = (
    Path.home() / ".config" / "serena" / "audio" / "mic-off-24k-mono-s16.raw"
)


def mic_off_audio() -> GreetingAudio:
    """Say "the mic's off" instead of beeping at him.

    He opened the app with the OS microphone muted and got the fallback tone,
    which reads as "something is broken" rather than the actual, fixable
    problem. The clip is pre-rendered in her own voice; if it is missing the
    tone is still better than silence.
    """
    try:
        pcm = MIC_OFF_CLIP_PATH.read_bytes()
        if pcm:
            return GreetingAudio(
                "mic-off", 24_000, pcm, "the mic's off.", time.time()
            )
    except OSError:
        pass
    return fallback_tone()


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


class PipeWireMicrophone:
    """Bounded native PipeWire capture for the always-on wake listener."""

    def __init__(
        self,
        *,
        target: str = DEFAULT_PIPEWIRE_WAKE_TARGET,
        max_frames: int = 64,
        executable: str = PIPEWIRE_CAPTURE_EXECUTABLE,
        terminate_timeout: float = 0.5,
        kill_timeout: float = 1.0,
        process_factory=None,
    ) -> None:
        if not str(target).strip():
            raise ValueError("PipeWire wake target cannot be empty")
        if max_frames < 1:
            raise ValueError("wake microphone queue must hold at least one frame")
        self.target = str(target).strip()
        self.executable = str(executable)
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self.dropped_frames = 0
        self.last_frame_monotonic_ns: int | None = None
        self._failure_reason: str | None = None
        self._terminate_timeout = max(0.0, float(terminate_timeout))
        self._kill_timeout = max(0.0, float(kill_timeout))
        self._process_factory = process_factory
        self._process = None
        self._reader: threading.Thread | None = None
        self._closing = threading.Event()

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    def _command(self) -> list[str]:
        return [
            self.executable,
            "--record",
            "--target",
            self.target,
            "--latency",
            "80ms",
            "--rate",
            str(MIC_SAMPLE_RATE),
            "--channels",
            "1",
            "--channel-map",
            "mono",
            "--format",
            "s16",
            "--media-role",
            "Communication",
            "-",
        ]

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("PipeWire wake capture is already running")
        self._closing.clear()
        self._failure_reason = None
        self.last_frame_monotonic_ns = None
        factory = self._process_factory or subprocess.Popen
        try:
            process = factory(
                self._command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
            )
        except OSError as exc:
            self._failure_reason = f"could not start PipeWire capture: {exc}"
            raise RuntimeError(self._failure_reason) from exc
        if process.stdout is None:
            with suppress(Exception):
                process.kill()
            self._failure_reason = "PipeWire capture has no audio stream"
            raise RuntimeError(self._failure_reason)
        self._process = process
        self._reader = threading.Thread(
            target=self._read_frames,
            name="serena-pipewire-capture",
            daemon=True,
        )
        self._reader.start()

    def _enqueue(self, payload: bytes) -> None:
        try:
            self.frames.put_nowait(payload)
        except queue.Full:
            self.dropped_frames += 1
            with suppress(queue.Empty):
                self.frames.get_nowait()
            with suppress(queue.Full):
                self.frames.put_nowait(payload)

    def _read_frames(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        pending = bytearray()
        try:
            while not self._closing.is_set():
                chunk = process.stdout.read(WAKE_FRAME_BYTES - len(pending))
                if not chunk:
                    break
                pending.extend(chunk)
                if len(pending) != WAKE_FRAME_BYTES:
                    continue
                payload = bytes(pending)
                pending.clear()
                self.last_frame_monotonic_ns = time.monotonic_ns()
                self._enqueue(payload)
        except Exception as exc:
            if not self._closing.is_set():
                self._failure_reason = (
                    f"PipeWire capture reader failed: {type(exc).__name__}"
                )
            return
        if self._closing.is_set():
            return
        returncode = process.poll()
        if returncode is None:
            self._failure_reason = "PipeWire capture stream closed unexpectedly"
        else:
            self._failure_reason = (
                f"PipeWire capture exited with status {returncode}"
            )
        if pending:
            self.dropped_frames += 1

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._closing.set()
        process = self._process
        reader = self._reader
        self._process = None
        self._reader = None
        if process is not None:
            try:
                running = process.poll() is None
            except Exception:
                running = True
            if running:
                with suppress(Exception):
                    process.terminate()
                try:
                    process.wait(timeout=self._terminate_timeout)
                except (subprocess.TimeoutExpired, TimeoutError):
                    with suppress(Exception):
                        process.kill()
                    with suppress(Exception):
                        process.wait(timeout=self._kill_timeout)
                except Exception:
                    with suppress(Exception):
                        process.kill()
            if process.stdout is not None:
                with suppress(Exception):
                    process.stdout.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self._kill_timeout)


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
        muted = read_voice_output_muted()
        output = bytes(len(pcm)) if muted else pcm
        underflow = bool(self._stream.write(output))
        self.last_amplitude = 0.0 if muted else pcm_visual_level(pcm)
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
