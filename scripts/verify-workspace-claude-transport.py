"""Called by the isolated native driver proof, never against a user's session."""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude_transport import ClaudeSdkTransport
from core.workspace_host import _claude_owner
from core.workspace_lease import SessionLease, SessionOwnedError


async def main():
    sdk, cli, sid, directory = sys.argv[1:]
    root = Path(directory).resolve()
    assert root.name.startswith("serena-claude-driver-")
    assert Path(os.environ["HOME"]).resolve() == root
    assert Path(os.environ["CLAUDE_CONFIG_DIR"]).resolve() == root / "config"
    result = asyncio.get_running_loop().create_future()

    async def publish(message):
        if message.get("type") == "result" and not result.done():
            result.set_result(message)
        elif message.get("type") == "transport_error" and not result.done():
            result.set_exception(RuntimeError(message["error"]))

    async def request(*args):
        raise AssertionError("Local command unexpectedly requested an approval")

    transport = ClaudeSdkTransport(session_id=sid, cwd=root, sdk_path=sdk, cli_path=cli,
                                  node_path=shutil.which("node"), publish=publish, request=request)
    assert transport.rpc.process is None
    lease = SessionLease(sid, directory=root / "leases")
    try:
        lease.launching()
        await transport.open()
        lease.bind(transport.owned_pid)
        child = psutil.Process(transport.owned_pid)
        try:
            duplicate = SessionLease(sid, directory=root / "leases")
        except SessionOwnedError:
            pass
        else:
            duplicate.release()
            raise AssertionError("Concurrent owner was admitted")
        await transport.control("applyFlagSettings", {"effortLevel": "high"})
        await transport.send({"type": "user", "uuid": str(uuid4()), "session_id": sid,
                              "parent_tool_use_id": None,
                              "message": {"role": "user", "content": "/effort low"}})
        completed = await asyncio.wait_for(result, 15)
        assert completed["session_id"] == sid
        assert completed["num_turns"] == 0 and completed["total_cost_usd"] == 0
        wrapper = transport.rpc.process
        await transport.close()
        assert wrapper.returncode == 0
        assert not child.is_running()
    finally:
        await transport.close()
        lease.release()
    recovered = SessionLease(sid, directory=root / "leases")
    recovered.release()
    print("PASS: Python WorkspaceRpc -> native Claude, exact session input/output, actual PID bound to shared lease, duplicate owner rejected, native child/wrapper reaped and lease recoverable")
    completed_turn = asyncio.get_running_loop().create_future()
    events = []
    discovery_form = os.environ.get("SERENA_PROOF_DISCOVERY_FORM") == "1"
    form_receipt = root / "form-result.json"
    if discovery_form:
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"form_proof": {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("workspace-claude-form-fixture.py")), str(form_receipt)],
        }}}), encoding="utf-8")
        (root / ".claude").mkdir(exist_ok=True)
        (root / ".claude" / "settings.local.json").write_text(
            json.dumps({"enabledMcpjsonServers": ["form_proof"]}), encoding="utf-8")

    async def publish_pane(event):
        events.append(event)
        if event.get("method") == "mcpServer/elicitation/request":
            assert event["params"]["serverName"] == "form_proof"
            assert event["params"]["message"] == "Choose proof count"
            await owner.answer(event["id"], {"action": "accept", "content": {"count": 2}})
        elif event.get("method") == "turn/completed" and not completed_turn.done():
            completed_turn.set_result(event["params"]["turn"])
        elif event.get("method") == "workspace/error" and not completed_turn.done():
            completed_turn.set_exception(RuntimeError(event["params"]["reason"]))

    owner = _claude_owner(session_id=sid, cwd=root, publish=publish_pane,
                          lease_factory=lambda session_id: SessionLease(session_id, directory=root / "leases"))
    try:
        await owner.open()
        original_pid = owner.client.owned_pid
        skill = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills/workspace-proof/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: workspace-proof\ndescription: Isolated reload proof\n---\nReturn the word proof.\n")
        refreshed = await owner.reload_skills()
        assert any(command["name"] == "workspace-proof" for command in refreshed["data"]), [item["name"] for item in refreshed["data"]]
        skill.unlink()
        refreshed = await owner.reload_skills()
        assert not any(command["name"] == "workspace-proof" for command in refreshed["data"]), [item["name"] for item in refreshed["data"]]
        assert owner.client.owned_pid == original_pid, "Skill reload replaced the native session process"
        print("PASS: native skill added and removed after attach; explicit reload refreshed catalog without replacing session")
        assert owner.client.transport.command[2] == str(Path(sdk).resolve())
        if os.environ.get("SERENA_WORKSPACE_NODE_MODE") == "electron":
            assert owner.client.transport.command[0] == str(Path(os.environ["SERENA_WORKSPACE_NODE"]).resolve())
        await owner.submit([{"type": "text", "text": "/effort low"}])
        if discovery_form:
            async with asyncio.timeout(15):
                while not form_receipt.exists():
                    await asyncio.sleep(0.05)
            native_form_result = json.loads(form_receipt.read_text())
            assert native_form_result["content"] == {"count": 2}, native_form_result
            print("PASS: real local MCP form crossed native SDK, Node channel, Python transport and pane owner; validated answer returned to the requesting MCP server without inference")
        assert any(event["method"] == "workspace/history" for event in events)
        history = next(event["params"]["thread"] for event in events if event["method"] == "workspace/history")
        commands = [item for turn in history["turns"] for item in turn["items"]
                    if item.get("type") == "userMessage" and item.get("providerOriginal")]
        assert commands, "Native persisted slash commands were not normalized"
        assert all(item["content"][0]["text"].startswith("/effort ") for item in commands)
        assert (await owner.list_models())["data"]
        native = psutil.Process(owner.client.owned_pid)
        turn = await asyncio.wait_for(completed_turn, 15)
        assert turn["status"] == "completed"
        assert turn["providerOriginal"]["num_turns"] == 0
        assert turn["providerOriginal"]["total_cost_usd"] == 0
        assert any(event["method"] == "item/completed" for event in events)
        result_text = turn["providerOriginal"]["result"]
        assert result_text
        visible_results = [event["params"]["item"] for event in events
                           if event["method"] == "item/completed"
                           and event["params"]["item"].get("text") == result_text]
        assert len(visible_results) == 1, "Native local-command output was duplicated"
        assert owner.state == "ready"
        from core.indexer import get_session
        from core.workspace_catalog import register_fork
        from core.workspace_host import WorkspaceHost
        from core.workspace_journal import WorkspaceJournal

        fork = await owner.fork_session()
        checkpoint_path = root / "fork-checkpoint.db"
        checkpoint = WorkspaceJournal(checkpoint_path)
        checkpoint.claim_command(sid, "interrupted", {"action": "fork_session", "payload": {}})
        checkpoint.append(sid, {"method": "workspace/sessionForked", "params": {
            "threadId": sid, "requestId": "interrupted", "fork": fork}})
        # Reopen durable state with the actual already-admitted owner. No second
        # runtime is admitted or created while recovering the unfinished receipt.
        recovery = WorkspaceHost(journal=WorkspaceJournal(checkpoint_path), resolve=None, register_fork=register_fork)
        recovery._sessions[sid] = (owner, "claude")
        before = set((root / "config/projects").glob("*/*.jsonl"))
        receipt = await recovery._command(sid, "interrupted", "fork_session", {})
        assert receipt["ok"] and receipt["result"]["session_id"] == fork["session_id"] and receipt["result"]["indexed"]
        assert await recovery._command(sid, "interrupted", "fork_session", {}) == receipt
        assert set((root / "config/projects").glob("*/*.jsonl")) == before
        indexed = get_session(fork["session_id"])
        assert indexed and indexed["session_id"] == fork["session_id"] and indexed["agent"] == "claude"
        assert owner.session_id == sid and owner.client.owned_pid == original_pid and native.is_running()
        print("PASS: native fork checkpoint reopened with unfinished receipt; exact fork indexed without another transcript or owner, original process unchanged")
    finally:
        await owner.close()
    assert not native.is_running()
    print("PASS: existing ClaudeWorkspace owner used public SDK client, replayed history, listed native models, converted real command output/completion for pane, returned ready and reaped CLI")
    if os.environ.get("SERENA_WORKSPACE_NODE_MODE") == "electron":
        print("PASS: default owner ran SDK worker using Electron in Node mode; no desktop window launched")


if __name__ == "__main__":
    asyncio.run(main())
