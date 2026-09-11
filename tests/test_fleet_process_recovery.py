"""Real disposable process death through Fleet's native stream and scheduler."""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from test_fleet_checkout import setup_run
from test_fleet_supervision import _run
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet import supervisor
from fleet.isolation import FleetIsolationStore
from fleet.resources import resume_ready_resource_waits
from fleet.store import FleetStore

# ruff: noqa: F811


@pytest.mark.parametrize("exit_code", [0, 1, 0xC0000005])
def test_native_attempt_cannot_claim_missing_helper_recovery(tmp_path, exit_code):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    store.claim_run(run["run_id"])
    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
    store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=exit_code,
                         error="unrecorded result", input_blocker_reason="unrecorded result",
                         helper_outcome_missing=True)
    current = store.get_run(run["run_id"])
    assert current["resource_waits"] == []
    assert current["phases"][0]["legs"][0]["state"] == "waiting_for_input"
    assert not any(e["type"] == "worker.integration_replay_outcome_missing" for e in store.events(run["run_id"]))


def test_killed_writer_resumes_with_its_patch_and_completed_research(fleet_env, monkeypatch):
    root, _, store, run = setup_run(fleet_env)
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "on")
    killed = False

    def command(request):
        nonlocal killed
        events = [
            {"type": "thread.started", "thread_id": "disposable-process-fixture"},
            {"type": "thread.settings", "model": request.model, "reasoning_effort": request.effort},
        ]
        code = "import sys,json,os,signal,time; from pathlib import Path; sys.stdin.read();\n"
        code += "\n".join(f"print({json.dumps(event)!r}, flush=True)" for event in events) + "\n"
        if request.phase == "execute" and not killed:
            killed = True
            code += "Path('survived.txt').write_text('before death'); time.sleep(0.5); "
            if os.name == "nt":
                # Terminate only this disposable worker, with a recorded native
                # access-violation status. This simulates its OS exit, not an
                # actual illegal memory access or a model/provider request.
                code += "import _winapi; _winapi.TerminateProcess(_winapi.GetCurrentProcess(), 0xC0000005)\n"
            else:
                code += "os.kill(os.getpid(), signal.SIGKILL)\n"
        elif request.phase == "execute":
            code += "assert Path('survived.txt').read_text() == 'before death'; Path('survived.txt').write_text('recovered')\n"
        final = {"type": "item.completed", "item": {"type": "agent_message", "text": "disposable fixture checked"}}
        code += f"print({json.dumps(final)!r}, flush=True)\n"
        return [sys.executable, "-c", code]

    monkeypatch.setattr("fleet.workers.worker_command", command)
    parked = supervisor.run_supervisor(run["run_id"])
    assert parked["state"] == "waiting_for_resources", parked.get("error")
    assert parked["resource_waits"][0]["resource"] == "process"
    research_id = parked["phases"][0]["legs"][0]["current_attempt"]["attempt_id"]
    assert parked["phases"][1]["legs"][0]["current_attempt"]["exit_code"] == (0xC0000005 if os.name == "nt" else -9)
    isolation = FleetIsolationStore(fleet_env / "fleet-isolation.sqlite3")
    workspace = isolation.get_workspace(run["run_id"], "agent:a")
    assert Path(workspace.path, "survived.txt").read_text() == "before death"
    assert not Path(parked["cwd"], "survived.txt").exists()
    assert resume_ready_resource_waits(store, now=time.time() + 120)
    completed = supervisor.run_supervisor(run["run_id"])
    assert completed["state"] == "completed", completed.get("error")
    assert completed["phases"][0]["legs"][0]["current_attempt"]["attempt_id"] == research_id
    assert Path(completed["cwd"], "survived.txt").read_text() == "recovered"
    assert not (root / "survived.txt").exists()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("exit_code", [-9, 0xC0000005, 0xC0000602, -1073741819])
def test_process_retry_is_bounded_and_cancellation_never_wakes(tmp_path, cancelled, exit_code):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    store.claim_run(rid)
    leg = run["phases"][0]["legs"][0]
    for index in range(3):
        attempt = store.begin_attempt(leg["leg_id"])
        store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
        if cancelled:
            store.request_cancel(rid)
        store.finish_attempt(attempt["attempt_id"], state="cancelled" if cancelled else "failed",
                             exit_code=exit_code, error=f"worker interrupted with status {exit_code}")
        snapshot = store.get_run(rid)
        if cancelled:
            assert snapshot["resource_waits"] == []
            assert resume_ready_resource_waits(store, now=time.time() + 120) == []
            break
        if index < 2:
            assert snapshot["resource_waits"][0]["resource"] == "process"
            store = FleetStore(store.path)
            assert resume_ready_resource_waits(store, now=time.time() + 120) == [leg["leg_id"]]
        else:
            assert snapshot["resource_waits"] == []
            blocked = store.resolve_phase_failure(rid, "discover", "repeated process death")
            assert blocked["state"] == "waiting_for_input"
            assert store.next_queued_run() is None
