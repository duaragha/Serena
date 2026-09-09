import asyncio
from types import SimpleNamespace

import pytest

from core.workspace_codex import CodexWorkspace
from core.workspace_rpc import WorkspaceRpcError


class Rpc:
    def __init__(self):
        self.events = asyncio.Queue()
        self.calls = []
        self.sid = "exact-session"
        self.closed = False
        self.race = False
        self.timeout = False
        self.process = SimpleNamespace(pid=12345)

    async def start(self, command, **kwargs):
        self.command, self.options = command, kwargs

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "thread/resume":
            return {"thread": {"id": self.sid, "turns": []}}
        if method == "turn/start":
            if self.timeout:
                raise TimeoutError()
            if self.race:
                await self.events.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": self.sid,
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    }
                )
                await asyncio.sleep(0)
            return {"turn": {"id": "turn-1"}}
        return {}

    async def notify(self, method, params):
        self.calls.append((method, params))

    async def answer(self, request_id, answer):
        self.calls.append(("answer", (request_id, answer)))

    async def close(self):
        self.closed = True


async def make(tmp_path):
    events = []

    async def publish(event):
        events.append(event)

    rpc = Rpc()
    lease = SimpleNamespace(launching=lambda: None, bind=lambda pid: None, release=lambda: None)
    client = CodexWorkspace(
        session_id=rpc.sid, cwd=tmp_path, publish=publish, rpc=rpc, lease_factory=lambda sid: lease
    )
    return client, rpc, events


def test_exact_resume_and_real_turn_controls(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={"OPENAI_API_KEY": "must-not-pass"})
            assert "OPENAI_API_KEY" not in rpc.options["env"]
            assert "--disable" not in rpc.command
            assert rpc.calls[2] == ("thread/resume", {"threadId": "exact-session"})
            inputs = [
                {"type": "text", "text": "hello"},
                {"type": "localImage", "path": "/photo.png"},
            ]
            await client.submit(inputs, options={"model": "chosen-model", "effort": "high"})
            assert rpc.calls[-1][1]["input"] == inputs
            assert rpc.calls[-1][1]["threadId"] == "exact-session"
            with pytest.raises(WorkspaceRpcError):
                await client.submit(inputs)
            await client.steer([{"type": "text", "text": "correction"}])
            assert rpc.calls[-1][1]["expectedTurnId"] == "turn-1"
            await client.interrupt()
            assert rpc.calls[-1] == (
                "turn/interrupt",
                {"threadId": "exact-session", "turnId": "turn-1"},
            )
            assert not rpc.closed
            assert events[0]["method"] == "workspace/history"
        finally:
            await client.close()

    asyncio.run(run())


def test_wrong_resume_never_creates_fallback_session(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        rpc.sid = "wrong-session"
        with pytest.raises(WorkspaceRpcError, match="different session"):
            await client.open(binary="codex", env={})
        assert rpc.closed
        assert not any(method == "thread/start" for method, _ in rpc.calls)

    asyncio.run(run())


def test_fast_completion_is_not_overwritten_by_start_reply(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            rpc.race = True
            await client.open(binary="codex", env={})
            await client.submit([{"type": "text", "text": "go"}])
            assert client.state == "ready"
            assert client.active_turn is None
        finally:
            await client.close()

    asyncio.run(run())


def test_ambiguous_submission_cannot_be_retried_as_new_turn(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            rpc.timeout = True
            with pytest.raises(TimeoutError):
                await client.submit([{"type": "text", "text": "go"}])
            assert client.state == "uncertain"
            with pytest.raises(WorkspaceRpcError):
                await client.submit([{"type": "text", "text": "go"}])
            assert sum(method == "turn/start" for method, _ in rpc.calls) == 1
            assert not rpc.closed
        finally:
            await client.close()

    asyncio.run(run())


def test_approval_validation_and_stale_resolution(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            event = {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": rpc.sid, "command": "git status"},
            }
            await rpc.events.put(event)
            await asyncio.sleep(0)
            assert events[-1] == event
            with pytest.raises(ValueError):
                await client.answer(7, {"decision": "yes"})
            assert 7 in client.questions
            await client.answer(7, {"decision": "decline"})
            with pytest.raises(WorkspaceRpcError):
                await client.answer(7, {"decision": "accept"})
            await rpc.events.put(event)
            await rpc.events.put(
                {
                    "method": "serverRequest/resolved",
                    "params": {"threadId": rpc.sid, "requestId": 7},
                }
            )
            await asyncio.sleep(0)
            assert 7 not in client.questions
        finally:
            await client.close()

    asyncio.run(run())
