"""Installed SDK control handshake in isolated storage; no user prompt or inference."""

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


async def main():
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
            print("Advertised models:", json.dumps(info.get("models", [])))
            models = info.get("models", [])
            assert models and models[0].get("value")
            await client.set_model(models[0]["value"])
            print("PASS: installed SDK accepted an advertised model through the existing control connection")
            print("PASS: installed Claude SDK control initialization and owned process identity")
            print(
                "No resume, user message, tool execution, or inference request sent; config isolated"
            )
        finally:
            await client.disconnect()
        assert process.returncode is not None
        print(f"PASS: owned Claude control probe reaped; child exit={process.returncode}")


if __name__ == "__main__":
    asyncio.run(main())
