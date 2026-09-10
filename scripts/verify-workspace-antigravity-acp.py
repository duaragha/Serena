"""Probe Google's separate ACP server without authenticating or opening a session."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_acp import WorkspaceAcpRpc


async def main():
    executable = Path(sys.argv[1]).resolve(strict=True)
    rpc = WorkspaceAcpRpc()
    with tempfile.TemporaryDirectory(prefix="serena-acp-probe-") as directory:
        home = Path(directory)
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
               "TMPDIR": str(home), "PATH": os.defpath, "LANG": "C.UTF-8"}
        try:
            await rpc.start([str(executable), "--uid="], cwd=home, env=env)
            result = await rpc.initialize(timeout=45)
            assert isinstance(result, dict) and result.get("protocolVersion") == 1, result
            assert isinstance(result.get("agentCapabilities"), dict), result
            assert isinstance(result.get("authMethods"), list), result
            print(json.dumps(result, sort_keys=True), flush=True)
            print("PASS: actual Google ACP initialization; no authenticate, session/new, session/load or prompt sent", flush=True)
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
