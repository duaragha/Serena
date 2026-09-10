"""Verify installed Windows Codex startup through the owned gate without inference."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.workspace_codex import CodexWorkspace  # noqa: E402
from core.workspace_lease import SessionLease  # noqa: E402
from core.workspace_rpc import WorkspaceRpc  # noqa: E402


async def main():
    if os.name != "nt":
        raise RuntimeError("This proof requires Windows")
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex is unavailable")
    with tempfile.TemporaryDirectory(prefix="serena-codex-windows-") as temporary:
        root = Path(temporary)
        (root / "codex").mkdir()
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
        env.update(HOME=str(root), USERPROFILE=str(root), CODEX_HOME=str(root / "codex"),
                   APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "localappdata"),
                   TEMP=str(root), TMP=str(root), OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        rpc = WorkspaceRpc()
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=root, env=env)
            assert rpc.windows_gated
            await rpc.request("initialize", {"clientInfo": {"name": "serena-isolated-proof", "version": "1"},
                                             "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized", {})
            result = await rpc.request("thread/list", {"limit": 1})
            assert result["data"] == [], "Isolated profile contains unexpected sessions"
            sid = (await rpc.request("thread/start", {"cwd": str(root)}))["thread"]["id"]
            for number in range(51):
                await rpc.request("thread/shellCommand", {"threadId": sid,
                    "command": f"echo SERENA_WINDOWS_SEED_{number:03d}", "timeoutMs": 5000})
                async with asyncio.timeout(15):
                    while True:
                        event = await rpc.events.get()
                        if event.get("method") == "turn/completed":
                            assert event["params"]["turn"]["status"] == "completed", event
                            break
        except Exception:
            await asyncio.sleep(.1)
            print("Native startup stderr:", "".join(rpc.stderr), file=sys.stderr)
            raise
        finally:
            await rpc.close()
        assert rpc.process is None
        completed, events = asyncio.Event(), []

        async def publish(event):
            events.append(event)
            if event.get("method") == "turn/completed":
                completed.set()

        owner = CodexWorkspace(session_id=sid, cwd=root, publish=publish,
                               lease_factory=lambda session: SessionLease(session, directory=root / "leases"))
        try:
            history = await owner.open(binary=binary, env=env)
            assert history["thread"]["id"] == sid
            assert len(history["thread"]["turns"]) == 50
            recent = json.dumps(history["thread"]["turns"])
            assert "SERENA_WINDOWS_SEED_050" in recent
            assert "SERENA_WINDOWS_SEED_000" not in recent
            original_pid = owner.rpc.process.pid
            cursor = owner.history_cursor
            assert cursor
            older = await owner.load_earlier(cursor)
            assert len(older["turns"]) == 1 and older["historyCursor"] is None
            assert "SERENA_WINDOWS_SEED_000" in json.dumps(older["turns"])
            assert owner.rpc.process.pid == original_pid
            await owner.shell_command("echo SERENA_WINDOWS_RESUMED", True)
            await asyncio.wait_for(completed.wait(), 15)
            items = [event["params"]["item"] for event in events if event.get("method") == "item/completed"]
            assert any(item.get("exitCode") == 0 and "SERENA_WINDOWS_RESUMED" in json.dumps(item) for item in items)
            assert owner.state == "ready"
        finally:
            await owner.close()
        assert owner.rpc.process is None
        print(json.dumps({"gated_startup": True, "isolated_empty_catalog": True,
                          "exact_resume": True, "native_command_exit_code": 0,
                          "recent_turns": 50, "older_turns": 1, "history_kept_owner": True,
                          "cleanup": True, "inference": False}))


if __name__ == "__main__":
    asyncio.run(main())
