"""Opt-in subscription inference proof in isolated storage, never a user session."""

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
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex unavailable")
    auth_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    auth = json.loads(auth_path.read_text())
    if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens"):
        raise RuntimeError("Proof requires existing ChatGPT subscription authentication")
    with tempfile.TemporaryDirectory(prefix="serena-codex-roundtrip-") as temporary:
        root = Path(temporary)
        home, project = root / "codex", root / "project"
        home.mkdir(mode=0o700)
        project.mkdir()
        target = home / "auth.json"
        with open(target, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump({"auth_mode": "chatgpt", "tokens": auth["tokens"]}, stream)
        env = strip_metered_auth_env(dict(os.environ))
        env["CODEX_HOME"] = str(home)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(key, None)
        rpc = WorkspaceRpc()
        owner = None
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "serena-roundtrip-proof", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await rpc.notify("initialized", {})
            started = await rpc.request(
                "thread/start",
                {
                    "cwd": str(project),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "developerInstructions": "This is a transport verification. Do not use tools. Reply with only the exact text requested.",
                    "config": {"features.shell_tool": False, "web_search": "disabled"},
                },
            )
            sid = started["thread"]["id"]
            await rpc.request(
                "turn/start",
                {
                    "threadId": sid,
                    "input": [{"type": "text", "text": "Reply exactly SERENA_FIRST_PROOF"}],
                },
            )
            async with asyncio.timeout(120):
                while True:
                    event = await rpc.events.get()
                    if event.get("method") == "turn/completed":
                        assert event["params"]["turn"]["status"] == "completed", event
                        break
                    if "id" in event:
                        raise RuntimeError("Unexpected interactive request during no-tool proof")
            first_process = rpc.process
            await rpc.close()
            assert first_process.returncode is not None
            print("PASS: isolated persisted first turn completed; original process reaped")
            finished = asyncio.Event()
            published = []

            async def publish(event):
                published.append(event)
                if event.get("method") == "turn/completed":
                    finished.set()

            owner = CodexWorkspace(
                session_id=sid,
                cwd=project,
                publish=publish,
                lease_factory=lambda session: SessionLease(session, directory=root / "leases"),
            )
            await owner.open(binary=binary, env=env)
            assert owner.thread["id"] == sid
            assert "SERENA_FIRST_PROOF" in json.dumps(owner.thread)
            await owner.submit([{"type": "text", "text": "Reply exactly SERENA_RESUME_PROOF"}])
            await asyncio.wait_for(finished.wait(), 120)
            completed = [e for e in published if e.get("method") == "turn/completed"][-1]
            assert completed["params"]["turn"]["status"] == "completed"
            assert any(
                "SERENA_RESUME_PROOF" in json.dumps(e)
                for e in published
                if e.get("method") == "item/completed"
                and e.get("params", {}).get("item", {}).get("type") == "agentMessage"
            )
            print(
                "PASS: exact persisted ID/history resumed through CodexWorkspace; real second-turn output received"
            )
            assert not list(project.iterdir()), "Proof unexpectedly changed its project"
        finally:
            if owner:
                owned_process = owner.rpc.process
                await owner.close()
                if owned_process:
                    assert owned_process.returncode is not None
            await rpc.close()
        print("PASS: owned processes closed; isolated auth/history storage removed on exit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-inference", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(main())
