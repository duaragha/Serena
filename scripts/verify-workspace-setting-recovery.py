"""Exercise failed speed restoration and explicit recovery on a real native owner."""

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
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


def main():
    with tempfile.TemporaryDirectory(prefix="workspace-setting-recovery-") as directory:
        root = Path(directory)
        (root / "codex").mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(root / "codex"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        binary = shutil.which("codex")
        processes = []

        async def seed():
            async def publish(event):
                pass

            async def checkpoint(target):
                pass

            owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=root, publish=publish,
                                   lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
            try:
                await owner.create(binary=binary, env=env, checkpoint=checkpoint)
                processes.append(owner.rpc.process)
                await owner.shell_command("echo setting-recovery-proof", True)
                async with asyncio.timeout(10):
                    while owner.state != "ready":
                        await asyncio.sleep(.02)
                return owner.session_id
            finally:
                await owner.close()

        sid = asyncio.run(seed())

        class NativeOwner(CodexWorkspace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, lease_factory=lambda value: SessionLease(value, directory=root / "leases"))

            async def open(self):
                result = await super().open(binary=binary, env=env)
                processes.append(self.rpc.process)
                return result

        journal = WorkspaceJournal(root / "events.db")
        journal.append(sid, {"method": "workspace/settings", "params": {"collaborationMode": "plan"}})
        journal.append(sid, {"method": "workspace/speed", "params": {"model": "old", "value": "invalid-proof-speed"}})
        host = WorkspaceHost(journal=journal, factories={"codex": NativeOwner},
                             resolve=lambda identity: {"session_id": identity, "provider": "codex", "cwd": directory})
        try:
            failed = host.attach(sid)
            assert not failed["ok"] and failed["setting_recovery"]["setting"] == "speed", failed
            owner = host._sessions[sid][0]
            assert owner.can_retry_attachment() and all(p.returncode is not None for p in processes)
            payload = {"failure_id": failed["setting_recovery"]["failure_id"], "confirmed": True}
            assert not host.command(sid, "unconfirmed", "reset_saved_setting", {**payload, "confirmed": False})["ok"]
            assert journal.saved_codex_speed(sid)["value"] == "invalid-proof-speed"
            result = host.command(sid, "recover", "reset_saved_setting", payload)
            assert result["ok"] and result["result"]["reconnectRequired"], result
            assert host.command(sid, "recover", "reset_saved_setting", payload) == result
            assert len(processes) == 2 and host._sessions[sid][0] is owner and owner.can_retry_attachment()
            assert journal.saved_codex_speed(sid) is None
            assert journal.saved_codex_mode(sid) == "plan"
            attached = host.attach(sid)
            assert attached["ok"], attached
            resumed = host._sessions[sid][0]
            assert resumed.session_id == sid and resumed.state == "ready"
            assert resumed.settings["collaborationMode"] == "plan"
            assert len(resumed.thread["turns"]) == 1
            assert host._work_admission_error(sid) == "Native session is in Plan mode"
        finally:
            host.shutdown()
        assert len(processes) == 3 and all(p.returncode is not None for p in processes)
        print(json.dumps({"ok": True, "sessionId": sid, "failedOwnerClosed": True,
                          "resetDidNotLaunch": True, "sameNativeHistory": True, "planModePreserved": True,
                          "processesReaped": 3, "authentication": False, "inference": False}))
    print("PASS: temporary profile removed; no installed app or user session changed")


if __name__ == "__main__":
    main()
