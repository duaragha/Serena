"""Restart only Fleet after source acceptance and a dispatch-locked quiescence check."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from fleet.acceptance import activation_gate
from core.work_jobs import process_start_token
from core.sqlite_connection import connect_database

SERVICE = "serena-fleet.service"
PARKED = {"waiting_for_input", "waiting_for_resources", "waiting_for_capacity"}


def _process_may_live(pid: object, token: object = None) -> bool:
    if pid is None:
        return False
    try:
        number = int(pid)
        if number <= 0:
            return True
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except (TypeError, ValueError):
        return True
    except OSError:
        # A reused PID may belong to another uid. A readable birth token can
        # still disprove ownership; otherwise permission denial stays blocked.
        pass
    # A retained completed receipt can reference a PID reused by an unrelated
    # process. Only verified birth-token disagreement proves it is not ours.
    expected = str(token or "")
    observed = process_start_token(number)
    pattern = r"(?:linux:[0-9]+|psutil:[0-9]+\.[0-9]+)"
    if (observed and re.fullmatch(pattern, expected) and re.fullmatch(pattern, observed)
            and expected.split(":", 1)[0] == observed.split(":", 1)[0]
            and expected != observed):
        return False
    return True


def quiescence_blockers(db: sqlite3.Connection) -> list[str]:
    """Caller holds BEGIN IMMEDIATE: claims and attempt/helper dispatch cannot race."""
    blockers = []
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("fleet_runs", "fleet_attempts"):
        if table not in tables:
            raise ValueError(f"missing mandatory Fleet table: {table}")
    def token_column(table: str, column: str) -> str:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        return column if column in columns else "NULL"

    owner_token = token_column("fleet_runs", "owner_token")
    for run_id, state, owner, token in db.execute(f"SELECT run_id,state,owner_pid,{owner_token} FROM fleet_runs"):
        if state not in PARKED | {"completed", "failed", "cancelled", "planned"}:
            blockers.append(f"run {run_id}: {state}")
        if _process_may_live(owner, token):
            blockers.append(f"run {run_id}: owner process may still be alive")
    for table, key, pid_column in (
        ("fleet_attempts", "attempt_id", "pid"),
        ("fleet_peer_help", "id", "pid"),
        ("fleet_lesson_reviews", "id", "pid"),
        ("fleet_worker_leases", "attempt_id", "owner_pid"),
    ):
        if table not in tables:
            continue
        token = token_column(table, "owner_token" if pid_column == "owner_pid" else "process_token")
        for row_id, state, pid, birth in db.execute(f"SELECT {key},state,{pid_column},{token} FROM {table}"):
            if state == "running" or _process_may_live(pid, birth):
                blockers.append(f"{table} {row_id}: live or unverified execution")
    return blockers


def restart_parked_fleet(repo_root: str | Path, receipt: str | Path, database: str | Path) -> dict:
    """No run mutations, cancellation, signal guessing, or chat-host restart.

    The SQLite writer lock spans validation and systemd restart. Existing workers
    cause refusal; new run claims and recovery dispatch wait until restart returns.
    Systemd Type=simple is required so startup need not acquire SQLite before the
    restart command returns and releases our lock.
    """
    root = Path(repo_root).resolve()
    path = Path(database).expanduser().resolve()
    result = {"passed": False, "restarted": False, "service": SERVICE, "reasons": []}
    try:
        # mode=rw refuses to create or migrate a missing production database.
        with connect_database(f"file:{path}?mode=rw", uri=True, timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            gate = activation_gate(root, receipt, path)
            reasons = [r for r in gate["reasons"] if r != "one or more Fleet runs are still active"]
            reasons.extend(quiescence_blockers(db))
            result["parked_runs"] = gate["active_runs"]
            if reasons:
                result["reasons"] = reasons
                return result
            shown = subprocess.run(
                ["systemctl", "--user", "show", SERVICE, "--property=WorkingDirectory,Type,ActiveState"],
                capture_output=True, text=True, check=True, timeout=10,
            )
            properties = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
            if (properties.get("WorkingDirectory") != str(root)
                    or properties.get("Type") != "simple"
                    or properties.get("ActiveState") != "active"):
                result["reasons"] = ["Fleet service identity, repository, type or active state is unverified"]
                return result
            subprocess.run(["systemctl", "--user", "restart", SERVICE],
                           capture_output=True, text=True, check=True, timeout=30)
            result.update(passed=True, restarted=True)
        return result
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        result["reasons"] = [f"activation refused or restart did not confirm: {error}"]
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--fleet-db", type=Path, required=True)
    args = parser.parse_args(argv)
    result = restart_parked_fleet(args.repo, args.receipt, args.fleet_db)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
