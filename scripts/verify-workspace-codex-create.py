"""Native create/checkpoint/lease/input/resume proof with no model invocation."""

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
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease, SessionOwnedError


async def main():
    binary = shutil.which("codex")
    assert binary, "Native Codex executable missing"
    with tempfile.TemporaryDirectory(prefix="workspace-create-proof-") as temporary:
        root = Path(temporary)
        project, home = root / "project", root / "home"
        project.mkdir()
        home.mkdir()
        (home / ".codex").mkdir()
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
               "XDG_CONFIG_HOME": str(home / ".config"), "PATH": os.environ["PATH"],
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        reservation = "new:" + str(uuid4())
        journal = WorkspaceJournal(root / "journal.db")
        events = []
        complete = asyncio.Event()
        async def publish(event):
            events.append(event)
            if event.get("method") == "turn/completed":
                assert event["params"]["turn"]["status"] == "completed"
                complete.set()
        async def checkpoint(target):
            assert not events
            journal.append(reservation, {"method": "workspace/created", "params": target})
        owner = CodexWorkspace(session_id=reservation, cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        try:
            result = await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            sid = owner.session_id
            assert result["thread"]["id"] == sid and result["thread"]["turns"] == []
            assert journal.read(reservation)["events"][0]["event"]["params"]["session_id"] == sid
            pid = owner.rpc.process.pid
            try:
                competing = SessionLease(sid, directory=root / "leases")
            except SessionOwnedError:
                pass
            else:
                competing.release()
                raise AssertionError("New native session was not exclusively owned")
            await owner.shell_command("printf SERENA_CREATED_NATIVE", True)
            await asyncio.wait_for(complete.wait(), 15)
            assert owner.rpc.process.pid == pid
            assert "SERENA_CREATED_NATIVE" in json.dumps(events)
            await owner.close()
            assert owner.rpc.process is None
            resumed = await owner.open(binary=binary, env=env)
            assert resumed["thread"]["id"] == sid
            assert "SERENA_CREATED_NATIVE" in json.dumps(resumed)
            paths = list((home / ".codex" / "sessions").rglob("*.jsonl"))
            assert len(paths) == 1, paths
            assert not list(project.iterdir())
            print("PASS: native creation checkpointed before output, exclusive lease transferred, same process accepted input")
            print("PASS: exact new session resumed with real output; one persisted transcript, zero inference, isolated project unchanged")
        except BaseException:
            print("\n".join(owner.rpc.stderr), file=sys.stderr)
            raise
        finally:
            await owner.close()
            assert owner.rpc.process is None
        print("PASS: native child reaped")


asyncio.run(main())
