"""Installed SDK control handshake in isolated storage; no user prompt or inference."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from core.billing import METERED_AUTH_ENV_VARS, strip_metered_auth_env


async def main(local_effort=False):
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("Installed Claude CLI is unavailable")
    inherited = dict(os.environ)
    clean = strip_metered_auth_env(inherited)
    env = {
        **clean,
        **{key: "" for key in set(METERED_AUTH_ENV_VARS) | (inherited.keys() - clean.keys())},
    }
    with tempfile.TemporaryDirectory(prefix="serena-claude-control-proof-") as directory:
        env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / "isolated-config")
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cwd=directory,
                cli_path=binary,
                env=env,
                setting_sources=[],
                strict_mcp_config=True,
                tools=[],
                mcp_servers={"serena-proof": {"command": sys.executable, "args": [str(Path(__file__).with_name("verify-workspace-mcp.py")), "--serve"]}},
                system_prompt="",
            )
        )
        process = None
        try:
            await asyncio.wait_for(client.connect(), 25)
            process = client._transport._process
            assert isinstance(process.pid, int)
            info = await client.get_server_info()
            assert isinstance(info, dict) and info
            print("Advertised control keys:", ", ".join(sorted(info)))
            commands = info.get("commands")
            assert isinstance(commands, list)
            print("Advertised commands:", ", ".join(command["name"] for command in commands))
            print("Advertised models:", json.dumps(info.get("models", [])))
            models = info.get("models", [])
            assert models and models[0].get("value")
            await client.set_model(models[0]["value"])
            await client.set_permission_mode("plan")
            await client.set_permission_mode("default")
            print("PASS: native permission-mode controls acknowledged plan and default without a turn")
            print("PASS: installed SDK accepted an advertised model through the existing control connection")
            async def wait_status(expected):
                async with asyncio.timeout(25):
                    while True:
                        servers = (await client.get_mcp_status())["mcpServers"]
                        if any(server["name"] == "serena-proof" and server["status"] == expected for server in servers):
                            return
                        await asyncio.sleep(0.1)
            await wait_status("connected")
            await client.toggle_mcp_server("serena-proof", False)
            await wait_status("disabled")
            await client.toggle_mcp_server("serena-proof", True)
            await wait_status("connected")
            await client.reconnect_mcp_server("serena-proof")
            await wait_status("connected")
            print("PASS: local MCP status, disable, enable and reconnect through one native control connection")
            print("PASS: installed Claude SDK control initialization and owned process identity")
            print(
                "No resume, user message, tool execution, or inference request sent; config isolated"
            )
            if local_effort:
                from claude_agent_sdk import ResultMessage

                from core.workspace_claude_events import ClaudeEvents

                await client.query("/effort high")
                result = None
                converter = None
                converted = []
                async with asyncio.timeout(30):
                    async for message in client.receive_response():
                        if converter is None and getattr(message, "subtype", None) == "init":
                            converter = ClaudeEvents(message.data["session_id"])
                            converter.turn = "isolated-local-command"
                        if converter is not None:
                            converted.extend(converter.receive(message))
                        if isinstance(message, ResultMessage):
                            result = message
                assert result is not None and not result.is_error
                assert result.num_turns == 0 and result.total_cost_usd == 0
                assert result.duration_api_ms == 0
                assert "Set effort level to high (this session only)" in result.result
                models = [event["params"].get("model") for event in converted if event["method"] == "workspace/settings"]
                assert models and all(model and model != "<synthetic>" for model in models)
                print("PASS: native /effort high acknowledged session-only change; zero model turns, API duration and cost")
                print("PASS: actual synthetic command response retains native model identity in pane events")
        finally:
            await client.disconnect()
        assert process.returncode is not None
        print(f"PASS: owned Claude control probe reaped; child exit={process.returncode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-effort", action="store_true")
    asyncio.run(main(parser.parse_args().local_effort))
