"""Prove native agent discovery/rejection without delegation or inference."""
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
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpcError


async def main():
    with tempfile.TemporaryDirectory(prefix="workspace-agents-proof-") as directory:
        root = Path(directory)
        home = root / ".codex"
        home.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(home),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}

        events = []

        async def publish(event):
            events.append(event)

        async def checkpoint(target):
            pass

        owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=root, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        try:
            await owner.create(binary=shutil.which("codex"), env=env, checkpoint=checkpoint)
            process = owner.rpc.process
            await owner.shell_command("echo agent-inspection-proof", True)
            async with asyncio.timeout(10):
                while owner.state != "ready":
                    await asyncio.sleep(.02)
            sid = owner.session_id
            unrelated = await owner.fork_session()
            other = unrelated["session_id"]
            assert other != sid
            request = owner.rpc.request
            calls = []

            async def observed(method, params):
                calls.append(method)
                return await request(method, params)

            owner.rpc.request = observed
            result = await owner.list_agents()
            assert result["data"] == [] and result["nextCursor"] is None
            try:
                await owner.inspect_agent(other)
            except WorkspaceRpcError as error:
                assert "does not belong" in str(error)
            else:
                raise AssertionError("Unrelated native thread was accepted")
            try:
                await owner.interrupt_agent(other, "not-a-child-turn", True)
            except WorkspaceRpcError as error:
                assert "does not belong" in str(error)
            else:
                raise AssertionError("Unrelated native thread stop was accepted")
            try:
                await owner.steer_agent(other, "not-a-child-turn", "Do not deliver this proof message")
            except WorkspaceRpcError as error:
                assert "does not belong" in str(error)
            else:
                raise AssertionError("Unrelated native thread message was accepted")
            try:
                await owner.continue_agent(other, "not-a-child-turn", "Do not deliver this proof message", True)
            except WorkspaceRpcError as error:
                assert "does not belong" in str(error)
            else:
                raise AssertionError("Unrelated native thread continuation was accepted")
            assert calls == ["thread/list", "thread/read", "thread/read", "thread/read", "thread/read"]
            assert owner.rpc.process is process and process.returncode is None
            assert owner.session_id == sid and owner.state == "ready"
            print(json.dumps({"nativeAgentList": "empty", "foreignThreadRejected": True, "foreignStopRejected": True, "foreignMessageRejected": True,
                              "foreignContinuationRejected": True, "sameProcess": True, "sameSession": True, "calls": calls,
                              "inference": False, "spawnedAgents": 0}))
            owner.rpc.request = request
            shell = asyncio.create_task(owner.shell_command("sleep 2", True))
            try:
                async with asyncio.timeout(5):
                    while owner.active_turn is None:
                        await asyncio.sleep(.01)
                turn = owner.active_turn
                page = await owner._history_page()
                assert [item["id"] for item in page["turns"] if item.get("status") == "inProgress"] == [turn]
                assert isinstance(await owner.interrupt(), dict)
                async with asyncio.timeout(5):
                    while owner.active_turn is not None:
                        await asyncio.sleep(.01)
                completed = [event["params"]["turn"] for event in events if event.get("method") == "turn/completed"
                             and event["params"]["turn"]["id"] == turn]
                assert len(completed) == 1 and completed[0]["status"] == "interrupted"
                print("PASS: native paged history exposes exact active shell turn; native completion status=interrupted")
            finally:
                await shell
        finally:
            await owner.close()
        assert process.returncode is not None
    print("PASS: native process reaped; disposable profile removed")


if __name__ == "__main__":
    asyncio.run(main())
