from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

from voice.call.tts import (
    KokoroOnnxBackend,
    PocketTTSBackend,
    create_tts_backend,
)


def test_kokoro_paths_can_be_selected_from_environment(monkeypatch, tmp_path: Path):
    model = tmp_path / "gpu.onnx"
    voices = tmp_path / "voices.bin"
    monkeypatch.setenv("SERENA_CALL_KOKORO_MODEL", str(model))
    monkeypatch.setenv("SERENA_CALL_KOKORO_VOICES", str(voices))

    backend = KokoroOnnxBackend()

    assert backend.model_path == model
    assert backend.voices_path == voices


def test_kokoro_worker_python_can_be_selected_from_environment(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_TTS_PYTHON", sys.executable)

    backend = KokoroOnnxBackend()

    assert backend._worker.python_executable == str(Path(sys.executable).absolute())
    assert backend._worker._direct_worker_script is True


def test_explicit_kokoro_paths_override_environment(monkeypatch, tmp_path: Path):
    explicit_model = tmp_path / "explicit.onnx"
    explicit_voices = tmp_path / "explicit.bin"
    monkeypatch.setenv("SERENA_CALL_KOKORO_MODEL", str(tmp_path / "env.onnx"))
    monkeypatch.setenv("SERENA_CALL_KOKORO_VOICES", str(tmp_path / "env.bin"))

    backend = KokoroOnnxBackend(
        model_path=explicit_model,
        voices_path=explicit_voices,
    )

    assert backend.model_path == explicit_model
    assert backend.voices_path == explicit_voices


def test_kokoro_chunk_size_cannot_exceed_transport_limit():
    backend = KokoroOnnxBackend(chunk_ms=200)

    assert backend.chunk_ms == 50


def test_tts_backend_selection(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "pocket")
    assert isinstance(create_tts_backend(), PocketTTSBackend)
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "kokoro")
    assert isinstance(create_tts_backend(), KokoroOnnxBackend)


def test_pocket_backend_streams_pcm_and_reuses_warm_process():
    async def scenario() -> None:
        backend = PocketTTSBackend(
            python_executable=sys.executable,
            worker_module="voice.call.tests.pocket_worker_stub",
            startup_timeout=2,
            inference_timeout=2,
        )
        try:
            await asyncio.gather(backend.warm(), backend.warm())
            first_pid = backend.metadata["pid"]
            chunks = [
                chunk
                async for chunk in backend.stream("hello", generation=1)
            ]
            assert [len(chunk.pcm) for chunk in chunks] == [960, 960]
            assert all(chunk.sample_rate == 24_000 for chunk in chunks)
            assert backend.metadata["pid"] == first_pid
        finally:
            await backend.cancel(None)

    asyncio.run(scenario())


def test_pocket_backend_rechunks_worker_pcm_to_transport_limit():
    async def scenario() -> None:
        backend = PocketTTSBackend(
            python_executable=sys.executable,
            worker_module="voice.call.tests.pocket_worker_stub",
            startup_timeout=2,
            inference_timeout=2,
        )
        try:
            chunks = [
                chunk
                async for chunk in backend.stream("wide", generation=1)
            ]
            assert [len(chunk.pcm) for chunk in chunks] == [2_400, 2_400, 2_400]
            assert all(chunk.sample_rate == 24_000 for chunk in chunks)
        finally:
            await backend.cancel(None)

    asyncio.run(scenario())


def test_pocket_backend_serves_warmed_backchannel_without_live_inference():
    async def scenario() -> None:
        backend = PocketTTSBackend(
            python_executable=sys.executable,
            worker_module="voice.call.tests.pocket_worker_stub",
            startup_timeout=2,
            inference_timeout=2,
        )
        try:
            await backend.warm()
            chunks = [
                chunk
                async for chunk in backend.stream("yeah.", generation=1)
            ]
            assert [len(chunk.pcm) for chunk in chunks] == [960]
            assert backend.metadata["backchannel_cached"] is True
            assert len(backend.metadata["backchannel_pcm_sha256"]) == 64
        finally:
            await backend.cancel(None)

    asyncio.run(scenario())


def test_pocket_worker_survives_warmup_event_loop_thread_exit():
    backend = PocketTTSBackend(
        python_executable=sys.executable,
        worker_module="voice.call.tests.pocket_worker_stub",
        startup_timeout=2,
        inference_timeout=2,
    )
    warm_thread = threading.Thread(target=lambda: asyncio.run(backend.warm()))
    warm_thread.start()
    warm_thread.join(timeout=3)

    assert not warm_thread.is_alive()
    assert backend.warmed

    async def consume() -> None:
        try:
            chunks = [
                chunk
                async for chunk in backend.stream("hello", generation=1)
            ]
            assert chunks
        finally:
            await backend.cancel(None)

    asyncio.run(consume())


def test_pocket_cancel_kills_active_inference_and_next_turn_restarts():
    async def scenario() -> None:
        backend = PocketTTSBackend(
            python_executable=sys.executable,
            worker_module="voice.call.tests.pocket_worker_stub",
            startup_timeout=2,
            inference_timeout=10,
        )

        async def collect(text: str, generation: int):
            return [
                chunk
                async for chunk in backend.stream(text, generation=generation)
            ]

        try:
            await backend.warm()
            first_pid = backend.metadata["pid"]
            obsolete = asyncio.create_task(collect("hang", 1))
            for _ in range(100):
                if backend._active_generation == 1:
                    break
                await asyncio.sleep(0.01)
            assert backend._active_generation == 1
            await backend.cancel(1)
            assert await asyncio.wait_for(obsolete, timeout=2) == []

            for _ in range(100):
                rewarm_pid = backend.metadata.get("pid")
                if (
                    backend.warmed
                    and isinstance(rewarm_pid, int)
                    and rewarm_pid != first_pid
                ):
                    break
                await asyncio.sleep(0.01)
            assert backend.warmed
            assert backend.metadata["pid"] != first_pid

            backend.allow_generation(2)
            current = await collect("current", 2)
            assert len(current) == 2
            assert backend.metadata["pid"] != first_pid
        finally:
            await backend.cancel(None)

    asyncio.run(scenario())
