"""Exercise fresh native creation through the production Python/JSONL bridge."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_lease import SessionLease, SessionOwnedError


async def main():
    sdk, cli, node, cwd = sys.argv[1:]
    assert Path(os.environ["HOME"]).resolve() == Path(cwd).resolve()
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    sid, prompt = str(uuid4()), str(uuid4())
    messages = []
    completed = asyncio.Event()
    async def request(*args):
        raise AssertionError("Local command requested permission")
    client = ClaudeTypeScriptClient(options=SimpleNamespace(resume=sid, cwd=cwd, cli_path=cli,
                                    env=dict(os.environ), can_use_tool=request), sdk_path=sdk, node_path=node)
    transport = client.transport
    async def consume():
        async for message in client.receive_messages():
            messages.append(message)
            if message.get("type") == "result" and message.get("user_message_uuid") == prompt:
                completed.set()
    reader = asyncio.create_task(consume())
    pid = None
    try:
        await client.create()
        pid = transport.owned_pid
        assert pid and psutil.pid_exists(pid)
        async def inputs():
            yield {"type": "user", "uuid": prompt, "session_id": sid,
                   "parent_tool_use_id": None, "message": {"role": "user", "content": "/effort low"}}
        await client.query(inputs(), sid)
        await asyncio.wait_for(completed.wait(), 20)
        result = next(message for message in messages if message.get("type") == "result")
        assert result["session_id"] == sid and result["num_turns"] == 0 and result["total_cost_usd"] == 0
        assert transport.owned_pid == pid
        assert all(message["session_id"] == sid for message in messages if message.get("session_id"))
    finally:
        try:
            await client.disconnect()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    assert transport.rpc.process is None and not psutil.pid_exists(pid)
    files = list((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").glob(f"*/{sid}.jsonl"))
    assert len(files) == 1
    print("PASS: real Python client create -> JSONL worker -> native UUID input/output; exact transcript, zero inference, child reaped")
    events, checkpointed = [], []
    completed = asyncio.Event()
    async def publish(event):
        events.append(event)
        if event["method"] == "turn/completed":
            completed.set()
    def factory(*, options):
        options.cli_path = cli
        return ClaudeTypeScriptClient(options=options, sdk_path=sdk, node_path=node)
    lease_dir = Path(cwd) / "leases"
    owner = ClaudeWorkspace(session_id="new:" + str(uuid4()), cwd=cwd, publish=publish,
                            client_factory=factory, lease_factory=lambda sid: SessionLease(sid, directory=lease_dir))
    async def checkpoint(target):
        assert owner.client.owned_pid is None
        try:
            duplicate = SessionLease(target["session_id"], directory=lease_dir)
        except SessionOwnedError:
            pass
        else:
            duplicate.release()
            raise AssertionError("Reserved session lease was not exclusive")
        with (Path(cwd) / "creation.json").open("w") as record:
            json.dump(target, record)
            record.flush()
            os.fsync(record.fileno())
        checkpointed.append(target)
    try:
        await owner.create(checkpoint=checkpoint)
        native_pid = owner.client.owned_pid
        assert native_pid and psutil.pid_exists(native_pid)
        assert checkpointed == [{"session_id": owner.session_id, "provider": "claude", "cwd": cwd}]
        assert json.loads((Path(cwd) / "creation.json").read_text()) == checkpointed[0]
        await owner.submit([{"type": "text", "text": "/effort low"}])
        await asyncio.wait_for(completed.wait(), 20)
        assert owner.state == "ready" and owner.client.owned_pid == native_pid
        assert not any(event["method"] == "workspace/error" for event in events)
    finally:
        await owner.close()
    assert not psutil.pid_exists(native_pid)
    assert len(list((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").glob(f"*/{owner.session_id}.jsonl"))) == 1
    released = SessionLease(owner.session_id, directory=lease_dir)
    released.release()
    print("PASS: real owner reserves exclusive UUID and durable checkpoint before spawn; same-process local turn; close reaps child and releases lease")


asyncio.run(main())
