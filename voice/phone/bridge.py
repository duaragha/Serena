"""SIP bridge: Linphone calls to and from Raghav, spoken through /ws/desk.

Runs in a Linux container on the PC's docker VM. The official liblinphone
Python wheel has no sound card support, so the image pairs its wrapper with
the libraries from the Linphone desktop AppImage, which include PulseAudio.
A private PulseAudio daemon inside the container provides two null sinks:

    phone_rx   Linphone plays the caller's voice here; we read its monitor.
    phone_tx   We play Serena's voice here; Linphone captures its monitor.

Keeping the two directions on separate sinks means her own speech never
reaches the speech recogniser, so no echo canceller is needed.

Each answered call opens one hands-free desk session on the voice host
(`voice.call.server` on the PC), exactly like the desk client does: caller
audio goes up in 200 ms frames, the host decides when he has finished
speaking, and her reply comes back as PCM. While she talks, sustained speech
from him interrupts her.

Only calls from Raghav's own SIP account are answered. Outgoing calls are
requested over a small authenticated HTTP API that only the PC can reach.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("serena.phone")

SAMPLE_RATE = 16_000
CHUNK_SAMPLES = 320  # 20 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2
RX_SINK = "phone_rx"
TX_SINK = "phone_tx"
TX_SOURCE = "phone_mic"
MAX_CALL_TEXT = 600


def _read_secret(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class PhoneConfig:
    sip_user: str
    sip_password: str
    sip_domain: str
    raghav_user: str
    voice_url: str
    voice_token: str
    control_token: str
    control_host: str = "0.0.0.0"
    control_port: int = 8796
    tts_url: str = "http://127.0.0.1:8812"
    data_dir: str = "/data"
    greeting: str = "hey, it's me."
    idle_hangup_seconds: float = 90.0
    ring_seconds: float = 45.0
    allowed_callers: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PhoneConfig:
        env = dict(os.environ if env is None else env)
        sip = _load_env_file(env.get("PHONE_SIP_ENV", "/run/phone/sip.env"))
        merged = {**sip, **{k: v for k, v in env.items() if k.startswith("SIP_")}}
        config = cls(
            sip_user=merged.get("SIP_USER", ""),
            sip_password=merged.get("SIP_PASSWORD", ""),
            sip_domain=merged.get("SIP_DOMAIN", "sip.linphone.org"),
            raghav_user=merged.get("RAGHAV_SIP", env.get("RAGHAV_SIP", "")),
            voice_url=env.get("SERENA_VOICE_URL", "ws://10.0.2.2:8766/ws/desk"),
            voice_token=_read_secret(env.get("SERENA_VOICE_TOKEN_FILE",
                                             "/run/phone/chat_token")),
            control_token=_read_secret(env.get("PHONE_CONTROL_TOKEN_FILE",
                                               "/run/phone/control_token")),
            control_host=env.get("PHONE_CONTROL_HOST", "0.0.0.0"),
            control_port=int(env.get("PHONE_CONTROL_PORT", "8796")),
            tts_url=env.get("SERENA_CALL_TTS_REMOTE_URL", "http://127.0.0.1:8812"),
            data_dir=env.get("PHONE_DATA_DIR", "/data"),
            greeting=env.get("PHONE_GREETING", "hey, it's me."),
            idle_hangup_seconds=float(env.get("PHONE_IDLE_HANGUP_SECONDS", "90")),
            ring_seconds=float(env.get("PHONE_RING_SECONDS", "45")),
            allowed_callers=tuple(
                name.strip().lower()
                for name in env.get("PHONE_ALLOWED_CALLERS", "").split(",") if name.strip()),
        )
        missing = [name for name, value in (
            ("SIP_USER", config.sip_user), ("SIP_PASSWORD", config.sip_password),
            ("RAGHAV_SIP", config.raghav_user), ("voice token", config.voice_token),
            ("control token", config.control_token)) if not value]
        if missing:
            raise ValueError(f"phone bridge is missing {', '.join(missing)}")
        return config


def caller_allowed(username: str, domain: str, config: PhoneConfig) -> bool:
    """Only Raghav's own account (plus any explicitly listed one) may reach her."""

    allowed = {config.raghav_user.lower(), *config.allowed_callers}
    return (username or "").lower() in allowed and (
        domain or "").lower() == config.sip_domain.lower()


def rms(pcm: bytes) -> float:
    import numpy as np

    if len(pcm) < 2:
        return 0.0
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


class VoiceGate:
    """Sustained speech detector for interrupting her.

    Phone audio arrives already noise-suppressed by his handset, so a plain
    energy threshold held for a few hundred milliseconds is enough, and her
    own voice is on a different sink so it can never trigger this.
    """

    # Measured after RX gain: his line idles in the low hundreds, his speech
    # sits around 6000-9000.
    def __init__(self, threshold: float = 3000.0, sustain_ms: int = 300,
                 chunk_ms: int = 20) -> None:
        self.threshold = threshold
        self.needed = max(1, sustain_ms // chunk_ms)
        self.run = 0
        self.frames: list[bytes] = []

    def reset(self) -> None:
        self.run = 0
        self.frames = []

    def feed(self, pcm: bytes) -> bool:
        if rms(pcm) >= self.threshold:
            self.run += 1
            self.frames.append(pcm)
        else:
            self.run = 0
            self.frames = []
        return self.run >= self.needed


def apply_gain(pcm: bytes, gain: float) -> bytes:
    import numpy as np

    if gain == 1.0 or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


class AudioLink:
    """Caller audio in, Serena audio out, through the container's PulseAudio.

    Her speech is queued to a writer thread and played with a quarter second
    of buffer. The voice host streams replies about as fast as they play, so a
    tight buffer ran dry between chunks and he heard her stutter.
    """

    PLAYBACK_LATENCY_MS = 250

    def __init__(self, rx_gain: float = 4.0) -> None:
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=500)
        # His handset delivers speech around -24 dBFS, quiet enough that the
        # voice host's VAD never fired on a real call.
        self.rx_gain = rx_gain
        self._reader: subprocess.Popen | None = None
        self._writer: subprocess.Popen | None = None
        self._writer_rate = 0
        self._outbox: queue.Queue[tuple[int, int, bytes]] = queue.Queue()
        self._epoch = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._reader = subprocess.Popen(
            ["parec", f"--device={RX_SINK}.monitor", f"--rate={SAMPLE_RATE}",
             "--channels=1", "--format=s16le", "--latency-msec=20", "--raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, name="phone-rx", daemon=True).start()
        threading.Thread(target=self._speaker, name="phone-tx", daemon=True).start()

    def _pump(self) -> None:
        reader = self._reader
        buffer = b""
        while reader is not None and not self._stop.is_set():
            chunk = reader.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= CHUNK_BYTES:
                frame, buffer = buffer[:CHUNK_BYTES], buffer[CHUNK_BYTES:]
                frame = apply_gain(frame, self.rx_gain)
                try:
                    self.frames.put_nowait(frame)
                except queue.Full:
                    # A stalled consumer must not grow memory; drop the oldest.
                    with contextlib.suppress(queue.Empty):
                        self.frames.get_nowait()

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def play(self, pcm: bytes, rate: int) -> None:
        """Queue her speech; returns immediately."""

        if pcm:
            self._outbox.put((self._epoch, rate, pcm))

    def _speaker(self) -> None:
        while not self._stop.is_set():
            try:
                epoch, rate, pcm = self._outbox.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                if epoch != self._epoch:
                    continue
                writer = self._ensure_writer(rate)
            try:
                # Blocks while the player is full, which paces this thread.
                writer.stdin.write(pcm)
                writer.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                if epoch == self._epoch:
                    log.warning("player write failed: %s", error)
                with self._lock:
                    if self._writer is writer:
                        self._close_writer()

    def _ensure_writer(self, rate: int) -> subprocess.Popen:
        if self._writer is None or self._writer.poll() is not None or (
                self._writer_rate != rate):
            self._close_writer()
            self._writer = subprocess.Popen(
                ["pacat", "--playback", f"--device={TX_SINK}", f"--rate={rate}",
                 "--channels=1", "--format=s16le",
                 f"--latency-msec={self.PLAYBACK_LATENCY_MS}", "--raw"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._writer_rate = rate
        return self._writer

    def flush(self) -> None:
        """Stop her mid-sentence: drop what is queued and what is buffered."""

        with self._lock:
            self._epoch += 1
            while True:
                try:
                    self._outbox.get_nowait()
                except queue.Empty:
                    break
            self._close_writer()

    def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.kill()
            writer.wait(timeout=2)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        self.flush()
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.kill()
                reader.wait(timeout=2)
            except Exception:
                pass


def synthesize(text: str, tts_url: str) -> tuple[bytes, int]:
    """Her own voice for lines she says before the conversation starts."""

    os.environ.setdefault("SERENA_CALL_TTS_REMOTE_URL", tts_url)
    os.environ.setdefault("SERENA_CALL_TTS_REMOTE_FIRST_AUDIO_TIMEOUT", "20")
    os.environ.setdefault("SERENA_CALL_TTS_REMOTE_FALLBACK", "0")
    from voice.call.tts import RemotePocketTTSBackend, SpeedAdjustedTTSBackend

    async def run() -> tuple[bytes, int]:
        backend = SpeedAdjustedTTSBackend(RemotePocketTTSBackend(base_url=tts_url))
        await backend.warm()
        pcm = bytearray()
        rate = 24_000
        async for chunk in backend.stream(text, generation=1):
            pcm += chunk.pcm
            rate = chunk.sample_rate
        return bytes(pcm), rate

    return asyncio.run(run())


class Conversation:
    """One call's worth of talking, over a desk session on the voice host."""

    def __init__(
        self,
        config: PhoneConfig,
        audio: AudioLink,
        *,
        opening: str,
        transport_factory: Callable[[], Any] | None = None,
        speak: Callable[[str], tuple[bytes, int]] | None = None,
        on_finished: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.audio = audio
        self.opening = opening
        self._transport_factory = transport_factory or self._default_transport
        self._speak = speak or (lambda text: synthesize(text, config.tts_url))
        self._on_finished = on_finished or (lambda reason: None)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="phone-talk", daemon=True)
        self.transcript: list[tuple[str, str]] = []

    def _default_transport(self):
        from voice.desk.transport import DeskTransport

        return DeskTransport(self.config.voice_url, self.config.voice_token,
                             connect_timeout=20)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _say_opening(self) -> None:
        if not self.opening:
            return
        try:
            pcm, rate = self._speak(self.opening)
        except Exception as error:
            log.warning("opening line failed: %s", error)
            return
        # Queued whole, then watched, so an interruption cuts it short like
        # any other reply.
        self.audio.drain()
        self.audio.play(pcm, rate)
        started = time.monotonic()
        duration = len(pcm) / 2 / rate + AudioLink.PLAYBACK_LATENCY_MS / 1000 + 0.2
        gate = VoiceGate()
        while time.monotonic() - started < duration:
            if self._stop.is_set():
                return
            try:
                frame = self.audio.frames.get(timeout=0.02)
            except queue.Empty:
                continue
            if gate.feed(frame):
                log.info("opening interrupted after %.1fs", time.monotonic() - started)
                self.audio.flush()
                return
        log.info("opening spoken (%.1fs)", len(pcm) / 2 / rate)

    def _run(self) -> None:
        reason = "ended"
        transport = None
        try:
            transport = self._transport_factory()
            transport.connect()
            ready = transport.wait_ready(30)
            log.info("voice host ready=%s %s", ready, transport.failure or "")
            self._say_opening()
            if not ready:
                reason = f"voice host unavailable: {transport.failure}"
                self._fallback_apology()
                return
            reason = self._talk(transport)
        except Exception as error:
            log.exception("conversation failed")
            reason = f"error: {error}"
        finally:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.hangup()
            self._on_finished(reason)

    def _fallback_apology(self) -> None:
        try:
            pcm, rate = self._speak("my voice brain is offline right now. i'll text you instead.")
            self.audio.play(pcm, rate)
            time.sleep(len(pcm) / 2 / rate + 0.5)
        except Exception:
            pass

    def _talk(self, transport) -> str:
        with contextlib.ExitStack() as stack:
            debug = None
            debug_dir = os.environ.get("PHONE_DEBUG_AUDIO", "")
            if debug_dir:
                Path(debug_dir).mkdir(parents=True, exist_ok=True)
                debug = stack.enter_context(
                    wave.open(str(Path(debug_dir) / f"rx-{int(time.time())}.wav"), "wb"))
                debug.setnchannels(1)
                debug.setsampwidth(2)
                debug.setframerate(SAMPLE_RATE)
            return self._talk_loop(transport, debug)

    def _talk_loop(self, transport, debug) -> str:
        from voice.desk.transport import MicFramePacker

        packer = MicFramePacker()
        gate = VoiceGate()
        phase = "listening"
        last_voice = time.monotonic()
        output_rate = 0
        discard_audio = False
        peak = 0.0
        last_level_log = time.monotonic()
        self.audio.drain()
        transport.begin_listening()
        while not self._stop.is_set():
            if transport.failure:
                return f"voice host dropped: {transport.failure}"
            try:
                event = transport.events.get_nowait()
            except queue.Empty:
                event = None
            if event is not None:
                if event.kind == "disconnected":
                    return f"voice host dropped: {event.payload}"
                if event.kind == "audio":
                    if not discard_audio and output_rate:
                        self.audio.play(event.payload.pcm, output_rate)
                    continue
                control = event.payload if isinstance(event.payload, dict) else {}
                kind = str(control.get("type") or "")
                generation = control.get("generation")
                if isinstance(generation, int) and generation not in (0, transport.generation):
                    continue
                if kind == "stt.result":
                    self.transcript.append(("raghav", str(control.get("text") or "")))
                elif kind == "brain.done":
                    self.transcript.append(("serena", str(control.get("text") or "")))
                if kind not in ("brain.delta", "pong", "tailnet.path"):
                    log.info("voice event %s (phase %s)", kind, phase)
                if kind in ("endpoint.detected", "stt.start", "stt.result",
                            "brain.delta", "brain.done") and phase == "listening":
                    phase = "thinking"
                    gate.reset()
                elif kind == "audio.start":
                    output_rate = int(control.get("sample_rate") or 24_000)
                    discard_audio = False
                    phase = "speaking"
                elif kind in ("audio.end", "turn.listen"):
                    # Let buffered speech finish before his next turn opens.
                    phase = "listening"
                    output_rate = 0
                    packer.reset()
                    gate.reset()
                    last_voice = time.monotonic()
                    transport.begin_listening()
                elif kind == "call.ended":
                    return "voice host ended the call"
                elif kind == "error" and control.get("fatal"):
                    return f"voice host error: {control.get('message')}"
                continue

            try:
                frame = self.audio.frames.get(timeout=0.02)
            except queue.Empty:
                continue
            now = time.monotonic()
            level = rms(frame)
            peak = max(peak, level)
            if now - last_level_log >= 5:
                log.info("caller level peak=%.0f phase=%s", peak, phase)
                peak = 0.0
                last_level_log = now
            if debug is not None:
                debug.writeframes(frame)
            if phase == "listening":
                if level >= gate.threshold:
                    last_voice = now
                for wire in packer.feed(frame):
                    transport.send_mic_frame(wire)
                if now - last_voice > self.config.idle_hangup_seconds:
                    return "idle"
                continue
            if gate.feed(frame):
                # He is talking over her: stop, and hear him out.
                log.info("barge-in during %s", phase)
                barged = list(gate.frames)
                self.audio.flush()
                transport.cancel()
                discard_audio = True
                output_rate = 0
                packer.reset()
                gate.reset()
                transport.begin_listening()
                phase = "listening"
                last_voice = now
                for pcm in barged:
                    for wire in packer.feed(pcm):
                        transport.send_mic_frame(wire)
        return "hung up"


@dataclass
class CallRequest:
    request_id: str
    text: str
    created: float = field(default_factory=time.time)


class ControlServer:
    """`POST /call`, `POST /hangup`, `GET /health`, bearer-token protected."""

    def __init__(self, config: PhoneConfig, bridge: PhoneBridge) -> None:
        self.config = config
        self.bridge = bridge
        self.httpd = ThreadingHTTPServer((config.control_host, config.control_port),
                                         self._handler())

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: N802 - stdlib signature
                log.debug("control: " + fmt, *args)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                supplied = header[7:] if header.startswith("Bearer ") else ""
                return bool(supplied) and hmac.compare_digest(
                    supplied, server.config.control_token)

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                if self.path == "/health":
                    return self._reply(200, server.bridge.health())
                return self._reply(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > 16_384:
                    return self._reply(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._reply(400, {"error": "invalid json"})
                if self.path == "/call":
                    text = " ".join(str(body.get("text") or "").split())[:MAX_CALL_TEXT]
                    if not text:
                        return self._reply(400, {"error": "text is required"})
                    status, payload = server.bridge.request_call(text)
                    return self._reply(status, payload)
                if self.path == "/hangup":
                    server.bridge.request_hangup()
                    return self._reply(202, {"ok": True})
                return self._reply(404, {"error": "not found"})

        return Handler

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="phone-control",
                         daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()


class PhoneBridge:
    """Owns the Linphone core; every Linphone call happens on this thread."""

    def __init__(self, config: PhoneConfig) -> None:
        self.config = config
        self.audio = AudioLink(rx_gain=float(os.environ.get("PHONE_RX_GAIN", "4")))
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.core = None
        self.account = None
        self.call = None
        self.call_id = ""
        self.call_started = 0.0
        self.conversation: Conversation | None = None
        self.pending_opening = ""
        self.last_error = ""
        self.last_call: dict[str, Any] = {}
        self._state_lock = threading.Lock()
        self._registered = False
        self._in_call = False

    # -- thread-safe surface used by the control server -------------------
    def health(self) -> dict[str, Any]:
        with self._state_lock:
            return {"ok": True, "registered": self._registered, "in_call": self._in_call,
                    "last_error": self.last_error, "last_call": dict(self.last_call)}

    def request_call(self, text: str) -> tuple[int, dict[str, Any]]:
        with self._state_lock:
            if not self._registered:
                return 503, {"error": "sip line is not registered"}
            if self._in_call:
                return 409, {"error": "already on a call"}
            self._in_call = True
        request = CallRequest(uuid.uuid4().hex, text)
        self.commands.put(("call", request))
        return 202, {"ok": True, "request_id": request.request_id}

    def request_hangup(self) -> None:
        self.commands.put(("hangup", None))

    # -- Linphone thread --------------------------------------------------
    def _set_state(self, *, registered: bool | None = None, in_call: bool | None = None,
                   last_error: str | None = None,
                   last_call: dict[str, Any] | None = None) -> None:
        with self._state_lock:
            if registered is not None:
                self._registered = registered
            if in_call is not None:
                self._in_call = in_call
            if last_error is not None:
                self.last_error = last_error
            if last_call is not None:
                self.last_call = last_call

    def setup(self) -> None:
        import linphone

        config = self.config
        logging_service = linphone.LoggingService.get()
        level = os.environ.get("PHONE_LINPHONE_LOG", "warning").lower()
        logging_service.set_log_level(
            linphone.LogLevel.LogLevelMessage if level == "message"
            else linphone.LogLevel.LogLevelWarning)
        factory = linphone.Factory.get()
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        factory.set_data_dir(config.data_dir)
        factory.set_config_dir(config.data_dir)
        factory.set_cache_dir(config.data_dir)
        core = factory.create_core("", "", None)
        core.echo_cancellation_enabled = False
        # Tones (the SAS reminder, call waiting) play into the same sink we
        # listen to, where they read as him talking.
        core.call_tone_indications_enabled = False
        core.media_encryption = linphone.MediaEncryption.MediaEncryptionZRTP
        core.set_media_encryption_mandatory(False)
        core.video_capture_enabled = False
        core.video_display_enabled = False
        core.max_calls = 1
        # Calls reach her only over the TLS connection she keeps to the
        # provider, which authenticates who is calling. Plain SIP listeners
        # would let anyone who can reach the port claim to be Raghav.
        transports = core.transports
        transports.udp_port = 0
        transports.tcp_port = 5060 if os.environ.get("PHONE_SIP_LISTEN") == "tcp" else 0
        transports.tls_port = -1
        core.transports = transports
        for device in core.extended_audio_devices:
            if device.driver_name != "PulseAudio":
                continue
            if device.device_name == RX_SINK:
                core.default_output_audio_device = device
            elif device.device_name == TX_SOURCE:
                core.default_input_audio_device = device
        output = core.default_output_audio_device
        source = core.default_input_audio_device
        if output is None or output.device_name != RX_SINK or (
                source is None or source.device_name != TX_SOURCE):
            raise RuntimeError("PulseAudio call devices are missing")
        nat = core.create_nat_policy()
        nat.stun_server = "stun.linphone.org"
        nat.stun_enabled = True
        nat.ice_enabled = True
        core.nat_policy = nat
        params = core.create_account_params()
        identity = factory.create_address(f"sip:{config.sip_user}@{config.sip_domain}")
        identity.display_name = "Serena"
        params.identity_address = identity
        params.server_address = factory.create_address(
            f"sip:{config.sip_domain};transport=tls")
        params.register_enabled = True
        params.nat_policy = nat
        core.add_auth_info(factory.create_auth_info(
            config.sip_user, None, config.sip_password, None, None, config.sip_domain))
        account = core.create_account(params)
        core.add_account(account)
        core.default_account = account
        core.start()
        self.factory = factory
        self.core = core
        self.account = account
        log.info("audio in=%s out=%s", source.id, output.id)

    def run(self, stop: threading.Event) -> None:
        import linphone

        self.setup()
        self.audio.start()
        states = linphone.CallState
        last_state = None
        while not stop.is_set():
            self.core.iterate()
            self._set_state(registered=self.account.state
                            == linphone.RegistrationState.RegistrationStateOk)
            self._drain_commands()
            for incoming in self.core.calls:
                # The wrapper hands back a fresh object per lookup, so calls
                # are matched by their SIP call id, never by identity.
                if (incoming.dir == linphone.CallDir.CallDirIncoming
                        and incoming.state == states.CallStateIncomingReceived
                        and incoming.call_log.call_id != self.call_id):
                    self._on_incoming(incoming)
            call = self.call
            if call is not None:
                state = call.state
                if state != last_state:
                    log.info("call state %s", state)
                    last_state = state
                if state == states.CallStateStreamsRunning and self.conversation is None:
                    self._start_conversation()
                elif state in (states.CallStateOutgoingProgress,
                               states.CallStateOutgoingRinging) and (
                        time.time() - self.call_started > self.config.ring_seconds):
                    call.terminate()
                elif state in (states.CallStateEnd, states.CallStateError,
                               states.CallStateReleased):
                    self._finish_call(call)
                    last_state = None
            talk_over = (self.conversation is not None
                         and not self.conversation.thread.is_alive())
            if talk_over and self.call is not None and self.call.state not in (
                    states.CallStateEnd, states.CallStateError, states.CallStateReleased):
                # She hung up (idle, or the voice host went away).
                self.call.terminate()
            time.sleep(0.02)
        if self.call is not None:
            self.call.terminate()
        self.audio.stop()
        self.core.stop()

    def _drain_commands(self) -> None:
        import linphone

        while True:
            try:
                command, payload = self.commands.get_nowait()
            except queue.Empty:
                return
            if command == "hangup" and self.call is not None:
                self.call.terminate()
            elif command == "call":
                params = self.core.create_call_params(None)
                params.video_enabled = False
                params.media_encryption = linphone.MediaEncryption.MediaEncryptionZRTP
                target = self.factory.create_address(
                    f"sip:{self.config.raghav_user}@{self.config.sip_domain}")
                call = self.core.invite_address_with_params(target, params)
                if call is None:
                    self._set_state(in_call=False, last_error="invite failed")
                    continue
                self.call = call
                self.call_id = call.call_log.call_id
                self.call_started = time.time()
                self.pending_opening = payload.text
                self._set_state(last_call={"request_id": payload.request_id,
                                           "direction": "outgoing", "state": "ringing",
                                           "started": self.call_started})

    def _on_incoming(self, call) -> None:
        import linphone

        remote = call.remote_address
        if self.call is not None or not caller_allowed(remote.username, remote.domain,
                                                       self.config):
            log.warning("declining call from %s@%s", remote.username, remote.domain)
            call.decline(linphone.Reason.ReasonDeclined)
            return
        params = self.core.create_call_params(call)
        params.video_enabled = False
        call.accept_with_params(params)
        self.call = call
        self.call_id = call.call_log.call_id
        self.call_started = time.time()
        self.pending_opening = self.config.greeting
        self._set_state(in_call=True, last_call={
            "request_id": uuid.uuid4().hex, "direction": "incoming",
            "state": "answered", "started": self.call_started})

    def _start_conversation(self) -> None:
        opening, self.pending_opening = self.pending_opening, ""
        self.audio.drain()
        self.conversation = Conversation(
            self.config, self.audio, opening=opening,
            on_finished=lambda reason: log.info("conversation finished: %s", reason))
        self.conversation.start()
        with self._state_lock:
            self.last_call["state"] = "talking"
            self.last_call["answered"] = time.time()

    def _finish_call(self, call) -> None:
        reason = str(call.reason)
        if self.conversation is not None:
            self.conversation.stop()
            self.conversation.thread.join(timeout=5)
        self.audio.flush()
        with self._state_lock:
            self.last_call["state"] = "ended"
            self.last_call["ended"] = time.time()
            self.last_call["reason"] = reason
            if self.conversation is not None:
                self.last_call["turns"] = len(self.conversation.transcript)
            self._in_call = False
        self.conversation = None
        self.call = None
        self.call_id = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate configuration and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("PHONE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = PhoneConfig.from_env()
    if args.check:
        print(json.dumps({"sip_user": config.sip_user, "raghav": config.raghav_user,
                          "voice_url": config.voice_url}))
        return 0
    bridge = PhoneBridge(config)
    control = ControlServer(config, bridge)
    control.start()
    stop = threading.Event()
    try:
        bridge.run(stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        control.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
