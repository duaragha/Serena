import asyncio
from types import SimpleNamespace

import pytest

from core.workspace_claude_transport import ClaudeSdkTransport
from core.workspace_rpc import WorkspaceRpcError

TARGET = "11111111-2222-4333-8444-555555555555"


def test_clear_blocks_input_until_exact_handoff_ack(monkeypatch, tmp_path):
    async def run():
        transport, output = make(monkeypatch, tmp_path)
        await transport.open()
        original = transport.rpc.request
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(method, params, **kwargs):
            if method == "begin_clear":
                await transport.rpc.events.put({"method": "claude/message", "params": {
                    "message": {"type": "assistant", "session_id": "exact"}}})
                return {"sessionId": TARGET}
            if method == "commit_clear":
                assert params == {"sessionId": TARGET}
                await transport.rpc.events.put({"method": "claude/message", "params": {
                    "message": {"type": "system", "session_id": TARGET}}})
                entered.set()
                await release.wait()
                return {"sessionId": TARGET}
            return await original(method, params, **kwargs)

        transport.rpc.request = request
        assert await transport.begin_clear() == {"sessionId": TARGET}
        assert output == [{"type": "assistant", "session_id": "exact"}]
        assert transport.session_id == "exact"
        with pytest.raises(WorkspaceRpcError):
            await transport.commit_clear("wrong")
        with pytest.raises(WorkspaceRpcError):
            await transport.begin_clear()
        task = asyncio.create_task(transport.commit_clear(TARGET))
        await entered.wait()
        await asyncio.sleep(0)
        assert output == [{"type": "assistant", "session_id": "exact"},
                          {"type": "system", "session_id": TARGET}]
        for call in (transport.send({"session_id": TARGET}),
                     transport.control("supportedAgents"), transport.commit_clear(TARGET)):
            with pytest.raises(WorkspaceRpcError):
                await call
        release.set()
        await task
        await transport.send({"session_id": TARGET})
        with pytest.raises(WorkspaceRpcError):
            await transport.send({"session_id": "exact"})
        assert transport.owned_pid == 22
        await transport.close()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["begin_clear", "commit_clear"])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "identity"])
def test_unconfirmed_clear_never_retries_or_unblocks(monkeypatch, tmp_path, phase, failure):
    async def run():
        transport, _ = make(monkeypatch, tmp_path)
        await transport.open()
        calls = []

        async def request(method, params, **kwargs):
            calls.append(method)
            if method == phase:
                if failure == "cancel":
                    raise asyncio.CancelledError()
                if failure == "timeout":
                    raise TimeoutError("lost acknowledgement")
                return {"sessionId": "invalid"}
            return {"sessionId": TARGET}

        transport.rpc.request = request
        if phase == "commit_clear":
            await transport.begin_clear()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else WorkspaceRpcError):
            await (transport.begin_clear() if phase == "begin_clear" else transport.commit_clear(TARGET))
        before = list(calls)
        for call in (transport.begin_clear(), transport.commit_clear(TARGET),
                     transport.send({"session_id": transport.session_id})):
            with pytest.raises(WorkspaceRpcError):
                await call
        assert calls == before
        assert transport.failure and transport.transition_pending
        await transport.close()
    asyncio.run(run())


def test_clear_refuses_pending_and_unexpected_interactions(monkeypatch, tmp_path):
    async def run():
        transport, output = make(monkeypatch, tmp_path)
        await transport.open()
        transport.questions["q"] = asyncio.create_task(asyncio.sleep(60))
        with pytest.raises(WorkspaceRpcError, match="pending interactions"):
            await transport.begin_clear()
        assert not transport.transition_pending
        task = transport.questions.pop("q")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        transport.transition_pending = True
        await transport.rpc.events.put({"id": "new", "method": "claude/elicitation", "params": {}})
        await transport.reader
        assert "during session handoff" in str(transport.failure)
        assert not transport.questions and not transport.rpc.answers
        assert output[-1]["type"] == "transport_error"
        await transport.close()
    asyncio.run(run())


class FakeRpc:
    def __init__(self):
        self.process = None
        self.events = asyncio.Queue()
        self.calls = []
        self.answers = []

    async def start(self, command, *, cwd, env):
        self.calls.append((command, env))
        self.process = SimpleNamespace(pid=11)

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "open":
            await self.events.put({"method": "claude/process", "params": {"pid": 22}})
        return {"ok": True}

    async def answer(self, request_id, result):
        self.answers.append((request_id, result))

    async def close(self):
        self.calls.append(("reaped", {}))
        self.process = None


def make(monkeypatch, tmp_path, request=None):
    monkeypatch.setattr("core.workspace_claude_transport.psutil.Process", lambda pid: SimpleNamespace(ppid=lambda: 11))
    output = []

    async def publish(message):
        output.append(message)

    async def decline(*args):
        return {"action": "decline"}

    transport = ClaudeSdkTransport(session_id="exact", cwd=tmp_path, sdk_path="sdk.mjs",
                                  cli_path="claude", node_path="node", publish=publish,
                                  request=request or decline, rpc_factory=FakeRpc)
    return transport, output


def test_explicit_open_sanitizes_auth_and_preserves_exact_routing(monkeypatch, tmp_path):
    async def run():
        transport, output = make(monkeypatch, tmp_path)
        assert transport.rpc.calls == []
        await transport.open(env={"ANTHROPIC_API_KEY": "never-send", "PATH": "/bin"})
        assert transport.owned_pid == 22
        assert not transport.rpc.calls[0][1].get("ANTHROPIC_API_KEY")
        await transport.send({"session_id": "exact", "type": "user"})
        await transport.control("supportedAgents")
        with pytest.raises(WorkspaceRpcError, match="exact session"):
            await transport.send({"session_id": "wrong"})
        with pytest.raises(WorkspaceRpcError, match="twice"):
            await transport.open()
        await transport.close()
        assert transport.rpc.calls[-2:] == [("close", {}), ("reaped", {})]
        assert output == []
    asyncio.run(run())


def test_rejects_pid_not_owned_by_worker(monkeypatch, tmp_path):
    async def run():
        transport, _ = make(monkeypatch, tmp_path)
        monkeypatch.setattr("core.workspace_claude_transport.psutil.Process", lambda pid: SimpleNamespace(ppid=lambda: 99))
        with pytest.raises(WorkspaceRpcError, match="does not belong"):
            await transport.open()
        assert transport.rpc.process is None
    asyncio.run(run())


def test_interactive_wait_does_not_block_native_output_and_can_cancel(monkeypatch, tmp_path):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def request(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        transport, output = make(monkeypatch, tmp_path, request)
        await transport.open()
        await transport.rpc.events.put({"id": "q", "method": "claude/elicitation", "params": {"request": {}}})
        await entered.wait()
        await transport.rpc.events.put({"method": "claude/message", "params": {"message": {"type": "assistant", "session_id": "exact"}}})
        await asyncio.sleep(0)
        assert output[0]["type"] == "assistant"
        await transport.rpc.events.put({"method": "serverRequest/resolved", "params": {"requestId": "q"}})
        await cancelled.wait()
        assert transport.rpc.answers == []
        await transport.close()
    asyncio.run(run())


def test_native_wrong_session_disables_further_input(monkeypatch, tmp_path):
    async def run():
        transport, output = make(monkeypatch, tmp_path)
        await transport.open()
        await transport.rpc.events.put({"method": "claude/message", "params": {"message": {"session_id": "wrong"}}})
        await transport.reader
        assert output[0]["type"] == "transport_error"
        with pytest.raises(WorkspaceRpcError, match="crossed session"):
            await transport.send({"session_id": "exact"})
        await transport.close()
    asyncio.run(run())


def test_closed_transport_never_launches(monkeypatch, tmp_path):
    async def run():
        transport, _ = make(monkeypatch, tmp_path)
        await transport.close()
        with pytest.raises(WorkspaceRpcError, match="cannot be started"):
            await transport.open()
        assert transport.rpc.calls == []
    asyncio.run(run())


def test_answers_only_the_exact_pending_rpc_request(monkeypatch, tmp_path):
    async def run():
        entered = asyncio.Event()
        seen = []

        async def request(method, params):
            seen.append((method, params))
            entered.set()
            return {"action": "accept", "content": {"choice": "one"}}

        transport, _ = make(monkeypatch, tmp_path, request)
        await transport.open()
        params = {"request": {"message": "Choose"}, "nativeRequestId": "native-1"}
        await transport.rpc.events.put({"id": "wire-1", "method": "claude/elicitation", "params": params})
        await entered.wait()
        await asyncio.sleep(0)
        assert seen == [("claude/elicitation", params)]
        assert transport.rpc.answers == [("wire-1", {"action": "accept", "content": {"choice": "one"}})]
        await transport.close()
    asyncio.run(run())


def test_electron_node_mode_is_scoped_to_worker(monkeypatch, tmp_path):
    async def run():
        transport, _ = make(monkeypatch, tmp_path)
        await transport.open(env={"SERENA_WORKSPACE_NODE_MODE": "electron"})
        assert transport.rpc.calls[0][1]["ELECTRON_RUN_AS_NODE"] == "1"
        await transport.close()
        transport, _ = make(monkeypatch, tmp_path)
        await transport.open(env={"ELECTRON_RUN_AS_NODE": "1"})
        assert "ELECTRON_RUN_AS_NODE" not in transport.rpc.calls[0][1]
        await transport.close()
    asyncio.run(run())


def test_external_worker_does_not_inherit_frozen_library_paths(monkeypatch, tmp_path):
    async def run():
        transport, _ = make(monkeypatch, tmp_path)
        monkeypatch.setattr("core.workspace_claude_transport.sys.platform", "linux")
        monkeypatch.setattr("core.workspace_claude_transport.sys._MEIPASS", "/frozen", raising=False)
        await transport.open(env={"APPDIR": "/mount", "LD_LIBRARY_PATH": "/frozen:/mount/lib:/wrong",
                                  "LD_LIBRARY_PATH_ORIG": "/mount/lib:/custom:/mount-other:"})
        env = transport.rpc.calls[0][1]
        assert env["LD_LIBRARY_PATH"] == "/custom:/mount-other"
        assert "LD_LIBRARY_PATH_ORIG" not in env
        await transport.close()
    asyncio.run(run())
