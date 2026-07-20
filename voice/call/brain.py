"""Streaming brain.sock client with an explicit HTTP non-stream fallback."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_SOCKET = Path.home() / ".config" / "serena" / "brain.sock"
DEFAULT_DISCOVERY = Path.home() / ".config" / "serena" / "brain.json"


class BrainError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrainDoneMeta:
    elapsed: float | None = None
    turns: int | None = None
    session_turns: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: object) -> BrainDoneMeta:
        raw = payload if isinstance(payload, dict) else {}
        return cls(
            elapsed=_optional_number(raw.get("elapsed")),
            turns=_optional_int(raw.get("turns")),
            session_turns=_optional_int(raw.get("session_turns")),
            extra={
                key: value
                for key, value in raw.items()
                if key not in {"elapsed", "turns", "session_turns"}
            },
        )

    def as_dict(self) -> dict[str, Any]:
        values = {
            "elapsed": self.elapsed,
            "turns": self.turns,
            "session_turns": self.session_turns,
            **self.extra,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class BrainEvent:
    type: str
    request_id: str
    delta: str = ""
    say: str = ""
    meta: BrainDoneMeta | None = None
    backend: str = "brain.sock"


class BrainBackend(Protocol):
    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]: ...


async def _stream_ndjson(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    text: str,
    call_id: str,
    turn_id: str,
    request_id: str,
    timeout: float,
    backend: str,
    auth_token: str | None = None,
    journal: bool = True,
) -> AsyncIterator[BrainEvent]:
    request = {
        "type": "turn",
        "request_id": request_id,
        "protocol": "voice",
        "text": text,
        "stream": True,
        "call_id": call_id,
        "turn_id": turn_id,
    }
    if not journal:
        request["journal"] = False
    if auth_token:
        request["auth"] = auth_token
    writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    started = False
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                raise BrainError(f"{backend} closed before response.done")
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BrainError(f"{backend} returned malformed NDJSON") from exc
            if not isinstance(payload, dict):
                raise BrainError(f"{backend} response must be an object")
            if payload.get("request_id") != request_id:
                continue
            kind = payload.get("type")
            if kind == "error":
                raise BrainError(str(payload.get("error") or "brain turn failed"))
            if kind == "response.start":
                if started:
                    raise BrainError(f"{backend} sent duplicate response.start")
                started = True
                yield BrainEvent("start", request_id, backend=backend)
            elif kind == "response.delta":
                if not started:
                    raise BrainError(f"{backend} sent delta before response.start")
                delta = payload.get("delta")
                if not isinstance(delta, str):
                    raise BrainError(f"{backend} delta must be text")
                if delta:
                    yield BrainEvent(
                        "delta", request_id, delta=delta, backend=backend
                    )
            elif kind == "response.done":
                if not started:
                    raise BrainError(f"{backend} sent done before response.start")
                say = payload.get("say", "")
                if not isinstance(say, str):
                    raise BrainError(f"{backend} done say must be text")
                yield BrainEvent(
                    "done",
                    request_id,
                    say=say,
                    meta=BrainDoneMeta.from_payload(payload.get("meta")),
                    backend=backend,
                )
                return
            else:
                raise BrainError(f"{backend} returned unknown type {kind!r}")
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


class BrainSocketClient:
    def __init__(self, socket_path: Path = DEFAULT_SOCKET, timeout: float = 90.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        request_id = request_id or uuid.uuid4().hex
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        async for event in _stream_ndjson(
            reader,
            writer,
            text=text,
            call_id=call_id,
            turn_id=turn_id,
            request_id=request_id,
            timeout=self.timeout,
            backend="brain.sock",
            journal=journal,
        ):
            yield event


class BrainTcpClient:
    def __init__(
        self, host: str, port: int, auth_token: str, timeout: float = 90.0
    ) -> None:
        self.host = host
        self.port = int(port)
        self.auth_token = auth_token
        self.timeout = timeout

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        request_id = request_id or uuid.uuid4().hex
        reader, writer = await asyncio.open_connection(self.host, self.port)
        async for event in _stream_ndjson(
            reader,
            writer,
            text=text,
            call_id=call_id,
            turn_id=turn_id,
            request_id=request_id,
            timeout=self.timeout,
            backend="brain.tcp",
            auth_token=self.auth_token,
            journal=journal,
        ):
            yield event


class BrainDiscoveryClient:
    """Use brain.sock on Unix and the loopback stream port on Windows."""

    def __init__(
        self,
        discovery_path: Path = DEFAULT_DISCOVERY,
        socket_path: Path = DEFAULT_SOCKET,
        timeout: float = 90.0,
    ) -> None:
        self.discovery_path = Path(discovery_path)
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        request_id = request_id or uuid.uuid4().hex
        unix_error: Exception | None = None
        if os.name != "nt" and self.socket_path.exists():
            unix_yielded = False
            try:
                async for event in BrainSocketClient(
                    self.socket_path, self.timeout
                ).stream_turn(
                    text,
                    call_id=call_id,
                    turn_id=turn_id,
                    request_id=request_id,
                    journal=journal,
                ):
                    unix_yielded = True
                    yield event
                return
            except (OSError, ConnectionError, BrainError) as exc:
                if unix_yielded:
                    raise
                unix_error = exc
        try:
            info = json.loads(self.discovery_path.read_text(encoding="utf-8"))
            stream = info["stream"]
            if not isinstance(stream, dict) or stream.get("transport") != "tcp":
                raise ValueError("brain discovery has no TCP stream")
            host = str(stream.get("host") or "")
            if host != "127.0.0.1":
                raise ValueError("brain TCP stream must use literal loopback")
            port = int(stream["port"])
            auth_token = str(stream.get("token") or "")
            if not auth_token:
                raise ValueError("brain TCP stream has no authentication token")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if unix_error is not None:
                raise BrainError("brain streaming discovery is unavailable") from unix_error
            raise BrainError("brain streaming discovery is unavailable") from exc
        async for event in BrainTcpClient(
            host, port, auth_token, self.timeout
        ).stream_turn(
            text,
            call_id=call_id,
            turn_id=turn_id,
            request_id=request_id,
            journal=journal,
        ):
            yield event


class BrainHttpFallback:
    """The current brain.json POST /turn path, explicitly non-streaming."""

    backend = "brain.json-http-nonstream"

    def __init__(
        self, discovery_path: Path = DEFAULT_DISCOVERY, timeout: float = 90.0
    ) -> None:
        self.discovery_path = Path(discovery_path)
        self.timeout = timeout

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        request_id = request_id or uuid.uuid4().hex
        yield BrainEvent("start", request_id, backend=self.backend)
        payload = await self._post(text, call_id, turn_id, journal=journal)
        if not payload.get("ok"):
            raise BrainError(str(payload.get("error") or "brain HTTP turn failed"))
        say = payload.get("say", "")
        if not isinstance(say, str):
            raise BrainError("brain HTTP response say must be text")
        if say:
            yield BrainEvent(
                "delta", request_id, delta=say, backend=self.backend
            )
        meta = BrainDoneMeta.from_payload(
            {
                key: payload.get(key)
                for key in (
                    "elapsed",
                    "turns",
                    "session_turns",
                    "first_delta",
                    "session_id",
                    "billing_mode",
                    "daemon_pid",
                    "daemon_started",
                )
                if key in payload
            }
            | {"turn_id": turn_id}
        )
        yield BrainEvent(
            "done",
            request_id,
            say=say,
            meta=meta,
            backend=self.backend,
        )

    async def _post(
        self, text: str, call_id: str, turn_id: str, *, journal: bool = True
    ) -> dict[str, Any]:
        try:
            info = json.loads(self.discovery_path.read_text(encoding="utf-8"))
            port = int(info["port"])
            auth_token = str(info.get("token") or "")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise BrainError("brain.json discovery is unavailable") from exc
        request_payload = {
            "text": text,
            "protocol": "voice",
            "call_id": call_id,
            "turn_id": turn_id,
        }
        if not journal:
            request_payload["journal"] = False
        body = json.dumps(request_payload).encode()
        if not auth_token:
            raise BrainError("brain.json has no HTTP authentication token")
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=self.timeout,
            )
            request = (
                "POST /turn HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                f"Authorization: Bearer {auth_token}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1") + body
            writer.write(request)
            await writer.drain()
            status_line = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout
            )
            try:
                status = int(status_line.decode("latin-1").split(" ", 2)[1])
            except (IndexError, ValueError, UnicodeDecodeError) as exc:
                raise BrainError("brain HTTP returned a malformed status") from exc
            length: int | None = None
            while True:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=self.timeout
                )
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                if key.strip().lower() == "content-length":
                    length = int(value.strip())
            if length is None:
                raise BrainError("brain HTTP response has no content length")
            response_body = await asyncio.wait_for(
                reader.readexactly(length), timeout=self.timeout
            )
            result = json.loads(response_body)
            if status >= 400:
                raise BrainError(
                    str(result.get("error") or f"brain HTTP status {status}")
                )
        except (
            OSError,
            TimeoutError,
            ValueError,
            UnicodeDecodeError,
            asyncio.IncompleteReadError,
            json.JSONDecodeError,
        ) as exc:
            raise BrainError("brain HTTP non-stream fallback failed") from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(ConnectionError, OSError):
                    await writer.wait_closed()
        if not isinstance(result, dict):
            raise BrainError("brain HTTP response must be an object")
        return result


class BrainClient:
    """Prefer the discovered local stream and fall back before it responds."""

    def __init__(
        self,
        socket: BrainBackend | None = None,
        http: BrainBackend | None = None,
    ) -> None:
        self.socket = socket or BrainDiscoveryClient()
        self.http = http or BrainHttpFallback()

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        yielded = False
        try:
            async for event in self.socket.stream_turn(
                text,
                call_id=call_id,
                turn_id=turn_id,
                request_id=request_id,
                journal=journal,
            ):
                yielded = True
                yield event
            return
        except (OSError, ConnectionError, BrainError):
            if yielded:
                raise
        async for event in self.http.stream_turn(
            text,
            call_id=call_id,
            turn_id=turn_id,
            request_id=request_id,
            journal=journal,
        ):
            yield event


class DeterministicBrainStub:
    """Test and benchmark backend. It never makes a network request."""

    def __init__(self, reply: str = "i heard you.", chunk_chars: int = 7) -> None:
        self.reply = reply
        self.chunk_chars = max(1, chunk_chars)
        self.requests: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ) -> AsyncIterator[BrainEvent]:
        request_id = request_id or f"stub-{len(self.requests) + 1}"
        self.requests.append(
            {
                "type": "turn",
                "request_id": request_id,
                "protocol": "voice",
                "text": text,
                "stream": True,
                "call_id": call_id,
                "turn_id": turn_id,
            }
        )
        if not journal:
            self.requests[-1]["journal"] = False
        yield BrainEvent("start", request_id, backend="deterministic-stub")
        for offset in range(0, len(self.reply), self.chunk_chars):
            yield BrainEvent(
                "delta",
                request_id,
                delta=self.reply[offset : offset + self.chunk_chars],
                backend="deterministic-stub",
            )
        yield BrainEvent(
            "done",
            request_id,
            say=self.reply,
            meta=BrainDoneMeta(elapsed=0.0, turns=1),
            backend="deterministic-stub",
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
