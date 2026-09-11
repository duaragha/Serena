"""Opt-in isolated subscription turn exercising a local MCP form through Codex."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.billing import strip_metered_auth_env
from core.workspace_codex import CodexWorkspace
from core.workspace_elicitation import validate_reply
from core.workspace_rpc import WorkspaceRpc
from scripts.workspace_proof_auth import read_test_auth


def serve():
    from mcp.server.fastmcp import Context, FastMCP
    from pydantic import BaseModel

    class Settings(BaseModel):
        name: str
        count: int

    server = FastMCP("Serena form proof")

    @server.tool()
    async def ask(ctx: Context) -> str:
        schema = Settings.model_json_schema()
        # Native typed MCP root schemas do not accept Pydantic's model title.
        schema.pop("title", None)
        result = await ctx.session.elicit_form("Choose proof settings", schema)
        if result.action == "accept":
            Settings.model_validate(result.content)
        return result.model_dump_json()

    server.run()


async def main(inventory_only=False, auth_home=None):
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex is unavailable")
    auth = None
    if not inventory_only:
        auth = read_test_auth(auth_home)
    with tempfile.TemporaryDirectory(prefix="serena-mcp-proof-") as directory:
        root = Path(directory)
        home = root / "codex"
        home.mkdir(mode=0o700)
        if auth:
            with open(home / "auth.json", "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump({"auth_mode": "chatgpt", "tokens": auth["tokens"]}, stream)
        (home / "config.toml").write_text(
            "[mcp_servers.form_proof]\ncommand = "
            + json.dumps(sys.executable)
            + "\nargs = "
            + json.dumps([str(Path(__file__).resolve()), "--serve"])
            + "\n"
        )
        env = strip_metered_auth_env(dict(os.environ))
        env["CODEX_HOME"] = str(home)
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(name, None)
        rpc = WorkspaceRpc()
        process = None
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=root, env=env)
            process = rpc.process
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "serena-mcp-proof", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await rpc.notify("initialized", {})
            thread = await rpc.request(
                "thread/start",
                {
                    "cwd": str(root), "sandbox": "read-only", "approvalPolicy": "on-request",
                    "developerInstructions": "Transport verification only. Call the form_proof ask tool exactly once, then report its result. Use no other tools. Never execute commands or edit files.",
                    "config": {"features.shell_tool": False, "web_search": "disabled"},
                },
            )
            sid = thread["thread"]["id"]
            if inventory_only:
                async def publish(event):
                    pass
                owner = CodexWorkspace(session_id=sid, cwd=root, rpc=rpc, publish=publish)
                owner.state = "ready"
                async with asyncio.timeout(25):
                    while True:
                        inventory = await owner.list_mcp_servers(True)
                        proof = next((server for server in inventory["data"] if server["name"] == "form_proof"), None)
                        if proof and proof["status"] == "connected" and proof["toolCount"] == 1:
                            break
                        await asyncio.sleep(0.1)
                assert proof["details"]["resourceCount"] == 0
                assert proof["details"]["resourceTemplateCount"] == 0
                assert [tool["name"] for tool in proof["details"]["tools"]] == ["ask"]
                assert proof["details"]["toolsError"] is None
                print("PASS: adapter reads exact-thread native MCP inventory and live connection status")
                print("PASS: full native MCP diagnostics include the exact bounded tool catalog")
                profiles = await owner.permissions()
                assert any(profile["id"] == ":read-only" and profile["allowed"] for profile in profiles["profiles"])
                changed = await owner.set_permissions(":read-only", True)
                assert changed["mode"] == ":read-only"
                print("PASS: native allowed permission profile selected on exact idle thread without a turn")
                print("No inference, tool call, copied authentication, or user session used")
                return
            await rpc.request(
                "turn/start",
                {"threadId": sid, "input": [{"type": "text", "text": "Call form_proof ask once to request the proof settings, then report the returned settings."}]},
            )
            seen = []
            answered = False
            returned = False
            async with asyncio.timeout(120):
                while True:
                    event = await rpc.events.get()
                    seen.append(event.get("method"))
                    if event.get("method") == "mcpServer/elicitation/request":
                        assert event["params"]["threadId"] == sid
                        params = event["params"]
                        assert params["serverName"] == "form_proof", params
                        properties = params["requestedSchema"]["properties"]
                        # Codex first requests approval to call this isolated MCP tool.
                        assert set(properties) in (set(), {"name", "count"}), params
                        answer = {"action": "accept", "content": {"name": "Proof", "count": 3} if properties else {}}
                        validate_reply(event["params"], answer)
                        await rpc.answer(event["id"], answer)
                        answered = answered or bool(properties)
                    elif "id" in event:
                        raise RuntimeError(f"Unexpected server request: {event.get('method')}")
                    if event.get("method") == "item/completed":
                        item = event.get("params", {}).get("item", {})
                        if item.get("type") == "mcpToolCall":
                            returned = "Proof" in json.dumps(item) and "accept" in json.dumps(item)
                    if event.get("method") == "turn/completed":
                        assert event["params"]["turn"]["status"] == "completed", event
                        break
            assert answered and returned, f"Form answer/result missing; methods: {seen}"
            print(
                "PASS: real local MCP form received through Codex, validated, answered, and returned typed data"
            )
            print("One isolated subscription turn; no user session, external MCP service, or permission bypass used")
        finally:
            await rpc.close()
            if process:
                assert process.returncode is not None
                print("PASS: owned Codex process reaped; isolated configuration removed on exit")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        parser = argparse.ArgumentParser()
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--allow-inference", action="store_true")
        mode.add_argument("--inventory-only", action="store_true")
        parser.add_argument("--auth-home", type=Path)
        args = parser.parse_args()
        if bool(args.auth_home) != args.allow_inference:
            parser.error("--auth-home is required only with --allow-inference")
        asyncio.run(main(inventory_only=args.inventory_only, auth_home=args.auth_home))
