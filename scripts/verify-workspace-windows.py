"""Safe Windows transport proof: local JSON echo, no CLI provider or credentials."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace  # noqa: E402
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
        async def publish(event):
            raise AssertionError("Directory validation must not publish or start a session")
        owner = CodexWorkspace(session_id="local-directory-proof", cwd=Path(directory), publish=publish)
        other = Path(directory) / "other"
        other.mkdir()
        assert owner._same_project(directory.swapcase())
        assert not owner._same_project(str(other))
        assert owner.state == "closed" and owner.rpc.process is None
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
                          "provider_started": False, "directory_identity": True}))


if __name__ == "__main__":
    asyncio.run(main())
