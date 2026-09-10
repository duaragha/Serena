"""Explicit subscription inference/resume proof, isolated from user sessions."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from core.billing import METERED_AUTH_ENV_VARS, strip_metered_auth_env
from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


async def main(bridge=False, background_task=False, typescript_sdk=None, mcp_form=False):
    if typescript_sdk:
        typescript_sdk = str(Path(typescript_sdk).resolve(strict=True))
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("Installed Claude unavailable")
    original_config = os.environ.get("CLAUDE_CONFIG_DIR")
    source = Path(original_config or Path.home() / ".claude") / ".credentials.json"
    oauth = None
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        oauth = json.loads(source.read_text()).get("claudeAiOauth")
        if not oauth or not oauth.get("accessToken"):
            raise RuntimeError("Proof requires existing Claude subscription authentication")
    with tempfile.TemporaryDirectory(prefix="serena-claude-roundtrip-") as temporary:
        root = Path(temporary)
        config, project = root / "config", root / "project"
        config.mkdir(mode=0o700)
        project.mkdir()
        if oauth:
            with open(
                config / ".credentials.json",
                "x",
                opener=lambda path, flags: os.open(path, flags, 0o600),
            ) as stream:
                json.dump({"claudeAiOauth": oauth}, stream)
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        inherited = dict(os.environ)
        clean = strip_metered_auth_env(inherited)
        env = {
            **clean,
            **{key: "" for key in set(METERED_AUTH_ENV_VARS) | (inherited.keys() - clean.keys())},
        }
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cli_path=binary,
                cwd=str(project),
                env=env,
                setting_sources=[],
                strict_mcp_config=True,
                tools=[],
                system_prompt="Transport proof only. Do not use tools. Reply with only the exact text requested.",
            )
        )
        owner = None
        try:
            await client.connect()
            first_process = client._transport._process
            await client.query("Reply exactly SERENA_CLAUDE_FIRST_PROOF")
            sid = None
            async with asyncio.timeout(120):
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        if message.is_error:
                            raise RuntimeError(f"Claude first turn failed: {message.subtype}")
                        sid = message.session_id
            assert sid, "No persisted Claude identity received"
            await client.disconnect()
            assert first_process.returncode is not None
            print("PASS: isolated Claude first turn completed; original process reaped")
            receipt = root / "form-receipt.json"
            if mcp_form:
                (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"form_proof": {
                    "command": sys.executable,
                    "args": [str(Path(__file__).with_name("workspace-claude-form-fixture.py")), str(receipt), "--tool"],
                }}}), encoding="utf-8")
                (project / ".claude").mkdir()
                (project / ".claude" / "settings.local.json").write_text(
                    json.dumps({"enabledMcpjsonServers": ["form_proof"]}), encoding="utf-8")
            events = []
            finished = asyncio.Event()

            async def publish(event):
                events.append(event)
                if mcp_form and event.get("method") == "workspace/claudeApproval":
                    await owner.answer(event["id"], {"decision": "allow" if event["params"]["tool"] == "mcp__form_proof__ask" else "deny"})
                if mcp_form and event.get("method") == "mcpServer/elicitation/request":
                    assert event["params"]["serverName"] == "form_proof"
                    await owner.answer(event["id"], {"action": "accept", "content": {"count": 2}})
                if background_task and event.get("method") == "workspace/claudeApproval":
                    params = event["params"]
                    allowed = params["tool"] == "Bash" and params["input"].get("command") == "sleep 60" and params["input"].get("run_in_background") is True
                    await owner.answer(event["id"], {"decision": "allow" if allowed else "deny"})
                if event.get("method") == "turn/completed":
                    finished.set()

            owner = ClaudeWorkspace(
                session_id=sid,
                cwd=project,
                publish=publish,
                lease_factory=lambda session: SessionLease(session, directory=root / "leases"),
                **({"client_factory": partial(ClaudeTypeScriptClient, sdk_path=typescript_sdk, node_path=shutil.which("node"))} if typescript_sdk else {}),
            )
            await owner.open()
            assert "SERENA_CLAUDE_FIRST_PROOF" in json.dumps(events[0])
            await owner.submit(
                [
                    {
                        "type": "text",
                        "text": "Do not use tools. Reply exactly SERENA_CLAUDE_RESUME_PROOF",
                    }
                ]
            )
            await asyncio.wait_for(finished.wait(), 120)
            completion = [event for event in events if event.get("method") == "turn/completed"][-1]
            assert completion["params"]["turn"]["status"] == "completed", completion
            duration = completion["params"]["turn"]["durationMs"]
            assert isinstance(duration, (int, float)) and duration >= 0
            assert any(event.get("method") == "workspace/claudeUsage" for event in events)
            assert any(
                "SERENA_CLAUDE_RESUME_PROOF" in json.dumps(event)
                for event in events
                if event.get("method") == "item/completed"
                and event.get("params", {}).get("item", {}).get("type") == "agentMessage"
            )
            assert {path.name for path in project.iterdir()} == ({".mcp.json", ".claude"} if mcp_form else set()), "Proof changed its project"
            print(
                "PASS: exact Claude ID/history resumed through workspace adapter; real second response received"
            )
            finished.clear()
            before = len(events)
            await owner.submit([{"type": "text", "text": "/context"}])
            await asyncio.wait_for(finished.wait(), 30)
            results = [
                e["params"]["item"]
                for e in events[before:]
                if e["method"] == "item/completed"
                and e["params"]["item"]["type"] == "commandOutput"
            ]
            assert len(results) == 1 and "Context Usage" in results[0]["text"]
            catalog = await owner.list_commands()
            assert any(c["name"] == "context" for c in catalog["data"])
            assert owner.state == "ready"
            usage = await owner.context_usage()
            assert usage["maxTokens"] > 0 and usage["totalTokens"] >= 0 and usage["categories"]
            print("PASS: native context breakdown read without submitting another turn")
            print(
                "PASS: native /context output rendered as commandOutput; advertised commands discovered on same session"
            )
            if mcp_form:
                finished.clear()
                await owner.submit([{"type": "text", "text": "Transport test: call mcp__form_proof__ask exactly once with empty arguments. It requests a count via the native user form. Do not call any other tools, edit files, or run commands. After it returns, report its returned count."}])
                await asyncio.wait_for(finished.wait(), 120)
                assert receipt.exists(), "MCP tool did not return a form result"
                result = json.loads(receipt.read_text())
                assert result["action"] == "accept" and result["content"] == {"count": 2}, result
                assert any(event.get("method") == "mcpServer/elicitation/request" for event in events)
                assert [event for event in events if event.get("method") == "turn/completed"][-1]["params"]["turn"]["status"] == "completed"
                print("PASS: real MCP tool elicited a form through public SDK and pane owner; validated answer returned to server, parent turn completed")
            if background_task:
                finished.clear()
                before = len(events)
                await owner.submit([{"type": "text", "text": "Transport test: use Bash exactly once with command sleep 60 and run_in_background true. Do not run anything else. Once started, reply STARTED without waiting for or stopping the task. The test controller will stop it."}])
                async with asyncio.timeout(120):
                    while True:
                        if owner.state == "unavailable":
                            failures = [event.get("params", {}).get("reason") for event in events[before:] if event.get("method") == "workspace/error"]
                            raise RuntimeError(f"Native tool proof owner became unavailable: {failures}")
                        if (await owner.list_background_tasks())["data"]:
                            break
                        await asyncio.sleep(0.1)
                task = (await owner.list_background_tasks())["data"][0]
                await owner.terminate_background_task(task["processId"])
                async with asyncio.timeout(15):
                    while (await owner.list_background_tasks())["data"]:
                        await asyncio.sleep(0.1)
                assert owner.events.tasks[task["processId"]]["status"] in {"stopped", "killed"}
                await asyncio.wait_for(finished.wait(), 60)
                assert [event for event in events if event.get("method") == "turn/completed"][-1]["params"]["turn"]["status"] == "completed"
                streamed = [event["params"]["item"] for event in events[before:] if event.get("method") == "item/started" and event["params"].get("item", {}).get("inputStreaming")]
                assert streamed and any(item.get("inputJson") for item in streamed)
                assert any(item.get("tool") == "Bash" for item in streamed)
                print("PASS: actual native tool call and partial JSON reached pane events before complete tool input")
                print("PASS: native background task discovered and stopped by exact ID; parent turn completed normally")
            if bridge:
                await owner.close()
                owner = None
                host = WorkspaceHost(
                    journal=WorkspaceJournal(root / "bridge.db"),
                    resolve=lambda session: {
                        "session_id": session,
                        "provider": "claude",
                        "cwd": str(project),
                    },
                    factories={
                        "claude": lambda **kwargs: ClaudeWorkspace(
                            **kwargs,
                            lease_factory=lambda session: SessionLease(
                                session, directory=root / "leases"
                            ),
                        )
                    },
                )
                try:
                    assert (await asyncio.to_thread(host.attach, sid))["ok"]
                    warm = await asyncio.to_thread(host.command, sid, "queue-warm", "submit", {"inputs": [{"type": "text", "text": "Do not use tools. Count from 1 to 80, one number per line."}]})
                    assert warm["ok"]
                    response = await asyncio.to_thread(
                        host.bridge,
                        sid,
                        "claude",
                        "Do not use tools. Reply exactly SERENA_CLAUDE_BRIDGE_PROOF",
                        "bridge-proof",
                    )
                    assert response.get("queued"), "Native turn completed before queue proof"
                    edited = await asyncio.to_thread(host.command, sid, "queue-edit", "edit_queued_bridge", {"request_id": "bridge-proof", "expected_prompt": "Do not use tools. Reply exactly SERENA_CLAUDE_BRIDGE_PROOF", "prompt": "Do not use tools. Reply exactly SERENA_CLAUDE_EDITED_BRIDGE_PROOF"})
                    assert edited["result"] == {"edited": True}
                    async with asyncio.timeout(120):
                        while response.get("pending"):
                            await asyncio.sleep(0.1)
                            response = await asyncio.to_thread(host.bridge, sid, "claude", "Do not use tools. Reply exactly SERENA_CLAUDE_BRIDGE_PROOF", "bridge-proof")
                    assert response["ok"] and response["session_id"] == sid
                    assert "SERENA_CLAUDE_EDITED_BRIDGE_PROOF" in response["response"]
                    assert response["turn_id"] != warm["result"]["turn"]["id"]
                    print(
                        "PASS: edited queued text reached native Claude through the exact resumed owner; original receipt preserved"
                    )
                    running = await asyncio.to_thread(host.command, sid, "interrupt-warm", "submit", {"inputs": [{"type": "text", "text": "Do not use tools. Count from 1 to 100, one number per line."}]})
                    turn_id = running["result"]["turn"]["id"]
                    rejected = await asyncio.to_thread(host.command, sid, "stale-interrupt", "interrupt", {"expectedTurnId": "not-the-active-turn"})
                    assert not rejected["ok"]
                    stopped = await asyncio.to_thread(host.command, sid, "exact-interrupt", "interrupt", {"expectedTurnId": turn_id})
                    assert stopped["ok"]
                    async with asyncio.timeout(30):
                        while host._sessions[sid][0].state != "ready":
                            await asyncio.sleep(0.1)
                    after = 0
                    completion = None
                    while True:
                        page = await asyncio.to_thread(host.events, sid, after=after)
                        for envelope in page["events"]:
                            event = envelope["event"]
                            if event["method"] == "turn/completed" and event["params"]["turn"]["id"] == turn_id:
                                completion = event["params"]["turn"]
                        if not page["has_more"]:
                            break
                        after = page["cursor"]
                    assert completion is not None
                    print(f"PASS: stale stop rejected, exact native turn interrupted, owner ready; native completion status={completion['status']}")
                finally:
                    await asyncio.to_thread(host.shutdown)
        finally:
            try:
                if owner:
                    process = getattr(getattr(owner.client, "_transport", None), "_process", None)
                    wrapper = owner.client.transport.rpc.process if isinstance(owner.client, ClaudeTypeScriptClient) else None
                    await owner.close()
                    if process:
                        assert process.returncode is not None
                    if wrapper:
                        assert wrapper.returncode == 0
                await client.disconnect()
            finally:
                if original_config is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = original_config
        print("PASS: owned processes closed; isolated authentication/history removed on exit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-inference", action="store_true", required=True)
    parser.add_argument("--bridge", action="store_true")
    parser.add_argument("--background-task", action="store_true")
    parser.add_argument("--typescript-sdk")
    parser.add_argument("--mcp-form", action="store_true")
    args = parser.parse_args()
    if args.mcp_form and (not args.typescript_sdk or args.bridge or args.background_task):
        parser.error("--mcp-form requires --typescript-sdk and cannot combine with other proof modes")
    asyncio.run(main(bridge=args.bridge, background_task=args.background_task,
                     typescript_sdk=args.typescript_sdk, mcp_form=args.mcp_form))
