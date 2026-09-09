"""Read-only protocol handshake against installed Codex. No thread or turn starts."""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_lease import SessionLease, SessionOwnedError
from core.workspace_rpc import WorkspaceRpc


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex is unavailable")
    rpc = WorkspaceRpc()
    temporary = tempfile.TemporaryDirectory(prefix="serena-ownership-proof-")
    directory = Path(temporary.name)
    lease = None
    process = None
    try:
        lease = SessionLease("probe-only-no-coding-session", directory=directory)
        lease.launching()
        await rpc.start(
            [binary, "app-server", "--stdio"],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ),
        )
        process = rpc.process
        lease.bind(process.pid)
        try:
            duplicate = SessionLease("probe-only-no-coding-session", directory=directory)
        except SessionOwnedError:
            print("PASS: second owner rejected while installed Codex probe is alive")
        else:
            duplicate.release()
            raise AssertionError("Second runtime owner was admitted")
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
        try:
            await rpc.close()
        finally:
            try:
                if lease:
                    lease.release()
                if process:
                    assert process.returncode is not None
                    recovered = SessionLease("probe-only-no-coding-session", directory=directory)
                    recovered.release()
                    print(f"Owned probe process reaped; child exit={process.returncode}")
            finally:
                temporary.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
