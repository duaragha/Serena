import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_events import ClaudeEvents


@pytest.fixture
def tmp_path(tmp_path):
    # Match the resolved project directory sent by the owner on every platform.
    return tmp_path.resolve()


@pytest.fixture(autouse=True)
def installed_binary(monkeypatch):
    from shutil import which

    monkeypatch.setattr("core.workspace_claude.shutil.which", lambda name: "/controlled/claude" if name == "claude" else which(name))


class Client:
    def __init__(self, options):
        self.options = options
        self._transport = SimpleNamespace(_process=SimpleNamespace(pid=12345))
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.fail_send = False
        self.interrupted = False
        self.models_set = []

    async def get_server_info(self):
        return {"models": [{"value": "sonnet", "displayName": "Sonnet"}]}

    async def set_model(self, model):
        self.models_set.append(model)

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


def test_runtime_observer_is_installed_before_client_connect(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        callbacks = []
        class ObservedClient(Client):
            def observe_runtime(self, callback):
                callbacks.append(callback)
            async def connect(self):
                assert callbacks == [owner._lease.bind]
                await super().connect()
        owner.client_factory = ObservedClient
        await owner.open()
        await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize("failed", [False, True])
def test_rename_catalog_signal_requires_exact_successful_turn(tmp_path, failed):
    async def run():
        owner, events = make(tmp_path)
        await owner.open()
        try:
            await owner.submit([{"type": "text", "text": "/rename New name"}])
            await owner.client.messages.put(ResultMessage(
                subtype="error" if failed else "success", duration_ms=1, duration_api_ms=0,
                is_error=failed, num_turns=0, session_id="exact"))
            for _ in range(50):
                if not owner._pending_renames:
                    break
                await asyncio.sleep(.01)
            assert not owner._pending_renames
            renamed = [event for event in events if event["method"] == "workspace/renameCompleted"]
            assert len(renamed) == (0 if failed else 1)
            if renamed:
                assert renamed[0]["params"]["title"] == "New name"
        finally:
            await owner.close()
    asyncio.run(run())


def test_queued_input_uses_same_client_and_preserves_running_turn(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        await owner.open()
        try:
            first = (await owner.submit([{"type": "text", "text": "first"}]))["turn"]["id"]
            with pytest.raises(RuntimeError, match="changed"):
                await owner.queue_input([{"type": "text", "text": "wrong"}], expected_turn_id="old")
            content = [{"type": "text", "text": "follow-up"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "bytes"}}]
            second = (await owner.queue_input(content, expected_turn_id=first))["turn"]["id"]
            assert first != second and owner.active_turn == first and owner.state == "running"
            assert len(owner.client.sent) == 2
            assert owner.client.sent[1][1]["message"]["content"] == content
            assert owner.client.models_set == []
            for input_id in [first, second]:
                await owner.client.messages.put({"type": "result", "session_id": "exact",
                                                  "user_message_uuid": input_id, "is_error": False})
                await asyncio.sleep(0)
                assert owner.active_turn == (second if input_id == first else None)
            assert owner.state == "ready"
            with pytest.raises(RuntimeError, match="changed"):
                await owner.queue_input(content, expected_turn_id=first)
            assert len(owner.client.sent) == 2
        finally:
            await owner.close()
    asyncio.run(run())


def test_uncertain_queued_delivery_cannot_be_retried_or_started_as_new(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        await owner.open()
        try:
            first = (await owner.submit([{"type": "text", "text": "first"}]))["turn"]["id"]
            owner.client.fail_send = True
            with pytest.raises(TimeoutError):
                await owner.queue_input([{"type": "text", "text": "follow-up"}], expected_turn_id=first)
            assert owner.state == "uncertain" and len(owner.events.pending_inputs) == 2
            with pytest.raises(RuntimeError):
                await owner.queue_input([{"type": "text", "text": "follow-up"}], expected_turn_id=first)
            assert len(owner.client.sent) == 2
            await owner.client.messages.put({"type": "result", "session_id": "exact",
                                              "user_message_uuid": first, "is_error": False})
            await asyncio.sleep(0)
            assert owner.state == "uncertain"
            queued = owner.events.pending_inputs[0]
            await owner.client.messages.put({"type": "result", "session_id": "exact",
                                              "user_message_uuid": queued, "is_error": False})
            await asyncio.sleep(0)
            assert owner.state == "ready" and owner._uncertain_input is None
        finally:
            await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "checkpoint", "native"])
def test_creation_checkpoints_leased_identity_before_single_native_launch(tmp_path, failure):
    async def run():
        owner, events = make(tmp_path)
        owner.session_id = "new:de6ddc26-7e96-42bb-86db-6c39ed682758"
        steps = []
        class CreatingClient(Client):
            async def connect(self):
                raise AssertionError("Fresh creation must not resume")
            async def create(self):
                self.open_task = asyncio.current_task()
                steps.append("create")
                if failure == "native":
                    raise RuntimeError("native failure")
            async def disconnect(self):
                steps.append("close")
                self.closed = True
        owner.client_factory = CreatingClient
        def lease(sid):
            assert sid == owner.session_id and not sid.startswith("new:")
            steps.append("lease")
            return SimpleNamespace(launching=lambda: steps.append("launching"),
                                   bind=lambda pid: steps.append("bind"), release=lambda: steps.append("release"))
        owner.lease_factory = lease
        async def checkpoint(target):
            assert target == {"session_id": owner.session_id, "provider": "claude", "cwd": str(tmp_path)}
            assert steps == ["lease"]
            steps.append("checkpoint")
            if failure == "checkpoint":
                raise RuntimeError("checkpoint failure")
        with pytest.raises(ValueError, match="explicit creation"):
            await owner.open()
        if failure:
            with pytest.raises(RuntimeError, match=failure):
                await owner.create(checkpoint=checkpoint)
            await owner._owner_task
            assert steps[-2:] == ["close", "release"]
            assert steps.count("create") == (1 if failure == "native" else 0)
        else:
            await owner.create(checkpoint=checkpoint)
            assert steps == ["lease", "checkpoint", "launching", "create", "bind"]
            assert owner.state == "ready"
            assert events[0]["params"]["thread"]["id"] == owner.session_id
            assert events[0]["params"]["thread"]["turns"] == []
            await owner.close()
            assert steps[-2:] == ["close", "release"]
        with pytest.raises(RuntimeError, match="already attempted"):
            await owner.create(checkpoint=checkpoint)
    asyncio.run(run())


def test_clear_moves_lease_converter_and_output_before_native_ack(tmp_path):
    async def run():
        owner, original = make(tmp_path)
        await owner.open()
        target = "11111111-2222-4333-8444-555555555555"
        changes, new_events = [], []
        new_lease = SimpleNamespace(release=lambda: changes.append("release-new"))

        def transfer(sid):
            assert sid == target and owner.state == "committing-handoff"
            changes.append("transfer")
            return new_lease

        owner._lease.transfer_after_transition = transfer
        owner._lease.release = lambda: changes.append("release-old")

        async def begin():
            assert owner.state == "clearing"
            return {"sessionId": target}

        async def publish(event):
            new_events.append(event)

        async def commit(sid):
            assert changes == ["transfer"]
            assert owner.session_id == owner.events.sid == sid == target
            assert new_events[0]["method"] == "workspace/history"
            await owner.client.messages.put({"type": "result", "session_id": sid,
                                             "subtype": "success", "result": "cleared"})
            await asyncio.sleep(0)
            assert owner.state == "committing-handoff"
            return {"sessionId": sid}

        owner.client.begin_clear, owner.client.commit_clear = begin, commit
        source_history = list(original)
        assert (await owner.begin_clear())["session_id"] == target
        assert owner.session_id == "exact" and owner.state == "awaiting-handoff"
        with pytest.raises(RuntimeError):
            await owner.submit([{"type": "text", "text": "no"}])
        with pytest.raises(RuntimeError):
            await owner.commit_clear("wrong", publish=publish)
        await owner.commit_clear(target, publish=publish)
        assert owner.state == "ready" and original == source_history
        assert new_events[0]["params"]["thread"]["id"] == target
        assert all(event["params"].get("threadId") == target for event in new_events[1:])
        await owner.close()
        assert changes == ["transfer", "release-new"]
    asyncio.run(run())


@pytest.mark.parametrize("busy", ["turn", "question", "elicitation", "task", "unknown-task"])
def test_clear_refuses_any_old_session_work(tmp_path, busy):
    async def run():
        owner, _ = make(tmp_path)
        await owner.open()
        if busy == "turn":
            owner.active_turn = "turn"
        elif busy == "question":
            owner.questions["q"] = asyncio.get_running_loop().create_future()
        elif busy == "elicitation":
            owner.elicitations["q"] = (asyncio.get_running_loop().create_future(), {})
        else:
            owner.events.tasks["task"] = {"status": "running" if busy == "task" else "unknown"}
        with pytest.raises(RuntimeError, match="Finish active work"):
            await owner.begin_clear()
        assert owner.state == "ready" and owner.session_id == "exact"
        await owner.close()
    asyncio.run(run())


def test_named_clear_requires_exact_native_title_confirmation(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        await owner.open()
        target = "11111111-2222-4333-8444-555555555555"

        async def begin(name):
            assert name == "Next work"
            return {
                "sessionId": target,
                "requestedName": name,
                "nameConfirmed": True,
            }

        owner.client.begin_clear = begin
        result = await owner.begin_clear("Next work")
        assert result == {
            "session_id": target,
            "provider": "claude",
            "cwd": str(tmp_path),
            "requestedName": "Next work",
            "nameConfirmed": True,
        }
        assert owner.state == "awaiting-handoff"
        await owner.close()

        owner, _ = make(tmp_path)
        await owner.open()

        async def unconfirmed(name):
            return {
                "sessionId": target,
                "requestedName": name,
                "nameConfirmed": False,
                "nameError": "Native rename unavailable",
            }

        owner.client.begin_clear = unconfirmed
        result = await owner.begin_clear("Next work")
        assert result["nameConfirmed"] is False
        assert result["nameError"] == "Native rename unavailable"
        assert owner.state == "awaiting-handoff"
        await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["lease", "publish", "ack", "cancel"])
def test_clear_handoff_failure_retains_correct_lease_and_blocks_input(tmp_path, failure):
    async def run():
        owner, _ = make(tmp_path)
        await owner.open()
        target = "11111111-2222-4333-8444-555555555555"
        released = []
        old_lease = owner._lease
        old_lease.release = lambda: released.append("old")
        new_lease = SimpleNamespace(release=lambda: released.append("new"))

        def transfer(sid):
            if failure == "lease":
                raise RuntimeError("target occupied")
            return new_lease

        async def begin():
            return {"sessionId": target}

        async def publish(event):
            if failure == "publish":
                raise RuntimeError("disk full")

        async def commit(sid):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("ack missing")

        old_lease.transfer_after_transition = transfer
        owner.client.begin_clear, owner.client.commit_clear = begin, commit
        await owner.begin_clear()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await owner.commit_clear(target, publish=publish)
        assert owner.state == "unavailable" and not owner.can_retry_attachment()
        assert released == []
        with pytest.raises(RuntimeError):
            await owner.begin_clear()
        await owner.close()
        assert released == ["old" if failure == "lease" else "new"]
    asyncio.run(run())


def test_clear_refuses_handoff_when_draining_reveals_background_work(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        await owner.open()

        async def begin():
            owner.events.tasks["late"] = {"status": "running"}
            return {"sessionId": "11111111-2222-4333-8444-555555555555"}

        owner.client.begin_clear = begin
        with pytest.raises(RuntimeError, match="no longer quiescent"):
            await owner.begin_clear()
        assert owner.state == "unavailable" and owner.session_id == "exact"
        assert owner._clear_target is None
        await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_attachment_retry_requires_finished_successful_cleanup(tmp_path, cleanup_fails):
    async def run():
        owner, _ = make(tmp_path)
        assert owner.can_retry_attachment()
        await owner.open()
        assert not owner.can_retry_attachment()
        owner.state = "unavailable"
        assert not owner.can_retry_attachment()
        if cleanup_fails:
            async def failed_disconnect():
                raise RuntimeError("Cleanup incomplete")
            owner.client.disconnect = failed_disconnect
            with pytest.raises(RuntimeError, match="Cleanup incomplete"):
                await owner.close()
            assert not owner.can_retry_attachment()
        else:
            await owner.close()
            assert owner.can_retry_attachment()
    asyncio.run(run())


def test_reload_skills_requires_idle_and_discards_stale_catalog(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        calls = []

        async def reload():
            calls.append("reload")

        async def info():
            return {"commands": [{"name": "fresh"}]}

        owner.client = SimpleNamespace(reload_skills=reload, get_server_info=info)
        owner.state = "running"
        with pytest.raises(RuntimeError, match="current turn"):
            await owner.reload_skills()
        assert not calls
        owner.state = "ready"
        owner.events.capabilities["slash_commands"] = ["removed"]
        assert await owner.reload_skills() == {"data": [{"name": "fresh"}]}
        assert calls == ["reload"]
        assert events[-1]["method"] == "workspace/commands"
    asyncio.run(run())


def test_file_search_requires_attached_owner_and_does_not_submit(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        (tmp_path / "selected.py").write_text("contents not returned")
        with pytest.raises(RuntimeError, match="unavailable"):
            await owner.search_files("selected")
        await owner.open()
        try:
            assert await owner.search_files("selected") == {"paths": ["selected.py"]}
            assert not owner.client.sent
            assert owner.session_id == "exact"
        finally:
            await owner.close()
    asyncio.run(run())


def test_plugin_reload_requires_idle_and_preserves_native_errors(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        calls = []
        async def reload():
            calls.append("reload")
            return {"plugins": [], "error_count": 2}
        async def info():
            return {"commands": [{"name": "fresh"}]}
        owner.client = SimpleNamespace(reload_plugins=reload, get_server_info=info)
        owner.state = "running"
        with pytest.raises(RuntimeError, match="current turn"):
            await owner.reload_plugins()
        assert not calls
        owner.state = "ready"
        owner.events.capabilities["slash_commands"] = ["removed"]
        result = await owner.reload_plugins()
        assert result == {"data": [{"name": "fresh"}], "plugins": [], "error_count": 2}
        assert calls == ["reload"]
        assert events[-2]["method"] == "workspace/plugins"
        assert owner.state == "ready"
    asyncio.run(run())


def test_fork_keeps_original_owner_and_requires_idle(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        calls = []

        async def fork():
            calls.append("fork")
            return {"sessionId": "new-fork"}

        owner.client = SimpleNamespace(fork_session=fork)
        owner.state = "running"
        with pytest.raises(RuntimeError, match="current turn"):
            await owner.fork_session()
        assert not calls
        owner.state = "ready"
        assert await owner.fork_session() == {"session_id": "new-fork", "provider": "claude", "cwd": str(tmp_path)}
        assert calls == ["fork"] and owner.session_id == "exact" and owner.state == "ready"
    asyncio.run(run())


def test_native_mcp_form_validates_answers_and_retires_exact_request(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        request = {"serverName": "Planner", "message": "Choose count",
                   "requestedSchema": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "required": ["count"]}}
        pending = asyncio.create_task(owner._elicitation(request, "native-form"))
        await asyncio.sleep(0)
        event = events[-1]
        assert event["method"] == "mcpServer/elicitation/request"
        assert event["id"] == "claude-mcp:native-form"
        assert event["params"]["threadId"] == "exact"
        assert event["params"]["mode"] == "form"
        assert "mode" not in request
        with pytest.raises(ValueError, match="does not match"):
            await owner.answer(event["id"], {"action": "accept", "content": {"count": "bad"}})
        assert not pending.done()
        answer = {"action": "accept", "content": {"count": 2}}
        await owner.answer(event["id"], answer)
        assert await pending == answer
        assert owner.elicitations == {}
        assert events[-1]["method"] == "serverRequest/resolved"
        with pytest.raises(ValueError, match="no longer pending"):
            await owner.answer(event["id"], answer)
    asyncio.run(run())


def test_native_mcp_cancellation_does_not_answer_or_interrupt_parent(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        owner.active_turn = "running-parent"
        pending = asyncio.create_task(owner._elicitation({"mode": "url", "url": "https://example.com"}, "url"))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert owner.elicitations == {}
        assert owner.active_turn == "running-parent"
        assert events[-1]["params"]["requestId"] == "claude-mcp:url"
    asyncio.run(run())


def test_owner_shutdown_cancels_mcp_form_without_accepting_it(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        await owner.open()
        pending = asyncio.create_task(owner._elicitation({"mode": "form", "requestedSchema": {}}, "shutdown"))
        await asyncio.sleep(0)
        await owner.close()
        assert await pending == {"action": "cancel", "content": None}
        assert owner.elicitations == {}
        assert not owner.client.interrupted
    asyncio.run(run())


def test_authoritative_tool_message_before_block_stop_keeps_complete_input():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    def stream(event):
        return converter.receive(StreamEvent(uuid="event", session_id="exact", event=event))
    stream({"type": "message_start", "message": {"id": "m"}})
    stream({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool", "name": "Bash", "input": {}}})
    converter.receive(AssistantMessage(content=[ToolUseBlock(id="tool", name="Bash", input={"command": "pwd"})], model="native", message_id="m"))
    stream({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "late fragment"}})
    stream({"type": "content_block_stop", "index": 0})
    assert converter.tools["tool"]["input"] == {"command": "pwd"}
    assert not converter.streaming_tools


def test_tool_arguments_stream_separately_for_parent_and_subagent():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    def stream(event, parent=None):
        return converter.receive(StreamEvent(uuid="event", session_id="exact", parent_tool_use_id=parent, event=event))
    for parent, tool_id in [(None, "root-tool"), ("agent-parent", "child-tool")]:
        stream({"type": "message_start", "message": {"id": tool_id + "-message"}}, parent)
        events = stream({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}}, parent)
        assert events[-1]["params"]["item"]["inputStreaming"] is True
        assert events[-1]["params"]["item"]["id"] == tool_id
        assert events[-1]["params"]["item"].get("parentToolUseId") == parent
    events = stream({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"command":'} })
    first = events[-1]["params"]["item"]
    assert first["input"] == {} and first["inputJson"] == '{"command":'
    stream({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"pwd"}'}})
    assert first["inputJson"] == '{"command":'
    events = stream({"type": "content_block_stop", "index": 0})
    assert events[-1]["params"]["item"]["input"] == {"command": "pwd"}
    assert events[-1]["params"]["item"]["status"] == "inProgress"
    assert converter.tools["child-tool"]["input"] == {}
    stream({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "invalid"}}, "agent-parent")
    events = stream({"type": "content_block_stop", "index": 0}, "agent-parent")
    assert events[-1]["params"]["item"]["inputUnavailable"] is True
    assert events[-1]["params"]["item"]["inputJson"] == "invalid"
    assert converter.streaming_tools == {}
    final = converter.blocks([{"type": "tool_use", "id": "child-tool", "name": "Bash", "input": {"command": "canonical"}}], "child-message")[0]
    assert "inputUnavailable" not in final
    assert final["input"] == {"command": "canonical"}


def test_synthetic_command_output_does_not_replace_the_selected_model():
    converter = ClaudeEvents("exact")
    converter.turn = "effort-command"
    events = converter.receive(AssistantMessage(
        content=[TextBlock(text="Set effort level to high (this session only)")],
        model="<synthetic>", message_id="local-result",
    ))
    assert not any(event["method"] == "workspace/settings" for event in events)
    assert any(event["method"] == "item/completed" for event in events)
    assert events[0]["params"]["record"]["model"] == "<synthetic>"
    events = converter.receive(AssistantMessage(
        content=[TextBlock(text="Subagent response")], model="child-model", message_id="child",
        parent_tool_use_id="agent-tool",
    ))
    assert not any(event["method"] == "workspace/settings" for event in events)
    child = next(event["params"]["item"] for event in events if event["method"] == "item/completed")
    assert child["parentToolUseId"] == "agent-tool" and child["sourceModel"] == "child-model"
    events = converter.receive(AssistantMessage(
        content=[TextBlock(text="Actual response")], model="claude-native-model", message_id="real",
    ))
    assert [event["params"]["model"] for event in events if event["method"] == "workspace/settings"] == ["claude-native-model"]


def test_history_restores_last_real_model_not_local_command_placeholder():
    converter = ClaudeEvents("exact")
    def record(number, model, kind="assistant"):
        return {"session_id": "exact", "uuid": str(number), "type": kind,
                "message": {"model": model, "content": [{"type": "text", "text": "content"}]}}
    records = [record(1, "old"), record(2, "current"), record(3, "<synthetic>"), record(4, "user-value", "user")]
    assert converter.history(records)["params"]["thread"]["model"] == "current"
    assert "model" not in converter.history([record(1, "<synthetic>")])["params"]["thread"]


def test_permission_modes_change_only_after_native_acknowledgement(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        calls = []
        async def set_mode(mode):
            calls.append(mode)
            if mode == "auto":
                raise RuntimeError("Policy disallows auto")
        try:
            await owner.open()
            owner.client.set_permission_mode = set_mode
            assert (await owner.permissions())["mode"] is None
            assert calls == []
            with pytest.raises(ValueError, match="confirmation"):
                await owner.set_permissions("bypassPermissions")
            assert (await owner.set_permissions("plan"))["mode"] == "plan"
            with pytest.raises(RuntimeError, match="Policy"):
                await owner.set_permissions("auto")
            assert (await owner.permissions())["mode"] == "plan"
            owner.state = "running"
            with pytest.raises(RuntimeError, match="current Claude turn"):
                await owner.set_permissions("default")
            assert calls == ["plan", "auto"]
            assert not owner.client.sent
            assert [event["params"]["permissionMode"] for event in events if event["method"] == "workspace/settings"] == ["plan"]
        finally:
            await owner.close()
    asyncio.run(run())


def test_context_breakdown_uses_native_totals_without_submitting(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        usage = {"totalTokens": 1500, "maxTokens": 10000, "percentage": 15.0, "model": "native", "categories": [{"name": "Messages", "tokens": 1500}], "memoryFiles": [{"path": "private"}]}
        async def context():
            return usage
        try:
            await owner.open()
            owner.client.get_context_usage = context
            result = await owner.context_usage()
            assert result["percentage"] == 15.0 and result["totalTokens"] == 1500
            assert "memoryFiles" not in result
            assert not owner.client.sent
            usage["percentage"] = float("nan")
            with pytest.raises(ValueError, match="percentage"):
                await owner.context_usage()
        finally:
            await owner.close()
    asyncio.run(run())


def test_task_notification_terminal_state_cannot_be_resurrected_by_late_update():
    events = ClaudeEvents("exact")
    events.receive(TaskNotificationMessage(subtype="task_notification", data={}, task_id="native-task", status="completed", output_file="/not-read", summary="Done", uuid="end", session_id="exact"))
    events.receive(TaskUpdatedMessage(subtype="task_updated", data={}, task_id="native-task", patch={"status": "running"}, session_id="exact"))
    assert events.tasks["native-task"]["status"] == "completed"


def test_native_background_task_stop_waits_for_lifecycle_without_parent_interrupt(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        stopped = []
        async def stop(task_id):
            stopped.append(task_id)
        try:
            await owner.open()
            owner.client.stop_task = stop
            owner.events.receive(TaskStartedMessage(subtype="task_started", data={}, task_id="native-task", description="Research", uuid="event-1", session_id="exact"))
            owner.active_turn = "parent"
            owner.state = "running"
            assert (await owner.list_background_tasks())["data"][0]["processId"] == "native-task"
            assert await owner.terminate_background_task("native-task") == {"terminated": False, "pending": True}
            assert owner.active_turn == "parent" and not owner.client.interrupted
            with pytest.raises(ValueError, match="not running"):
                await owner.terminate_background_task("other-session-task")
            owner.events.receive(TaskUpdatedMessage(subtype="task_updated", data={}, task_id="native-task", patch={"status": "killed"}, session_id="exact"))
            assert await owner.list_background_tasks() == {"data": []}
            with pytest.raises(ValueError, match="not running"):
                await owner.terminate_background_task("native-task")
            assert stopped == ["native-task"]
            with pytest.raises(ValueError, match="identity"):
                owner.events.receive(TaskUpdatedMessage(subtype="task_updated", data={}, task_id="foreign", patch={"status": "running"}, session_id="other"))
            assert "foreign" not in owner.events.tasks
        finally:
            await owner.close()
    asyncio.run(run())


def test_mcp_controls_use_current_session_and_strip_connection_secrets(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        calls = []
        servers = [{"name": "local", "status": "failed", "config": {"token": "secret"}, "error": "secret"}]
        async def status():
            return {"mcpServers": servers}
        async def reconnect(name):
            calls.append(("reconnect", name))
            servers[0]["status"] = "connected"
        async def toggle(name, enabled):
            calls.append(("toggle", name, enabled))
            servers[0]["status"] = "connected" if enabled else "disabled"
        try:
            await owner.open()
            owner.client.get_mcp_status = status
            owner.client.reconnect_mcp_server = reconnect
            owner.client.toggle_mcp_server = toggle
            assert await owner.list_mcp_servers() == {"data": [{"name": "local", "status": "failed"}]}
            assert not calls
            assert (await owner.control_mcp_server("local", "reconnect"))["data"][0]["status"] == "connected"
            assert (await owner.control_mcp_server("local", "disable"))["data"][0]["status"] == "disabled"
            with pytest.raises(ValueError, match="not configured"):
                await owner.control_mcp_server("other-session", "enable")
            owner.state = "running"
            with pytest.raises(RuntimeError, match="current turn"):
                await owner.control_mcp_server("local", "enable")
            assert calls == [("reconnect", "local"), ("toggle", "local", False)]
            assert not owner.client.sent
            assert "secret" not in str(events)
        finally:
            await owner.close()
    asyncio.run(run())


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


def test_interrupt_denies_pending_requests_without_claiming_turn_finished(tmp_path):
    async def run():
        owner, _ = make(tmp_path)
        try:
            await owner.open()
            await owner.submit([{"type": "text", "text": "hello"}])
            permission = asyncio.get_running_loop().create_future()
            elicitation = asyncio.get_running_loop().create_future()
            owner.questions["pending"] = permission
            owner.elicitations["mcp"] = (elicitation, {})
            await owner.interrupt()
            assert permission.result().behavior == "deny"
            assert permission.result().interrupt is True
            assert elicitation.result() == {"action": "cancel"}
            assert owner.state == "running"
        finally:
            await owner.close()
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


def test_command_catalog_and_session_switch_guard(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()

            async def info():
                return {
                    "commands": [
                        {"name": "context"},
                        {"name": "clear", "aliases": ["reset", "new"]},
                    ]
                }

            owner.client.get_server_info = info
            owner.events.capabilities = {
                "slash_commands": ["context", "extra", "color", "reload-plugins", "reload-skills", "doctor"],
                "terminal_slash_commands": ["color", "reload-plugins", "doctor"],
                "skills": ["doctor"],
            }
            result = await owner.list_commands()
            assert [c["name"] for c in result["data"]] == ["context", "clear", "extra", "color", "reload-plugins", "reload-skills", "doctor"]
            assert result["data"][1]["workspaceAction"] == "clear"
            assert "unavailableReason" not in result["data"][1]
            assert result["data"][3]["workspaceAction"] == "color"
            assert "unavailableReason" not in result["data"][3]
            for command in result["data"][-3:-1]:
                assert command["workspaceAction"] == command["name"]
                assert "unavailableReason" not in command
            assert "workspaceAction" not in result["data"][-1]
            assert "unavailableReason" not in result["data"][-1]
            assert events[-1]["method"] == "workspace/commands"
            owner.events.capabilities["skills"] = []
            terminal_catalog = await owner.list_commands()
            assert "unavailableReason" in terminal_catalog["data"][-1]
            for name in ("clear", "new", "reset", "resume", "fork"):
                with pytest.raises(ValueError, match="Session switching"):
                    await owner.submit([{"type": "text", "text": f"/{name} target"}])
            assert not owner.client.sent
            await owner.submit([{"type": "text", "text": "/context"}])
            assert owner.client.sent[0][0] == "exact"
            await owner.client.messages.put(ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=0, is_error=False,
                num_turns=0, session_id="exact"))
            for _ in range(50):
                if owner.state == "ready":
                    break
                await asyncio.sleep(.01)
            await owner.submit([{"type": "text", "text": "/reload-plugins --force"}])
            assert owner.client.sent[-1][1]["message"]["content"][0]["text"] == "/reload-plugins --force"
        finally:
            await owner.close()

    asyncio.run(run())


def test_diagnostics_keep_exact_owner_and_refuse_a_running_turn(tmp_path, monkeypatch):
    async def run():
        calls = []

        async def doctor(binary, cwd, env):
            calls.append((binary, cwd))
            return {"command": "claude doctor", "exitCode": 0, "output": "Native report"}

        monkeypatch.setattr("core.workspace_diagnostics.claude_doctor", doctor)
        owner, _ = make(tmp_path)
        try:
            await owner.open()
            client = owner.client
            assert (await owner.diagnostics())["output"] == "Native report"
            assert calls == [("/controlled/claude", tmp_path)]
            assert owner.client is client and not client.sent and not client.closed
            owner.state = "running"
            with pytest.raises(RuntimeError, match="current turn"):
                await owner.diagnostics()
            assert len(calls) == 1
        finally:
            await owner.close()

    asyncio.run(run())


def test_advertised_model_selection_uses_existing_client(tmp_path):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            catalog = await owner.list_models()
            assert catalog["data"][0]["model"] == "sonnet"
            assert catalog["data"][0]["supportedReasoningEfforts"] == []
            with pytest.raises(ValueError, match="advertise"):
                await owner.submit([{"type": "text", "text": "hi"}], options={"model": "invented"})
            assert not owner.client.sent and not owner.client.models_set
            await owner.submit([{"type": "text", "text": "hi"}], options={"model": "sonnet"})
            assert owner.client.models_set == ["sonnet"]
            assert owner.client.sent[0][0] == "exact"
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_advertised_effort_is_validated_before_settings_and_input(tmp_path, failure):
    async def run():
        owner, events = make(tmp_path)
        try:
            await owner.open()
            async def info():
                return {"models": [{"value": "sonnet", "supportsEffort": True, "supportedEffortLevels": ["low", "high", "invented"]}]}
            efforts = []
            async def set_effort(effort):
                efforts.append(effort)
                if failure:
                    raise RuntimeError("Setting rejected")
            owner.client.get_server_info = info
            owner.client.set_effort = set_effort
            catalog = await owner.list_models()
            assert catalog["data"][0]["supportedReasoningEfforts"] == [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]
            for options in ({"effort": "high"}, {"model": "sonnet", "effort": "max"}, {"model": "sonnet", "effort": "invented"}):
                with pytest.raises(ValueError, match="advertise"):
                    await owner.submit([{"type": "text", "text": "hi"}], options=options)
            assert not owner.client.models_set and not efforts and not owner.client.sent
            if failure:
                with pytest.raises(RuntimeError, match="Setting rejected"):
                    await owner.submit([{"type": "text", "text": "hi"}], options={"model": "sonnet", "effort": "high"})
                assert not owner.client.sent and owner.state == "ready"
                assert not any(event.get("params", {}).get("reasoningEffort") for event in events)
            else:
                await owner.submit([{"type": "text", "text": "hi"}], options={"model": "sonnet", "effort": "high"})
                assert owner.client.sent[0][0] == "exact"
                assert any(event.get("params", {}).get("reasoningEffort") == "high" for event in events)
            assert efforts == ["high"] and owner.client.models_set == ["sonnet"]
        finally:
            await owner.close()
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


def test_permission_suggestions_are_explicit_pending_and_exact(tmp_path):
    from claude_agent_sdk import PermissionUpdate

    async def run():
        owner, events = make(tmp_path)
        update = PermissionUpdate(type="addDirectories", directories=[str(tmp_path)], destination="session")
        try:
            await owner.open()
            question = asyncio.create_task(owner._permission("Read", {}, ToolPermissionContext(tool_use_id="q", suggestions=[update])))
            await asyncio.sleep(0)
            assert events[-1]["params"]["suggestions"] == [update.to_dict()]
            for selected in [[-1], [1], [True], [0, 0], [], "0", [{}]]:
                with pytest.raises(ValueError):
                    await owner.answer("q", {"decision": "allow", "suggestions": selected})
            with pytest.raises(ValueError):
                await owner.answer("q", {"decision": "deny", "suggestions": [0]})
            assert not question.done()
            await owner.answer("q", {"decision": "allow", "suggestions": [0]})
            assert (await question).updated_permissions == [update]
            assert not owner.question_suggestions
            with pytest.raises(ValueError):
                await owner.answer("q", {"decision": "allow", "suggestions": [0]})
        finally:
            await owner.close()
    asyncio.run(run())


def test_history_item_index_preserves_updates_and_resets_between_turns():
    from copy import deepcopy

    def record(uuid, kind, content):
        return {"session_id": "exact", "uuid": uuid, "type": kind, "message": {"id": uuid, "content": content}}

    records = [record("u1", "user", "First turn")]
    for index in range(1000):
        records.append(record(f"a{index}", "assistant", [{"type": "tool_use", "id": f"t{index}", "name": "Read", "input": {"file": index}}]))
    for index in reversed(range(1000)):
        records.append(record(f"r{index}", "user", [{"type": "tool_result", "tool_use_id": f"t{index}", "content": str(index)}]))
    records += [record("u2", "user", "Second turn"), record("new", "assistant", [{"type": "tool_use", "id": "t0", "name": "Edit", "input": {"file": "new"}}])]
    original = deepcopy(records)
    turns = ClaudeEvents("exact").history(records)["params"]["thread"]["turns"]
    assert len(turns) == 2
    assert [item["id"] for item in turns[0]["items"][1:]] == [f"t{i}" for i in range(1000)]
    for index, item in enumerate(turns[0]["items"][1:]):
        assert item["input"] == {"file": index} and item["output"] == str(index)
        assert item["tool"] == "Read" and item["status"] == "completed"
    assert turns[1]["items"][1]["tool"] == "Edit"
    assert turns[0]["items"][1]["tool"] == "Read"
    assert records == original


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


@pytest.mark.parametrize("num_turns, expected", [(0, 1), (1, 0)])
def test_local_command_result_is_visible_without_duplicating_model_response(num_turns, expected):
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    events = converter.receive(
        ResultMessage(
            subtype="success",
            duration_ms=5,
            duration_api_ms=0,
            is_error=False,
            num_turns=num_turns,
            session_id="exact",
            result="## Context Usage",
        )
    )
    results = [e for e in events if e["method"] == "item/completed"]
    assert len(results) == expected
    if results:
        assert results[0]["params"]["item"]["type"] == "commandOutput"
        assert results[0]["params"]["item"]["text"] == "## Context Usage"
    assert converter.turn is None


@pytest.mark.parametrize('suffix, expected', [('', 0), ('\n\n', 0), ('\r\n', 0), (' ', 1), ('\nChanged', 1)])
def test_local_command_terminal_newlines_do_not_duplicate_output(suffix, expected):
    converter = ClaudeEvents('exact')
    converter.begin_input('turn')
    converter.receive({'type': 'assistant', 'session_id': 'exact', 'uuid': 'message',
                       'message': {'content': [{'type': 'text', 'text': '## Context Usage'}]}})
    result = {'type': 'result', 'session_id': 'exact', 'is_error': False,
              'num_turns': 0, 'result': '## Context Usage' + suffix}
    events = converter.receive(result)
    assert sum(event['method'] == 'item/completed' for event in events) == expected
    assert events[0]['params']['record'] == result
    assert any(event['method'] == 'turn/completed' for event in events)


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
