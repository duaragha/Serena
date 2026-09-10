"""Resource failure receipts survive restart without silently abandoning a leg."""

import time
from types import SimpleNamespace

import pytest
from test_fleet_supervision import _run
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet.resources import is_disk_exhaustion, resume_ready_resource_waits
from fleet.store import FleetStore

# Fixture imported from the real supervisor harness.
# ruff: noqa: F811


def test_real_scheduler_parks_and_resumes_same_worker(fleet_env, monkeypatch):
    from fleet import supervisor
    from fleet.workers import WorkerResult

    failed_once = False
    calls = []

    def worker(request, **kwargs):
        nonlocal failed_once
        calls.append(request.phase)
        if request.phase == "execute" and not failed_once:
            failed_once = True
            return WorkerResult(False, "", "disk-session", request.model, request.effort,
                                1, "[Errno 28] No space left on device")
        return WorkerResult(True, "verified fixture", "disk-session", request.model,
                            request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    run = supervisor.start_run("research one resource recovery fixture", activity="research",
                               provider_mode="codex", worker_count=1, cwd=str(fleet_env))
    parked_run = supervisor.run_supervisor(run["run_id"])
    assert parked_run["state"] == "waiting_for_resources", parked_run.get("error")
    first = parked_run["phases"][0]["legs"][0]["current_attempt"]["attempt_id"]
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(supervisor._store(), now=time.time() + 60)
    completed = supervisor.run_supervisor(run["run_id"])
    assert completed["state"] == "completed", completed.get("error")
    assert completed["phases"][0]["legs"][0]["current_attempt"]["attempt_id"] == first
    assert calls.count("discover") == 1
    assert calls.count("execute") == 2


def parked(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    store.claim_run(rid)
    research = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.finish_attempt(research["attempt_id"], state="completed", output_text="keep this")
    leg = run["phases"][1]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.finish_attempt(attempt["attempt_id"], state="failed", error="[Errno 28] No space left on device")
    return store, rid, research, leg, attempt


def test_disk_wait_and_attempt_outcome_commit_together(tmp_path):
    store, rid, research, leg, attempt = parked(tmp_path)
    snapshot = store.get_run(rid)
    assert snapshot["phases"][1]["legs"][0]["state"] == "waiting_for_resources"
    assert snapshot["phases"][1]["legs"][0]["current_attempt"]["state"] == "failed"
    assert store.resolve_phase_failure(rid, "execute", "disk full")["resource_waiting"]
    assert store.get_run(rid)["state"] == "waiting_for_resources"


def test_disk_probe_survives_restart_preserves_completed_attempts(tmp_path, monkeypatch):
    store, rid, research, leg, attempt = parked(tmp_path)
    store.resolve_phase_failure(rid, "execute", "disk full")
    store = FleetStore(store.path)
    now = time.time() + 60
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    assert resume_ready_resource_waits(store, now=now) == []
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(store, now=now + 31) == [leg["leg_id"]]
    assert resume_ready_resource_waits(store, now=now + 32) == []
    snapshot = store.get_run(rid)
    assert snapshot["state"] == "queued"
    assert snapshot["phases"][0]["legs"][0]["current_attempt"]["attempt_id"] == research["attempt_id"]
    assert snapshot["phases"][0]["legs"][0]["state"] == "completed"
    assert snapshot["phases"][1]["legs"][0]["state"] == "queued"


def test_recovery_does_not_release_live_run_owner(tmp_path, monkeypatch):
    store, rid, _, leg, _ = parked(tmp_path)
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(store, now=time.time() + 60) == [leg["leg_id"]]
    assert store.get_run(rid)["state"] == "running"


def test_cancelled_wait_never_resumes(tmp_path, monkeypatch):
    store, rid, _, _, _ = parked(tmp_path)
    store.resolve_phase_failure(rid, "execute", "disk full")
    assert store.request_cancel(rid)["state"] == "cancelled"
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(store, now=time.time() + 60) == []


def test_late_old_attempt_cannot_overwrite_replacement(tmp_path, monkeypatch):
    store, rid, _, leg, old = parked(tmp_path)
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(store, now=time.time() + 60)
    replacement = store.begin_attempt(leg["leg_id"])
    store.finish_attempt(old["attempt_id"], state="completed", output_text="late old result")
    current = store.get_run(rid)["phases"][1]["legs"][0]
    assert current["state"] == "running"
    assert current["current_attempt"]["attempt_id"] == replacement["attempt_id"]
    store.finish_attempt(replacement["attempt_id"], state="completed", output_text="accepted")
    store.finish_attempt(replacement["attempt_id"], state="failed", error="late duplicate")
    current = store.get_run(rid)["phases"][1]["legs"][0]
    assert current["state"] == "completed"
    assert current["current_attempt"]["output_text"] == "accepted"


@pytest.mark.parametrize("message", ["permission denied", "network unreachable", "quota exhausted"])
def test_unrelated_failures_are_not_disk_waits(message):
    assert not is_disk_exhaustion(message)
