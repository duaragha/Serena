"""Durable resource waits; readiness is observed, never inferred from elapsed time."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

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


def resume_ready_resource_waits(store, *, now: float | None = None) -> list[str]:
    """Wake only the parked generation, preserving running owners and siblings."""
    now = time.time() if now is None else now
    resumed = []
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT w.*, r.cwd FROM fleet_resource_waits w "
            "JOIN fleet_runs r ON r.run_id = w.run_id "
            "WHERE w.not_before <= ? AND r.cancel_requested = 0 "
            "AND r.state IN ('running', 'queued', 'waiting_for_resources', 'waiting_for_capacity')",
            (now,),
        ).fetchall()
    for row in rows:
        try:
            ready = row["resource"] in {"transport", "process"} or (row["resource"] == "disk" and all(
                shutil.disk_usage(path).free >= row["required_bytes"]
                for path in (Path(row["cwd"]), store.path.parent)
            ))
        except OSError:
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
                connection.execute(
                    "UPDATE fleet_resource_waits SET not_before = ? WHERE leg_id = ?",
                    (now + 30, row["leg_id"]),
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
                payload={"resource": row["resource"], "required_bytes": row["required_bytes"]},
            )
            resumed.append(row["leg_id"])
    return resumed
