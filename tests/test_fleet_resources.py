"""Resource failure receipts survive restart without silently abandoning a leg."""

import time
from types import SimpleNamespace

import pytest
from test_fleet_supervision import _run
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet.resources import (
    is_disk_exhaustion,
    is_transient_transport_error,
    resume_ready_resource_waits,
)
from fleet.store import FleetStore

# Fixture imported from the real supervisor harness.
# ruff: noqa: F811


@pytest.mark.parametrize("recover_first", ["disk", "capacity"])
def test_mixed_waits_survive_an_independent_failure_and_restart(tmp_path, monkeypatch, recover_first):
    from test_fleet_policy_store import _create

    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, worker_count=3)
    rid = run["run_id"]
    store.claim_run(rid)
    blocked, disk, capacity = run["phases"][0]["legs"]
    reason = "work stopped before completion: authority unavailable"
    for leg, error in [(blocked, reason), (disk, "[Errno 28] No space left on device"),
                       (capacity, "quota exhausted")]:
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error=error)
    store.request_capacity_wait(rid, capacity["leg_id"], failed_provider=capacity["runtime"],
                                eligible_providers=[capacity["runtime"]], reason="quota exhausted",
                                not_before=time.time())
    parked_run = store.resolve_phase_failure(rid, "discover", reason)
    assert parked_run["state"] == "waiting_for_capacity"
    assert len(parked_run["capacity_waits"]) == len(parked_run["resource_waits"]) == 1
    store = FleetStore(store.path)
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))

    for resource in [recover_first, "capacity" if recover_first == "disk" else "disk"]:
        leg = disk if resource == "disk" else capacity
        if resource == "disk":
            assert resume_ready_resource_waits(store, now=time.time() + 120) == [leg["leg_id"]]
        else:
            store.resume_capacity_wait(rid, leg["leg_id"], provider=leg["runtime"], reason="positive probe")
        assert store.claim_run(rid)
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="completed", output_text="recovered independently")
        parked_run = store.resolve_phase_failure(rid, "discover", reason)
    assert parked_run["state"] == "waiting_for_input"
    assert [leg["state"] for leg in parked_run["phases"][0]["legs"]] == [
        "waiting_for_input", "completed", "completed",
    ]
    assert parked_run["completed_at"] is None
    assert parked_run["capacity_waits"] == parked_run["resource_waits"] == []


def test_multiple_capacity_waits_do_not_resume_a_stale_run_snapshot(tmp_path):
    from test_fleet_policy_store import _create

    from fleet import supervisor

    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    rid = run["run_id"]
    store.claim_run(rid)
    for leg in run["phases"][0]["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error="quota exhausted")
        store.request_capacity_wait(rid, leg["leg_id"], failed_provider=leg["runtime"],
                                    eligible_providers=[leg["runtime"]], reason="quota exhausted",
                                    not_before=time.time())
    store.resolve_phase_failure(rid, "discover", "quota")
    # Supply the same positive observation shape used by the production probe.
    observed = {"codex": {"available": True}, "claude": {"available": True}}
    from unittest.mock import patch
    with patch.object(supervisor, "_capacity_recovered", return_value=(True, "available")):
        assert supervisor.resume_ready_capacity_waits(store, capacity=observed, now=time.time() + 120) == [rid]
    snapshot = store.get_run(rid)
    assert snapshot["state"] == "queued"
    assert len(snapshot["capacity_waits"]) == 1
    assert sorted(leg["state"] for leg in snapshot["phases"][0]["legs"]) == ["queued", "waiting_for_capacity"]


def test_scheduler_keeps_recoverable_lane_alive_beside_honest_stop(fleet_env, monkeypatch):
    from fleet import supervisor
    from fleet.workers import WorkerResult

    calls = []
    disk_failed = False

    def worker(request, **kwargs):
        nonlocal disk_failed
        calls.append((request.worker_key, request.phase))
        error = None
        if request.worker_key == "agent:a":
            error = "work stopped before completion: additional authority required"
        elif not disk_failed:
            disk_failed = True
            error = "[Errno 28] No space left on device"
        return WorkerResult(not error, "fixture evidence" if not error else "", "mixed-session",
                            request.model, request.effort, 1 if error else 0, error)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    run = supervisor.start_run("research two independent recovery fixtures", activity="research",
                               provider_mode="codex", worker_count=2, cwd=str(fleet_env))
    rid = run["run_id"]
    parked_run = supervisor.run_supervisor(rid)
    assert parked_run["state"] == "waiting_for_resources", parked_run.get("error")
    monkeypatch.setattr("fleet.resources.shutil.disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert resume_ready_resource_waits(supervisor._store(), now=time.time() + 120)
    recovered = supervisor.run_supervisor(rid)
    assert recovered["state"] == "waiting_for_input", recovered.get("error")
    assert calls.count(("agent:a", "discover")) == 1
    assert calls.count(("agent:b", "discover")) == 2
    assert recovered["phases"][0]["legs"][1]["state"] == "completed"


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


@pytest.mark.parametrize("message", [
    "permission denied: connection reset", "quota exhausted: connection reset",
    "work stopped before completion: connection reset", "identity mismatch",
    "completion evidence rejected: stream disconnected before completion",
    "integration gate failed: connection reset", "cancelled by user",
])
def test_transport_retries_never_override_authority_or_acceptance(message):
    assert not is_transient_transport_error(message)


def test_transport_retry_budget_survives_restart_and_preserves_provider(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    store.claim_run(rid)
    leg = run["phases"][0]["legs"][0]
    for index in range(3):
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error="connection reset by peer")
        snapshot = store.get_run(rid)
        current = snapshot["phases"][0]["legs"][0]
        assert current["runtime"] == leg["runtime"]
        assert current["model"] == leg["model"]
        if index == 2:
            assert current["state"] == "waiting_for_input"
            assert current["current_attempt"]["state"] == "failed"
            assert snapshot["resource_waits"] == []
            break
        wait = snapshot["resource_waits"][0]
        assert wait["resource"] == "transport"
        assert resume_ready_resource_waits(store, now=wait["not_before"] - 1) == []
        store = FleetStore(store.path)
        assert resume_ready_resource_waits(store, now=wait["not_before"] + 1) == [leg["leg_id"]]


def test_scheduler_recovers_transport_without_manual_retry(fleet_env, monkeypatch):
    from fleet import supervisor
    from fleet.workers import WorkerResult

    calls = []

    def worker(request, **kwargs):
        calls.append((request.phase, request.provider, request.model))
        if len(calls) == 1:
            return WorkerResult(False, "", None, request.model, request.effort, 1,
                                "stream disconnected before completion")
        return WorkerResult(True, "fixture result", "transport-session", request.model,
                            request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    run = supervisor.start_run("research a transient retry fixture", activity="research",
                               provider_mode="codex", worker_count=1, cwd=str(fleet_env))
    parked_run = supervisor.run_supervisor(run["run_id"])
    assert parked_run["state"] == "waiting_for_resources"
    assert resume_ready_resource_waits(supervisor._store(), now=time.time() + 120)
    completed = supervisor.run_supervisor(run["run_id"])
    assert completed["state"] == "completed", completed.get("error")
    assert calls[0] == calls[1]


@pytest.mark.parametrize("cancel", [False, True])
def test_honest_blocker_preserves_work_and_allows_scoped_resume(tmp_path, cancel):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    store.claim_run(rid)
    research = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.finish_attempt(research["attempt_id"], state="completed", output_text="preserved research")
    leg = run["phases"][1]["legs"][0]
    failed = store.begin_attempt(leg["leg_id"])
    reason = "work stopped before completion: missing authority to modify shared service"
    store.finish_attempt(failed["attempt_id"], state="failed", error=reason)
    parked_run = store.resolve_phase_failure(rid, "execute", reason)
    assert parked_run["state"] == "waiting_for_input"
    assert parked_run["completed_at"] is None
    assert store.next_queued_run() is None
    assert resume_ready_resource_waits(store, now=time.time() + 10000) == []
    store = FleetStore(store.path)
    if cancel:
        assert store.request_cancel(rid)["state"] == "cancelled"
        with pytest.raises(RuntimeError):
            store.request_leg_retry(rid, leg["leg_id"])
    else:
        store.add_steering(rid, "Keep shared services read-only; complete the local evidence instead.")
        resumed = store.request_leg_retry(rid, leg["leg_id"])
        assert resumed["state"] == "queued"
        assert resumed["phases"][1]["legs"][0]["state"] == "queued"
        assert resumed["phases"][0]["legs"][0]["current_attempt"]["attempt_id"] == research["attempt_id"]
