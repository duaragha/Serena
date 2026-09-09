"""Prove native history pagination with print-only shell turns, no inference."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Native Codex unavailable")
    with tempfile.TemporaryDirectory(prefix="serena-history-proof-") as temporary:
        root = Path(temporary)
        home, project = root / "home", root / "project"
        (home / ".codex").mkdir(parents=True)
        project.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "CODEX_HOME": str(home / ".codex"), "XDG_CONFIG_HOME": str(home / ".config"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        rpc, owner = WorkspaceRpc(), None
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            await rpc.request("initialize", {"clientInfo": {"name": "serena-history-proof", "version": "1"},
                                             "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized", {})
            sid = (await rpc.request("thread/start", {"cwd": str(project)}))["thread"]["id"]
            for number in range(51):
                await rpc.request("thread/shellCommand", {"threadId": sid,
                    "command": f"printf SERENA_HISTORY_{number:03d}", "timeoutMs": 5000})
                async with asyncio.timeout(15):
                    while True:
                        event = await rpc.events.get()
                        if event.get("method") == "turn/completed":
                            assert event["params"]["turn"]["status"] == "completed", event
                            break
            print("PASS: 51 native print-only turns persisted without inference")
            await rpc.close()
            events = []
            completed = asyncio.Event()
            async def publish(event):
                events.append(event)
                if event.get("method") == "turn/completed":
                    completed.set()
            owner = CodexWorkspace(session_id=sid, cwd=project, publish=publish,
                                   lease_factory=lambda session: SessionLease(session, directory=root / "leases"))
            history = await owner.open(binary=binary, env=env)
            assert len(history["thread"]["turns"]) == 50, len(history["thread"]["turns"])
            assert "SERENA_HISTORY_050" in json.dumps(history)
            assert "SERENA_HISTORY_000" not in json.dumps(history)
            assert owner.history_cursor
            host = WorkspaceHost(journal=WorkspaceJournal(root / "receipts.db"), resolve=None)
            host._sessions[sid] = (owner, "codex")
            refusal = await host._command(sid, "stale-read", "load_earlier", {"cursor": "not-the-current-cursor"})
            assert not refusal["ok"] and refusal["retryable"]
            receipt = await host._command(sid, "valid-read", "load_earlier", {"cursor": owner.history_cursor})
            assert receipt["ok"], receipt
            page = receipt["result"]
            assert len(page["turns"]) == 1 and page["historyCursor"] is None, page
            assert "SERENA_HISTORY_000" in json.dumps(page)
            assert events[-1]["method"] == "workspace/historyPage"
            assert owner.state == "ready" and owner.session_id == sid
            print("PASS: native resumed history loads 50 recent turns, then exact oldest full turn on demand")
            print("PASS: host rejects stale history with retryable receipt and routes valid read to unchanged native owner")
            completed.clear()
            start = len(events)
            payload = {"command": "printf SERENA_EXPLICIT_SHELL", "confirmed": True}
            shell = await host._command(sid, "explicit-shell", "shell_command", payload)
            assert shell["ok"], shell
            await asyncio.wait_for(completed.wait(), 10)
            assert await host._command(sid, "explicit-shell", "shell_command", payload) == shell
            assert owner.state == "ready"
            results = [e["params"]["item"] for e in events[start:] if e.get("method") == "item/completed"]
            assert any("SERENA_EXPLICIT_SHELL" in json.dumps(item) and item.get("exitCode") == 0 for item in results), results
            assert sum(e.get("method") == "turn/completed" for e in events[start:]) == 1
            print("PASS: explicit host shell command produced native output and exit 0 exactly once; repeated receipt did not rerun")
        finally:
            if owner:
                await owner.close()
            await rpc.close()
        assert not list(project.iterdir())
        print("PASS: isolated project untouched; native owners closed; no credentials used")


if __name__ == "__main__":
    asyncio.run(main())
