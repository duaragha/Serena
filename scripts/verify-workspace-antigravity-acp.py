"""Probe Google's separate ACP server without authenticating or opening a session."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_acp import WorkspaceAcpRpc
from core.workspace_rpc import WorkspaceRpcError


async def main():
    executable = Path(sys.argv[1]).resolve(strict=True)
    rpc = WorkspaceAcpRpc()
    with tempfile.TemporaryDirectory(prefix="serena-acp-probe-") as directory:
        home = Path(directory)
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
               "TMPDIR": str(home), "PATH": os.defpath, "LANG": "C.UTF-8"}
        sid = "11111111-2222-4333-8444-555555555555"
        cli_db = home / ".gemini" / "antigravity-cli" / "conversations" / f"{sid}.db"
        cli_db.parent.mkdir(parents=True)
        with sqlite3.connect(cli_db) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB)")
        original = cli_db.read_bytes()
        try:
            await rpc.start([str(executable), "--uid="], cwd=home, env=env)
            result = await rpc.initialize(timeout=45)
            assert isinstance(result, dict) and result.get("protocolVersion") == 1, result
            assert isinstance(result.get("agentCapabilities"), dict), result
            assert isinstance(result.get("authMethods"), list), result
            print(json.dumps(result, sort_keys=True), flush=True)
            print("PASS: actual Google ACP initialization; no authenticate, session/new, session/load or prompt sent", flush=True)
            sessions = await rpc.request("session/list", {}, timeout=10)
            assert sessions.get("sessions") == [], sessions
            try:
                await rpc.request("session/load", {"sessionId": sid, "cwd": str(home), "mcpServers": []}, timeout=10)
            except WorkspaceRpcError as error:
                assert "-32002" in str(error) and "Session not found" in str(error), str(error)
            else:
                raise AssertionError("ACP unexpectedly loaded a CLI-only fixture")
            assert cli_db.read_bytes() == original
            assert not (home / ".gemini" / "antigravity-acp" / "conversations" / f"{sid}.db").exists()
            print("PASS: CLI-only SQLite fixture absent from ACP list and rejected on exact load; original unchanged and no replacement created", flush=True)
        except Exception:
            print("Native stderr:", "".join(rpc.stderr)[-6000:], file=sys.stderr)
            raise
        finally:
            process = rpc.process
            await rpc.close()
            assert process is not None and process.returncode is not None
            print(f"PASS: isolated ACP server reaped, exit {process.returncode}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
