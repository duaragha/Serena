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
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


async def main(review=False, compact=False, permissions=False, bridge=False):
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
        if permissions:
            (home / "config.toml").write_text("[features]\nrequest_permissions_tool = true\n")
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
                    "developerInstructions": (
                        "This is a transport verification. Use no tools except request_permissions when explicitly asked. Never execute commands or edit files."
                        if permissions
                        else "This is a transport verification. Do not use tools. Reply with only the exact text requested."
                    ),
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
                if permissions and event.get("method") == "item/permissions/requestApproval":
                    await owner.answer(event["id"], {"permissions": {}, "scope": "turn"})
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
            assert await owner.list_background_tasks() == {"data": []}
            print("PASS: native background-task discovery on the exact resumed thread")
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
            if bridge:
                await owner.close()
                owner = None

                class BridgeOwner(CodexWorkspace):
                    async def open(self):
                        return await super().open(binary=binary, env=env)

                host = WorkspaceHost(
                    journal=WorkspaceJournal(root / "bridge.db"),
                    resolve=lambda session: {
                        "session_id": session,
                        "provider": "codex",
                        "cwd": str(project),
                    },
                    factories={
                        "codex": lambda **kwargs: BridgeOwner(
                            **kwargs,
                            lease_factory=lambda session: SessionLease(
                                session, directory=root / "leases"
                            ),
                        )
                    },
                )
                try:
                    assert (await asyncio.to_thread(host.attach, sid))["ok"]
                    warm = await asyncio.to_thread(
                        host.command,
                        sid,
                        "warm-turn",
                        "submit",
                        {"inputs": [{"type": "text", "text": "Reply exactly SERENA_BUSY_PROOF"}]},
                    )
                    assert warm["ok"]
                    response = await asyncio.to_thread(
                        host.bridge,
                        sid,
                        "codex",
                        "Reply exactly SERENA_BRIDGE_PROOF",
                        "proof-bridge",
                    )
                    assert response.get("queued"), "Proof did not encounter the running native turn"
                    async with asyncio.timeout(120):
                        while response.get("pending"):
                            await asyncio.sleep(0.1)
                            response = await asyncio.to_thread(
                                host.bridge,
                                sid,
                                "codex",
                                "Reply exactly SERENA_BRIDGE_PROOF",
                                "proof-bridge",
                            )
                    assert response["ok"] and response["session_id"] == sid
                    assert response["turn_id"] != warm["result"]["turn"]["id"]
                    assert "SERENA_BRIDGE_PROOF" in response["response"]
                    same = await asyncio.to_thread(
                        host.bridge,
                        sid,
                        "codex",
                        "Reply exactly SERENA_BRIDGE_PROOF",
                        "proof-bridge",
                    )
                    assert same == response
                    print(
                        "PASS: native host bridge returns exact-session output; repeated request reuses receipt"
                    )
                finally:
                    await asyncio.to_thread(host.shutdown)
            if permissions:
                finished.clear()
                before = len(published)
                await owner.submit(
                    [
                        {
                            "type": "text",
                            "text": "Call request_permissions to request network access only, explaining this is an isolated permission UI verification. Do not perform network access or any other tool call. After the answer, reply PERMISSION_PROOF_DONE.",
                        }
                    ],
                    options={"approvalPolicy": "on-request"},
                )
                await asyncio.wait_for(finished.wait(), 90)
                assert any(
                    e.get("method") == "item/permissions/requestApproval"
                    for e in published[before:]
                ), "Provider did not produce a permission request"
                assert any(
                    e.get("method") == "serverRequest/resolved" for e in published[before:]
                ), "Provider did not resolve the denied request"
                print(
                    "PASS: native permissions request denied through exact session adapter and resolved"
                )
            if compact:
                finished.clear()
                await owner.compact()
                await asyncio.wait_for(finished.wait(), 120)
                completion = [e for e in published if e.get("method") == "turn/completed"][-1]
                assert completion["params"]["turn"]["status"] == "completed"
                assert any(
                    e.get("method") == "item/completed"
                    and e.get("params", {}).get("item", {}).get("type") == "contextCompaction"
                    for e in published
                )
                assert owner.state == "ready"
                print("PASS: native context compaction completed on the same thread; owner ready")
            if review:
                finished.clear()
                result = await owner.review(
                    {
                        "type": "custom",
                        "instructions": "Transport verification only. Do not use tools or inspect files. Report no findings.",
                    }
                )
                assert result["reviewThreadId"] == sid
                await asyncio.wait_for(finished.wait(), 120)
                completed = [e for e in published if e.get("method") == "turn/completed"][-1]
                assert completed["params"]["turn"]["status"] == "completed"
                print("PASS: native review completed inline on the exact persisted thread")
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
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--permissions", action="store_true")
    parser.add_argument("--bridge", action="store_true")
    args = parser.parse_args()
    if args.bridge and (args.review or args.compact or args.permissions):
        parser.error("--bridge must run independently of other optional controls")
    asyncio.run(
        main(
            review=args.review,
            compact=args.compact,
            permissions=args.permissions,
            bridge=args.bridge,
        )
    )
