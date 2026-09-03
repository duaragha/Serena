"""Sample-count-based Silero endpoint detection for call audio."""

from __future__ import annotations

import array
import base64
import math
import os
import sys
import threading
from pathlib import Path
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from .process_worker import CancellableModelProcess
from .protocol import MIC_SAMPLE_RATE, ProtocolError

VAD_WINDOW_SAMPLES = 512


class VADModel(Protocol):
    def __call__(self, samples: Sequence[float], sample_rate: int) -> object: ...


@dataclass(frozen=True, slots=True)
class EndpointResult:
    pcm: bytes
    reason: str
    sample_count: int
    trailing_silence_samples: int

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000.0 / MIC_SAMPLE_RATE


# Silero's ONNX graph is stateful: it takes a 2x1x128 hidden state plus a
# short context of the previous window's tail, and returns the updated state.
_SILERO_STATE_SHAPE = (2, 1, 128)
_SILERO_CONTEXT_SAMPLES = 64  # 32 at 8kHz; this pipeline is 16kHz only


class LocalSileroModel:
    """Lazy adapter around the installed Silero model, without torch.

    No model is fetched here. The silero-vad package and its local model
    assets must already be installed on the host.

    The package ships the ONNX graph and a torch-based wrapper around it.
    Importing that wrapper costs ~200MB of torch runtime per process purely
    to allocate the state array and concatenate the context window, which is
    a bad trade for two long-lived children on a laptop. The graph is called
    here directly with numpy instead; the framing semantics below mirror
    silero_vad.utils_vad.OnnxWrapper exactly.
    """

    def __init__(self) -> None:
        self._session = None
        self._numpy = None
        self._state = None
        self._context = None
        self._load_lock = threading.Lock()

    def warm(self) -> None:
        self._ensure_loaded()
        self([0.0] * VAD_WINDOW_SAMPLES, MIC_SAMPLE_RATE)
        self.reset_states()

    @staticmethod
    def _model_path() -> Path:
        """Locate the shipped ONNX graph without importing the package.

        `import silero_vad` executes utils_vad, whose first line is `import
        torch` — which is the 200MB this class exists to avoid. find_spec
        resolves the install location without running any of it.
        """
        import importlib.util

        spec = importlib.util.find_spec("silero_vad")
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin:
            raise RuntimeError(
                "local Silero VAD is unavailable; install voice/call/requirements.txt"
            )
        candidate = Path(origin).resolve().parent / "data" / "silero_vad.onnx"
        if not candidate.is_file():
            raise RuntimeError(f"Silero VAD model is missing at {candidate}")
        return candidate

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._load_lock:
            if self._session is not None:
                return
            try:
                import numpy
                import onnxruntime
            except ImportError as exc:
                raise RuntimeError(
                    "local Silero VAD is unavailable; install voice/call/requirements.txt"
                ) from exc
            options = onnxruntime.SessionOptions()
            # one worker per child already; extra threads only add contention
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            self._numpy = numpy
            self._session = onnxruntime.InferenceSession(
                str(self._model_path()),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._reset_arrays()

    def _reset_arrays(self) -> None:
        numpy = self._numpy
        assert numpy is not None
        self._state = numpy.zeros(_SILERO_STATE_SHAPE, dtype=numpy.float32)
        self._context = numpy.zeros(
            (1, _SILERO_CONTEXT_SAMPLES), dtype=numpy.float32
        )

    def __call__(self, samples: Sequence[float], sample_rate: int) -> object:
        self._ensure_loaded()
        numpy = self._numpy
        assert numpy is not None and self._session is not None
        if sample_rate != MIC_SAMPLE_RATE:
            raise RuntimeError(
                f"Silero VAD expects {MIC_SAMPLE_RATE} Hz, got {sample_rate}"
            )
        window = numpy.asarray(samples, dtype=numpy.float32).reshape(1, -1)
        if window.shape[1] != VAD_WINDOW_SAMPLES:
            raise RuntimeError(
                f"Silero VAD expects {VAD_WINDOW_SAMPLES} samples per window, "
                f"got {window.shape[1]}"
            )
        padded = numpy.concatenate([self._context, window], axis=1)
        output, state = self._session.run(
            None,
            {
                "input": padded,
                "state": self._state,
                "sr": numpy.array(sample_rate, dtype="int64"),
            },
        )
        self._state = state
        self._context = padded[:, -_SILERO_CONTEXT_SAMPLES:]
        return float(output[0][0])

    def reset_states(self) -> None:
        if self._session is None:
            return
        self._reset_arrays()


class ProcessEndpointDetector:
    """Endpoint state and Silero inference isolated in one killable child."""

    def __init__(self, worker: CancellableModelProcess) -> None:
        self.worker = worker
        self._generation = 0
        self._generation_lock = threading.Lock()
        # Per-frame mirror of the child detector's state, refreshed by every
        # process_pcm call so the parent can run adaptive endpointing without
        # extra round trips. Consumers read these via getattr with defaults.
        self.speech_active = False
        self.trailing_ms = 0

    def warm(self) -> None:
        self.worker.warm_sync()

    def reset(self) -> None:
        self.speech_active = False
        self.trailing_ms = 0
        self._request({"op": "reset"})

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        if len(pcm) % 2:
            raise ProtocolError("PCM16 payload must contain whole samples")
        response = self._request(
            {
                "op": "process",
                "pcm_b64": base64.b64encode(bytes(pcm)).decode("ascii"),
            }
        )
        self.speech_active = bool(response.get("speech_active", False))
        self.trailing_ms = int(response.get("trailing_ms", 0))
        return self._decode_result(response.get("result"))

    def finish(self, *, force: bool = False, reason: str | None = None) -> EndpointResult | None:
        payload: dict = {"op": "finish", "force": bool(force)}
        if reason is not None:
            payload["reason"] = str(reason)
        self.speech_active = False
        self.trailing_ms = 0
        response = self._request(payload)
        return self._decode_result(response.get("result"))

    def close(self) -> None:
        self.worker.close()

    def _request(self, payload: dict) -> dict:
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
        return self.worker.request_sync(payload, generation=generation)

    @staticmethod
    def _decode_result(raw: object) -> EndpointResult | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RuntimeError("VAD process returned an invalid endpoint result")
        try:
            pcm = base64.b64decode(str(raw["pcm_b64"]), validate=True)
            result = EndpointResult(
                pcm=pcm,
                reason=str(raw["reason"]),
                sample_count=int(raw["sample_count"]),
                trailing_silence_samples=int(raw["trailing_silence_samples"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("VAD process returned an invalid endpoint result") from exc
        if result.sample_count != len(result.pcm) // 2:
            raise RuntimeError("VAD process returned an inconsistent sample count")
        return result


class SileroProcessPool:
    """Bounded pool of warm, state-isolated Silero endpoint processes."""

    name = "silero-vad"
    execution = "local"
    model_source = "offline_cache"

    def __init__(
        self,
        size: int | None = None,
        *,
        silence_ms: int | None = None,
        pre_roll_ms: int | None = None,
        max_utterance_ms: int | None = None,
        worker_factory: Callable[[], CancellableModelProcess] | None = None,
    ) -> None:
        configured = int(os.environ.get("SERENA_CALL_VAD_SLOTS", "1"))
        self.size = max(1, int(size if size is not None else configured))
        self.detector_config = {
            "threshold": float(os.environ.get("SERENA_CALL_VAD_THRESHOLD", "0.5")),
            "silence_ms": int(
                silence_ms
                if silence_ms is not None
                else os.environ.get("SERENA_CALL_VAD_SILENCE_MS", "30000")
            ),
            "pre_roll_ms": int(
                pre_roll_ms
                if pre_roll_ms is not None
                else os.environ.get("SERENA_CALL_VAD_PRE_ROLL_MS", "400")
            ),
            "max_utterance_ms": int(
                max_utterance_ms
                if max_utterance_ms is not None
                else os.environ.get("SERENA_CALL_MAX_UTTERANCE_MS", "30000")
            ),
        }
        inference_timeout = float(os.environ.get("SERENA_CALL_VAD_INFERENCE_TIMEOUT", "2"))
        factory = worker_factory or (
            lambda: CancellableModelProcess(
                "vad",
                self.detector_config,
                inference_timeout=inference_timeout,
                network_disabled=True,
            )
        )
        self._workers = [factory() for _ in range(self.size)]
        self._available = deque(self._workers)
        self._condition = threading.Condition()
        self._warm_lock = threading.Lock()
        self._warming = 0

    def endpoint(self) -> PooledSileroEndpoint:
        return PooledSileroEndpoint(self)

    def warm(self) -> None:
        """Prewarm every idle slot while keeping each slot single-owner."""
        with self._warm_lock:
            with self._condition:
                workers = list(self._available)
                self._available.clear()
                self._warming += len(workers)
            errors: list[Exception] = []
            try:
                for worker in workers:
                    try:
                        worker.warm_sync()
                    except Exception as exc:
                        errors.append(exc)
            finally:
                with self._condition:
                    self._available.extend(workers)
                    self._warming -= len(workers)
                    self._condition.notify_all()
            if errors:
                raise RuntimeError(f"Silero VAD warmup failed: {errors[0]}")

    def acquire(self) -> ProcessEndpointDetector:
        wait_timeout = float(os.environ.get("SERENA_CALL_VAD_SLOT_WAIT", "5"))
        with self._condition:
            ready = self._condition.wait_for(
                lambda: bool(self._available) or self._warming == 0,
                timeout=max(0.1, wait_timeout),
            )
            if not ready or not self._available:
                raise RuntimeError("local Silero VAD capacity is busy")
            worker = self._available.popleft()
        try:
            worker.warm_sync()
        except Exception:
            with self._condition:
                self._available.append(worker)
                self._condition.notify()
            raise
        return ProcessEndpointDetector(worker)

    def release(self, detector: ProcessEndpointDetector, *, reusable: bool) -> None:
        if reusable:
            try:
                detector.reset()
            except Exception:
                reusable = False
        if reusable:
            self._return_worker(detector.worker)
            return

        detector.close()
        with self._condition:
            self._warming += 1

        def restore() -> None:
            try:
                with suppress(Exception):
                    detector.worker.warm_sync()
            finally:
                with self._condition:
                    self._available.append(detector.worker)
                    self._warming -= 1
                    self._condition.notify_all()

        threading.Thread(
            target=restore,
            name="serena-vad-restore",
            daemon=True,
        ).start()

    def _return_worker(self, worker: CancellableModelProcess) -> None:
        with self._condition:
            self._available.append(worker)
            self._condition.notify()


class PooledSileroEndpoint:
    """Lazy per-call lease so an idle authenticated socket loads no model."""

    def __init__(self, pool: SileroProcessPool) -> None:
        self._pool = pool
        self._detector: ProcessEndpointDetector | None = None
        self._lock = threading.Lock()
        self._closed = False

    def warm(self) -> None:
        self._ensure_detector()

    def reset(self) -> None:
        with self._lock:
            detector = self._detector
        if detector is not None:
            detector.reset()

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        return self._ensure_detector().process_pcm(pcm)

    def finish(self, *, force: bool = False, reason: str | None = None) -> EndpointResult | None:
        return self._ensure_detector().finish(force=force, reason=reason)

    @property
    def speech_active(self) -> bool:
        with self._lock:
            detector = self._detector
        return bool(getattr(detector, "speech_active", False))

    @property
    def trailing_ms(self) -> int:
        with self._lock:
            detector = self._detector
        return int(getattr(detector, "trailing_ms", 0))

    def close(self, *, reusable: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            detector = self._detector
            self._detector = None
        if detector is not None:
            self._pool.release(detector, reusable=reusable)

    def _ensure_detector(self) -> ProcessEndpointDetector:
        with self._lock:
            if self._closed:
                raise RuntimeError("Silero VAD endpoint is closed")
            existing = self._detector
        if existing is not None:
            return existing
        acquired = self._pool.acquire()
        with self._lock:
            if self._closed:
                use = None
            elif self._detector is None:
                self._detector = acquired
                use = acquired
            else:
                use = self._detector
        if use is not acquired:
            self._pool.release(acquired, reusable=False)
        if use is None:
            raise RuntimeError("Silero VAD endpoint closed during warmup")
        return use


class SileroEndpointDetector:
    """Consumes arbitrary PCM batches and calls Silero in exact 512 windows."""

    def __init__(
        self,
        model: VADModel | None = None,
        *,
        threshold: float = 0.5,
        silence_ms: int = 640,
        pre_roll_ms: int = 400,
        max_utterance_ms: int = 30_000,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between zero and one")
        if silence_ms <= 0 or pre_roll_ms < 0 or max_utterance_ms <= 0:
            raise ValueError("endpoint durations are invalid")
        self._model = model or LocalSileroModel()
        self._threshold = threshold
        self._silence_samples = (
            math.ceil(silence_ms * MIC_SAMPLE_RATE / 1000 / VAD_WINDOW_SAMPLES) * VAD_WINDOW_SAMPLES
        )
        self._pre_roll_windows = math.ceil(
            pre_roll_ms * MIC_SAMPLE_RATE / 1000 / VAD_WINDOW_SAMPLES
        )
        self._max_samples = int(max_utterance_ms * MIC_SAMPLE_RATE / 1000)
        self._pending = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=self._pre_roll_windows)
        self._raw: deque[bytes] = deque()
        self._utterance: list[bytes] = []
        self._speech_started = False
        self._trailing_silence = 0

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    @property
    def trailing_silence_samples(self) -> int:
        return self._trailing_silence

    def warm(self) -> None:
        warm = getattr(self._model, "warm", None)
        if callable(warm):
            warm()

    def reset(self) -> None:
        self._pending.clear()
        self._pre_roll.clear()
        self._raw.clear()
        self._utterance.clear()
        self._speech_started = False
        self._trailing_silence = 0
        reset = getattr(self._model, "reset_states", None)
        if callable(reset):
            reset()

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        if len(pcm) % 2:
            raise ProtocolError("PCM16 payload must contain whole samples")
        self._pending.extend(pcm)
        window_bytes = VAD_WINDOW_SAMPLES * 2
        while len(self._pending) >= window_bytes:
            window = bytes(self._pending[:window_bytes])
            del self._pending[:window_bytes]
            result = self._process_window(window)
            if result is not None:
                return result
        return None

    def finish(self, *, force: bool = False, reason: str | None = None) -> EndpointResult | None:
        """Flush a PTT utterance.

        A forced PTT end preserves all received samples even if the VAD model
        never crossed threshold. Normal endpointing still requires speech.
        An explicit ``reason`` (the parent's adaptive early commit passes
        "silence") overrides the default "ptt_end" classification; without it
        behaviour is unchanged.
        """
        residual = bytes(self._pending)
        self._pending.clear()
        if self._speech_started:
            pcm = b"".join(self._utterance) + residual
            return self._emit(pcm, reason if reason else "ptt_end")
        if force:
            pcm = b"".join(self._raw) + residual
            if pcm:
                return self._emit(pcm, "ptt_end_forced")
        self.reset()
        return None

    def _process_window(self, window: bytes) -> EndpointResult | None:
        self._raw.append(window)
        while len(self._raw) * VAD_WINDOW_SAMPLES > self._max_samples:
            self._raw.popleft()
        probability = _as_probability(self._model(_normalise_pcm16(window), MIC_SAMPLE_RATE))
        is_speech = probability >= self._threshold

        if not self._speech_started:
            if is_speech:
                self._speech_started = True
                self._utterance.extend(self._pre_roll)
                self._utterance.append(window)
                self._pre_roll.clear()
            else:
                self._pre_roll.append(window)
            return None

        self._utterance.append(window)
        if is_speech:
            self._trailing_silence = 0
        else:
            self._trailing_silence += VAD_WINDOW_SAMPLES

        samples = len(self._utterance) * VAD_WINDOW_SAMPLES
        if self._trailing_silence >= self._silence_samples:
            return self._emit(b"".join(self._utterance), "silence")
        if samples >= self._max_samples:
            return self._emit(b"".join(self._utterance), "max_duration")
        return None

    def _emit(self, pcm: bytes, reason: str) -> EndpointResult:
        result = EndpointResult(
            pcm=pcm,
            reason=reason,
            sample_count=len(pcm) // 2,
            trailing_silence_samples=self._trailing_silence,
        )
        self.reset()
        return result


def _normalise_pcm16(pcm: bytes) -> tuple[float, ...]:
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return tuple(value / 32768.0 for value in values)


def _as_probability(value: object) -> float:
    item: Callable[[], object] | None = getattr(value, "item", None)
    if callable(item):
        value = item()
    try:
        probability = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Silero VAD returned a non-numeric probability") from exc
    if not math.isfinite(probability):
        raise RuntimeError("Silero VAD returned a non-finite probability")
    return probability
