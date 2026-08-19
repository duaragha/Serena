"""VAD to STT to brain to sentence TTS orchestration for /ws/call."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.billing import present_metered_auth_env
from core.work_jobs import WorkJobEvent

from .brain import BrainBackend, BrainClient
from .endpoint import EndpointResult, SileroProcessPool
from .greeting import CallGreetingState
from .hello_state import CallHelloState
from .protocol import (
    FLAG_FINAL,
    MAX_TTS_FRAME_MS,
    AudioFrame,
    AudioHeader,
    AudioKind,
    ProtocolError,
    SequenceTracker,
    encode_control,
    parse_audio_frame,
    parse_control,
)
from .sentences import IncrementalSentenceSplitter
from .spoken_text import prepare_spoken_text
from .stt import FasterWhisperWorker
from .tailnet import TailnetPathMeasurement, normalize_tailnet_peer, probe_tailscale_path
from .tasking import (
    CallTaskDispatcher,
    get_default_task_dispatcher,
    parse_code_panel_intent,
)
from .telemetry import DEFAULT_METRICS_PATH, CallTelemetry
from .thinking_sounds import ThinkingClip, ThinkingSoundPool, ensure_thinking_clips
from .tts import AsyncTTSBackend, PCMChunk, create_tts_backend
from .turn_taking import UNKNOWN, AdaptiveEouPolicy, assess_completeness

_SESSION_IDS = itertools.count(1)
_RTT_SAMPLE_EXPIRY_NS = 30 * 1_000_000_000
_MAX_PENDING_RTT_SAMPLES = 64
_ADAPTIVE_PRE_ROLL_SAMPLES = 6_400  # ~400ms of 16kHz mic audio mirrored pre-speech
DEFAULT_DESK_METRICS_PATH = Path.home() / ".config" / "serena" / "desk_metrics.jsonl"
DEFAULT_DESK_GREETING_STATE = Path.home() / ".config" / "serena" / "desk_greeting_state.json"


_BARE_NAME_CALL = re.compile(
    r"^\s*(?:hey\s+|hi\s+|okay\s+|ok\s+)?"
    r"(?:serena|serina|sirena|sarena|sarina|sereena)"
    r"\s*[.!,?]*\s*$",
    re.IGNORECASE,
)


def _is_bare_name_call(text: str) -> bool:
    """True when the whole utterance is just her name being called."""
    return bool(_BARE_NAME_CALL.match(text or ""))


def _reply_splitter(tts: object) -> IncrementalSentenceSplitter:
    """Splitter for brain-delta segmentation, shared by all TTS backends.

    True-streaming backends used to override the first-clause knobs to
    260/260, which silenced the fast first clause entirely: nothing was
    emitted until a full sentence boundary or a 260-char hard cut, so first
    audio could wait out an entire long opening sentence. The splitter's
    tested defaults (12/56 with clause-boundary preference) already preserve
    natural phrasing, and continuous synthesis makes an early first clause
    cheaper for true-stream backends, not costlier. Env knobs stay for
    tuning phrasing without a deploy.
    """
    del tts  # same segmentation policy for every backend today
    first = int(os.environ.get("SERENA_CALL_FIRST_CLAUSE_CHARS", "12"))
    hard = int(os.environ.get("SERENA_CALL_FIRST_CLAUSE_HARD_CHARS", "56"))
    return IncrementalSentenceSplitter(
        first_clause_chars=first,
        first_clause_hard_chars=hard,
    )


class STTBackend(Protocol):
    async def warm(self) -> None: ...

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        generation: int = 0,
    ) -> str: ...


class EndpointDetector(Protocol):
    def warm(self) -> None: ...

    def reset(self) -> None: ...

    def process_pcm(self, pcm: bytes) -> EndpointResult | None: ...

    def finish(
        self,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> EndpointResult | None: ...


@dataclass(frozen=True, slots=True)
class _BinaryInput:
    generation: int
    frame: AudioFrame


@dataclass(frozen=True, slots=True)
class _ResetEndpoint:
    generation: int


@dataclass(frozen=True, slots=True)
class _FlushEndpoint:
    generation: int
    force: bool


@dataclass(frozen=True, slots=True)
class _TTSRequest:
    text: str
    kind: str
    # A pre-rendered thinking sound carries its own audio.  Synthesising a
    # filler live would spend TTS latency at the exact moment it exists to
    # cover, so the worker plays these bytes instead of calling the engine.
    clip: ThinkingClip | None = None


async def _prerendered_chunks(clip: ThinkingClip):
    """Yield a stored clip in the same frame sizes the engine streams."""

    # Compute whole samples before converting to bytes. At 22.05 kHz the old
    # byte-first formula produced 2,205-byte PCM16 frames, which the protocol
    # correctly rejected and took the real answer down with the filler.
    frame_samples = max(1, clip.sample_rate * MAX_TTS_FRAME_MS // 1_000)
    frame_bytes = frame_samples * 2
    for offset in range(0, len(clip.pcm), frame_bytes):
        yield PCMChunk(clip.pcm[offset : offset + frame_bytes], clip.sample_rate)


@dataclass(slots=True)
class _PendingRttProbe:
    issued_ns: int
    path_task: asyncio.Task[TailnetPathMeasurement] | None
    pong_finished: asyncio.Event = field(default_factory=asyncio.Event)
    pong_sent: bool = False


class CallRuntime:
    """Long-lived model workers shared by reconnecting call sockets."""

    def __init__(
        self,
        *,
        stt: STTBackend,
        brain: BrainBackend,
        tts: AsyncTTSBackend,
        endpoint_factory: Callable[[], EndpointDetector],
        endpoint_warmer: object | None = None,
        greeting_state: CallGreetingState | None = None,
        hello_state: CallHelloState | None = None,
        task_dispatcher: CallTaskDispatcher | None = None,
        metrics_path: Path = DEFAULT_METRICS_PATH,
        warm_timeout: float | None = None,
        eou_policy: AdaptiveEouPolicy | None = None,
    ) -> None:
        self.stt = stt
        self.brain = brain
        self.tts = tts
        self.endpoint_factory = endpoint_factory
        self.endpoint_warmer = endpoint_warmer
        self.eou_policy = eou_policy
        self.greeting_state = greeting_state or CallGreetingState()
        self.hello_state = hello_state or CallHelloState()
        self.task_dispatcher = task_dispatcher
        self.metrics_path = Path(metrics_path)
        self.warm_timeout = max(
            5.0,
            float(
                warm_timeout
                if warm_timeout is not None
                else os.environ.get("SERENA_CALL_WARM_TIMEOUT", "120")
            ),
        )
        self._warm_lock = threading.Lock()
        self._warm_started = False
        self._warm_done = threading.Event()
        self._warm_thread: threading.Thread | None = None
        self._warm_status: dict[str, str] = {
            "stt": "pending",
            "tts": "pending",
            "vad": "pending" if endpoint_warmer is not None else "ready",
        }

    def start_warmup(self) -> None:
        """Start model warmup on a loop that outlives every call socket."""
        with self._warm_lock:
            if self._warm_started:
                return
            self._warm_started = True
            self._warm_thread = threading.Thread(
                target=self._warm_thread_main,
                name="serena-call-runtime-warm",
                daemon=True,
            )
            self._warm_thread.start()

    def _warm_thread_main(self) -> None:
        try:
            asyncio.run(self._run_warmup())
        except BaseException as exc:
            with self._warm_lock:
                for name, value in self._warm_status.items():
                    if value == "pending":
                        self._warm_status[name] = f"error: warmup aborted: {exc}"
            self._warm_done.set()

    async def warm(self) -> dict[str, str]:
        self.start_warmup()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.warm_timeout + 5.0
        while not self._warm_done.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("call runtime warmup did not finish")
            await asyncio.sleep(min(0.01, remaining))
        return self.status

    async def _run_warmup(self) -> None:
        async def run_async(name: str, fn: Callable[[], Any]) -> None:
            try:
                result = fn()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._warm_status[name] = f"error: {exc}"
            else:
                self._warm_status[name] = "ready"

        async def warm_tts() -> None:
            await self.tts.warm()
            if not str(getattr(self.tts, "name", "")).startswith("pocket-tts"):
                return
            # The deployed host had only mic-off.raw, so the feature stayed
            # permanently silent despite passing synthetic tests. Provision
            # the tiny clips from the already-warm local Pocket voice. Failure
            # remains non-fatal because a missing filler must never lose a turn.
            try:
                await asyncio.wait_for(
                    ensure_thinking_clips(backend=self.tts),
                    timeout=min(30.0, self.warm_timeout),
                )
            except Exception:
                return

        try:
            tasks = [
                run_async("stt", self.stt.warm),
                run_async("tts", warm_tts),
            ]
            if self.endpoint_warmer is not None:
                tasks.append(
                    run_async(
                        "vad",
                        lambda: asyncio.to_thread(self.endpoint_warmer.warm),
                    )
                )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=self.warm_timeout,
                )
            except TimeoutError:
                for name, value in self._warm_status.items():
                    if value == "pending":
                        self._warm_status[name] = (
                            f"error: warmup timed out after {self.warm_timeout:.1f}s"
                        )
                for backend in (self.stt, self.tts):
                    close = getattr(backend, "close", None)
                    if callable(close):
                        await asyncio.to_thread(close)
        finally:
            self._warm_done.set()

    @property
    def status(self) -> dict[str, str]:
        with self._warm_lock:
            return dict(self._warm_status)

    @property
    def ready(self) -> bool:
        return all(value == "ready" for value in self.status.values())

    @property
    def model_details(self) -> dict[str, dict[str, Any]]:
        details: dict[str, dict[str, Any]] = {}
        device = getattr(self.stt, "device", None)
        if device is not None:
            details["stt"] = {
                "backend": getattr(self.stt, "name", "unknown"),
                "execution": getattr(self.stt, "execution", "unknown"),
                "model_source": getattr(self.stt, "model_source", "unknown"),
                "device": getattr(device, "device", "unknown"),
                "compute_type": getattr(device, "compute_type", "unknown"),
            }
        tts_details: dict[str, Any] = {
            "backend": getattr(self.tts, "name", "unknown"),
            "execution": getattr(self.tts, "execution", "unknown"),
            "model_source": getattr(self.tts, "model_source", "unknown"),
            "true_streaming": bool(getattr(self.tts, "supports_true_stream", False)),
        }
        provider = getattr(self.tts, "provider", "")
        if provider:
            tts_details["provider"] = provider
        metadata = getattr(self.tts, "metadata", None)
        if isinstance(metadata, dict):
            for key in ("sample_rate", "voice", "threads", "quantized"):
                if key in metadata:
                    tts_details[key] = metadata[key]
        details["tts"] = tts_details
        if self.endpoint_warmer is not None:
            details["vad"] = {
                "backend": getattr(self.endpoint_warmer, "name", "unknown"),
                "execution": getattr(self.endpoint_warmer, "execution", "unknown"),
                "model_source": getattr(self.endpoint_warmer, "model_source", "unknown"),
            }
        return details

    def make_telemetry(self, call_id: str) -> CallTelemetry:
        return CallTelemetry(call_id, self.metrics_path)

    def claim_call_hello(self, call_id: str) -> bool:
        """Claim the one durable hello measurement for a reconnecting call."""

        return self.hello_state.claim(call_id)

    def finish_call(self, call_id: str) -> None:
        """Release bounded reconnect state after an explicit clean hangup."""

        self.hello_state.finish(call_id)


def build_default_runtime() -> CallRuntime:
    vad_pool = SileroProcessPool()
    return CallRuntime(
        stt=FasterWhisperWorker(),
        brain=BrainClient(),
        tts=create_tts_backend(),
        endpoint_factory=vad_pool.endpoint,
        endpoint_warmer=vad_pool,
        task_dispatcher=get_default_task_dispatcher(),
    )


_DEFAULT_RUNTIME: CallRuntime | None = None
_DEFAULT_RUNTIME_LOCK = threading.RLock()


def get_default_runtime() -> CallRuntime:
    global _DEFAULT_RUNTIME
    if _DEFAULT_RUNTIME is not None:
        return _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_LOCK:
        if _DEFAULT_RUNTIME is None:
            _DEFAULT_RUNTIME = build_default_runtime()
    return _DEFAULT_RUNTIME


def warm_default_runtime_background() -> None:
    """Start one daemon model-warm thread without making web import fragile."""
    get_default_runtime().start_warmup()


def build_desk_runtime() -> CallRuntime:
    """Share the local models while using hands-free desk endpoint timing.

    The VAD child's silence threshold is the adaptive CEILING: the parent's
    AdaptiveEouPolicy commits earlier (at min/base trailing silence) via
    finish(reason="silence"), and the child only emits on its own when an
    incomplete utterance runs the pause out to the ceiling.
    """

    shared = get_default_runtime()
    eou_min_ms = int(os.environ.get("SERENA_DESK_EOU_MIN_MS", "480"))
    eou_base_ms = int(
        os.environ.get(
            "SERENA_DESK_EOU_BASE_MS",
            os.environ.get("SERENA_DESK_VAD_SILENCE_MS", "800"),
        )
    )
    eou_max_ms = int(os.environ.get("SERENA_DESK_EOU_MAX_MS", "2400"))
    # An operator raising only the legacy silence knob must never end up with
    # a ceiling below the base commit threshold.
    eou_max_ms = max(eou_max_ms, eou_base_ms, eou_min_ms)
    vad_pool = SileroProcessPool(
        silence_ms=eou_max_ms,
        pre_roll_ms=int(os.environ.get("SERENA_DESK_VAD_PRE_ROLL_MS", "400")),
        max_utterance_ms=int(os.environ.get("SERENA_DESK_MAX_UTTERANCE_MS", "30000")),
    )
    return CallRuntime(
        stt=shared.stt,
        brain=shared.brain,
        tts=shared.tts,
        endpoint_factory=vad_pool.endpoint,
        endpoint_warmer=vad_pool,
        greeting_state=CallGreetingState(DEFAULT_DESK_GREETING_STATE),
        task_dispatcher=shared.task_dispatcher,
        metrics_path=DEFAULT_DESK_METRICS_PATH,
        eou_policy=AdaptiveEouPolicy(
            min_ms=eou_min_ms,
            base_ms=eou_base_ms,
            max_ms=eou_max_ms,
            partial_trigger_ms=int(
                os.environ.get("SERENA_DESK_EOU_PARTIAL_TRIGGER_MS", "240")
            ),
        ),
    )


_DESK_RUNTIME: CallRuntime | None = None
_DESK_RUNTIME_LOCK = threading.RLock()


def get_desk_runtime() -> CallRuntime:
    global _DESK_RUNTIME
    if _DESK_RUNTIME is not None:
        return _DESK_RUNTIME
    with _DESK_RUNTIME_LOCK:
        if _DESK_RUNTIME is None:
            _DESK_RUNTIME = build_desk_runtime()
    return _DESK_RUNTIME


def warm_desk_runtime_background() -> None:
    get_desk_runtime().start_warmup()


class CallSession:
    def __init__(
        self,
        ws: Any,
        runtime: CallRuntime,
        *,
        peer_host: str | None = None,
    ) -> None:
        self.ws = ws
        self.runtime = runtime
        self.endpoint = runtime.endpoint_factory()
        self.call_id = f"pending-{uuid.uuid4().hex}"
        self.telemetry = runtime.make_telemetry(self.call_id)
        self.current_generation = 0
        self._call_started = False
        self._continuity = True
        self._greeting_requested = False
        self._greeting_claimed = False
        self._greeting_completed = False
        self._greeting_generation: int | None = None
        self._ptt_active = False
        self._closed_input_generation: int | None = None
        self._input_sequences = SequenceTracker()
        self._message_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=64)
        self._audio_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=16)
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._close_done = asyncio.Event()
        self._hung_up = asyncio.Event()
        self._workers: list[asyncio.Task[Any]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._turn_task: asyncio.Task[Any] | None = None
        self._turn_generation: int | None = None
        self._first_sentence_generations: set[int] = set()
        self._cancelled_generations: set[int] = set()
        self._corrupt_input_generations: set[int] = set()
        self._first_output_sequence: dict[int, int] = {}
        self._first_output_kind: dict[int, str] = {}
        self._first_content_output_sequence: dict[int, int] = {}
        self._playback_acknowledged_generations: set[int] = set()
        self._content_playback_acknowledged_generations: set[int] = set()
        self._audio_ended_generations: set[int] = set()
        self._task_job_ids: dict[int, str] = {}
        self._recent_dialogue: deque[str] = deque(maxlen=6)
        self._job_event_cursor = 0
        self._acknowledged_job_events: set[int] = set()
        self._opened_artifact_events: set[int] = set()
        self._tts_namespace = next(_SESSION_IDS)
        self._endpoint_warm_task: asyncio.Task[str] | None = None
        # Adaptive end-of-utterance state (desk runtime only). The parent
        # mirrors the child's utterance so a speculative STT decode can start
        # at the first candidate pause and be reused at commit time.
        self._eou_policy = runtime.eou_policy
        self._mirror_pre_roll: deque[bytes] = deque()
        self._mirror_pre_roll_samples = 0
        self._mirror_utterance = bytearray()
        self._mirror_speech_active = False
        self._last_trailing_ms = 0
        self._speculative_task: asyncio.Task[str] | None = None
        self._speculative_text: str | None = None
        self._speculative_stale = False
        self._tailnet_peer = normalize_tailnet_peer(peer_host)
        self._tailnet_path = TailnetPathMeasurement.unknown()
        self._tailnet_probe_task: asyncio.Task[TailnetPathMeasurement] | None = None
        self._tailnet_sample_tasks: set[asyncio.Task[TailnetPathMeasurement]] = set()
        self._pending_rtt_probes: dict[str, _PendingRttProbe] = {}

    def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._message_worker(), name="call-messages"),
            asyncio.create_task(self._audio_worker(), name="call-audio"),
        ]

    async def _warm_endpoint(self) -> str:
        try:
            await asyncio.to_thread(self.endpoint.warm)
        except Exception as exc:
            return f"error: {exc}"
        return "ready"

    def _start_tailnet_path_probe(
        self,
        *,
        fresh: bool = False,
    ) -> asyncio.Task[TailnetPathMeasurement] | None:
        if self._closed or self._tailnet_peer is None:
            return None
        if fresh:
            if len(self._tailnet_sample_tasks) >= _MAX_PENDING_RTT_SAMPLES:
                return None
            task = self._spawn(self._refresh_tailnet_path(), "call-tailnet-path-sample")
            self._tailnet_sample_tasks.add(task)
            task.add_done_callback(self._tailnet_sample_tasks.discard)
            return task
        if self._tailnet_probe_task is None or self._tailnet_probe_task.done():
            self._tailnet_probe_task = self._spawn(
                self._refresh_tailnet_path(), "call-tailnet-path"
            )
        return self._tailnet_probe_task

    async def _refresh_tailnet_path(self) -> TailnetPathMeasurement:
        try:
            measurement = await probe_tailscale_path(self._tailnet_peer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            measurement = TailnetPathMeasurement.unknown(f"tailscale probe failed: {exc}")
        if not self._closed:
            self._tailnet_path = measurement
            self.telemetry.record(
                "network.path_probe",
                path=measurement.path,
                path_source=measurement.source,
                evidence=measurement.evidence,
            )
        return measurement

    def _expire_rtt_probes(self, now_ns: int) -> None:
        expired = [
            sample_id
            for sample_id, pending in self._pending_rtt_probes.items()
            if now_ns - pending.issued_ns > _RTT_SAMPLE_EXPIRY_NS
        ]
        for sample_id in expired:
            self._pending_rtt_probes.pop(sample_id, None)
            self.telemetry.record(
                "network.rtt_rejected",
                network_sample_id=sample_id,
                reason="expired_without_report",
            )

    async def _answer_ping(
        self,
        control: dict[str, Any],
        sample_id: str,
        pending: _PendingRttProbe,
    ) -> None:
        try:
            measurement = self._tailnet_path
            if pending.path_task is not None:
                measurement = await asyncio.shield(pending.path_task)
            processing_us = max(0, (time.monotonic_ns() - pending.issued_ns) // 1_000)
            fields = {
                "nonce": control["nonce"],
                "sample_id": sample_id,
                "path": measurement.path,
                "path_source": measurement.source,
                "server_processing_us": processing_us,
            }
            if "sent_at_us" in control:
                fields["sent_at_us"] = control["sent_at_us"]
            pending.pong_sent = await self._send(encode_control("pong", **fields))
        finally:
            pending.pong_finished.set()

    async def _commit_rtt_report(
        self,
        control: dict[str, Any],
        pending: _PendingRttProbe,
    ) -> None:
        await pending.pong_finished.wait()
        if self._closed or not pending.pong_sent:
            return
        measurement = self._tailnet_path
        if pending.path_task is not None:
            measurement = await asyncio.shield(pending.path_task)
        self.telemetry.report_rtt(
            control["rtt_ms"],
            measurement.path,
            path_source=measurement.source,
            sample_id=control["sample_id"],
        )

    async def accept(self, raw: object) -> bool:
        """Validate and queue input. Returns true for a valid hangup."""
        try:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                item = ("binary", parse_audio_frame(raw, inbound=True))
                is_hangup = False
            elif isinstance(raw, str):
                control = parse_control(raw)
                item = ("control", control)
                is_hangup = control["type"] == "hangup"
            else:
                raise ProtocolError("websocket frame must be text or binary")
            await self._message_queue.put(item)
            return is_hangup
        except ProtocolError as exc:
            await self._error("protocol", str(exc))
            return False

    async def wait_hung_up(self) -> None:
        await self._hung_up.wait()

    async def close(self, *, reusable_endpoint: bool = False) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._run_close(reusable_endpoint=reusable_endpoint),
                name="call-session-close",
            )
        task = self._close_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run_close(self, *, reusable_endpoint: bool) -> None:
        first_error: BaseException | None = None
        try:
            try:
                await self._release_greeting()
            except BaseException as exc:
                first_error = exc
            try:
                await self._cancel_generation(self.current_generation)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            tasks = list(self._background) + self._workers
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                close_endpoint = getattr(self.endpoint, "close", None)
                if callable(close_endpoint):
                    await asyncio.to_thread(close_endpoint, reusable=reusable_endpoint)
                else:
                    await asyncio.to_thread(self.endpoint.reset)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        finally:
            self.telemetry.record(
                "call.end",
                generation=self.current_generation,
                clean_hangup=reusable_endpoint,
            )
            if reusable_endpoint and self._call_started:
                self.runtime.finish_call(self.call_id)
            self._close_done.set()
        if first_error is not None:
            raise first_error

    async def _message_worker(self) -> None:
        while True:
            kind, payload = await self._message_queue.get()
            try:
                if kind == "binary":
                    await self._handle_binary(payload)
                else:
                    await self._handle_control(payload)
            except ProtocolError as exc:
                await self._error("protocol", str(exc))
            except Exception as exc:
                await self._error("call", str(exc))
            finally:
                self._message_queue.task_done()

    async def _handle_binary(self, frame: AudioFrame) -> None:
        if not self._call_started:
            raise ProtocolError("call.start is required before audio")
        if not self._ptt_active:
            if self._closed_input_generation == self.current_generation:
                self.telemetry.record(
                    "receive.tail_drop",
                    generation=self.current_generation,
                    sequence=frame.header.sequence,
                )
                return
            raise ProtocolError("ptt.begin is required before audio")
        generation = self.current_generation
        gap = self._input_sequences.observe(frame.header.sequence)
        if gap is not None:
            self.telemetry.sequence_gap(
                direction="input",
                generation=generation,
                expected=gap.expected,
                received=gap.received,
            )
            await self._send_control(
                "sequence.gap",
                direction="input",
                generation=generation,
                expected=gap.expected,
                received=gap.received,
            )
            if gap.stale:
                return
        try:
            self._audio_queue.put_nowait(_BinaryInput(generation, frame))
        except asyncio.QueueFull:
            self.telemetry.record(
                "queue.depth", queue="audio", depth=self._audio_queue.qsize(), full=True
            )
            # Continuing after one dropped frame would hand STT a corrupted
            # utterance. Fail this generation explicitly and drain its queued
            # PCM so the next push-to-talk turn starts clean.
            self._ptt_active = False
            self._closed_input_generation = generation
            # Invalidate the input path before the websocket send yields. An
            # already-running VAD call may finish while the fatal error is in
            # flight, and its partial result must never be handed to STT.
            self._corrupt_input_generations.add(generation)
            await self._error(
                "audio_overload",
                "audio processing could not keep up; the call was stopped",
                generation=generation,
                fatal=True,
            )
            await self._cancel_generation(generation)
            self._queue_audio_control(_ResetEndpoint(generation))
            return
        depth = self._audio_queue.qsize()
        self.telemetry.receive_batch(
            generation=generation,
            sequence=frame.header.sequence,
            samples=len(frame.pcm) // 2,
            queue_depth=depth,
            client_monotonic_us=frame.header.timestamp_us,
        )
        self.telemetry.record("queue.depth", queue="audio", depth=depth)
        if frame.header.final:
            self._ptt_active = False
            self.telemetry.speech_end(generation, source="binary_final")
            self._schedule_endpoint_flush(generation, True)

    async def _handle_control(self, control: dict[str, Any]) -> None:
        kind = control["type"]
        if kind == "call.start":
            if self._call_started:
                raise ProtocolError("call.start was already received")
            self._call_started = True
            self.call_id = control["call_id"].strip()
            self.telemetry = self.runtime.make_telemetry(self.call_id)
            requested = int(control.get("generation", 0))
            self.current_generation = requested
            self._greeting_requested = bool(control.get("greeting", False))
            self._continuity = bool(control.get("continuity", True))
            requested_job_cursor = int(control.get("job_cursor", 0))
            self.telemetry.record(
                "call.start",
                job_cursor=requested_job_cursor,
                call_host_pid=os.getpid(),
                anthropic_api_key_present=bool(os.environ.get("ANTHROPIC_API_KEY")),
                metered_auth_env_present=present_metered_auth_env(os.environ),
                speech_execution="local_only",
                continuity=self._continuity,
            )
            if requested_job_cursor:
                await self._accept_job_cursor(requested_job_cursor)
            if self.runtime.task_dispatcher is not None:
                self._spawn(self._watch_job_events(), "call-job-events")
            self._endpoint_warm_task = self._spawn(self._warm_endpoint(), "call-vad-warm")
            self._start_tailnet_path_probe()
            self._spawn(self._announce_ready(), "call-ready")
            return
        if not self._call_started:
            raise ProtocolError("call.start is required first")
        if kind == "job.ack":
            await self._acknowledge_job_event(int(control["event_seq"]))
        elif kind == "artifact.opened":
            await self._acknowledge_artifact_open(
                int(control["event_seq"]),
                str(control["job_id"]),
                str(control["receipt"]),
            )
        elif kind == "ptt.begin":
            await self._begin_generation(int(control["generation"]))
        elif kind == "ptt.end":
            generation = int(control["generation"])
            self._require_current(generation)
            if "eou_monotonic_us" in control:
                self.telemetry.record(
                    "client.eou",
                    generation=generation,
                    client_monotonic_us=control["eou_monotonic_us"],
                    source="ptt_release",
                )
            if self._ptt_active:
                self._ptt_active = False
                self.telemetry.speech_end(
                    generation,
                    source="ptt",
                    client_eou_us=control.get("eou_monotonic_us"),
                )
                self._schedule_endpoint_flush(generation, True)
            await self._send_control("ptt.ended", generation=generation)
        elif kind == "cancel":
            generation = int(control.get("generation", self.current_generation))
            if generation == self.current_generation:
                self._ptt_active = False
                await self._complete_greeting(generation, reason="cancelled")
                await self._cancel_generation(generation)
                self._queue_audio_control(_ResetEndpoint(generation))
            await self._send_control("cancelled", generation=generation)
        elif kind == "hangup":
            self._ptt_active = False
            try:
                await self._complete_greeting(self.current_generation, reason="call_ended")
                await self._cancel_generation(self.current_generation)
                await self._send_control("call.ended", call_id=self.call_id)
            finally:
                self._hung_up.set()
        elif kind == "ping":
            now_ns = time.monotonic_ns()
            self._expire_rtt_probes(now_ns)
            if (
                len(self._pending_rtt_probes) >= _MAX_PENDING_RTT_SAMPLES
                or len(self._tailnet_sample_tasks) >= _MAX_PENDING_RTT_SAMPLES
            ):
                self.telemetry.record(
                    "network.rtt_rejected",
                    reason="pending_limit",
                    client_nonce=str(control["nonce"]),
                )
                await self._error("rtt_busy", "too many RTT samples are already pending")
                return
            sample_id = secrets.token_urlsafe(24)
            while sample_id in self._pending_rtt_probes:
                sample_id = secrets.token_urlsafe(24)
            pending = _PendingRttProbe(
                issued_ns=now_ns,
                path_task=self._start_tailnet_path_probe(fresh=True),
            )
            self._pending_rtt_probes[sample_id] = pending
            self._spawn(
                self._answer_ping(control, sample_id, pending),
                "call-pong",
            )
        elif kind == "pong":
            return
        elif kind == "rtt.report":
            sample_id = control["sample_id"]
            pending = self._pending_rtt_probes.pop(sample_id, None)
            if pending is None:
                self.telemetry.record(
                    "network.rtt_rejected",
                    network_sample_id=sample_id,
                    reason="unknown_or_replayed_sample",
                )
                return
            self._spawn(
                self._commit_rtt_report(control, pending),
                "call-rtt-report",
            )
        elif kind == "playback.started":
            generation = int(control["generation"])
            expected_sequence = self._first_output_sequence.get(generation)
            if (
                not self._generation_active(generation)
                or expected_sequence is None
                or control.get("sequence") != expected_sequence
            ):
                self.telemetry.record(
                    "playback.ack_rejected",
                    generation=generation,
                    sequence=control.get("sequence"),
                    expected_sequence=expected_sequence,
                    reason="stale_or_unsent_generation",
                )
                return
            self.telemetry.playback_started(
                generation,
                sequence=control.get("sequence"),
                timestamp_us=control.get("timestamp_us"),
                eou_to_playback_ms=control.get("eou_to_playback_ms"),
                first_output_to_playback_ms=control.get("first_output_to_playback_ms"),
                first_pcm_write_to_playback_ms=control.get("first_pcm_write_to_playback_ms"),
                play_return_to_head_ms=control.get("play_return_to_head_ms"),
                measurement_point=control.get("measurement_point"),
                kind=self._first_output_kind.get(generation, "content"),
            )
            if control.get("measurement_point") == "playback_head_advanced":
                self._playback_acknowledged_generations.add(generation)
                self._retire_completed_generation(generation)
        elif kind == "playback.segment_started":
            generation = int(control["generation"])
            expected_sequence = self._first_content_output_sequence.get(generation)
            if (
                not self._generation_active(generation)
                or control.get("kind") != "content"
                or expected_sequence is None
                or control.get("sequence") != expected_sequence
            ):
                self.telemetry.record(
                    "playback.content_ack_rejected",
                    generation=generation,
                    sequence=control.get("sequence"),
                    expected_sequence=expected_sequence,
                    kind=control.get("kind"),
                    reason="stale_or_unsent_generation",
                )
                return
            self.telemetry.content_playback_started(
                generation,
                sequence=int(control["sequence"]),
                timestamp_us=control.get("timestamp_us"),
                eou_to_playback_ms=control.get("eou_to_playback_ms"),
                measurement_point=control.get("measurement_point"),
            )
            if control.get("call_hello") and self.runtime.claim_call_hello(self.call_id):
                self.telemetry.call_hello(
                    generation,
                    app_uptime_ms=int(control["app_uptime_ms"]),
                    cold_start=bool(control.get("cold_start", False)),
                )
            self._content_playback_acknowledged_generations.add(generation)
            await self._complete_greeting(generation, reason="playback")
            self._retire_completed_generation(generation)
        elif kind == "playback.underrun":
            self.telemetry.record(
                "playback.underrun",
                generation=control["generation"],
                count=control.get("count"),
            )
        elif kind == "sequence.gap":
            self.telemetry.sequence_gap(
                direction=control.get("direction", "output"),
                generation=control["generation"],
                expected=control["expected"],
                received=control["received"],
            )

    async def _announce_ready(self) -> None:
        status = await self.runtime.warm()
        if self._endpoint_warm_task is not None:
            status["vad"] = await self._endpoint_warm_task
        ready = all(value == "ready" for value in status.values())
        await self._send_control(
            "call.ready",
            call_id=self.call_id,
            ready=ready,
            protocol_version=1,
            models=status,
            model_details=self.runtime.model_details,
        )
        self.telemetry.record(
            "call.ready",
            ready=ready,
            models=status,
            model_details=self.runtime.model_details,
            call_host_pid=os.getpid(),
            anthropic_api_key_present=bool(os.environ.get("ANTHROPIC_API_KEY")),
            metered_auth_env_present=present_metered_auth_env(os.environ),
            speech_execution="local_only",
        )
        for component, value in status.items():
            if value.startswith("error:"):
                await self._error(
                    "model_warm_failed", value.removeprefix("error: "), component=component
                )
        if ready and self._greeting_requested and self._generation_active(self.current_generation):
            await self._start_greeting()

    async def _start_greeting(self) -> None:
        claim_task = asyncio.create_task(
            asyncio.to_thread(self.runtime.greeting_state.claim, self.call_id),
            name=f"call-greeting-claim-{self.call_id}",
        )
        try:
            context = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            # Cancelling an asyncio.to_thread waiter does not stop the worker.
            # Reap the durable claim before allowing session teardown to finish,
            # otherwise the reconnect can be suppressed by an orphaned claim.
            context = await claim_task
            if context is not None:
                await asyncio.to_thread(self.runtime.greeting_state.release, self.call_id)
            raise
        if context is None:
            self.telemetry.record("call.greeting_skipped", reason="already_greeted")
            return
        generation = self.current_generation
        self._greeting_claimed = True
        self._greeting_completed = False
        self._greeting_generation = generation
        self.telemetry.record(
            "call.greeting_start",
            generation=generation,
            first_call_today=context.first_call_today,
            call_number_today=context.call_number_today,
        )
        self._turn_generation = generation
        self._turn_task = self._spawn(
            self._brain_to_tts(generation, context.prompt()),
            f"call-greeting-{generation}",
        )

    async def _complete_greeting(self, generation: int, *, reason: str) -> None:
        if (
            not self._greeting_claimed
            or self._greeting_completed
            or generation != self._greeting_generation
        ):
            return
        persisted = await asyncio.to_thread(self.runtime.greeting_state.complete, self.call_id)
        self._greeting_completed = True
        self.telemetry.record(
            "call.greeting_complete",
            generation=generation,
            reason=reason,
            persisted=persisted,
        )

    async def _release_greeting(self) -> None:
        if not self._greeting_claimed or self._greeting_completed:
            return
        await asyncio.to_thread(self.runtime.greeting_state.release, self.call_id)
        self._greeting_claimed = False
        self.telemetry.record(
            "call.greeting_released",
            generation=self._greeting_generation,
        )

    async def _begin_generation(self, generation: int) -> None:
        if generation <= self.current_generation:
            raise ProtocolError(
                f"stale generation {generation}, current is {self.current_generation}"
            )
        await self._complete_greeting(self.current_generation, reason="interrupted_by_speech")
        await self._cancel_generation(self.current_generation)
        async with self._send_lock:
            self.current_generation = generation
            self._cancelled_generations.clear()
            self._corrupt_input_generations.clear()
            self._ptt_active = True
            self._closed_input_generation = None
            self._input_sequences.reset()
        allow = getattr(self.runtime.tts, "allow_generation", None)
        if callable(allow):
            allow(self._tts_generation(generation))
        self._queue_audio_control(_ResetEndpoint(generation))
        self.telemetry.record("generation.begin", generation=generation)
        await self._send_control("generation", generation=generation)

    async def _cancel_generation(self, generation: int) -> None:
        # Serialize invalidation with binary sends. Once this lock is released,
        # no old-generation audio can begin a websocket send.
        async with self._send_lock:
            self._cancelled_generations.add(generation)
            self._first_output_sequence.pop(generation, None)
            self._first_output_kind.pop(generation, None)
            self._first_content_output_sequence.pop(generation, None)
        turn_active = (
            self._turn_task is not None
            and not self._turn_task.done()
            and self._turn_generation == generation
        )
        if turn_active:
            cancel_stt = getattr(self.runtime.stt, "cancel", None)
            cancellations = [self.runtime.tts.cancel(self._tts_generation(generation))]
            if callable(cancel_stt):
                result = cancel_stt(self._stt_generation(generation))
                if inspect.isawaitable(result):
                    cancellations.append(result)
            await asyncio.gather(*cancellations, return_exceptions=True)
            self._turn_task.cancel()
            await asyncio.gather(self._turn_task, return_exceptions=True)
            retire = getattr(self.runtime.tts, "retire_generation", None)
            if callable(retire):
                retire(self._tts_generation(generation))
        self._discard_queued_audio(generation)
        self.telemetry.record("generation.cancel", generation=generation)
        self.telemetry.retire_generation(generation)
        self._first_sentence_generations.discard(generation)
        self._playback_acknowledged_generations.discard(generation)
        self._content_playback_acknowledged_generations.discard(generation)
        self._audio_ended_generations.discard(generation)

    def _retire_completed_generation(self, generation: int) -> None:
        if (
            generation not in self._playback_acknowledged_generations
            or generation not in self._audio_ended_generations
            or (
                generation in self._first_content_output_sequence
                and generation not in self._content_playback_acknowledged_generations
            )
        ):
            return
        self.telemetry.retire_generation(generation)
        self._first_output_sequence.pop(generation, None)
        self._first_output_kind.pop(generation, None)
        self._first_content_output_sequence.pop(generation, None)
        self._first_sentence_generations.discard(generation)
        self._playback_acknowledged_generations.discard(generation)
        self._content_playback_acknowledged_generations.discard(generation)
        self._audio_ended_generations.discard(generation)

    def _tts_generation(self, generation: int) -> int:
        return (self._tts_namespace << 32) | (generation & 0xFFFF_FFFF)

    def _stt_generation(self, generation: int) -> int:
        return (self._tts_namespace << 32) | (generation & 0xFFFF_FFFF)

    def _discard_queued_audio(self, generation: int) -> None:
        kept: list[Any] = []
        while True:
            try:
                item = self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._audio_queue.task_done()
            if getattr(item, "generation", None) != generation:
                kept.append(item)
            else:
                self.telemetry.record(
                    "receive.cancelled_drop",
                    generation=generation,
                    item_type=type(item).__name__,
                )
        for item in kept:
            self._audio_queue.put_nowait(item)

    def _queue_audio_control(self, item: Any) -> None:
        try:
            self._audio_queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise ProtocolError("audio control queue is full") from exc

    def _schedule_endpoint_flush(self, generation: int, force: bool) -> None:
        try:
            self._audio_queue.put_nowait(_FlushEndpoint(generation, force))
            return
        except asyncio.QueueFull:
            pass

        async def flush() -> None:
            await self._audio_queue.join()
            if not self._generation_active(generation):
                return
            self._queue_audio_control(_FlushEndpoint(generation, force))

        self._spawn(flush(), f"call-flush-{generation}")

    def _generation_active(self, generation: int) -> bool:
        return (
            not self._closed
            and generation == self.current_generation
            and generation not in self._cancelled_generations
        )

    def _input_generation_active(self, generation: int) -> bool:
        return (
            self._generation_active(generation)
            and generation not in self._corrupt_input_generations
        )

    def _require_current(self, generation: int) -> None:
        if generation != self.current_generation:
            raise ProtocolError(
                f"stale generation {generation}, current is {self.current_generation}"
            )

    async def _audio_worker(self) -> None:
        await self.runtime.warm()
        while True:
            item = await self._audio_queue.get()
            try:
                if self._endpoint_warm_task is not None:
                    endpoint_status = await self._endpoint_warm_task
                    if endpoint_status != "ready":
                        raise RuntimeError(endpoint_status)
                generation = item.generation
                if generation != self.current_generation:
                    continue
                if isinstance(item, _ResetEndpoint):
                    await asyncio.to_thread(self.endpoint.reset)
                    self._reset_turn_mirror()
                    continue
                if not self._input_generation_active(generation):
                    self.telemetry.record(
                        "receive.cancelled_drop",
                        generation=generation,
                        item_type=type(item).__name__,
                    )
                    continue
                if isinstance(item, _BinaryInput) and self._closed_input_generation == generation:
                    self.telemetry.record(
                        "receive.tail_drop",
                        generation=generation,
                        sequence=item.frame.header.sequence,
                    )
                    continue
                if isinstance(item, _FlushEndpoint):
                    result = await asyncio.to_thread(self.endpoint.finish, force=item.force)
                else:
                    result = await asyncio.to_thread(self.endpoint.process_pcm, item.frame.pcm)
                    if result is None and self._eou_policy is not None:
                        result = await self._observe_adaptive_frame(
                            generation, item.frame.pcm
                        )
                if result is not None:
                    precomputed = self._take_speculative_transcript()
                    self._reset_turn_mirror()
                    await self._endpoint_detected(
                        generation, result, precomputed=precomputed
                    )
            except Exception as exc:
                await self._error("audio_processing", str(exc), generation=generation)
            finally:
                self._audio_queue.task_done()

    async def _observe_adaptive_frame(
        self, generation: int, pcm: bytes
    ) -> EndpointResult | None:
        """Adaptive end-of-utterance step for one already-processed mic frame.

        Mirrors the utterance parent-side, starts one speculative STT decode
        at the first candidate pause, and commits the turn early through
        finish(reason="silence") when the policy allows. Returns the early
        EndpointResult, or None to keep listening.
        """
        policy = self._eou_policy
        assert policy is not None
        speech_active = bool(getattr(self.endpoint, "speech_active", False))
        trailing_ms = int(getattr(self.endpoint, "trailing_ms", 0))
        if not speech_active:
            self._push_pre_roll(pcm)
            self._last_trailing_ms = 0
            return None
        if not self._mirror_speech_active:
            self._mirror_speech_active = True
            self._mirror_utterance = bytearray()
            for chunk in self._mirror_pre_roll:
                self._mirror_utterance.extend(chunk)
            self._mirror_pre_roll.clear()
            self._mirror_pre_roll_samples = 0
        self._mirror_utterance.extend(pcm)
        self._harvest_speculative_result()
        if (
            self._speculative_task is not None or self._speculative_text is not None
        ) and trailing_ms < self._last_trailing_ms:
            # Speech resumed after the speculative decode was submitted, so
            # its transcript no longer covers the utterance.
            self._speculative_stale = True
        self._last_trailing_ms = trailing_ms
        if (
            trailing_ms >= policy.partial_trigger_ms
            and self._speculative_task is None
            and (self._speculative_text is None or self._speculative_stale)
        ):
            self._start_speculative_transcribe(generation)
        completeness = UNKNOWN
        if self._speculative_text is not None and not self._speculative_stale:
            completeness = assess_completeness(self._speculative_text)
        transcript_pending = self._speculative_task is not None
        if (
            policy.decide(
                trailing_ms,
                completeness,
                transcript_pending=transcript_pending,
            )
            != "commit"
        ):
            return None
        result = await asyncio.to_thread(self.endpoint.finish, reason="silence")
        if result is not None:
            self.telemetry.record(
                "endpoint.adaptive_commit",
                generation=generation,
                trailing_ms=trailing_ms,
                completeness=completeness,
            )
        return result

    def _push_pre_roll(self, pcm: bytes) -> None:
        self._mirror_pre_roll.append(pcm)
        self._mirror_pre_roll_samples += len(pcm) // 2
        while (
            self._mirror_pre_roll
            and self._mirror_pre_roll_samples - len(self._mirror_pre_roll[0]) // 2
            >= _ADAPTIVE_PRE_ROLL_SAMPLES
        ):
            dropped = self._mirror_pre_roll.popleft()
            self._mirror_pre_roll_samples -= len(dropped) // 2

    def _start_speculative_transcribe(self, generation: int) -> None:
        pcm = bytes(self._mirror_utterance)
        if not pcm:
            return
        self._speculative_text = None
        self._speculative_stale = False
        self.telemetry.record(
            "stt.speculative_start",
            generation=generation,
            samples=len(pcm) // 2,
        )
        self._speculative_task = self._spawn(
            self.runtime.stt.transcribe(
                pcm, 16_000, generation=self._stt_generation(generation)
            ),
            f"call-speculative-stt-{generation}",
        )

    def _harvest_speculative_result(self) -> None:
        task = self._speculative_task
        if task is None or not task.done():
            return
        self._speculative_task = None
        if task.cancelled() or task.exception() is not None:
            return
        self._speculative_text = str(task.result())

    def _take_speculative_transcript(self) -> str | asyncio.Task[str] | None:
        """Pop the speculative decode for reuse at end of utterance.

        Returns the finished transcript, the still-running decode task when it
        is fresh (awaiting it beats re-decoding the same audio), or None when
        no reusable speculative decode exists.
        """
        self._harvest_speculative_result()
        task = self._speculative_task
        text = self._speculative_text
        stale = self._speculative_stale
        self._speculative_task = None
        self._speculative_text = None
        self._speculative_stale = False
        if stale:
            if task is not None:
                task.cancel()
            return None
        if text is not None:
            return text
        return task

    def _reset_turn_mirror(self) -> None:
        self._mirror_pre_roll.clear()
        self._mirror_pre_roll_samples = 0
        self._mirror_utterance = bytearray()
        self._mirror_speech_active = False
        self._last_trailing_ms = 0
        task = self._speculative_task
        self._speculative_task = None
        self._speculative_text = None
        self._speculative_stale = False
        if task is not None:
            task.cancel()

    async def _endpoint_detected(
        self,
        generation: int,
        result: EndpointResult,
        *,
        precomputed: str | asyncio.Task[str] | None = None,
    ) -> None:
        if not self._input_generation_active(generation):
            if isinstance(precomputed, asyncio.Task):
                precomputed.cancel()
            self.telemetry.record(
                "endpoint.cancelled_drop",
                generation=generation,
                reason=result.reason,
            )
            return
        automatic = result.reason in {"silence", "max_duration"}
        endpoint_delay_ms = round(result.trailing_silence_samples * 1000 / 16_000, 3)
        if automatic:
            self._ptt_active = False
            self._closed_input_generation = generation
            self.telemetry.speech_end(
                generation,
                source="silero",
                trailing_silence_samples=result.trailing_silence_samples,
            )
        self.telemetry.stage(
            generation,
            "endpoint.detected",
            reason=result.reason,
            endpoint_delay_ms=endpoint_delay_ms,
        )
        self.telemetry.record(
            "endpoint.detected",
            generation=generation,
            reason=result.reason,
            samples=result.sample_count,
            trailing_silence_samples=result.trailing_silence_samples,
            endpoint_delay_ms=endpoint_delay_ms,
        )
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            await asyncio.gather(self._turn_task, return_exceptions=True)
        self._turn_generation = generation
        self._turn_task = self._spawn(
            self._run_turn(generation, result.pcm, precomputed=precomputed),
            f"call-turn-{generation}",
        )

    async def _run_turn(
        self,
        generation: int,
        pcm: bytes,
        *,
        precomputed: str | asyncio.Task[str] | None = None,
    ) -> None:
        if not self._input_generation_active(generation):
            if isinstance(precomputed, asyncio.Task):
                precomputed.cancel()
            return
        stt_started = time.monotonic_ns()
        self.telemetry.stage(generation, "stt.start", samples=len(pcm) // 2)
        self.telemetry.record("stt.start", generation=generation, samples=len(pcm) // 2)
        await self._send_control_for_generation(generation, "stt.start", generation=generation)
        if not self._input_generation_active(generation):
            if isinstance(precomputed, asyncio.Task):
                precomputed.cancel()
            return
        text: str | None = None
        if isinstance(precomputed, str):
            text = precomputed
        elif precomputed is not None:
            try:
                text = await asyncio.shield(precomputed)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                text = None
            except Exception:
                text = None
        speculative_hit = text is not None
        if precomputed is not None:
            self.telemetry.record(
                "stt.speculative", generation=generation, hit=speculative_hit
            )
        if text is None:
            try:
                text = await self.runtime.stt.transcribe(
                    pcm, 16_000, generation=self._stt_generation(generation)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._error("stt", str(exc), generation=generation)
                return
        if not self._generation_active(generation):
            return
        elapsed_ms = (time.monotonic_ns() - stt_started) / 1_000_000
        self.telemetry.record(
            "stt.done",
            generation=generation,
            elapsed_ms=round(elapsed_ms, 3),
            text_chars=len(text),
            speculative=speculative_hit,
        )
        self.telemetry.stage(
            generation,
            "stt.done",
            elapsed_ms=round(elapsed_ms, 3),
            text_chars=len(text),
            speculative=speculative_hit,
        )
        await self._send_control_for_generation(
            generation, "stt.result", generation=generation, text=text
        )
        if not text:
            await self._error("no_speech", "no speech was transcribed", generation=generation)
            return
        if _is_bare_name_call(text):
            # "Serena." on its own is him stopping her, not a question. The
            # old behaviour ran a brain turn on it, so interrupting her made
            # her START TALKING AGAIN six seconds later (observed 2026-08-11
            # 00:29, generations 3 through 5 in desk_metrics). Attention means
            # shut up and listen.
            self.telemetry.record("turn.bare_name_attention", generation=generation)
            await self._send_control_for_generation(
                generation, "turn.listen", generation=generation
            )
            return
        self._recent_dialogue.append(f"Raghav: {text}")
        panel_action = await self._apply_code_panel_intent(generation, text)
        task_job_id = None
        if panel_action is None:
            task_job_id = await self._submit_call_task(generation, text)
        brain_text = text
        if panel_action:
            exact = (
                "i've opened my coding view."
                if panel_action == "open"
                else "i've hidden my coding view."
            )
            brain_text += (
                "\n\n<voice-ui-status>Reply with exactly this sentence and nothing else: "
                + exact
                + "</voice-ui-status>"
            )
        elif task_job_id:
            brain_text += (
                "\n\n<call-task-status>"
                f"draft job {task_job_id} is already accepted and running. "
                "Acknowledge it briefly. Tell Raghav the link will appear in the "
                "call screen. Do not claim the draft is ready yet."
                "</call-task-status>"
            )
        spoken_reply = await self._brain_to_tts(generation, brain_text)
        if spoken_reply:
            self._recent_dialogue.append(f"Serena: {spoken_reply}")

    async def _apply_code_panel_intent(
        self,
        generation: int,
        text: str,
    ) -> str | None:
        intent = parse_code_panel_intent(text)
        if intent is None:
            return None
        await self._send_control_for_generation(
            generation,
            "code.panel",
            generation=generation,
            action=intent.action,
        )
        self.telemetry.record(
            "code.panel",
            generation=generation,
            action=intent.action,
        )
        return intent.action

    async def _submit_call_task(self, generation: int, text: str) -> str | None:
        dispatcher = self.runtime.task_dispatcher
        if dispatcher is None:
            return None
        try:
            job = await asyncio.to_thread(
                dispatcher.submit_if_explicit,
                text,
                call_id=self.call_id,
                turn_id=f"{self.call_id}:{generation}",
            )
        except Exception as exc:
            await self._error("task", str(exc), generation=generation)
            return None
        if job is None:
            return None
        self._task_job_ids[generation] = job.job_id
        self.telemetry.record(
            "task.accepted",
            generation=generation,
            job_id=job.job_id,
            state=job.state,
        )
        return job.job_id

    async def _brain_to_tts(self, generation: int, text: str) -> str | None:
        splitter = _reply_splitter(self.runtime.tts)
        sentences: asyncio.Queue[_TTSRequest | None] = asyncio.Queue(maxsize=8)
        content_queued = asyncio.Event()
        first_delta_received = asyncio.Event()
        tts_task = asyncio.create_task(
            self._tts_worker(generation, sentences),
            name=f"call-tts-{generation}",
        )
        backchannel_task = asyncio.create_task(
            self._delayed_backchannel(
                generation,
                sentences,
                content_queued,
                first_delta_received,
            ),
            name=f"call-backchannel-{generation}",
        )
        first_delta = True
        saw_delta = False
        done_say = ""
        reply_parts: list[str] = []
        request_id = uuid.uuid4().hex
        self.telemetry.stage(
            generation,
            "brain.request",
            request_id=request_id,
        )
        try:
            async for event in self.runtime.brain.stream_turn(
                text,
                call_id=self.call_id,
                turn_id=f"{self.call_id}:{generation}",
                request_id=request_id,
                journal=self._continuity,
            ):
                if not self._generation_active(generation):
                    return
                if event.type == "delta":
                    first_delta_received.set()
                    saw_delta = True
                    reply_parts.append(event.delta)
                    if first_delta:
                        first_delta = False
                        self.telemetry.record(
                            "brain.first_delta",
                            generation=generation,
                            backend=event.backend,
                        )
                        self.telemetry.stage(
                            generation,
                            "brain.first_delta",
                            backend=event.backend,
                        )
                    await self._send_control_for_generation(
                        generation,
                        "brain.delta",
                        generation=generation,
                        delta=event.delta,
                    )
                    for sentence in splitter.feed(event.delta):
                        await self._queue_sentence(
                            generation,
                            sentences,
                            sentence,
                            content_queued=content_queued,
                        )
                elif event.type == "done":
                    done_say = event.say
                    meta = event.meta.as_dict() if event.meta is not None else {}
                    self.telemetry.record(
                        "brain.done",
                        generation=generation,
                        backend=event.backend,
                        meta=meta,
                    )
                    job_id = self._task_job_ids.get(generation)
                    session_id = meta.get("session_id")
                    if (
                        job_id is not None
                        and self.runtime.task_dispatcher is not None
                        and isinstance(session_id, str)
                        and session_id.strip()
                    ):
                        await asyncio.to_thread(
                            self.runtime.task_dispatcher.link_origin_session,
                            job_id,
                            session_id,
                        )
                    await self._send_control_for_generation(
                        generation,
                        "brain.done",
                        generation=generation,
                        text=event.say,
                        meta=meta,
                        backend=event.backend,
                    )
            if not saw_delta and done_say:
                for sentence in splitter.feed(done_say):
                    await self._queue_sentence(
                        generation,
                        sentences,
                        sentence,
                        content_queued=content_queued,
                    )
            for sentence in splitter.flush():
                await self._queue_sentence(
                    generation,
                    sentences,
                    sentence,
                    content_queued=content_queued,
                )
            backchannel_task.cancel()
            await asyncio.gather(backchannel_task, return_exceptions=True)
            await sentences.put(None)
            await tts_task
            return done_say or "".join(reply_parts)
        except asyncio.CancelledError:
            backchannel_task.cancel()
            tts_task.cancel()
            await asyncio.gather(backchannel_task, tts_task, return_exceptions=True)
            raise
        except Exception as exc:
            backchannel_task.cancel()
            await self.runtime.tts.cancel(self._tts_generation(generation))
            tts_task.cancel()
            await asyncio.gather(backchannel_task, tts_task, return_exceptions=True)
            await self._error("brain", str(exc), generation=generation)
        finally:
            retire = getattr(self.runtime.tts, "retire_generation", None)
            if callable(retire):
                retire(self._tts_generation(generation))

    async def _queue_sentence(
        self,
        generation: int,
        queue: asyncio.Queue[_TTSRequest | None],
        sentence: str,
        *,
        content_queued: asyncio.Event,
    ) -> None:
        sentence = prepare_spoken_text(sentence)
        if not sentence:
            return
        content_queued.set()
        if generation not in self._first_sentence_generations:
            self._first_sentence_generations.add(generation)
            self.telemetry.record("sentence.first", generation=generation, chars=len(sentence))
            self.telemetry.stage(
                generation,
                "sentence.first",
                chars=len(sentence),
            )
        await queue.put(_TTSRequest(sentence, "content"))
        self.telemetry.record("queue.depth", queue="tts", depth=queue.qsize())

    @property
    def _thinking_sounds(self) -> ThinkingSoundPool:
        """One pool per call, so the no-repeat memory spans her whole session."""

        pool = getattr(self, "_thinking_sound_pool", None)
        if pool is None:
            pool = ThinkingSoundPool()
            self._thinking_sound_pool = pool
        return pool

    async def _delayed_backchannel(
        self,
        generation: int,
        queue: asyncio.Queue[_TTSRequest | None],
        content_queued: asyncio.Event,
        first_delta_received: asyncio.Event,
    ) -> None:
        """Cover a slow brain with one short sound, never on every turn.

        Memory 414 records that Raghav rejected the automatic "yeah" on every
        turn. This waits for the brain's first delta and speaks only when no
        response has arrived by the deadline, so a fast turn stays silent. The
        clip is queued behind the same generation-scoped worker as the reply,
        which is what keeps sequence numbers contiguous and makes barge-in and
        cancellation stop it for free.
        """

        pool = self._thinking_sounds
        sample_rate = getattr(self.runtime.tts, "sample_rate", None)
        # A positive legacy delay is an operator decision and still wins. The
        # deployed service exports the old knob as zero to disable it; treating
        # mere presence as an override disabled the new clip path too.
        try:
            legacy_delay_ms = float(
                os.environ.get("SERENA_CALL_BACKCHANNEL_DELAY_MS", "0")
            )
        except ValueError:
            legacy_delay_ms = 0.0
        legacy_delay_ms = max(0.0, min(legacy_delay_ms, 5_000.0))
        legacy_requested = legacy_delay_ms > 0
        use_clip = (
            not legacy_requested
            and pool.enabled
            and isinstance(sample_rate, int)
            and sample_rate > 0
            and bool(pool.clips)
        )
        text = ""
        if use_clip:
            delay_ms = pool.delay_ms
        else:
            text = os.environ.get("SERENA_CALL_BACKCHANNEL_TEXT", "yeah.").strip()
            delay_ms = legacy_delay_ms
            if not text:
                return
        # Zero means off for both knobs.  A filler with no gate in front of it
        # is the every-turn behaviour that was already rejected once.
        if delay_ms == 0:
            return
        delta_wait = asyncio.create_task(first_delta_received.wait())
        content_wait = asyncio.create_task(content_queued.wait())
        try:
            ready, _pending = await asyncio.wait(
                (delta_wait, content_wait),
                timeout=delay_ms / 1_000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready:
                return
        finally:
            for waiter in (delta_wait, content_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(delta_wait, content_wait, return_exceptions=True)
        if (
            first_delta_received.is_set()
            or content_queued.is_set()
            or not self._generation_active(generation)
        ):
            return
        clip = None
        if use_clip:
            # Chosen only now that it is really going to play, so a fast turn
            # does not silently burn the clip that varies the next slow one.
            clip = pool.take(sample_rate=sample_rate)
            if clip is None:
                return
            text = clip.clip_id
        await queue.put(_TTSRequest(text, "acknowledgement", clip=clip))
        self.telemetry.record(
            "backchannel.queued",
            generation=generation,
            delay_ms=round(delay_ms, 3),
            chars=len(text),
            clip=clip.clip_id if clip is not None else "",
        )
        self.telemetry.stage(
            generation,
            "backchannel.queued",
            delay_ms=round(delay_ms, 3),
            chars=len(text),
            clip=clip.clip_id if clip is not None else "",
        )

    async def _tts_worker(
        self,
        generation: int,
        sentences: asyncio.Queue[_TTSRequest | None],
    ) -> None:
        output_sequence = 0
        output_started = False
        output_sample_rate: int | None = None
        while True:
            request = await sentences.get()
            try:
                if request is None:
                    break
                if not self._generation_active(generation):
                    return
                self.telemetry.record(
                    "tts.start",
                    generation=generation,
                    chars=len(request.text),
                    kind=request.kind,
                )
                self.telemetry.stage(
                    generation,
                    f"tts.{request.kind}.start",
                    chars=len(request.text),
                    backend=getattr(self.runtime.tts, "name", "unknown"),
                )
                segment_started = False
                source = (
                    _prerendered_chunks(request.clip)
                    if request.clip is not None
                    else self.runtime.tts.stream(
                        request.text, generation=self._tts_generation(generation)
                    )
                )
                async for chunk in source:
                    if not self._generation_active(generation):
                        return
                    if output_sample_rate is None:
                        output_sample_rate = chunk.sample_rate
                        self.telemetry.stage(
                            generation,
                            "tts.first_pcm",
                            sample_rate=output_sample_rate,
                            pcm_bytes=len(chunk.pcm),
                        )
                    elif chunk.sample_rate != output_sample_rate:
                        raise RuntimeError("TTS sample rate changed within a generation")
                    if not output_started:
                        await self._send_control_for_generation(
                            generation,
                            "audio.start",
                            generation=generation,
                            sample_rate=output_sample_rate,
                        )
                        output_started = True
                    if not segment_started:
                        await self._send_control_for_generation(
                            generation,
                            "audio.segment",
                            generation=generation,
                            kind=request.kind,
                            sequence=output_sequence,
                        )
                        self.telemetry.record(
                            "audio.segment",
                            generation=generation,
                            kind=request.kind,
                            sequence=output_sequence,
                        )
                        self.telemetry.stage(
                            generation,
                            f"tts.{request.kind}.first_pcm",
                            sample_rate=output_sample_rate,
                            pcm_bytes=len(chunk.pcm),
                        )
                        segment_started = True
                    sent = await self._send_pcm(
                        generation,
                        output_sequence,
                        chunk.pcm,
                        output_sample_rate,
                        final=False,
                        segment_kind=request.kind,
                    )
                    if not sent:
                        return
                    output_sequence = (output_sequence + 1) & 0xFFFFFFFF
            finally:
                sentences.task_done()
        if output_started and self._generation_active(generation):
            sent = await self._send_control_for_generation(
                generation,
                "audio.end",
                generation=generation,
                last_sequence=(output_sequence - 1) & 0xFFFFFFFF,
            )
            if sent:
                self._audio_ended_generations.add(generation)
                self.telemetry.record(
                    "audio.end",
                    generation=generation,
                    last_sequence=(output_sequence - 1) & 0xFFFFFFFF,
                )
                self._retire_completed_generation(generation)

    async def _send_pcm(
        self,
        generation: int,
        sequence: int,
        pcm: bytes,
        sample_rate: int,
        *,
        final: bool,
        segment_kind: str = "content",
    ) -> bool:
        if not self._generation_active(generation):
            return False
        frame = AudioFrame(
            AudioHeader(
                kind=AudioKind.TTS_PCM16,
                flags=FLAG_FINAL if final else 0,
                sequence=sequence,
                sample_rate=sample_rate,
                timestamp_us=time.monotonic_ns() // 1_000,
            ),
            pcm,
        ).pack()
        sent = await self._send(frame, generation=generation)
        if not sent:
            return False
        self._first_output_sequence.setdefault(generation, sequence)
        self._first_output_kind.setdefault(generation, segment_kind)
        if segment_kind == "content":
            self._first_content_output_sequence.setdefault(generation, sequence)
        self.telemetry.first_audio_send(generation, sequence=sequence, kind=segment_kind)
        if segment_kind == "content":
            self.telemetry.first_content_audio_send(generation, sequence=sequence)
        return True

    async def _watch_job_events(self) -> None:
        dispatcher = self.runtime.task_dispatcher
        if dispatcher is None:
            return
        while not self._closed:
            events = await asyncio.to_thread(
                dispatcher.events_for_call,
                self.call_id,
                after=self._job_event_cursor,
            )
            for event in events:
                if self._closed:
                    return
                control = event.control()
                control_type = str(control.pop("type"))
                sent = await self._send(encode_control(control_type, **control))
                if not sent:
                    return
                self._job_event_cursor = max(self._job_event_cursor, event.event_seq)
                self.telemetry.record(
                    "task.event_delivered",
                    job_id=event.job_id,
                    event_seq=event.event_seq,
                    event_type=event.type,
                )
            await asyncio.sleep(0.1)

    async def _accept_job_cursor(self, event_seq: int) -> None:
        dispatcher = self.runtime.task_dispatcher
        if dispatcher is None:
            raise ProtocolError("job cursor is unavailable")
        events = await asyncio.to_thread(
            dispatcher.events_for_call,
            self.call_id,
            after=0,
        )
        if not any(event.event_seq == event_seq for event in events):
            raise ProtocolError("job cursor does not belong to this call")
        self._job_event_cursor = event_seq
        for event in events:
            if event.event_seq <= event_seq:
                self._record_job_ack(event, source="resume_cursor")

    async def _acknowledge_job_event(self, event_seq: int) -> None:
        event = await self._find_job_event(event_seq)
        self._record_job_ack(event, source="phone_ack")

    async def _acknowledge_artifact_open(self, event_seq: int, job_id: str, receipt: str) -> None:
        event = await self._find_job_event(event_seq)
        if event.type != "artifact.ready" or event.job_id != job_id:
            raise ProtocolError("artifact.opened does not match a ready event")
        dispatcher = self.runtime.task_dispatcher
        consumer = getattr(dispatcher, "consume_artifact_receipt", None)
        artifact_id = event.payload.get("artifact_id")
        sha256 = event.payload.get("sha256")
        if (
            consumer is None
            or not isinstance(artifact_id, str)
            or not isinstance(sha256, str)
            or not await asyncio.to_thread(
                consumer,
                receipt,
                job_id=job_id,
                artifact_id=artifact_id,
                sha256=sha256,
            )
        ):
            raise ProtocolError("artifact.opened receipt was not issued by the server")
        if event_seq in self._opened_artifact_events:
            return
        self._opened_artifact_events.add(event_seq)
        self.telemetry.record(
            "task.artifact_opened",
            job_id=event.job_id,
            event_seq=event.event_seq,
            event_type=event.type,
            source="phone_app",
            receipt_verified=True,
        )

    async def _find_job_event(self, event_seq: int) -> WorkJobEvent:
        dispatcher = self.runtime.task_dispatcher
        if dispatcher is None:
            raise ProtocolError("job events are unavailable")
        event = await asyncio.to_thread(
            dispatcher.event_for_call,
            self.call_id,
            event_seq,
        )
        if event is None:
            raise ProtocolError("job event does not belong to this call")
        return event

    def _record_job_ack(self, event: WorkJobEvent, *, source: str) -> None:
        if event.event_seq in self._acknowledged_job_events:
            return
        self._acknowledged_job_events.add(event.event_seq)
        self.telemetry.record(
            "task.event_acknowledged",
            job_id=event.job_id,
            event_seq=event.event_seq,
            event_type=event.type,
            source=source,
        )

    async def _send_control(self, control_type: str, **fields: Any) -> None:
        await self._send(encode_control(control_type, **fields))

    async def _send_control_for_generation(
        self, guard_generation: int, control_type: str, **fields: Any
    ) -> bool:
        return await self._send(encode_control(control_type, **fields), generation=guard_generation)

    async def _error(self, code: str, message: str, **fields: Any) -> None:
        self.telemetry.record(
            "error",
            code=code,
            message=message,
            **fields,
        )
        generation = fields.get("generation")
        if isinstance(generation, int) and not isinstance(generation, bool):
            await self._send_control_for_generation(
                generation, "error", code=code, message=message, **fields
            )
            return
        await self._send_control("error", code=code, message=message, **fields)

    async def _send(self, payload: str | bytes, *, generation: int | None = None) -> bool:
        if self._closed:
            return False
        async with self._send_lock:
            if self._closed:
                return False
            if generation is not None and not self._generation_active(generation):
                return False
            try:
                await asyncio.to_thread(self.ws.send, payload)
            except Exception:
                self._closed = True
                raise
        return True

    def _spawn(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background_finished)
        return task

    def _background_finished(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        with suppress(Exception):
            self.telemetry.record(
                "task.error",
                task=task.get_name(),
                error_type=type(error).__name__,
                error=str(error),
            )


async def serve_websocket(
    ws: Any,
    *,
    runtime: CallRuntime | None = None,
    peer_host: str | None = None,
) -> None:
    """Async adapter around a simple-websocket compatible object."""
    session = CallSession(
        ws,
        runtime or get_default_runtime(),
        peer_host=peer_host,
    )
    session.start()
    clean_hangup = False
    try:
        while True:
            try:
                raw = await asyncio.to_thread(ws.receive)
            except Exception:
                break
            if raw is None:
                break
            hangup = await session.accept(raw)
            if hangup:
                await session.wait_hung_up()
                clean_hangup = True
                break
    finally:
        await session.close(reusable_endpoint=clean_hangup)


def handle_websocket(
    ws: Any,
    *,
    runtime: CallRuntime | None = None,
    peer_host: str | None = None,
) -> None:
    """Synchronous Flask-Sock entrypoint shared by call and desk routes."""
    asyncio.run(serve_websocket(ws, runtime=runtime, peer_host=peer_host))
