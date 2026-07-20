"""Durable, idempotent jobs and replayable events for Serena work surfaces."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

DEFAULT_JOBS_PATH = Path.home() / ".local" / "state" / "serena" / "work_jobs.sqlite3"
TERMINAL_STATES = frozenset({"artifact_ready", "failed", "cancelled"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class WorkJob:
    job_id: str
    idempotency_key: str
    call_id: str
    turn_id: str
    request: str
    state: str
    created_at: float
    updated_at: float
    origin_session_id: str | None = None
    worker_session_id: str | None = None
    worker_pid: int | None = None
    worker_token: str | None = None
    billing_evidence: dict[str, Any] | None = None
    artifact_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WorkJobEvent:
    event_seq: int
    job_id: str
    call_id: str
    type: str
    payload: dict[str, Any]

    def control(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "event_seq": self.event_seq,
            "job_id": self.job_id,
            "call_id": self.call_id,
            **self.payload,
        }


class WorkJobStore:
    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_WORK_JOBS_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_JOBS_PATH).expanduser()
        self._initialize()

    def create_draft_link(
        self,
        *,
        call_id: str,
        turn_id: str,
        request: str,
    ) -> tuple[WorkJob, bool]:
        idempotency_key = f"voice:{call_id}:{turn_id}:draft_link"
        now = time.time()
        job_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM work_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._job(existing), False
            connection.execute(
                """
                INSERT INTO work_jobs(
                    job_id, idempotency_key, call_id, turn_id, request,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (job_id, idempotency_key, call_id, turn_id, request, now, now),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                call_id=call_id,
                event_type="job.accepted",
                payload={"state": "accepted"},
            )
            row = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._job(row), True

    def get(self, job_id: str) -> WorkJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job(row) if row is not None else None

    def jobs_for_call(self, call_id: str) -> list[WorkJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_jobs WHERE call_id = ? ORDER BY created_at",
                (call_id,),
            ).fetchall()
        return [self._job(row) for row in rows]

    def recoverable(self) -> list[WorkJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_jobs WHERE state IN ('accepted', 'running') ORDER BY created_at"
            ).fetchall()
        jobs = [self._job(row) for row in rows]
        return [
            job
            for job in jobs
            if job.state == "accepted" or not _process_alive(job.worker_pid, job.worker_token)
        ]

    def mark_running(
        self,
        job_id: str,
        worker_session_id: str,
        worker_pid: int | None = None,
    ) -> WorkJob | None:
        """Atomically reserve a job before a gated worker is allowed to start."""

        owner_pid = int(worker_pid or os.getpid())
        owner_token = _process_token(owner_pid)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown job {job_id}")
            state = str(row["state"])
            if state not in {"accepted", "running"}:
                return None
            if state == "running" and _process_alive(row["worker_pid"], row["worker_token"]):
                return None
            now = time.time()
            connection.execute(
                "UPDATE work_jobs SET state = 'running', updated_at = ?, "
                "worker_session_id = ?, worker_pid = ?, worker_token = ? "
                "WHERE job_id = ?",
                (now, worker_session_id, owner_pid, owner_token, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                call_id=str(row["call_id"]),
                event_type="job.progress",
                payload={"state": "running", "message": "drafting"},
            )
            updated = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            return self._job(updated)

    def adopt_worker(
        self,
        job_id: str,
        *,
        worker_session_id: str,
        owner_pid: int,
        worker_pid: int,
    ) -> bool:
        """Move a reservation to the gated child before that child can exec."""

        worker_token = _process_token(worker_pid)
        if worker_token is None or not _process_alive(worker_pid, worker_token):
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, worker_session_id, worker_pid, worker_token "
                "FROM work_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["worker_session_id"] != worker_session_id
                or row["worker_pid"] != owner_pid
            ):
                return False
            connection.execute(
                "UPDATE work_jobs SET worker_pid = ?, worker_token = ?, "
                "billing_evidence_json = NULL, updated_at = ? WHERE job_id = ?",
                (worker_pid, worker_token, time.time(), job_id),
            )
        return True

    def record_worker_billing(
        self,
        job_id: str,
        *,
        worker_session_id: str,
        worker_pid: int,
        evidence: dict[str, Any],
    ) -> bool:
        """Persist the adopted gate's attestation before it can launch Claude."""

        metered = evidence.get("metered_auth_env_present")
        captured_at = evidence.get("captured_at")
        worker_token = evidence.get("worker_token")
        command_hash = evidence.get("command_sha256")
        environment_hash = evidence.get("environment_sha256")
        stage = evidence.get("stage")
        exec_pid = evidence.get("exec_pid")
        exec_token = evidence.get("exec_token")
        containment = evidence.get("containment")
        if (
            evidence.get("schema_version") != 3
            or evidence.get("ok") is not True
            or evidence.get("logged_in") is not True
            or evidence.get("auth_method") != "claude.ai"
            or evidence.get("api_provider") != "firstParty"
            or evidence.get("subscription_type") != "max"
            or evidence.get("api_key_present") is not False
            or metered != []
            or evidence.get("auth_mode") != "subscription_oauth_guarded"
            or evidence.get("setting_sources") != []
            or evidence.get("gate_pid") != worker_pid
            or evidence.get("worker_session_id") != worker_session_id
            or not isinstance(worker_token, str)
            or not worker_token
            or not isinstance(command_hash, str)
            or _SHA256.fullmatch(command_hash) is None
            or not isinstance(environment_hash, str)
            or _SHA256.fullmatch(environment_hash) is None
            or not isinstance(captured_at, (int, float))
            or isinstance(captured_at, bool)
            or not math.isfinite(float(captured_at))
            or float(captured_at) <= 0
            or stage not in {"authenticated", "exec_ready"}
        ):
            raise ValueError("worker subscription billing evidence is unsafe")
        if stage == "exec_ready" and (
            not isinstance(exec_pid, int)
            or isinstance(exec_pid, bool)
            or exec_pid < 1
            or not isinstance(exec_token, str)
            or not exec_token
            or containment not in {"linux_pdeathsig", "windows_kill_on_close_job"}
            or not _process_alive(exec_pid, exec_token)
        ):
            raise ValueError("worker exec containment evidence is unsafe")
        safe = {
            "schema_version": 3,
            "ok": True,
            "captured_at": float(captured_at),
            "auth_mode": "subscription_oauth_guarded",
            "logged_in": True,
            "auth_method": "claude.ai",
            "api_provider": "firstParty",
            "subscription_type": "max",
            "api_key_present": False,
            "metered_auth_env_present": [],
            "setting_sources": [],
            "gate_pid": worker_pid,
            "worker_token": worker_token,
            "worker_session_id": worker_session_id,
            "command_sha256": command_hash,
            "environment_sha256": environment_hash,
            "stage": stage,
        }
        if stage == "exec_ready":
            safe.update(
                {
                    "exec_pid": exec_pid,
                    "exec_token": exec_token,
                    "containment": containment,
                }
            )
        encoded = json.dumps(safe, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, worker_session_id, worker_pid, worker_token "
                "FROM work_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["worker_session_id"] != worker_session_id
                or row["worker_pid"] != worker_pid
                or not isinstance(row["worker_token"], str)
                or not hmac.compare_digest(row["worker_token"], worker_token)
                or not _process_alive(worker_pid, worker_token)
            ):
                return False
            connection.execute(
                "UPDATE work_jobs SET billing_evidence_json = ?, updated_at = ? WHERE job_id = ?",
                (encoded, time.time(), job_id),
            )
        return True

    def mark_artifact_ready(
        self,
        job_id: str,
        *,
        artifact_id: str,
        name: str,
        url: str,
        sha256: str,
        size: int,
        worker_session_id: str | None,
    ) -> WorkJob:
        return self._transition(
            job_id,
            state="artifact_ready",
            event_type="artifact.ready",
            payload={
                "state": "artifact_ready",
                "artifact_id": artifact_id,
                "name": name,
                "url": url,
                "sha256": sha256,
                "size": size,
            },
            worker_session_id=worker_session_id,
            artifact_id=artifact_id,
            allow_from={"accepted", "running"},
        )

    def mark_failed(self, job_id: str, error: str) -> WorkJob:
        clean = str(error).strip()[:500] or "draft job failed"
        return self._transition(
            job_id,
            state="failed",
            event_type="job.failed",
            payload={"state": "failed", "error": clean},
            error=clean,
            allow_from={"accepted", "running"},
        )

    def link_origin_session(self, job_id: str, session_id: str) -> None:
        clean = session_id.strip()
        if not clean:
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE work_jobs SET origin_session_id = ?, updated_at = ? WHERE job_id = ?",
                (clean, time.time(), job_id),
            )

    def events_for_call(self, call_id: str, *, after: int = 0) -> list[WorkJobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_seq, job_id, call_id, type, payload_json "
                "FROM work_job_events WHERE call_id = ? AND event_seq > ? "
                "ORDER BY event_seq",
                (call_id, max(0, int(after))),
            ).fetchall()
        events: list[WorkJobEvent] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            events.append(
                WorkJobEvent(
                    event_seq=int(row["event_seq"]),
                    job_id=str(row["job_id"]),
                    call_id=str(row["call_id"]),
                    type=str(row["type"]),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        return events

    def event_for_call(self, call_id: str, event_seq: int) -> WorkJobEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_seq, job_id, call_id, type, payload_json "
                "FROM work_job_events WHERE call_id = ? AND event_seq = ?",
                (call_id, int(event_seq)),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        return WorkJobEvent(
            event_seq=int(row["event_seq"]),
            job_id=str(row["job_id"]),
            call_id=str(row["call_id"]),
            type=str(row["type"]),
            payload=payload if isinstance(payload, dict) else {},
        )

    def _transition(
        self,
        job_id: str,
        *,
        state: str,
        event_type: str,
        payload: dict[str, Any],
        allow_from: set[str],
        worker_session_id: str | None = None,
        artifact_id: str | None = None,
        error: str | None = None,
    ) -> WorkJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown job {job_id}")
            current = str(row["state"])
            if current == state:
                return self._job(row)
            if current not in allow_from:
                raise ValueError(f"cannot move job from {current} to {state}")
            now = time.time()
            connection.execute(
                """
                UPDATE work_jobs SET state = ?, updated_at = ?,
                    worker_session_id = COALESCE(?, worker_session_id),
                    worker_pid = NULL, worker_token = NULL,
                    artifact_id = COALESCE(?, artifact_id),
                    error = COALESCE(?, error)
                WHERE job_id = ?
                """,
                (state, now, worker_session_id, artifact_id, error, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                call_id=str(row["call_id"]),
                event_type=event_type,
                payload=payload,
            )
            updated = connection.execute(
                "SELECT * FROM work_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            return self._job(updated)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        call_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO work_job_events(job_id, call_id, type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                job_id,
                call_id,
                event_type,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                time.time(),
            ),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> WorkJob:
        return WorkJob(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            call_id=str(row["call_id"]),
            turn_id=str(row["turn_id"]),
            request=str(row["request"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            origin_session_id=row["origin_session_id"],
            worker_session_id=row["worker_session_id"],
            worker_pid=row["worker_pid"],
            worker_token=row["worker_token"],
            billing_evidence=WorkJobStore._billing_evidence(row),
            artifact_id=row["artifact_id"],
            error=row["error"],
        )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    call_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    origin_session_id TEXT,
                    worker_session_id TEXT,
                    worker_pid INTEGER,
                    worker_token TEXT,
                    billing_evidence_json TEXT,
                    artifact_id TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS work_jobs_call_idx
                    ON work_jobs(call_id, created_at);
                CREATE TABLE IF NOT EXISTS work_job_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES work_jobs(job_id),
                    call_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_job_events_call_idx
                    ON work_job_events(call_id, event_seq);
                """
            )
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(work_jobs)")
            }
            if "worker_pid" not in columns:
                connection.execute("ALTER TABLE work_jobs ADD COLUMN worker_pid INTEGER")
            if "worker_token" not in columns:
                connection.execute("ALTER TABLE work_jobs ADD COLUMN worker_token TEXT")
            if "billing_evidence_json" not in columns:
                connection.execute("ALTER TABLE work_jobs ADD COLUMN billing_evidence_json TEXT")
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)

    @staticmethod
    def _billing_evidence(row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            payload = json.loads(row["billing_evidence_json"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def _process_alive(value: object, token: object = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    expected = token if isinstance(token, str) and token else None
    current = _process_token(value)
    return expected is None or (isinstance(current, str) and hmac.compare_digest(current, expected))


def process_start_token(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        start_time = fields[19]
    except (OSError, IndexError, ValueError):
        if Path("/proc").is_dir():
            return None
        try:
            return f"psutil:{psutil.Process(pid).create_time():.6f}"
        except (OSError, psutil.Error, ValueError):
            return None
    return f"linux:{start_time}"


_process_token = process_start_token
