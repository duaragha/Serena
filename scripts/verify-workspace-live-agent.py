"""Opt-in real child routing proof; never delegates repository implementation."""

import argparse
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
from core.workspace_lease import SessionLease


def reject_finished_parent(events, turn_id):
    """Do not keep polling after inference already failed or ended without a child."""
    for event in events:
        if event.get("method") != "turn/completed":
            continue
        turn = event.get("params", {}).get("turn", {})
        if turn.get("id") != turn_id:
            continue
        error = turn.get("error") or {}
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"Parent finished without a child ({turn.get('status', 'unknown')}): {message[:400]}")


async def main(model):
    source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    auth = json.loads(source.read_text())
    if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens"):
        raise RuntimeError("An existing ChatGPT subscription login is required")
    with tempfile.TemporaryDirectory(prefix="workspace-live-agent-") as temporary:
        root = Path(temporary)
        home, project = root / "codex", root / "project"
        home.mkdir(mode=0o700)
        project.mkdir()
        with open(home / "auth.json", "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump({"auth_mode": "chatgpt", "tokens": auth["tokens"]}, stream)
        (home / "config.toml").write_text(
            f'model = {json.dumps(model)}\nmodel_reasoning_effort = "medium"\n'
            'sandbox_mode = "read-only"\napproval_policy = "never"\nweb_search = "disabled"\n'
            '[agents]\nenabled = true\nmax_concurrent_threads_per_session = 1\n'
        )
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=str(root), CODEX_HOME=str(home))
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(key, None)
        events = []

        async def publish(event):
            events.append(event)

        async def checkpoint(target):
            pass

        owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        process = None
        try:
            await owner.create(binary=shutil.which("codex"), env=env, checkpoint=checkpoint)
            process = owner.rpc.process
            sid = owner.session_id
            started = await owner.submit([{"type": "text", "text": (
                "This is a bounded transport integration test, not a coding task. "
                "Spawn exactly ONE native subagent. Tell it: run only the shell command `sleep 20`, "
                "then reply exactly PROOF_CHILD_FINISHED. Do not read or write files, use network, "
                "or spawn further agents. You, the parent, must wait for this child and then report "
                "its terminal status in one short line. Do not do any repository work."
            )}])
            parent_turn = started["turn"]["id"]
            child = None
            async with asyncio.timeout(120):
                while child is None:
                    if owner.state == "unavailable":
                        raise RuntimeError("Parent event routing became unavailable: " + str(events[-1]))
                    children = (await owner.list_agents())["data"]
                    if len(children) > 1:
                        raise AssertionError("Proof spawned more than one child")
                    if children:
                        child = children[0]["id"]
                    else:
                        reject_finished_parent(events, parent_turn)
                        await asyncio.sleep(.25)
            print("PASS: one real native child discovered through parent connection", flush=True)
            async with asyncio.timeout(30):
                while True:
                    snapshot = await owner.inspect_agent(child)
                    running = [turn for turn in snapshot["thread"]["turns"] if turn.get("status") == "inProgress"]
                    if len(running) == 1:
                        child_turn = running[0]["id"]
                        break
                    await asyncio.sleep(.1)
            assert owner.active_turn == parent_turn
            message = "Keep waiting for your sleep command, then reply exactly PROOF_CHILD_STEERED. Do not use other tools."
            result = await owner.steer_agent(child, child_turn, message)
            assert result == {"accepted": True, "threadId": child, "turnId": child_turn}
            print("PASS: native steering acknowledged exact child turn; parent turn unchanged", flush=True)
            result = await owner.interrupt_agent(child, child_turn, True)
            assert result == {"requested": True, "threadId": child, "turnId": child_turn}
            async with asyncio.timeout(30):
                while True:
                    child_events = [event["params"]["event"] for event in events
                                    if event.get("method") == "workspace/agentEvent"
                                    and event["params"].get("agentThreadId") == child]
                    completed = [event["params"]["turn"] for event in child_events
                                 if event.get("method") == "turn/completed"
                                 and event["params"]["turn"]["id"] == child_turn]
                    if completed:
                        assert len(completed) == 1 and completed[0]["status"] == "interrupted"
                        break
                    if owner.state == "unavailable":
                        raise RuntimeError("Child event routing failed")
                    await asyncio.sleep(.05)
            assert child not in owner.active_agent_threads
            assert owner.session_id == sid and owner.rpc.process is process
            assert not owner.questions
            assert not list(project.iterdir()), "Proof unexpectedly wrote project files"
            print(json.dumps({"parent": sid, "child": child, "childTurn": child_turn,
                              "sameParentProcess": True, "nativeChildStatus": "interrupted",
                              "inference": True, "projectWrites": False}), flush=True)
        finally:
            await owner.close()
        assert process is not None and process.returncode is not None
    print("PASS: native process tree reaped and disposable profile removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-inference", action="store_true")
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()
    if not args.allow_inference:
        parser.error("This proof requires explicit --allow-inference")
    asyncio.run(main(args.model))
