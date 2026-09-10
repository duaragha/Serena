"""Zero-inference native Claude pause/wake in a disposable profile."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def prove(root, sdk, node, cli):
    from core.workspace_claude_transport import ClaudeSdkTransport

    sid, prompt = str(uuid4()), str(uuid4())
    completed = asyncio.get_running_loop().create_future()
    async def publish(message):
        if message.get("type") == "result" and not completed.done():
            completed.set_result(message)
        if message.get("type") == "transport_error" and not completed.done():
            completed.set_exception(RuntimeError(message["error"]))
    async def deny(*args):
        raise AssertionError("Local proof requested permission")
    transport = ClaudeSdkTransport(session_id=sid, cwd=root, sdk_path=sdk, cli_path=cli,
                                  node_path=node, publish=publish, request=deny)
    child = wrapper = None
    try:
        await transport.create(env=dict(os.environ))
        wrapper = transport.rpc.process
        child = psutil.Process(transport.owned_pid)
        async with asyncio.timeout(10):
            while not await transport.rpc.pause_idle():
                await asyncio.sleep(.01)
            if os.name == "nt":
                job = transport.rpc._windows_job
                assert child.pid in {pid for pid, _, _ in job._suspended}
                for _, _, handle in job._suspended:
                    if not job._thread_alive(handle):
                        continue
                    previous = job._api.SuspendThread(handle)
                    assert previous != 0xFFFFFFFF
                    restored = job._api.ResumeThread(handle)
                    assert previous >= 1 and restored == previous + 1
            else:
                while child.status() != psutil.STATUS_STOPPED:
                    await asyncio.sleep(.01)
        paused_reply = asyncio.get_running_loop().create_future()
        transport.rpc._pending["paused-proof"] = paused_reply
        wrapper.stdin.write((json.dumps({"id": "paused-proof", "method": "control", "params": {
            "method": "applyFlagSettings", "args": [{"effortLevel": "low"}]}}) + "\n").encode())
        await wrapper.stdin.drain()
        await asyncio.sleep(.1)
        assert not paused_reply.done(), "Paused native control unexpectedly executed"
        started = asyncio.get_running_loop().time()
        await transport.control("applyFlagSettings", {"effortLevel": "high"})
        wake_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
        assert "error" not in await asyncio.wait_for(paused_reply, 10)
        transport.rpc._pending.pop("paused-proof")
        assert not transport.rpc.suspended
        await transport.send({"type": "user", "uuid": prompt, "session_id": sid,
                              "parent_tool_use_id": None,
                              "message": {"role": "user", "content": "/effort low"}})
        result = await asyncio.wait_for(completed, 20)
        assert result["session_id"] == sid and result["num_turns"] == 0 and result["total_cost_usd"] == 0
        assert transport.rpc.process is wrapper and transport.owned_pid == child.pid
        async with asyncio.timeout(10):
            while not await transport.rpc.pause_idle():
                await asyncio.sleep(.01)
    finally:
        await transport.close()
    assert wrapper.returncode is not None and not child.is_running()
    print(json.dumps({"ok": True, "provider": "claude", "platform": sys.platform,
                      "sameSession": True, "sameProcess": True, "pausedInputBlocked": True,
                      "nativeSuspendCountsConfirmed": os.name == "nt",
                      "wakeControlRoundTripMs": wake_ms, "localSlashCommand": True,
                      "inference": False, "closedWhilePaused": True, "childrenReaped": True}))


def main():
    from core.billing import strip_metered_auth_env
    from core.workspace_claude_runtime import runtime_paths

    sdk, node = runtime_paths()
    cli = shutil.which("claude")
    assert cli, "Native Claude executable is required"
    original = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="serena-claude-sleep-") as directory:
            root = Path(directory).resolve()
            env = strip_metered_auth_env(original)
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"):
                env.pop(key, None)
            env.update(HOME=str(root), USERPROFILE=str(root), CLAUDE_CONFIG_DIR=str(root / "config"),
                       ANTHROPIC_BASE_URL="http://127.0.0.1:9", CHATS_DATA_DIR=str(root / "data"),
                       SERENA_RUNTIME_LEASE_DIR=str(root / "leases"))
            os.environ.clear()
            os.environ.update(env)
            asyncio.run(prove(root, sdk, node, cli))
    finally:
        os.environ.clear()
        os.environ.update(original)
    assert not root.exists()
    print("PASS: disposable profile removed; no user credentials or installed services changed")


if __name__ == "__main__":
    main()
