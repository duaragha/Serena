"""Remote Pocket TTS backend: selection, fallback, streaming and cancel.

The engine itself lives in a container on the PC, so these tests stub the
HTTP layer. What is worth pinning here is the contract the call path relies
on: chunks arrive as they are produced, a cancel stops yielding, and a
container that is down makes her speak locally instead of going silent.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from voice.call.protocol import MAX_TTS_FRAME_MS
from voice.call.tts import (
    PocketTTSBackend,
    RemotePocketTTSBackend,
    SpeedAdjustedTTSBackend,
    create_tts_backend,
)


class _FakeResponse:
    def __init__(self, lines: list[dict]) -> None:
        self._lines = [json.dumps(line).encode() + b"\n" for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._lines[0] if self._lines else b"{}"


def _pcm_line(sample: int = 1234, count: int = 160) -> dict:
    pcm = sample.to_bytes(2, "little", signed=True) * count
    return {"ok": True, "chunk_b64": base64.b64encode(pcm).decode()}


def _remote(monkeypatch, lines: list[dict]) -> RemotePocketTTSBackend:
    backend = RemotePocketTTSBackend(base_url="https://example.invalid:8812")
    backend._warmed = True
    backend.sample_rate = 24_000
    monkeypatch.setattr(
        backend, "_open", lambda path, payload, timeout: _FakeResponse(lines)
    )
    return backend


def test_remote_backend_streams_chunks_as_they_arrive(monkeypatch):
    backend = _remote(
        monkeypatch,
        [_pcm_line(), _pcm_line(), {"ok": True, "final": True, "chunks": 2}],
    )

    async def scenario():
        return [chunk async for chunk in backend.stream("hello", generation=1)]

    chunks = asyncio.run(scenario())
    audio = [chunk for chunk in chunks if chunk.pcm]
    assert audio, "the caller must receive audio"
    assert all(chunk.sample_rate == 24_000 for chunk in audio)
    # an empty trailing frame is rejected by protocol.validate_audio_frame
    assert all(chunk.pcm for chunk in chunks)


def test_remote_backend_stops_yielding_once_cancelled(monkeypatch):
    backend = _remote(
        monkeypatch, [_pcm_line(), _pcm_line(), _pcm_line(), {"ok": True, "final": True}]
    )

    async def scenario():
        seen = []
        async for chunk in backend.stream("hello", generation=4):
            if chunk.pcm:
                seen.append(chunk)
                # barge in after the first chunk has been handed to the caller
                backend._cancelled.add(4)
        return seen

    assert len(asyncio.run(scenario())) == 1


def test_remote_backend_speaks_locally_when_the_container_errors(monkeypatch):
    backend = _remote(monkeypatch, [{"ok": False, "final": True, "error": "boom"}])
    spoken: list[str] = []

    async def _fallback(self, sentence, generation):
        spoken.append(sentence)
        yield type(self)  # never reached as a chunk; replaced below

    # a minimal stand-in for the local engine
    class _Local:
        async def stream(self, sentence, *, generation):
            spoken.append(sentence)
            yield __import__("voice.call.tts", fromlist=["PCMChunk"]).PCMChunk(
                pcm=b"\x01\x00", sample_rate=24_000, final=True
            )

        async def cancel(self, generation=None):
            return None

    backend._local = _Local()

    async def scenario():
        return [chunk async for chunk in backend.stream("hello", generation=2)]

    chunks = asyncio.run(scenario())
    assert spoken == ["hello"]
    assert any(chunk.pcm for chunk in chunks)


def test_remote_backend_raises_when_fallback_is_disabled(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_FALLBACK", "0")
    backend = _remote(monkeypatch, [{"ok": False, "final": True, "error": "boom"}])

    async def scenario():
        return [chunk async for chunk in backend.stream("hello", generation=3)]

    with pytest.raises(Exception, match="boom"):
        asyncio.run(scenario())


def test_factory_prefers_remote_when_the_container_answers(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "remote")
    monkeypatch.setattr(
        RemotePocketTTSBackend, "_health_sync", lambda self: {"worker_alive": True}
    )

    backend = create_tts_backend()

    assert isinstance(backend, SpeedAdjustedTTSBackend)
    assert isinstance(backend._inner, RemotePocketTTSBackend)


def test_factory_keeps_the_remote_backend_so_it_can_recover(monkeypatch):
    """A container that is down at startup must not pin us to local forever."""
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "remote")
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_ATTEMPTS", "1")

    def _dead(self):
        raise OSError("no route to host")

    monkeypatch.setattr(RemotePocketTTSBackend, "_health_sync", _dead)

    backend = create_tts_backend()

    assert isinstance(backend._inner, RemotePocketTTSBackend)


def test_factory_can_be_told_not_to_fall_back(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "remote")
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_FALLBACK", "0")
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_ATTEMPTS", "1")

    def _dead(self):
        raise OSError("no route to host")

    monkeypatch.setattr(RemotePocketTTSBackend, "_health_sync", _dead)

    with pytest.raises(RuntimeError, match="unreachable"):
        create_tts_backend()


def test_local_fallback_picks_the_pocket_interpreter(monkeypatch):
    """The pocket engine lives in its own venv, not the main one."""
    monkeypatch.delenv("SERENA_CALL_POCKET_PYTHON", raising=False)
    from voice.call.tts import _local_pocket_fallback

    backend = _local_pocket_fallback()

    assert isinstance(backend, PocketTTSBackend)
    assert ".venv-pocket" in backend.python_executable


def _frame_limit(sample_rate: int) -> int:
    return sample_rate * 2 * MAX_TTS_FRAME_MS // 1000


def test_streamed_frames_respect_the_transport_frame_limit(monkeypatch):
    """The container emits ~80ms chunks; the transport caps frames at 50ms.

    Yielding a container chunk whole made the very first real frame fail
    protocol validation, so she played the cached backchannel and then went
    silent. Every frame has to be re-sliced before it leaves this backend.
    """
    big = _pcm_line(count=4000)  # 8000 bytes, well over the 2400 byte limit
    backend = _remote(monkeypatch, [big, {"ok": True, "final": True}])

    async def scenario():
        return [chunk async for chunk in backend.stream("hello", generation=1)]

    chunks = asyncio.run(scenario())
    limit = _frame_limit(24_000)
    assert chunks, "expected audio frames"
    assert max(len(chunk.pcm) for chunk in chunks) <= limit
    # nothing is dropped in the re-slicing
    assert sum(len(chunk.pcm) for chunk in chunks) == 8000


def test_cached_backchannel_is_also_split(monkeypatch):
    """The warm-up backchannel is one long blob and needs the same treatment."""
    import base64 as _b64

    backend = RemotePocketTTSBackend(base_url="https://example.invalid:8812")
    pcm = (1234).to_bytes(2, "little", signed=True) * 4000
    monkeypatch.setattr(
        RemotePocketTTSBackend,
        "_health_sync",
        lambda self: {
            "worker_alive": True,
            "sample_rate": 24_000,
            "backchannel_pcm_b64": _b64.b64encode(pcm).decode(),
        },
    )
    monkeypatch.setenv("SERENA_CALL_POCKET_BACKCHANNEL", "yeah.")

    asyncio.run(backend.warm())

    frames = backend._cached_backchannel_chunks
    assert len(frames) > 1
    assert max(len(frame.pcm) for frame in frames) <= _frame_limit(24_000)
    assert sum(len(frame.pcm) for frame in frames) == len(pcm)


def test_local_delegate_is_retired_once_the_container_returns(monkeypatch):
    """A fallback at boot must not pin the local model for the whole session.

    The laptop is up before the tailnet is, so the first sentence after login
    routinely falls back. If the delegate survives that, ~700MB of model stays
    resident on the machine we just moved TTS off.
    """
    backend = RemotePocketTTSBackend(base_url="https://example.invalid:8812")
    killed = []

    class _Process:
        pass

    class _Local:
        _process = _Process()
        _model_executor = None

        def _discard_process(self, process):
            killed.append(process)

    backend._local = _Local()
    monkeypatch.setattr(
        RemotePocketTTSBackend,
        "_health_sync",
        lambda self: {"worker_alive": True, "sample_rate": 24_000},
    )

    asyncio.run(backend.warm())

    assert backend._local is None, "the local delegate must be dropped"
    assert len(killed) == 1, "its worker process must be terminated"


def test_retiring_without_a_local_delegate_is_a_no_op(monkeypatch):
    backend = RemotePocketTTSBackend(base_url="https://example.invalid:8812")
    monkeypatch.setattr(
        RemotePocketTTSBackend,
        "_health_sync",
        lambda self: {"worker_alive": True, "sample_rate": 24_000},
    )

    asyncio.run(backend.warm())

    assert backend._local is None


def test_a_stalled_remote_falls_back_before_he_notices(monkeypatch) -> None:
    """2026-08-21: her finished reply sat on screen in silence for close to a
    minute. The remote is not slow (0.10s p50 over 15 runs), it stalls, and the
    only deadline was the 60s inference timeout. A conversation cannot wait
    that long when a local engine answers in 0.03s."""
    from voice.call.tts import RemotePocketTTSBackend

    url = "https://example.invalid:8812"
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_FIRST_AUDIO_TIMEOUT", "2.0")
    assert RemotePocketTTSBackend(base_url=url).first_audio_timeout == 2.0

    # the default is short because the remote is normally 0.10s
    monkeypatch.delenv("SERENA_CALL_TTS_REMOTE_FIRST_AUDIO_TIMEOUT", raising=False)
    assert RemotePocketTTSBackend(base_url=url).first_audio_timeout == 1.5

    # and it can never be tuned down to something that trips on a healthy round trip
    monkeypatch.setenv("SERENA_CALL_TTS_REMOTE_FIRST_AUDIO_TIMEOUT", "0.01")
    assert RemotePocketTTSBackend(base_url=url).first_audio_timeout == 0.5


def test_the_deadline_only_guards_audio_he_has_not_heard_yet() -> None:
    """Once a chunk has played, cutting to a different engine mid-sentence
    would splice two voices together. The deadline applies before the first
    chunk only; after that the long inference timeout is correct."""
    from pathlib import Path as _P

    source = _P("voice/call/tts.py").read_text()
    loop = source.split("failure: str | None = None", 1)[1][:1400]
    assert "if emitted:" in loop
    assert "first_audio_timeout" in loop
    # the plain unbounded get is what runs once audio is already flowing
    assert loop.index("if emitted:") < loop.index("first_audio_timeout")
