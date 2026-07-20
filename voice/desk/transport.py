"""Authenticated thin-client transport for the shared `/ws/desk` pipeline."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from voice.call.protocol import (
    MIC_FRAME_BYTES,
    MIC_SAMPLE_RATE,
    AudioFrame,
    AudioHeader,
    AudioKind,
    encode_control,
    parse_audio_frame,
)


@dataclass(frozen=True, slots=True)
class TransportEvent:
    kind: str
    payload: dict[str, Any] | AudioFrame | str


class MicFramePacker:
    """Re-chunk native 80 ms wake frames into exact 200 ms wire frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, pcm: bytes) -> Iterator[bytes]:
        if not pcm or len(pcm) % 2:
            raise ValueError("desk microphone input must contain whole PCM16 samples")
        self._buffer.extend(pcm)
        while len(self._buffer) >= MIC_FRAME_BYTES:
            frame = bytes(self._buffer[:MIC_FRAME_BYTES])
            del self._buffer[:MIC_FRAME_BYTES]
            yield frame

    def reset(self) -> None:
        self._buffer.clear()


class DeskTransport:
    """One shutdown-safe desk session over the v2a binary protocol."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        connect_timeout: float = 5.0,
        event_limit: int = 512,
    ) -> None:
        if not token.strip():
            raise ValueError("desk transport requires an authentication token")
        self.url = url
        self.token = token.strip()
        self.connect_timeout = max(0.5, float(connect_timeout))
        self.events: queue.Queue[TransportEvent] = queue.Queue(maxsize=event_limit)
        self.call_id = f"desk-{uuid.uuid4().hex}"
        self.generation = 0
        self._input_sequence = 0
        self._socket = None
        self._receiver: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._failure = ""

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and not self._closed.is_set()

    @property
    def failure(self) -> str:
        return self._failure

    def connect(self) -> None:
        import websocket

        with self._lifecycle_lock:
            if self._closed.is_set() or self._socket is not None:
                return
        try:
            socket = websocket.create_connection(
                self.url,
                header=[f"Authorization: Bearer {self.token}"],
                timeout=self.connect_timeout,
                enable_multithread=True,
            )
            socket.settimeout(0.5)
        except Exception as exc:
            self._fail(f"connect failed: {exc}")
            return
        with self._lifecycle_lock:
            if self._closed.is_set():
                with suppress(Exception):
                    socket.close()
                return
            self._socket = socket
            self._receiver = threading.Thread(
                target=self._receive_loop,
                name="serena-desk-websocket",
                daemon=True,
            )
            self._receiver.start()
        try:
            self._send_control(
                "call.start",
                call_id=self.call_id,
                generation=0,
                greeting=False,
            )
        except Exception as exc:
            self._fail(f"call start failed: {exc}")

    def wait_ready(self, timeout: float | None = None) -> bool:
        self._ready.wait(self.connect_timeout if timeout is None else max(0, timeout))
        return self.ready

    def begin_listening(self) -> int:
        if not self.ready:
            raise RuntimeError(self._failure or "desk transport is not ready")
        self.generation += 1
        self._input_sequence = 0
        self._send_control("ptt.begin", generation=self.generation)
        return self.generation

    def send_mic_frame(self, pcm: bytes) -> None:
        if len(pcm) != MIC_FRAME_BYTES:
            raise ValueError(f"desk mic frame must contain {MIC_FRAME_BYTES} bytes")
        frame = AudioFrame(
            AudioHeader(
                kind=AudioKind.MIC_PCM16,
                flags=0,
                sequence=self._input_sequence,
                sample_rate=MIC_SAMPLE_RATE,
                timestamp_us=time.monotonic_ns() // 1_000,
            ),
            pcm,
        ).pack()
        self._send(frame, binary=True)
        self._input_sequence = (self._input_sequence + 1) & 0xFFFF_FFFF

    def cancel(self) -> None:
        if self._socket is not None and not self._closed.is_set():
            self._send_control("cancel", generation=self.generation)

    def playback_started(self, sequence: int, timestamp_ns: int) -> None:
        """Report first local buffer write, never claim acoustic playback."""

        self._send_control(
            "playback.started",
            generation=self.generation,
            sequence=sequence,
            timestamp_us=timestamp_ns // 1_000,
            measurement_point="audio_track_play_return",
        )

    def report_output_gap(self, expected: int, received: int) -> None:
        self._send_control(
            "sequence.gap",
            generation=self.generation,
            expected=expected,
            received=received,
            direction="output",
        )

    def hangup(self) -> None:
        if self._socket is not None and not self._closed.is_set():
            with suppress(Exception):
                self._send_control("hangup")
        self.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed.is_set() and self._socket is None:
                return
            self._closed.set()
            socket = self._socket
            self._socket = None
            receiver = self._receiver
            self._receiver = None
        if socket is not None:
            with suppress(Exception):
                socket.close()
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)

    def _send_control(self, control_type: str, **fields: Any) -> None:
        self._send(encode_control(control_type, **fields), binary=False)

    def _send(self, payload: str | bytes, *, binary: bool) -> None:
        with self._send_lock:
            socket = self._socket
            if socket is None or self._closed.is_set():
                raise RuntimeError(self._failure or "desk websocket is closed")
            try:
                if binary:
                    import websocket

                    socket.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    socket.send(payload)
            except Exception as exc:
                self._fail(f"send failed: {exc}")
                raise

    def _receive_loop(self) -> None:
        try:
            import websocket

            while not self._closed.is_set():
                socket = self._socket
                if socket is None:
                    return
                try:
                    raw = socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw in {None, "", b""}:
                    self._fail("desk websocket closed")
                    return
                if isinstance(raw, bytes):
                    self._put(TransportEvent("audio", parse_audio_frame(raw, inbound=False)))
                    continue
                try:
                    control = json.loads(raw)
                except (TypeError, ValueError):
                    self._fail("desk server sent malformed JSON")
                    return
                if not isinstance(control, dict) or not isinstance(
                    control.get("type"), str
                ):
                    self._fail("desk server sent an invalid control")
                    return
                if control["type"] == "call.ready":
                    if control.get("ready") is True:
                        self._ready.set()
                    else:
                        self._fail("desk voice models are not ready")
                elif control["type"] == "error" and control.get("fatal"):
                    self._fail(str(control.get("message") or "fatal desk error"))
                self._put(TransportEvent("control", control))
        except Exception as exc:
            self._fail(f"receive failed: {exc}")

    def _put(self, event: TransportEvent) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self._fail("desk network event queue overflowed")

    def _fail(self, message: str) -> None:
        if not self._failure:
            self._failure = message
        self._closed.set()
        self._ready.set()
        with suppress(queue.Full):
            self.events.put_nowait(TransportEvent("disconnected", self._failure))
