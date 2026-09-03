"""Durable executor-level work units for Serena Fleet.

The phase legs remain provider turns. This module makes the work assigned to
those turns durable and executable: every unit has persisted phase state,
dependencies, ownership, runnable selection, and failure propagation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict, deque
from typing import Any

from fleet.contracts import completion_unit_ids

UNIT_TERMINAL_STATES = frozenset(
    {"completed", "failed", "blocked_dependency_failed", "cancelled"}
)
UNIT_FAILURE_STATES = frozenset({"failed", "blocked_dependency_failed", "cancelled"})
UNIT_ACTIVE_STATES = frozenset({"queued", "waiting_for_dependencies", "running"})


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS fleet_work_units (
            run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
            unit_id TEXT NOT NULL,
            owner_worker_key TEXT NOT NULL,
            reviewer_worker_keys_json TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            state TEXT NOT NULL,
            current_phase_index INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            PRIMARY KEY(run_id, unit_id)
        );
        CREATE INDEX IF NOT EXISTS fleet_work_units_state_idx
            ON fleet_work_units(run_id, state, current_phase_index);

        CREATE TABLE IF NOT EXISTS fleet_work_unit_dependencies (
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            dependency_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(run_id, unit_id, dependency_id),
            FOREIGN KEY(run_id, unit_id)
                REFERENCES fleet_work_units(run_id, unit_id) ON DELETE CASCADE,
            FOREIGN KEY(run_id, dependency_id)
                REFERENCES fleet_work_units(run_id, unit_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS fleet_work_unit_dependencies_reverse_idx
            ON fleet_work_unit_dependencies(run_id, dependency_id, unit_id);

        CREATE TABLE IF NOT EXISTS fleet_work_unit_phases (
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            phase_index INTEGER NOT NULL,
            phase TEXT NOT NULL,
            leg_id TEXT NOT NULL REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            attempt_id TEXT,
            blocked_by_json TEXT NOT NULL DEFAULT '[]',
            failure_reason TEXT,
            started_at REAL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(run_id, unit_id, phase_index),
            FOREIGN KEY(run_id, unit_id)
                REFERENCES fleet_work_units(run_id, unit_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS fleet_work_unit_phases_runnable_idx
            ON fleet_work_unit_phases(run_id, phase_index, state, leg_id);
        """
    )


def materialize_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    policy: dict[str, Any],
    initial_state: str,
    replace: bool = False,
    now: float | None = None,
) -> int:
    """Persist a policy's work units and per-phase executor records."""

    ensure_schema(connection)
    units = policy.get("work_units") if isinstance(policy, dict) else None
    phases = policy.get("phases") if isinstance(policy, dict) else None
    if not isinstance(units, list) or not units or not isinstance(phases, list):
        return 0
    created_at = float(time.time() if now is None else now)
    if replace:
        connection.execute("DELETE FROM fleet_work_units WHERE run_id = ?", (run_id,))
    existing = connection.execute(
        "SELECT COUNT(*) AS count FROM fleet_work_units WHERE run_id = ?", (run_id,)
    ).fetchone()
    if int(existing["count"] if existing is not None else 0):
        return 0

    top_state = "planned" if initial_state == "planned" else "queued"
    phase_zero_state = top_state
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "").strip()
        owner = str(unit.get("owner_worker_key") or "").strip()
        if not unit_id or not owner:
            continue
        reviewers = [
            str(item).strip()
            for item in unit.get("reviewer_worker_keys") or []
            if str(item).strip()
        ]
        connection.execute(
            """
            INSERT INTO fleet_work_units(
                run_id, unit_id, owner_worker_key, reviewer_worker_keys_json,
                contract_json, state, current_phase_index, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                run_id,
                unit_id,
                owner,
                _json(reviewers),
                _json(unit),
                top_state,
                created_at,
                created_at,
            ),
        )

    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "").strip()
        for dependency in unit.get("dependency_ids") or []:
            dependency_id = str(dependency).strip()
            if unit_id and dependency_id:
                connection.execute(
                    """
                    INSERT INTO fleet_work_unit_dependencies(
                        run_id, unit_id, dependency_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, unit_id, dependency_id, created_at),
                )

    inserted = 0
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_index = int(phase.get("index") or 0)
        phase_name = str(phase.get("name") or f"phase-{phase_index}")
        workers = phase.get("workers") or []
        legs = connection.execute(
            "SELECT leg_id, ordinal FROM fleet_legs WHERE run_id = ? AND phase_index = ?",
            (run_id, phase_index),
        ).fetchall()
        leg_by_ordinal = {int(row["ordinal"]): str(row["leg_id"]) for row in legs}
        for ordinal, worker in enumerate(workers):
            if not isinstance(worker, dict) or ordinal not in leg_by_ordinal:
                continue
            # Review is rotated: the reviewer leg advances the target unit,
            # not the reviewer's own implementation unit. This makes Review
            # wait for the code it actually inspects and makes Fix wait for
            # the peer review that can raise findings against it.
            for raw_id in completion_unit_ids(worker, phase_name):
                unit_id = str(raw_id).strip()
                if not unit_id:
                    continue
                state = phase_zero_state if phase_index == 0 else "pending"
                connection.execute(
                    """
                    INSERT INTO fleet_work_unit_phases(
                        run_id, unit_id, phase_index, phase, leg_id, state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        unit_id,
                        phase_index,
                        phase_name,
                        leg_by_ordinal[ordinal],
                        state,
                        created_at,
                    ),
                )
                inserted += 1
    return inserted


def repair_review_phase_ownership(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    policy: dict[str, Any],
    now: float | None = None,
) -> list[str]:
    """Remap legacy Review rows onto the units they actually review.

    Early executor snapshots persisted every phase against the worker's owned
    assignment. Review is rotated, so that let a reviewer run before the peer
    Code output existed and then advanced the wrong unit into Fix. New runs are
    materialized correctly by :func:`materialize_run`; this repairs an older
    terminal run immediately before an exact retry.

    The swap is refused while any Review leg is running. A retry already
    refuses live worker processes, so callers can treat a non-empty result as a
    complete atomic remap.
    """

    phases = policy.get("phases") if isinstance(policy, dict) else None
    if not isinstance(phases, list):
        return []
    review = next(
        (
            item
            for item in phases
            if isinstance(item, dict) and str(item.get("name") or "") == "verify"
        ),
        None,
    )
    if review is None:
        return []
    phase_index = int(review.get("index") or 0)
    workers = review.get("workers")
    if not isinstance(workers, list) or not workers:
        return []

    legs = connection.execute(
        "SELECT leg_id, ordinal, state FROM fleet_legs "
        "WHERE run_id = ? AND phase_index = ? ORDER BY ordinal",
        (run_id, phase_index),
    ).fetchall()
    leg_by_ordinal = {int(row["ordinal"]): str(row["leg_id"]) for row in legs}
    if any(str(row["state"] or "") == "running" for row in legs):
        return []

    expected: list[tuple[str, str]] = []
    for ordinal, worker in enumerate(workers):
        if not isinstance(worker, dict) or ordinal not in leg_by_ordinal:
            continue
        for raw_id in completion_unit_ids(worker, "verify"):
            unit_id = str(raw_id).strip()
            if unit_id:
                expected.append((unit_id, leg_by_ordinal[ordinal]))
    if not expected:
        return []
    expected_units = [unit_id for unit_id, _leg_id in expected]
    if len(expected_units) != len(set(expected_units)):
        raise ValueError("Review policy maps more than one reviewer onto the same work unit")

    rows = connection.execute(
        "SELECT * FROM fleet_work_unit_phases "
        "WHERE run_id = ? AND phase_index = ? ORDER BY unit_id",
        (run_id, phase_index),
    ).fetchall()
    actual = {(str(row["unit_id"]), str(row["leg_id"])) for row in rows}
    if actual == set(expected):
        return []
    if any(str(row["state"] or "") == "running" for row in rows):
        return []

    known_units = {
        str(row["unit_id"])
        for row in connection.execute(
            "SELECT unit_id FROM fleet_work_units WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    if set(expected_units) - known_units:
        raise ValueError("Review policy references a work unit that was not materialized")
    templates: dict[str, sqlite3.Row] = {}
    for row in rows:
        templates.setdefault(str(row["leg_id"]), row)
    missing = {leg_id for _unit_id, leg_id in expected} - set(templates)
    if missing:
        raise ValueError("Legacy Review phase is missing persisted worker rows")

    at = float(time.time() if now is None else now)
    connection.execute(
        "DELETE FROM fleet_work_unit_phases WHERE run_id = ? AND phase_index = ?",
        (run_id, phase_index),
    )
    for unit_id, leg_id in expected:
        row = templates[leg_id]
        connection.execute(
            """
            INSERT INTO fleet_work_unit_phases(
                run_id, unit_id, phase_index, phase, leg_id, state, attempt_id,
                blocked_by_json, failure_reason, started_at, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                unit_id,
                phase_index,
                "verify",
                leg_id,
                str(row["state"]),
                row["attempt_id"],
                str(row["blocked_by_json"] or "[]"),
                row["failure_reason"],
                row["started_at"],
                row["completed_at"],
                at,
            ),
        )
    return sorted({leg_id for _unit_id, leg_id in expected})


def backfill_runs(connection: sqlite3.Connection) -> int:
    """Materialize executor records for pre-migration runs with work-unit policy."""

    ensure_schema(connection)
    rows = connection.execute(
        """
        SELECT r.run_id, r.policy_json, r.state, r.dry_run
        FROM fleet_runs r
        WHERE NOT EXISTS (
            SELECT 1 FROM fleet_work_units u WHERE u.run_id = r.run_id
        )
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        try:
            policy = json.loads(str(row["policy_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        initial = "planned" if bool(row["dry_run"]) else "queued"
        inserted += materialize_run(
            connection,
            run_id=str(row["run_id"]),
            policy=policy,
            initial_state=initial,
        )
        _reconcile_from_legs(connection, str(row["run_id"]), str(row["state"]))
    return inserted


def prepare_phase(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    phase_index: int,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Persist dependency blocks and return the exact runnable leg ids."""

    records = connection.execute(
        """
        SELECT p.*, l.state AS leg_state
        FROM fleet_work_unit_phases p
        JOIN fleet_legs l ON l.leg_id = p.leg_id
        WHERE p.run_id = ? AND p.phase_index = ?
        ORDER BY p.unit_id
        """,
        (run_id, int(phase_index)),
    ).fetchall()
    if not records:
        return None
    at = float(time.time() if now is None else now)
    by_leg: dict[str, list[sqlite3.Row]] = defaultdict(list)
    blocked_units: dict[str, list[str]] = {}
    waiting_units: dict[str, list[str]] = {}
    predecessor_blocked_units: set[str] = set()
    predecessor_waiting_units: set[str] = set()
    ready_units: set[str] = set()
    for record in records:
        unit_id = str(record["unit_id"])
        leg_id = str(record["leg_id"])
        by_leg[leg_id].append(record)
        predecessor = connection.execute(
            """
            SELECT phase, state
            FROM fleet_work_unit_phases
            WHERE run_id = ? AND unit_id = ? AND phase_index < ?
            ORDER BY phase_index DESC
            LIMIT 1
            """,
            (run_id, unit_id, int(phase_index)),
        ).fetchone()
        if predecessor is not None and str(predecessor["state"]) != "completed":
            marker = f"{unit_id}:{str(predecessor['phase'])}"
            if str(predecessor["state"]) in UNIT_FAILURE_STATES:
                blocked_units[unit_id] = [marker]
                predecessor_blocked_units.add(unit_id)
            else:
                waiting_units[unit_id] = [marker]
                predecessor_waiting_units.add(unit_id)
            continue
        dependencies = connection.execute(
            """
            SELECT d.dependency_id, p.state
            FROM fleet_work_unit_dependencies d
            LEFT JOIN fleet_work_unit_phases p
              ON p.run_id = d.run_id AND p.unit_id = d.dependency_id
             AND p.phase_index = ?
            WHERE d.run_id = ? AND d.unit_id = ?
            ORDER BY d.dependency_id
            """,
            (int(phase_index), run_id, unit_id),
        ).fetchall()
        failures = [
            str(row["dependency_id"])
            for row in dependencies
            if str(row["state"] or "") in UNIT_FAILURE_STATES
        ]
        incomplete = [
            str(row["dependency_id"])
            for row in dependencies
            if str(row["state"] or "") != "completed"
        ]
        if failures:
            blocked_units[unit_id] = sorted(set(failures))
        elif incomplete:
            waiting_units[unit_id] = sorted(set(incomplete))
        else:
            ready_units.add(unit_id)

    runnable: list[str] = []
    for leg_id, leg_records in by_leg.items():
        unit_ids = [str(record["unit_id"]) for record in leg_records]
        hard_blocked = [unit_id for unit_id in unit_ids if unit_id in blocked_units]
        waiting = [unit_id for unit_id in unit_ids if unit_id in waiting_units]
        stored_leg_state = str(leg_records[0]["leg_state"] or "")
        # A terminal leg is the durable source of truth for the phase it ran.
        # A stale dependency reconciliation used to be able to overwrite the
        # work-unit projection back to waiting after finish_attempt() had
        # completed the leg. The scheduler would then see no runnable leg (the
        # leg was already completed) and eventually fail the whole run with
        # "phase did not complete". Heal that projection before considering
        # dependency state, and never regress completed work.
        if stored_leg_state == "completed":
            attempt = connection.execute(
                "SELECT attempt_id, error FROM fleet_attempts "
                "WHERE leg_id = ? ORDER BY attempt_number DESC LIMIT 1",
                (leg_id,),
            ).fetchone()
            mark_leg_finished(
                connection,
                leg_id=leg_id,
                attempt_id=str(attempt["attempt_id"] if attempt is not None else ""),
                state="completed",
                error=str(attempt["error"] or "") if attempt is not None else None,
                now=at,
            )
            continue
        dependency_block_only = all(
            str(record["state"])
            in {"blocked_dependency_failed", "waiting_for_dependencies"}
            and record["attempt_id"] is None
            for record in leg_records
        )
        if hard_blocked:
            failed_dependencies = sorted(
                {
                    dependency
                    for blocked_unit_id in hard_blocked
                    for dependency in blocked_units[blocked_unit_id]
                }
            )
            blocked_scope = ", ".join(hard_blocked)
            for unit_id in unit_ids:
                dependencies = blocked_units.get(unit_id, failed_dependencies)
                reason = "dependency failed: " + ", ".join(dependencies)
                if unit_id not in hard_blocked:
                    reason = f"co-owned executor blocked by {blocked_scope}: " + reason
                _set_phase_state(
                    connection,
                    run_id,
                    unit_id,
                    phase_index,
                    "blocked_dependency_failed",
                    at,
                    blocked_by=dependencies,
                    failure_reason=reason,
                )
                if unit_id not in predecessor_blocked_units:
                    connection.execute(
                        """
                        UPDATE fleet_work_units
                        SET state = 'blocked_dependency_failed', failure_reason = ?, updated_at = ?
                        WHERE run_id = ? AND unit_id = ?
                        """,
                        (reason, at, run_id, unit_id),
                    )
                _propagate_failure(connection, run_id, unit_id, phase_index, at)
            if stored_leg_state not in {"completed", "running", "cancelled"}:
                connection.execute(
                    "UPDATE fleet_legs SET state = 'failed', updated_at = ? WHERE leg_id = ?",
                    (at, leg_id),
                )
            continue
        if waiting:
            blocked_by = sorted(
                {dependency for unit_id in waiting for dependency in waiting_units[unit_id]}
            )
            for unit_id in unit_ids:
                _set_phase_state(
                    connection,
                    run_id,
                    unit_id,
                    phase_index,
                    "waiting_for_dependencies",
                    at,
                    blocked_by=blocked_by,
                )
                if unit_id not in predecessor_waiting_units:
                    connection.execute(
                        """
                        UPDATE fleet_work_units
                        SET state = 'waiting_for_dependencies', failure_reason = NULL, updated_at = ?
                        WHERE run_id = ? AND unit_id = ?
                        """,
                        (at, run_id, unit_id),
                    )
            if stored_leg_state not in {"completed", "running", "cancelled"} and (
                stored_leg_state != "failed" or dependency_block_only
            ):
                connection.execute(
                    """
                    UPDATE fleet_legs SET state = 'waiting_for_dependencies', updated_at = ?
                    WHERE leg_id = ?
                    """,
                    (at, leg_id),
                )
            continue

        if stored_leg_state in {"queued", "waiting_for_dependencies"} or (
            stored_leg_state == "failed" and dependency_block_only
        ):
            for unit_id in unit_ids:
                _set_phase_state(
                    connection,
                    run_id,
                    unit_id,
                    phase_index,
                    "queued",
                    at,
                    blocked_by=[],
                    failure_reason=None,
                )
                connection.execute(
                    """
                    UPDATE fleet_work_units
                    SET state = 'queued', current_phase_index = ?, failure_reason = NULL,
                        completed_at = NULL, updated_at = ?
                    WHERE run_id = ? AND unit_id = ?
                    """,
                    (int(phase_index), at, run_id, unit_id),
                )
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                (at, leg_id),
            )
            runnable.append(leg_id)

    return {
        "phase_index": int(phase_index),
        "runnable_leg_ids": runnable,
        "waiting_unit_ids": sorted(waiting_units),
        "blocked_unit_ids": sorted(blocked_units),
    }


def mark_leg_started(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    attempt_id: str,
    now: float | None = None,
) -> None:
    at = float(time.time() if now is None else now)
    rows = connection.execute(
        "SELECT run_id, unit_id, phase_index FROM fleet_work_unit_phases WHERE leg_id = ?",
        (leg_id,),
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            UPDATE fleet_work_unit_phases
            SET state = 'running', attempt_id = ?, blocked_by_json = '[]',
                failure_reason = NULL, started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE run_id = ? AND unit_id = ? AND phase_index = ?
            """,
            (
                attempt_id,
                at,
                at,
                str(row["run_id"]),
                str(row["unit_id"]),
                int(row["phase_index"]),
            ),
        )
        connection.execute(
            """
            UPDATE fleet_work_units SET state = 'running', current_phase_index = ?,
                failure_reason = NULL, updated_at = ?
            WHERE run_id = ? AND unit_id = ?
            """,
            (
                int(row["phase_index"]),
                at,
                str(row["run_id"]),
                str(row["unit_id"]),
            ),
        )


def mark_leg_finished(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    attempt_id: str,
    state: str,
    error: str | None,
    now: float | None = None,
) -> None:
    at = float(time.time() if now is None else now)
    rows = connection.execute(
        """
        SELECT run_id, unit_id, phase_index
        FROM fleet_work_unit_phases WHERE leg_id = ?
        """,
        (leg_id,),
    ).fetchall()
    if not rows:
        return
    phase_state = "completed" if state == "completed" else state
    clean_error = str(error or "").strip()[:4_000] or None
    for row in rows:
        run_id = str(row["run_id"])
        unit_id = str(row["unit_id"])
        phase_index = int(row["phase_index"])
        connection.execute(
            """
            UPDATE fleet_work_unit_phases
            SET state = ?, attempt_id = ?, blocked_by_json = '[]',
                failure_reason = ?, completed_at = ?, updated_at = ?
            WHERE run_id = ? AND unit_id = ? AND phase_index = ?
            """,
            (
                phase_state,
                attempt_id,
                clean_error,
                at,
                at,
                run_id,
                unit_id,
                phase_index,
            ),
        )
        if phase_state == "completed":
            later = connection.execute(
                """
                SELECT MIN(phase_index) AS phase_index
                FROM fleet_work_unit_phases
                WHERE run_id = ? AND unit_id = ? AND phase_index > ?
                """,
                (run_id, unit_id, phase_index),
            ).fetchone()
            next_index = later["phase_index"] if later is not None else None
            if next_index is None:
                connection.execute(
                    """
                    UPDATE fleet_work_units SET state = 'completed', failure_reason = NULL,
                        completed_at = ?, updated_at = ?
                    WHERE run_id = ? AND unit_id = ?
                    """,
                    (at, at, run_id, unit_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE fleet_work_units SET state = 'queued', current_phase_index = ?,
                        failure_reason = NULL, updated_at = ?
                    WHERE run_id = ? AND unit_id = ?
                    """,
                    (int(next_index), at, run_id, unit_id),
                )
                connection.execute(
                    """
                    UPDATE fleet_work_unit_phases SET state = 'queued', updated_at = ?
                    WHERE run_id = ? AND unit_id = ? AND phase_index = ? AND state = 'pending'
                    """,
                    (at, run_id, unit_id, int(next_index)),
                )
        else:
            connection.execute(
                """
                UPDATE fleet_work_units SET state = ?, current_phase_index = ?,
                    failure_reason = ?, updated_at = ?
                WHERE run_id = ? AND unit_id = ?
                """,
                (phase_state, phase_index, clean_error, at, run_id, unit_id),
            )
            if phase_state in UNIT_FAILURE_STATES:
                _propagate_failure(connection, run_id, unit_id, phase_index, at)


def cancel_unfinished(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    reason: str,
    now: float | None = None,
) -> None:
    at = float(time.time() if now is None else now)
    connection.execute(
        """
        UPDATE fleet_work_unit_phases
        SET state = 'cancelled', failure_reason = ?, completed_at = ?, updated_at = ?
        WHERE run_id = ? AND state != 'completed'
        """,
        (reason[:4_000], at, at, run_id),
    )
    connection.execute(
        """
        UPDATE fleet_work_units
        SET state = 'cancelled', failure_reason = ?, updated_at = ?
        WHERE run_id = ? AND state != 'completed'
        """,
        (reason[:4_000], at, run_id),
    )


def reset_leg_for_retry(
    connection: sqlite3.Connection,
    *,
    leg_id: str,
    now: float | None = None,
    include_completed: bool = False,
    reset_completed_descendants: bool = False,
) -> int:
    """Reset one unit chain without replaying later phases already completed."""

    at = float(time.time() if now is None else now)
    rows = connection.execute(
        """
        SELECT run_id, unit_id, phase_index
        FROM fleet_work_unit_phases
        WHERE leg_id = ?
        ORDER BY unit_id
        """,
        (leg_id,),
    ).fetchall()
    reset = 0
    for row in rows:
        run_id = str(row["run_id"])
        unit_id = str(row["unit_id"])
        start_index = int(row["phase_index"])
        phases = connection.execute(
            """
            SELECT phase_index, leg_id, state
            FROM fleet_work_unit_phases
            WHERE run_id = ? AND unit_id = ? AND phase_index >= ?
            ORDER BY phase_index
            """,
            (run_id, unit_id, start_index),
        ).fetchall()
        for phase in phases:
            index = int(phase["phase_index"])
            was_completed = str(phase["state"] or "") == "completed"
            reset_completed = (
                include_completed and index == start_index
            ) or (
                reset_completed_descendants and index > start_index
            )
            if was_completed and not reset_completed:
                continue
            connection.execute(
                """
                UPDATE fleet_work_unit_phases
                SET state = ?, attempt_id = NULL, blocked_by_json = '[]',
                    failure_reason = NULL, started_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE run_id = ? AND unit_id = ? AND phase_index = ?
                """,
                (
                    "queued" if index == start_index else "pending",
                    at,
                    run_id,
                    unit_id,
                    index,
                ),
            )
            connection.execute(
                """
                UPDATE fleet_legs SET state = ?, updated_at = ?
                WHERE leg_id = ? AND (state != 'completed' OR ?)
                """,
                (
                    "queued" if index == start_index else "waiting_for_dependencies",
                    at,
                    str(phase["leg_id"]),
                    int(bool(reset_completed)),
                ),
            )
        connection.execute(
            """
            UPDATE fleet_work_units
            SET state = 'queued', current_phase_index = ?, failure_reason = NULL,
                completed_at = NULL, updated_at = ?
            WHERE run_id = ? AND unit_id = ?
            """,
            (start_index, at, run_id, unit_id),
        )
        reset += 1
    return reset


def project_run(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Return the persisted executor graph for status, CLI, MCP, and UI."""

    unit_rows = connection.execute(
        "SELECT * FROM fleet_work_units WHERE run_id = ? ORDER BY unit_id", (run_id,)
    ).fetchall()
    if not unit_rows:
        return []
    dependency_rows = connection.execute(
        """
        SELECT unit_id, dependency_id FROM fleet_work_unit_dependencies
        WHERE run_id = ? ORDER BY unit_id, dependency_id
        """,
        (run_id,),
    ).fetchall()
    dependencies: dict[str, list[str]] = defaultdict(list)
    for row in dependency_rows:
        dependencies[str(row["unit_id"])].append(str(row["dependency_id"]))
    evidence_rows = connection.execute(
        """
        SELECT attempt_id, payload_json FROM fleet_events
        WHERE run_id = ? AND type IN (
            'leg.completion_evidence_accepted',
            'leg.completion_evidence_stopped'
        )
          AND attempt_id IS NOT NULL
        ORDER BY event_seq
        """,
        (run_id,),
    ).fetchall()
    evidence_by_attempt = {
        str(row["attempt_id"]): _decoded_dict(row["payload_json"])
        for row in evidence_rows
        if str(row["attempt_id"] or "")
    }
    phase_rows = connection.execute(
        """
        SELECT p.*, l.ordinal, l.runtime, l.state AS leg_state,
               a.session_id, a.output_text
        FROM fleet_work_unit_phases p
        JOIN fleet_legs l ON l.leg_id = p.leg_id
        LEFT JOIN fleet_attempts a ON a.attempt_id = p.attempt_id
        WHERE p.run_id = ?
        ORDER BY p.unit_id, p.phase_index
        """,
        (run_id,),
    ).fetchall()
    phases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in phase_rows:
        blocked = _decoded_list(row["blocked_by_json"])
        attempt_id = str(row["attempt_id"] or "") or None
        phases[str(row["unit_id"])].append(
            {
                "phase_index": int(row["phase_index"]),
                "phase": str(row["phase"]),
                "state": str(row["state"]),
                "leg_id": str(row["leg_id"]),
                "attempt_id": attempt_id,
                "provider": str(row["runtime"]),
                "blocked_by": blocked,
                "failure_reason": row["failure_reason"],
                "started_at": float(row["started_at"]) if row["started_at"] else None,
                "completed_at": (
                    float(row["completed_at"]) if row["completed_at"] else None
                ),
                "evidence_preview": str(row["output_text"] or "")[:500] or None,
                "completion_evidence": evidence_by_attempt.get(attempt_id or ""),
                "session_id": row["session_id"],
            }
        )

    result: list[dict[str, Any]] = []
    for row in unit_rows:
        unit_id = str(row["unit_id"])
        try:
            contract = json.loads(str(row["contract_json"] or "{}"))
        except json.JSONDecodeError:
            contract = {"id": unit_id}
        history = phases.get(unit_id, [])
        completed = sum(item["state"] == "completed" for item in history)
        current_index = int(row["current_phase_index"] or 0)
        current = next(
            (item for item in history if item["phase_index"] == current_index),
            history[-1] if history else None,
        )
        view = dict(contract)
        view.update(
            {
                "id": unit_id,
                "owner_worker_key": str(row["owner_worker_key"]),
                "reviewer_worker_keys": _decoded_list(row["reviewer_worker_keys_json"]),
                "dependency_ids": dependencies.get(unit_id, []),
                "state": str(row["state"]),
                "current_phase_index": current_index,
                "current_phase": current.get("phase") if current else None,
                "runnable": bool(current and current["state"] == "queued"),
                "blocked_dependency_ids": current.get("blocked_by", []) if current else [],
                "failure_reason": row["failure_reason"],
                "progress": {"completed": completed, "total": len(history)},
                "phase_executions": history,
                "evidence_receipts": [
                    {
                        "phase": item["phase"],
                        "attempt_id": item["attempt_id"],
                        "session_id": item["session_id"],
                        "summary": item["evidence_preview"],
                        "accepted": (
                            bool(item["completion_evidence"].get("accepted"))
                            if item["completion_evidence"]
                            else None
                        ),
                        "reason": str(
                            (item["completion_evidence"] or {}).get("reason") or ""
                        ),
                        "failures": list(
                            (item["completion_evidence"] or {}).get("failures") or []
                        ),
                        "units": list(
                            (item["completion_evidence"] or {}).get("units") or []
                        ),
                    }
                    for item in history
                    if item["state"] == "completed"
                    and (item["completion_evidence"] or item["evidence_preview"])
                ][-4:],
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "completed_at": (
                    float(row["completed_at"]) if row["completed_at"] else None
                ),
            }
        )
        result.append(view)
    return result


def _propagate_failure(
    connection: sqlite3.Connection,
    run_id: str,
    failed_unit_id: str,
    phase_index: int,
    now: float,
) -> None:
    queue: deque[str] = deque([failed_unit_id])
    seen = {failed_unit_id}
    while queue:
        dependency = queue.popleft()
        rows = connection.execute(
            """
            SELECT unit_id FROM fleet_work_unit_dependencies
            WHERE run_id = ? AND dependency_id = ? ORDER BY unit_id
            """,
            (run_id, dependency),
        ).fetchall()
        for row in rows:
            unit_id = str(row["unit_id"])
            if unit_id in seen:
                continue
            seen.add(unit_id)
            queue.append(unit_id)
            phase = connection.execute(
                """
                SELECT leg_id, state FROM fleet_work_unit_phases
                WHERE run_id = ? AND unit_id = ? AND phase_index = ?
                """,
                (run_id, unit_id, int(phase_index)),
            ).fetchone()
            if phase is None or str(phase["state"]) == "completed":
                continue
            reason = f"dependency failed: {dependency}"
            _set_phase_state(
                connection,
                run_id,
                unit_id,
                phase_index,
                "blocked_dependency_failed",
                now,
                blocked_by=[dependency],
                failure_reason=reason,
            )
            connection.execute(
                """
                UPDATE fleet_work_units
                SET state = 'blocked_dependency_failed', current_phase_index = ?,
                    failure_reason = ?, updated_at = ?
                WHERE run_id = ? AND unit_id = ?
                """,
                (int(phase_index), reason, now, run_id, unit_id),
            )
            connection.execute(
                """
                UPDATE fleet_legs SET state = 'failed', updated_at = ?
                WHERE leg_id = ? AND state NOT IN ('completed', 'running', 'cancelled')
                """,
                (now, str(phase["leg_id"])),
            )


def _reconcile_from_legs(
    connection: sqlite3.Connection,
    run_id: str,
    run_state: str,
) -> None:
    rows = connection.execute(
        """
        SELECT p.leg_id, p.attempt_id, l.state, l.current_attempt
        FROM fleet_work_unit_phases p
        JOIN fleet_legs l ON l.leg_id = p.leg_id
        WHERE p.run_id = ?
        GROUP BY p.leg_id, p.attempt_id, l.state, l.current_attempt
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        state = str(row["state"])
        if state not in UNIT_TERMINAL_STATES:
            continue
        attempt = connection.execute(
            """
            SELECT attempt_id, error FROM fleet_attempts
            WHERE leg_id = ? ORDER BY attempt_number DESC LIMIT 1
            """,
            (str(row["leg_id"]),),
        ).fetchone()
        mark_leg_finished(
            connection,
            leg_id=str(row["leg_id"]),
            attempt_id=str(attempt["attempt_id"] if attempt is not None else ""),
            state=state,
            error=str(attempt["error"] or "") if attempt is not None else None,
        )
    if run_state == "cancelled":
        cancel_unfinished(connection, run_id=run_id, reason="cancelled by user")


def _set_phase_state(
    connection: sqlite3.Connection,
    run_id: str,
    unit_id: str,
    phase_index: int,
    state: str,
    now: float,
    *,
    blocked_by: list[str] | None = None,
    failure_reason: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE fleet_work_unit_phases
        SET state = ?, blocked_by_json = ?, failure_reason = ?, updated_at = ?
        WHERE run_id = ? AND unit_id = ? AND phase_index = ?
        """,
        (
            state,
            _json(blocked_by or []),
            failure_reason,
            now,
            run_id,
            unit_id,
            int(phase_index),
        ),
    )


def _decoded_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if str(item)]


def _decoded_dict(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
