"""Persist and restore native Codex modes across owned processes, without inference."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.billing import strip_metered_auth_env
from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


async def main():
    binary = shutil.which("codex")
    assert binary, "Native Codex is required"
    with tempfile.TemporaryDirectory(prefix="serena-mode-resume-") as directory:
        root = Path(directory)
        (root / ".codex").mkdir()
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=directory, USERPROFILE=directory, CODEX_HOME=str(root / ".codex"))
        journal = WorkspaceJournal(root / "journal.db")

        class LocalOwner(CodexWorkspace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))

            async def open(self):
                return await super().open(binary=binary, env=env)

        async def publish(event):
            journal.append(owner.session_id, event)

        async def checkpoint(identity):
            (root / "identity.json").write_text(json.dumps(identity))

        owner = LocalOwner(session_id="new:" + str(uuid4()), cwd=root, publish=publish)
        pids = []
        try:
            await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            sid = owner.session_id
            await owner.shell_command("echo mode-proof", True)
            async with asyncio.timeout(10):
                while owner.state != "ready":
                    await asyncio.sleep(.01)
            model, effort = owner.settings["model"], owner.settings.get("reasoningEffort")
            for mode in ("plan", "default"):
                await owner.set_session_mode(mode)
                assert journal.saved_codex_mode(sid) == mode
                old = owner.rpc.process
                pids.append(old.pid)
                await owner.close()
                assert old.returncode is not None
                host = WorkspaceHost(journal=journal,
                                     resolve=lambda value: {"session_id": value, "provider": "codex", "cwd": directory},
                                     factories={"codex": LocalOwner})
                assert host.events(sid)["runtime"] is None and not host._sessions
                result = await host._attach(sid)
                assert result["ok"], result
                owner = host._sessions[sid][0]
                assert owner.session_id == sid and owner.rpc.process.pid != old.pid
                assert owner.settings["collaborationMode"] == mode
                assert owner.settings["model"] == model and owner.settings.get("reasoningEffort") == effort
                assert (host._work_admission_error(sid) == "Native session is in Plan mode") == (mode == "plan")
                same = owner.rpc.process
                assert (await host._attach(sid))["ok"] and owner.rpc.process is same
                assert owner.active_turn is None and owner.state == "ready"
            final = owner.rpc.process
        finally:
            await owner.close()
        assert final.returncode is not None
    assert not root.exists()
    print(json.dumps({"ok": True, "restoredModes": ["plan", "default"], "sameSession": True,
                      "modelAndEffortPreserved": True, "readDidNotLaunch": True,
                      "oldProcessesReapedBeforeResume": len(pids), "inference": False,
                      "shellCommand": "echo mode-proof", "profileRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
