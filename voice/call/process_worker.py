"""Killable local model process used by STT and TTS.

Threads cannot stop a running CTranslate2 or ONNX inference. A canceled call
would therefore leave the next push-to-talk turn queued behind obsolete work.
This small NDJSON subprocess boundary keeps the models warm in normal use and
lets cancellation terminate the active inference process immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any


class ModelProcessError(RuntimeError):
    pass


class CancellableModelProcess:
    def __init__(
        self,
        kind: str,
        config: dict[str, Any],
        *,
        python_executable: str | Path | None = None,
        network_disabled: bool = False,
        startup_timeout: float | None = None,
        inference_timeout: float | None = None,
    ) -> None:
        self.kind = kind
        self.config = dict(config)
        self.python_executable = str(
            Path(python_executable).expanduser().absolute()
            if python_executable is not None
            else Path(sys.executable).absolute()
        )
        self._direct_worker_script = python_executable is not None
        self.network_disabled = bool(network_disabled)
        self.network_isolation = "none"
        if self._direct_worker_script:
            configured = Path(self.python_executable)
            current = Path(sys.executable)
            if (
                configured.name.lower() not in {"python", "python3", "python.exe"}
                or not configured.is_file()
                or _file_sha256(configured.resolve())
                != _file_sha256(current.resolve())
            ):
                raise ValueError(
                    "dedicated model interpreter must be the same trusted Python binary"
                )
        configured_timeout = os.environ.get(
            "SERENA_CALL_MODEL_STARTUP_TIMEOUT", "90"
        )
        self.startup_timeout = max(
            1.0,
            float(startup_timeout if startup_timeout is not None else configured_timeout),
        )
        configured_inference_timeout = os.environ.get(
            "SERENA_CALL_MODEL_INFERENCE_TIMEOUT", "60"
        )
        self.inference_timeout = max(
            0.05,
            float(
                inference_timeout
                if inference_timeout is not None
                else configured_inference_timeout
            ),
        )
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Bubblewrap's --die-with-parent follows the Linux thread that spawned
        # it. A default asyncio executor can retire with one websocket loop and
        # kill an otherwise healthy warm model. This owner thread lives with
        # the backend, preserving both warm reuse and parent-death cleanup.
        self._model_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"serena-{kind}-model-owner",
        )
        self._process: subprocess.Popen[str] | None = None
        self._active_generation: int | None = None
        self._pending_generations: set[int] = set()
        self._cancelled_generations: set[int] = set()
        self._metadata: dict[str, Any] = {}

    @property
    def warmed(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.poll() is None

    @property
    def metadata(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._metadata)

    async def warm(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._model_executor, self._warm_sync)

    def warm_sync(self) -> dict[str, Any]:
        return self._model_executor.submit(self._warm_sync).result()

    async def request(
        self, payload: dict[str, Any], *, generation: int
    ) -> dict[str, Any]:
        with self._state_lock:
            self._pending_generations.add(generation)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._model_executor, self._request_sync, dict(payload), generation
        )

    def request_sync(
        self, payload: dict[str, Any], *, generation: int
    ) -> dict[str, Any]:
        with self._state_lock:
            self._pending_generations.add(generation)
        return self._model_executor.submit(
            self._request_sync, dict(payload), generation
        ).result()

    async def cancel(self, generation: int) -> bool:
        return await asyncio.to_thread(self._cancel_sync, generation)

    def close(self) -> None:
        with self._state_lock:
            process = self._process
            self._process = None
            self._active_generation = None
            self._pending_generations.clear()
            self._cancelled_generations.clear()
            self._metadata = {}
        if process is not None:
            _terminate_process(process)

    def _warm_sync(self) -> dict[str, Any]:
        with self._request_lock:
            process = self._ensure_process_sync()
            if process.poll() is not None:
                raise ModelProcessError(
                    f"{self.kind} model process exited during warmup"
                )
            return self.metadata

    def _ensure_process_sync(self) -> subprocess.Popen[str]:
        with self._state_lock:
            existing = self._process
            if existing is not None and existing.poll() is None:
                return existing
        if self._direct_worker_script and self.kind not in {"stt", "tts", "test"}:
            raise ModelProcessError(
                f"external model Python is unsupported for {self.kind} workers"
            )
        worker_target = (
            [str(Path(__file__).resolve())]
            if self._direct_worker_script
            else ["-m", "voice.call.process_worker"]
        )
        command = [
            self.python_executable,
            "-u",
            *worker_target,
            "--child",
            self.kind,
            "--config",
            json.dumps(self.config, separators=(",", ":")),
        ]
        repo_root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        if self.network_disabled:
            env.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "HF_HUB_DISABLE_TELEMETRY": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "SERENA_MODEL_NETWORK_DISABLED": "1",
                }
            )
            for key in tuple(env):
                if key.lower() in {
                    "all_proxy",
                    "http_proxy",
                    "https_proxy",
                    "ftp_proxy",
                }:
                    env.pop(key, None)
            if os.name == "posix" and sys.platform.startswith("linux"):
                bubblewrap = _trusted_bubblewrap()
                if not bubblewrap:
                    raise ModelProcessError(
                        "local model network isolation requires bubblewrap on Linux"
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
                self.network_isolation = "linux_network_namespace+python_socket_guard"
            else:
                self.network_isolation = "python_socket_guard"
            env["SERENA_MODEL_NETWORK_ISOLATION"] = self.network_isolation
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            cwd=str(repo_root),
            **_subprocess_group_options(),
        )
        with self._state_lock:
            self._process = process
            self._metadata = {}
        try:
            line = _readline_with_timeout(process, self.startup_timeout)
        except ModelProcessError:
            self._discard_process(process)
            raise
        if not line:
            code = process.poll()
            self._discard_process(process)
            raise ModelProcessError(
                f"{self.kind} model process failed to start (exit {code})"
            )
        try:
            ready = json.loads(line)
        except json.JSONDecodeError as exc:
            self._discard_process(process)
            raise ModelProcessError(
                f"{self.kind} model process returned malformed readiness"
            ) from exc
        if not ready.get("ok") or ready.get("event") != "ready":
            self._discard_process(process)
            raise ModelProcessError(
                str(ready.get("error") or f"{self.kind} model failed to warm")
            )
        metadata = ready.get("meta")
        with self._state_lock:
            if self._process is process:
                self._metadata = metadata if isinstance(metadata, dict) else {}
        return process

    def _request_sync(
        self, payload: dict[str, Any], generation: int
    ) -> dict[str, Any]:
        try:
            with self._request_lock:
                with self._state_lock:
                    if generation in self._cancelled_generations:
                        raise ModelProcessError(
                            f"{self.kind} model request was canceled before inference"
                        )
                process = self._ensure_process_sync()
                with self._state_lock:
                    if self._process is not process:
                        raise ModelProcessError(f"{self.kind} model was replaced")
                    if generation in self._cancelled_generations:
                        raise ModelProcessError(
                            f"{self.kind} model request was canceled before inference"
                        )
                    self._active_generation = generation
                payload["generation"] = generation
                if process.stdin is None or process.stdout is None:
                    raise ModelProcessError(
                        f"{self.kind} model process has no pipes"
                    )
                try:
                    process.stdin.write(
                        json.dumps(payload, separators=(",", ":")) + "\n"
                    )
                    process.stdin.flush()
                    line = _readline_with_timeout(
                        process,
                        self.inference_timeout,
                        phase="inference",
                    )
                except ModelProcessError:
                    self._discard_process(process)
                    raise
                except (BrokenPipeError, OSError, ValueError) as exc:
                    self._discard_process(process)
                    raise ModelProcessError(
                        f"{self.kind} model process stopped during inference"
                    ) from exc
                if not line:
                    self._discard_process(process)
                    raise ModelProcessError(
                        f"{self.kind} model process stopped during inference"
                    )
                try:
                    response = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._discard_process(process)
                    raise ModelProcessError(
                        f"{self.kind} model process returned malformed output"
                    ) from exc
                if not response.get("ok"):
                    raise ModelProcessError(
                        str(
                            response.get("error")
                            or f"{self.kind} inference failed"
                        )
                    )
                return response
        finally:
            with self._state_lock:
                if self._active_generation == generation:
                    self._active_generation = None
                self._pending_generations.discard(generation)
                self._cancelled_generations.discard(generation)

    def _cancel_sync(self, generation: int) -> bool:
        with self._state_lock:
            if (
                self._active_generation != generation
                and generation not in self._pending_generations
            ):
                return False
            self._cancelled_generations.add(generation)
            if self._active_generation != generation:
                return True
            process = self._process
            self._process = None
            self._active_generation = None
            self._metadata = {}
        if process is not None:
            _terminate_process(process)
        return process is not None

    def _discard_process(self, process: subprocess.Popen[str]) -> None:
        with self._state_lock:
            if self._process is process:
                self._process = None
                self._active_generation = None
                self._metadata = {}
        _terminate_process(process)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _subprocess_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _trusted_bubblewrap() -> str | None:
    """Return only a root-owned, non-writable system bubblewrap binary."""

    trusted_owners = {0}
    if os.environ.get("SERENA_CALL_TRUST_IDMAPPED_SYSTEM") == "1":
        # User systemd mount namespaces expose root-owned system files as the
        # overflow uid. The fixed path must still be non-writable here.
        trusted_owners.add(65534)
    for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap")):
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if metadata.st_uid not in trusted_owners or metadata.st_mode & 0o022:
            continue
        if metadata.st_uid != 0 and os.access(resolved, os.W_OK):
            continue
        return str(resolved)
    return None


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt" and process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            with suppress(OSError, ValueError):
                process.kill()
        else:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(OSError, ValueError, subprocess.TimeoutExpired):
            process.wait(timeout=3)
    except (OSError, ValueError):
        with suppress(OSError, ValueError):
            process.kill()
            process.wait(timeout=3)


def _install_network_guard() -> None:
    if os.environ.get("SERENA_MODEL_NETWORK_DISABLED") != "1":
        return
    original_socket = socket.socket

    class LocalOnlySocket(original_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family in {socket.AF_INET, socket.AF_INET6}:
                raise PermissionError("model worker network access is disabled")
            super().__init__(family, *args, **kwargs)

    def blocked_connection(*_args, **_kwargs):
        raise PermissionError("model worker network access is disabled")

    socket.socket = LocalOnlySocket
    socket.create_connection = blocked_connection


def _readline_with_timeout(
    process: subprocess.Popen[str],
    timeout: float,
    *,
    phase: str = "readiness",
) -> str:
    """Read a child response without letting a model or driver hang forever."""
    result: queue.Queue[str] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            line = process.stdout.readline() if process.stdout is not None else ""
        except (OSError, ValueError):
            line = ""
        with suppress(queue.Full):
            result.put_nowait(line)

    thread = threading.Thread(
        target=read,
        name=f"serena-{process.pid}-readiness",
        daemon=True,
    )
    thread.start()
    try:
        return result.get(timeout=timeout)
    except queue.Empty as exc:
        _terminate_process(process)
        raise ModelProcessError(
            f"model process {phase} timed out after {timeout:.1f}s"
        ) from exc
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _serve_stt(config: dict[str, Any]) -> None:
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(Path(config["model"])),
        device=str(config["device"]),
        compute_type=str(config["compute_type"]),
        local_files_only=True,
    )
    silence = np.zeros(int(config.get("sample_rate", 16_000)), dtype=np.float32)
    segments, _ = model.transcribe(
        silence,
        language="en",
        beam_size=1,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    list(segments)
    _send(
        {
            "ok": True,
            "event": "ready",
            "meta": {
                "device": config["device"],
                "compute_type": config["compute_type"],
            },
        }
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") != "transcribe":
                raise ValueError("unknown STT operation")
            pcm = base64.b64decode(request["pcm_b64"], validate=True)
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            audio /= 32768.0
            segments, _ = model.transcribe(
                audio,
                language="en",
                beam_size=1,
                condition_on_previous_text=False,
                vad_filter=False,
                hotwords=str(config.get("hotwords") or "") or None,
            )
            text = " ".join(
                segment.text.strip()
                for segment in segments
                if segment.text.strip()
            ).strip()
            _send({"ok": True, "text": text})
        except Exception as exc:
            _send({"ok": False, "error": str(exc)})


def _serve_tts(config: dict[str, Any]) -> None:
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    model_path = Path(str(config["model"])).expanduser().resolve(strict=True)
    voices_path = Path(str(config["voices"])).expanduser().resolve(strict=True)
    model_sha256 = _file_sha256(model_path)
    voices_sha256 = _file_sha256(voices_path)
    if model_sha256 != str(config.get("model_sha256") or ""):
        raise RuntimeError("Kokoro model hash does not match the parent attestation")
    if voices_sha256 != str(config.get("voices_sha256") or ""):
        raise RuntimeError("Kokoro voices hash does not match the parent attestation")
    requested = str(config.get("provider") or "CPUExecutionProvider")
    provider_required = bool(config.get("provider_required"))
    available = list(ort.get_available_providers())
    if provider_required and requested not in available:
        raise RuntimeError(
            f"required ONNX provider {requested} is unavailable; "
            f"available providers: {', '.join(available) or 'none'}"
        )
    provider = requested if requested in available else "CPUExecutionProvider"
    if provider not in available:
        raise RuntimeError("onnxruntime exposes no supported execution provider")
    previous = os.environ.get("ONNX_PROVIDER")
    os.environ["ONNX_PROVIDER"] = provider
    try:
        try:
            kokoro = Kokoro(str(model_path), str(voices_path))
        except Exception:
            if (
                provider_required
                or provider == "CPUExecutionProvider"
                or "CPUExecutionProvider" not in available
            ):
                raise
            provider = "CPUExecutionProvider"
            os.environ["ONNX_PROVIDER"] = provider
            kokoro = Kokoro(str(model_path), str(voices_path))
    finally:
        if previous is None:
            os.environ.pop("ONNX_PROVIDER", None)
        else:
            os.environ["ONNX_PROVIDER"] = previous
    voice = str(config["voice"])
    speed = float(config["speed"])
    kokoro.create("ready.", voice=voice, speed=speed, lang="en-us")
    _send(
        {
            "ok": True,
            "event": "ready",
            "meta": {
                "provider": provider,
                "requested_provider": requested,
                "provider_required": provider_required,
                "execution": "local",
                "model_source": "local_path",
                "model_path": str(model_path),
                "model_sha256": model_sha256,
                "voices_path": str(voices_path),
                "voices_sha256": voices_sha256,
                "python_executable": sys.executable,
                "python_binary_sha256": _file_sha256(Path(sys.executable).resolve()),
                "worker_pid": os.getpid(),
                "network_isolation": os.environ.get(
                    "SERENA_MODEL_NETWORK_ISOLATION", "none"
                ),
            },
        }
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") != "synthesise":
                raise ValueError("unknown TTS operation")
            samples, sample_rate = kokoro.create(
                str(request["text"]),
                voice=voice,
                speed=speed,
                lang="en-us",
            )
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
            _send(
                {
                    "ok": True,
                    "pcm_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
                    "sample_rate": int(sample_rate),
                }
            )
        except Exception as exc:
            _send({"ok": False, "error": str(exc)})


def _encode_endpoint_result(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "pcm_b64": base64.b64encode(result.pcm).decode("ascii"),
        "reason": result.reason,
        "sample_count": result.sample_count,
        "trailing_silence_samples": result.trailing_silence_samples,
    }


def _serve_vad(config: dict[str, Any]) -> None:
    from .endpoint import LocalSileroModel, SileroEndpointDetector
    from .protocol import MIC_SAMPLE_RATE

    detector = SileroEndpointDetector(
        LocalSileroModel(),
        threshold=float(config.get("threshold", 0.5)),
        silence_ms=int(config.get("silence_ms", 640)),
        pre_roll_ms=int(config.get("pre_roll_ms", 400)),
        max_utterance_ms=int(config.get("max_utterance_ms", 30_000)),
    )
    detector.warm()
    _send(
        {
            "ok": True,
            "event": "ready",
            "meta": {"provider": "CPUExecutionProvider"},
        }
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request.get("op")
            response: dict[str, Any] = {"ok": True}
            if operation == "process":
                pcm = base64.b64decode(request["pcm_b64"], validate=True)
                result = detector.process_pcm(pcm)
                # Per-frame endpoint state lets the parent run adaptive
                # end-of-utterance commits without extra round trips.
                response["speech_active"] = detector.speech_started
                response["trailing_ms"] = int(
                    detector.trailing_silence_samples * 1000 // MIC_SAMPLE_RATE
                )
            elif operation == "finish":
                reason = request.get("reason")
                result = detector.finish(
                    force=bool(request.get("force")),
                    reason=str(reason) if reason else None,
                )
            elif operation == "reset":
                detector.reset()
                result = None
            else:
                raise ValueError("unknown VAD operation")
            response["result"] = _encode_endpoint_result(result)
            _send(response)
        except Exception as exc:
            _send({"ok": False, "error": str(exc)})


def _serve_test(config: dict[str, Any]) -> None:
    child = None
    child_pid_file = str(config.get("child_pid_file") or "")
    if child_pid_file:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(child_pid_file).write_text(str(child.pid), encoding="ascii")
    network_blocked = None
    if config.get("probe_network"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            network_blocked = True
        else:
            network_blocked = False
            probe.close()
    time.sleep(float(config.get("ready_delay", 0)))
    _send(
        {
            "ok": True,
            "event": "ready",
            "meta": {
                "pid": os.getpid(),
                "child_pid": child.pid if child is not None else None,
                "network_blocked": network_blocked,
                "network_isolation": os.environ.get(
                    "SERENA_MODEL_NETWORK_ISOLATION", "none"
                ),
            },
        }
    )
    for line in sys.stdin:
        request = json.loads(line)
        time.sleep(float(request.get("delay", 0)))
        _send({"ok": True, "value": request.get("value")})


def _child(kind: str, config: dict[str, Any]) -> None:
    try:
        if kind == "stt":
            _serve_stt(config)
        elif kind == "tts":
            _serve_tts(config)
        elif kind == "vad":
            _serve_vad(config)
        elif kind == "test":
            _serve_test(config)
        else:
            raise ValueError(f"unknown model process kind {kind}")
    except Exception as exc:
        _send({"ok": False, "event": "ready", "error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--child", choices=("stt", "tts", "vad", "test"), required=True
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    _install_network_guard()
    _child(args.child, json.loads(args.config))


if __name__ == "__main__":
    main()
