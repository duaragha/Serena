from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core import fleet_supervisor as supervisor
from core.fleet_policy import build_policy, builtin_config
from core.fleet_store import FleetStore
from core.fleet_supervision import FleetSupervisionStore
from core.fleet_workers import WorkerResult


def _run(store: FleetStore):
    return store.create_run(
        task="supervise one durable worker",
        activity="research",
        cwd=str(Path.cwd()),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=build_policy(
            "research", config=builtin_config(), provider_mode="codex", worker_count=1
        ).to_dict(),
    )


def test_attempt_lease_records_heartbeat_progress_and_projection(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg = run["phases"][0]["legs"][0]
    attempt = fleet.begin_attempt(leg["leg_id"])
    supervision = FleetSupervisionStore(fleet.path)

    lease = supervision.acquire(
        attempt["attempt_id"], now=100.0, lease_seconds=20, stall_seconds=60
    )
    assert lease.state == "active"
    assert supervision.heartbeat(
        attempt["attempt_id"], lease.lease_token, progress=True, now=105.0, lease_seconds=20
    )

    projection = supervision.project_run(run["run_id"], now=110.0)
    worker = projection["workers"][0]
    assert projection["active"] == 1
    assert worker["heartbeat_age_seconds"] == 5.0
    assert worker["progress_age_seconds"] == 5.0
    assert worker["lease_expired"] is False


def test_wrong_token_cannot_renew_or_release_a_worker(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    attempt = fleet.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    supervision = FleetSupervisionStore(fleet.path)
    lease = supervision.acquire(attempt["attempt_id"], now=10.0)

    assert supervision.heartbeat(attempt["attempt_id"], "wrong", now=11.0) is False
    assert supervision.release(attempt["attempt_id"], "wrong", state="failed") is False
    assert supervision.owns(attempt["attempt_id"], lease.lease_token, now=11.0) is True


def test_delayed_owner_recovers_until_reassignment_fences_it(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    attempt = fleet.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    supervision = FleetSupervisionStore(fleet.path)
    lease = supervision.acquire(attempt["attempt_id"], now=10.0, lease_seconds=1.0)

    assert supervision.heartbeat(
        attempt["attempt_id"], lease.lease_token, now=11.0, lease_seconds=1.0
    ) is True
    assert supervision.owns(
        attempt["attempt_id"], lease.lease_token, now=11.0
    ) is True
    worker = supervision.project_run(run["run_id"], now=11.0)["workers"][0]
    assert worker["state"] == "active"
    assert worker["lease_expired"] is False
    with sqlite3.connect(fleet.path) as connection:
        assert connection.execute(
            "SELECT state FROM fleet_attempts WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        ).fetchone()[0] == "running"

    assert supervision.fence_expired_leg(
        run["phases"][0]["legs"][0]["leg_id"], now=13.0
    )
    assert supervision.owns(
        attempt["attempt_id"], lease.lease_token, now=13.0
    ) is False


def test_worker_process_binding_is_fenced_and_visible(tmp_path, monkeypatch) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    attempt = fleet.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    supervision = FleetSupervisionStore(fleet.path)
    lease = supervision.acquire(attempt["attempt_id"], now=10.0)
    monkeypatch.setattr(
        "core.fleet_supervision.process_start_token", lambda pid: f"birth:{pid}"
    )

    assert supervision.bind_process(attempt["attempt_id"], "wrong", 4321) is False
    assert supervision.bind_process(attempt["attempt_id"], lease.lease_token, 4321)
    worker = supervision.project_run(run["run_id"], now=11.0)["workers"][0]
    assert worker["owner_pid"] == 4321
    assert worker["owner_token"] == "birth:4321"


def test_stall_retry_is_bounded_and_requeues_the_same_logical_leg(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    supervision = FleetSupervisionStore(fleet.path)

    first = fleet.begin_attempt(leg_id)
    first_lease = supervision.acquire(
        first["attempt_id"], now=10.0, stall_seconds=5.0, max_retries=1
    )
    assert supervision.mark_stalled(first["attempt_id"], first_lease.lease_token, now=16.0)
    fleet.finish_attempt(first["attempt_id"], state="interrupted", error="worker stalled")
    scheduled = supervision.schedule_stall_retry(
        first["attempt_id"], first_lease.lease_token, now=17.0
    )
    assert scheduled == {
        "scheduled": True,
        "reason": "worker stalled",
        "retries_used": 1,
        "max_retries": 1,
        "leg_id": leg_id,
    }
    assert fleet.get_run(run["run_id"])["phases"][0]["legs"][0]["state"] == "queued"

    second = fleet.begin_attempt(leg_id)
    second_lease = supervision.acquire(
        second["attempt_id"], now=20.0, stall_seconds=5.0, max_retries=1
    )
    assert second["resume_kind"] == "retry" or second["resume_kind"] is None
    assert supervision.mark_stalled(second["attempt_id"], second_lease.lease_token, now=26.0)
    fleet.finish_attempt(second["attempt_id"], state="interrupted", error="worker stalled again")
    exhausted = supervision.schedule_stall_retry(
        second["attempt_id"], second_lease.lease_token, now=27.0
    )
    assert exhausted["scheduled"] is False
    assert exhausted["reason"] == "stall retry budget exhausted"
    assert exhausted["retries_used"] == exhausted["max_retries"] == 1


def test_expired_lease_is_fenced_before_safe_reassignment(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    supervision = FleetSupervisionStore(fleet.path)

    first = fleet.begin_attempt(leg_id)
    first_lease = supervision.acquire(first["attempt_id"], now=10.0, lease_seconds=1.0)
    second = fleet.begin_attempt(leg_id)
    second_lease = supervision.acquire(second["attempt_id"], now=12.0, lease_seconds=10.0)

    assert second_lease.generation == first_lease.generation + 1
    assert supervision.owns(first["attempt_id"], first_lease.lease_token, now=12.0) is False
    assert supervision.owns(second["attempt_id"], second_lease.lease_token, now=12.0) is True
    with sqlite3.connect(fleet.path) as connection:
        state = connection.execute(
            "SELECT state FROM fleet_attempts WHERE attempt_id = ?", (first["attempt_id"],)
        ).fetchone()[0]
    assert state == "interrupted"


def test_interrupted_attempt_is_reassigned_without_waiting_for_lease_timeout(
    tmp_path,
) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    supervision = FleetSupervisionStore(fleet.path)

    first = fleet.begin_attempt(leg_id)
    first_lease = supervision.acquire(
        first["attempt_id"], now=10.0, lease_seconds=300.0
    )
    fleet.finish_attempt(first["attempt_id"], state="interrupted", error="service restarted")
    with sqlite3.connect(fleet.path) as connection:
        connection.execute(
            "UPDATE fleet_legs SET state = 'queued' WHERE leg_id = ?",
            (leg_id,),
        )
    second = fleet.begin_attempt(leg_id)
    second_lease = supervision.acquire(
        second["attempt_id"], now=11.0, lease_seconds=30.0
    )

    assert second_lease.generation == first_lease.generation + 1
    assert supervision.owns(first["attempt_id"], first_lease.lease_token, now=11.0) is False
    assert supervision.owns(second["attempt_id"], second_lease.lease_token, now=11.0) is True


def test_reassignment_fails_closed_when_expired_process_cannot_be_stopped(
    tmp_path, monkeypatch
) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    supervision = FleetSupervisionStore(fleet.path)

    first = fleet.begin_attempt(leg_id)
    first_lease = supervision.acquire(first["attempt_id"], now=10.0, lease_seconds=1.0)
    fleet.mark_attempt_process(first["attempt_id"], 999_999)
    calls = []
    monkeypatch.setattr(
        "core.fleet_supervision._terminate_owned_process",
        lambda pid, token: calls.append((pid, token)) or False,
    )

    try:
        supervision.fence_expired_leg(leg_id, now=12.0)
    except RuntimeError as exc:
        assert "reassignment refused" in str(exc)
    else:
        raise AssertionError("unsafe reassignment unexpectedly acquired a lease")

    assert calls == [(999_999, "pid:999999")]
    assert supervision.owns(first["attempt_id"], first_lease.lease_token, now=10.5)
    with sqlite3.connect(fleet.path) as connection:
        assert connection.execute(
            "SELECT state FROM fleet_attempts WHERE attempt_id = ?",
            (first["attempt_id"],),
        ).fetchone()[0] == "running"


def test_reassignment_uses_the_persisted_birth_token_before_a_new_generation(
    tmp_path, monkeypatch
) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    supervision = FleetSupervisionStore(fleet.path)

    first = fleet.begin_attempt(leg_id)
    first_lease = supervision.acquire(first["attempt_id"], now=10.0, lease_seconds=1.0)
    fleet.mark_attempt_process(first["attempt_id"], 999_998)
    terminated = []
    monkeypatch.setattr(
        "core.fleet_supervision._terminate_owned_process",
        lambda pid, token: terminated.append((pid, token)) or True,
    )

    assert supervision.fence_expired_leg(leg_id, now=12.0)
    assert terminated == [(999_998, "pid:999998")]
    second = fleet.begin_attempt(leg_id)
    second_lease = supervision.acquire(
        second["attempt_id"], now=12.0, lease_seconds=10.0
    )

    assert terminated == [(999_998, "pid:999998")] * 2
    assert second_lease.generation == first_lease.generation + 1


def test_restart_reconciliation_closes_an_interrupted_lease(tmp_path) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    attempt = fleet.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    supervision = FleetSupervisionStore(fleet.path)
    supervision.acquire(attempt["attempt_id"])
    fleet.finish_attempt(attempt["attempt_id"], state="interrupted", error="service exited")

    assert supervision.reconcile_run(run["run_id"], now=50.0) == 1
    worker = supervision.project_run(run["run_id"], now=51.0)["workers"][0]
    assert worker["state"] == "expired"
    assert worker["recovery_reason"] == "reconciled from durable attempt state"


def test_live_supervisor_stops_a_stalled_worker_and_exhausts_retry_budget(
    tmp_path, monkeypatch
) -> None:
    fleet = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(fleet)
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(fleet.path))
    monkeypatch.setenv("SERENA_FLEET_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setenv("SERENA_FLEET_LEASE_SECONDS", "0.2")
    monkeypatch.setenv("SERENA_FLEET_STALL_SECONDS", "0.05")
    monkeypatch.setenv("SERENA_FLEET_MAX_STALL_RETRIES", "1")
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", (str(fleet.path), fleet))
    monkeypatch.setattr(supervisor, "_reconcile_run_sessions", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(supervisor, "_refresh_index", lambda: None)
    monkeypatch.setattr(supervisor, "_release_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(supervisor, "_send_raghav_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(supervisor, "_completion_verdict", lambda *_args, **_kwargs: None)

    calls = 0

    def stalled_worker(request, *, cancel_requested, on_event):
        nonlocal calls
        calls += 1
        on_event("process.started", {"pid": 999_999, "event_log_path": ""})
        deadline = time.monotonic() + 2.0
        while not cancel_requested() and time.monotonic() < deadline:
            time.sleep(0.005)
        return WorkerResult(
            False,
            "",
            f"session-{request.attempt_id}",
            request.model,
            request.effort,
            -15,
            "cancelled by monitor",
            True,
        )

    monkeypatch.setattr(supervisor, "run_worker", stalled_worker)
    outcome = supervisor.run_supervisor(run["run_id"])

    assert outcome["state"] == "failed"
    assert calls == 2
    projection = FleetSupervisionStore(fleet.path).project_run(run["run_id"])
    assert projection["workers"][0]["state"] == "failed"
    assert projection["workers"][0]["retries_used"] == 1
    assert "phase did not complete" in str(outcome["error"])
