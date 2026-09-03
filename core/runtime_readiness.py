"""Explicit runtime readiness, bounded recovery, and durable resumption.

The resident brain, voice loop, tool boundary, primary UI, and coding worker
remain separate processes. This module does not start or stop any of them by
itself. It gives their existing supervisors one small contract for reporting
health and injects every recovery action, which makes recovery bounded and
fully fakeable in tests.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_DB = Path.home() / ".local/state/serena/runtime-readiness.sqlite3"
REQUIRED_COMPONENTS = ("brain", "voice", "tool_boundary", "primary_ui", "coding_jobs")
MAX_DETAIL_CHARS = 1_000
MAX_UNFINISHED_PAYLOAD_CHARS = 32_000
MAX_SOAK_SECONDS = 3_600.0
MAX_SOAK_SAMPLES = 10_000
RUNTIME_SCHEMA_VERSION = 2
DEFAULT_WORK_LEASE_SECONDS = 300.0


class RuntimeState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    RECOVERING = "recovering"


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    name: str
    available: bool
    critical: bool = True
    ready: bool = True
    detail: str = ""
    checked_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("dependency name must be 1 to 128 characters")
        if len(self.detail) > MAX_DETAIL_CHARS:
            raise ValueError("dependency detail is too long")
        if self.ready and not self.available:
            raise ValueError("an unavailable dependency cannot be ready")

    @classmethod
    def healthy(cls, name: str, *, critical: bool = True, now: float | None = None):
        return cls(name, True, critical, True, "", time.time() if now is None else now)

    @classmethod
    def unavailable(
        cls,
        name: str,
        detail: str,
        *,
        critical: bool = True,
        now: float | None = None,
    ):
        return cls(
            name,
            False,
            critical,
            False,
            str(detail)[:MAX_DETAIL_CHARS],
            time.time() if now is None else now,
        )


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: RuntimeState
    dependencies: tuple[DependencyStatus, ...]
    checked_at: float
    reason: str = ""
    recovery_attempted: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == RuntimeState.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.ready,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "recovery_attempted": list(self.recovery_attempted),
            "dependencies": [asdict(item) for item in self.dependencies],
        }


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    max_attempts: int = 3
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not 0 <= self.cooldown_seconds <= 86_400:
            raise ValueError("cooldown_seconds must be between 0 and 86400")


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    component: str
    attempted: bool
    recovered: bool
    attempts: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    before: RuntimeSnapshot
    after: RuntimeSnapshot
    attempts: tuple[RecoveryAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class UnfinishedWork:
    work_id: str
    kind: str
    payload: dict[str, Any]
    state: str
    attempts: int
    created_at: float
    updated_at: float
    lease_owner: str = ""
    lease_expires_at: float = 0.0


class RuntimeLedger:
    """SQLite evidence for recovery budgets and unfinished local work."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DEFAULT_RUNTIME_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)

    def _initialize(self) -> None:
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > RUNTIME_SCHEMA_VERSION:
                raise RuntimeError(
                    "runtime ledger schema is newer than this Serena runtime supports"
                )
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runtime_recovery (
                    component TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_allowed_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_unfinished_work (
                    work_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','working','completed','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runtime_unfinished_work)")
            }
            if "lease_owner" not in columns:
                connection.execute(
                    "ALTER TABLE runtime_unfinished_work "
                    "ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''"
                )
            if "lease_expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE runtime_unfinished_work "
                    "ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0"
                )
            connection.execute(f"PRAGMA user_version={RUNTIME_SCHEMA_VERSION}")

    def recovery_state(self, component: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts, next_allowed_at, last_error, updated_at "
                "FROM runtime_recovery WHERE component = ?",
                (_component(component),),
            ).fetchone()
        if row is None:
            return {"attempts": 0, "next_allowed_at": 0.0, "last_error": "", "updated_at": 0.0}
        return {
            "attempts": int(row["attempts"]),
            "next_allowed_at": float(row["next_allowed_at"]),
            "last_error": str(row["last_error"]),
            "updated_at": float(row["updated_at"]),
        }

    def record_recovery(
        self,
        component: str,
        *,
        success: bool,
        detail: str,
        policy: RecoveryPolicy,
        now: float,
    ) -> int:
        name = _component(component)
        current = self.recovery_state(name)
        attempts = 0 if success else int(current["attempts"]) + 1
        next_allowed = 0.0 if success else now + policy.cooldown_seconds
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runtime_recovery(component, attempts, next_allowed_at, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(component) DO UPDATE SET "
                "attempts=excluded.attempts, next_allowed_at=excluded.next_allowed_at, "
                "last_error=excluded.last_error, updated_at=excluded.updated_at",
                (name, attempts, next_allowed, str(detail)[:MAX_DETAIL_CHARS], now),
            )
        return attempts

    def reset_recovery(self, component: str, *, now: float) -> None:
        self.record_recovery(
            component,
            success=True,
            detail="",
            policy=RecoveryPolicy(),
            now=now,
        )

    def queue_work(self, kind: str, payload: Mapping[str, Any], *, now: float | None = None) -> str:
        clean_kind = _component(kind)
        body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        if len(body) > MAX_UNFINISHED_PAYLOAD_CHARS:
            raise ValueError("unfinished work payload is too large")
        work_id = str(uuid.uuid4())
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runtime_unfinished_work("
                "work_id, kind, payload_json, state, attempts, created_at, updated_at"
                ") VALUES (?, ?, ?, 'queued', 0, ?, ?)",
                (work_id, clean_kind, body, moment, moment),
            )
        return work_id

    def resumable_work(
        self, *, limit: int = 50, now: float | None = None
    ) -> list[UnfinishedWork]:
        """Return queued work and working rows whose durable lease expired."""

        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_unfinished_work WHERE state='queued' "
                "OR (state='working' AND lease_expires_at <= ?) "
                "ORDER BY created_at, work_id LIMIT ?",
                (moment, max(1, min(int(limit), 500))),
            ).fetchall()
        return [_work_from_row(row) for row in rows]

    def resume(
        self,
        handlers: Mapping[str, Callable[[dict[str, Any]], bool]],
        *,
        max_attempts: int = 3,
        limit: int = 50,
        now: float | None = None,
        worker_id: str = "",
        lease_seconds: float = DEFAULT_WORK_LEASE_SECONDS,
    ) -> dict[str, list[str]]:
        """Claim and replay unfinished work through explicit idempotent handlers."""

        if not 1 <= int(max_attempts) <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        lease = float(lease_seconds)
        if not 1 <= lease <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        moment = time.time() if now is None else float(now)
        owner = _component(worker_id or f"runtime_{uuid.uuid4().hex}")
        result: dict[str, list[str]] = {
            "completed": [],
            "queued": [],
            "failed": [],
            "unhandled": [],
            "skipped": [],
        }
        for item in self.resumable_work(limit=limit, now=moment):
            handler = handlers.get(item.kind)
            if handler is None:
                result["unhandled"].append(item.work_id)
                continue
            claimed = self._claim_work(
                item.work_id,
                owner=owner,
                now=moment,
                lease_expires_at=moment + lease,
            )
            if claimed is None:
                result["skipped"].append(item.work_id)
                continue
            try:
                completed = bool(handler(dict(claimed.payload)))
            except Exception:
                completed = False
            attempts = claimed.attempts + 1
            state = (
                "completed" if completed else ("failed" if attempts >= max_attempts else "queued")
            )
            with self._connect() as connection:
                changed = connection.execute(
                    "UPDATE runtime_unfinished_work SET state=?, attempts=?, updated_at=?, "
                    "lease_owner='', lease_expires_at=0 WHERE work_id=? AND state='working' "
                    "AND lease_owner=?",
                    (state, attempts, moment, claimed.work_id, owner),
                ).rowcount
            if changed:
                result[state].append(claimed.work_id)
            else:
                result["skipped"].append(claimed.work_id)
        return result

    def _claim_work(
        self,
        work_id: str,
        *,
        owner: str,
        now: float,
        lease_expires_at: float,
    ) -> UnfinishedWork | None:
        """Atomically lease one row so concurrent supervisors cannot run it twice."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE runtime_unfinished_work SET state='working', lease_owner=?, "
                "lease_expires_at=?, updated_at=? WHERE work_id=? AND "
                "(state='queued' OR (state='working' AND lease_expires_at <= ?))",
                (owner, lease_expires_at, now, str(work_id), now),
            ).rowcount
            if not changed:
                return None
            row = connection.execute(
                "SELECT * FROM runtime_unfinished_work WHERE work_id=?", (str(work_id),)
            ).fetchone()
        return _work_from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection


Probe = Callable[[], DependencyStatus]
Recoverer = Callable[[], bool]


class RuntimeCoordinator:
    """Aggregate probes and apply a durable retry budget to recovery."""

    def __init__(
        self,
        probes: Mapping[str, Probe],
        *,
        recoverers: Mapping[str, Recoverer] | None = None,
        ledger: RuntimeLedger | None = None,
        policy: RecoveryPolicy | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        missing = sorted(set(REQUIRED_COMPONENTS) - set(probes))
        if missing:
            raise ValueError("runtime probes missing: " + ", ".join(missing))
        self.probes = dict(probes)
        self.recoverers = dict(recoverers or {})
        self.ledger = ledger or RuntimeLedger()
        self.policy = policy or RecoveryPolicy()
        self.now = now

    def snapshot(self, *, recovering: tuple[str, ...] = ()) -> RuntimeSnapshot:
        moment = self.now()
        statuses: list[DependencyStatus] = []
        for name in REQUIRED_COMPONENTS:
            try:
                status = self.probes[name]()
                if status.name != name:
                    raise ValueError(f"probe {name} returned status for {status.name}")
            except Exception as error:
                status = DependencyStatus.unavailable(
                    name,
                    f"probe failed: {type(error).__name__}: {error}",
                    now=moment,
                )
            statuses.append(status)
        state, reason = _runtime_state(statuses)
        if recovering and state != RuntimeState.READY:
            state = RuntimeState.RECOVERING
            reason = "bounded recovery started for " + ", ".join(recovering)
        return RuntimeSnapshot(state, tuple(statuses), moment, reason, recovering)

    def reconcile(self) -> ReconcileResult:
        before = self.snapshot()
        moment = self.now()
        attempts: list[RecoveryAttempt] = []
        started: list[str] = []
        for dependency in before.dependencies:
            if dependency.available and dependency.ready:
                self.ledger.reset_recovery(dependency.name, now=moment)
                continue
            recoverer = self.recoverers.get(dependency.name)
            state = self.ledger.recovery_state(dependency.name)
            attempts_used = int(state["attempts"])
            if recoverer is None:
                attempts.append(
                    RecoveryAttempt(
                        dependency.name, False, False, attempts_used, "no recoverer registered"
                    )
                )
                continue
            if attempts_used >= self.policy.max_attempts:
                attempts.append(
                    RecoveryAttempt(
                        dependency.name, False, False, attempts_used, "attempt budget exhausted"
                    )
                )
                continue
            if moment < float(state["next_allowed_at"]):
                attempts.append(
                    RecoveryAttempt(dependency.name, False, False, attempts_used, "cooldown active")
                )
                continue
            try:
                recovered = bool(recoverer())
                detail = (
                    "recoverer accepted the request"
                    if recovered
                    else "recoverer reported no recovery"
                )
            except Exception as error:
                recovered = False
                detail = f"recoverer failed: {type(error).__name__}: {error}"
            count = self.ledger.record_recovery(
                dependency.name,
                success=False,
                detail=detail,
                policy=self.policy,
                now=moment,
            )
            if recovered:
                started.append(dependency.name)
            attempts.append(RecoveryAttempt(dependency.name, True, recovered, count, detail))
        after = self.snapshot(recovering=tuple(started))
        for dependency in after.dependencies:
            if dependency.available and dependency.ready:
                self.ledger.reset_recovery(dependency.name, now=moment)
        return ReconcileResult(before, after, tuple(attempts))


@dataclass(frozen=True, slots=True)
class SoakReport:
    duration_seconds: float
    samples: int
    states: dict[str, int]
    transitions: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def run_short_soak(
    probe: Callable[[], RuntimeSnapshot],
    *,
    duration_seconds: float = 300.0,
    interval_seconds: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SoakReport:
    """Sample readiness for at most one hour. A 24-hour run is impossible."""

    duration = float(duration_seconds)
    interval = float(interval_seconds)
    if not 0 <= duration <= MAX_SOAK_SECONDS:
        raise ValueError("short soak duration must be between 0 and 3600 seconds")
    if interval <= 0:
        raise ValueError("short soak interval must be positive")
    started = clock()
    states: dict[str, int] = {}
    transitions: list[tuple[str, str]] = []
    previous = ""
    samples = 0
    while samples < MAX_SOAK_SAMPLES:
        current = probe().state.value
        states[current] = states.get(current, 0) + 1
        samples += 1
        if previous and current != previous:
            transitions.append((previous, current))
        previous = current
        elapsed = max(0.0, clock() - started)
        if elapsed >= duration:
            break
        sleep(min(interval, duration - elapsed))
    return SoakReport(max(0.0, clock() - started), samples, states, tuple(transitions))


def _runtime_state(statuses: list[DependencyStatus]) -> tuple[RuntimeState, str]:
    failed_critical = [item.name for item in statuses if item.critical and not item.ready]
    failed_optional = [item.name for item in statuses if not item.critical and not item.ready]
    if failed_critical:
        return RuntimeState.OFFLINE, "critical dependencies unavailable: " + ", ".join(
            failed_critical
        )
    if failed_optional:
        return RuntimeState.DEGRADED, "optional dependencies unavailable: " + ", ".join(
            failed_optional
        )
    return RuntimeState.READY, "all runtime dependencies are ready"


def _component(value: object) -> str:
    clean = str(value or "").strip().lower().replace("-", "_")
    if not clean or len(clean) > 128 or not all(char.isalnum() or char == "_" for char in clean):
        raise ValueError("component must be a short identifier")
    return clean


def _work_from_row(row: sqlite3.Row) -> UnfinishedWork:
    return UnfinishedWork(
        work_id=str(row["work_id"]),
        kind=str(row["kind"]),
        payload=json.loads(str(row["payload_json"])),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        lease_owner=str(row["lease_owner"] or ""),
        lease_expires_at=float(row["lease_expires_at"] or 0.0),
    )
