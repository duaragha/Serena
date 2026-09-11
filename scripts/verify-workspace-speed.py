"""Verify native session speed and restoration without authentication or inference."""
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
    with tempfile.TemporaryDirectory(prefix="workspace-speed-") as directory:
        root = Path(directory)
        (root / "codex").mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(root / "codex"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        binary = shutil.which("codex")
        processes = []

        async def seed():
            async def publish(event): pass
            async def checkpoint(target): pass
            owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=root, publish=publish,
                                   lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
            try:
                await owner.create(binary=binary, env=env, checkpoint=checkpoint)
                processes.append(owner.rpc.process)
                await owner.shell_command("echo speed-proof", True)
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
        chosen = None
        for index in range(3):
            host = WorkspaceHost(journal=journal, factories={"codex": NativeOwner},
                                 resolve=lambda identity: {"session_id": identity, "provider": "codex", "cwd": directory})
            try:
                after = host.events(sid)["cursor"]
                attached = host.attach(sid)
                assert attached["ok"], {"replacement": index, "result": attached}
                owner = host._sessions[sid][0]
                model = owner.settings["model"]
                if index == 0:
                    catalog = host.command(sid, "list", "speed_tiers", {})["result"]
                    assert catalog["options"], catalog
                    chosen = catalog["options"][0]["id"]
                    result = host.command(sid, "set", "set_speed_tier", {"value": chosen, "expected_model": model})
                    assert result["ok"], result
                    assert host.command(sid, "set", "set_speed_tier", {"value": chosen, "expected_model": model}) == result
                expected = chosen if index < 2 else "default"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    rows = host.events(sid, after=after)["events"]
                    if owner.settings.get("serviceTier") == expected and any(row["event"].get("method") == "thread/settings/updated"
                           and "serviceTier" in row["event"]["params"].get("threadSettings", {})
                           and row["event"]["params"].get("threadSettings", {}).get("serviceTier") == expected
                           for row in rows):
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError({"replacement": index, "expected": expected, "settings": owner.settings,
                                          "native": [row["event"] for row in rows if row["event"].get("method") == "thread/settings/updated"]})
                assert owner.settings["serviceTier"] == expected and owner.settings["model"] == model
                assert owner.state == "ready" and len(owner.thread.get("turns", [])) == 1
                if index == 1:
                    assert host.command(sid, "clear", "set_speed_tier", {"value": None, "expected_model": model})["ok"]
                if index == 2:
                    assert journal.saved_codex_speed(sid)["value"] is None
            finally:
                host.shutdown()
        assert len(processes) == 4 and all(process.returncode is not None for process in processes)
        print(json.dumps({"exactSession": sid, "nativeSpeed": chosen, "restored": True, "clearRestored": True,
                          "processesReaped": 4, "inference": False}))
    print("PASS: disposable profile removed; no authentication used")


if __name__ == "__main__":
    main()
