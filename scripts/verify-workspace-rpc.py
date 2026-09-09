"""Read-only protocol handshake against installed Codex. No thread or turn starts."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_rpc import WorkspaceRpc


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex is unavailable")
    rpc = WorkspaceRpc()
    await rpc.start(
        [binary, "app-server", "--stdio"],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ),
    )
    process = rpc.process
    try:
        result = await rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "serena-workspace-probe", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=20,
        )
        assert isinstance(result, dict) and result.get("userAgent"), result
        await rpc.notify("initialized", {})
        print(
            "PASS: installed Codex app-server initialize/initialized over real bidirectional pipes"
        )
        print("No thread/start, thread/resume, turn/start, approval, or model request sent")
    finally:
        await rpc.close()
        assert process.returncode is not None
        print(f"Owned probe process reaped; child exit={process.returncode}")


if __name__ == "__main__":
    asyncio.run(main())
