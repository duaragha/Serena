from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from voice.call.brain import DeterministicBrainStub
from voice.call.orchestrator import CallRuntime, CallSession, serve_websocket
from voice.call.tts import DeterministicTTSStub


class _Endpoint:
    def warm(self) -> None:
        return None

    def reset(self) -> None:
        return None

    def process_pcm(self, pcm: bytes):
        return None

    def finish(self, *, force: bool = False):
        return None


class _CountingEndpoint(_Endpoint):
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self, *, reusable: bool = False) -> None:
        self.close_calls += 1


class _BlockingSTT:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    async def warm(self) -> None:
        self.entered.set()
        await asyncio.to_thread(self.release.wait)


class _ReadySTT:
    async def warm(self) -> None:
        return None


class _BrokenSocket:
    def send(self, payload: str | bytes) -> None:
        raise RuntimeError("socket write failed")


class _HangupDropSocket:
    def __init__(self) -> None:
        self.inputs = iter(
            [
                json.dumps({"type": "call.start", "call_id": "send-failure"}),
                json.dumps({"type": "hangup"}),
            ]
        )

    def receive(self):
        return next(self.inputs, None)

    def send(self, payload: str | bytes) -> None:
        if (
            isinstance(payload, str)
            and json.loads(payload).get("type") == "call.ended"
        ):
            raise RuntimeError("peer vanished before call.ended")


class _BlockingGreetingState:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.active: set[str] = set()

    def claim(self, call_id: str):
        from voice.call.greeting import GreetingContext

        self.entered.set()
        assert self.unblock.wait(timeout=2)
        self.active.add(call_id)
        return GreetingContext(True, 1, "2026-07-16T12:00+00:00")

    def release(self, call_id: str) -> None:
        self.active.discard(call_id)


def _runtime(tmp_path: Path, *, stt: object) -> CallRuntime:
    return CallRuntime(
        stt=stt,
        brain=DeterministicBrainStub(),
        tts=DeterministicTTSStub(),
        endpoint_factory=_Endpoint,
        metrics_path=tmp_path / "runtime-resilience.jsonl",
    )


def test_cancelled_warm_caller_cannot_poison_a_later_event_loop(
    tmp_path: Path,
) -> None:
    stt = _BlockingSTT()
    runtime = _runtime(tmp_path, stt=stt)

    async def cancel_first_loop() -> None:
        first_caller = asyncio.create_task(runtime.warm())
        assert await asyncio.to_thread(stt.entered.wait, 1)

        first_caller.cancel()
        await asyncio.gather(first_caller, return_exceptions=True)

    asyncio.run(cancel_first_loop())
    assert not runtime._warm_done.is_set()
    stt.release.set()

    status = asyncio.run(runtime.warm())
    assert status == {"stt": "ready", "tts": "ready", "vad": "ready"}


def test_background_send_failure_is_retrieved_and_recorded(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        runtime = _runtime(tmp_path, stt=object())
        session = CallSession(_BrokenSocket(), runtime)
        session._call_started = True

        await session._handle_control({"type": "ping", "nonce": "probe"})
        for _ in range(100):
            if not session._background:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)

        assert not session._background
        assert unhandled == []
        rows = [
            json.loads(line)
            for line in runtime.metrics_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(
            row["event"] == "task.error"
            and row["error_type"] == "RuntimeError"
            and row["error"] == "socket write failed"
            for row in rows
        )

    asyncio.run(scenario())


def test_hangup_send_failure_still_closes_workers(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path, stt=_ReadySTT())

        await asyncio.wait_for(
            serve_websocket(_HangupDropSocket(), runtime=runtime), timeout=1
        )

        leaked = [
            task
            for task in asyncio.all_tasks()
            if not task.done() and task.get_name() in {"call-messages", "call-audio"}
        ]
        assert leaked == []

    asyncio.run(scenario())


def test_cancelled_close_waits_for_owned_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = _CountingEndpoint()
        greeting_state = _BlockingGreetingState()
        runtime = CallRuntime(
            stt=_ReadySTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=lambda: endpoint,
            greeting_state=greeting_state,
            metrics_path=tmp_path / "cancelled-close.jsonl",
        )
        session = CallSession(_HangupDropSocket(), runtime)
        session.call_id = "cancelled-close"
        session.start()
        greeting = session._spawn(
            session._start_greeting(), "call-greeting-cancelled-close"
        )
        assert await asyncio.to_thread(greeting_state.entered.wait, 1)

        closing = asyncio.create_task(session.close())
        for _ in range(100):
            if greeting.cancelling():
                break
            await asyncio.sleep(0.001)
        assert greeting.cancelling()

        closing.cancel()
        asyncio.get_running_loop().call_later(0.01, greeting_state.unblock.set)
        result = await asyncio.gather(closing, return_exceptions=True)

        assert isinstance(result[0], asyncio.CancelledError)
        assert session._close_done.is_set()
        assert endpoint.close_calls == 1
        assert greeting_state.active == set()
        assert greeting.done()
        await session.close()
        assert endpoint.close_calls == 1

    asyncio.run(scenario())
