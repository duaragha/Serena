"""Called by the isolated native driver proof, never against a user's session."""
import asyncio
import os
import shutil
import sys
from functools import partial
from pathlib import Path
from uuid import uuid4

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_claude_transport import ClaudeSdkTransport
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

    async def publish_pane(event):
        events.append(event)
        if event.get("method") == "turn/completed" and not completed_turn.done():
            completed_turn.set_result(event["params"]["turn"])
        elif event.get("method") == "workspace/error" and not completed_turn.done():
            completed_turn.set_exception(RuntimeError(event["params"]["reason"]))

    owner = ClaudeWorkspace(session_id=sid, cwd=root, publish=publish_pane,
                            client_factory=partial(ClaudeTypeScriptClient, sdk_path=sdk, node_path=shutil.which("node")),
                            lease_factory=lambda session_id: SessionLease(session_id, directory=root / "leases"))
    try:
        await owner.open()
        assert any(event["method"] == "workspace/history" for event in events)
        assert (await owner.list_models())["data"]
        native = psutil.Process(owner.client.owned_pid)
        await owner.submit([{"type": "text", "text": "/effort low"}])
        turn = await asyncio.wait_for(completed_turn, 15)
        assert turn["status"] == "completed"
        assert turn["providerOriginal"]["num_turns"] == 0
        assert turn["providerOriginal"]["total_cost_usd"] == 0
        assert any(event["method"] == "item/completed" for event in events)
        assert owner.state == "ready"
    finally:
        await owner.close()
    assert not native.is_running()
    print("PASS: existing ClaudeWorkspace owner used public SDK client, replayed history, listed native models, converted real command output/completion for pane, returned ready and reaped CLI")


if __name__ == "__main__":
    asyncio.run(main())
