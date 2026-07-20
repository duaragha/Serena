"""Swappable, local-only asynchronous TTS for call audio."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from .process_worker import (
    CancellableModelProcess,
    ModelProcessError,
    _file_sha256,
    _readline_with_timeout,
    _subprocess_group_options,
    _terminate_process,
    _trusted_bubblewrap,
)
from .protocol import MAX_TTS_FRAME_MS, TTS_SAMPLE_RATES


@dataclass(frozen=True, slots=True)
class PCMChunk:
    pcm: bytes
    sample_rate: int
    final: bool = False


class AsyncTTSBackend(ABC):
    """Adapter boundary shared by sentence and true-stream local engines."""

    name = "abstract"
    supports_true_stream = False

    @abstractmethod
    async def warm(self) -> None: ...

    @abstractmethod
    async def stream(
        self, sentence: str, *, generation: int
    ) -> AsyncIterator[PCMChunk]: ...

    @abstractmethod
    async def cancel(self, generation: int | None = None) -> None: ...

    def retire_generation(self, generation: int) -> None:
        return None


class TrueStreamingTTSBackend(AsyncTTSBackend):
    """Interface for a future installed Orpheus or CosyVoice adapter.

    This class intentionally has no concrete engine or dependency claim.
    """

    supports_true_stream = True


def requested_onnx_provider() -> str:
    return (
        os.environ.get("SERENA_CALL_ONNX_PROVIDER")
        or os.environ.get("ONNX_PROVIDER")
        or "CPUExecutionProvider"
    )


def select_onnx_provider(available: list[str] | tuple[str, ...]) -> str:
    requested = requested_onnx_provider()
    if requested in available:
        return requested
    if "CPUExecutionProvider" in available:
        return "CPUExecutionProvider"
    raise RuntimeError("onnxruntime exposes no supported execution provider")


class KokoroOnnxBackend(AsyncTTSBackend):
    """Kokoro ONNX, synthesized one sentence at a time and chunked as PCM."""

    name = "kokoro-onnx"
    execution = "local"
    model_source = "local_path"
    supports_true_stream = False

    def __init__(
        self,
        model_path: str | Path | None = None,
        voices_path: str | Path | None = None,
        *,
        python_executable: str | Path | None = None,
        voice: str | None = None,
        speed: float | None = None,
        chunk_ms: int = 40,
    ) -> None:
        models = Path(__file__).resolve().parents[1] / "models"
        self.model_path = Path(
            model_path
            or os.environ.get("SERENA_CALL_KOKORO_MODEL")
            or models / "kokoro-v1.0.int8.onnx"
        )
        self.voices_path = Path(
            voices_path
            or os.environ.get("SERENA_CALL_KOKORO_VOICES")
            or models / "voices-v1.0.bin"
        )
        self.voice = voice or os.environ.get("SERENA_CALL_VOICE", "af_heart")
        self.speed = speed or float(os.environ.get("SERENA_CALL_VOICE_SPEED", "1.2"))
        self.chunk_ms = max(10, min(chunk_ms, MAX_TTS_FRAME_MS))
        self.provider = ""
        self._provenance: dict[str, Any] = {}
        self._cancel_lock = threading.Lock()
        self._cancelled: set[int] = set()
        self._cancel_all = False
        self._worker = CancellableModelProcess(
            "tts",
            {
                "model": str(self.model_path),
                "voices": str(self.voices_path),
                "voice": self.voice,
                "speed": self.speed,
                "provider": requested_onnx_provider(),
                "provider_required": bool(
                    os.environ.get("SERENA_CALL_ONNX_PROVIDER")
                    or os.environ.get("ONNX_PROVIDER")
                ),
            },
            python_executable=(
                python_executable
                or os.environ.get("SERENA_CALL_TTS_PYTHON")
                or None
            ),
            network_disabled=True,
        )

    @property
    def warmed(self) -> bool:
        return self._worker.warmed

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._provenance)

    async def warm(self) -> None:
        if not self.model_path.is_file() or not self.voices_path.is_file():
            raise RuntimeError("Kokoro model assets are missing from voice/models")
        model_path = self.model_path.expanduser().resolve(strict=True)
        voices_path = self.voices_path.expanduser().resolve(strict=True)
        model_sha256 = _file_sha256(model_path)
        voices_sha256 = _file_sha256(voices_path)
        self._worker.config.update(
            {
                "model": str(model_path),
                "voices": str(voices_path),
                "model_sha256": model_sha256,
                "voices_sha256": voices_sha256,
            }
        )
        metadata = await self._worker.warm()
        expected = {
            "execution": "local",
            "model_source": "local_path",
            "model_path": str(model_path),
            "model_sha256": model_sha256,
            "voices_path": str(voices_path),
            "voices_sha256": voices_sha256,
            "python_executable": self._worker.python_executable,
            "python_binary_sha256": _file_sha256(
                Path(self._worker.python_executable).resolve(strict=True)
            ),
            "network_isolation": self._worker.network_isolation,
        }
        mismatches = [key for key, value in expected.items() if metadata.get(key) != value]
        worker_pid = metadata.get("worker_pid")
        if mismatches or not isinstance(worker_pid, int) or worker_pid <= 0:
            self._worker.close()
            raise RuntimeError(
                "Kokoro worker failed local provenance attestation: "
                + ", ".join(mismatches or ["worker_pid"])
            )
        self._provenance = dict(metadata)
        self.provider = str(metadata.get("provider") or "")

    async def stream(
        self, sentence: str, *, generation: int
    ) -> AsyncIterator[PCMChunk]:
        if not sentence.strip() or self._is_cancelled(generation):
            return
        response = await self._worker.request(
            {"op": "synthesise", "text": sentence},
            generation=generation,
        )
        if self._is_cancelled(generation):
            return
        pcm = base64.b64decode(str(response["pcm_b64"]), validate=True)
        sample_rate = int(response["sample_rate"])
        chunk_samples = max(1, sample_rate * self.chunk_ms // 1000)
        chunk_bytes = chunk_samples * 2
        for offset in range(0, len(pcm), chunk_bytes):
            if self._is_cancelled(generation):
                return
            end = min(offset + chunk_bytes, len(pcm))
            yield PCMChunk(
                pcm=pcm[offset:end],
                sample_rate=sample_rate,
                final=end == len(pcm),
            )
            await asyncio.sleep(0)

    async def cancel(self, generation: int | None = None) -> None:
        with self._cancel_lock:
            if generation is None:
                self._cancel_all = True
            else:
                self._cancelled.add(generation)
        if generation is None:
            await asyncio.to_thread(self._worker.close)
        else:
            await self._worker.cancel(generation)

    def allow_generation(self, generation: int) -> None:
        with self._cancel_lock:
            self._cancel_all = False
            self._cancelled.discard(generation)

    def retire_generation(self, generation: int) -> None:
        with self._cancel_lock:
            self._cancelled.discard(generation)

    def _is_cancelled(self, generation: int) -> bool:
        with self._cancel_lock:
            return self._cancel_all or generation in self._cancelled


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class PocketTTSBackend(AsyncTTSBackend):
    """True-streaming Pocket TTS in a dedicated, killable local process."""

    name = "pocket-tts"
    execution = "local"
    model_source = "offline_cache"
    supports_true_stream = True

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        worker_module: str = "voice.call.pocket_worker",
        voice: str | None = None,
        language: str | None = None,
        quantize: bool | None = None,
        threads: int | None = None,
        startup_timeout: float | None = None,
        inference_timeout: float | None = None,
    ) -> None:
        self.python_executable = str(
            python_executable
            or os.environ.get("SERENA_CALL_POCKET_PYTHON")
            or sys.executable
        )
        self.worker_module = worker_module
        self.voice = voice or os.environ.get("SERENA_CALL_POCKET_VOICE", "alba")
        self.language = language or os.environ.get(
            "SERENA_CALL_POCKET_LANGUAGE", "english"
        )
        self.quantize = (
            _env_truthy("SERENA_CALL_POCKET_QUANTIZE", True)
            if quantize is None
            else quantize
        )
        self.threads = max(
            1,
            int(
                threads
                if threads is not None
                else os.environ.get("SERENA_CALL_POCKET_THREADS", "2")
            ),
        )
        self.startup_timeout = max(
            1.0,
            float(
                startup_timeout
                if startup_timeout is not None
                else os.environ.get("SERENA_CALL_MODEL_STARTUP_TIMEOUT", "90")
            ),
        )
        self.inference_timeout = max(
            0.05,
            float(
                inference_timeout
                if inference_timeout is not None
                else os.environ.get("SERENA_CALL_MODEL_INFERENCE_TIMEOUT", "60")
            ),
        )
        self.provider = "CPUExecutionProvider"
        self.sample_rate = 24_000
        self.network_isolation = "none"
        self._stream_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._metadata: dict[str, Any] = {}
        self._cached_backchannel_text = ""
        self._cached_backchannel_chunks: tuple[PCMChunk, ...] = ()
        self._active_generation: int | None = None
        self._pending_generations: set[int] = set()
        self._cancelled: set[int] = set()
        self._cancel_all = False
        self._model_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="serena-pocket-model",
        )
        self._rewarm_future: Future[dict[str, Any]] | None = None

    @property
    def warmed(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.poll() is None

    @property
    def metadata(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._metadata)

    async def warm(self) -> None:
        metadata = await asyncio.get_running_loop().run_in_executor(
            self._model_executor,
            self._ensure_process_sync,
        )
        self.provider = str(metadata.get("provider") or "CPUExecutionProvider")
        self.sample_rate = int(metadata.get("sample_rate") or 24_000)

    async def stream(
        self, sentence: str, *, generation: int
    ) -> AsyncIterator[PCMChunk]:
        if not sentence.strip() or self._is_cancelled(generation):
            return
        with self._state_lock:
            cached = (
                self._cached_backchannel_chunks
                if sentence.strip() == self._cached_backchannel_text
                else ()
            )
        if cached:
            for chunk in cached:
                if self._is_cancelled(generation):
                    return
                yield chunk
            return
        with self._state_lock:
            self._pending_generations.add(generation)
        try:
            async with self._stream_lock:
                if self._is_cancelled(generation):
                    return
                metadata = await asyncio.get_running_loop().run_in_executor(
                    self._model_executor,
                    self._ensure_process_sync,
                )
                self.provider = str(
                    metadata.get("provider") or "CPUExecutionProvider"
                )
                if self._is_cancelled(generation):
                    return
                process = self._current_process()
                with self._state_lock:
                    if self._process is not process:
                        raise ModelProcessError("Pocket TTS process was replaced")
                    self._active_generation = generation
                await asyncio.get_running_loop().run_in_executor(
                    self._model_executor,
                    self._write_request_sync,
                    process,
                    {
                        "op": "synthesise",
                        "text": sentence,
                        "generation": generation,
                    },
                )
                while True:
                    try:
                        line = await asyncio.get_running_loop().run_in_executor(
                            self._model_executor,
                            partial(
                                _readline_with_timeout,
                                process,
                                self.inference_timeout,
                                phase="inference",
                            ),
                        )
                    except ModelProcessError:
                        self._discard_process(process)
                        if self._is_cancelled(generation):
                            return
                        raise
                    if not line:
                        self._discard_process(process)
                        if self._is_cancelled(generation):
                            return
                        raise ModelProcessError(
                            "Pocket TTS process stopped during inference"
                        )
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError as exc:
                        self._discard_process(process)
                        raise ModelProcessError(
                            "Pocket TTS process returned malformed output"
                        ) from exc
                    if not response.get("ok"):
                        raise ModelProcessError(
                            str(response.get("error") or "Pocket TTS inference failed")
                        )
                    event = response.get("event")
                    if event == "done":
                        return
                    if event != "chunk":
                        self._discard_process(process)
                        raise ModelProcessError(
                            f"Pocket TTS returned unknown event {event!r}"
                        )
                    if self._is_cancelled(generation):
                        return
                    try:
                        pcm = base64.b64decode(
                            str(response["pcm_b64"]), validate=True
                        )
                        sample_rate = int(response["sample_rate"])
                    except (KeyError, TypeError, ValueError) as exc:
                        self._discard_process(process)
                        raise ModelProcessError(
                            "Pocket TTS returned an invalid PCM chunk"
                        ) from exc
                    if (
                        sample_rate not in TTS_SAMPLE_RATES
                        or not pcm
                        or len(pcm) % 2
                    ):
                        self._discard_process(process)
                        raise ModelProcessError(
                            "Pocket TTS returned empty or partial PCM16"
                        )
                    if len(pcm) > sample_rate:
                        self._discard_process(process)
                        raise ModelProcessError(
                            "Pocket TTS worker chunk exceeds 500 ms"
                        )
                    transport_bytes = (
                        sample_rate * 2 * MAX_TTS_FRAME_MS // 1_000
                    )
                    for offset in range(0, len(pcm), transport_bytes):
                        if self._is_cancelled(generation):
                            return
                        yield PCMChunk(
                            pcm=pcm[offset : offset + transport_bytes],
                            sample_rate=sample_rate,
                        )
        finally:
            with self._state_lock:
                if self._active_generation == generation:
                    self._active_generation = None
                self._pending_generations.discard(generation)

    async def cancel(self, generation: int | None = None) -> None:
        process: subprocess.Popen[str] | None = None
        rewarm: Future[dict[str, Any]] | None = None
        with self._state_lock:
            if generation is None:
                self._cancel_all = True
                process = self._process
                self._process = None
                self._active_generation = None
                self._metadata = {}
                rewarm = self._rewarm_future
            else:
                self._cancelled.add(generation)
                if self._active_generation == generation:
                    process = self._process
                    self._process = None
                    self._active_generation = None
                    self._metadata = {}
        if process is not None:
            await asyncio.to_thread(_terminate_process, process)
        if generation is None:
            if rewarm is not None:
                with suppress(Exception):
                    await asyncio.wrap_future(rewarm)
            with self._state_lock:
                late_process = self._process
                self._process = None
                self._active_generation = None
                self._metadata = {}
            if late_process is not None:
                await asyncio.to_thread(_terminate_process, late_process)
            await asyncio.to_thread(
                self._model_executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        elif process is not None:
            self._schedule_rewarm()

    def allow_generation(self, generation: int) -> None:
        with self._state_lock:
            self._cancel_all = False
            self._cancelled.discard(generation)

    def retire_generation(self, generation: int) -> None:
        with self._state_lock:
            self._cancelled.discard(generation)

    def _is_cancelled(self, generation: int) -> bool:
        with self._state_lock:
            return self._cancel_all or generation in self._cancelled

    def _current_process(self) -> subprocess.Popen[str]:
        with self._state_lock:
            process = self._process
        if process is None or process.poll() is not None:
            raise ModelProcessError("Pocket TTS process is unavailable")
        return process

    def _ensure_process_sync(self) -> dict[str, Any]:
        with self._start_lock:
            return self._ensure_process_locked_sync()

    def _ensure_process_locked_sync(self) -> dict[str, Any]:
        with self._state_lock:
            if self._cancel_all:
                raise ModelProcessError("Pocket TTS process is shutting down")
            existing = self._process
            if existing is not None and existing.poll() is None:
                return dict(self._metadata)
        config = {
            "voice": self.voice,
            "language": self.language,
            "quantize": self.quantize,
            "threads": self.threads,
            "backchannel_text": os.environ.get(
                "SERENA_CALL_BACKCHANNEL_TEXT", "yeah."
            ).strip(),
        }
        repo_root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        env["HF_HUB_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["SERENA_MODEL_NETWORK_DISABLED"] = "1"
        for key in tuple(env):
            if key.lower() in {
                "all_proxy",
                "http_proxy",
                "https_proxy",
                "ftp_proxy",
            }:
                env.pop(key, None)
        command = [
            self.python_executable,
            "-u",
            "-m",
            self.worker_module,
            "--config",
            json.dumps(config, separators=(",", ":")),
        ]
        if os.name == "posix" and sys.platform.startswith("linux"):
            bubblewrap = _trusted_bubblewrap()
            if not bubblewrap:
                raise ModelProcessError(
                    "local Pocket TTS network isolation requires bubblewrap on Linux"
                )
            command = [
                bubblewrap,
                "--unshare-net",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--proc",
                "/proc",
                "--",
                *command,
            ]
            self.network_isolation = (
                "linux_network_namespace+python_socket_guard"
            )
        else:
            self.network_isolation = "python_socket_guard"
        env["SERENA_MODEL_NETWORK_ISOLATION"] = self.network_isolation
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(repo_root),
                env=env,
                **_subprocess_group_options(),
            )
        except OSError as exc:
            raise ModelProcessError(
                f"Pocket TTS interpreter could not start: {exc}"
            ) from exc
        with self._state_lock:
            self._process = process
            self._metadata = {}
        try:
            line = _readline_with_timeout(process, self.startup_timeout)
            if not line:
                raise ModelProcessError("Pocket TTS process stopped during warmup")
            ready = json.loads(line)
            if not ready.get("ok") or ready.get("event") != "ready":
                raise ModelProcessError(
                    str(ready.get("error") or "Pocket TTS failed to warm")
                )
            metadata = ready.get("meta")
            if not isinstance(metadata, dict):
                metadata = {}
            cached_text = str(metadata.pop("backchannel_text", "")).strip()
            cached_pcm_b64 = metadata.pop("backchannel_pcm_b64", "")
            try:
                cached_pcm = base64.b64decode(str(cached_pcm_b64), validate=True)
                cached_rate = int(metadata.pop("backchannel_sample_rate", 0))
            except (TypeError, ValueError) as exc:
                self._discard_process(process)
                raise ModelProcessError(
                    "Pocket TTS returned an invalid cached backchannel"
                ) from exc
            if (
                not cached_text
                or not cached_pcm
                or len(cached_pcm) % 2
                or cached_rate not in TTS_SAMPLE_RATES
            ):
                self._discard_process(process)
                raise ModelProcessError(
                    "Pocket TTS returned no valid cached backchannel"
                )
            transport_bytes = cached_rate * 2 * MAX_TTS_FRAME_MS // 1_000
            cached_chunks = tuple(
                PCMChunk(cached_pcm[offset : offset + transport_bytes], cached_rate)
                for offset in range(0, len(cached_pcm), transport_bytes)
            )
            python_path = Path(self.python_executable).expanduser().resolve(strict=True)
            metadata.update(
                {
                    "execution": "local",
                    "model_source": "offline_cache",
                    "network_isolation": self.network_isolation,
                    "python_executable": str(python_path),
                    "python_binary_sha256": _file_sha256(python_path),
                    "worker_pid": int(metadata.get("pid") or process.pid),
                    "backchannel_cached": True,
                    "backchannel_pcm_sha256": hashlib.sha256(cached_pcm).hexdigest(),
                }
            )
        except (json.JSONDecodeError, ModelProcessError):
            self._discard_process(process)
            raise
        with self._state_lock:
            if self._process is process:
                self._metadata = dict(metadata)
                self._cached_backchannel_text = cached_text
                self._cached_backchannel_chunks = cached_chunks
        return dict(metadata)

    def _schedule_rewarm(self) -> None:
        with self._state_lock:
            if self._cancel_all:
                return
            existing = self._rewarm_future
            if existing is not None and not existing.done():
                return
            self._rewarm_future = self._model_executor.submit(
                self._ensure_process_sync
            )

    def _write_request_sync(
        self, process: subprocess.Popen[str], payload: dict[str, Any]
    ) -> None:
        with self._write_lock:
            if process.stdin is None:
                raise ModelProcessError("Pocket TTS process has no input pipe")
            try:
                process.stdin.write(
                    json.dumps(payload, separators=(",", ":")) + "\n"
                )
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._discard_process(process)
                raise ModelProcessError(
                    "Pocket TTS process stopped before inference"
                ) from exc

    def _discard_process(self, process: subprocess.Popen[str]) -> None:
        with self._state_lock:
            if self._process is process:
                self._process = None
                self._active_generation = None
                self._metadata = {}
        _terminate_process(process)


def create_tts_backend() -> AsyncTTSBackend:
    backend = os.environ.get("SERENA_CALL_TTS_BACKEND", "kokoro").strip().lower()
    if backend == "pocket":
        return PocketTTSBackend()
    if backend == "kokoro":
        return KokoroOnnxBackend()
    raise RuntimeError(f"unsupported Serena call TTS backend {backend!r}")


class DeterministicTTSStub(AsyncTTSBackend):
    name = "deterministic-stub"

    def __init__(self, *, sample_rate: int = 24_000, samples_per_sentence: int = 960):
        self.sample_rate = sample_rate
        self.samples_per_sentence = samples_per_sentence
        self.cancelled: set[int] = set()
        self.cancel_all = False
        self.sentences: list[tuple[int, str]] = []

    async def warm(self) -> None:
        return None

    async def stream(
        self, sentence: str, *, generation: int
    ) -> AsyncIterator[PCMChunk]:
        if self.cancel_all or generation in self.cancelled:
            return
        self.sentences.append((generation, sentence))
        half = max(1, self.samples_per_sentence // 2)
        chunks = [b"\x01\x00" * half]
        remaining = self.samples_per_sentence - half
        if remaining:
            chunks.append(b"\x02\x00" * remaining)
        for index, pcm in enumerate(chunks):
            if self.cancel_all or generation in self.cancelled:
                return
            yield PCMChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                final=index == len(chunks) - 1,
            )

    async def cancel(self, generation: int | None = None) -> None:
        if generation is None:
            self.cancel_all = True
        else:
            self.cancelled.add(generation)

    def allow_generation(self, generation: int) -> None:
        self.cancel_all = False
        self.cancelled.discard(generation)

    def retire_generation(self, generation: int) -> None:
        self.cancelled.discard(generation)
