"""Durable per-worker leases, liveness, stalls, retries, and recovery fences."""

from __future__ import annotations

import hmac
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fleet.store import _terminate_owned_process
from core.work_jobs import process_start_token

DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_STALL_SECONDS = 45.0 * 60.0
DEFAULT_MAX_RETRIES = 2

LEASE_STATES = frozenset(
    {"active", "stalled", "retry_scheduled", "completed", "failed", "cancelled", "expired"}
)


@dataclass(frozen=True, slots=True)
class WorkerLease:
    attempt_id: str
    run_id: str
    leg_id: str
    lease_token: str
    generation: int
    owner_pid: int
    owner_token: str
    state: str
    heartbeat_at: float
    progress_at: float
    lease_expires_at: float
    stall_after_seconds: float
    retries_used: int
    max_retries: int
    recovery_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FleetSupervisionStore:
    """Attempt leases colocated transactionally with Fleet's authoritative DB."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._initialize()

    def acquire(
        self,
        attempt_id: str,
        *,
        owner_pid: int | None = None,
        now: float | None = None,
        lease_seconds: float | None = None,
        stall_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> WorkerLease:
        at = time.time() if now is None else float(now)
        pid = int(owner_pid or os.getpid())
        birth = process_start_token(pid) or f"pid:{pid}"
        lease_for = _positive(lease_seconds, "SERENA_FLEET_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        stall_for = _positive(stall_seconds, "SERENA_FLEET_STALL_SECONDS", DEFAULT_STALL_SECONDS)
        retry_limit = _nonnegative_int(
            max_retries,
            "SERENA_FLEET_MAX_STALL_RETRIES",
            DEFAULT_MAX_RETRIES,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                """
                SELECT a.attempt_id, a.state AS attempt_state, l.leg_id, l.run_id
                FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id = a.leg_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(f"unknown Fleet attempt {attempt_id}")
            if str(attempt["attempt_state"]) != "running":
                raise RuntimeError("a worker lease requires a running Fleet attempt")
            leg_id = str(attempt["leg_id"])
            run_id = str(attempt["run_id"])
            active = connection.execute(
                """
                SELECT w.*, a.pid AS attempt_pid, a.process_token AS attempt_process_token,
                    a.state AS attempt_state
                FROM fleet_worker_leases w
                JOIN fleet_attempts a ON a.attempt_id = w.attempt_id
                WHERE w.leg_id = ? AND w.attempt_id != ?
                    AND w.state IN ('active', 'stalled', 'expired')
                ORDER BY w.generation DESC LIMIT 1
                """,
                (leg_id, attempt_id),
            ).fetchone()
            if active is not None:
                if (
                    str(active["attempt_state"]) == "running"
                    and float(active["lease_expires_at"] or 0) > at
                ):
                    raise RuntimeError("Fleet worker already has a live lease")
                self._fence_for_reassignment(connection, active, at)
            generation_row = connection.execute(
                "SELECT MAX(generation) AS generation FROM fleet_worker_leases WHERE leg_id = ?",
                (leg_id,),
            ).fetchone()
            generation = int(generation_row["generation"] or 0) + 1
            token = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO fleet_worker_retry_state(
                    leg_id, run_id, retries_used, max_retries, updated_at
                ) VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(leg_id) DO UPDATE SET
                    max_retries = excluded.max_retries,
                    updated_at = excluded.updated_at
                """,
                (leg_id, run_id, retry_limit, at),
            )
            connection.execute(
                """
                INSERT INTO fleet_worker_leases(
                    attempt_id, run_id, leg_id, lease_token, generation,
                    owner_pid, owner_token, state, acquired_at, heartbeat_at,
                    progress_at, lease_expires_at, stall_after_seconds,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    run_id,
                    leg_id,
                    token,
                    generation,
                    pid,
                    birth,
                    at,
                    at,
                    at,
                    at + lease_for,
                    stall_for,
                    at,
                    at,
                ),
            )
            return self._lease(connection, attempt_id)

    def fence_expired_leg(self, leg_id: str, *, now: float | None = None) -> bool:
        """Stop and fence an expired provider before a replacement attempt exists."""

        at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT w.*, a.pid AS attempt_pid, a.process_token AS attempt_process_token,
                    a.state AS attempt_state
                FROM fleet_worker_leases w
                JOIN fleet_attempts a ON a.attempt_id = w.attempt_id
                WHERE w.leg_id = ? AND w.state IN ('active', 'stalled', 'expired')
                ORDER BY w.generation DESC LIMIT 1
                """,
                (str(leg_id),),
            ).fetchone()
            if active is None:
                return False
            if (
                str(active["attempt_state"]) == "running"
                and float(active["lease_expires_at"] or 0) > at
            ):
                return False
            self._fence_for_reassignment(connection, active, at)
            return True

    @staticmethod
    def _fence_for_reassignment(
        connection: sqlite3.Connection,
        active: sqlite3.Row,
        at: float,
    ) -> None:
        if active["attempt_pid"] is not None and not _terminate_owned_process(
            active["attempt_pid"], active["attempt_process_token"]
        ):
            raise RuntimeError(
                "expired Fleet worker could not be safely terminated; reassignment refused"
            )
        connection.execute(
            """
            UPDATE fleet_worker_leases
            SET state = 'expired', recovery_reason = 'lease expired before reassignment',
                released_at = ?, updated_at = ?
            WHERE attempt_id = ? AND state IN ('active', 'stalled', 'expired')
            """,
            (at, at, str(active["attempt_id"])),
        )
        connection.execute(
            """
            UPDATE fleet_attempts
            SET state = 'interrupted', error = 'worker lease expired',
                completed_at = ?, updated_at = ?
            WHERE attempt_id = ? AND state = 'running'
            """,
            (at, at, str(active["attempt_id"])),
        )

    def heartbeat(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        progress: bool = False,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        at = time.time() if now is None else float(now)
        lease_for = _positive(lease_seconds, "SERENA_FLEET_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM fleet_worker_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "active":
                return False
            if not hmac.compare_digest(str(row["lease_token"]), str(lease_token)):
                return False
            # A delayed heartbeat can be caused by system suspend or scheduler
            # starvation. The current token still owns the lease until an
            # explicit reassignment fences it, so let that owner recover. This
            # remains race-safe because a reassignment changes the lease state
            # before the old token can renew it.
            updated = connection.execute(
                """
                UPDATE fleet_worker_leases
                SET heartbeat_at = ?, progress_at = CASE WHEN ? THEN ? ELSE progress_at END,
                    lease_expires_at = ?, updated_at = ?
                WHERE attempt_id = ? AND lease_token = ? AND state = 'active'
                """,
                (at, int(progress), at, at + lease_for, at, attempt_id, lease_token),
            ).rowcount
            return bool(updated)

    def bind_process(self, attempt_id: str, lease_token: str, pid: int) -> bool:
        """Bind a fenced lease to the provider process once it has spawned."""

        worker_pid = int(pid)
        if worker_pid < 1:
            raise ValueError("Fleet worker pid must be positive")
        owner_token = process_start_token(worker_pid) or f"pid:{worker_pid}"
        now = time.time()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE fleet_worker_leases
                SET owner_pid = ?, owner_token = ?, updated_at = ?
                WHERE attempt_id = ? AND lease_token = ? AND state = 'active'
                """,
                (worker_pid, owner_token, now, attempt_id, lease_token),
            ).rowcount
            return bool(updated)

    def mark_stalled(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM fleet_worker_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "active":
                return str(row["state"]) == "stalled" if row is not None else False
            if not hmac.compare_digest(str(row["lease_token"]), str(lease_token)):
                return False
            if at - float(row["progress_at"]) < float(row["stall_after_seconds"]):
                return False
            reason = (
                f"no worker progress for {float(row['stall_after_seconds']):.0f} seconds"
            )
            connection.execute(
                """
                UPDATE fleet_worker_leases
                SET state = 'stalled', stalled_at = ?, recovery_reason = ?, updated_at = ?
                WHERE attempt_id = ? AND lease_token = ? AND state = 'active'
                """,
                (at, reason, at, attempt_id, lease_token),
            )
            return True

    def owns(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        del now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM fleet_worker_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return not (
                row is None
                or str(row["state"]) not in {"active", "stalled"}
                or not hmac.compare_digest(str(row["lease_token"]), str(lease_token))
            )

    @staticmethod
    def _expire(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        at: float,
        *,
        reason: str,
    ) -> None:
        connection.execute(
            """
            UPDATE fleet_worker_leases
            SET state = 'expired', recovery_reason = ?, released_at = ?, updated_at = ?
            WHERE attempt_id = ? AND state IN ('active', 'stalled')
            """,
            (reason, at, at, str(row["attempt_id"])),
        )
        connection.execute(
            """
            UPDATE fleet_attempts
            SET state = 'interrupted', error = ?, completed_at = ?, updated_at = ?
            WHERE attempt_id = ? AND state = 'running'
            """,
            (reason, at, at, str(row["attempt_id"])),
        )

    def release(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        state: str,
        reason: str = "",
        now: float | None = None,
    ) -> bool:
        clean_state = str(state or "").strip().lower()
        if clean_state not in LEASE_STATES - {"active", "stalled", "retry_scheduled"}:
            raise ValueError("invalid terminal worker lease state")
        at = time.time() if now is None else float(now)
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE fleet_worker_leases
                SET state = ?, recovery_reason = CASE WHEN ? != '' THEN ? ELSE recovery_reason END,
                    released_at = ?, updated_at = ?
                WHERE attempt_id = ? AND lease_token = ? AND state IN ('active', 'stalled')
                """,
                (clean_state, reason[:2_000], reason[:2_000], at, at, attempt_id, lease_token),
            ).rowcount
            return bool(updated)

    def schedule_stall_retry(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Queue a bounded retry after the stalled attempt is durably interrupted."""

        at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM fleet_worker_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if lease is None or not hmac.compare_digest(
                str(lease["lease_token"]), str(lease_token)
            ):
                return {"scheduled": False, "reason": "worker lease was fenced"}
            if str(lease["state"]) != "stalled":
                return {"scheduled": False, "reason": "worker was not marked stalled"}
            leg_id = str(lease["leg_id"])
            retry = connection.execute(
                "SELECT * FROM fleet_worker_retry_state WHERE leg_id = ?", (leg_id,)
            ).fetchone()
            used = int(retry["retries_used"] or 0) if retry is not None else 0
            limit = int(retry["max_retries"] or 0) if retry is not None else 0
            if used >= limit:
                connection.execute(
                    """
                    UPDATE fleet_worker_leases
                    SET state = 'failed', recovery_reason = 'stall retry budget exhausted',
                        released_at = ?, updated_at = ? WHERE attempt_id = ?
                    """,
                    (at, at, attempt_id),
                )
                return {
                    "scheduled": False,
                    "reason": "stall retry budget exhausted",
                    "retries_used": used,
                    "max_retries": limit,
                }
            used += 1
            connection.execute(
                """
                UPDATE fleet_worker_retry_state
                SET retries_used = ?, last_failure_kind = 'stalled',
                    last_attempt_id = ?, updated_at = ? WHERE leg_id = ?
                """,
                (used, attempt_id, at, leg_id),
            )
            connection.execute(
                """
                UPDATE fleet_worker_leases
                SET state = 'retry_scheduled', released_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (at, at, attempt_id),
            )
            connection.execute(
                "UPDATE fleet_legs SET state = 'queued', updated_at = ? WHERE leg_id = ?",
                (at, leg_id),
            )
            connection.execute(
                """
                UPDATE fleet_work_unit_phases
                SET state = 'queued', failure_reason = NULL, completed_at = NULL, updated_at = ?
                WHERE leg_id = ? AND attempt_id = ?
                """,
                (at, leg_id, attempt_id),
            )
            connection.execute(
                """
                UPDATE fleet_work_units
                SET state = 'queued', failure_reason = NULL, completed_at = NULL, updated_at = ?
                WHERE (run_id, unit_id) IN (
                    SELECT run_id, unit_id FROM fleet_work_unit_phases WHERE leg_id = ?
                )
                """,
                (at, leg_id),
            )
            return {
                "scheduled": True,
                "reason": "worker stalled",
                "retries_used": used,
                "max_retries": limit,
                "leg_id": leg_id,
            }

    def reconcile_run(self, run_id: str, *, now: float | None = None) -> int:
        """Close leases whose attempts were recovered by Fleet's native journal."""

        at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT w.attempt_id, a.state AS attempt_state
                FROM fleet_worker_leases w
                JOIN fleet_attempts a ON a.attempt_id = w.attempt_id
                WHERE w.run_id = ? AND w.state IN ('active', 'stalled')
                    AND a.state != 'running'
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                state = "expired" if str(row["attempt_state"]) == "interrupted" else str(
                    row["attempt_state"]
                )
                connection.execute(
                    """
                    UPDATE fleet_worker_leases
                    SET state = ?, recovery_reason = 'reconciled from durable attempt state',
                        released_at = ?, updated_at = ? WHERE attempt_id = ?
                    """,
                    (state, at, at, str(row["attempt_id"])),
                )
            return len(rows)

    def project_run(self, run_id: str, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT w.*, COALESCE(r.retries_used, 0) AS retries_used,
                    COALESCE(r.max_retries, 0) AS max_retries
                FROM fleet_worker_leases w
                LEFT JOIN fleet_worker_retry_state r ON r.leg_id = w.leg_id
                WHERE w.run_id = ?
                ORDER BY w.generation DESC
                """,
                (run_id,),
            ).fetchall()
        latest: dict[str, WorkerLease] = {}
        for row in rows:
            leg_id = str(row["leg_id"])
            if leg_id not in latest:
                latest[leg_id] = _lease_from_row(row)
        workers = []
        for lease in latest.values():
            item = lease.to_dict()
            item["heartbeat_age_seconds"] = max(0.0, current - lease.heartbeat_at)
            item["progress_age_seconds"] = max(0.0, current - lease.progress_at)
            item["lease_expired"] = lease.lease_expires_at <= current
            workers.append(item)
        workers.sort(key=lambda item: (item["leg_id"], -int(item["generation"])))
        return {
            "workers": workers,
            "active": sum(item["state"] == "active" for item in workers),
            "stalled": sum(item["state"] == "stalled" for item in workers),
            "retry_scheduled": sum(item["state"] == "retry_scheduled" for item in workers),
        }

    def _lease(self, connection: sqlite3.Connection, attempt_id: str) -> WorkerLease:
        row = connection.execute(
            """
            SELECT w.*, COALESCE(r.retries_used, 0) AS retries_used,
                COALESCE(r.max_retries, 0) AS max_retries
            FROM fleet_worker_leases w
            LEFT JOIN fleet_worker_retry_state r ON r.leg_id = w.leg_id
            WHERE w.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Fleet worker lease {attempt_id}")
        return _lease_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_worker_leases (
                    attempt_id TEXT PRIMARY KEY
                        REFERENCES fleet_attempts(attempt_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    leg_id TEXT NOT NULL REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    lease_token TEXT NOT NULL UNIQUE,
                    generation INTEGER NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    progress_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    stall_after_seconds REAL NOT NULL,
                    stalled_at REAL,
                    released_at REAL,
                    recovery_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(leg_id, generation)
                );
                CREATE INDEX IF NOT EXISTS fleet_worker_leases_run_idx
                    ON fleet_worker_leases(run_id, state, lease_expires_at);
                CREATE TABLE IF NOT EXISTS fleet_worker_retry_state (
                    leg_id TEXT PRIMARY KEY REFERENCES fleet_legs(leg_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES fleet_runs(run_id) ON DELETE CASCADE,
                    retries_used INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL,
                    last_failure_kind TEXT,
                    last_attempt_id TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )


class WorkerLeaseMonitor:
    """Renew a lease independently while the provider stream is quiet."""

    def __init__(
        self,
        store: FleetSupervisionStore,
        lease: WorkerLease,
        *,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.lease = lease
        self.heartbeat_seconds = _positive(
            heartbeat_seconds,
            "SERENA_FLEET_HEARTBEAT_SECONDS",
            DEFAULT_HEARTBEAT_SECONDS,
        )
        self._stop = threading.Event()
        self._stalled = threading.Event()
        self._fenced = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"fleet-heartbeat-{lease.attempt_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def progress(self) -> bool:
        renewed = self.store.heartbeat(
            self.lease.attempt_id,
            self.lease.lease_token,
            progress=True,
        )
        if not renewed:
            self._fenced.set()
        return renewed

    def should_cancel(self) -> bool:
        return self._stalled.is_set() or self._fenced.is_set()

    @property
    def stalled(self) -> bool:
        return self._stalled.is_set()

    @property
    def fenced(self) -> bool:
        return self._fenced.is_set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            renewed = self.store.heartbeat(
                self.lease.attempt_id,
                self.lease.lease_token,
            )
            if not renewed:
                self._fenced.set()
                return
            if self.store.mark_stalled(self.lease.attempt_id, self.lease.lease_token):
                self._stalled.set()
                return


def _lease_from_row(row: sqlite3.Row) -> WorkerLease:
    return WorkerLease(
        attempt_id=str(row["attempt_id"]),
        run_id=str(row["run_id"]),
        leg_id=str(row["leg_id"]),
        lease_token=str(row["lease_token"]),
        generation=int(row["generation"]),
        owner_pid=int(row["owner_pid"]),
        owner_token=str(row["owner_token"]),
        state=str(row["state"]),
        heartbeat_at=float(row["heartbeat_at"]),
        progress_at=float(row["progress_at"]),
        lease_expires_at=float(row["lease_expires_at"]),
        stall_after_seconds=float(row["stall_after_seconds"]),
        retries_used=int(row["retries_used"] or 0),
        max_retries=int(row["max_retries"] or 0),
        recovery_reason=str(row["recovery_reason"] or ""),
    )


def _positive(explicit: float | None, name: str, default: float) -> float:
    raw: object = explicit if explicit is not None else os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.05, value)


def _nonnegative_int(explicit: int | None, name: str, default: int) -> int:
    raw: object = explicit if explicit is not None else os.environ.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(20, max(0, value))
