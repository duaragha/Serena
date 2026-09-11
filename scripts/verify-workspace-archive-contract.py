"""Probe native archive/restore semantics in an isolated, unsigned profile."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


async def main():
    with tempfile.TemporaryDirectory(prefix="workspace-archive-contract-") as directory:
        root = Path(directory)
        home = root / "codex"
        home.mkdir()
        project = root / "project"
        project.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(home),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        binary = shutil.which("codex")
        events = []
        async def publish(event):
            events.append(event)
        async def checkpoint(target): pass
        owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        processes = []
        try:
            await owner.create(binary=binary, env=env, checkpoint=checkpoint)
            processes.append(owner.rpc.process)
            sid = owner.session_id
            await owner.shell_command("echo archive-contract-original", True)
            async with asyncio.timeout(10):
                while owner.state != "ready":
                    await asyncio.sleep(.02)
            original = list((home / "sessions").rglob(f"*{sid}.jsonl"))
            assert len(original) == 1
            response = await owner.rpc.request("thread/archive", {"threadId": sid})
            assert response == {}, response
            async with asyncio.timeout(10):
                while not any(event.get("method") == "thread/archived" and event.get("params", {}).get("threadId") == sid
                              for event in events):
                    await asyncio.sleep(.02)
            archived = list((home / "archived_sessions").rglob(f"*{sid}.jsonl"))
            assert len(archived) == 1 and not original[0].exists()
            assert "archive-contract-original" in archived[0].read_text()
            print("PASS: native archive confirmed exact ID and moved, rather than deleted, its real transcript")
        finally:
            await owner.close()

        rpc = WorkspaceRpc()
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            processes.append(rpc.process)
            await rpc.request("initialize", {"clientInfo": {"name": "serena-archive-proof", "version": "1"},
                                              "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized", {})
            read = await rpc.request("thread/read", {"threadId": sid, "includeTurns": True})
            assert read["thread"]["id"] == sid and "archive-contract-original" in json.dumps(read)
            restored = await rpc.request("thread/unarchive", {"threadId": sid})
            assert restored["thread"]["id"] == sid
            assert not archived[0].exists()
            assert len(list((home / "sessions").rglob(f"*{sid}.jsonl"))) == 1
            loaded = await rpc.request("thread/loaded/list", {})
            assert loaded["data"] == [], loaded
            print("PASS: archived history read and exact restore require no resumed writer or model turn")
        finally:
            await rpc.close()
        assert all(process.returncode is not None for process in processes)
        assert not list(project.iterdir())
        print(json.dumps({"ok": True, "processesReaped": len(processes), "credentialsUsed": False,
                          "inference": False, "archiveNotificationConfirmed": True, "restoreDoesNotResume": True}))
    print("PASS: disposable profile removed; no user sessions or project files changed")


if __name__ == "__main__":
    asyncio.run(main())
