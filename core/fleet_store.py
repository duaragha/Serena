"""Durable SQLite state for Serena Fleet runs and worker attempts."""

from __future__ import annotations

import hmac
import json
import os
import signal
import sqlite3
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.fleet_context import redact_text, redact_value
from core.fleet_contracts import derive_work_unit_views
from core.fleet_dag import (
    backfill_runs as backfill_work_unit_runs,
)
from core.fleet_dag import (
    cancel_unfinished as cancel_unfinished_work_units,
)
from core.fleet_dag import (
    ensure_schema as ensure_work_unit_schema,
)
from core.fleet_dag import (
    mark_leg_finished as mark_work_unit_leg_finished,
)
from core.fleet_dag import (
    mark_leg_started as mark_work_unit_leg_started,
)
from core.fleet_dag import (
    materialize_run as materialize_work_unit_run,
)
from core.fleet_dag import (
    prepare_phase as prepare_work_unit_phase,
)
from core.fleet_dag import (
    project_run as project_work_unit_run,
)
from core.fleet_dag import (
    reset_leg_for_retry as reset_work_unit_leg_for_retry,
)
from core.work_jobs import process_start_token

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "fleet.sqlite3"
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled", "planned"})
WAITING_RUN_STATES = frozenset({"waiting_for_capacity"})
TERMINAL_ATTEMPT_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
MAX_OUTPUT_CHARS = 256_000
MAX_EVENT_CHARS = 64_000
MAX_PUBLIC_PREVIEW_CHARS = 4_000
MAX_STEERING_MESSAGE_CHARS = 8_000
MAX_STEERING_TOTAL_CHARS = 16_000
MAX_HANDOFF_CONTEXT_CHARS = 32_000


class FleetStore:
    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._initialize()

    def create_run(
        self,
        *,
        task: str,
        activity: str,
        cwd: str,
        origin_session_id: str | None,
        origin_agent: str | None,
        dry_run: bool,
        policy: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        run_id = str(uuid.uuid4())
        worker_group_id = _new_worker_group_id()
        safe_task, _task_redactions = redact_text(task)
        safe_policy, _policy_redactions = redact_value(policy)
        if not isinstance(safe_policy, dict):
            raise ValueError("Fleet policy must be an object")
        clean_key = str(idempotency_key or "").strip() or None
        initial_state = "planned" if dry_run else "queued"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if clean_key:
                existing = connection.execute(
                    "SELECT run_id FROM fleet_runs WHERE idempotency_key = ?",
                    (clean_key,),
                ).fetchone()
                if existing is not None:
                    return self._snapshot(connection, str(existing["run_id"]))
            connection.execute(
                """
                INSERT INTO fleet_runs(
                    run_id, idempotency_key, task, activity, cwd,
                    origin_session_id, origin_agent, worker_group_id, state, current_phase,
                    policy_json, dry_run, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?)
                """,
                (
                    run_id,
                    clean_key,
                    safe_task,
                    activity,
                    cwd,
                    origin_session_id,
                    origin_agent,
                    worker_group_id,
                    initial_state,
                    json.dumps(safe_policy, separators=(",", ":"), sort_keys=True),
                    int(dry_run),
                    now,
                    now,
                ),
            )
            leg_state = "planned" if dry_run else "queued"
            for phase in safe_policy["phases"]:
                for ordinal, worker in enumerate(phase["workers"]):
                    connection.execute(
                        """
                        INSERT INTO fleet_legs(
                            leg_id, run_id, phase_index, phase, execution,
                            ordinal, runtime, role, requested_model,
                            requested_effort, access_mode, state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            run_id,
                            int(phase["index"]),
                            str(phase["name"]),
                            str(phase["execution"]),
                            ordinal,
                            str(worker.get("provider") or worker.get("runtime")),
                            str(worker["role"]),
                            str(worker["model"]),
                            str(worker["effort"]),
                            str(worker["access_mode"]),
                            leg_state,
                            now,
                            now,
                        ),
                    )
            materialize_work_unit_run(
                connection,
                run_id=run_id,
                policy=safe_policy,
                initial_state=initial_state,
                now=now,
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.created",
                payload={"state": initial_state, "activity": activity, "dry_run": dry_run},
            )
            return self._snapshot(connection, run_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM fleet_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._snapshot(connection, run_id) if row is not None else None

    def get_result(self, run_id: str) -> dict[str, Any]:
        """Return the potentially large final result only on explicit request."""

        with self._connect() as connection:
            row = self._require_run(connection, run_id)
            return {
                "run_id": str(row["run_id"]),
                "state": str(row["state"]),
                "result_text": row["result_text"],
                "error": row["error"],
                "completed_at": (float(row["completed_at"]) if row["completed_at"] else None),
            }

    def upgrade_unstarted_session_mode(self, run_id: str) -> bool:
        """Adopt durable workers for a new run created by a stale MCP process.

        Existing or retried runs are frozen as materialized. This narrow
        upgrade applies only while queued and before the first attempt exists.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if str(run["state"]) != "queued":
                return False
            attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM fleet_attempts a "
                "JOIN fleet_legs l ON l.leg_id = a.leg_id WHERE l.run_id = ?",
                (run_id,),
            ).fetchone()
            if int(attempts["count"] or 0):
                return False
            try:
                policy = json.loads(str(run["policy_json"] or "{}"))
            except json.JSONDecodeError:
                return False
            if not isinstance(policy, dict) or "session_mode" in policy:
                return False
            policy["session_mode"] = "persistent_by_worker"
            connection.execute(
                "UPDATE fleet_runs SET policy_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(policy, separators=(",", ":"), sort_keys=True), time.time(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.session_mode_upgraded",
                payload={"session_mode": "persistent_by_worker"},
            )
            return True

    def refresh_unstarted_policy(self, run_id: str, policy: dict[str, Any]) -> bool:
        """Replace a stale host's plan before any worker has materialized.

        MCP hosts can outlive a Fleet deploy.  The resident supervisor is the
        final authority, so a queued run with zero attempts may be rebuilt from
        the current policy.  Once any provider session has started, the saved
        topology is immutable so retries and chat lineage remain truthful.
        """

        encoded = json.dumps(policy, separators=(",", ":"), sort_keys=True)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if str(run["state"]) != "queued" or bool(run["dry_run"]):
                return False
            attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM fleet_attempts a "
                "JOIN fleet_legs l ON l.leg_id = a.leg_id WHERE l.run_id = ?",
                (run_id,),
            ).fetchone()
            if int(attempts["count"] or 0):
                return False
            if hmac.compare_digest(str(run["policy_json"] or ""), encoded):
                return False

            connection.execute("DELETE FROM fleet_legs WHERE run_id = ?", (run_id,))
            for phase in policy["phases"]:
                for ordinal, worker in enumerate(phase["workers"]):
                    connection.execute(
                        """
                        INSERT INTO fleet_legs(
                            leg_id, run_id, phase_index, phase, execution,
                            ordinal, runtime, role, requested_model,
                            requested_effort, access_mode, state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            run_id,
                            int(phase["index"]),
                            str(phase["name"]),
                            str(phase["execution"]),
                            ordinal,
                            str(worker.get("provider") or worker.get("runtime")),
                            str(worker["role"]),
                            str(worker["model"]),
                            str(worker["effort"]),
                            str(worker["access_mode"]),
                            now,
                            now,
                        ),
                    )
            materialize_work_unit_run(
                connection,
                run_id=run_id,
                policy=policy,
                initial_state="queued",
                replace=True,
                now=now,
            )
            connection.execute(
                "UPDATE fleet_runs SET policy_json = ?, current_phase = NULL, updated_at = ? "
                "WHERE run_id = ?",
                (encoded, now, run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.policy_refreshed",
                payload={
                    "agent_count": len(policy["phases"][0]["workers"]),
                    "reason": "current-supervisor-policy",
                },
            )
            return True

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = min(500, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, substr(r.task, 1, 240) AS task,
                    length(r.task) > 240 AS task_truncated,
                    r.activity, r.cwd, r.origin_session_id, r.origin_agent,
                    r.worker_group_id, r.state, r.current_phase, r.policy_json, r.dry_run,
                    r.cancel_requested, r.error, r.created_at, r.updated_at,
                    r.started_at, r.completed_at,
                    (SELECT COUNT(*) FROM fleet_legs l
                        WHERE l.run_id = r.run_id AND l.state = 'completed') AS completed,
                    (SELECT COUNT(*) FROM fleet_legs l
                        WHERE l.run_id = r.run_id) AS total,
                    (SELECT COUNT(*) FROM fleet_legs l
                        WHERE l.run_id = r.run_id AND l.phase_index = 0) AS agent_count,
                    (SELECT COUNT(DISTINCT a.session_id)
                        FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id = a.leg_id
                        WHERE l.run_id = r.run_id AND a.session_id IS NOT NULL
                            AND a.session_id != '') AS chat_count
                FROM fleet_runs r
                ORDER BY r.created_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            return [
                {
                    "run_id": str(row["run_id"]),
                    "task": str(row["task"] or ""),
                    "task_truncated": bool(row["task_truncated"]),
                    "activity": str(row["activity"]),
                    "cwd": str(row["cwd"]),
                    "origin_session_id": row["origin_session_id"],
                    "origin_agent": row["origin_agent"],
                    "worker_group_id": row["worker_group_id"],
                    "state": str(row["state"]),
                    "current_phase": row["current_phase"],
                    "current_phase_display": _current_phase_display(
                        row["policy_json"], row["current_phase"]
                    ),
                    "dry_run": bool(row["dry_run"]),
                    "cancel_requested": bool(row["cancel_requested"]),
                    "progress": {
                        "completed": int(row["completed"]),
                        "total": int(row["total"]),
                    },
                    "agent_count": int(row["agent_count"]),
                    "chat_count": int(row["chat_count"]),
                    "error": row["error"],
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "started_at": (float(row["started_at"]) if row["started_at"] else None),
                    "completed_at": (float(row["completed_at"]) if row["completed_at"] else None),
                }
                for row in rows
            ]

    def delete_run(self, run_id: str) -> dict[str, Any]:
        """Delete one terminal run and every Fleet-owned database record."""

        clean_id = str(run_id or "").strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, clean_id)
            state = str(run["state"])
            if state not in TERMINAL_RUN_STATES:
                raise RuntimeError("stop this Fleet and wait for it to finish before deleting it")
            rows = connection.execute(
                "SELECT DISTINCT a.session_id FROM fleet_attempts a "
                "JOIN fleet_legs l ON l.leg_id = a.leg_id "
                "WHERE l.run_id = ? AND a.session_id IS NOT NULL AND a.session_id != ''",
                (clean_id,),
            ).fetchall()
            session_ids = sorted({str(row["session_id"]) for row in rows})
            connection.execute("DELETE FROM fleet_runs WHERE run_id = ?", (clean_id,))
            return {
                "run_id": clean_id,
                "state": state,
                "cwd": str(run["cwd"]),
                "origin_session_id": run["origin_session_id"],
                "worker_group_id": run["worker_group_id"],
                "session_ids": session_ids,
            }

    def claim_run(self, run_id: str, *, owner_pid: int | None = None) -> bool:
        pid = int(owner_pid or os.getpid())
        token = process_start_token(pid) or f"pid:{pid}"
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run(connection, run_id)
            state = str(row["state"])
            if state in TERMINAL_RUN_STATES:
                return False
            if state in WAITING_RUN_STATES:
                return False
            if state in {"running", "stopping"} and _process_alive(
                row["owner_pid"], row["owner_token"]
            ):
                return int(row["owner_pid"] or 0) == pid and hmac.compare_digest(
                    str(row["owner_token"] or ""), token
                )
            connection.execute(
                """
                UPDATE fleet_runs SET state = 'running', owner_pid = ?, owner_token = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE run_id = ?
                """,
                (pid, token, now, now, run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.started",
                payload={"state": "running", "owner_pid": pid},
            )
            return True

    def next_queued_run(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM fleet_runs WHERE state = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            return str(row["run_id"]) if row is not None else None

    def set_current_phase(self, run_id: str, phase: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            connection.execute(
                "UPDATE fleet_runs SET current_phase = ?, updated_at = ? WHERE run_id = ?",
                (phase, time.time(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="phase.started",
                payload={"phase": phase},
            )

    def prepare_phase_runnable(self, run_id: str, phase_index: int) -> dict[str, Any] | None:
        """Persist DAG blocks and return the exact provider legs allowed to start."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            result = prepare_work_unit_phase(
                connection,
                run_id=run_id,
                phase_index=int(phase_index),
            )
            if result is not None:
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="dag.runnable_selected",
                    payload=result,
                )
            return result

    def begin_attempt(self, leg_id: str) -> dict[str, Any]:
        now = time.time()
        attempt_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            leg = connection.execute(
                "SELECT * FROM fleet_legs WHERE leg_id = ?", (leg_id,)
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown Fleet leg {leg_id}")
            run = self._require_run(connection, str(leg["run_id"]))
            if bool(run["cancel_requested"]):
                raise RuntimeError("Fleet run cancellation was requested")
            if leg["state"] == "completed":
                raise ValueError("completed Fleet leg cannot start another attempt")
            active = connection.execute(
                "SELECT * FROM fleet_attempts WHERE leg_id = ? AND state = 'running' "
                "ORDER BY attempt_number DESC LIMIT 1",
                (leg_id,),
            ).fetchone()
            if active is not None and _process_alive(active["pid"], active["process_token"]):
                raise RuntimeError("Fleet leg already has a live attempt")
            previous = connection.execute(
                "SELECT * FROM fleet_attempts WHERE leg_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (leg_id,),
            ).fetchone()
            number = int(previous["attempt_number"] or 0) + 1 if previous else 1
            resume_sid = ""
            resume_kind: str | None = None
            resume_source_phase: str | None = None
            handoff_context: dict[str, Any] | None = None
            previous_provider = (
                str(previous["requested_provider"] or "") if previous is not None else ""
            )
            if previous and previous_provider and previous_provider != str(leg["runtime"]):
                resume_kind = "provider_handoff"
                resume_source_phase = str(leg["phase"])
                handoff_context = {
                    "provider": previous_provider,
                    "model": str(previous["requested_model"] or previous["actual_model"] or ""),
                    "effort": str(previous["requested_effort"] or previous["actual_effort"] or ""),
                    "session_id": str(previous["session_id"] or ""),
                    "output_text": str(previous["output_text"] or "")[-MAX_HANDOFF_CONTEXT_CHARS:],
                    "error": str(previous["error"] or "")[:4_000],
                }
            if (
                previous
                and previous["session_id"]
                and (not previous_provider or previous_provider == str(leg["runtime"]))
            ):
                candidate = str(previous["session_id"])
                if self._session_can_resume(
                    connection,
                    run_id=str(leg["run_id"]),
                    runtime=str(leg["runtime"]),
                    session_id=candidate,
                ):
                    resume_sid = candidate
                    resume_kind = "retry"
                    resume_source_phase = str(leg["phase"])
            if (
                not resume_sid
                and resume_kind != "provider_handoff"
                and _session_mode(run["policy_json"]) == "persistent_by_worker"
            ):
                # A durable worker deliberately changes model and effort between
                # Fleet phases. Identity is the stable ordinal plus provider;
                # worker_command pins the next phase's requested settings on
                # resume, and the result verifier checks what actually ran.
                prior = connection.execute(
                    """
                    SELECT a.session_id, l.phase
                    FROM fleet_attempts a
                    JOIN fleet_legs l ON l.leg_id = a.leg_id
                    WHERE l.run_id = ? AND l.phase_index < ? AND l.ordinal = ?
                        AND COALESCE(a.requested_provider, l.runtime) = ?
                        AND a.state = 'completed'
                        AND a.session_id IS NOT NULL AND a.session_id != ''
                    ORDER BY l.phase_index DESC, a.attempt_number DESC
                    LIMIT 1
                    """,
                    (
                        str(leg["run_id"]),
                        int(leg["phase_index"]),
                        int(leg["ordinal"]),
                        str(leg["runtime"]),
                    ),
                ).fetchone()
                if prior and self._session_can_resume(
                    connection,
                    run_id=str(leg["run_id"]),
                    runtime=str(leg["runtime"]),
                    session_id=str(prior["session_id"]),
                ):
                    resume_sid = str(prior["session_id"])
                    resume_kind = "phase_continuation"
                    resume_source_phase = str(prior["phase"])
            connection.execute(
                """
                INSERT INTO fleet_attempts(
                    attempt_id, leg_id, attempt_number, state,
                    requested_provider, requested_model, requested_effort,
                    started_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    leg_id,
                    number,
                    str(leg["runtime"]),
                    str(leg["requested_model"]),
                    str(leg["requested_effort"]),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE fleet_legs SET state = 'running', current_attempt = ?, updated_at = ? "
                "WHERE leg_id = ?",
                (number, now, leg_id),
            )
            mark_work_unit_leg_started(
                connection,
                leg_id=leg_id,
                attempt_id=attempt_id,
                now=now,
            )
            self._insert_event(
                connection,
                run_id=str(leg["run_id"]),
                leg_id=leg_id,
                attempt_id=attempt_id,
                event_type="attempt.started",
                payload={
                    "attempt_number": number,
                    "resume_session_id": resume_sid or None,
                    "resume_kind": resume_kind,
                    "resume_source_phase": resume_source_phase,
                    "handoff_from_provider": (
                        handoff_context.get("provider") if handoff_context else None
                    ),
                },
            )
            return {
                "attempt_id": attempt_id,
                "attempt_number": number,
                "leg_id": leg_id,
                "run_id": str(leg["run_id"]),
                "resume_session_id": resume_sid or None,
                "resume_kind": resume_kind,
                "resume_source_phase": resume_source_phase,
                "handoff_context": handoff_context,
            }

    @staticmethod
    def _session_can_resume(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        runtime: str,
        session_id: str,
    ) -> bool:
        """Return whether a provider session is known to exist durably.

        Codex only reports a session id after thread creation. Claude reserves
        one before launch, so at least one init/model event must have confirmed
        that id. Confirmation can come from an earlier phase when a resumed
        turn itself failed before init.
        """

        if not session_id:
            return False
        if runtime != "claude":
            return True
        row = connection.execute(
            """
            SELECT 1
            FROM fleet_attempts a
            JOIN fleet_legs l ON l.leg_id = a.leg_id
            WHERE l.run_id = ? AND a.session_id = ?
                AND a.actual_model IS NOT NULL AND a.actual_model != ''
            LIMIT 1
            """,
            (run_id, session_id),
        ).fetchone()
        return row is not None

    def previous_attempt_error(self, leg_id: str, *, before_attempt_number: int) -> str | None:
        """Most recent recorded error from an earlier attempt of this leg."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT error FROM fleet_attempts WHERE leg_id = ? AND attempt_number < ? "
                "AND error IS NOT NULL AND error != '' "
                "ORDER BY attempt_number DESC LIMIT 1",
                (str(leg_id), int(before_attempt_number)),
            ).fetchone()
        return str(row["error"]) if row else None

    def mark_attempt_process(self, attempt_id: str, pid: int, event_log_path: str = "") -> None:
        token = process_start_token(int(pid)) or f"pid:{int(pid)}"
        with self._connect() as connection:
            connection.execute(
                "UPDATE fleet_attempts SET pid = ?, process_token = ?, event_log_path = ?, "
                "updated_at = ? WHERE attempt_id = ? AND state = 'running'",
                (int(pid), token, event_log_path or None, time.time(), attempt_id),
            )

    def mark_attempt_session(self, attempt_id: str, session_id: str) -> None:
        clean = str(session_id or "").strip()
        if not clean:
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE fleet_attempts SET session_id = ?, updated_at = ? WHERE attempt_id = ?",
                (clean, time.time(), attempt_id),
            )

    def mark_attempt_identity(
        self,
        attempt_id: str,
        *,
        actual_model: str | None,
        actual_effort: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE fleet_attempts SET actual_model = COALESCE(?, actual_model), "
                "actual_effort = COALESCE(?, actual_effort), updated_at = ? "
                "WHERE attempt_id = ?",
                (actual_model, actual_effort, time.time(), attempt_id),
            )

    def record_context_receipt(self, attempt_id: str, receipt: dict[str, Any]) -> None:
        """Persist the prompt context budget without storing another copy of context."""

        allowed = {
            "strategy": str(receipt.get("strategy") or "unknown")[:64],
            "source_chars": max(0, int(receipt.get("source_chars") or 0)),
            "delivered_chars": max(0, int(receipt.get("delivered_chars") or 0)),
            "omitted_chars": max(0, int(receipt.get("omitted_chars") or 0)),
            "source_count": max(0, int(receipt.get("source_count") or 0)),
            "redaction_count": max(0, int(receipt.get("redaction_count") or 0)),
            "full_history_preserved": bool(receipt.get("full_history_preserved")),
            "source_sha256": str(receipt.get("source_sha256") or "")[:128],
            "budget_chars": max(0, int(receipt.get("budget_chars") or 0)),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                """
                SELECT a.attempt_id, l.run_id, l.leg_id
                FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id = a.leg_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(f"unknown Fleet attempt {attempt_id}")
            connection.execute(
                """
                INSERT INTO fleet_context_receipts(
                    attempt_id, strategy, source_chars, delivered_chars, omitted_chars,
                    source_count, redaction_count, full_history_preserved,
                    source_sha256, budget_chars, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    strategy = excluded.strategy,
                    source_chars = excluded.source_chars,
                    delivered_chars = excluded.delivered_chars,
                    omitted_chars = excluded.omitted_chars,
                    source_count = excluded.source_count,
                    redaction_count = excluded.redaction_count,
                    full_history_preserved = excluded.full_history_preserved,
                    source_sha256 = excluded.source_sha256,
                    budget_chars = excluded.budget_chars,
                    created_at = excluded.created_at
                """,
                (
                    attempt_id,
                    allowed["strategy"],
                    allowed["source_chars"],
                    allowed["delivered_chars"],
                    allowed["omitted_chars"],
                    allowed["source_count"],
                    allowed["redaction_count"],
                    int(allowed["full_history_preserved"]),
                    allowed["source_sha256"],
                    allowed["budget_chars"],
                    time.time(),
                ),
            )
            self._insert_event(
                connection,
                run_id=str(attempt["run_id"]),
                leg_id=str(attempt["leg_id"]),
                attempt_id=attempt_id,
                event_type="context.budgeted",
                payload=allowed,
            )

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        output_text: str = "",
        error: str | None = None,
        session_id: str | None = None,
        actual_model: str | None = None,
        actual_effort: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        if state not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("invalid Fleet attempt terminal state")
        output = redact_text(output_text)[0][:MAX_OUTPUT_CHARS]
        clean_error = redact_text(error)[0].strip()[:4_000] or None
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT a.*, l.run_id FROM fleet_attempts a "
                "JOIN fleet_legs l ON l.leg_id = a.leg_id WHERE a.attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(f"unknown Fleet attempt {attempt_id}")
            run = self._require_run(connection, str(attempt["run_id"]))
            if state == "completed" and bool(run["cancel_requested"]):
                state = "cancelled"
                clean_error = clean_error or "cancelled by user"
            connection.execute(
                """
                UPDATE fleet_attempts SET state = ?, session_id = COALESCE(?, session_id),
                    actual_model = COALESCE(?, actual_model),
                    actual_effort = COALESCE(?, actual_effort),
                    output_text = ?, error = ?, exit_code = ?, pid = NULL,
                    process_token = NULL, completed_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    state,
                    session_id,
                    actual_model,
                    actual_effort,
                    output,
                    clean_error,
                    exit_code,
                    now,
                    now,
                    attempt_id,
                ),
            )
            leg_state = "completed" if state == "completed" else state
            connection.execute(
                "UPDATE fleet_legs SET state = ?, updated_at = ? WHERE leg_id = ?",
                (leg_state, now, str(attempt["leg_id"])),
            )
            mark_work_unit_leg_finished(
                connection,
                leg_id=str(attempt["leg_id"]),
                attempt_id=attempt_id,
                state=state,
                error=clean_error,
                now=now,
            )
            self._insert_event(
                connection,
                run_id=str(attempt["run_id"]),
                leg_id=str(attempt["leg_id"]),
                attempt_id=attempt_id,
                event_type=f"attempt.{state}",
                payload={"state": state, "error": clean_error},
            )

    def complete_run(self, run_id: str, result_text: str) -> dict[str, Any]:
        return self._finish_run(run_id, "completed", result_text=result_text)

    def fail_run(self, run_id: str, error: str) -> dict[str, Any]:
        return self._finish_run(run_id, "failed", error=error)

    def cancel_run(self, run_id: str, error: str = "cancelled by user") -> dict[str, Any]:
        return self._finish_run(run_id, "cancelled", error=error)

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run(connection, run_id)
            state = str(row["state"])
            if state in TERMINAL_RUN_STATES:
                return self._snapshot(connection, run_id)
            if state in {"queued", "waiting_for_capacity"}:
                connection.execute(
                    "UPDATE fleet_runs SET state = 'cancelled', cancel_requested = 1, "
                    "error = 'cancelled by user', completed_at = ?, updated_at = ? WHERE run_id = ?",
                    (now, now, run_id),
                )
                connection.execute(
                    "UPDATE fleet_legs SET state = 'cancelled', updated_at = ? "
                    "WHERE run_id = ? AND state IN "
                    "('queued', 'waiting_for_capacity', 'waiting_for_dependencies')",
                    (now, run_id),
                )
                cancel_unfinished_work_units(
                    connection,
                    run_id=run_id,
                    reason="cancelled by user",
                    now=now,
                )
            else:
                connection.execute(
                    "UPDATE fleet_runs SET state = 'stopping', cancel_requested = 1, "
                    "updated_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE run_id = ?",
                (run_id,),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.cancel_requested",
                payload={
                    "state": (
                        "cancelled"
                        if state in {"queued", "waiting_for_capacity"}
                        else "stopping"
                    )
                },
            )
            return self._snapshot(connection, run_id)

    def retry_run(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run(connection, run_id)
            if row["dry_run"]:
                raise ValueError("a dry-run plan cannot be retried")
            if row["state"] not in {"failed", "cancelled"}:
                raise ValueError("only failed or cancelled Fleet runs can be retried")
            connection.execute(
                """
                UPDATE fleet_runs SET state = 'queued', cancel_requested = 0,
                    owner_pid = NULL, owner_token = NULL, error = NULL,
                    result_text = NULL, completed_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            connection.execute(
                "UPDATE fleet_attempts SET state = 'interrupted', completed_at = ?, "
                "updated_at = ? WHERE leg_id IN "
                "(SELECT leg_id FROM fleet_legs WHERE run_id = ?) AND state = 'running'",
                (now, now, run_id),
            )
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? "
                "WHERE run_id = ? AND state != 'completed'",
                (now, run_id),
            )
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE run_id = ?",
                (run_id,),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.retried",
                payload={"state": "queued"},
            )
            return self._snapshot(connection, run_id)

    def promote_queued_run_policy(
        self,
        run_id: str,
        policy: dict[str, Any],
        *,
        control_message: str,
    ) -> dict[str, Any]:
        """Promote unfinished legs to a new snapshotted policy before they resume."""

        now = time.time()
        phases = policy.get("phases") if isinstance(policy, dict) else None
        if not isinstance(phases, list) or not phases:
            raise ValueError("replacement Fleet policy has no phases")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if run["state"] != "queued":
                raise RuntimeError("only a queued Fleet run can be promoted")
            legs = connection.execute(
                "SELECT leg_id, phase_index, ordinal, state FROM fleet_legs "
                "WHERE run_id = ? ORDER BY phase_index, ordinal",
                (run_id,),
            ).fetchall()
            legs_by_phase: dict[int, list[sqlite3.Row]] = {}
            for leg in legs:
                legs_by_phase.setdefault(int(leg["phase_index"]), []).append(leg)
            for phase in phases:
                index = int(phase["index"])
                phase_legs = legs_by_phase.get(index, [])
                workers = phase.get("workers")
                if (
                    not phase_legs
                    or not isinstance(workers, list)
                    or len(workers) != len(phase_legs)
                ):
                    raise ValueError("replacement Fleet policy does not match persisted workers")
                if all(str(leg["state"]) == "completed" for leg in phase_legs):
                    continue
                connection.execute(
                    "UPDATE fleet_legs SET execution = ?, updated_at = ? "
                    "WHERE run_id = ? AND phase_index = ?",
                    (str(phase["execution"]), now, run_id, index),
                )
                workers_by_ordinal = {ordinal: worker for ordinal, worker in enumerate(workers)}
                for leg in phase_legs:
                    if str(leg["state"]) == "completed":
                        continue
                    worker = workers_by_ordinal[int(leg["ordinal"])]
                    connection.execute(
                        """
                        UPDATE fleet_legs SET runtime = ?, role = ?, requested_model = ?,
                            requested_effort = ?, access_mode = ?, updated_at = ?
                        WHERE leg_id = ?
                        """,
                        (
                            str(worker.get("provider") or worker.get("runtime")),
                            str(worker["role"]),
                            str(worker["model"]),
                            str(worker["effort"]),
                            str(worker["access_mode"]),
                            now,
                            str(leg["leg_id"]),
                        ),
                    )
            connection.execute(
                "UPDATE fleet_runs SET policy_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(policy, separators=(",", ":"), sort_keys=True), now, run_id),
            )
            connection.execute(
                "DELETE FROM fleet_steering WHERE run_id = ? AND message = ?",
                (run_id, str(control_message)),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.policy_promoted",
                payload={"reason": "parallel-collaboration", "state": "queued"},
            )
            return self._snapshot(connection, run_id)

    def request_leg_retry(self, run_id: str, leg_id: str) -> dict[str, Any]:
        """Persist a targeted retry without interrupting a live sibling worker."""

        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if bool(run["dry_run"]):
                raise ValueError("a dry-run worker cannot be retried")
            if run["state"] not in {"queued", "running", "failed", "waiting_for_capacity"}:
                raise RuntimeError(
                    "a failed worker can be retried only while its run is active or failed"
                )
            leg = connection.execute(
                "SELECT * FROM fleet_legs WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown Fleet worker {leg_id}")
            if leg["state"] not in {"failed", "waiting_for_capacity"}:
                raise ValueError("only a failed or capacity-waiting Fleet worker can be retried")

            if run["state"] in {"failed", "waiting_for_capacity"}:
                connection.execute(
                    """
                    UPDATE fleet_runs SET state = 'queued', cancel_requested = 0,
                        owner_pid = NULL, owner_token = NULL, error = NULL,
                        result_text = NULL, completed_at = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, run_id),
                )
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                (now, leg_id),
            )
            reset_work_unit_leg_for_retry(connection, leg_id=leg_id, now=now)
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE leg_id = ?",
                (leg_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE leg_id = ?",
                (leg_id,),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.retry_requested",
                payload={"state": "queued"},
            )
            return self._snapshot(connection, run_id)

    def request_leg_handoff(
        self,
        run_id: str,
        leg_id: str,
        *,
        target_provider: str,
        reason: str,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Queue a provider switch and interrupt only that worker when live."""

        target = str(target_provider or "").strip().lower()
        if target not in {"codex", "claude"}:
            raise ValueError("handoff provider must be claude or codex")
        clean_reason = str(reason or "provider handoff").strip()[:1_000]
        now = time.time()
        owned_process: tuple[object, object] | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if bool(run["dry_run"]):
                raise ValueError("a dry-run worker cannot be handed off")
            if str(run["state"]) not in {
                "queued",
                "running",
                "failed",
                "waiting_for_capacity",
            }:
                raise RuntimeError(
                    "provider handoff requires an active, failed, or capacity-waiting Fleet run"
                )
            leg = connection.execute(
                "SELECT * FROM fleet_legs WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown Fleet worker {leg_id}")
            if str(leg["state"]) == "completed":
                raise ValueError("a completed Fleet worker cannot be handed off")
            source = str(leg["runtime"])
            if source == target:
                raise ValueError(f"worker already uses {target}")
            connection.execute(
                """
                INSERT INTO fleet_leg_handoff_requests(
                    leg_id, run_id, target_provider, reason, automatic, requested_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(leg_id) DO UPDATE SET
                    target_provider = excluded.target_provider,
                    reason = excluded.reason,
                    automatic = excluded.automatic,
                    requested_at = excluded.requested_at
                """,
                (leg_id, run_id, target, clean_reason, int(bool(automatic)), now),
            )
            if str(leg["state"]) == "running":
                attempt = connection.execute(
                    "SELECT pid, process_token FROM fleet_attempts "
                    "WHERE leg_id = ? AND state = 'running' "
                    "ORDER BY attempt_number DESC LIMIT 1",
                    (leg_id,),
                ).fetchone()
                if attempt is not None and attempt["pid"]:
                    owned_process = (attempt["pid"], attempt["process_token"])
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.handoff_requested",
                payload={
                    "from_provider": source,
                    "to_provider": target,
                    "automatic": bool(automatic),
                    "reason": clean_reason,
                    "state": "waiting-for-worker" if owned_process else "waiting-for-phase-barrier",
                },
            )
            snapshot = self._snapshot(connection, run_id)
        if owned_process is not None:
            interrupted = _terminate_owned_process(*owned_process)
            self.append_event(
                run_id,
                "leg.handoff_interrupt",
                {"interrupted": interrupted, "to_provider": target},
                leg_id=leg_id,
            )
            snapshot = self.get_run(run_id) or snapshot
        return snapshot

    def pending_handoffs(self, run_id: str, phase: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._require_run(connection, run_id)
            params: list[object] = [run_id]
            where = "r.run_id = ?"
            if phase is not None:
                where += " AND l.phase = ?"
                params.append(str(phase))
            rows = connection.execute(
                """
                SELECT r.leg_id, r.target_provider, r.reason, r.automatic,
                    r.requested_at, l.phase_index, l.phase, l.ordinal,
                    l.runtime AS source_provider, l.state AS leg_state
                FROM fleet_leg_handoff_requests r
                JOIN fleet_legs l ON l.leg_id = r.leg_id
                WHERE """
                + where
                + " ORDER BY l.phase_index, l.ordinal",
                tuple(params),
            ).fetchall()
            return [
                {
                    "leg_id": str(row["leg_id"]),
                    "target_provider": str(row["target_provider"]),
                    "reason": str(row["reason"] or "provider handoff"),
                    "automatic": bool(row["automatic"]),
                    "requested_at": float(row["requested_at"]),
                    "phase_index": int(row["phase_index"]),
                    "phase": str(row["phase"]),
                    "ordinal": int(row["ordinal"]),
                    "source_provider": str(row["source_provider"]),
                    "leg_state": str(row["leg_state"]),
                }
                for row in rows
            ]

    def request_capacity_wait(
        self,
        run_id: str,
        leg_id: str,
        *,
        failed_provider: str,
        eligible_providers: list[str] | tuple[str, ...],
        reason: str,
        not_before: float,
        resets_at: float | None = None,
    ) -> dict[str, Any]:
        """Park one failed logical worker until a native provider is usable."""

        failed = str(failed_provider or "").strip().lower()
        eligible = tuple(
            dict.fromkeys(
                str(provider or "").strip().lower()
                for provider in eligible_providers
                if str(provider or "").strip().lower() in {"codex", "claude"}
            )
        )
        if failed not in {"codex", "claude"}:
            raise ValueError("failed capacity provider must be claude or codex")
        if not eligible:
            raise ValueError("capacity wait requires at least one eligible provider")
        clean_reason = str(reason or f"{failed} capacity is exhausted").strip()[:1_000]
        now = time.time()
        resume_time = max(now, float(not_before))
        reset_time = float(resets_at) if resets_at is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if str(run["state"]) in TERMINAL_RUN_STATES or bool(run["dry_run"]):
                raise RuntimeError("capacity wait requires an active Fleet run")
            leg = connection.execute(
                "SELECT * FROM fleet_legs WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown Fleet worker {leg_id}")
            if str(leg["state"]) not in {"failed", "waiting_for_capacity"}:
                raise ValueError("only a failed Fleet worker can wait for capacity")
            connection.execute(
                """
                INSERT INTO fleet_capacity_waits(
                    leg_id, run_id, failed_provider, eligible_providers_json,
                    reason, not_before, resets_at, requested_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(leg_id) DO UPDATE SET
                    failed_provider = excluded.failed_provider,
                    eligible_providers_json = excluded.eligible_providers_json,
                    reason = excluded.reason,
                    not_before = excluded.not_before,
                    resets_at = excluded.resets_at,
                    updated_at = excluded.updated_at
                """,
                (
                    leg_id,
                    run_id,
                    failed,
                    json.dumps(list(eligible), separators=(",", ":")),
                    clean_reason,
                    resume_time,
                    reset_time,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE fleet_legs SET state = 'waiting_for_capacity', updated_at = ? "
                "WHERE leg_id = ?",
                (now, leg_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.waiting_for_capacity",
                payload={
                    "failed_provider": failed,
                    "eligible_providers": list(eligible),
                    "reason": clean_reason,
                    "not_before": resume_time,
                    "resets_at": reset_time,
                },
            )
            return self._snapshot(connection, run_id)

    def capacity_waits(
        self,
        run_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = min(500, max(1, int(limit)))
        where = "WHERE w.run_id = ?" if run_id else ""
        params: tuple[object, ...] = (run_id, safe_limit) if run_id else (safe_limit,)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT w.*, l.runtime, l.phase, l.phase_index, l.ordinal, r.state AS run_state "
                "FROM fleet_capacity_waits w "
                "JOIN fleet_legs l ON l.leg_id = w.leg_id "
                "JOIN fleet_runs r ON r.run_id = w.run_id "
                f"{where} ORDER BY w.not_before, w.requested_at LIMIT ?",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                decoded = json.loads(str(row["eligible_providers_json"] or "[]"))
            except json.JSONDecodeError:
                decoded = []
            result.append(
                {
                    "run_id": str(row["run_id"]),
                    "leg_id": str(row["leg_id"]),
                    "failed_provider": str(row["failed_provider"]),
                    "eligible_providers": [
                        str(provider)
                        for provider in decoded
                        if str(provider) in {"codex", "claude"}
                    ],
                    "reason": str(row["reason"] or ""),
                    "not_before": float(row["not_before"]),
                    "resets_at": (
                        float(row["resets_at"]) if row["resets_at"] is not None else None
                    ),
                    "requested_at": float(row["requested_at"]),
                    "updated_at": float(row["updated_at"]),
                    "runtime": str(row["runtime"]),
                    "phase": str(row["phase"]),
                    "phase_index": int(row["phase_index"]),
                    "ordinal": int(row["ordinal"]),
                    "run_state": str(row["run_state"]),
                }
            )
        return result

    def resume_capacity_wait(
        self,
        run_id: str,
        leg_id: str,
        *,
        provider: str,
        reason: str,
    ) -> dict[str, Any]:
        """Requeue a parked worker on its existing native provider."""

        selected = str(provider or "").strip().lower()
        if selected not in {"codex", "claude"}:
            raise ValueError("capacity resume provider must be claude or codex")
        now = time.time()
        clean_reason = str(reason or f"{selected} capacity recovered").strip()[:1_000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if str(run["state"]) != "waiting_for_capacity":
                raise RuntimeError("Fleet run is not waiting for capacity")
            wait = connection.execute(
                "SELECT * FROM fleet_capacity_waits WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if wait is None:
                raise KeyError(f"Fleet worker {leg_id} is not waiting for capacity")
            try:
                eligible = json.loads(str(wait["eligible_providers_json"] or "[]"))
            except json.JSONDecodeError:
                eligible = []
            if selected not in eligible:
                raise ValueError(f"{selected} is not eligible for this capacity wait")
            leg = connection.execute(
                "SELECT runtime FROM fleet_legs WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown Fleet worker {leg_id}")
            if str(leg["runtime"]) != selected:
                raise ValueError("a cross-provider capacity resume requires provider handoff")
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE leg_id = ?",
                (leg_id,),
            )
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                (now, leg_id),
            )
            connection.execute(
                """
                UPDATE fleet_runs SET state = 'queued', owner_pid = NULL,
                    owner_token = NULL, error = NULL, completed_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.capacity_resumed",
                payload={"provider": selected, "reason": clean_reason, "state": "queued"},
            )
            return self._snapshot(connection, run_id)

    def clear_leg_handoff(self, run_id: str, leg_id: str, *, reason: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.handoff_cleared",
                payload={"reason": str(reason or "handoff no longer applies")[:1_000]},
            )
            return self._snapshot(connection, run_id)

    def apply_leg_handoff(
        self,
        run_id: str,
        leg_id: str,
        *,
        policy: dict[str, Any],
        start_phase_index: int,
        target_provider: str,
        reason: str,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Atomically switch one logical worker slot for all unfinished phases."""

        phases = policy.get("phases") if isinstance(policy, dict) else None
        if not isinstance(phases, list) or not phases:
            raise ValueError("replacement Fleet policy has no phases")
        target = str(target_provider or "").strip().lower()
        if target not in {"codex", "claude"}:
            raise ValueError("handoff provider must be claude or codex")
        now = time.time()
        clean_reason = str(reason or "provider handoff").strip()[:1_000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if str(run["state"]) not in {
                "queued",
                "running",
                "failed",
                "waiting_for_capacity",
            }:
                raise RuntimeError("provider handoff can no longer change this Fleet run")
            source_leg = connection.execute(
                "SELECT * FROM fleet_legs WHERE run_id = ? AND leg_id = ?",
                (run_id, leg_id),
            ).fetchone()
            if source_leg is None:
                raise KeyError(f"unknown Fleet worker {leg_id}")
            if str(source_leg["state"]) == "running":
                raise RuntimeError("provider handoff is waiting for the active worker to stop")
            ordinal = int(source_leg["ordinal"])
            start_index = int(start_phase_index)
            affected: list[str] = []
            for phase in phases:
                index = int(phase["index"])
                if index < start_index:
                    continue
                workers = phase.get("workers")
                if not isinstance(workers, list) or ordinal >= len(workers):
                    raise ValueError("replacement Fleet policy does not match persisted workers")
                worker = workers[ordinal]
                provider = str(worker.get("provider") or worker.get("runtime") or "").lower()
                if provider != target:
                    raise ValueError("replacement Fleet policy does not use the target provider")
                persisted = connection.execute(
                    "SELECT * FROM fleet_legs WHERE run_id = ? AND phase_index = ? AND ordinal = ?",
                    (run_id, index, ordinal),
                ).fetchone()
                if persisted is None:
                    raise ValueError("replacement Fleet policy does not match persisted phases")
                if str(persisted["state"]) == "completed":
                    continue
                connection.execute(
                    """
                    UPDATE fleet_attempts
                    SET requested_provider = COALESCE(requested_provider, ?),
                        requested_model = COALESCE(requested_model, ?),
                        requested_effort = COALESCE(requested_effort, ?)
                    WHERE leg_id = ?
                    """,
                    (
                        str(persisted["runtime"]),
                        str(persisted["requested_model"]),
                        str(persisted["requested_effort"]),
                        str(persisted["leg_id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE fleet_legs SET runtime = ?, role = ?, requested_model = ?,
                        requested_effort = ?, access_mode = ?, updated_at = ?
                    WHERE leg_id = ?
                    """,
                    (
                        target,
                        str(worker["role"]),
                        str(worker["model"]),
                        str(worker["effort"]),
                        str(worker["access_mode"]),
                        now,
                        str(persisted["leg_id"]),
                    ),
                )
                affected.append(str(persisted["leg_id"]))
            if str(source_leg["state"]) != "completed" and int(source_leg["phase_index"]) >= start_index:
                connection.execute(
                    "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                    (now, leg_id),
                )
            connection.execute(
                "UPDATE fleet_runs SET policy_json = ?, error = NULL, updated_at = ? WHERE run_id = ?",
                (json.dumps(policy, separators=(",", ":"), sort_keys=True), now, run_id),
            )
            if str(run["state"]) in {"failed", "waiting_for_capacity"}:
                connection.execute(
                    """
                    UPDATE fleet_runs SET state = 'queued', cancel_requested = 0,
                        owner_pid = NULL, owner_token = NULL, result_text = NULL,
                        completed_at = NULL, updated_at = ? WHERE run_id = ?
                    """,
                    (now, run_id),
                )
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE leg_id = ?",
                (leg_id,),
            )
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE leg_id = ?",
                (leg_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE leg_id = ?",
                (leg_id,),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                event_type="leg.handoff_activated",
                payload={
                    "to_provider": target,
                    "automatic": bool(automatic),
                    "reason": clean_reason,
                    "start_phase_index": start_index,
                    "affected_legs": affected,
                    "state": "queued",
                },
            )
            return self._snapshot(connection, run_id)

    def resolve_phase_failure(self, run_id: str, phase: str, error: str) -> dict[str, Any]:
        """Atomically activate queued worker retries or fail the run.

        This closes the race between the last live sibling finishing and a UI
        retry request arriving at the phase barrier.
        """

        now = time.time()
        clean_error = str(error or "phase did not complete").strip()[:4_000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            if run["state"] in TERMINAL_RUN_STATES:
                snapshot = self._snapshot(connection, run_id)
                snapshot["retry_activated"] = False
                return snapshot
            if bool(run["cancel_requested"]) or run["state"] == "stopping":
                connection.execute(
                    "UPDATE fleet_legs SET state = 'cancelled', updated_at = ? "
                    "WHERE run_id = ? AND state != 'completed'",
                    (now, run_id),
                )
                cancel_unfinished_work_units(
                    connection,
                    run_id=run_id,
                    reason="cancelled by user",
                    now=now,
                )
                connection.execute(
                    """
                    UPDATE fleet_runs SET state = 'cancelled', result_text = NULL,
                        error = 'cancelled by user', owner_pid = NULL, owner_token = NULL,
                        completed_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, now, run_id),
                )
                connection.execute(
                    "DELETE FROM fleet_leg_retry_requests WHERE run_id = ?",
                    (run_id,),
                )
                connection.execute(
                    "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ?",
                    (run_id,),
                )
                connection.execute(
                    "DELETE FROM fleet_capacity_waits WHERE run_id = ?",
                    (run_id,),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run.cancelled",
                    payload={"state": "cancelled", "error": "cancelled by user"},
                )
                snapshot = self._snapshot(connection, run_id)
                snapshot["retry_activated"] = False
                return snapshot
            incomplete = connection.execute(
                "SELECT leg_id, state FROM fleet_legs WHERE run_id = ? AND phase = ? "
                "AND state != 'completed' ORDER BY ordinal",
                (run_id, phase),
            ).fetchall()
            capacity_rows = connection.execute(
                "SELECT w.leg_id, w.reason, w.not_before, w.resets_at, "
                "w.eligible_providers_json FROM fleet_capacity_waits w "
                "JOIN fleet_legs l ON l.leg_id = w.leg_id "
                "WHERE w.run_id = ? AND l.phase = ? ORDER BY l.ordinal",
                (run_id, phase),
            ).fetchall()
            waiting_ids = {str(row["leg_id"]) for row in capacity_rows}
            if incomplete and all(
                str(row["state"]) == "waiting_for_capacity"
                and str(row["leg_id"]) in waiting_ids
                for row in incomplete
            ):
                earliest = min(float(row["not_before"]) for row in capacity_rows)
                reset_values = [
                    float(row["resets_at"])
                    for row in capacity_rows
                    if row["resets_at"] is not None
                ]
                next_reset = min(reset_values) if reset_values else None
                providers: set[str] = set()
                for row in capacity_rows:
                    try:
                        decoded = json.loads(str(row["eligible_providers_json"] or "[]"))
                    except json.JSONDecodeError:
                        decoded = []
                    providers.update(str(provider) for provider in decoded)
                wait_error = next(
                    (str(row["reason"]) for row in capacity_rows if row["reason"]),
                    clean_error,
                )
                connection.execute(
                    """
                    UPDATE fleet_runs SET state = 'waiting_for_capacity', error = ?,
                        owner_pid = NULL, owner_token = NULL, completed_at = NULL,
                        updated_at = ? WHERE run_id = ?
                    """,
                    (wait_error, now, run_id),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run.waiting_for_capacity",
                    payload={
                        "phase": phase,
                        "providers": sorted(providers),
                        "not_before": earliest,
                        "resets_at": next_reset,
                        "state": "waiting_for_capacity",
                    },
                )
                snapshot = self._snapshot(connection, run_id)
                snapshot["retry_activated"] = False
                snapshot["capacity_waiting"] = True
                return snapshot
            requested = connection.execute(
                """
                SELECT r.leg_id FROM fleet_leg_retry_requests r
                JOIN fleet_legs l ON l.leg_id = r.leg_id
                WHERE r.run_id = ? AND l.phase = ? AND l.state = 'failed'
                ORDER BY l.ordinal
                """,
                (run_id, phase),
            ).fetchall()
            leg_ids = [str(row["leg_id"]) for row in requested]
            if leg_ids:
                placeholders = ",".join("?" for _ in leg_ids)
                connection.execute(
                    f"UPDATE fleet_legs SET state = 'queued', updated_at = ? "
                    f"WHERE leg_id IN ({placeholders})",
                    (now, *leg_ids),
                )
                connection.execute(
                    f"DELETE FROM fleet_leg_retry_requests WHERE leg_id IN ({placeholders})",
                    tuple(leg_ids),
                )
                connection.execute(
                    "UPDATE fleet_runs SET error = NULL, updated_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                for leg_id in leg_ids:
                    self._insert_event(
                        connection,
                        run_id=run_id,
                        leg_id=leg_id,
                        event_type="leg.retry_activated",
                        payload={"phase": phase, "state": "queued"},
                    )
                snapshot = self._snapshot(connection, run_id)
                snapshot["retry_activated"] = True
                return snapshot

            connection.execute(
                """
                UPDATE fleet_runs SET state = 'failed', result_text = NULL, error = ?,
                    owner_pid = NULL, owner_token = NULL, completed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (clean_error, now, now, run_id),
            )
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE run_id = ?",
                (run_id,),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.failed",
                payload={"state": "failed", "error": clean_error},
            )
            snapshot = self._snapshot(connection, run_id)
            snapshot["retry_activated"] = False
            return snapshot

    def add_steering(self, run_id: str, message: str) -> dict[str, Any]:
        clean = redact_text(message)[0].strip()
        if not clean:
            raise ValueError("steering message is required")
        if len(clean) > MAX_STEERING_MESSAGE_CHARS:
            raise ValueError("steering message is too long")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run(connection, run_id)
            if row["state"] in TERMINAL_RUN_STATES:
                raise ValueError("cannot steer a terminal Fleet run")
            total = connection.execute(
                "SELECT COALESCE(SUM(length(message)), 0) AS total "
                "FROM fleet_steering WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if int(total["total"] or 0) + len(clean) > MAX_STEERING_TOTAL_CHARS:
                raise ValueError("cumulative Fleet steering context is too long")
            connection.execute(
                "INSERT INTO fleet_steering(run_id, message, created_at) VALUES (?, ?, ?)",
                (run_id, clean, time.time()),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.steered",
                payload={"message": clean},
            )
            return self._snapshot(connection, run_id)

    def steering_messages(self, run_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT message FROM fleet_steering WHERE run_id = ? ORDER BY steering_seq",
                (run_id,),
            ).fetchall()
            return [str(row["message"]) for row in rows]

    def run_cancel_requested(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM fleet_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def completed_outputs(
        self, run_id: str, *, before_phase: int | None = None
    ) -> list[dict[str, Any]]:
        where = "l.run_id = ? AND a.state = 'completed'"
        params: list[Any] = [run_id]
        if before_phase is not None:
            where += " AND l.phase_index < ?"
            params.append(int(before_phase))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT l.phase_index, l.phase, l.ordinal, l.role, l.runtime, a.output_text
                FROM fleet_legs l
                JOIN fleet_attempts a ON a.leg_id = l.leg_id
                WHERE """
                + where
                + " "
                "AND a.attempt_number = (SELECT MAX(a2.attempt_number) "
                "FROM fleet_attempts a2 WHERE a2.leg_id = l.leg_id AND a2.state = 'completed') "
                "ORDER BY l.phase_index, l.ordinal",
                tuple(params),
            ).fetchall()
            return [
                {
                    "phase_index": int(row["phase_index"]),
                    "phase": str(row["phase"]),
                    "ordinal": int(row["ordinal"]),
                    "role": str(row["role"]),
                    "runtime": str(row["runtime"]),
                    "output_text": str(row["output_text"] or ""),
                }
                for row in rows
            ]

    def set_worker_group(self, run_id: str, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE fleet_runs SET worker_group_id = ?, updated_at = ? WHERE run_id = ?",
                (group_id, time.time(), run_id),
            )

    def ensure_worker_group(self, run_id: str) -> str:
        """Return the run's reserved worker group, creating it atomically for legacy runs."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_run(connection, run_id)
            existing = str(run["worker_group_id"] or "").strip()
            if existing:
                return existing
            group_id = _new_worker_group_id()
            connection.execute(
                "UPDATE fleet_runs SET worker_group_id = ?, updated_at = ? WHERE run_id = ?",
                (group_id, time.time(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.worker_group_reserved",
                payload={"worker_group_id": group_id},
            )
            return group_id

    def worker_sessions(self, run_id: str) -> list[dict[str, Any]]:
        """Return every distinct surfaced session with its latest Fleet assignment."""

        with self._connect() as connection:
            run = self._require_run(connection, run_id)
            worker_policy = _policy_worker_map(run["policy_json"])
            rows = connection.execute(
                """
                SELECT a.session_id, a.attempt_id, a.attempt_number, a.state AS attempt_state,
                    a.pid, a.actual_model, a.actual_effort,
                    l.leg_id, l.phase_index, l.phase, l.ordinal, l.runtime,
                    l.role, l.requested_model, l.requested_effort
                FROM fleet_attempts a
                JOIN fleet_legs l ON l.leg_id = a.leg_id
                WHERE l.run_id = ? AND a.session_id IS NOT NULL AND a.session_id != ''
                ORDER BY l.phase_index, l.ordinal, a.attempt_number
                """,
                (run_id,),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            sid = str(row["session_id"])
            policy_worker = worker_policy.get(
                (int(row["phase_index"]), int(row["ordinal"])),
                {},
            )
            worker_key = str(
                policy_worker.get("worker_key")
                or f"{row['runtime']}:{int(row['ordinal'])}"
            )
            latest[sid] = {
                "session_id": sid,
                "attempt_id": str(row["attempt_id"]),
                "attempt_number": int(row["attempt_number"]),
                "attempt_state": str(row["attempt_state"]),
                "pid": row["pid"],
                "actual_model": row["actual_model"],
                "actual_effort": row["actual_effort"],
                "leg_id": str(row["leg_id"]),
                "phase_index": int(row["phase_index"]),
                "phase": str(row["phase"]),
                "ordinal": int(row["ordinal"]),
                "runtime": str(row["runtime"]),
                "provider": str(row["runtime"]),
                "role": str(row["role"]),
                "model": str(row["requested_model"]),
                "effort": str(row["requested_effort"]),
                "worker_key": worker_key,
                "worker_label": str(policy_worker.get("worker_label") or worker_key),
                "assignment": str(policy_worker.get("assignment") or ""),
                "assignment_ids": list(policy_worker.get("assignment_ids") or ()),
                "review_target_ids": list(policy_worker.get("review_target_ids") or ()),
            }
        return list(latest.values())

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM fleet_events WHERE run_id = ? AND event_seq > ? "
                "ORDER BY event_seq LIMIT ?",
                (run_id, max(0, int(after)), min(2_000, max(1, int(limit)))),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def has_event(self, run_id: str, event_type: str, *, leg_id: str | None = None) -> bool:
        """Return whether a durable event already exists for this run or leg."""

        with self._connect() as connection:
            self._require_run(connection, run_id)
            if leg_id is None:
                row = connection.execute(
                    "SELECT 1 FROM fleet_events WHERE run_id = ? AND type = ? LIMIT 1",
                    (run_id, event_type),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT 1 FROM fleet_events WHERE run_id = ? AND leg_id = ? "
                    "AND type = ? LIMIT 1",
                    (run_id, leg_id, event_type),
                ).fetchone()
        return row is not None

    def terminal_notice_delivered(
        self,
        run_id: str,
        token: str,
        *,
        channel: str | None = None,
    ) -> bool:
        """Return whether one exact terminal transition reached one channel."""

        return self._terminal_notice_recorded(
            run_id,
            token,
            channel=channel,
            event_types=("run.notification.delivered",),
        )

    def terminal_notice_dispatched(
        self,
        run_id: str,
        token: str,
        *,
        channel: str,
    ) -> bool:
        """Return whether a transition was already queued to a live channel."""

        return self._terminal_notice_recorded(
            run_id,
            token,
            channel=channel,
            event_types=("run.notification.dispatched", "run.notification.delivered"),
        )

    def _terminal_notice_recorded(
        self,
        run_id: str,
        token: str,
        *,
        channel: str | None,
        event_types: tuple[str, ...],
    ) -> bool:
        placeholders = ",".join("?" for _event_type in event_types)

        with self._connect() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                "SELECT payload_json FROM fleet_events "
                f"WHERE run_id = ? AND type IN ({placeholders})",
                (run_id, *event_types),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            # The idempotency key lives under `notice_id`, not `token`. A field
            # named `token` is redacted on write by the secret filter, which
            # silently broke this comparison and re-sent Raghav the same
            # terminal alert on every pass and every service restart. The
            # filter is right; the field name was wrong.
            if (
                isinstance(payload, dict)
                and payload.get("notice_id") == token
                and (channel is None or payload.get("channel") == channel)
            ):
                return True
        return False

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        leg_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            self._require_run(connection, run_id)
            self._insert_event(
                connection,
                run_id=run_id,
                leg_id=leg_id,
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
            )
        with suppress(Exception):
            self.flush_control_outbox()

    def flush_control_outbox(
        self,
        control_store: object | None = None,
        *,
        limit: int = 250,
    ) -> int:
        """Idempotently publish committed Fleet events to Serena's shared plane."""

        from core.control_plane import ControlPlaneStore

        control_path: Path | None = None
        if (
            control_store is None
            and not os.environ.get("SERENA_CONTROL_PLANE_DB_PATH", "").strip()
            and self.path.resolve() != DEFAULT_DB_PATH.expanduser().resolve()
        ):
            control_path = self.path.with_name("control-plane.sqlite3")
        target = control_store or ControlPlaneStore(control_path)
        append = getattr(target, "append_envelope", None)
        if not callable(append):
            raise TypeError("control store must provide append_envelope")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_seq, envelope_json FROM fleet_control_outbox "
                "ORDER BY event_seq LIMIT ?",
                (min(1_000, max(1, int(limit))),),
            ).fetchall()
        flushed = 0
        for row in rows:
            try:
                envelope = json.loads(str(row["envelope_json"]))
                if not isinstance(envelope, dict):
                    raise ValueError("Fleet control envelope must be an object")
                append(envelope)
            except Exception:
                # Preserve ordering and the committed source event. A later
                # service pass can retry after the control plane is repaired.
                break
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM fleet_control_outbox WHERE event_seq = ?",
                    (int(row["event_seq"]),),
                )
            flushed += 1
        return flushed

    def pending_control_events(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM fleet_control_outbox"
            ).fetchone()
            return int(row["count"] or 0)

    def recover_stale_runs(self) -> list[str]:
        recovered: list[str] = []
        now = time.time()
        candidates: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fleet_runs WHERE state IN ('running', 'stopping')"
            ).fetchall()
            for row in rows:
                if _process_alive(row["owner_pid"], row["owner_token"]):
                    continue
                run_id = str(row["run_id"])
                attempts = connection.execute(
                    "SELECT a.pid, a.process_token FROM fleet_attempts a "
                    "JOIN fleet_legs l ON l.leg_id = a.leg_id "
                    "WHERE l.run_id = ? AND a.state = 'running' AND a.pid IS NOT NULL",
                    (run_id,),
                ).fetchall()
                candidates.append(
                    {
                        "run_id": run_id,
                        "owner_pid": row["owner_pid"],
                        "owner_token": row["owner_token"],
                        "attempts": [
                            (attempt["pid"], attempt["process_token"]) for attempt in attempts
                        ],
                    }
                )
        for candidate in candidates:
            candidate["workers_safe"] = True
            for pid, token in candidate["attempts"]:
                if not _terminate_owned_process(pid, token):
                    candidate["workers_safe"] = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                run_id = str(candidate["run_id"])
                row = self._require_run(connection, run_id)
                if row["state"] not in {"running", "stopping"}:
                    continue
                if _process_alive(row["owner_pid"], row["owner_token"]):
                    continue
                if (
                    row["owner_pid"] != candidate["owner_pid"]
                    or row["owner_token"] != candidate["owner_token"]
                ):
                    continue
                if not candidate["workers_safe"]:
                    self._insert_event(
                        connection,
                        run_id=run_id,
                        event_type="run.recovery_blocked",
                        payload={"reason": "worker ownership could not be verified"},
                    )
                    continue
                if row["state"] == "stopping" or row["cancel_requested"]:
                    next_state = "cancelled"
                    connection.execute(
                        "UPDATE fleet_legs SET state = 'cancelled', updated_at = ? "
                        "WHERE run_id = ? AND state != 'completed'",
                        (now, run_id),
                    )
                else:
                    next_state = "queued"
                    connection.execute(
                        "UPDATE fleet_legs SET state = 'queued', updated_at = ? "
                        "WHERE run_id = ? AND state = 'running'",
                        (now, run_id),
                    )
                connection.execute(
                    "UPDATE fleet_attempts SET state = 'interrupted', completed_at = ?, "
                    "pid = NULL, process_token = NULL, updated_at = ? WHERE state = 'running' "
                    "AND leg_id IN (SELECT leg_id FROM fleet_legs WHERE run_id = ?)",
                    (now, now, run_id),
                )
                connection.execute(
                    "UPDATE fleet_runs SET state = ?, owner_pid = NULL, owner_token = NULL, "
                    "completed_at = CASE WHEN ? = 'cancelled' THEN ? ELSE NULL END, "
                    "updated_at = ? WHERE run_id = ?",
                    (next_state, next_state, now, now, run_id),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run.recovered",
                    payload={"state": next_state},
                )
                recovered.append(run_id)
        return recovered

    def active_attempt_pids(self, run_id: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT pid FROM fleet_attempts WHERE state = 'running' AND pid IS NOT NULL "
                "AND leg_id IN (SELECT leg_id FROM fleet_legs WHERE run_id = ?)",
                (run_id,),
            ).fetchall()
            return [int(row["pid"]) for row in rows if int(row["pid"] or 0) > 0]

    def _finish_run(
        self,
        run_id: str,
        state: str,
        *,
        result_text: str = "",
        error: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        result = redact_text(result_text)[0][:MAX_OUTPUT_CHARS] or None
        clean_error = redact_text(error)[0].strip()[:4_000] or None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run(connection, run_id)
            if row["state"] in TERMINAL_RUN_STATES:
                return self._snapshot(connection, run_id)
            if state == "completed" and (
                bool(row["cancel_requested"]) or row["state"] == "stopping"
            ):
                state = "cancelled"
                result = None
                clean_error = "cancelled by user"
                connection.execute(
                    "UPDATE fleet_legs SET state = 'cancelled', updated_at = ? "
                    "WHERE run_id = ? AND state != 'completed'",
                    (now, run_id),
                )
            if state == "cancelled":
                cancel_unfinished_work_units(
                    connection,
                    run_id=run_id,
                    reason=clean_error or "cancelled by user",
                    now=now,
                )
            if state == "completed":
                incomplete = connection.execute(
                    "SELECT COUNT(*) AS count FROM fleet_legs "
                    "WHERE run_id = ? AND state != 'completed'",
                    (run_id,),
                ).fetchone()
                if int(incomplete["count"] or 0):
                    raise ValueError("cannot complete a Fleet run with unfinished legs")
            connection.execute(
                "DELETE FROM fleet_leg_retry_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_leg_handoff_requests WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM fleet_capacity_waits WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                """
                UPDATE fleet_runs SET state = ?, result_text = ?, error = ?,
                    cancel_requested = CASE WHEN ? = 'cancelled' THEN 1 ELSE cancel_requested END,
                    owner_pid = NULL, owner_token = NULL, completed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (state, result, clean_error, state, now, now, run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type=f"run.{state}",
                payload={"state": state, "error": clean_error},
            )
            return self._snapshot(connection, run_id)

    def _snapshot(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        run = connection.execute(
            """
            SELECT run_id, task, activity, cwd, origin_session_id, origin_agent,
                worker_group_id, state, current_phase, policy_json, dry_run,
                cancel_requested, substr(result_text, 1, ?) AS result_text,
                length(result_text) > ? AS result_truncated, error,
                created_at, updated_at, started_at, completed_at
            FROM fleet_runs WHERE run_id = ?
            """,
            (MAX_PUBLIC_PREVIEW_CHARS, MAX_PUBLIC_PREVIEW_CHARS, run_id),
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown Fleet run {run_id}")
        legs = connection.execute(
            "SELECT * FROM fleet_legs WHERE run_id = ? ORDER BY phase_index, ordinal",
            (run_id,),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT a.attempt_id, a.leg_id, a.attempt_number, a.state,
                a.requested_provider, a.requested_model, a.requested_effort,
                a.session_id, a.actual_model, a.actual_effort, a.pid,
                substr(a.output_text, 1, ?) AS output_text,
                length(a.output_text) > ? AS output_truncated,
                a.error, a.exit_code, a.event_log_path, a.started_at, a.completed_at
            FROM fleet_attempts a
            JOIN fleet_legs l ON l.leg_id = a.leg_id
            WHERE l.run_id = ? ORDER BY a.attempt_number
            """,
            (MAX_PUBLIC_PREVIEW_CHARS, MAX_PUBLIC_PREVIEW_CHARS, run_id),
        ).fetchall()
        context_rows = connection.execute(
            """
            SELECT c.* FROM fleet_context_receipts c
            JOIN fleet_attempts a ON a.attempt_id = c.attempt_id
            JOIN fleet_legs l ON l.leg_id = a.leg_id
            WHERE l.run_id = ?
            """,
            (run_id,),
        ).fetchall()
        context_by_attempt = {
            str(row["attempt_id"]): {
                "strategy": str(row["strategy"]),
                "source_chars": int(row["source_chars"]),
                "delivered_chars": int(row["delivered_chars"]),
                "omitted_chars": int(row["omitted_chars"]),
                "source_count": int(row["source_count"]),
                "redaction_count": int(row["redaction_count"]),
                "full_history_preserved": bool(row["full_history_preserved"]),
                "source_sha256": str(row["source_sha256"]),
                "budget_chars": int(row["budget_chars"]),
                "created_at": float(row["created_at"]),
            }
            for row in context_rows
        }
        attempts_by_leg: dict[str, list[sqlite3.Row]] = {}
        native_session_ids: set[str] = set()
        for attempt in attempts:
            attempts_by_leg.setdefault(str(attempt["leg_id"]), []).append(attempt)
            if attempt["session_id"]:
                native_session_ids.add(str(attempt["session_id"]))
        retry_rows = connection.execute(
            "SELECT leg_id FROM fleet_leg_retry_requests WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        retry_requested = {str(row["leg_id"]) for row in retry_rows}
        handoff_rows = connection.execute(
            "SELECT leg_id, target_provider, reason, automatic, requested_at "
            "FROM fleet_leg_handoff_requests WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        handoff_requested = {str(row["leg_id"]): row for row in handoff_rows}
        capacity_rows = connection.execute(
            "SELECT leg_id, failed_provider, eligible_providers_json, reason, "
            "not_before, resets_at, requested_at, updated_at "
            "FROM fleet_capacity_waits WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        capacity_waiting: dict[str, dict[str, Any]] = {}
        for row in capacity_rows:
            try:
                eligible = json.loads(str(row["eligible_providers_json"] or "[]"))
            except json.JSONDecodeError:
                eligible = []
            capacity_waiting[str(row["leg_id"])] = {
                "leg_id": str(row["leg_id"]),
                "failed_provider": str(row["failed_provider"]),
                "eligible_providers": [
                    str(provider)
                    for provider in eligible
                    if str(provider) in {"codex", "claude"}
                ],
                "reason": str(row["reason"] or ""),
                "not_before": float(row["not_before"]),
                "resets_at": (
                    float(row["resets_at"]) if row["resets_at"] is not None else None
                ),
                "requested_at": float(row["requested_at"]),
                "updated_at": float(row["updated_at"]),
            }
        try:
            policy = json.loads(run["policy_json"])
        except (TypeError, json.JSONDecodeError):
            policy = {}
        worker_policy = _policy_worker_map(policy)
        phases: list[dict[str, Any]] = []
        phase_map: dict[int, dict[str, Any]] = {}
        completed = 0
        worker_slots: set[str] = set()
        for leg in legs:
            index = int(leg["phase_index"])
            policy_worker = worker_policy.get((index, int(leg["ordinal"])), {})
            worker_key = str(
                policy_worker.get("worker_key")
                or f"{leg['runtime']}:{int(leg['ordinal'])}"
            )
            phase = phase_map.get(index)
            if phase is None:
                phase = {
                    "index": index,
                    "name": str(leg["phase"]),
                    "execution": str(leg["execution"]),
                    "state": "queued",
                    "legs": [],
                }
                phase_map[index] = phase
                phases.append(phase)
            leg_attempts = attempts_by_leg.get(str(leg["leg_id"]), [])
            current = leg_attempts[-1] if leg_attempts else None
            current_attempt = self._attempt_dict(current) if current is not None else None
            if current_attempt is not None:
                current_attempt["context_receipt"] = context_by_attempt.get(
                    str(current_attempt["attempt_id"])
                )
            leg_dict = {
                "leg_id": str(leg["leg_id"]),
                "ordinal": int(leg["ordinal"]),
                "runtime": str(leg["runtime"]),
                "provider": str(leg["runtime"]),
                "role": str(leg["role"]),
                "model": str(leg["requested_model"]),
                "effort": str(leg["requested_effort"]),
                "access_mode": str(leg["access_mode"]),
                "worker_key": worker_key,
                "worker_label": str(policy_worker.get("worker_label") or worker_key),
                "assignment": str(policy_worker.get("assignment") or ""),
                "assignment_ids": list(policy_worker.get("assignment_ids") or ()),
                "review_target_ids": list(policy_worker.get("review_target_ids") or ()),
                "state": str(leg["state"]),
                "attempt_count": len(leg_attempts),
                "current_attempt": current_attempt,
                "retry_requested": str(leg["leg_id"]) in retry_requested,
                "handoff_requested": str(leg["leg_id"]) in handoff_requested,
                "capacity_wait": capacity_waiting.get(str(leg["leg_id"])),
            }
            pending_handoff = handoff_requested.get(str(leg["leg_id"]))
            if pending_handoff is not None:
                leg_dict["handoff_target_provider"] = str(
                    pending_handoff["target_provider"]
                )
                leg_dict["handoff_reason"] = str(pending_handoff["reason"] or "")
                leg_dict["handoff_automatic"] = bool(pending_handoff["automatic"])
                leg_dict["handoff_requested_at"] = float(pending_handoff["requested_at"])
            phase["legs"].append(leg_dict)
            worker_slots.add(worker_key)
            completed += int(leg["state"] == "completed")
        for phase in phases:
            phase["state"] = _phase_state([leg["state"] for leg in phase["legs"]])
        policy_phases = policy.get("phases") if isinstance(policy, dict) else None
        if isinstance(policy_phases, list):
            display_by_index = {
                int(item["index"]): str(item.get("display_name") or item.get("name") or "")
                for item in policy_phases
                if isinstance(item, dict) and "index" in item
            }
            for phase in phases:
                phase["display_name"] = display_by_index.get(
                    int(phase["index"]), str(phase["name"])
                )
        current_phase = str(run["current_phase"] or "")
        current_phase_display = next(
            (
                str(phase.get("display_name") or phase["name"])
                for phase in phases
                if phase["name"] == current_phase
            ),
            current_phase or None,
        )
        work_units = project_work_unit_run(connection, run_id)
        if not work_units:
            work_units = derive_work_unit_views(policy, phases, str(run["state"]))
        return {
            "run_id": str(run["run_id"]),
            "task": str(run["task"]),
            "activity": str(run["activity"]),
            "cwd": str(run["cwd"]),
            "origin_session_id": run["origin_session_id"],
            "origin_agent": run["origin_agent"],
            "worker_group_id": run["worker_group_id"],
            "state": str(run["state"]),
            "current_phase": run["current_phase"],
            "current_phase_display": current_phase_display,
            "dry_run": bool(run["dry_run"]),
            "cancel_requested": bool(run["cancel_requested"]),
            "policy": policy,
            "agent_count": len(worker_slots),
            "chat_count": len(native_session_ids),
            "progress": {"completed": completed, "total": len(legs)},
            "phases": phases,
            "work_units": work_units,
            "capacity_waits": list(capacity_waiting.values()),
            "result_text": run["result_text"],
            "result_truncated": bool(run["result_truncated"]),
            "error": run["error"],
            "created_at": float(run["created_at"]),
            "updated_at": float(run["updated_at"]),
            "started_at": float(run["started_at"]) if run["started_at"] else None,
            "completed_at": float(run["completed_at"]) if run["completed_at"] else None,
        }

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "number": int(row["attempt_number"]),
            "state": str(row["state"]),
            "requested_provider": row["requested_provider"],
            "requested_model": row["requested_model"],
            "requested_effort": row["requested_effort"],
            "session_id": row["session_id"],
            "actual_model": row["actual_model"],
            "actual_effort": row["actual_effort"],
            "pid": row["pid"],
            "output_text": row["output_text"],
            "output_truncated": bool(row["output_truncated"]),
            "error": row["error"],
            "exit_code": row["exit_code"],
            "event_log_path": row["event_log_path"],
            "started_at": float(row["started_at"]) if row["started_at"] else None,
            "completed_at": float(row["completed_at"]) if row["completed_at"] else None,
        }

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return {
            "event_seq": int(row["event_seq"]),
            "run_id": str(row["run_id"]),
            "leg_id": row["leg_id"],
            "attempt_id": row["attempt_id"],
            "type": str(row["type"]),
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        leg_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        safe_payload, _redaction_count = redact_value(payload)
        encoded = json.dumps(
            safe_payload,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        if len(encoded) > MAX_EVENT_CHARS:
            encoded = json.dumps({"truncated": True, "preview": encoded[:MAX_EVENT_CHARS]})
        created_at = time.time()
        inserted = connection.execute(
            "INSERT INTO fleet_events(run_id, leg_id, attempt_id, type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, leg_id, attempt_id, event_type, encoded, created_at),
        )
        event_seq = int(inserted.lastrowid)
        run = connection.execute(
            "SELECT task, activity, origin_session_id, origin_agent FROM fleet_runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        leg = (
            connection.execute(
                "SELECT runtime FROM fleet_legs WHERE leg_id = ?", (leg_id,)
            ).fetchone()
            if leg_id
            else None
        )
        try:
            source_payload = json.loads(encoded)
        except json.JSONDecodeError:
            source_payload = {"preview": encoded}
        envelope_payload = dict(source_payload) if isinstance(source_payload, dict) else {}
        envelope_payload.update(
            {
                "fleet_event_seq": event_seq,
                "leg_id": leg_id,
                "attempt_id": attempt_id,
                "activity": str(run["activity"]) if run is not None else None,
            }
        )
        if event_type == "run.created" and run is not None:
            envelope_payload["summary"] = str(run["task"] or "")[:4_000]
        if event_type == "run.notification.delivered":
            delivery_state = "delivered"
        elif event_type == "run.notification.failed":
            delivery_state = "failed"
        elif event_type == "run.notification.dispatched":
            delivery_state = "pending"
        else:
            delivery_state = "not_applicable"
        lifecycle_state = str(envelope_payload.get("state") or event_type.rsplit(".", 1)[-1])
        envelope = {
            "event_id": f"fleet:{run_id}:{event_seq}",
            "surface": "fleet",
            "event_type": event_type,
            "session_id": (run["origin_session_id"] if run is not None else None),
            "turn_id": None,
            "request_id": None,
            "job_id": run_id,
            "provider": (str(leg["runtime"]) if leg is not None else None),
            "authority": "user_delegated",
            "lifecycle_state": lifecycle_state[:64] or "unknown",
            "delivery_state": delivery_state,
            "payload": envelope_payload,
            "occurred_at": created_at,
        }
        connection.execute(
            "INSERT INTO fleet_control_outbox(event_seq, envelope_json, created_at) "
            "VALUES (?, ?, ?)",
            (
                event_seq,
                json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str),
                created_at,
            ),
        )

    @staticmethod
    def _require_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM fleet_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown Fleet run {run_id}")
        return row

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    task TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    origin_session_id TEXT,
                    origin_agent TEXT,
                    worker_group_id TEXT,
                    state TEXT NOT NULL,
                    current_phase TEXT,
                    policy_json TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    owner_pid INTEGER,
                    owner_token TEXT,
                    result_text TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS fleet_runs_state_idx
                    ON fleet_runs(state, created_at);

                CREATE TABLE IF NOT EXISTS fleet_legs (
                    leg_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    phase_index INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    execution TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    runtime TEXT NOT NULL,
                    role TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    requested_effort TEXT NOT NULL,
                    access_mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(run_id, phase_index, ordinal)
                );
                CREATE INDEX IF NOT EXISTS fleet_legs_run_idx
                    ON fleet_legs(run_id, phase_index, ordinal);

                CREATE TABLE IF NOT EXISTS fleet_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    leg_id TEXT NOT NULL REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    requested_provider TEXT,
                    requested_model TEXT,
                    requested_effort TEXT,
                    session_id TEXT,
                    actual_model TEXT,
                    actual_effort TEXT,
                    pid INTEGER,
                    process_token TEXT,
                    output_text TEXT,
                    error TEXT,
                    exit_code INTEGER,
                    event_log_path TEXT,
                    started_at REAL,
                    completed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(leg_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS fleet_attempts_leg_idx
                    ON fleet_attempts(leg_id, attempt_number);

                CREATE TABLE IF NOT EXISTS fleet_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    leg_id TEXT,
                    attempt_id TEXT,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fleet_events_run_idx
                    ON fleet_events(run_id, event_seq);

                CREATE TABLE IF NOT EXISTS fleet_control_outbox (
                    event_seq INTEGER PRIMARY KEY REFERENCES fleet_events(event_seq) ON DELETE CASCADE,
                    envelope_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fleet_steering (
                    steering_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fleet_leg_retry_requests (
                    leg_id TEXT PRIMARY KEY REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    requested_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fleet_leg_retry_run_idx
                    ON fleet_leg_retry_requests(run_id, requested_at);

                CREATE TABLE IF NOT EXISTS fleet_leg_handoff_requests (
                    leg_id TEXT PRIMARY KEY REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    target_provider TEXT NOT NULL,
                    reason TEXT,
                    automatic INTEGER NOT NULL DEFAULT 0,
                    requested_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fleet_leg_handoff_run_idx
                    ON fleet_leg_handoff_requests(run_id, requested_at);

                CREATE TABLE IF NOT EXISTS fleet_capacity_waits (
                    leg_id TEXT PRIMARY KEY REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    failed_provider TEXT NOT NULL,
                    eligible_providers_json TEXT NOT NULL,
                    reason TEXT,
                    not_before REAL NOT NULL,
                    resets_at REAL,
                    requested_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fleet_capacity_waits_run_idx
                    ON fleet_capacity_waits(run_id, not_before);

                CREATE TABLE IF NOT EXISTS fleet_context_receipts (
                    attempt_id TEXT PRIMARY KEY
                        REFERENCES fleet_attempts(attempt_id) ON DELETE CASCADE,
                    strategy TEXT NOT NULL,
                    source_chars INTEGER NOT NULL,
                    delivered_chars INTEGER NOT NULL,
                    omitted_chars INTEGER NOT NULL,
                    source_count INTEGER NOT NULL,
                    redaction_count INTEGER NOT NULL,
                    full_history_preserved INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    budget_chars INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            ensure_work_unit_schema(connection)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(fleet_attempts)").fetchall()
            }
            for name in ("requested_provider", "requested_model", "requested_effort"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE fleet_attempts ADD COLUMN {name} TEXT")
            connection.execute(
                """
                UPDATE fleet_attempts
                SET requested_provider = COALESCE(
                        requested_provider,
                        (SELECT l.runtime FROM fleet_legs l WHERE l.leg_id = fleet_attempts.leg_id)
                    ),
                    requested_model = COALESCE(
                        requested_model,
                        (SELECT l.requested_model FROM fleet_legs l WHERE l.leg_id = fleet_attempts.leg_id)
                    ),
                    requested_effort = COALESCE(
                        requested_effort,
                        (SELECT l.requested_effort FROM fleet_legs l WHERE l.leg_id = fleet_attempts.leg_id)
                    )
                WHERE requested_provider IS NULL
                   OR requested_model IS NULL
                   OR requested_effort IS NULL
                """
            )
            backfill_work_unit_runs(connection)
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _phase_state(states: list[str]) -> str:
    if not states:
        return "queued"
    if all(state == "completed" for state in states):
        return "completed"
    if any(state == "running" for state in states):
        return "running"
    if any(state == "waiting_for_capacity" for state in states):
        return "waiting_for_capacity"
    if any(state == "failed" for state in states):
        return "failed"
    if any(state == "cancelled" for state in states):
        return "cancelled"
    if all(state == "planned" for state in states):
        return "planned"
    return "queued"


def _new_worker_group_id() -> str:
    return "g_" + uuid.uuid4().hex[:12]


def _policy_worker_map(policy_json: object) -> dict[tuple[int, int], dict[str, Any]]:
    """Index optional snapshotted worker identity without migrating legacy rows."""

    if isinstance(policy_json, dict):
        policy = policy_json
    else:
        try:
            policy = json.loads(str(policy_json or "{}"))
        except json.JSONDecodeError:
            return {}
    phases = policy.get("phases") if isinstance(policy, dict) else None
    if not isinstance(phases, list):
        return {}
    indexed: dict[tuple[int, int], dict[str, Any]] = {}
    for fallback_index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            continue
        try:
            phase_index = int(phase.get("index", fallback_index))
        except (TypeError, ValueError):
            continue
        workers = phase.get("workers")
        if not isinstance(workers, list):
            continue
        for ordinal, worker in enumerate(workers):
            if isinstance(worker, dict):
                indexed[(phase_index, ordinal)] = worker
    return indexed


def _session_mode(policy_json: object) -> str:
    try:
        policy = json.loads(str(policy_json or "{}"))
    except json.JSONDecodeError:
        return "per_leg"
    value = str(policy.get("session_mode") or "per_leg") if isinstance(policy, dict) else "per_leg"
    return value if value in {"per_leg", "persistent_by_worker"} else "per_leg"


def _current_phase_display(policy_json: object, current_phase: object) -> str | None:
    current = str(current_phase or "")
    if not current:
        return None
    try:
        policy = json.loads(str(policy_json or "{}"))
    except json.JSONDecodeError:
        return current
    phases = policy.get("phases") if isinstance(policy, dict) else None
    if not isinstance(phases, list):
        return current
    for phase in phases:
        if isinstance(phase, dict) and str(phase.get("name") or "") == current:
            return str(phase.get("display_name") or current)
    return current


def _process_alive(pid: object, token: object = None) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    expected = token if isinstance(token, str) and token else None
    if expected is None or expected == f"pid:{pid}":
        return True
    current = process_start_token(pid)
    return isinstance(current, str) and hmac.compare_digest(current, expected)


def _terminate_owned_process(pid: object, token: object) -> bool:
    """Terminate only a worker whose persisted birth token still matches."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return True
    if not _pid_exists(pid):
        return True
    if pid == os.getpid():
        return False
    expected = token if isinstance(token, str) and token and not token.startswith("pid:") else None
    if expected is None:
        return False
    current = process_start_token(pid)
    if isinstance(current, str) and not hmac.compare_digest(current, expected):
        return True
    if not isinstance(current, str):
        return False
    try:
        if os.name != "nt":
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_alive(pid, expected):
            return True
        time.sleep(0.05)
    if _process_alive(pid, expected):
        with suppress(ProcessLookupError, PermissionError, OSError):
            if os.name != "nt":
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
    return not _process_alive(pid, expected)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
