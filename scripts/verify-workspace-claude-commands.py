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
    from core.workspace_claude_events import ClaudeEvents
    from core.workspace_claude_runtime import runtime_paths
    from core.workspace_claude_transport import ClaudeSdkTransport

    sdk, node = runtime_paths()
    cli = shutil.which("claude")
    assert cli, "Claude CLI is required"
    sid = str(uuid4())
    done = asyncio.Queue()
    messages = []
    adapter = ClaudeEvents(sid)
    translated = []

    async def publish(message):
        messages.append(message)
        translated.extend(adapter.receive(message))
        if message.get("type") == "result":
            await done.put(message)

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

        async def command(text):
            input_id = str(uuid4())
            adapter.begin_input(input_id)
            translated.clear()
            await transport.send({"type": "user", "uuid": input_id, "session_id": sid,
                                  "parent_tool_use_id": None,
                                  "message": {"role": "user", "content": text}})
            result = await asyncio.wait_for(done.get(), 25)
            assert result["session_id"] == sid and transport.rpc.process is process
            assert result["total_cost_usd"] == 0
            outputs = [event["params"]["item"] for event in translated
                       if event["method"] == "item/completed"
                       and event["params"].get("turnId") == input_id
                       and event["params"]["item"]["type"] in {"commandOutput", "agentMessage"}]
            assert len(outputs) == 1 and outputs[0]["text"].rstrip("\r\n") == result["result"].rstrip("\r\n"), (text, outputs)
            completions = [event["params"]["turn"] for event in translated
                           if event["method"] == "turn/completed"]
            assert len(completions) == 1 and completions[0]["id"] == input_id
            assert completions[0]["status"] == ("failed" if result["is_error"] else "completed")
            return result

        local_commands = {
            "/effort low": "Set effort level to low",
            "/context": "Context Usage",
            "/usage": "Total cost:",
            "/agents": "wizard has been removed",
            "/list-agents": "This session:",
            "/model": "Current model:",
            "/config --help": "Usage: /config",
            "/rename command-proof": "Session renamed to: command-proof",
            "/autocompact auto": "Auto-compact window set to auto",
        }
        for text, expected in local_commands.items():
            result = await command(text)
            assert not result["is_error"] and result["num_turns"] == 0
            assert expected in result["result"], text
        title_records = []
        for path in (root / "config").rglob(f"{sid}.jsonl"):
            for line in path.read_text().splitlines():
                record = json.loads(line)
                if record.get("type") == "custom-title":
                    title_records.append(record)
        assert title_records and title_records[-1].get("customTitle") == "command-proof", title_records
        assert title_records[-1].get("sessionId") == sid
        result = await command("/doctor")
        init = next(message for message in messages if message.get("subtype") == "init")
        assert "doctor" in init["skills"]
        assert result["session_id"] == sid and transport.rpc.process is process
        assert result["is_error"] and result["total_cost_usd"] == 0
        assert any(message.get("error") == "authentication_failed" for message in messages)
        assert not result.get("permission_denials")
        print(json.dumps({"commandNames": [command["name"] for command in catalog],
                          "catalogCount": len(catalog), "doctorIsNativeSkill": True,
                          "localCommandsVerified": list(local_commands),
                          "exactCommandOutputAndTurnIdentity": True,
                          "nativeRenamePersistedForExactSession": True,
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
