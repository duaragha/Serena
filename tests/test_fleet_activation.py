"""Parked-run activation cannot kill live work or race a new dispatch."""

import os
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from fleet import activation


@pytest.fixture
def activation_env(tmp_path, monkeypatch):
    path = tmp_path / "fleet.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE fleet_runs(run_id TEXT,state TEXT,owner_pid INTEGER);
            CREATE TABLE fleet_attempts(attempt_id TEXT,state TEXT,pid INTEGER);
            CREATE TABLE fleet_peer_help(id TEXT,state TEXT,pid INTEGER);
            CREATE TABLE fleet_lesson_reviews(id TEXT,state TEXT,pid INTEGER);
            CREATE TABLE fleet_worker_leases(attempt_id TEXT,state TEXT,owner_pid INTEGER);
            INSERT INTO fleet_runs VALUES ('work','waiting_for_input',NULL);
            INSERT INTO fleet_attempts VALUES ('saved','completed',NULL);
        """)
    monkeypatch.setattr(activation, "activation_gate", lambda *_: {
        "reasons": ["one or more Fleet runs are still active"],
        "active_runs": [{"run_id": "work", "state": "waiting_for_input"}],
    })
    calls = []

    def systemctl(command, **kwargs):
        calls.append(command)
        if "restart" in command:
            # The critical proof: another dispatcher cannot claim while the
            # restart runs. No window between inspection and stopping Fleet.
            with sqlite3.connect(path, timeout=0) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("BEGIN IMMEDIATE")
        return SimpleNamespace(stdout=f"WorkingDirectory={tmp_path}\nType=simple\nActiveState=active\n")

    monkeypatch.setattr(activation.subprocess, "run", systemctl)
    return tmp_path, path, calls


@pytest.mark.parametrize("state", sorted(activation.PARKED))
def test_restart_preserves_parked_work_under_dispatch_lock(activation_env, state):
    root, path, calls = activation_env
    with sqlite3.connect(path) as db:
        db.execute("UPDATE fleet_runs SET state=?", (state,))
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    result = activation.restart_parked_fleet(root, root / "receipt.json", path)
    assert result["passed"] and result["restarted"]
    assert calls[-1] == ["systemctl", "--user", "restart", "serena-fleet.service"]
    assert all("serena-mobile-host" not in part for command in calls for part in command)
    with sqlite3.connect(path, timeout=0) as db:
        assert list(db.iterdump()) == before
        db.execute("BEGIN IMMEDIATE")  # Dispatch is released afterward.


@pytest.mark.parametrize("table", ["fleet_attempts", "fleet_peer_help", "fleet_lesson_reviews", "fleet_worker_leases"])
@pytest.mark.parametrize("state,pid", [("running", None), ("completed", os.getpid())])
def test_active_or_orphan_execution_prevents_restart(activation_env, table, state, pid):
    root, path, calls = activation_env
    with sqlite3.connect(path) as db:
        db.execute(f"INSERT INTO {table} VALUES ('live',?,?)", (state, pid))
    result = activation.restart_parked_fleet(root, root / "receipt.json", path)
    assert not result["passed"] and not result["restarted"]
    assert calls == []


@pytest.mark.parametrize("state", ["queued", "running", "stopping", "unknown"])
def test_nonparked_work_cannot_be_stopped(activation_env, state):
    root, path, calls = activation_env
    with sqlite3.connect(path) as db:
        db.execute("UPDATE fleet_runs SET state=?", (state,))
    assert not activation.restart_parked_fleet(root, root / "receipt.json", path)["passed"]
    assert calls == []


def test_failed_source_acceptance_still_refuses(activation_env, monkeypatch):
    root, path, calls = activation_env
    monkeypatch.setattr(activation, "activation_gate", lambda *_: {
        "reasons": ["source changed after acceptance"], "active_runs": [],
    })
    assert not activation.restart_parked_fleet(root, root / "receipt.json", path)["passed"]
    assert calls == []


def test_missing_database_is_not_created(activation_env):
    root, _, calls = activation_env
    absent = root / "missing.sqlite3"
    assert not activation.restart_parked_fleet(root, root / "receipt.json", absent)["passed"]
    assert not absent.exists()
    assert calls == []


@pytest.mark.parametrize("properties", ["Type=notify", "WorkingDirectory=/wrong", "ActiveState=inactive"])
def test_unverified_service_is_never_restarted(activation_env, monkeypatch, properties):
    root, path, _ = activation_env
    calls = []
    def show(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=properties)
    monkeypatch.setattr(activation.subprocess, "run", show)
    assert not activation.restart_parked_fleet(root, root / "receipt.json", path)["passed"]
    assert len(calls) == 1 and "show" in calls[0]


def test_permission_denied_process_probe_fails_closed(monkeypatch):
    def denied(*_):
        raise PermissionError("unverifiable process")
    monkeypatch.setattr(activation.os, "kill", denied)
    assert activation._process_may_live(1234)
    assert activation._process_may_live(-1)
    assert activation._process_may_live("invalid")


@pytest.mark.parametrize("expected,observed,blocked", [
    ("linux:100", "linux:200", False), ("linux:100", "linux:100", True),
    (None, "linux:200", True), ("linux:100", None, True),
    ("pid:100", "linux:200", True), ("linux:100", "psutil:200.0", True),
])
def test_pid_reuse_requires_verified_birth_token_disagreement(monkeypatch, expected, observed, blocked):
    monkeypatch.setattr(activation, "process_start_token", lambda _: observed)
    assert activation._process_may_live(os.getpid(), expected) == blocked


def test_reused_foreign_uid_pid_requires_birth_time_proof(monkeypatch):
    def denied(*_):
        raise PermissionError("foreign process")
    monkeypatch.setattr(activation.os, "kill", denied)
    monkeypatch.setattr(activation, "process_start_token", lambda _: "linux:200")
    assert not activation._process_may_live(1234, "linux:100")
    assert activation._process_may_live(1234, "linux:200")


def test_completed_lease_of_reused_pid_does_not_block_restart(activation_env, monkeypatch):
    root, path, _ = activation_env
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE fleet_worker_leases ADD COLUMN owner_token TEXT")
        db.execute("INSERT INTO fleet_worker_leases VALUES ('old','completed',?,'linux:100')", (os.getpid(),))
    monkeypatch.setattr(activation, "process_start_token", lambda _: "linux:200")
    assert activation.restart_parked_fleet(root, root / "receipt.json", path)["passed"]


def test_failed_restart_releases_dispatch_lock_without_changing_work(activation_env, monkeypatch):
    root, path, _ = activation_env
    original = activation.subprocess.run
    def fail(command, **kwargs):
        if "restart" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(command, **kwargs)
    monkeypatch.setattr(activation.subprocess, "run", fail)
    result = activation.restart_parked_fleet(root, root / "receipt.json", path)
    assert not result["passed"]
    assert "did not confirm" in result["reasons"][0]
    with sqlite3.connect(path, timeout=0) as db:
        db.execute("BEGIN IMMEDIATE")
        assert db.execute("SELECT state FROM fleet_runs").fetchone()[0] == "waiting_for_input"
