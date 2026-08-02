"""Local, privacy-bounded Serena desk wake and conversation client."""

from __future__ import annotations

import argparse
import importlib.metadata
import math
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from core.brain_lifetime import append_json_line
from voice.call.protocol import AudioFrame
from voice.call.wakeword import (
    OpenWakeWordScorer,
    WakeGate,
    WakeWordConfigurationError,
    WakeWordModelSpec,
    rms_dbfs,
    sha256_file,
)
from voice.call.wakeword_calibration import load_acceptance_manifest
from voice.desk.duplex import BargeInGate, speech_level_dbfs
from voice.desk.io import (
    DEFAULT_STATE_PATH,
    GreetingFetcher,
    OverlayPublisher,
    SoundDeviceMicrophone,
    SoundDevicePlayback,
    WakeGreetingHandoff,
    consume_wake_greeting_handoff,
    fallback_tone,
)
from voice.desk.transport import DeskTransport, MicFramePacker, TransportEvent

SAMPLE_DTYPE = "<i2"

DEFAULT_MODEL_PATH = Path.home() / ".config" / "serena" / "models" / "hey_serena.onnx"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "serena" / "chat_token"
DEFAULT_METRICS_PATH = Path.home() / ".local" / "state" / "serena" / "desk_metrics.jsonl"
DEFAULT_MANIFEST_PATH = Path.home() / ".config" / "serena" / "wakeword-acceptance.json"


class DeskConnectionLost(ConnectionError):
    """A recoverable loss of the remote desk voice transport."""


@dataclass(frozen=True, slots=True)
class ManifestWakeConfig:
    spec: WakeWordModelSpec
    gate: WakeGate
    input_device: int | str | None
    configuration_sha256: str


class WakeScorer(Protocol):
    def score_frame(self, frame: np.ndarray) -> float: ...

    def reset(self) -> None: ...


def _device_selector(value: str | int | None) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except ValueError:
        return value


def load_manifest_wake_config(
    path: Path,
    *,
    requested_device: str | int | None = None,
    sounddevice_module=None,
    package_version: str | None = None,
    validate_device: bool = True,
) -> ManifestWakeConfig:
    """Load frozen wake inputs.

    A named native capture backend may own runtime device validation.
    """

    try:
        manifest = load_acceptance_manifest(Path(path))
    except ValueError as exc:
        raise WakeWordConfigurationError(str(exc)) from exc
    required = {
        "configuration_sha256",
        "model_path",
        "model_sha256",
        "score_label",
        "verifier_threshold",
        "vad_threshold",
        "speex_noise_suppression",
        "threshold",
        "patience_frames",
        "cooldown_seconds",
        "device",
        "device_selector",
        "package_version",
    }
    missing = sorted(key for key in required if key not in manifest)
    if missing:
        raise WakeWordConfigurationError(
            "wake-word acceptance manifest is incomplete: " + ", ".join(missing)
        )
    if (
        manifest.get("target_phrase") != "hey serena"
        or manifest.get("sample_rate") != 16_000
        or manifest.get("frame_samples") != 1_280
    ):
        raise WakeWordConfigurationError(
            "wake-word acceptance manifest targets an incompatible audio contract"
        )

    model_path = Path(str(manifest.get("model_path") or "")).expanduser()
    verifier_raw = manifest.get("verifier_path")
    verifier_path = Path(str(verifier_raw)).expanduser() if verifier_raw else None
    try:
        if sha256_file(model_path) != str(manifest.get("model_sha256") or ""):
            raise WakeWordConfigurationError(
                "frozen hey serena model hash does not match the acceptance manifest"
            )
        if verifier_path is not None and sha256_file(verifier_path) != str(
            manifest.get("verifier_sha256") or ""
        ):
            raise WakeWordConfigurationError(
                "frozen verifier hash does not match the acceptance manifest"
            )
    except OSError as exc:
        raise WakeWordConfigurationError(
            f"cannot read a frozen wake-word model asset: {exc}"
        ) from exc

    if package_version is None:
        try:
            package_version = importlib.metadata.version("openwakeword")
        except importlib.metadata.PackageNotFoundError as exc:
            raise WakeWordConfigurationError(
                "openwakeword is not installed in this interpreter"
            ) from exc
    expected_package = str(manifest.get("package_version") or "")
    if package_version != expected_package:
        raise WakeWordConfigurationError(
            "openwakeword version does not match the acceptance manifest: "
            f"expected {expected_package}, got {package_version}"
        )

    manifest_selector = _device_selector(manifest.get("device_selector"))
    explicit_selector = _device_selector(requested_device)
    if requested_device is not None and explicit_selector != manifest_selector:
        raise WakeWordConfigurationError(
            "requested microphone does not match the acceptance manifest selector"
        )
    if validate_device:
        if sounddevice_module is None:
            try:
                import sounddevice as sounddevice_module
            except ImportError as exc:
                raise WakeWordConfigurationError(
                    "sounddevice is not installed"
                ) from exc
        resolved = (
            sounddevice_module.default.device[0]
            if manifest_selector is None
            else manifest_selector
        )
        try:
            info = sounddevice_module.query_devices(resolved, "input")
            sounddevice_module.check_input_settings(
                device=manifest_selector,
                channels=1,
                dtype="int16",
                samplerate=16_000,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise WakeWordConfigurationError(
                f"frozen microphone path is unavailable: {exc}"
            ) from exc
        identity = ":".join(
            (
                str(resolved),
                str(info.get("name") or "unknown"),
                str(info.get("hostapi") or "unknown"),
                str(info.get("max_input_channels") or "unknown"),
            )
        )
        if identity != str(manifest.get("device") or ""):
            raise WakeWordConfigurationError(
                "microphone identity does not match the acceptance manifest"
            )

    spec = WakeWordModelSpec(
        model_path=model_path,
        score_label=str(manifest.get("score_label") or ""),
        verifier_path=verifier_path,
        verifier_threshold=float(manifest["verifier_threshold"]),
        vad_threshold=float(manifest["vad_threshold"]),
        enable_speex_noise_suppression=bool(
            manifest["speex_noise_suppression"]
        ),
    )
    spec.validate()
    return ManifestWakeConfig(
        spec=spec,
        gate=WakeGate(
            float(manifest["threshold"]),
            int(manifest["patience_frames"]),
            float(manifest["cooldown_seconds"]),
        ),
        input_device=manifest_selector,
        configuration_sha256=str(manifest["configuration_sha256"]),
    )


class DeskMetrics:
    """Persist timing and state events without retaining audio or transcripts."""

    def __init__(self, path: Path = DEFAULT_METRICS_PATH) -> None:
        self.path = Path(path).expanduser()
        self.dropped = 0
        self.last_error = ""
        self._closed = threading.Event()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=512)
        self._thread = threading.Thread(
            target=self._write_loop,
            name="serena-desk-metrics",
            daemon=True,
        )
        self._thread.start()

    def record(self, event: str, **fields: Any) -> None:
        if self._closed.is_set():
            return
        payload = {
            "schema": 1,
            "event": event,
            "wall_time": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                else:
                    self._queue.task_done()
                    self.dropped += 1
        self._thread.join(timeout=1.0)

    def _write_loop(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                try:
                    append_json_line(self.path, payload)
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()


# A fixed voice threshold assumes a quiet microphone. This laptop's built-in
# DMIC idles around -42 dBFS, above the -45 default, so every frame read as
# speech: the barge-in gate cancelled her reply the instant she began forming
# it, and she went to "thinking" and then silent. Measure the room instead.
NOISE_CALIBRATION_SECONDS = 1.0
# A freshly opened capture hands back zero-filled frames before real audio
# flows. Those are not a quiet room, they are no room at all, and believing
# them measured a floor of -120 dBFS and dropped the threshold back to the
# default that the room sits above, so every frame read as speech again.
NOISE_CALIBRATION_DEADLINE_SECONDS = 5.0
NOISE_MIN_REAL_FRAMES = 8
NOISE_SETTLING_FRAMES = 3
NOISE_FLOOR_PERCENTILE = 75.0
NOISE_FLOOR_MARGIN_DB = 8.0
NOISE_FLOOR_CEILING_DBFS = -30.0


def is_real_audio(frame: bytes) -> bool:
    """True when a frame carries signal rather than digital silence."""
    if not frame:
        return False
    samples = np.frombuffer(frame, dtype=SAMPLE_DTYPE)
    return bool(samples.size) and bool(np.any(samples))


def measure_noise_floor(frames: list[bytes]) -> tuple[float, float]:
    """Return (floor dBFS, DC offset) for the room, ignoring settling frames.

    DC offset is removed before measuring. This laptop's AMD DMIC wedges to a
    DC-heavy signal after a reboot, and a constant offset reads as a loud room
    forever, which would pin the threshold above his actual voice.
    """
    real = [frame for frame in frames if is_real_audio(frame)]
    usable = real[NOISE_SETTLING_FRAMES:] or real
    blocks = [np.frombuffer(frame, dtype=SAMPLE_DTYPE).astype(np.float64) for frame in usable]
    blocks = [block for block in blocks if block.size]
    if not blocks:
        return float("nan"), 0.0
    offset = float(np.mean(np.concatenate(blocks)))
    levels = []
    for block in blocks:
        level = speech_level_dbfs(block)
        if math.isfinite(level):
            levels.append(level)
    if not levels:
        return float("nan"), offset
    return float(np.percentile(levels, NOISE_FLOOR_PERCENTILE)), offset


def calibrate_voice_threshold(
    frames: list[bytes],
    configured: float,
    *,
    margin_db: float = NOISE_FLOOR_MARGIN_DB,
    ceiling_dbfs: float = NOISE_FLOOR_CEILING_DBFS,
) -> float:
    """Lift the voice threshold above the room, never below the configured one.

    Per-frame levels at a high percentile rather than one RMS over everything:
    the first frames off a freshly opened capture carry settling garbage that
    reads far louder than the room, and one such frame would otherwise set the
    threshold for the whole session.

    Capped, because a threshold raised past speech would leave her unable to
    hear him at all, which is a worse failure than an occasional false trigger.
    """
    floor, _offset = measure_noise_floor(frames)
    if not math.isfinite(floor):
        return configured
    return min(max(configured, floor + margin_db), ceiling_dbfs)


@dataclass(frozen=True, slots=True)
class DeskConfig:
    websocket_url: str = "ws://127.0.0.1:8767/ws/desk"
    greeting_url: str = "http://127.0.0.1:8767/desk/greeting"
    connect_timeout: float = 6.0
    listen_idle_seconds: float = 12.0
    listen_max_seconds: float = 45.0
    response_timeout: float = 120.0
    voice_activity_dbfs: float = -45.0
    reconnect_attempts: int = 2
    reconnect_backoff_seconds: float = 0.25

    def __post_init__(self) -> None:
        if urlsplit(self.websocket_url).scheme not in {"ws", "wss"}:
            raise ValueError("desk websocket URL must use ws or wss")
        if urlsplit(self.greeting_url).scheme not in {"http", "https"}:
            raise ValueError("desk greeting URL must use http or https")
        for name in (
            "connect_timeout",
            "listen_idle_seconds",
            "listen_max_seconds",
            "response_timeout",
            "reconnect_backoff_seconds",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.listen_max_seconds < self.listen_idle_seconds:
            raise ValueError("listen_max_seconds cannot be shorter than idle timeout")
        if not -120.0 <= self.voice_activity_dbfs <= 0.0:
            raise ValueError("voice_activity_dbfs must be between -120 and 0")
        if self.reconnect_attempts < 0:
            raise ValueError("reconnect_attempts cannot be negative")


class DeskClient:
    """Own one local microphone and connect outward only after a valid wake."""

    def __init__(
        self,
        scorer: WakeScorer | None,
        gate: WakeGate,
        microphone: SoundDeviceMicrophone,
        playback: SoundDevicePlayback,
        overlay: OverlayPublisher,
        greeting_fetcher: GreetingFetcher,
        transport_factory,
        *,
        config: DeskConfig | None = None,
        metrics: DeskMetrics | None = None,
        local_fallback=None,
        session_stop: threading.Event | None = None,
    ) -> None:
        self.scorer = scorer
        self.gate = gate
        self.microphone = microphone
        self.playback = playback
        self.overlay = overlay
        self.greeting_fetcher = greeting_fetcher
        self.transport_factory = transport_factory
        self.config = config or DeskConfig()
        self.metrics = metrics or DeskMetrics()
        self.local_fallback = local_fallback
        self.session_stop = session_stop or threading.Event()
        # Replaced by the measured room floor once the microphone is running.
        self.voice_activity_dbfs = self.config.voice_activity_dbfs
        self.noise_calibrated = False

    def _session_should_stop(self, service_stop: threading.Event) -> bool:
        return service_stop.is_set() or self.session_stop.is_set()

    def _wait_for_session_stop(
        self, service_stop: threading.Event, timeout: float
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._session_should_stop(service_stop):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.session_stop.wait(min(0.05, remaining))
        return True

    def process_idle_frame(
        self, pcm: bytes, *, monotonic_ns: int | None = None
    ) -> tuple[bool, float]:
        if len(pcm) != 2_560:
            raise ValueError("wake frame must contain exactly 1,280 PCM16 samples")
        if self.scorer is None:
            raise RuntimeError("wake scorer is unavailable in handoff mode")
        frame = np.frombuffer(pcm, dtype="<i2")
        score = self.scorer.score_frame(frame)
        return self.gate.observe(score, monotonic_ns=monotonic_ns), score

    def _calibrate_microphone(self) -> None:
        """Learn the room's noise floor before deciding what counts as speech."""
        frames: list[bytes] = []
        started = time.monotonic()
        deadline = started + NOISE_CALIBRATION_SECONDS
        give_up = started + NOISE_CALIBRATION_DEADLINE_SECONDS
        while True:
            now = time.monotonic()
            real = sum(1 for frame in frames if is_real_audio(frame))
            # Keep waiting past the normal window while the capture is still
            # warming up and handing back silence, rather than calibrating to it.
            if now >= deadline and real >= NOISE_MIN_REAL_FRAMES:
                break
            if now >= give_up:
                break
            try:
                frames.append(self.microphone.frames.get(timeout=0.1))
            except queue.Empty:
                continue
        floor, offset = measure_noise_floor(frames)
        self.noise_calibrated = math.isfinite(floor)
        self.voice_activity_dbfs = calibrate_voice_threshold(
            frames, self.config.voice_activity_dbfs
        )
        self.metrics.record(
            "desk.noise_calibrated",
            configured_dbfs=round(self.config.voice_activity_dbfs, 2),
            threshold_dbfs=round(self.voice_activity_dbfs, 2),
            floor_dbfs=None if not math.isfinite(floor) else round(floor, 2),
            dc_offset=round(offset, 1),
            frames=len(frames),
            real_frames=sum(1 for frame in frames if is_real_audio(frame)),
            waited_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def run(
        self,
        stop: threading.Event | None = None,
        *,
        start_awake: bool = False,
    ) -> None:
        stop = stop or threading.Event()
        self.overlay.open()
        self.overlay.adopt_state()
        self.microphone.start()
        self._calibrate_microphone()
        self.metrics.record("desk.started", network_preconnected=False)
        try:
            if start_awake:
                self.session_stop.clear()
                self.metrics.record("wake.handoff_received")
                self._activate(
                    time.monotonic_ns(),
                    stop,
                    greeting_handoff=consume_wake_greeting_handoff(),
                )
                return
            while not stop.is_set():
                try:
                    pcm = self.microphone.frames.get(timeout=0.25)
                except queue.Empty:
                    continue
                woke, score = self.process_idle_frame(pcm)
                if not woke:
                    continue
                wake_ns = time.monotonic_ns()
                self.session_stop.clear()
                self.metrics.record(
                    "wake.detected",
                    score=round(score, 6),
                    model_label=getattr(self.scorer, "score_label", None),
                )
                self._activate(wake_ns, stop)
                if self.session_stop.is_set():
                    self.metrics.record("desk.session_dismissed")
                self.scorer.reset()
                self.microphone.drain()
                self.overlay.release_state()
        finally:
            self.playback.close()
            self.microphone.close()
            self.overlay.release_state()
            self.overlay.close()
            self.metrics.record("desk.stopped")
            close_fallback = getattr(self.local_fallback, "close", None)
            if callable(close_fallback):
                close_fallback()
            close_metrics = getattr(self.metrics, "close", None)
            if callable(close_metrics):
                close_metrics()

    def _activate(
        self,
        wake_ns: int,
        stop: threading.Event,
        *,
        greeting_handoff: WakeGreetingHandoff | None = None,
    ) -> None:
        # Wake is the start of a listening interaction. Show that immediately;
        # thinking begins only after Raghav has actually finished an utterance.
        self.overlay.set_state("listening")
        transport: DeskTransport = self.transport_factory()
        connect_thread = self._start_connect(transport)

        if greeting_handoff is None:
            greeting, source = self.greeting_fetcher.fetch()
            self.overlay.set_state("speaking")
            try:
                first_write = self.playback.play(greeting)
            except Exception as exc:
                self.metrics.record(
                    "greeting.playback_failed",
                    source=source,
                    error=f"{type(exc).__name__}: {exc}",
                )
                transport.close()
                return
            latency_ms = (first_write.monotonic_ns - wake_ns) / 1_000_000
            self.metrics.record(
                "greeting.first_write",
                call_id=transport.call_id,
                greeting_id=greeting.greeting_id,
                source=source,
                wake_to_first_write_ms=round(latency_ms, 3),
                underflow=first_write.underflow,
                assistant_voice=source != "tone-fallback",
                acoustic_acceptance_claim=False,
            )
        else:
            self.overlay.set_state("speaking")
            self.metrics.record(
                "greeting.handoff_consumed",
                call_id=transport.call_id,
                greeting_id=greeting_handoff.greeting_id,
                source=greeting_handoff.source,
            )
            while not self._session_should_stop(stop):
                remaining_ns = (
                    greeting_handoff.playback_end_monotonic_ns
                    - time.monotonic_ns()
                )
                if remaining_ns <= 0:
                    break
                self.session_stop.wait(min(0.05, remaining_ns / 1_000_000_000))
            self.microphone.drain()
            self.overlay.set_state("listening")
        if self._session_should_stop(stop):
            transport.close()
            return

        if (
            not self._run_remote_with_reconnect(transport, connect_thread, stop)
            and not self._session_should_stop(stop)
        ):
            self._run_local_fallback(stop)

    @staticmethod
    def _start_connect(transport: DeskTransport) -> threading.Thread:
        connect_thread = threading.Thread(
            target=transport.connect,
            name="serena-desk-connect",
            daemon=True,
        )
        connect_thread.start()
        return connect_thread

    def _run_remote_with_reconnect(
        self,
        transport: DeskTransport,
        connect_thread: threading.Thread,
        stop: threading.Event,
    ) -> bool:
        attempt = 0
        current = transport
        thread = connect_thread
        while not self._session_should_stop(stop):
            ready = current.wait_ready(self.config.connect_timeout)
            thread.join(timeout=0.2)
            if self._session_should_stop(stop):
                current.hangup()
                return True
            if ready:
                if attempt:
                    self.overlay.set_state("speaking")
                    cue = self.playback.play(fallback_tone())
                    self.metrics.record(
                        "desk.reconnected",
                        call_id=current.call_id,
                        attempt=attempt,
                        cue_first_write_ns=cue.monotonic_ns,
                        acoustic_acceptance_claim=False,
                    )
                try:
                    self._run_conversation(current, stop)
                    return True
                except DeskConnectionLost as exc:
                    connection_error = str(exc)
                except Exception as exc:
                    if current.failure:
                        connection_error = current.failure
                    else:
                        self.metrics.record(
                            "desk.session_failed",
                            call_id=current.call_id,
                            generation=current.generation,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return True
                finally:
                    self.playback.finish()
                    current.hangup()
                self.metrics.record(
                    "desk.disconnected",
                    call_id=current.call_id,
                    generation=current.generation,
                    error=connection_error,
                )
            else:
                self.metrics.record(
                    "desk.connection_failed",
                    call_id=current.call_id,
                    attempt=attempt,
                    error=current.failure or "voice runtime did not become ready",
                )
                current.close()

            if attempt >= self.config.reconnect_attempts:
                return False
            attempt += 1
            self.metrics.record("desk.reconnecting", attempt=attempt)
            delay = min(
                self.config.reconnect_backoff_seconds * (2 ** (attempt - 1)),
                2.0,
            )
            if self._wait_for_session_stop(stop, delay):
                current.close()
                return True
            current = self.transport_factory()
            thread = self._start_connect(current)
        current.close()
        return True

    def _run_local_fallback(self, stop: threading.Event) -> None:
        if self.local_fallback is None or stop.is_set():
            self.metrics.record("desk.local_fallback_unavailable", error="not configured")
            return
        try:
            self.overlay.set_state("speaking")
            cue = self.playback.play(fallback_tone())
            self.metrics.record(
                "desk.local_fallback_cue",
                first_write_ns=cue.monotonic_ns,
                acoustic_acceptance_claim=False,
            )
            self.local_fallback.run(stop)
        except Exception as exc:
            self.metrics.record(
                "desk.local_fallback_failed",
                stage="activation",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _run_conversation(
        self,
        transport: DeskTransport,
        stop: threading.Event,
        *,
        max_completed_turns: int | None = None,
    ) -> None:
        completed_turns = 0
        packer = MicFramePacker()
        phase = "listening"
        phase_started_ns = time.monotonic_ns()
        heard_voice = False
        expected_output_sequence = 0
        output_rate: int | None = None
        first_output_written = False
        # SERENA_DESK_BARGE_IN=0 restores the exact half-duplex behavior:
        # thinking/speaking phases never consume the microphone queue.
        #
        # Without a measured room the threshold is a guess, and a guess below
        # the real floor makes the gate fire on every frame and cancel her
        # reply before a word of it is spoken. Not being interruptible is a far
        # better failure than never answering, so barge-in sits this one out.
        barge_gate: BargeInGate | None = (
            BargeInGate(voice_activity_dbfs=self.voice_activity_dbfs)
            if self.noise_calibrated
            else None
        )
        if barge_gate is not None and not barge_gate.enabled:
            barge_gate = None
        if not self.noise_calibrated:
            self.metrics.record("desk.barge_in_disabled", reason="noise floor unmeasured")
        # After a barge-in cancels a generation, its already-in-flight TTS
        # frames may still arrive; drop them until the next audio.start.
        discard_stale_audio = False

        self.microphone.drain()
        transport.begin_listening()
        self.overlay.set_state("listening")
        self.metrics.record(
            "desk.listening",
            call_id=transport.call_id,
            generation=transport.generation,
        )

        while not self._session_should_stop(stop):
            try:
                event = transport.events.get_nowait()
            except queue.Empty:
                event = None
            if event is not None:
                if discard_stale_audio and event.kind == "audio":
                    continue
                outcome = self._handle_event(
                    transport,
                    event,
                    phase=phase,
                    expected_output_sequence=expected_output_sequence,
                    output_rate=output_rate,
                    first_output_written=first_output_written,
                )
                phase = outcome["phase"]
                expected_output_sequence = outcome["expected_output_sequence"]
                output_rate = outcome["output_rate"]
                first_output_written = outcome["first_output_written"]
                if discard_stale_audio and output_rate is not None:
                    discard_stale_audio = False
                if outcome["ended"]:
                    return
                if outcome["phase_changed"]:
                    phase_started_ns = time.monotonic_ns()
                if outcome["turn_complete"]:
                    completed_turns += 1
                    if (
                        max_completed_turns is not None
                        and completed_turns >= max_completed_turns
                    ):
                        return
                    self.microphone.drain()
                    packer.reset()
                    if barge_gate is not None:
                        barge_gate.reset()
                    discard_stale_audio = False
                    transport.begin_listening()
                    self.overlay.set_state("listening")
                    phase = "listening"
                    phase_started_ns = time.monotonic_ns()
                    heard_voice = False
                    expected_output_sequence = 0
                    output_rate = None
                    first_output_written = False
                    self.metrics.record(
                        "desk.listening",
                        call_id=transport.call_id,
                        generation=transport.generation,
                    )
            if transport.failure:
                raise DeskConnectionLost(transport.failure)

            now_ns = time.monotonic_ns()
            if phase == "listening":
                elapsed = (now_ns - phase_started_ns) / 1_000_000_000
                if (
                    (not heard_voice and elapsed >= self.config.listen_idle_seconds)
                    or elapsed >= self.config.listen_max_seconds
                ):
                    transport.cancel()
                    self.metrics.record(
                        "desk.listen_timeout",
                        call_id=transport.call_id,
                        generation=transport.generation,
                        heard_voice=heard_voice,
                        elapsed_ms=round(elapsed * 1_000, 3),
                    )
                    return
                try:
                    pcm = self.microphone.frames.get(timeout=0.08)
                except queue.Empty:
                    continue
                frame = np.frombuffer(pcm, dtype="<i2")
                if speech_level_dbfs(frame) >= self.voice_activity_dbfs:
                    heard_voice = True
                for wire_frame in packer.feed(pcm):
                    transport.send_mic_frame(wire_frame)
                continue

            elapsed = (now_ns - phase_started_ns) / 1_000_000_000
            if elapsed >= self.config.response_timeout:
                transport.cancel()
                self.metrics.record(
                    "desk.response_timeout",
                    call_id=transport.call_id,
                    generation=transport.generation,
                    phase=phase,
                    elapsed_ms=round(elapsed * 1_000, 3),
                )
                return
            if barge_gate is None:
                time.sleep(0.01)
                continue
            try:
                pcm = self.microphone.frames.get(
                    timeout=0.0 if event is not None else 0.08
                )
            except queue.Empty:
                continue
            playback_amplitude = (
                float(getattr(self.playback, "last_amplitude", 0.0))
                if phase == "speaking"
                else 0.0
            )
            if not barge_gate.feed(
                pcm, phase=phase, playback_amplitude=playback_amplitude
            ):
                continue
            barged_frames = barge_gate.trigger_frames
            self.playback.finish()
            transport.cancel()
            packer.reset()
            transport.begin_listening()
            self.overlay.set_state("listening")
            for barged_pcm in barged_frames:
                for wire_frame in packer.feed(barged_pcm):
                    transport.send_mic_frame(wire_frame)
            self.metrics.record(
                "desk.barge_in",
                call_id=transport.call_id,
                generation=transport.generation,
                phase=phase,
                sustained_frames=len(barged_frames),
            )
            phase = "listening"
            phase_started_ns = time.monotonic_ns()
            heard_voice = True
            expected_output_sequence = 0
            output_rate = None
            first_output_written = False
            discard_stale_audio = True
            barge_gate.reset()

    def _handle_event(
        self,
        transport: DeskTransport,
        event: TransportEvent,
        *,
        phase: str,
        expected_output_sequence: int,
        output_rate: int | None,
        first_output_written: bool,
    ) -> dict[str, Any]:
        result = {
            "phase": phase,
            "expected_output_sequence": expected_output_sequence,
            "output_rate": output_rate,
            "first_output_written": first_output_written,
            "phase_changed": False,
            "turn_complete": False,
            "ended": False,
        }
        if event.kind == "disconnected":
            raise DeskConnectionLost(str(event.payload))

        if event.kind == "audio":
            frame = event.payload
            if not isinstance(frame, AudioFrame):
                raise RuntimeError("desk transport emitted an invalid audio event")
            if output_rate is None or frame.header.sample_rate != output_rate:
                raise RuntimeError("desk audio arrived before a matching audio.start")
            if frame.header.sequence != expected_output_sequence:
                transport.report_output_gap(
                    expected_output_sequence, frame.header.sequence
                )
                raise RuntimeError(
                    "desk output sequence gap: "
                    f"expected {expected_output_sequence}, got {frame.header.sequence}"
                )
            first = self.playback.write(frame.pcm)
            if first is not None and not first_output_written:
                transport.playback_started(frame.header.sequence, first.monotonic_ns)
                first_output_written = True
                self.metrics.record(
                    "desk.response_first_write",
                    call_id=transport.call_id,
                    generation=transport.generation,
                    sequence=frame.header.sequence,
                    underflow=first.underflow,
                    acoustic_acceptance_claim=False,
                )
            result["phase"] = "speaking"
            result["phase_changed"] = phase != "speaking"
            result["expected_output_sequence"] = (
                expected_output_sequence + 1
            ) & 0xFFFF_FFFF
            result["first_output_written"] = first_output_written
            return result

        if event.kind != "control" or not isinstance(event.payload, dict):
            raise RuntimeError("desk transport emitted an invalid event")
        control = event.payload
        kind = str(control.get("type") or "")
        generation = control.get("generation")
        if isinstance(generation, int) and generation not in {0, transport.generation}:
            return result

        if kind == "code.panel":
            action = str(control.get("action") or "")
            if action == "open":
                self.overlay.send_event(
                    {"type": "code_start", "project": "coding", "status": "ready"}
                )
            elif action == "hide":
                self.overlay.send_event({"type": "code_hide"})
            else:
                raise RuntimeError("desk code.panel action is invalid")
            return result
        if kind in {"endpoint.detected", "stt.start", "stt.result", "brain.delta", "brain.done"}:
            if phase == "speaking" or output_rate is not None:
                return result
            result["phase"] = "thinking"
            result["phase_changed"] = phase != "thinking"
            if result["phase_changed"]:
                self.overlay.set_state("thinking")
            return result
        if kind == "audio.start":
            sample_rate = int(control.get("sample_rate", 0))
            if sample_rate <= 0:
                raise RuntimeError("desk audio.start omitted its sample rate")
            self.playback.start(sample_rate)
            self.overlay.set_state("speaking")
            result["phase"] = "speaking"
            result["phase_changed"] = phase != "speaking"
            result["output_rate"] = sample_rate
            return result
        if kind == "audio.end":
            last_sequence = control.get("last_sequence")
            if expected_output_sequence == 0 or last_sequence != (
                expected_output_sequence - 1
            ) & 0xFFFF_FFFF:
                raise RuntimeError("desk audio.end did not match received PCM")
            self.playback.finish()
            result["turn_complete"] = True
            return result
        if kind == "error":
            self.metrics.record(
                "desk.server_error",
                call_id=transport.call_id,
                generation=transport.generation,
                code=str(control.get("code") or "unknown"),
                fatal=bool(control.get("fatal")),
            )
            result["ended"] = True
            return result
        if kind == "call.ended":
            result["ended"] = True
        return result


def derive_greeting_url(websocket_url: str) -> str:
    parsed = urlsplit(websocket_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "/desk/greeting", "", ""))


def load_token(path: Path = DEFAULT_TOKEN_PATH, *, explicit: str = "") -> str:
    token = explicit.strip() or os.environ.get("SERENA_CALL_TOKEN", "").strip()
    if not token:
        try:
            token = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read Serena call token from {path}: {exc}") from exc
    if not token:
        raise RuntimeError("Serena call token is empty")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("SERENA_DESK_URL", "ws://127.0.0.1:8767/ws/desk")
    )
    parser.add_argument("--greeting-url", default=os.environ.get("SERENA_DESK_GREETING_URL"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--verifier", type=Path)
    parser.add_argument("--score-label")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--patience-frames", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=3.0)
    parser.add_argument("--verifier-threshold", type=float, default=0.1)
    parser.add_argument("--vad-threshold", type=float, default=0.0)
    parser.add_argument("--listen-idle-seconds", type=float, default=12.0)
    parser.add_argument("--listen-max-seconds", type=float, default=45.0)
    parser.add_argument("--response-timeout", type=float, default=120.0)
    parser.add_argument("--reconnect-attempts", type=int, default=2)
    parser.add_argument("--reconnect-backoff-seconds", type=float, default=0.25)
    parser.add_argument("--voice-activity-dbfs", type=float, default=-45.0)
    parser.add_argument(
        "--fallback-whisper-model",
        type=Path,
        default=None,
    )
    parser.add_argument("--input-device")
    parser.add_argument("--output-device")
    parser.add_argument("--overlay-url", default="ws://127.0.0.1:8765")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument(
        "--start-awake",
        action="store_true",
        help="start one visible conversation immediately after a wake-only handoff",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    stop = threading.Event()
    session_stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    def request_session_stop(_signum, _frame) -> None:
        session_stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, request_session_stop)

    token = load_token(args.token_file)
    greeting_url = args.greeting_url or derive_greeting_url(args.url)
    input_device = _device_selector(args.input_device)
    if args.manifest:
        frozen = load_manifest_wake_config(
            args.manifest,
            requested_device=input_device,
        )
        spec = frozen.spec
        gate = frozen.gate
        input_device = frozen.input_device
    else:
        spec = WakeWordModelSpec(
            args.model.expanduser(),
            score_label=args.score_label,
            verifier_path=args.verifier.expanduser() if args.verifier else None,
            verifier_threshold=args.verifier_threshold,
            vad_threshold=args.vad_threshold,
        )
        gate = WakeGate(args.threshold, args.patience_frames, args.cooldown_seconds)
    # Model construction is deliberately before either audio device opens.
    # The wake-only listener already verified the phrase. Loading the same ONNX
    # model again here added roughly 1.7 seconds before the greeting could play.
    scorer = None if args.start_awake else OpenWakeWordScorer(spec)
    overlay = OverlayPublisher(
        state_path=args.state_path,
        websocket_url=args.overlay_url,
    )
    microphone = SoundDeviceMicrophone(device=input_device)
    playback = SoundDevicePlayback(
        overlay,
        device=_device_selector(args.output_device),
    )
    config = DeskConfig(
        websocket_url=args.url,
        greeting_url=greeting_url,
        listen_idle_seconds=args.listen_idle_seconds,
        listen_max_seconds=args.listen_max_seconds,
        response_timeout=args.response_timeout,
        voice_activity_dbfs=args.voice_activity_dbfs,
        reconnect_attempts=args.reconnect_attempts,
        reconnect_backoff_seconds=args.reconnect_backoff_seconds,
    )
    metrics = DeskMetrics(args.metrics)
    from voice.desk.fallback import DEFAULT_WHISPER_MODEL, LocalColdResponder

    local_fallback = LocalColdResponder(
        microphone,
        playback,
        overlay,
        metrics,
        whisper_model=args.fallback_whisper_model or DEFAULT_WHISPER_MODEL,
        voice_activity_dbfs=args.voice_activity_dbfs,
        idle_seconds=args.listen_idle_seconds,
    )
    client = DeskClient(
        scorer,
        gate,
        microphone,
        playback,
        overlay,
        GreetingFetcher(greeting_url, token),
        lambda: DeskTransport(args.url, token, connect_timeout=config.connect_timeout),
        config=config,
        metrics=metrics,
        local_fallback=local_fallback,
        session_stop=session_stop,
    )

    client.run(stop, start_awake=args.start_awake)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
