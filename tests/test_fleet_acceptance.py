from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from core.fleet_acceptance import (
    activation_gate,
    main,
    run_acceptance,
    run_disposable_canary,
    source_fingerprint,
)


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "source"
    root.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=root, check=True)
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def _fleet_db(path: Path, *, state: str | None = None) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE fleet_runs(run_id TEXT, state TEXT, created_at REAL)"
        )
        if state:
            connection.execute(
                "INSERT INTO fleet_runs VALUES ('run-1', ?, ?)", (state, time.time())
            )


def test_disposable_canary_proves_every_dangerous_transition():
    checks = run_disposable_canary()

    assert {item["name"] for item in checks} == {
        "claims_and_conflict_refusal",
        "worktree_isolation_and_integration_gate",
        "explicit_rollback",
        "automatic_gate_rollback",
        "completion_rejection",
        "dag_retry_and_chat_continuity",
        "crash_recovery",
    }
    assert all(item["passed"] for item in checks)


def test_receipt_and_gate_are_bound_to_source_and_terminal_fleet_state(tmp_path):
    root = _git_repo(tmp_path)
    receipt_path = tmp_path / "acceptance.json"
    database = tmp_path / "fleet.sqlite3"
    _fleet_db(database)

    receipt = run_acceptance(
        root,
        receipt_path,
        test_command=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    gate = activation_gate(root, receipt_path, database)

    assert receipt["passed"] is True
    assert receipt["source_fingerprint"] == source_fingerprint(root)
    assert receipt["tests"]["exit_code"] == 0
    assert gate["passed"] is True
    assert gate["next_action"] == "restart Fleet, then Serena desktop"

    (root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = activation_gate(root, receipt_path, database)
    assert changed["passed"] is False
    assert "source changed after acceptance" in changed["reasons"]


def test_gate_refuses_active_runs_without_changing_the_database(tmp_path):
    root = _git_repo(tmp_path)
    receipt_path = tmp_path / "acceptance.json"
    database = tmp_path / "fleet.sqlite3"
    _fleet_db(database, state="running")
    receipt = {
        "schema_version": 1,
        "kind": "serena.fleet.activation_receipt",
        "passed": True,
        "source_fingerprint": source_fingerprint(root),
        "completed_at": time.time(),
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = database.read_bytes()

    gate = activation_gate(root, receipt_path, database)

    assert gate["passed"] is False
    assert gate["active_runs"] == [{"run_id": "run-1", "state": "running"}]
    assert gate["next_action"] == "do not restart Serena"
    assert database.read_bytes() == before


def test_gate_fails_closed_for_missing_or_stale_proof(tmp_path):
    root = _git_repo(tmp_path)
    database = tmp_path / "fleet.sqlite3"
    _fleet_db(database)
    missing = activation_gate(root, tmp_path / "missing.json", database)
    assert missing["passed"] is False
    assert any("unreadable" in reason for reason in missing["reasons"])

    receipt = tmp_path / "stale.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "serena.fleet.activation_receipt",
                "passed": True,
                "source_fingerprint": source_fingerprint(root),
                "completed_at": time.time() - 10,
            }
        ),
        encoding="utf-8",
    )
    stale = activation_gate(root, receipt, database, max_age_seconds=1)
    assert stale["passed"] is False
    assert "acceptance receipt is stale" in stale["reasons"]


def test_gate_cli_is_json_and_uses_exit_status(tmp_path, capsys):
    root = _git_repo(tmp_path)
    database = tmp_path / "fleet.sqlite3"
    _fleet_db(database, state="queued")

    exit_code = main(
        [
            "gate",
            "--repo",
            str(root),
            "--receipt",
            str(tmp_path / "missing.json"),
            "--fleet-db",
            str(database),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["passed"] is False
    assert output["active_runs"][0]["state"] == "queued"
