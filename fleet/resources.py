"""Durable resource waits; readiness is observed, never inferred from elapsed time."""

from __future__ import annotations

import shutil
import os
import time
from pathlib import Path

from core.sqlite_connection import connect_database

from fleet.dag import reset_leg_for_retry as reset_work_unit_leg_for_retry


def is_disk_exhaustion(error: str) -> bool:
    text = error.lower()
    return any(marker in text for marker in (
        "[errno 28]", "enospc", "no space left on device", "database or disk is full",
    ))


def is_transient_transport_error(error: str) -> bool:
    """Narrow infrastructure classifier; never turn an authority stop into a retry."""
    text = error.lower()
    if any(marker in text for marker in (
        "permission", "unauthorized", "forbidden", "authentication", "api key", "login",
        "not logged in", "401", "403", "429", "too many requests",
        "quota", "rate limit", "rate_limit", "usage limit", "identity", "certificate",
        "work stopped", "completion evidence", "integration", "cancelled", "canceled",
    )):
        return False
    return any(marker in text for marker in (
        "connection reset by peer", "connection reset", "econnreset", "econnrefused",
        "connection refused", "temporary failure in name resolution", "eai_again",
        "stream disconnected before completion", "websocket connection closed",
        "server disconnected", "remote protocol error", "502 bad gateway",
        "503 service unavailable", "504 gateway timeout",
    ))


def _disk_path_readiness(path: Path, required_bytes: int) -> dict:
    """Read-only point-in-time checks, including unprivileged inode availability."""
    result = {"path": str(path), "ready": False}
    try:
        result["free_bytes"] = shutil.disk_usage(path).free
        if result["free_bytes"] < required_bytes:
            result["reason"] = "insufficient free bytes"
            return result
        statvfs = getattr(os, "statvfs", None)
        result["inode_accounting"] = False
        if statvfs is not None:
            stats = statvfs(path)
            if stats.f_flag & getattr(os, "ST_RDONLY", 0):
                result["reason"] = "filesystem is read-only"
                return result
            # Some filesystems use dynamic inode allocation and report no
            # fixed inode pool. Zero available is exhaustion only for a pool.
            if stats.f_files > 0:
                result["inode_accounting"] = True
                result["available_inodes"] = stats.f_favail
                if stats.f_favail <= 0:
                    result["reason"] = "no available inodes"
                    return result
        result.update(ready=True, reason="observed filesystem headroom")
    except OSError as error:
        result["reason"] = f"filesystem probe failed: {type(error).__name__}"
    return result


def _disk_probe_paths(store, row) -> list[Path]:
    """Include recorded integration/worker filesystems, without initializing stores."""
    paths = [Path(row["cwd"]), store.path.parent, store.path.resolve().parent]
    if row["access_mode"] == "write":
        from fleet.isolation import DEFAULT_WORKSPACE_ROOT

        path = Path(os.environ.get("SERENA_FLEET_WORKSPACE_ROOT", "").strip() or DEFAULT_WORKSPACE_ROOT).expanduser()
        while not path.exists() and path != path.parent:
            path = path.parent
        paths.append(path)
    if row["event_log_path"]:
        path = Path(row["event_log_path"]).parent
        while not path.exists() and path != path.parent:
            path = path.parent
        paths.append(path)
    with store._connect() as db:
        checkout = db.execute("SELECT path,state FROM fleet_run_checkouts WHERE run_id=?", (row["run_id"],)).fetchone()
    if checkout:
        path = Path(checkout["path"])
        if checkout["state"] != "ready":
            while not path.exists() and path != path.parent:
                path = path.parent
        paths.append(path)
    configured_isolation = os.environ.get("SERENA_FLEET_ISOLATION_DB_PATH", "").strip()
    isolation_db = Path(configured_isolation or store.path.with_name("fleet-isolation.sqlite3")).expanduser()
    if isolation_db.exists():
        paths.append(isolation_db.parent)
        run = store.get_run(row["run_id"])
        leg = next((leg for phase in (run or {}).get("phases", []) for leg in phase["legs"] if leg["leg_id"] == row["leg_id"]), None)
        if leg is None:
            raise ValueError("resource wait has no worker identity")
        with connect_database(isolation_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            # ENOSPC can interrupt initial schema creation. No registry yet is
            # not corruption: after headroom returns, normal startup creates it.
            has_registry = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_workspaces'").fetchone()
            workspace = db.execute("SELECT path FROM fleet_workspaces WHERE run_id=? AND worker_key=?", (row["run_id"], leg["worker_key"])).fetchone() if has_registry else None
        if workspace:
            paths.append(Path(workspace[0]))
    return list(dict.fromkeys(paths))


def resume_ready_resource_waits(store, *, now: float | None = None) -> list[str]:
    """Wake only the parked generation, preserving running owners and siblings."""
    now = time.time() if now is None else now
    resumed = []
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT w.*, r.cwd, l.access_mode, a.error AS failure_reason, a.event_log_path FROM fleet_resource_waits w "
            "JOIN fleet_runs r ON r.run_id = w.run_id "
            "JOIN fleet_legs l ON l.leg_id = w.leg_id "
            "JOIN fleet_attempts a ON a.attempt_id = w.attempt_id "
            "WHERE w.not_before <= ? AND r.cancel_requested = 0 "
            "AND r.state IN ('running', 'queued', 'waiting_for_resources', 'waiting_for_capacity')",
            (now,),
        ).fetchall()
    for row in rows:
        checks = []
        try:
            if row["resource"] == "disk":
                checks = [_disk_path_readiness(path, row["required_bytes"]) for path in _disk_probe_paths(store, row)]
                ready = bool(checks) and all(check["ready"] for check in checks)
            else:
                ready = row["resource"] in {"transport", "process"}
        except (OSError, sqlite3.Error, ValueError) as error:
            checks = [{"ready": False, "reason": f"resource path inspection failed: {type(error).__name__}"}]
            ready = False
        with store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT w.*, r.state AS run_state, r.cancel_requested, l.state AS leg_state "
                "FROM fleet_resource_waits w JOIN fleet_runs r ON r.run_id = w.run_id "
                "JOIN fleet_legs l ON l.leg_id = w.leg_id "
                "WHERE w.leg_id = ? AND w.attempt_id = ?",
                (row["leg_id"], row["attempt_id"]),
            ).fetchone()
            if (current is None or current["cancel_requested"]
                    or current["run_state"] not in {"running", "queued", "waiting_for_resources", "waiting_for_capacity"}
                    or current["leg_state"] != "waiting_for_resources"
                    or current["not_before"] > now):
                continue
            if not ready:
                detail = "; ".join(f"{check.get('path', 'workspace')}: {check['reason']}" for check in checks if not check["ready"])
                reason = str(row["failure_reason"] or row["reason"]).split("\nReadiness: ", 1)[0]
                if detail:
                    reason += "\nReadiness: " + detail[:1000]
                connection.execute(
                    "UPDATE fleet_resource_waits SET not_before = ?, reason = ? WHERE leg_id = ?",
                    (now + 30, reason, row["leg_id"]),
                )
                continue
            connection.execute("DELETE FROM fleet_resource_waits WHERE leg_id = ?", (row["leg_id"],))
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                (now, row["leg_id"]),
            )
            reset_work_unit_leg_for_retry(connection, leg_id=row["leg_id"], now=now)
            if current["run_state"] in {"waiting_for_resources", "waiting_for_capacity"}:
                connection.execute(
                    "UPDATE fleet_runs SET state = 'queued', error = NULL, updated_at = ? "
                    "WHERE run_id = ?", (now, row["run_id"]),
                )
            store._insert_event(
                connection, run_id=row["run_id"], leg_id=row["leg_id"],
                attempt_id=row["attempt_id"],
                event_type=f"leg.{row['resource']}_retry_started" if row["resource"] != "disk" else "leg.resource_resumed",
                payload={"resource": row["resource"], "required_bytes": row["required_bytes"], "checks": checks},
            )
            resumed.append(row["leg_id"])
    return resumed
