"""Real worker handoff proof; invoked with the isolated native clear fixture."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil

from core.workspace_claude_transport import ClaudeSdkTransport
from core.workspace_rpc import WorkspaceRpcError


async def main():
    sdk, cli, node, root, source = sys.argv[1:]
    assert Path(os.environ["HOME"]).resolve() == Path(root).resolve()
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    output = []
    completed = asyncio.Event()
    message_id = str(uuid4())

    async def publish(message):
        output.append(message)
        if message.get("type") == "transport_error":
            completed.set()
        if message.get("type") == "result" and message.get("user_message_uuid") == message_id:
            completed.set()

    async def deny(*args):
        raise AssertionError("Local clear unexpectedly requested interaction")

    transport = ClaudeSdkTransport(session_id=source, cwd=root, sdk_path=sdk,
                                   cli_path=cli, node_path=node, publish=publish, request=deny)
    assert transport.rpc.process is None
    native_pid = None
    try:
        await transport.open()
        native_pid = transport.owned_pid
        wrapper_pid = transport.rpc.process.pid
        target = (await transport.begin_clear())["sessionId"]
        assert source != target and transport.session_id == source
        for operation in (transport.send({"session_id": source}),
                          transport.control("supportedAgents"), transport.commit_clear(source)):
            try:
                await operation
            except WorkspaceRpcError:
                pass
            else:
                raise AssertionError("Input or wrong acknowledgement accepted during handoff")
        await transport.commit_clear(target)
        assert transport.session_id == target and not transport.transition_pending
        await transport.send({"type": "user", "uuid": message_id, "session_id": target,
                              "parent_tool_use_id": None,
                              "message": {"role": "user", "content": "/effort low"}})
        await asyncio.wait_for(completed.wait(), 25)
        assert not transport.failure, str(transport.failure)
        result = next(message for message in output if message.get("type") == "result"
                      and message.get("user_message_uuid") == message_id)
        assert result["session_id"] == target
        assert result["total_cost_usd"] == 0 and result["num_turns"] == 0
        assert transport.owned_pid == native_pid and transport.rpc.process.pid == wrapper_pid
        assert psutil.Process(native_pid).ppid() == wrapper_pid
        print(json.dumps({"source": source, "target": target, "nativePid": native_pid,
                          "sameNativeProcess": True, "modelTurns": 0, "cost": 0}))
    finally:
        await transport.close()
    assert transport.rpc.process is None
    assert native_pid is not None and not psutil.pid_exists(native_pid)
    print("PASS: real Python/JSONL/native clear handoff, blocked input, exact subsequent session, child reaped")


asyncio.run(main())
