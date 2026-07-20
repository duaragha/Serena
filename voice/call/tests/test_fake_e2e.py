from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from voice.call.brain import BrainDoneMeta, BrainEvent, DeterministicBrainStub
from voice.call.endpoint import EndpointResult, SileroEndpointDetector
from voice.call.greeting import CallGreetingState, GreetingContext
from voice.call.hello_state import CallHelloState
from voice.call.orchestrator import CallRuntime, CallSession
from voice.call.protocol import AudioFrame, AudioHeader, AudioKind, parse_audio_frame
from voice.call.tts import DeterministicTTSStub


class AlwaysSpeech:
    def __call__(self, samples, sample_rate: int) -> float:
        return 0.9

    def reset_states(self) -> None:
        return None


class FakeSTT:
    def __init__(self) -> None:
        self.warmed = False
        self.inputs: list[bytes] = []

    async def warm(self) -> None:
        self.warmed = True

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16_000,
        *,
        generation: int = 0,
    ) -> str:
        assert self.warmed
        assert sample_rate == 16_000
        self.inputs.append(pcm16)
        return "testing the call"


class CancelableSTT(FakeSTT):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[int] = []

    async def cancel(self, generation: int) -> bool:
        self.cancelled.append(generation)
        return True


class RecordingTTS(DeterministicTTSStub):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls: list[int | None] = []

    async def cancel(self, generation: int | None = None) -> None:
        self.cancel_calls.append(generation)
        await super().cancel(generation)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.lock = threading.Lock()

    def send(self, payload) -> None:
        with self.lock:
            self.sent.append(payload)


class BlockingSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send(self, payload) -> None:
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(timeout=2)
        super().send(payload)


class BlockingOverloadSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.error_entered = threading.Event()
        self.release_error = threading.Event()

    def send(self, payload) -> None:
        if (
            isinstance(payload, str)
            and json.loads(payload).get("code") == "audio_overload"
        ):
            self.error_entered.set()
            self.release_error.wait(timeout=2)
        super().send(payload)


class ImmediateEndpoint:
    def __init__(self) -> None:
        self.process_calls = 0

    def warm(self) -> None:
        return None

    def reset(self) -> None:
        return None

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        self.process_calls += 1
        return EndpointResult(
            pcm=pcm,
            reason="silence",
            sample_count=len(pcm) // 2,
            trailing_silence_samples=512,
        )

    def finish(self, *, force: bool = False) -> EndpointResult | None:
        return None


class BlockingFailingEndpoint(ImmediateEndpoint):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        self.process_calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        raise RuntimeError("late vad failure")


class BlockingEndpoint(ImmediateEndpoint):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def process_pcm(self, pcm: bytes) -> EndpointResult | None:
        self.process_calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        return EndpointResult(
            pcm=pcm,
            reason="silence",
            sample_count=len(pcm) // 2,
            trailing_silence_samples=0,
        )


def control(control_type: str, **fields) -> str:
    return json.dumps({"type": control_type, **fields})


def mic_frame(sequence: int) -> bytes:
    return AudioFrame(
        AudioHeader(AudioKind.MIC_PCM16, 0, sequence, 16_000, sequence * 200_000),
        b"\x10\x00" * 3_200,
    ).pack()


def test_fake_end_to_end_pipeline_and_stale_generation_drop(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = FakeSocket()
        stt = FakeSTT()
        brain = DeterministicBrainStub("first sentence. second sentence.", chunk_chars=5)
        tts = DeterministicTTSStub(samples_per_sentence=120)
        runtime = CallRuntime(
            stt=stt,
            brain=brain,
            tts=tts,
            endpoint_factory=lambda: SileroEndpointDetector(
                AlwaysSpeech(), pre_roll_ms=0
            ),
            metrics_path=tmp_path / "metrics.jsonl",
        )
        session = CallSession(socket, runtime)
        session.start()
        await session.accept(
            control("call.start", call_id="fake-call", continuity=False)
        )
        await session.accept(control("ptt.begin", generation=1))
        await session.accept(mic_frame(0))
        await session.accept(mic_frame(2))
        await session.accept(control("ptt.end", generation=1))
        await session._message_queue.join()
        await session._audio_queue.join()
        assert session._turn_task is not None
        await session._turn_task

        text_frames = [json.loads(item) for item in socket.sent if isinstance(item, str)]
        binary_frames = [item for item in socket.sent if isinstance(item, bytes)]
        types = [item["type"] for item in text_frames]
        assert "sequence.gap" in types
        assert "stt.result" in types
        assert "brain.delta" in types
        assert "brain.done" in types
        assert "audio.start" in types
        assert "audio.end" in types
        assert stt.inputs
        assert brain.requests[0]["protocol"] == "voice"
        assert brain.requests[0]["call_id"] == "fake-call"
        assert brain.requests[0]["journal"] is False
        assert [sentence for _, sentence in tts.sentences] == [
            "first sentence.",
            "second sentence.",
        ]
        decoded = [parse_audio_frame(frame, inbound=False) for frame in binary_frames]
        assert [frame.header.sequence for frame in decoded] == list(range(len(decoded)))
        assert all(not frame.header.final for frame in decoded)

        before = len(socket.sent)
        session.current_generation = 2
        sent = await session._send_pcm(1, 99, b"\x01\x00", 24_000, final=True)
        assert sent is False
        assert len(socket.sent) == before
        await session.close()

    asyncio.run(scenario())


def test_call_start_can_stream_one_reconnect_safe_greeting(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = FakeSocket()
        brain = DeterministicBrainStub("there you are. good timing.", chunk_chars=4)
        tts = DeterministicTTSStub(samples_per_sentence=120)
        greeting_state = CallGreetingState(tmp_path / "greeting-state.json")
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=brain,
            tts=tts,
            endpoint_factory=ImmediateEndpoint,
            greeting_state=greeting_state,
            metrics_path=tmp_path / "greeting-metrics.jsonl",
        )
        session = CallSession(socket, runtime)
        await session._handle_control(
            {
                "type": "call.start",
                "call_id": "greeting-call",
                "generation": 0,
                "greeting": True,
            }
        )
        for _ in range(100):
            if session._turn_task is not None:
                break
            await asyncio.sleep(0.01)
        assert session._turn_task is not None
        await session._turn_task

        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        types = [item["type"] for item in controls]
        assert types.index("call.ready") < types.index("brain.delta")
        assert "audio.start" in types
        assert "audio.end" in types
        assert brain.requests[0]["call_id"] == "greeting-call"
        assert "<call-open" in brain.requests[0]["text"]
        assert "first-call-today=\"true\"" in brain.requests[0]["text"]
        assert [sentence for _, sentence in tts.sentences] == [
            "there you are.",
            "good timing.",
        ]
        content = next(
            item
            for item in controls
            if item["type"] == "audio.segment" and item["kind"] == "content"
        )
        await session._handle_control(
            {
                "type": "playback.segment_started",
                "generation": 0,
                "sequence": content["sequence"],
                "kind": "content",
                "measurement_point": "playback_head_advanced",
            }
        )

        reconnect = CallSession(FakeSocket(), runtime)
        await reconnect._handle_control(
            {
                "type": "call.start",
                "call_id": "greeting-call",
                "generation": 0,
                "greeting": True,
            }
        )
        await asyncio.sleep(0.05)
        assert reconnect._turn_task is None
        assert len(brain.requests) == 1
        await reconnect.close()
        await session.close()

    asyncio.run(scenario())


def test_reconnect_retries_a_greeting_that_never_reached_playback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        brain = DeterministicBrainStub("i'm here.", chunk_chars=20)
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=brain,
            tts=DeterministicTTSStub(samples_per_sentence=120),
            endpoint_factory=ImmediateEndpoint,
            greeting_state=CallGreetingState(tmp_path / "retry-state.json"),
            metrics_path=tmp_path / "retry-metrics.jsonl",
        )

        first = CallSession(FakeSocket(), runtime)
        await first._handle_control(
            {
                "type": "call.start",
                "call_id": "retry-call",
                "generation": 0,
                "greeting": True,
            }
        )
        for _ in range(100):
            if first._turn_task is not None:
                break
            await asyncio.sleep(0.01)
        assert first._turn_task is not None
        await first._turn_task
        await first.close()

        second_socket = FakeSocket()
        second = CallSession(second_socket, runtime)
        await second._handle_control(
            {
                "type": "call.start",
                "call_id": "retry-call",
                "generation": 1,
                "greeting": True,
            }
        )
        for _ in range(100):
            if second._turn_task is not None:
                break
            await asyncio.sleep(0.01)
        assert second._turn_task is not None
        await second._turn_task
        assert len(brain.requests) == 2

        controls = [
            json.loads(item)
            for item in second_socket.sent
            if isinstance(item, str)
        ]
        content = next(
            item
            for item in controls
            if item["type"] == "audio.segment" and item["kind"] == "content"
        )
        await second._handle_control(
            {
                "type": "playback.segment_started",
                "generation": 1,
                "sequence": content["sequence"],
                "kind": "content",
                "measurement_point": "playback_head_advanced",
            }
        )
        await second.close()

        third = CallSession(FakeSocket(), runtime)
        await third._handle_control(
            {
                "type": "call.start",
                "call_id": "retry-call",
                "generation": 2,
                "greeting": True,
            }
        )
        await asyncio.sleep(0.05)
        assert third._turn_task is None
        assert len(brain.requests) == 2
        await third.close()

    asyncio.run(scenario())


def test_cancelled_async_claim_releases_after_worker_finishes(
    tmp_path: Path,
) -> None:
    class BlockingGreetingState:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.unblock = threading.Event()
            self.active: set[str] = set()

        def claim(self, call_id: str) -> GreetingContext:
            self.entered.set()
            assert self.unblock.wait(timeout=2)
            self.active.add(call_id)
            return GreetingContext(True, 1, "2026-07-16T12:00+00:00")

        def release(self, call_id: str) -> None:
            self.active.discard(call_id)

    async def scenario() -> None:
        state = BlockingGreetingState()
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub("hello."),
            tts=DeterministicTTSStub(samples_per_sentence=120),
            endpoint_factory=ImmediateEndpoint,
            greeting_state=state,
            metrics_path=tmp_path / "claim-race-metrics.jsonl",
        )
        session = CallSession(FakeSocket(), runtime)
        session.call_id = "claim-race"
        task = asyncio.create_task(session._start_greeting())
        assert await asyncio.to_thread(state.entered.wait, 1)

        task.cancel()
        state.unblock.set()
        await asyncio.gather(task, return_exceptions=True)

        assert state.active == set()

    asyncio.run(scenario())


def test_import_does_not_load_heavy_model_modules() -> None:
    import sys

    assert "faster_whisper" not in sys.modules
    assert "kokoro_onnx" not in sys.modules
    assert "silero_vad" not in sys.modules


def test_slow_brain_gets_labelled_backchannel_before_content(
    monkeypatch, tmp_path: Path
) -> None:
    class SlowBrain:
        async def stream_turn(
            self,
            text,
            *,
            call_id,
            turn_id,
            request_id=None,
            journal=True,
        ):
            request_id = request_id or "slow"
            yield BrainEvent("start", request_id, backend="slow-test")
            await asyncio.sleep(0.02)
            yield BrainEvent(
                "delta",
                request_id,
                delta="actual answer.",
                backend="slow-test",
            )
            yield BrainEvent(
                "done",
                request_id,
                say="actual answer.",
                meta=BrainDoneMeta(elapsed=0.02, turns=1),
                backend="slow-test",
            )

    async def scenario() -> None:
        monkeypatch.setenv("SERENA_CALL_BACKCHANNEL_DELAY_MS", "1")
        path = tmp_path / "backchannel.jsonl"
        socket = FakeSocket()
        tts = DeterministicTTSStub(samples_per_sentence=120)
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=SlowBrain(),
            tts=tts,
            endpoint_factory=ImmediateEndpoint,
            metrics_path=path,
        )
        session = CallSession(socket, runtime)
        session._call_started = True
        session.call_id = "backchannel-call"
        session.current_generation = 1
        session.telemetry.speech_end(1, source="test")

        await session._brain_to_tts(1, "hello")

        assert [text for _, text in tts.sentences] == [
            "yeah.",
            "actual answer.",
        ]
        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        segments = [
            item for item in controls if item["type"] == "audio.segment"
        ]
        assert [(item["kind"], item["sequence"]) for item in segments] == [
            ("acknowledgement", 0),
            ("content", 2),
        ]
        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        first = next(row for row in metrics if row["event"] == "audio.first_send")
        content = next(
            row for row in metrics if row["event"] == "audio.first_content_send"
        )
        assert first["kind"] == "acknowledgement"
        assert first["sequence"] == 0
        assert content["sequence"] == 2

    asyncio.run(scenario())


def test_automatic_endpoint_drops_queued_tail_and_duplicate_frames(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket = FakeSocket()
        endpoint = ImmediateEndpoint()
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub("done."),
            tts=DeterministicTTSStub(),
            endpoint_factory=lambda: endpoint,
            metrics_path=tmp_path / "tail-metrics.jsonl",
        )
        session = CallSession(socket, runtime)
        session.start()
        await session.accept(control("call.start", call_id="tail-call"))
        await session.accept(control("ptt.begin", generation=1))
        await session.accept(mic_frame(0))
        await session.accept(mic_frame(0))
        await session.accept(mic_frame(1))
        await session.accept(mic_frame(2))
        await session._message_queue.join()
        await session._audio_queue.join()
        assert session._turn_task is not None
        await session._turn_task
        await session.accept(control("ptt.end", generation=1))
        await session._message_queue.join()
        assert endpoint.process_calls == 1
        text_frames = [json.loads(item) for item in socket.sent if isinstance(item, str)]
        assert any(item["type"] == "sequence.gap" for item in text_frames)
        assert not any(
            item["type"] == "error" and item.get("code") == "protocol"
            for item in text_frames
        )
        await session.close()

    asyncio.run(scenario())


def test_new_generation_serializes_after_inflight_old_audio(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = BlockingSocket()
        tts = DeterministicTTSStub()
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=tts,
            endpoint_factory=ImmediateEndpoint,
            metrics_path=tmp_path / "send-race.jsonl",
        )
        session = CallSession(socket, runtime)
        session.current_generation = 1

        old_send = asyncio.create_task(
            session._send_pcm(1, 0, b"\x01\x00", 24_000, final=False)
        )
        assert await asyncio.to_thread(socket.entered.wait, 1)
        next_generation = asyncio.create_task(session._begin_generation(2))
        await asyncio.sleep(0.01)
        assert not next_generation.done()

        socket.release.set()
        assert await old_send is True
        await next_generation
        assert isinstance(socket.sent[0], bytes)
        assert json.loads(socket.sent[1])["type"] == "generation"
        assert await session._send_pcm(
            1, 1, b"\x02\x00", 24_000, final=False
        ) is False

    asyncio.run(scenario())


def test_model_cancellation_keys_are_isolated_between_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        stt = CancelableSTT()
        tts = RecordingTTS()
        runtime = CallRuntime(
            stt=stt,
            brain=DeterministicBrainStub(),
            tts=tts,
            endpoint_factory=ImmediateEndpoint,
            metrics_path=tmp_path / "namespace.jsonl",
        )
        first = CallSession(FakeSocket(), runtime)
        second = CallSession(FakeSocket(), runtime)
        assert first._tts_generation(1) != second._tts_generation(1)
        assert first._stt_generation(1) != second._stt_generation(1)
        first._turn_generation = 1
        first._turn_task = asyncio.create_task(asyncio.sleep(10))
        await first._cancel_generation(1)
        assert tts.cancel_calls == [first._tts_generation(1)]
        assert stt.cancelled == [first._stt_generation(1)]
        assert second._tts_generation(1) not in tts.cancel_calls
        assert second._stt_generation(1) not in stt.cancelled
        assert not tts.cancelled

    asyncio.run(scenario())


def test_cancel_drops_mic_batches_already_queued_for_vad(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = ImmediateEndpoint()
        stt = FakeSTT()
        runtime = CallRuntime(
            stt=stt,
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=lambda: endpoint,
            metrics_path=tmp_path / "cancelled-input.jsonl",
        )
        session = CallSession(FakeSocket(), runtime)
        session._call_started = True
        session.current_generation = 1
        session._ptt_active = True
        await session._handle_binary(parse_audio_frame(mic_frame(0)))
        await session._cancel_generation(1)

        worker = asyncio.create_task(session._audio_worker())
        await session._audio_queue.join()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        assert endpoint.process_calls == 0
        assert stt.inputs == []
        metrics = [
            json.loads(line)
            for line in (tmp_path / "cancelled-input.jsonl").read_text().splitlines()
        ]
        assert any(row["event"] == "receive.cancelled_drop" for row in metrics)

    asyncio.run(scenario())


def test_cancel_is_not_blocked_by_full_audio_queue(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=tmp_path / "full-audio-queue.jsonl",
        )
        session = CallSession(FakeSocket(), runtime)
        session._call_started = True
        session.current_generation = 1
        session._ptt_active = True
        frame = parse_audio_frame(mic_frame(0))
        for _ in range(session._audio_queue.maxsize):
            session._audio_queue.put_nowait(_BinaryInput(1, frame))

        await asyncio.wait_for(
            session._handle_control({"type": "cancel", "generation": 1}),
            timeout=0.25,
        )
        assert session._audio_queue.qsize() == 1
        assert isinstance(session._audio_queue.get_nowait(), _ResetEndpoint)
        session._audio_queue.task_done()

    from voice.call.orchestrator import _BinaryInput, _ResetEndpoint

    asyncio.run(scenario())


def test_full_audio_queue_fails_generation_before_stt_can_use_corrupt_audio(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "audio-overload.jsonl"
        socket = BlockingOverloadSocket()
        endpoint = BlockingEndpoint()
        stt = FakeSTT()
        runtime = CallRuntime(
            stt=stt,
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=lambda: endpoint,
            metrics_path=path,
        )
        session = CallSession(socket, runtime)
        session._call_started = True
        session.current_generation = 1
        session._ptt_active = True
        frame = parse_audio_frame(mic_frame(0))
        worker = asyncio.create_task(session._audio_worker())
        await session._handle_binary(frame)
        assert await asyncio.to_thread(endpoint.entered.wait, 1)
        overflow_frame = parse_audio_frame(mic_frame(1))
        for _ in range(session._audio_queue.maxsize):
            session._audio_queue.put_nowait(_BinaryInput(1, overflow_frame))

        overload_task = asyncio.create_task(session._handle_binary(overflow_frame))
        assert await asyncio.to_thread(socket.error_entered.wait, 1)
        endpoint.release.set()
        await asyncio.sleep(0.02)
        assert stt.inputs == []
        socket.release_error.set()
        await overload_task

        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        overload = next(
            item
            for item in controls
            if item.get("type") == "error"
            and item.get("code") == "audio_overload"
        )
        assert overload["fatal"] is True
        assert overload["generation"] == 1
        assert not session._ptt_active
        assert session._closed_input_generation == 1
        assert 1 in session._cancelled_generations
        await session._audio_queue.join()
        assert stt.inputs == []

        await session._begin_generation(2)
        fresh_pcm = b"\x22\x00" * 3_200
        await session._handle_binary(
            AudioFrame(
                AudioHeader(AudioKind.MIC_PCM16, 0, 0, 16_000, 0), fresh_pcm
            )
        )
        await session._audio_queue.join()
        assert session._turn_task is not None
        await session._turn_task
        assert stt.inputs == [fresh_pcm]

        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        assert any(
            row["event"] == "queue.depth" and row.get("full") is True
            for row in metrics
        )
        assert any(row["event"] == "endpoint.cancelled_drop" for row in metrics)
        assert any(row["event"] == "generation.cancel" for row in metrics)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await session.close()

    from voice.call.orchestrator import _BinaryInput

    asyncio.run(scenario())


def test_cancel_suppresses_late_vad_error_and_retires_timing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "late-vad-error.jsonl"
        socket = FakeSocket()
        endpoint = BlockingFailingEndpoint()
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=lambda: endpoint,
            metrics_path=path,
        )
        session = CallSession(socket, runtime)
        session._call_started = True
        session.current_generation = 1
        session._ptt_active = True
        session.telemetry.speech_end(1, source="test")
        session.telemetry.stage(1, "test.stage")
        session.telemetry._first_audio_ns[1] = 1
        worker = asyncio.create_task(session._audio_worker())
        await session._handle_binary(parse_audio_frame(mic_frame(0)))
        assert await asyncio.to_thread(endpoint.entered.wait, 1)

        await session._cancel_generation(1)
        endpoint.release.set()
        await session._audio_queue.join()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        assert not any(
            item.get("type") == "error"
            and item.get("code") == "audio_processing"
            for item in controls
        )
        assert 1 not in session.telemetry._speech_end_ns
        assert 1 not in session.telemetry._first_audio_ns
        assert 1 not in session.telemetry._stage_ns

    asyncio.run(scenario())


def test_playback_ack_requires_current_server_pcm(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "stale-playback.jsonl"
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=path,
        )
        session = CallSession(FakeSocket(), runtime)
        session.start()
        await session.accept(control("call.start", call_id="stale-ack"))
        await session.accept(control("ptt.begin", generation=1))
        await session.accept(
            control(
                "playback.started",
                generation=1,
                sequence=0,
                timestamp_us=50,
                eou_to_playback_ms=1.0,
                measurement_point="playback_head_advanced",
            )
        )
        await session._message_queue.join()
        await session.close()

        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        assert any(row["event"] == "playback.ack_rejected" for row in metrics)
        assert not any(
            row["event"] == "latency.eou_first_playable_phone"
            for row in metrics
        )

    asyncio.run(scenario())


def test_first_playback_ack_before_later_pcm_does_not_reset_first_audio(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "early-playback-ack.jsonl"
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=path,
        )
        session = CallSession(FakeSocket(), runtime)
        session._call_started = True
        session.current_generation = 1
        session.telemetry.speech_end(1, source="test")

        assert await session._send_pcm(
            1, 0, b"\x01\x00", 24_000, final=False
        )
        await session._handle_control(
            json.loads(
                control(
                    "playback.started",
                    generation=1,
                    sequence=0,
                    timestamp_us=50,
                    eou_to_playback_ms=1.0,
                    measurement_point="playback_head_advanced",
                )
            )
        )
        assert await session._send_pcm(
            1, 1, b"\x02\x00", 24_000, final=False
        )
        await session._handle_control(
            json.loads(
                control(
                    "playback.segment_started",
                    generation=1,
                    sequence=0,
                    kind="content",
                    timestamp_us=55,
                    eou_to_playback_ms=1.1,
                    measurement_point="playback_head_advanced",
                )
            )
        )

        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        assert sum(row["event"] == "audio.first_send" for row in metrics) == 1
        assert session._first_output_sequence[1] == 0
        assert 1 in session.telemetry._speech_end_ns

        session._audio_ended_generations.add(1)
        session._retire_completed_generation(1)
        assert 1 not in session._first_output_sequence
        assert 1 not in session.telemetry._speech_end_ns

    asyncio.run(scenario())


def test_acknowledgement_and_content_playback_are_measured_separately(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "content-playback.jsonl"
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=path,
        )
        session = CallSession(FakeSocket(), runtime)
        session._call_started = True
        session.current_generation = 1
        session.telemetry.speech_end(1, source="test")

        assert await session._send_pcm(
            1,
            0,
            b"\x01\x00",
            24_000,
            final=False,
            segment_kind="acknowledgement",
        )
        assert await session._send_pcm(
            1,
            1,
            b"\x02\x00",
            24_000,
            final=False,
            segment_kind="content",
        )
        session._audio_ended_generations.add(1)
        await session._handle_control(
            json.loads(
                control(
                    "playback.started",
                    generation=1,
                    sequence=0,
                    timestamp_us=50,
                    eou_to_playback_ms=900.0,
                    measurement_point="playback_head_advanced",
                )
            )
        )
        assert 1 in session.telemetry._speech_end_ns

        await session._handle_control(
            json.loads(
                control(
                    "playback.segment_started",
                    generation=1,
                    sequence=1,
                    kind="content",
                    timestamp_us=75,
                    eou_to_playback_ms=1_650.0,
                    measurement_point="playback_head_advanced",
                )
            )
        )

        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        first_phone = next(
            row
            for row in metrics
            if row["event"] == "latency.eou_first_playable_phone"
        )
        content_phone = next(
            row
            for row in metrics
            if row["event"]
            == "latency.eou_first_content_playable_phone"
        )
        assert first_phone["elapsed_ms"] == 900.0
        assert first_phone["kind"] == "acknowledgement"
        assert content_phone["elapsed_ms"] == 1_650.0
        assert 1 not in session.telemetry._speech_end_ns

    asyncio.run(scenario())


def test_call_hello_is_one_shot_across_duplicate_acks_and_reconnect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "one-shot-hello.jsonl"
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            hello_state=CallHelloState(tmp_path / "one-shot-hello.sqlite3"),
            metrics_path=path,
        )

        async def acknowledge(session: CallSession, generation: int) -> None:
            session._call_started = True
            session.call_id = "hello-call"
            session.telemetry = runtime.make_telemetry(session.call_id)
            session.current_generation = generation
            session._first_content_output_sequence[generation] = 0
            await session._handle_control(
                {
                    "type": "playback.segment_started",
                    "generation": generation,
                    "sequence": 0,
                    "kind": "content",
                    "measurement_point": "playback_head_advanced",
                    "call_hello": True,
                    "cold_start": True,
                    "app_uptime_ms": 3_900,
                }
            )

        first = CallSession(FakeSocket(), runtime)
        await acknowledge(first, 1)
        await acknowledge(first, 1)
        await first.close(reusable_endpoint=False)
        for index in range(1_024):
            assert runtime.claim_call_hello(f"other-call-{index}") is True

        reconnect = CallSession(FakeSocket(), runtime)
        await acknowledge(reconnect, 2)
        await reconnect.close(reusable_endpoint=True)

        metrics = [json.loads(line) for line in path.read_text().splitlines()]
        hellos = [row for row in metrics if row["event"] == "call.hello"]
        assert len(hellos) == 1
        assert hellos[0]["app_uptime_ms"] == 3_900

    asyncio.run(scenario())
