"""Safe installed-Codex host proof: initialization only, no coding thread or turn."""

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.billing import strip_metered_auth_env
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_rpc import WorkspaceRpc


def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex is unavailable")
    owners = []

    class InitializationProbe:
        def __init__(self, *, session_id, cwd, publish):
            self.rpc = WorkspaceRpc()
            self.cwd, self.publish = cwd, publish
            self.state, self.active_turn = "closed", None
            owners.append(self)

        async def open(self):
            await self.rpc.start(
                [binary, "app-server", "--stdio"],
                cwd=self.cwd,
                env=strip_metered_auth_env(dict(os.environ)),
            )
            result = await self.rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "serena-host-probe", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            assert result.get("userAgent"), result
            await self.rpc.notify("initialized", {})
            await self.publish({"method": "workspace/probe", "params": {"initialized": True}})
            self.state = "ready"

        async def close(self):
            await self.rpc.close()

    with tempfile.TemporaryDirectory(prefix="serena-host-proof-") as directory:
        journal = WorkspaceJournal(Path(directory) / "journal.db")
        host = WorkspaceHost(
            journal=journal,
            resolve=lambda sid: {"session_id": sid, "provider": "probe", "cwd": directory},
            factories={"probe": InitializationProbe},
        )
        try:
            assert host.events("probe-only")["events"] == []
            assert not owners
            upload = host.uploads.save(
                "probe-only", "proof.txt", io.BytesIO(b"exact attachment bytes")
            )
            path, _ = host.uploads.resolve("probe-only", upload["token"])
            assert path.read_bytes() == b"exact attachment bytes"
            try:
                host.uploads.resolve("different-session", upload["token"])
            except ValueError:
                pass
            else:
                raise AssertionError("Attachment crossed session ownership")
            print("PASS: private attachment persisted byte-for-byte; cross-session lookup rejected")
            assert host.attach("probe-only")["ok"]
            process = owners[0].rpc.process
            assert host.attach("probe-only")["ok"]
            assert len(owners) == 1 and owners[0].rpc.process is process
            events = host.events("probe-only")["events"]
            assert events[0]["event"]["params"]["initialized"] is True
            assert host.events("probe-only")["events"] == events
            print("PASS: explicit host attachment initializes one installed Codex process")
            print("PASS: repeated attachment and journal replay preserve the same owner")
            print("No thread/start, thread/resume, turn/start, terminal, or model request sent")
        finally:
            host.shutdown()
        assert process.returncode is not None
        print(f"PASS: explicit host shutdown reaped probe child; exit={process.returncode}")


if __name__ == "__main__":
    main()
