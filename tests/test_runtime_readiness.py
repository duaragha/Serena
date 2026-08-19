from __future__ import annotations

import sqlite3

import pytest

from core.runtime_readiness import (
    DependencyStatus,
    RecoveryPolicy,
    RuntimeCoordinator,
    RuntimeLedger,
    RuntimeState,
    run_short_soak,
)

COMPONENTS = ("brain", "voice", "tool_boundary", "primary_ui", "coding_jobs")


def test_runtime_ledger_declares_schema_version(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    RuntimeLedger(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_dependency_status_rejects_contradictory_state():
    with pytest.raises(ValueError, match="unavailable dependency cannot be ready"):
        DependencyStatus("brain", available=False, ready=True)


def test_runtime_ledger_migrates_v1_work_rows(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE runtime_recovery (
                component TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_allowed_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE runtime_unfinished_work (
                work_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            PRAGMA user_version=1;
            """
        )

    RuntimeLedger(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_unfinished_work)")}
        assert {"lease_owner", "lease_expires_at"} <= columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_runtime_ledger_refuses_to_downgrade_a_future_schema(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(RuntimeError, match="schema is newer"):
        RuntimeLedger(path)


def _probes(states):
    return {
        name: (
            lambda name=name: DependencyStatus(
                name=name,
                available=states[name][0],
                ready=states[name][0],
                critical=states[name][1],
                detail="ok" if states[name][0] else "unavailable",
                checked_at=10.0,
            )
        )
        for name in COMPONENTS
    }


def test_runtime_reports_ready_degraded_and_offline(tmp_path):
    states = {name: [True, True] for name in COMPONENTS}
    coordinator = RuntimeCoordinator(
        _probes(states), ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"), now=lambda: 10.0
    )

    assert coordinator.snapshot().state == RuntimeState.READY
    states["coding_jobs"] = [False, False]
    assert coordinator.snapshot().state == RuntimeState.DEGRADED
    states["brain"] = [False, True]
    snapshot = coordinator.snapshot()
    assert snapshot.state == RuntimeState.OFFLINE
    assert "brain" in snapshot.reason


def test_recovery_is_bounded_and_cooldown_is_durable(tmp_path):
    states = {name: [True, True] for name in COMPONENTS}
    states["brain"] = [False, True]
    attempts = []

    def recover():
        attempts.append("brain")
        return False

    ledger = RuntimeLedger(tmp_path / "runtime.sqlite3")
    coordinator = RuntimeCoordinator(
        _probes(states),
        recoverers={"brain": recover},
        ledger=ledger,
        policy=RecoveryPolicy(max_attempts=2, cooldown_seconds=30),
        now=lambda: 100.0,
    )

    first = coordinator.reconcile()
    second = coordinator.reconcile()

    assert first.after.state == RuntimeState.OFFLINE
    assert first.attempts[0].attempted is True
    assert second.attempts[0].detail == "cooldown active"
    assert attempts == ["brain"]
    assert ledger.recovery_state("brain")["attempts"] == 1


def test_successful_recovery_resets_attempt_budget(tmp_path):
    states = {name: [True, True] for name in COMPONENTS}
    states["brain"] = [False, True]

    def recover():
        states["brain"][0] = True
        return True

    ledger = RuntimeLedger(tmp_path / "runtime.sqlite3")
    result = RuntimeCoordinator(
        _probes(states),
        recoverers={"brain": recover},
        ledger=ledger,
        now=lambda: 10.0,
    ).reconcile()

    assert result.after.state == RuntimeState.READY
    assert result.attempts[0].recovered is True
    assert ledger.recovery_state("brain")["attempts"] == 0


def test_async_recovery_that_never_becomes_ready_still_exhausts_its_budget(tmp_path):
    states = {name: [True, True] for name in COMPONENTS}
    states["brain"] = [False, True]
    calls = []
    ledger = RuntimeLedger(tmp_path / "runtime.sqlite3")
    coordinator = RuntimeCoordinator(
        _probes(states),
        recoverers={"brain": lambda: calls.append("start") or True},
        ledger=ledger,
        policy=RecoveryPolicy(max_attempts=2, cooldown_seconds=0),
        now=lambda: 10.0,
    )

    assert coordinator.reconcile().after.state == RuntimeState.RECOVERING
    assert coordinator.reconcile().after.state == RuntimeState.RECOVERING
    exhausted = coordinator.reconcile()

    assert exhausted.after.state == RuntimeState.OFFLINE
    assert exhausted.attempts[0].detail == "attempt budget exhausted"
    assert calls == ["start", "start"]


def test_unfinished_work_resumes_after_reopening_store(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    work_id = RuntimeLedger(path).queue_work("briefing", {"date": "2026-08-08"}, now=1.0)
    seen = []

    reopened = RuntimeLedger(path)
    outcome = reopened.resume({"briefing": lambda payload: seen.append(payload) or True}, now=2.0)

    assert outcome["completed"] == [work_id]
    assert seen == [{"date": "2026-08-08"}]
    assert reopened.resumable_work() == []


def test_unfinished_work_has_a_failure_budget(tmp_path):
    ledger = RuntimeLedger(tmp_path / "runtime.sqlite3")
    work_id = ledger.queue_work("task", {"id": 1}, now=1.0)

    assert ledger.resume({"task": lambda _payload: False}, max_attempts=2, now=2.0)["queued"] == [
        work_id
    ]
    assert ledger.resume({"task": lambda _payload: False}, max_attempts=2, now=3.0)["failed"] == [
        work_id
    ]
    assert ledger.resumable_work() == []


def test_unfinished_work_is_atomically_leased_between_supervisors(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = RuntimeLedger(path)
    second = RuntimeLedger(path)
    work_id = first.queue_work("task", {"id": 1}, now=1.0)
    second_seen = []

    def first_handler(_payload):
        nested = second.resume(
            {"task": lambda payload: second_seen.append(payload) or True},
            now=2.0,
            worker_id="second",
        )
        assert nested["completed"] == []
        return True

    outcome = first.resume({"task": first_handler}, now=2.0, worker_id="first")

    assert outcome["completed"] == [work_id]
    assert second_seen == []


def test_stale_working_lease_is_recovered(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    ledger = RuntimeLedger(path)
    work_id = ledger.queue_work("task", {"id": 1}, now=1.0)
    claimed = ledger._claim_work(work_id, owner="dead_worker", now=2.0, lease_expires_at=3.0)
    assert claimed is not None

    seen = []
    assert ledger.resume(
        {"task": lambda payload: seen.append(payload) or True},
        now=4.0,
        worker_id="replacement",
    )["completed"] == [work_id]
    assert seen == [{"id": 1}]


def test_short_soak_is_deterministic_and_refuses_a_24_hour_claim(tmp_path):
    states = {name: [True, True] for name in COMPONENTS}
    coordinator = RuntimeCoordinator(
        _probes(states), ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"), now=lambda: 0.0
    )
    current = [0.0]

    report = run_short_soak(
        coordinator.snapshot,
        duration_seconds=10,
        interval_seconds=5,
        clock=lambda: current[0],
        sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )

    assert report.samples == 3
    assert report.states == {"ready": 3}

    try:
        run_short_soak(coordinator.snapshot, duration_seconds=86_400)
    except ValueError as error:
        assert "3600" in str(error)
    else:
        raise AssertionError("a 24-hour soak was accepted")
