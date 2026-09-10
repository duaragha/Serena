"""Native command catalog and doctor routing without credentials or repair work."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def prove(root):
    from core.workspace_claude_runtime import runtime_paths
    from core.workspace_claude_transport import ClaudeSdkTransport

    sdk, node = runtime_paths()
    cli = shutil.which("claude")
    assert cli, "Claude CLI is required"
    sid = str(uuid4())
    done = asyncio.get_running_loop().create_future()
    messages = []

    async def publish(message):
        messages.append(message)
        if message.get("type") == "result" and not done.done():
            done.set_result(message)

    async def deny(*args):
        raise AssertionError("No repair tools authorized")

    transport = ClaudeSdkTransport(session_id=sid, cwd=root, sdk_path=sdk,
                                   node_path=node, cli_path=cli, publish=publish, request=deny)
    try:
        await transport.create(env=dict(os.environ))
        process = transport.rpc.process
        catalog = await transport.control("supportedCommands")
        doctor = next(command for command in catalog if command["name"] == "doctor")
        assert "checkup" in doctor.get("aliases", [])
        await transport.send({"type": "user", "uuid": str(uuid4()), "session_id": sid,
                              "parent_tool_use_id": None,
                              "message": {"role": "user", "content": "/doctor"}})
        result = await asyncio.wait_for(done, 25)
        init = next(message for message in messages if message.get("subtype") == "init")
        assert "doctor" in init["skills"]
        assert result["session_id"] == sid and transport.rpc.process is process
        assert result["is_error"] and result["total_cost_usd"] == 0
        assert any(message.get("error") == "authentication_failed" for message in messages)
        assert not result.get("permission_denials")
        print(json.dumps({"commandNames": [command["name"] for command in catalog],
                          "catalogCount": len(catalog), "doctorIsNativeSkill": True,
                          "exactSession": True, "expectedAuthenticationFailure": True,
                          "inference": False, "repairExecuted": False}))
    finally:
        await transport.close()
    assert process.returncode is not None


def main():
    from core.billing import strip_metered_auth_env

    original = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="serena-command-proof-") as directory:
            root = Path(directory)
            env = strip_metered_auth_env(original)
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"):
                env.pop(key, None)
            env.update(HOME=directory, USERPROFILE=directory, CLAUDE_CONFIG_DIR=str(root / "config"),
                       ANTHROPIC_BASE_URL="http://127.0.0.1:9", CHATS_DATA_DIR=str(root / "data"),
                       SERENA_RUNTIME_LEASE_DIR=str(root / "leases"))
            os.environ.clear()
            os.environ.update(env)
            asyncio.run(prove(root))
    finally:
        os.environ.clear()
        os.environ.update(original)
    assert not root.exists()
    print("PASS: owned transport closed; disposable profile removed; no user credentials used")


if __name__ == "__main__":
    main()
