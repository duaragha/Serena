import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
)

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_events import ClaudeEvents


@pytest.fixture(autouse=True)
def installed_binary(monkeypatch):
    monkeypatch.setattr("core.workspace_claude.shutil.which", lambda name: "/controlled/claude")


class Client:
    def __init__(self, options):
        self.options = options
        self._transport = SimpleNamespace(_process=SimpleNamespace(pid=12345))
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.fail_send = False
        self.interrupted = False

    async def connect(self):
        self.open_task = asyncio.current_task()

    async def query(self, messages, session_id):
        async for message in messages:
            self.sent.append((session_id, message))
        if self.fail_send:
            raise TimeoutError("ambiguous delivery")

    async def receive_messages(self):
        while True:
            yield await self.messages.get()

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        assert self.open_task is asyncio.current_task()
        self.closed = True


def make(tmp_path):
    events = []
    lease = SimpleNamespace(launching=lambda: None, bind=lambda pid: None, release=lambda: None)

    async def publish(event):
        events.append(event)

    owner = ClaudeWorkspace(
        session_id="exact",
        cwd=tmp_path,
        publish=publish,
        client_factory=Client,
        lease_factory=lambda sid: lease,
        session_info=lambda sid, directory: SimpleNamespace(session_id=sid),
        history=lambda sid, directory: [],
    )
    return owner, events


def test_exact_resume_configuration_and_billing_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_CODE_USE_FUTURE_PROVIDER", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-fixture")

    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            options = owner.client.options
            assert options.cli_path == "/controlled/claude"
            assert options.resume == "exact" and not options.fork_session
            assert options.setting_sources == ["user", "project", "local"]
            assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
            assert options.allowed_tools == [] and options.disallowed_tools == []
            assert options.env["ANTHROPIC_API_KEY"] == ""
            assert options.env["CLAUDE_CODE_USE_FUTURE_PROVIDER"] == ""
            assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-fixture"
            assert events[0]["method"] == "workspace/history"
            await owner.submit([{"type": "text", "text": "hello"}])
            sid, sent = owner.client.sent[0]
            assert sid == sent["session_id"] == "exact"
            assert sent["message"]["content"] == [{"type": "text", "text": "hello"}]
            assert owner.state == "running"
            await owner.interrupt()
            assert owner.client.interrupted
        finally:
            await owner.close()
        assert owner.client.closed

    asyncio.run(run())


@pytest.mark.parametrize("native_id", [None, "different"])
def test_missing_or_mismatched_native_session_never_launches(tmp_path, native_id):
    async def run():
        owner, events = make(tmp_path)
        owner.session_info = lambda *args, **kwargs: (
            None if native_id is None else SimpleNamespace(session_id=native_id)
        )
        with pytest.raises(ValueError, match="Exact native"):
            await owner.open()
        assert owner.client is None and owner._owner_task is None
        assert events == []

    asyncio.run(run())


def test_clarifying_answers_preserve_question_contract_and_reject_empty_approval(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        inputs = {"questions": [{"question": "Which database?", "multiSelect": False}]}
        task = asyncio.create_task(
            owner._permission(
                "AskUserQuestion", inputs, ToolPermissionContext(tool_use_id="question")
            )
        )
        await asyncio.sleep(0)
        assert not task.done()
        for invalid in [
            {"decision": "allow"},
            {"answers": {}},
            {"answers": {"Which database?": ""}},
        ]:
            with pytest.raises(ValueError):
                await owner.answer("question", invalid)
        assert not task.done()
        await owner.answer("question", {"answers": {"Which database?": "Postgres"}})
        result = await task
        assert result.behavior == "allow"
        assert result.updated_input == {**inputs, "answers": {"Which database?": "Postgres"}}
        assert "answers" not in inputs
        assert not owner.questions and not owner.question_inputs
        assert events[-1]["method"] == "serverRequest/resolved"
        with pytest.raises(ValueError, match="no longer pending"):
            await owner.answer("question", {"answers": {"Which database?": "SQLite"}})

    asyncio.run(run())


def test_ambiguous_send_cannot_repeat_and_wrong_identity_closes(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            owner.client.fail_send = True
            with pytest.raises(TimeoutError):
                await owner.submit([{"type": "text", "text": "once"}])
            assert owner.state == "uncertain"
            with pytest.raises(RuntimeError):
                await owner.submit([{"type": "text", "text": "again"}])
            await owner.client.messages.put(
                SystemMessage(subtype="init", data={"session_id": "wrong"})
            )
            await asyncio.wait_for(owner._owner_task, 1)
            assert owner.client.closed
            assert any("different session" in e.get("params", {}).get("reason", "") for e in events)
        finally:
            await owner.close()

    asyncio.run(run())


def test_permissions_wait_for_user_reject_stale_and_cleanup_denies(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            question = asyncio.create_task(
                owner._permission(
                    "Bash", {"command": "echo hi"}, ToolPermissionContext(tool_use_id="q")
                )
            )
            await asyncio.sleep(0)
            assert not question.done()
            with pytest.raises(ValueError):
                await owner.answer("q", {"decision": "bypass"})
            await owner.answer("q", {"decision": "allow"})
            assert (await question).behavior == "allow"
            with pytest.raises(ValueError):
                await owner.answer("q", {"decision": "allow"})
            waiting = asyncio.create_task(
                owner._permission("Edit", {}, ToolPermissionContext(tool_use_id="pending"))
            )
            await asyncio.sleep(0)
            await owner.close()
            assert (await waiting).behavior == "deny"
            assert any(e["method"] == "serverRequest/resolved" for e in events)
        finally:
            await owner.close()

    asyncio.run(run())


def test_real_sdk_messages_stream_to_same_item_and_complete(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            await owner.submit([{"type": "text", "text": "hello"}])
            for event in [
                StreamEvent(
                    uuid="e1",
                    session_id="exact",
                    event={"type": "message_start", "message": {"id": "m"}},
                ),
                StreamEvent(
                    uuid="e2",
                    session_id="exact",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "partial"},
                    },
                ),
                AssistantMessage(
                    content=[TextBlock(text="complete")],
                    model="actual-claude-model",
                    message_id="m",
                    session_id="exact",
                ),
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="exact",
                ),
            ]:
                await owner.client.messages.put(event)
            for _ in range(20):
                if owner.state == "ready":
                    break
                await asyncio.sleep(0.01)
            assert owner.state == "ready"
            delta = next(e for e in events if e["method"] == "item/agentMessage/delta")
            final = next(
                e
                for e in events
                if e["method"] == "item/completed" and e["params"]["item"]["type"] == "agentMessage"
            )
            assert delta["params"]["itemId"] == final["params"]["item"]["id"] == "m:0"
            assert final["params"]["item"]["text"] == "complete"
            assert any(
                e["method"] == "workspace/settings"
                and e["params"]["model"] == "actual-claude-model"
                for e in events
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_history_merges_tool_result_without_losing_tool_input():
    converter = ClaudeEvents("exact")
    records = [
        {"session_id": "exact", "uuid": "u", "type": "user", "message": {"content": "hello"}},
        {
            "session_id": "exact",
            "uuid": "a",
            "type": "assistant",
            "message": {
                "id": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    }
                ],
            },
        },
        {
            "session_id": "exact",
            "uuid": "r",
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool",
                        "content": "hi",
                        "is_error": False,
                    }
                ]
            },
        },
    ]
    items = converter.history(records)["params"]["thread"]["turns"][0]["items"]
    assert len(items) == 2
    assert items[1]["tool"] == "Bash" and items[1]["input"]["command"] == "echo hi"
    assert items[1]["output"] == "hi" and items[1]["status"] == "completed"
    records[0]["session_id"] = "wrong"
    with pytest.raises(ValueError):
        converter.history(records)
