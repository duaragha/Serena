"""Exercise exact native clear handoff using print-only commands, no credentials."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


def main():
    with tempfile.TemporaryDirectory(prefix="workspace-codex-clear-") as directory:
        root = Path(directory)
        (root / "codex").mkdir()
        project = root / "project"
        project.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(root / "codex"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        binary = shutil.which("codex")
        processes = []

        async def seed():
            async def publish(event): pass
            async def checkpoint(target): pass
            owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=project, publish=publish,
                                   lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
            try:
                await owner.create(binary=binary, env=env, checkpoint=checkpoint)
                processes.append(owner.rpc.process)
                await owner.shell_command("echo clear-original", True)
                async with asyncio.timeout(10):
                    while owner.state != "ready":
                        await asyncio.sleep(.02)
                return owner.session_id
            finally:
                await owner.close()

        source = asyncio.run(seed())

        class NativeOwner(CodexWorkspace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))

            async def open(self):
                result = await super().open(binary=binary, env=env)
                processes.append(self.rpc.process)
                return result

            async def create(self, *, checkpoint):
                result = await super().create(checkpoint=checkpoint, binary=binary, env=env)
                processes.append(self.rpc.process)
                return result

        host = WorkspaceHost(journal=WorkspaceJournal(root / "events.db"), factories={"codex": NativeOwner},
                             resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(project)})
        try:
            attached = host.attach(source)
            assert attached["ok"], attached
            owner = host._sessions[source][0]
            pid = owner.rpc.process.pid
            before = host.events(source)
            assert not host.command(source, "unconfirmed", "clear_session", {})["ok"]
            result = host.command(source, "clear-once", "clear_session", {"confirmed": True})
            assert result["ok"], result
            target = result["result"]["session_id"]
            assert target != source and owner.state == 'closed'
            owner = host._sessions[target][0]
            assert host._sessions[target][1] == "codex" and owner.rpc.process.pid != pid
            pid = owner.rpc.process.pid
            assert owner.thread["turns"] == [] and owner.state == "ready"
            assert host.command(source, "clear-once", "clear_session", {"confirmed": True}) == result
            assert host.attach(target)["ok"] and owner.rpc.process.pid == pid
            source_history = host.events(source)
            assert source_history["events"][:len(before["events"])] == before["events"]
            sent = host.command(target, "new-shell", "shell_command", {"command": "echo clear-target", "confirmed": True})
            assert sent["ok"], sent
            deadline = time.monotonic() + 10
            while owner.state != "ready" and time.monotonic() < deadline:
                time.sleep(.02)
            assert owner.state == "ready"
            assert "clear-target" in json.dumps(host.events(target))
            assert host.events(source) == source_history
            resumed = host.attach(source)
            assert resumed["ok"], resumed
            original = host._sessions[source][0]
            assert original is not owner and "clear-original" in json.dumps(original.thread)
            assert "clear-target" not in json.dumps(original.thread)
            assert owner.session_id == target and owner.rpc.process.pid == pid
            assert not list(project.iterdir())
        finally:
            host.shutdown()
        assert len(processes) == 4 and all(process.returncode is not None for process in processes)
        print(json.dumps({"ok": True, "idleProcessReplaced": True, "newExactSession": True,
                          "receiptDeduplicated": True, "oldHistoryResumable": True,
                          "newOutputNeverReachedSource": True, "processesReaped": 4,
                          "inference": False, "credentialsUsed": False}))
    print("PASS: temporary profile removed; project unchanged")


if __name__ == "__main__":
    main()
