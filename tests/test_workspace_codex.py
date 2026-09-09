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
            return {
                "thread": {"id": self.sid, "turns": []},
                "model": "chosen-model",
                "reasoningEffort": "high",
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "chosen-model",
                        "displayName": "Chosen model",
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high"},
                            {"reasoningEffort": "low"},
                        ],
                    }
                ],
                "nextCursor": None,
            }
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


def test_background_tasks_paginate_and_stop_only_exact_session_process(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []

        async def request(method, params):
            calls.append((method, params))
            assert params["threadId"] == "exact-session"
            if method.endswith("/terminate"):
                return {"terminated": True}
            if params.get("cursor") == "next":
                return {"data": [], "nextCursor": None}
            return {
                "data": [
                    {"processId": "p1", "itemId": "i1", "command": "sleep 30", "cwd": "/project"}
                ],
                "nextCursor": "next",
            }

        rpc.request = request
        try:
            assert len((await client.list_background_tasks())["data"]) == 1
            assert (await client.terminate_background_task("p1"))["terminated"]
            with pytest.raises(ValueError, match="no longer running"):
                await client.terminate_background_task("foreign-process")
            stops = [p for m, p in calls if m.endswith("/terminate")]
            assert stops == [{"threadId": "exact-session", "processId": "p1"}]
            assert client.state == "ready"
            assert not any(m in {"turn/start", "turn/interrupt"} for m, _ in calls)

            async def looping(method, params):
                return {"data": [], "nextCursor": "loop"}

            rpc.request = looping
            with pytest.raises(WorkspaceRpcError, match="pagination"):
                await client.list_background_tasks()
        finally:
            await client.close()

    asyncio.run(run())


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


def test_model_discovery_and_unsupported_effort_never_starts_turn(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex")
            catalog = await client.list_models()
            assert catalog["settings"] == {"model": "chosen-model", "reasoningEffort": "high"}
            assert catalog["data"][0]["model"] == "chosen-model"
            assert events[-1]["method"] == "workspace/models"
            for options in (
                {"model": "invented"},
                {"model": "chosen-model", "effort": "unsupported"},
                {"serviceTier": "invented-fast"},
            ):
                with pytest.raises(ValueError):
                    await client.submit([{"type": "text", "text": "message"}], options=options)
            assert not any(method == "turn/start" for method, _ in rpc.calls)
            assert client.state == "ready"
            await client.submit([{"type": "text", "text": "message"}], options={"effort": "low"})
            assert rpc.calls[-1][1]["effort"] == "low"
            assert "model" not in rpc.calls[-1][1]
            assert events[-1] == {
                "method": "workspace/settings",
                "params": {"model": "chosen-model", "reasoningEffort": "low"},
            }
        finally:
            await client.close()

    asyncio.run(run())


def test_changed_model_without_effort_uses_advertised_default(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex")
            client.model_catalog = [
                {
                    "model": "new-model",
                    "defaultReasoningEffort": "low",
                    "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    "serviceTiers": [{"id": "fast"}],
                }
            ]
            await client.submit(
                [{"type": "text", "text": "hello"}],
                options={"model": "new-model", "serviceTier": "fast"},
            )
            assert rpc.calls[-1][1]["effort"] == "low"
            assert rpc.calls[-1][1]["serviceTier"] == "fast"
        finally:
            await client.close()

    asyncio.run(run())


def test_model_catalog_pagination_and_loop_rejection(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex")
            original = rpc.request
            repeat = False

            async def paged(method, params):
                if method != "model/list":
                    return await original(method, params)
                if params.get("cursor"):
                    return {"data": [{"model": "second"}], "nextCursor": "next" if repeat else None}
                return {"data": [{"model": "first"}], "nextCursor": "next"}

            rpc.request = paged
            assert [m["model"] for m in (await client.list_models())["data"]] == ["first", "second"]
            repeat = True
            with pytest.raises(WorkspaceRpcError, match="pagination"):
                await client.list_models()
        finally:
            await client.close()

    asyncio.run(run())


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
            previous_calls = list(rpc.calls)
            with pytest.raises(WorkspaceRpcError, match="turn changed"):
                await client.steer(
                    [{"type": "text", "text": "correction"}], expected_turn_id="old-turn"
                )
            assert rpc.calls == previous_calls
            await client.steer([{"type": "text", "text": "correction"}], expected_turn_id="turn-1")
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


def test_review_stays_inline_and_rejects_busy_or_unknown_targets(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            original = rpc.request

            async def request(method, params):
                if method == "review/start":
                    rpc.calls.append((method, params))
                    return {"reviewThreadId": "exact-session", "turn": {"id": "review-1"}}
                return await original(method, params)

            rpc.request = request
            with pytest.raises(ValueError):
                await client.review({"type": "custom", "instructions": ""})
            result = await client.review({"type": "baseBranch", "branch": "main"})
            assert result["reviewThreadId"] == "exact-session"
            assert rpc.calls[-1] == (
                "review/start",
                {
                    "threadId": "exact-session",
                    "delivery": "inline",
                    "target": {"type": "baseBranch", "branch": "main"},
                },
            )
            assert client.active_turn == "review-1"
            with pytest.raises(WorkspaceRpcError, match="not ready"):
                await client.review({"type": "uncommittedChanges"})
        finally:
            await client.close()

    asyncio.run(run())


def test_compaction_ack_does_not_make_session_ready_before_completion(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})

            async def request(method, params):
                rpc.calls.append((method, params))
                return {}

            rpc.request = request
            await client.compact()
            assert rpc.calls[-1] == ("thread/compact/start", {"threadId": "exact-session"})
            assert client.state == "submitting"
            with pytest.raises(WorkspaceRpcError, match="not ready"):
                await client.submit([{"type": "text", "text": "too early"}])
            await rpc.events.put(
                {
                    "method": "turn/started",
                    "params": {"threadId": "exact-session", "turn": {"id": "compact-1"}},
                }
            )
            await rpc.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "exact-session",
                        "turn": {"id": "compact-1", "status": "completed"},
                    },
                }
            )
            await asyncio.sleep(0.01)
            assert client.state == "ready"
        finally:
            await client.close()

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


def test_permission_grants_cannot_expand_profile_or_drop_deny_entries(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        permissions = {
            "network": {"enabled": True},
            "fileSystem": {
                "entries": [
                    {"access": "write", "path": {"type": "path", "path": "/project"}},
                    {"access": "deny", "path": {"type": "path", "path": "/project/private"}},
                ]
            },
        }
        question = {
            "method": "item/permissions/requestApproval",
            "params": {"permissions": permissions},
        }
        for granted in (
            {"network": {"enabled": False}},
            {"fileSystem": {"write": ["/"]}},
            {"fileSystem": {"entries": permissions["fileSystem"]["entries"][:1]}},
            {"unexpected": {}},
        ):
            client.questions[7] = question
            with pytest.raises(ValueError):
                await client.answer(7, {"permissions": granted, "scope": "turn"})
        assert not rpc.calls
        for scope, granted in (
            ("turn", {}),
            ("turn", {"network": permissions["network"]}),
            ("session", permissions),
        ):
            client.questions[7] = question
            await client.answer(7, {"permissions": granted, "scope": scope})
            assert rpc.calls[-1] == ("answer", (7, {"permissions": granted, "scope": scope}))
            assert 7 not in client.questions
        with pytest.raises(WorkspaceRpcError, match="no longer pending"):
            await client.answer(7, {"permissions": permissions, "scope": "session"})

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
