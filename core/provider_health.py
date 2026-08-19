"""Full, degraded, or offline: what Serena can still do when providers go dark.

`core.fleet_capacity` already answers "is Claude out, is Codex out". That is a
per-provider fact. This turns those facts into one system-wide mode, states
plainly what survives in each mode, and keeps the work that could not run so it
can run for real when a subscription comes back.

Three modes, and the boundaries are literal rather than vibes:

full      at least one cloud subscription has capacity.
degraded  no cloud capacity, but a local model is loaded and answering.
offline   no cloud capacity and no local model. Serena is her local data only.

The point of naming offline separately is that it is still useful. Memory
recall, briefings, the commitment list, and safe local actions are sqlite and
string formatting; none of them ever needed a model. So offline mode does not
mean "down", it means "no new reasoning", and the capability matrix below says
exactly which of those two it is instead of leaving him to guess.

Deferred work is kept here rather than in the control plane's obligation ledger
on purpose. An obligation is something Serena owes Raghav and must redeliver; a
deferred turn is work that never started because there was nothing to run it on.
Those have different lifecycles and different resolution rules, and collapsing
them would make "I owe you an answer" and "I could not begin" the same row.

Nothing in this module ever says a cloud model produced a local answer.
`describe` reports the actual selected provider, the actual model, and the
actual reason for any fallback.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "continuity.sqlite3"

FULL = "full"
DEGRADED = "degraded"
OFFLINE = "offline"
MODES = (FULL, DEGRADED, OFFLINE)

CLOUD_PROVIDERS = ("claude", "codex")

# What still works, per mode. Everything true in offline is local by
# construction: sqlite, files, and formatting. Nothing here is aspirational.
CAPABILITY_MATRIX: dict[str, dict[str, bool]] = {
    FULL: {
        "memory_recall": True,
        "briefings": True,
        "queued_work": True,
        "safe_local_actions": True,
        "local_reasoning": True,
        "frontier_reasoning": True,
        "coding_jobs": True,
    },
    DEGRADED: {
        "memory_recall": True,
        "briefings": True,
        "queued_work": True,
        "safe_local_actions": True,
        "local_reasoning": True,
        # A 14b model does not review a diff or run a Fleet leg, and saying it
        # can is how a job hangs for an hour producing nothing.
        "frontier_reasoning": False,
        "coding_jobs": False,
    },
    OFFLINE: {
        "memory_recall": True,
        "briefings": True,
        "queued_work": True,
        "safe_local_actions": True,
        "local_reasoning": False,
        "frontier_reasoning": False,
        "coding_jobs": False,
    },
}

# Past this, retrying deferred work is a loop rather than recovery.
DEFAULT_MAX_ATTEMPTS = 5
MAX_QUEUE_ROWS = 1_000
MAX_PAYLOAD_CHARS = 16_000


class ContinuityError(ValueError):
    """The continuity request was not valid."""


@dataclass(frozen=True, slots=True)
class ProviderReading:
    provider: str
    usable: bool
    status: str
    reason: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContinuityState:
    """One assessment: the mode, why, and what may be attempted in it."""

    mode: str
    assessed_at: float
    cloud: tuple[ProviderReading, ...] = ()
    selected_provider: str = ""
    selected_model: str = ""
    fallback_reason: str = ""
    local_available: bool = False
    local_reason: str = ""
    capabilities: Mapping[str, bool] = field(default_factory=dict)

    def allows(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability, False))

    @property
    def usable_cloud(self) -> tuple[str, ...]:
        return tuple(reading.provider for reading in self.cloud if reading.usable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "assessed_at": self.assessed_at,
            "cloud": [reading.to_dict() for reading in self.cloud],
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "fallback_reason": self.fallback_reason,
            "local_available": self.local_available,
            "local_reason": self.local_reason,
            "capabilities": dict(self.capabilities),
            "usable_cloud": list(self.usable_cloud),
            "banner": describe(self),
        }


def _reading(capacity: Mapping[str, Any] | None, provider: str) -> ProviderReading:
    entry: Any = None
    if capacity is not None:
        with suppress(Exception):
            entry = capacity.get(provider)
    if entry is None:
        # Matches fleet_capacity's own rule: unknown is not the same as out.
        return ProviderReading(
            provider=provider,
            usable=True,
            status="unknown",
            reason="no capacity reading; treating unknown as usable",
        )
    if hasattr(entry, "to_dict"):
        entry = entry.to_dict()
    data = dict(entry or {})
    status = str(data.get("status") or "unknown")
    usable = data.get("usable")
    if usable is None:
        usable = status != "unavailable"
    return ProviderReading(
        provider=provider,
        usable=bool(usable),
        status=status,
        reason=str(data.get("reason") or ""),
        source=str(data.get("source") or ""),
    )


def assess_continuity(
    capacity: Mapping[str, Any] | None = None,
    *,
    local: tuple[Any, Any] | None = None,
    preferred_provider: str = "claude",
    cloud_model_for: Callable[[str], str] | None = None,
    now: float | None = None,
    probe_local: bool = True,
) -> ContinuityState:
    """Read capacity and the local endpoint, and decide what mode we are in.

    `local` may be handed in as the `(profile, status)` pair that
    `local_model_fallback.local_status` returns, which is what tests and any
    caller that already probed should do. Left out, it probes once. Set
    `probe_local=False` to skip probing entirely, which is the right choice when
    cloud is healthy and there is no reason to touch the GPU.
    """

    moment = float(time.time() if now is None else now)
    if capacity is None:
        with suppress(Exception):
            from core.fleet_capacity import read_fleet_capacity

            capacity = read_fleet_capacity()
    readings = tuple(_reading(capacity, provider) for provider in CLOUD_PROVIDERS)
    usable = [reading for reading in readings if reading.usable]

    if usable:
        preferred = str(preferred_provider or "claude").strip().lower()
        chosen = next(
            (reading for reading in usable if reading.provider == preferred), usable[0]
        )
        fallback_reason = ""
        if chosen.provider != preferred:
            blocked = next(
                (reading for reading in readings if reading.provider == preferred), None
            )
            fallback_reason = (
                f"{preferred} is out of capacity"
                + (f" ({blocked.reason})" if blocked and blocked.reason else "")
            )
        model = ""
        if cloud_model_for is not None:
            with suppress(Exception):
                model = str(cloud_model_for(chosen.provider) or "")
        return ContinuityState(
            mode=FULL,
            assessed_at=moment,
            cloud=readings,
            selected_provider=chosen.provider,
            selected_model=model,
            fallback_reason=fallback_reason,
            local_available=False,
            local_reason="not needed while a subscription has capacity",
            capabilities=dict(CAPABILITY_MATRIX[FULL]),
        )

    exhausted = "; ".join(
        f"{reading.provider}: {reading.reason or reading.status}" for reading in readings
    )
    profile = None
    status = None
    if local is not None:
        profile, status = local
    elif probe_local:
        with suppress(Exception):
            from core.local_model_fallback import local_status

            profile, status = local_status(now=moment)

    local_ok = bool(status is not None and getattr(status, "available", False))
    local_reason = str(getattr(status, "reason", "") or "no local model configured")

    if local_ok and profile is not None:
        return ContinuityState(
            mode=DEGRADED,
            assessed_at=moment,
            cloud=readings,
            selected_provider="local",
            selected_model=str(getattr(profile, "model_id", "")),
            fallback_reason=f"both subscriptions are out ({exhausted})",
            local_available=True,
            local_reason=local_reason,
            capabilities=dict(CAPABILITY_MATRIX[DEGRADED]),
        )

    return ContinuityState(
        mode=OFFLINE,
        assessed_at=moment,
        cloud=readings,
        selected_provider="",
        selected_model="",
        fallback_reason=f"both subscriptions are out ({exhausted}); {local_reason}",
        local_available=False,
        local_reason=local_reason,
        capabilities=dict(CAPABILITY_MATRIX[OFFLINE]),
    )


def _local_pair(state: ContinuityState, kwargs: Mapping[str, Any]) -> tuple[Any, Any]:
    """The (profile, status) an explicit local request should actually be judged on."""

    supplied = kwargs.get("local")
    if supplied is not None:
        return supplied
    if state.mode != FULL:
        # The assessment already probed, so reuse it rather than hit the GPU twice.
        return None, LocalModelStatusView(state.local_available, state.local_reason)
    if kwargs.get("probe_local") is False:
        return None, LocalModelStatusView(False, "the local endpoint was not probed")
    with suppress(Exception):
        from core.local_model_fallback import local_status

        return local_status(now=state.assessed_at)
    return None, LocalModelStatusView(False, "the local endpoint could not be read")


@dataclass(frozen=True, slots=True)
class LocalModelStatusView:
    """The two fields a routing decision needs, without importing the endpoint."""

    available: bool
    reason: str


@dataclass(frozen=True, slots=True)
class BrainRouting:
    """What the resident brain should actually do with the next turn."""

    provider: str
    model: str
    mode: str
    reason: str
    should_queue: bool

    @property
    def is_local(self) -> bool:
        return self.provider == "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "reason": self.reason,
            "should_queue": self.should_queue,
            "is_local": self.is_local,
        }


def route_brain_turn(
    capacity: Mapping[str, Any] | None = None,
    *,
    override: str | None = None,
    state: ContinuityState | None = None,
    **kwargs: Any,
) -> BrainRouting:
    """Decide provider, model, and whether the turn has to be queued instead.

    This is the whole integration surface for the resident brain. The existing
    `core.brain_provider.choose_brain_provider` knows only `claude` and `codex`
    and raises `BrainProviderUnavailable` when both are out, which is correct
    for a chooser that has no third option but is exactly the moment continuity
    is supposed to take over. Rather than fork that chooser, this returns the
    same decision extended with `local` and with the honest instruction to
    queue when nothing can answer at all.

    `override` is honored the way the environment variable already is, with one
    refusal: asking for `local` when no local model is loaded returns a queue
    instruction rather than pretending, because a named model that is not
    running is not a provider.
    """

    resolved = state if state is not None else assess_continuity(capacity, **kwargs)
    wanted = str(override or "").strip().lower()
    if wanted == "local":
        # `assess_continuity` short-circuits to full mode without ever looking at
        # the GPU, so its `local_*` fields say "not needed" rather than anything
        # about the weights. Asking for local explicitly has to consult the
        # local endpoint itself, or the refusal would quote a reason that was
        # never about the local model at all.
        profile, status = _local_pair(resolved, kwargs)
        if status is not None and getattr(status, "available", False):
            return BrainRouting(
                provider="local",
                model=str(getattr(profile, "model_id", "") or resolved.selected_model),
                mode=DEGRADED if resolved.mode != FULL else FULL,
                reason="local model requested explicitly",
                should_queue=False,
            )
        reason = str(getattr(status, "reason", "") or "no local model is loaded")
        return BrainRouting(
            provider="",
            model="",
            mode=OFFLINE,
            reason=f"local model requested but {reason}",
            should_queue=True,
        )
    if wanted in CLOUD_PROVIDERS:
        usable = wanted in resolved.usable_cloud
        return BrainRouting(
            provider=wanted if usable else "",
            model=resolved.selected_model if usable else "",
            mode=resolved.mode if usable else resolved.mode,
            reason=(
                f"{wanted} requested explicitly"
                if usable
                else f"{wanted} requested explicitly but it is out of capacity"
            ),
            should_queue=not usable,
        )

    return BrainRouting(
        provider=resolved.selected_provider,
        model=resolved.selected_model,
        mode=resolved.mode,
        reason=resolved.fallback_reason or describe(resolved),
        should_queue=resolved.mode == OFFLINE,
    )


def describe(state: ContinuityState) -> str:
    """One honest line naming the real mode, the real model, and the real reason.

    Never rounds up. In degraded mode it says the local model's actual id, and
    in offline mode it does not imply a model is answering at all.
    """

    if state.mode == FULL:
        model = state.selected_model or "the usual model"
        line = f"running normally on {state.selected_provider} ({model})"
        if state.fallback_reason:
            line += f", after falling back because {state.fallback_reason}"
        return line
    if state.mode == DEGRADED:
        return (
            f"degraded: answering locally on {state.selected_model} because "
            f"{state.fallback_reason}. no cloud reasoning, no coding jobs until it's back"
        )
    return (
        f"offline: {state.fallback_reason}. i can still pull memory, commitments, "
        "and briefings, and anything that needs a model is queued"
    )


def assert_not_claiming_cloud(state: ContinuityState, result: Mapping[str, Any]) -> None:
    """Refuse to let a result mislabel where it came from.

    Two directions, both bad. Degraded mode must not hand back something wearing
    a cloud provider's name, and full mode must not claim a local answer it did
    not produce locally.
    """

    provider = str(result.get("provider") or "")
    if state.mode in {DEGRADED, OFFLINE} and provider not in {"local", ""}:
        raise AssertionError(
            f"mode is {state.mode} but a result claimed provider {provider!r}; "
            "a local answer must never be presented as a cloud one"
        )
    if state.mode == FULL and provider == "local":
        raise AssertionError(
            "mode is full but a result claimed to be local; provenance must match "
            "what actually ran"
        )


# ---- deferred work --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeferredWork:
    work_id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    reason: str
    state: str
    attempts: int
    created_at: float
    updated_at: float
    resumed_at: float | None
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContinuityStore:
    """Work that could not run because no provider was available, kept durably."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_CONTINUITY_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._initialize()

    # -- mode history -------------------------------------------------------

    def record_mode(self, state: ContinuityState) -> bool:
        """Note a mode change. Returns True when the mode actually changed.

        Only transitions are stored. Writing a row every poll would turn "when
        did we lose Claude" into a scan of thousands of identical rows.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT mode FROM continuity_modes ORDER BY at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if row is not None and str(row["mode"]) == state.mode:
                return False
            connection.execute(
                "INSERT INTO continuity_modes(mode, reason, selected_provider, "
                "selected_model, at) VALUES (?, ?, ?, ?, ?)",
                (
                    state.mode,
                    _clean(state.fallback_reason, 1_000),
                    state.selected_provider,
                    state.selected_model,
                    state.assessed_at,
                ),
            )
        return True

    def mode_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM continuity_modes ORDER BY at DESC, rowid DESC LIMIT ?",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def current_mode(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT mode FROM continuity_modes ORDER BY at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return str(row["mode"]) if row is not None else FULL

    # -- queue --------------------------------------------------------------

    def defer(
        self,
        *,
        kind: str,
        summary: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> DeferredWork:
        """Hold onto work that had nothing to run on, with why."""

        moment = float(time.time() if now is None else now)
        clean_kind = _clean(kind, 64)
        if not clean_kind:
            raise ContinuityError("deferred work needs a kind")
        clean_summary = _clean(summary, 1_000)
        if not clean_summary:
            raise ContinuityError("deferred work needs a summary")
        work_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO deferred_work(work_id, kind, summary, payload_json, reason, "
                "state, attempts, created_at, updated_at, resumed_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, NULL, '')",
                (
                    work_id,
                    clean_kind,
                    clean_summary,
                    json.dumps(payload or {}, default=str)[:MAX_PAYLOAD_CHARS],
                    _clean(reason, 1_000),
                    moment,
                    moment,
                ),
            )
        return self.require(work_id)

    def get(self, work_id: str) -> DeferredWork | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deferred_work WHERE work_id = ?", (work_id,)
            ).fetchone()
        return _deferred(row) if row is not None else None

    def require(self, work_id: str) -> DeferredWork:
        found = self.get(work_id)
        if found is None:
            raise KeyError(f"unknown deferred work {work_id}")
        return found

    def pending(self, limit: int = MAX_QUEUE_ROWS) -> list[DeferredWork]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deferred_work WHERE state = 'queued' "
                "ORDER BY created_at LIMIT ?",
                (min(MAX_QUEUE_ROWS, max(1, int(limit))),),
            ).fetchall()
        return [_deferred(row) for row in rows]

    def list(self, *, state: str | None = None, limit: int = MAX_QUEUE_ROWS) -> list[DeferredWork]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(MAX_QUEUE_ROWS, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deferred_work" + where + " ORDER BY created_at LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_deferred(row) for row in rows]

    def resume(
        self,
        handler: Callable[[DeferredWork], bool],
        *,
        state: ContinuityState,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int = 25,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Run what was queued, but only once there is something to run it on.

        Deliberately refuses in degraded and offline mode. Draining a queue of
        frontier work onto a 14b local model would produce a pile of answers
        nobody asked that model for, and quietly recording them as done is worse
        than waiting.
        """

        moment = float(time.time() if now is None else now)
        if state.mode != FULL:
            return {
                "resumed": 0,
                "failed": 0,
                "abandoned": 0,
                "skipped": len(self.pending()),
                "reason": f"still {state.mode}; nothing to resume onto yet",
            }
        resumed = 0
        failed = 0
        abandoned = 0
        for item in self.pending(limit=limit):
            if item.attempts >= max_attempts:
                self._finish(
                    item.work_id,
                    state="abandoned",
                    error=f"gave up after {item.attempts} attempts",
                    moment=moment,
                )
                abandoned += 1
                continue
            try:
                ok = bool(handler(item))
                error = "" if ok else "handler declined to resume this"
            except Exception as exc:
                ok = False
                error = _clean(exc, 500) or "resume handler raised"
            if ok:
                self._finish(item.work_id, state="resumed", error="", moment=moment)
                resumed += 1
                continue
            # Resolve an exhausted budget in the pass that exhausts it. Leaving
            # it queued until some later sweep happens to look would mean
            # `pending()` reporting work as still waiting when it is never going
            # to run again, which is the one thing this queue must not do.
            spent = item.attempts + 1
            if spent >= max_attempts:
                self._finish(
                    item.work_id,
                    state="abandoned",
                    error=f"gave up after {spent} attempts; last error: {error}",
                    moment=moment,
                )
                abandoned += 1
            else:
                self._retry(item.work_id, error=error, moment=moment)
                failed += 1
        return {
            "resumed": resumed,
            "failed": failed,
            "abandoned": abandoned,
            "skipped": 0,
            "reason": f"cloud is back on {state.selected_provider}",
        }

    def _finish(self, work_id: str, *, state: str, error: str, moment: float) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE deferred_work SET state = ?, resumed_at = ?, updated_at = ?, "
                "last_error = ? WHERE work_id = ?",
                (
                    state,
                    moment if state == "resumed" else None,
                    moment,
                    _clean(error, 500),
                    work_id,
                ),
            )

    def _retry(self, work_id: str, *, error: str, moment: float) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE deferred_work SET attempts = attempts + 1, updated_at = ?, "
                "last_error = ? WHERE work_id = ?",
                (moment, _clean(error, 500), work_id),
            )

    # -- internals ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deferred_work (
                    work_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resumed_at REAL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS deferred_work_state_idx
                    ON deferred_work(state, created_at);
                CREATE TABLE IF NOT EXISTS continuity_modes (
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    selected_provider TEXT NOT NULL DEFAULT '',
                    selected_model TEXT NOT NULL DEFAULT '',
                    at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS continuity_modes_idx
                    ON continuity_modes(at);
                CREATE TABLE IF NOT EXISTS continuity_schema (
                    version INTEGER NOT NULL
                );
                """
            )
            row = connection.execute("SELECT version FROM continuity_schema").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO continuity_schema(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                connection.execute(
                    "UPDATE continuity_schema SET version = ?", (SCHEMA_VERSION,)
                )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _deferred(row: sqlite3.Row) -> DeferredWork:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    return DeferredWork(
        work_id=str(row["work_id"]),
        kind=str(row["kind"]),
        summary=str(row["summary"]),
        payload=payload if isinstance(payload, dict) else {},
        reason=str(row["reason"] or ""),
        state=str(row["state"]),
        attempts=int(row["attempts"] or 0),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        resumed_at=row["resumed_at"],
        last_error=str(row["last_error"] or ""),
    )


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


__all__ = [
    "CAPABILITY_MATRIX",
    "DEGRADED",
    "FULL",
    "MODES",
    "OFFLINE",
    "BrainRouting",
    "ContinuityError",
    "ContinuityState",
    "ContinuityStore",
    "DeferredWork",
    "ProviderReading",
    "assert_not_claiming_cloud",
    "assess_continuity",
    "describe",
    "route_brain_turn",
]
