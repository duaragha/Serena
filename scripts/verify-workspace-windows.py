"""Safe Windows transport proof: local JSON echo, no CLI provider or credentials."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_rpc import WorkspaceRpc  # noqa: E402


async def main():
    if os.name != "nt":
        raise RuntimeError("Run this proof on Windows")
    peer = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line); print(json.dumps({'id':m['id'],'result':m['params']}),flush=True)\n"
    )
    rpc = WorkspaceRpc()
    with tempfile.TemporaryDirectory(prefix="serena-workspace-proof-") as directory:
        try:
            await rpc.start([sys.executable, "-u", "-c", peer], cwd=Path(directory), env=dict(os.environ))
            pid = rpc.process.pid
            payload = {"session": "local-transport-proof", "text": "first\nsecond"}
            answer = await rpc.request("echo", payload)
            assert answer == payload and rpc.process.pid == pid
            active = rpc._windows_job.active_processes()
            assert active >= 2
        finally:
            await rpc.close()
        assert rpc.process is None and rpc._windows_job is None
        print(json.dumps({"platform": sys.platform, "bidirectional": True,
                          "owned_processes": active, "closed": True,
                          "provider_started": False}))


if __name__ == "__main__":
    asyncio.run(main())
