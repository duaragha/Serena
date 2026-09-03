"""Persistent four-phase orchestrator and public facade for Serena Fleet."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
)
from concurrent.futures import (
    wait as wait_for_futures,
)
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from fleet.capacity import read_fleet_capacity
from fleet.completion import CompletionVerdict, render_evidence_instructions
from fleet.completion_gate import evaluate_leg_completion
from fleet.context import budget_context, redact_text, redact_value
from fleet.contracts import completion_unit_ids
from fleet.policy import (
    PHASE_MODEL_POLICY,
    PHASES,
    build_policy,
    build_provider_handoff_policy,
    load_config,
    policy_from_snapshot,
    policy_models_match_contract,
    resolve_activity,
)
from fleet.read_mcp import prompt_block as read_mcp_prompt_block
from fleet.store import DEFAULT_DB_PATH, TERMINAL_RUN_STATES, FleetStore
from fleet.supervision import FleetSupervisionStore, WorkerLeaseMonitor
from fleet.workers import WorkerRequest, WorkerResult, run_worker, runtime_doctor
from core.session_identity import resolve_origin_session

POLL_SECONDS = 0.5
WAIT_POLL_SECONDS = 0.5
CAPACITY_POLL_SECONDS = 30.0
CAPACITY_RETRY_COOLDOWN_SECONDS = 300.0
CONTROL_PLANE_FLUSH_SECONDS = 2.0
MAX_CONTEXT_CHARS = 96_000
MAX_STEERING_CONTEXT_CHARS = 16_000
MAX_REVIEW_DIFF_CHARS = 60_000
TERMINAL_NOTICE_RETRY_SECONDS = 6 * 60 * 60
OVERLAY_EVENT_SOCKET = Path.home() / ".local" / "state" / "serena" / "brain-events.sock"
PARALLELIZE_CONTROL_MESSAGE = "[fleet-control] parallelize-current-run-v1"
_METADATA_LOCK = threading.Lock()
_INDEX_LOCK = threading.Lock()
_STORE_LOCK = threading.Lock()
_INTEGRATION_LOCK = threading.Lock()
_PENDING_INTEGRATIONS_LOCK = threading.Lock()
_PENDING_INTEGRATIONS: dict[str, dict[str, dict[str, Any]]] = {}
_STORE_INSTANCE: tuple[str, FleetStore] | None = None
_CAPACITY_EXHAUSTION_ERROR = re.compile(
    r"(?:rate_limit_reached|quota[_ -]?exhausted|insufficient_quota|"
    r"usage (?:is )?(?:exhausted|limit reached)|(?:weekly|five[- ]hour|session) limit|"
    r"out of (?:extra )?usage|credit balance (?:is )?(?:too low|zero)|"
    r"(?:subscription|account) limit (?:has been )?reached)",
    re.IGNORECASE,
)

_GIB = 1024**3
_DEFAULT_CODING_DISK_BASE_BYTES = _GIB
_DEFAULT_CODING_DISK_PER_WORKER_BYTES = _GIB
_DEFAULT_RESEARCH_DISK_BYTES = _GIB
_SQLITE_BUSY_RETRY_DELAYS = (0.25, 0.5, 1.0)


def _required_disk_headroom(activity: str, worker_count: int) -> int:
    """Return the minimum free bytes required before a Fleet run is persisted."""

    override = os.environ.get("SERENA_FLEET_MIN_FREE_BYTES", "").strip()
    if override:
        try:
            value = int(override)
        except ValueError as exc:
            raise ValueError("SERENA_FLEET_MIN_FREE_BYTES must be an integer") from exc
        if value < 0:
            raise ValueError("SERENA_FLEET_MIN_FREE_BYTES cannot be negative")
        return value
    if activity == "coding":
        return _DEFAULT_CODING_DISK_BASE_BYTES + (
            max(1, int(worker_count)) * _DEFAULT_CODING_DISK_PER_WORKER_BYTES
        )
    return _DEFAULT_RESEARCH_DISK_BYTES


def _ensure_disk_headroom(cwd: Path, *, activity: str, worker_count: int) -> None:
    """Fail before dispatch when isolated work would exhaust its filesystem."""

    required = _required_disk_headroom(activity, worker_count)
    if required == 0:
        return
    locations = [cwd, DEFAULT_DB_PATH.parent]
    checked_devices: set[int] = set()
    for location in locations:
        resolved = location.resolve()
        device = resolved.stat().st_dev
        if device in checked_devices:
            continue
        checked_devices.add(device)
        free = int(shutil.disk_usage(resolved).free)
        if free < required:
            free_gib = free / _GIB
            required_gib = required / _GIB
            raise RuntimeError(
                "Fleet disk preflight refused dispatch: "
                f"{free_gib:.1f} GiB free at {resolved}, "
                f"{required_gib:.1f} GiB required for "
                f"{activity} with {worker_count} worker(s). "
                "Remove disposable caches or inactive worktree dependencies, "
                "then retry the same run request."
            )


def _retry_sqlite_busy(operation: Callable[[], Any]) -> Any:
    """Retry short Fleet control-plane lock collisions before failing a leg."""

    for attempt, delay in enumerate((0.0, *_SQLITE_BUSY_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            locked = "database is locked" in str(exc).lower() or (
                "database table is locked" in str(exc).lower()
            )
            if not locked or attempt == len(_SQLITE_BUSY_RETRY_DELAYS):
                raise
    raise AssertionError("unreachable SQLite retry loop")


def _read_start_capacity() -> dict[str, Any]:
    """Read best-effort quota state without making unknown telemetry fatal."""

    try:
        return read_fleet_capacity()
    except Exception as exc:
        # Provider policy treats omitted states as usable. A telemetry failure
        # must not invent an outage or prevent an explicit provider attempt.
        return {
            provider: {
                "usable": True,
                "status": "unknown",
                "reason": f"capacity preflight unavailable: {exc}",
            }
            for provider in ("codex", "claude")
        }


def _saved_policy_request(
    snapshot: object,
    *,
    preserve_selected_provider: bool = False,
) -> tuple[str, int | None]:
    """Recover additive routing inputs when rebuilding an unstarted policy."""

    if not isinstance(snapshot, dict):
        return "auto", None
    requested = str(snapshot.get("requested_provider_mode") or "").strip().lower()
    selected = str(snapshot.get("provider_mode") or "").strip().lower()
    provider_mode = (
        selected
        if preserve_selected_provider and selected in {"balanced", "codex", "claude"}
        else requested or "auto"
    )
    scaling = snapshot.get("scaling")
    raw_workers = scaling.get("requested_workers") if isinstance(scaling, dict) else None
    try:
        worker_count = int(raw_workers) if raw_workers is not None else None
    except (TypeError, ValueError):
        worker_count = None
    return provider_mode, worker_count


def start_run(
    task: str,
    activity: str = "auto",
    provider_mode: str = "auto",
    worker_count: int | None = None,
    cwd: str | None = None,
    origin_session_id: str | None = None,
    origin_agent: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Persist one Fleet run and arrange for a resident or detached owner."""

    if os.environ.get("SERENA_FLEET_WORKER", "").strip().lower() in {"1", "true", "on"}:
        raise RuntimeError("nested Fleet runs are disabled inside Fleet workers")
    clean_task = redact_text(task)[0].strip()
    if not clean_task:
        raise ValueError("Fleet task is required")
    if len(clean_task) > 32_000:
        raise ValueError("Fleet task is too long")
    resolved_cwd = _resolve_cwd(cwd)
    resolved_activity = resolve_activity(clean_task, activity, resolved_cwd)
    capacity = _read_start_capacity()
    policy = build_policy(
        resolved_activity,
        task=clean_task,
        config=load_config(),
        cwd=resolved_cwd,
        provider_mode=provider_mode,
        worker_count=worker_count,
        provider_capacity=capacity,
    )
    if not dry_run:
        _ensure_disk_headroom(
            resolved_cwd,
            activity=resolved_activity,
            worker_count=policy.scaling.selected_workers,
        )
    resolved_origin_id, resolved_origin_agent = _resolve_origin(
        origin_session_id,
        origin_agent,
    )
    store = _store()
    run = store.create_run(
        task=clean_task,
        activity=resolved_activity,
        cwd=str(resolved_cwd),
        origin_session_id=resolved_origin_id,
        origin_agent=resolved_origin_agent,
        dry_run=bool(dry_run),
        policy=policy.to_dict(),
    )
    with suppress(Exception):
        store.flush_control_outbox()
    if not dry_run and run["state"] == "queued":
        _wake_or_launch(str(run["run_id"]))
    return get_run(str(run["run_id"])) or run


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return _store().list_runs(limit)


def get_run(run_id: str) -> dict[str, Any] | None:
    store = _store()
    clean_id = _require_id(run_id)
    run = store.get_run(clean_id)
    if run is not None:
        _reconcile_run_sessions(store, run)
        refreshed = store.get_run(clean_id) or run
        refreshed["isolation"] = _isolation_projection(refreshed)
        refreshed["supervision"] = _supervision_projection(store, refreshed)
        return refreshed
    return None


def stop_run(run_id: str, *, force: bool = False) -> dict[str, Any]:
    """Request cooperative stop. Streaming workers observe it within 250ms.

    ``force`` is the way out of a run whose supervisor is alive but wedged, so
    it can neither converge the cancel nor be retried. It refuses while any
    worker is alive and until the run has gone quiet.
    """

    clean = _require_id(run_id)
    if force:
        return _store().force_cancel_run(clean)
    return _store().request_cancel(clean)


def delete_run(run_id: str) -> dict[str, Any]:
    """Delete a terminal Fleet, its worker chats, and its private runtime state."""

    clean_id = _require_id(run_id)
    store = _store()
    run = preflight_delete_run(clean_id, store=store)

    origin_id = str(run.get("origin_session_id") or "").strip()
    session_ids = sorted(
        {
            str(worker.get("session_id") or "").strip()
            for worker in store.worker_sessions(clean_id)
            if str(worker.get("session_id") or "").strip()
            and str(worker.get("session_id") or "").strip() != origin_id
        }
    )
    deleted_chats: list[str] = []
    missing_chats: list[str] = []
    cleanup_warnings: list[str] = []
    from core.indexer import delete_session
    from core.metadata import delete_meta

    for session_id in session_ids:
        try:
            delete_session(session_id, source="serena-fleet-delete")
            deleted_chats.append(session_id)
        except ValueError:
            delete_meta(session_id)
            missing_chats.append(session_id)
        except Exception as exc:
            cleanup_warnings.append(f"worker chat {session_id} remains: {exc}")

    from fleet.isolation import FleetIsolationStore, cleanup_workspace

    isolation = FleetIsolationStore()
    for workspace in isolation.workspaces(clean_id):
        removed = cleanup_workspace(
            isolation,
            run_id=clean_id,
            worker_key=workspace.worker_key,
            cwd=str(run.get("cwd") or ""),
        )
        if not removed and Path(workspace.path).exists():
            raise RuntimeError(
                f"Fleet deletion stopped because worktree cleanup failed: {workspace.path}"
            )
    isolation.delete_run_records(clean_id)
    deleted = store.delete_run(clean_id)

    state_root = Path(
        os.environ.get("SERENA_FLEET_STATE_DIR", "").strip()
        or (Path.home() / ".local" / "state" / "serena" / "fleet")
    ).expanduser()
    with suppress(OSError):
        shutil.rmtree(state_root / "runs" / clean_id)
    _refresh_index()
    return {
        "run_id": clean_id,
        "state": "deleted",
        "previous_state": deleted["state"],
        "deleted_chat_ids": deleted_chats,
        "missing_chat_ids": missing_chats,
        "deleted_chat_count": len(deleted_chats),
        "cleanup_warnings": cleanup_warnings,
    }


def preflight_delete_run(
    run_id: str, *, store: FleetStore | None = None
) -> dict[str, Any]:
    """Prove a terminal run has no unrecovered worker edits before deletion."""

    clean_id = _require_id(run_id)
    fleet_store = store or _store()
    run = fleet_store.get_run(clean_id)
    if run is None:
        raise KeyError(f"unknown Fleet run {clean_id}")
    if run["state"] not in TERMINAL_RUN_STATES:
        raise RuntimeError("stop this Fleet and wait for it to finish before deleting it")
    from fleet.isolation import FleetIsolationStore, unrecovered_workspaces

    blockers = unrecovered_workspaces(FleetIsolationStore(), clean_id)
    if blockers:
        summary = "; ".join(
            f"{item['worker_key']} ({', '.join(item.get('changed_paths') or []) or item.get('reason') or 'unrecovered'})"
            for item in blockers[:4]
        )
        raise RuntimeError(
            "Fleet has unrecovered worker changes. integrate or recover them before deletion: "
            + summary
        )
    return run


def retry_run(run_id: str) -> dict[str, Any]:
    clean_id = _require_id(run_id)
    run = _store().retry_run(clean_id)
    _wake_or_launch(clean_id)
    return get_run(clean_id) or run


def retry_leg(run_id: str, leg_id: str) -> dict[str, Any]:
    """Retry one failed worker now, or queue it behind its live phase sibling."""

    clean_id = _require_id(run_id)
    clean_leg_id = _require_id(leg_id)
    run = _store().request_leg_retry(clean_id, clean_leg_id)
    if run["state"] == "queued":
        _wake_or_launch(clean_id)
    return get_run(clean_id) or run


def handoff_leg(
    run_id: str,
    leg_id: str,
    target_provider: str,
    *,
    reason: str = "user requested provider handoff",
) -> dict[str, Any]:
    """Continue one logical worker slot on the other native provider."""

    clean_id = _require_id(run_id)
    clean_leg_id = _require_id(leg_id)
    target = str(target_provider or "").strip().lower()
    if target not in {"codex", "claude"}:
        raise ValueError("handoff provider must be claude or codex")
    capacity = _read_start_capacity()
    usable, detail = _capacity_decision(capacity.get(target))
    if not usable:
        raise RuntimeError(f"{target} cannot take this Fleet worker ({detail or 'no capacity'})")
    store = _store()
    requested = store.request_leg_handoff(
        clean_id,
        clean_leg_id,
        target_provider=target,
        reason=reason,
        automatic=False,
    )
    if requested["state"] in {"queued", "failed", "waiting_for_capacity"}:
        pending = next(
            (
                item
                for item in store.pending_handoffs(clean_id)
                if item["leg_id"] == clean_leg_id
            ),
            None,
        )
        if pending is not None:
            requested = _activate_handoff(store, clean_id, pending, capacity=capacity)
    if requested["state"] == "queued":
        _wake_or_launch(clean_id)
    return get_run(clean_id) or requested


def steer_run(run_id: str, message: str) -> dict[str, Any]:
    """Queue context for future legs. Active model turns are never mislabelled as steered."""

    run = _store().add_steering(_require_id(run_id), message)
    run["steering_scope"] = "future-legs"
    return run


def get_result(run_id: str) -> dict[str, Any]:
    return _store().get_result(_require_id(run_id))


def inspect_run(run_id: str, focus: str = "", *, event_limit: int = 100) -> dict[str, Any]:
    """Return a bounded DAG, worker-context, and event inspection projection."""

    clean_id = _require_id(run_id)
    store = _store()
    run = store.get_run(clean_id)
    if run is None:
        raise KeyError(f"unknown Fleet run {clean_id}")
    wanted = str(focus or "").strip()
    units = list(run.get("work_units") or [])
    workers = [
        {**leg, "phase": phase.get("display_name") or phase.get("name")}
        for phase in run.get("phases") or []
        for leg in phase.get("legs") or []
    ]
    if wanted:
        units = [
            unit
            for unit in units
            if str(unit.get("id") or "") == wanted
            or str(unit.get("owner_worker_key") or "") == wanted
            or wanted in {str(item) for item in unit.get("reviewer_worker_keys") or []}
        ]
        workers = [
            worker
            for worker in workers
            if wanted
            in {
                str(worker.get("leg_id") or ""),
                str(worker.get("worker_key") or ""),
            }
            or wanted in {str(item) for item in worker.get("assignment_ids") or []}
        ]
        if not units and not workers:
            raise KeyError(f"unknown Fleet focus {wanted}")
    events = store.events(
        clean_id,
        limit=min(500, max(1, int(event_limit))),
        latest=True,
    )
    projection = {
        "run_id": clean_id,
        "state": run.get("state"),
        "current_phase": run.get("current_phase_display") or run.get("current_phase"),
        "focus": wanted or None,
        "work_units": units,
        "workers": workers,
        "events": events[-min(100, max(1, int(event_limit))) :],
        "isolation": _isolation_projection(run),
        "supervision": _supervision_projection(store, run),
    }
    redacted, redaction_count = redact_value(projection)
    redacted["redaction_count"] = redaction_count
    return redacted


def _isolation_projection(run: dict[str, Any]) -> dict[str, Any]:
    """Read the isolation registry for status without creating new state."""

    if str(run.get("activity") or "") != "coding":
        return {"enabled": False, "mode": "not_applicable", "claims": [], "workspaces": []}
    try:
        from fleet.isolation import (
            DEFAULT_ISOLATION_DB_PATH,
            FleetIsolationStore,
            assess_isolation,
        )

        configured = os.environ.get("SERENA_FLEET_ISOLATION_DB_PATH", "").strip()
        fleet_path = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
        registry_path = (
            Path(configured).expanduser()
            if configured
            else Path(fleet_path).expanduser().with_name("fleet-isolation.sqlite3")
            if fleet_path
            else DEFAULT_ISOLATION_DB_PATH
        )
        assessment = assess_isolation(str(run.get("cwd") or ""))
        claims: list[dict[str, Any]] = []
        workspaces: list[dict[str, Any]] = []
        integrations: list[dict[str, Any]] = []
        if registry_path.exists():
            registry = FleetIsolationStore(registry_path)
            claims = registry.active_claims(str(run["run_id"]))
            workspaces = [item.to_dict() for item in registry.workspaces(str(run["run_id"]))]
            integrations = registry.integrations(str(run["run_id"]))
        return {
            "enabled": _isolation_enabled(),
            "mode": "worktree" if workspaces else assessment.mode,
            "safe": assessment.safe,
            "reason": assessment.reason,
            "base_dirty_path_count": len(assessment.base_dirty_paths),
            "claims": claims,
            "workspaces": workspaces,
            "integrations": integrations,
        }
    except Exception as exc:
        return {
            "enabled": _isolation_enabled(),
            "mode": "unavailable",
            "reason": str(exc)[:500],
            "claims": [],
            "workspaces": [],
            "integrations": [],
        }


def _supervision_projection(store: FleetStore, run: dict[str, Any]) -> dict[str, Any]:
    """Expose durable liveness without making status dependent on it."""

    try:
        projection = FleetSupervisionStore(store.path).project_run(str(run["run_id"]))
        projection["workers"] = [
            {
                key: value
                for key, value in worker.items()
                if key not in {"lease_token", "owner_token"}
            }
            for worker in projection.get("workers") or []
            if isinstance(worker, dict)
        ]
        return projection
    except Exception as exc:
        return {
            "workers": [],
            "active": 0,
            "stalled": 0,
            "retry_scheduled": 0,
            "error": str(exc)[:500],
        }


def wait_for_run(run_id: str, timeout: float | None = None) -> dict[str, Any]:
    clean_id = _require_id(run_id)
    deadline = time.monotonic() + max(0.0, float(timeout)) if timeout is not None else None
    while True:
        run = get_run(clean_id)
        if run is None:
            raise KeyError(f"unknown Fleet run {clean_id}")
        if run["state"] in TERMINAL_RUN_STATES:
            run["wait_timed_out"] = False
            return run
        if deadline is not None and time.monotonic() >= deadline:
            run["wait_timed_out"] = True
            return run
        time.sleep(WAIT_POLL_SECONDS)


def _capacity_decision(entry: object) -> tuple[bool, str]:
    if hasattr(entry, "to_dict"):
        entry = entry.to_dict()
    if isinstance(entry, dict):
        return bool(entry.get("usable", True)), str(
            entry.get("reason") or entry.get("status") or ""
        ).strip()
    if entry is None:
        return True, "capacity is unknown"
    usable = getattr(entry, "usable", True)
    reason = getattr(entry, "reason", "")
    return bool(usable), str(reason or "").strip()


def _capacity_field(entry: object, name: str) -> object:
    if hasattr(entry, "to_dict"):
        entry = entry.to_dict()
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def _capacity_reset(entry: object) -> float | None:
    value = _capacity_field(entry, "resets_at")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _capacity_recovered(entry: object) -> tuple[bool, str]:
    usable, reason = _capacity_decision(entry)
    status = str(_capacity_field(entry, "status") or "").strip().lower()
    # A stored exhaustion must not thrash on an unknown probe. Require a
    # positive available signal. Legacy deterministic mappings without a
    # status remain usable for compatibility.
    return usable and status not in {"unknown", "unavailable"}, reason


def _leg_by_id(run: dict[str, Any], leg_id: str) -> dict[str, Any]:
    for phase in run.get("phases") or []:
        for leg in phase.get("legs") or []:
            if str(leg.get("leg_id") or "") == leg_id:
                return leg
    raise KeyError(f"unknown Fleet worker {leg_id}")


def _activate_handoff(
    store: FleetStore,
    run_id: str,
    request: dict[str, Any],
    *,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = store.get_run(run_id)
    if snapshot is None:
        raise KeyError(f"unknown Fleet run {run_id}")
    leg = _leg_by_id(snapshot, str(request["leg_id"]))
    if leg["state"] == "running":
        return snapshot
    start_index = int(request["phase_index"])
    if leg["state"] == "completed":
        start_index += 1
    if start_index >= len(snapshot.get("phases") or []):
        return store.clear_leg_handoff(
            run_id,
            str(request["leg_id"]),
            reason="the worker completed the final phase before its handoff activated",
        )
    target = str(request["target_provider"])
    source_phase = snapshot["phases"][start_index]
    source_worker = source_phase["legs"][int(request["ordinal"])]
    if str(source_worker["runtime"]) == target:
        return store.clear_leg_handoff(
            run_id,
            str(request["leg_id"]),
            reason=f"the logical worker already uses {target}",
        )
    observed = _read_start_capacity() if capacity is None else capacity
    usable, detail = _capacity_decision(observed.get(target))
    if not usable:
        store.clear_leg_handoff(
            run_id,
            str(request["leg_id"]),
            reason=f"{target} became unavailable before activation: {detail}",
        )
        raise RuntimeError(f"{target} cannot take over this Fleet worker ({detail})")
    replacement = build_provider_handoff_policy(
        snapshot["policy"],
        phase_index=start_index,
        ordinal=int(request["ordinal"]),
        target_provider=target,
        reason=str(request.get("reason") or "provider handoff"),
        automatic=bool(request.get("automatic")),
        requested_at=float(request.get("requested_at") or time.time()),
    )
    return store.apply_leg_handoff(
        run_id,
        str(request["leg_id"]),
        policy=replacement,
        start_phase_index=start_index,
        target_provider=target,
        reason=str(request.get("reason") or "provider handoff"),
        automatic=bool(request.get("automatic")),
    )


def _activate_pending_handoffs(store: FleetStore, run_id: str, phase: str) -> int:
    activated = 0
    for request in store.pending_handoffs(run_id, phase):
        before = store.get_run(run_id)
        _activate_handoff(store, run_id, request)
        after = store.get_run(run_id)
        if before != after:
            activated += 1
    return activated


def _queue_automatic_capacity_handoff(
    store: FleetStore,
    run: dict[str, Any],
    leg: dict[str, Any],
    result: WorkerResult,
) -> None:
    if result.ok or result.cancelled:
        return
    policy = run.get("policy") or {}
    automatic_handoff = str(policy.get("capacity_handoff") or "automatic") == "automatic"
    requested_mode = str(policy.get("requested_provider_mode") or "auto").lower()
    if any(
        item["leg_id"] == str(leg["leg_id"])
        for item in store.pending_handoffs(str(run["run_id"]))
    ):
        return
    source = str(leg["runtime"])
    target = "claude" if source == "codex" else "codex"
    capacity = _read_start_capacity()
    source_usable, source_reason = _capacity_decision(capacity.get(source))
    error = str(result.error or "")
    confirmed = not source_usable or bool(_CAPACITY_EXHAUSTION_ERROR.search(error))
    if not confirmed:
        return
    target_usable, target_reason = _capacity_decision(capacity.get(target))
    may_cross_provider = automatic_handoff and requested_mode in {"auto", "balanced", "mixed"}
    if may_cross_provider and target_usable:
        reason = (
            f"confirmed {source} capacity exhaustion; continuing with {target}. "
            f"{source_reason or error[:500]}"
        ).strip()
        store.request_leg_handoff(
            str(run["run_id"]),
            str(leg["leg_id"]),
            target_provider=target,
            reason=reason,
            automatic=True,
        )
        return

    eligible = [source]
    if may_cross_provider:
        eligible.append(target)
    now = time.time()
    resets = [
        reset
        for provider in eligible
        if (reset := _capacity_reset(capacity.get(provider))) is not None and reset > now
    ]
    next_probe = min(resets) if resets else now + CAPACITY_RETRY_COOLDOWN_SECONDS
    next_probe = max(now + CAPACITY_POLL_SECONDS, next_probe)
    reason_parts = [
        f"confirmed {source} capacity exhaustion",
        source_reason or error[:500],
    ]
    if may_cross_provider and not target_usable:
        reason_parts.append(f"{target} is also unavailable: {target_reason or 'no capacity'}")
    elif not may_cross_provider:
        reason_parts.append(
            "the frozen provider policy does not allow automatic cross-provider routing"
        )
    wait_reason = "; ".join(part for part in reason_parts if part)
    store.request_capacity_wait(
        str(run["run_id"]),
        str(leg["leg_id"]),
        failed_provider=source,
        eligible_providers=eligible,
        reason=wait_reason,
        not_before=next_probe,
        resets_at=min(resets) if resets else None,
    )


def resume_ready_capacity_waits(
    store: FleetStore | None = None,
    *,
    capacity: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[str]:
    """Resume parked runs only after a positive native-capacity observation."""

    fleet_store = store or _store()
    waits = fleet_store.capacity_waits(limit=100)
    if not waits:
        return []
    observed = _read_start_capacity() if capacity is None else capacity
    current = time.time() if now is None else float(now)
    resumed: list[str] = []
    for wait in waits:
        if wait.get("run_state") != "waiting_for_capacity":
            continue
        if current < float(wait.get("not_before") or 0.0):
            continue
        runtime = str(wait.get("runtime") or "")
        eligible = [str(provider) for provider in wait.get("eligible_providers") or []]
        ordered = ([runtime] if runtime in eligible else []) + [
            provider for provider in eligible if provider != runtime
        ]
        selected = next(
            (
                provider
                for provider in ordered
                if _capacity_recovered(observed.get(provider))[0]
            ),
            None,
        )
        if not selected:
            continue
        provider_reason = _capacity_recovered(observed.get(selected))[1]
        reason = f"{selected} capacity recovered: {provider_reason or 'available'}"
        run_id = str(wait["run_id"])
        leg_id = str(wait["leg_id"])
        if selected == runtime:
            fleet_store.resume_capacity_wait(
                run_id,
                leg_id,
                provider=selected,
                reason=reason,
            )
        else:
            fleet_store.request_leg_handoff(
                run_id,
                leg_id,
                target_provider=selected,
                reason=reason,
                automatic=True,
            )
            request = next(
                item
                for item in fleet_store.pending_handoffs(run_id)
                if item["leg_id"] == leg_id
            )
            _activate_handoff(fleet_store, run_id, request, capacity=observed)
        if run_id not in resumed:
            resumed.append(run_id)
    return resumed


def run_supervisor(run_id: str) -> dict[str, Any]:
    """Claim and execute persisted work units through their own phase chains."""

    clean_id = _require_id(run_id)
    store = _store()
    existing = store.get_run(clean_id)
    if existing is None:
        raise KeyError(f"unknown Fleet run {clean_id}")
    if existing["state"] in TERMINAL_RUN_STATES:
        return _terminal_outcome(store, existing)
    saved_policy = existing.get("policy") or {}
    needs_policy_refresh = not isinstance(saved_policy, dict) or (
        "scaling" not in saved_policy
        or "max_parallel_writers" not in saved_policy
        or "provider_mode" not in saved_policy
        or not policy_models_match_contract(existing["activity"], saved_policy)
    )
    if existing["state"] == "queued" and needs_policy_refresh:
        requested_mode, requested_workers = _saved_policy_request(saved_policy)
        current_policy = build_policy(
            existing["activity"],
            task=existing["task"],
            config=load_config(),
            cwd=existing["cwd"],
            provider_mode=requested_mode,
            worker_count=requested_workers,
            provider_capacity=_read_start_capacity(),
        )
        if store.refresh_unstarted_policy(clean_id, current_policy.to_dict()):
            existing = store.get_run(clean_id) or existing
            saved_policy = existing.get("policy") or current_policy.to_dict()
    if (
        existing["state"] == "queued"
        and str(saved_policy.get("provider_mode") or "") != "adaptive"
        and PARALLELIZE_CONTROL_MESSAGE in store.steering_messages(clean_id)
    ):
        requested_mode, requested_workers = _saved_policy_request(
            saved_policy,
            preserve_selected_provider=True,
        )
        current_policy = build_policy(
            existing["activity"],
            task=existing["task"],
            config=load_config(),
            cwd=existing["cwd"],
            provider_mode=requested_mode,
            worker_count=requested_workers,
        )
        existing = store.promote_queued_run_policy(
            clean_id,
            current_policy.to_dict(),
            control_message=PARALLELIZE_CONTROL_MESSAGE,
        )
    stale_modules = _stale_fleet_modules()
    if stale_modules:
        # Third incident from this cause: a resident supervisor keeps serving a
        # run with the gate and policy it imported at boot, so a fix on disk is
        # not a fix in the process. The failures then look like worker bugs,
        # which is where two days of debugging went. Say it plainly instead.
        detail = ", ".join(stale_modules[:6])
        with suppress(Exception):
            store.append_event(
                clean_id,
                "supervisor.stale_code",
                {"modules": stale_modules[:20], "started_at": _PROCESS_STARTED_AT},
            )
        return _terminal_outcome(
            store,
            store.fail_run(
                clean_id,
                "the Fleet supervisor is running code older than what is on disk "
                f"({detail}). Restart it with `systemctl --user restart "
                "serena-fleet.service`, then retry this run. Nothing was "
                "dispatched, so no worker time was spent.",
            ),
        )
    if not store.claim_run(clean_id):
        return store.get_run(clean_id) or existing
    run = store.get_run(clean_id)
    assert run is not None
    _reconcile_run_sessions(store, run)
    _refresh_read_mcp_catalog(store, clean_id)
    policy = policy_from_snapshot(run["policy"])
    try:
        with _coding_run_lock(run):
            interrupted = _run_work_unit_scheduler(store, clean_id, policy)
            if interrupted is not None:
                return interrupted
        final_phase = policy.phases[-1]
        final_outputs = [
            output
            for output in store.completed_outputs(clean_id)
            if output.get("phase") == final_phase.name
            and str(output.get("output_text") or "").strip()
        ]
        if final_phase.execution == "parallel":
            result_text = _combine_worker_outputs(final_outputs)
        else:
            result_text = next(
                (
                    str(output.get("output_text") or "").strip()
                    for output in reversed(final_outputs)
                ),
                "",
            )
        if not result_text:
            return _terminal_outcome(
                store,
                store.fail_run(
                    clean_id,
                    f"{final_phase.display_name} completed without a final response",
                ),
            )
        return _terminal_outcome(store, store.complete_run(clean_id, result_text))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            with suppress(Exception):
                store.cancel_run(clean_id, "Fleet supervisor stopped")
            raise
        current = store.get_run(clean_id)
        if current and current["cancel_requested"]:
            return _terminal_outcome(store, store.cancel_run(clean_id))
        return _terminal_outcome(store, store.fail_run(clean_id, str(exc)))


_PROCESS_STARTED_AT = time.time()


def _stale_fleet_modules() -> list[str]:
    """Fleet source files that changed after this process imported them.

    A resident supervisor holds its modules for the life of the process, so
    editing the gate on disk does nothing to a running daemon. Comparing the
    files it actually loaded against their current mtime is enough to know, and
    it costs one stat per module.
    """

    if os.environ.get("SERENA_FLEET_ALLOW_STALE_CODE", "").strip().lower() in {
        "1",
        "true",
        "on",
    }:
        return []
    stale: list[str] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("core.fleet"):
            continue
        source = getattr(module, "__file__", "") or ""
        if not source.endswith(".py"):
            continue
        try:
            changed_at = os.stat(source).st_mtime
        except OSError:
            continue
        if changed_at > _PROCESS_STARTED_AT:
            stale.append(Path(source).name)
    return sorted(set(stale))


def _refresh_read_mcp_catalog(store: FleetStore, run_id: str) -> None:
    """Refresh what read-only legs may reach, once per run, before any leg runs.

    Best effort on purpose: an unreachable account server must not fail a run.
    A stale or missing catalog simply means those legs run with no MCP at all,
    which is exactly how they behaved before this existed.
    """

    from fleet.read_mcp import catalog_tools, refresh_catalog

    try:
        summary = refresh_catalog(allow_background=True)
        store.append_event(
            run_id,
            "read_mcp_catalog",
            {
                "refreshed": bool(summary.get("refreshed")),
                "reason": str(summary.get("reason") or ""),
                "tool_count": len(catalog_tools()),
                "servers": summary.get("servers") or {},
            },
        )
    except Exception as exc:
        with suppress(Exception):
            store.append_event(
                run_id,
                "read_mcp_catalog",
                {"refreshed": False, "reason": f"{type(exc).__name__}: {exc}"[:200]},
            )


def _run_work_unit_scheduler(
    store: FleetStore,
    run_id: str,
    policy: Any,
) -> dict[str, Any] | None:
    """Run ready legs across phases as soon as each work unit advances."""

    max_workers = max(1, int(policy.max_parallel_workers))
    writer_limit = min(max_workers, max(1, int(policy.max_parallel_writers)))
    if _isolation_enabled():
        writer_limit = max_workers
    phase_policy_by_index = {int(item.index): item for item in policy.phases}
    completed_phases: set[int] = set()
    running: dict[Future[WorkerResult], dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fleet-leg") as pool:
        while True:
            snapshot = store.get_run(run_id)
            if snapshot is None:
                raise KeyError(f"unknown Fleet run {run_id}")

            if snapshot["state"] in TERMINAL_RUN_STATES:
                if running:
                    done, _pending = wait_for_futures(
                        set(running),
                        timeout=POLL_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        running.pop(future)
                        future.result()
                    continue
                return _terminal_outcome(store, snapshot)

            if store.run_cancel_requested(run_id):
                if running:
                    wait_for_futures(set(running))
                    running.clear()
                return _terminal_outcome(store, store.cancel_run(run_id))

            for phase_policy in policy.phases:
                _activate_pending_handoffs(store, run_id, phase_policy.name)
                store.prepare_phase_runnable(run_id, phase_policy.index)

            snapshot = store.get_run(run_id)
            assert snapshot is not None
            if snapshot["state"] in TERMINAL_RUN_STATES:
                continue
            if _isolation_enabled():
                _release_orphaned_write_claims(store, snapshot)
            for phase in snapshot["phases"]:
                index = int(phase["index"])
                if phase["state"] == "completed" and index not in completed_phases:
                    completed_phases.add(index)
                    store.append_event(
                        run_id,
                        "phase.completed",
                        {"phase": str(phase["name"])},
                    )

            if all(phase["state"] == "completed" for phase in snapshot["phases"]):
                if running:
                    wait_for_futures(set(running))
                    running.clear()
                    continue
                return None

            running_leg_ids = {
                str(leg.get("leg_id") or "") for leg in running.values()
            }
            running_worker_keys = {
                _worker_key(leg) for leg in running.values() if _worker_key(leg)
            }
            running_writers = sum(
                1 for leg in running.values() if leg.get("access_mode") == "write"
            )
            active_writer_legs = [
                leg for leg in running.values() if leg.get("access_mode") == "write"
            ]
            running_phase_indexes = {
                int(leg.get("phase_index", -1)) for leg in running.values()
            }
            candidates: list[dict[str, Any]] = []
            for phase in snapshot["phases"]:
                phase_policy = phase_policy_by_index[int(phase["index"])]
                if len(phase["legs"]) < int(snapshot["policy"]["minimum_workers_per_phase"]):
                    raise RuntimeError(
                        f"phase {phase['name']} violates its configured worker minimum"
                    )
                for leg in phase["legs"]:
                    if leg["state"] != "queued" or str(leg["leg_id"]) in running_leg_ids:
                        continue
                    candidate = dict(leg)
                    candidate["phase_index"] = int(phase["index"])
                    candidate["phase_execution"] = str(phase_policy.execution)
                    candidates.append(candidate)

            candidates.sort(
                key=lambda leg: (
                    -int(leg["phase_index"]),
                    *_leg_sort_key(leg),
                )
            )
            slots = max_workers - len(running)
            scheduled = 0
            for leg in candidates:
                if scheduled >= slots:
                    break
                phase_index = int(leg["phase_index"])
                worker_key = _worker_key(leg)
                # Rotated Review advances the target unit, but it is still the
                # reviewer's next turn. Keep each durable worker in phase order
                # and never let two turns for the same worker run concurrently.
                # Without both checks a fast target could launch Agent A's
                # Review before Agent A had finished Research or Code.
                if worker_key in running_worker_keys or any(
                    _worker_key(prior_leg) == worker_key
                    and str(prior_leg.get("state") or "") != "completed"
                    for prior_phase in snapshot["phases"]
                    if int(prior_phase["index"]) < phase_index
                    for prior_leg in prior_phase["legs"]
                ):
                    continue
                if (
                    leg["phase_execution"] == "sequential"
                    and phase_index in running_phase_indexes
                ):
                    continue
                if leg.get("access_mode") == "write" and running_writers >= writer_limit:
                    continue
                if (
                    leg.get("access_mode") == "write"
                    and _isolation_enabled()
                    and not _writer_is_disjoint_from(run=snapshot, candidate=leg, active=active_writer_legs)
                ):
                    continue
                if (
                    leg.get("access_mode") == "write"
                    and _isolation_enabled()
                    and _writer_has_durable_claim_conflict(run=snapshot, candidate=leg)
                ):
                    continue
                phase_name = str(snapshot["phases"][phase_index]["name"])
                if str(snapshot.get("current_phase") or "") != phase_name:
                    store.set_current_phase(run_id, phase_name)
                    snapshot["current_phase"] = phase_name
                future = pool.submit(_execute_leg, store, run_id, leg)
                running[future] = leg
                running_phase_indexes.add(phase_index)
                running_worker_keys.add(worker_key)
                if leg.get("access_mode") == "write":
                    running_writers += 1
                    active_writer_legs.append(leg)
                scheduled += 1

            if running:
                done, _pending = wait_for_futures(
                    set(running),
                    timeout=POLL_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    running.pop(future)
                    future.result()
                refreshed = store.get_run(run_id)
                if refreshed is not None:
                    _reconcile_run_sessions(store, refreshed)
                _refresh_index()
                continue

            snapshot = store.get_run(run_id)
            assert snapshot is not None
            if store.run_cancel_requested(run_id):
                return _terminal_outcome(store, store.cancel_run(run_id))
            unresolved = next(
                (phase for phase in snapshot["phases"] if phase["state"] != "completed"),
                None,
            )
            if unresolved is None:
                return None
            errors = [
                leg.get("current_attempt", {}).get("error")
                for leg in unresolved["legs"]
                if leg.get("current_attempt") and leg["state"] == "failed"
            ]
            detail = next((str(error) for error in errors if error), "phase did not complete")
            resolution = store.resolve_phase_failure(
                run_id,
                str(unresolved["name"]),
                f"{unresolved['name']}: {detail}",
            )
            if resolution.pop("retry_activated", False):
                continue
            if resolution.pop("capacity_waiting", False):
                return resolution
            return _terminal_outcome(store, resolution)


def recover_stale_runs() -> list[str]:
    store = _store()
    recovered = store.recover_stale_runs()
    supervision = FleetSupervisionStore(store.path)
    for run_id in recovered:
        supervision.reconcile_run(run_id)
    return recovered


def _notice_token(run: dict[str, Any]) -> str:
    completed = float(run.get("completed_at") or run.get("updated_at") or 0.0)
    return f"{run.get('state') or 'unknown'}:{completed:.6f}"


def _notice_summary(value: object, *, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _terminal_notice_text(run: dict[str, Any]) -> str:
    short = str(run.get("run_id") or "")[:8]
    state = str(run.get("state") or "unknown")
    activity = str(run.get("activity") or "Fleet")
    project = Path(str(run.get("cwd") or "")).name or "unknown project"
    task = _notice_summary(run.get("task"), limit=140)
    progress = run.get("progress") or {}
    done = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)
    if state == "completed":
        message = (
            f"fleet {short} finished. {activity} in {project}, {done}/{total} agent steps complete."
        )
        if task:
            message += f" {task}"
        return message + " Open the Fleet tab for the result."
    error = (
        _notice_summary(run.get("error"), limit=320) or "the run stopped without an error detail"
    )
    phase = str(run.get("current_phase_display") or run.get("current_phase") or "its current phase")
    return (
        f"fleet {short} needs you. {activity} in {project} failed during {phase}: "
        f"{error}. Open the Fleet tab for the worker details."
    )


def _terminal_spoken_text(run: dict[str, Any]) -> str:
    short = str(run.get("run_id") or "")[:8]
    state = str(run.get("state") or "unknown")
    project = Path(str(run.get("cwd") or "")).name or "your project"
    if state == "completed":
        return (
            f"Fleet {short} finished the {project} run successfully. "
            "The result is ready in the Fleet tab."
        )
    phase = str(run.get("current_phase_display") or run.get("current_phase") or "its work")
    return (
        f"Fleet {short} failed during {phase} for {project}. "
        "The details are ready in the Fleet tab."
    )


def _send_spoken_notice(run: dict[str, Any], token: str) -> bool:
    message = {
        "type": "fleet_notice",
        "run_id": str(run.get("run_id") or ""),
        "state": str(run.get("state") or ""),
        "token": token,
        "text": _terminal_spoken_text(run),
    }
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(payload, str(OVERLAY_EVENT_SOCKET))
    except OSError:
        return False
    finally:
        client.close()
    return True


def _send_raghav_text(message: str) -> bool:
    candidates = [
        shutil.which("chats"),
        str(Path(sys.executable).resolve().with_name("chats")),
        str(Path.home() / ".local" / "bin" / "chats"),
    ]
    executable = next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_file()), None
    )
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "text", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _terminal_notice_pending(store: FleetStore, run_id: str, token: str) -> bool:
    for event in store.events(run_id, limit=2_000):
        if event["type"] not in {
            "run.notification.deferred",
            "run.notification.pending_approval",
        }:
            continue
        if str(event["payload"].get("notice_id") or "") == token:
            return True
    return False


def _emit_terminal_plugin_hook(store: FleetStore, run: dict[str, Any], token: str) -> None:
    run_id = str(run["run_id"])
    for event in store.events(run_id, limit=2_000):
        if event["type"] != "run.plugin_hook.dispatched":
            continue
        if str(event["payload"].get("notice_id") or "") == token:
            return
    from core.plugin_loader import emit_plugin_hook

    hook = f"fleet.run.{run['state']}"
    results = emit_plugin_hook(
        hook,
        {
            "run_id": run_id,
            "state": str(run["state"]),
            "activity": str(run.get("activity") or ""),
            "progress": dict(run.get("progress") or {}),
        },
        project_root=str(run.get("cwd") or "") or None,
    )
    store.append_event(
        run_id,
        "run.plugin_hook.dispatched",
        {
            "notice_id": token,
            "hook": hook,
            "plugin_count": len(results),
            "failure_count": sum(1 for _plugin, result in results if not result.ok),
        },
    )


def _terminal_notification_authority(run: dict[str, Any], token: str):
    from core.control_plane import ControlPlaneStore
    from core.notification_authority import NotificationAuthority
    from core.notification_senders import (
        _policy_from_environment,
        observe_notification_result,
    )

    notification_path = os.environ.get("SERENA_NOTIFICATION_DB_PATH", "").strip()
    control_path = os.environ.get("SERENA_CONTROL_PLANE_DB_PATH", "").strip()
    if os.environ.get("SERENA_FLEET_NO_AUTOSTART") == "1":
        fleet_path = Path(os.environ.get("SERENA_FLEET_DB_PATH", str(DEFAULT_DB_PATH)))
        notification_path = notification_path or str(
            fleet_path.with_name("notifications.sqlite3")
        )
        control_path = control_path or str(fleet_path.with_name("control.sqlite3"))
    return NotificationAuthority(
        Path(notification_path) if notification_path else None,
        policy=_policy_from_environment(),
        senders={
            "voice": lambda _request: _send_spoken_notice(run, token),
            "telegram": lambda request: _send_raghav_text(request.summary),
        },
        control_store=ControlPlaneStore(Path(control_path) if control_path else None),
        result_observer=observe_notification_result,
    )


def _request_terminal_notification(run: dict[str, Any], token: str):
    from core.notification_authority import NotificationRequest

    authority = _terminal_notification_authority(run, token)
    metadata = {
        "fleet_notice_id": token,
        "fleet_state": str(run.get("state") or ""),
    }
    common = {
        "kind": f"fleet.run.{run['state']}",
        "urgency": "normal",
        "source_surface": "fleet",
        "job_id": str(run["run_id"]),
        "metadata": metadata,
    }
    voice = authority.request(
        NotificationRequest(
            summary=_terminal_spoken_text(run),
            channel="voice",
            dedupe_key=f"fleet:{run['run_id']}:{token}:voice",
            **common,
        )
    )
    if voice.decision != "failed":
        return voice
    return authority.request(
        NotificationRequest(
            summary=_terminal_notice_text(run),
            channel="telegram",
            dedupe_key=f"fleet:{run['run_id']}:{token}:telegram",
            **common,
        )
    )


def _terminal_outcome(store: FleetStore, run: dict[str, Any]) -> dict[str, Any]:
    """Route one terminal alert through the shared notification authority."""

    state = str(run.get("state") or "")
    if state not in {"completed", "failed"} or bool(run.get("dry_run")):
        return run
    token = _notice_token(run)
    run_id = str(run["run_id"])
    try:
        _emit_terminal_plugin_hook(store, run, token)
        if store.terminal_notice_delivered(run_id, token):
            return run
        if _terminal_notice_pending(store, run_id, token):
            return run
        result = _request_terminal_notification(run, token)
        if result.decision in {"deferred", "pending_approval", "suppressed"}:
            store.append_event(
                run_id,
                (
                    "run.notification.pending_approval"
                    if result.decision == "pending_approval"
                    else "run.notification.deferred"
                ),
                {
                    "notice_id": token,
                    "state": state,
                    "channel": result.channel,
                    "notification_id": result.notification_id,
                    "authority_decision": result.decision,
                    "deliver_after": result.deliver_after,
                },
            )
        elif result.sent and not store.terminal_notice_delivered(run_id, token):
            store.append_event(
                run_id,
                "run.notification.delivered",
                {
                    "notice_id": token,
                    "state": state,
                    "channel": result.channel,
                    "notification_id": result.notification_id,
                    "authority_decision": result.decision,
                },
            )
        elif result.decision == "failed" and not store.terminal_notice_delivered(
            run_id, token
        ):
            store.append_event(
                run_id,
                "run.notification.failed",
                {
                    "notice_id": token,
                    "state": state,
                    "channel": result.channel,
                    "notification_id": result.notification_id,
                    "authority_decision": result.decision,
                    "error": result.reason,
                },
            )
    except Exception as error:
        with suppress(Exception):
            store.append_event(
                run_id,
                "run.notification.failed",
                {
                    "notice_id": token,
                    "state": state,
                    "channel": "telegram",
                    "error": _notice_summary(error, limit=300),
                },
            )
    return run


def _retry_recent_terminal_notices(store: FleetStore) -> bool:
    """Retry only the newest recent unannounced terminal run on service boot."""

    now = time.time()
    for run in store.list_runs(limit=20):
        if run.get("state") not in {"completed", "failed"} or run.get("dry_run"):
            continue
        completed = float(run.get("completed_at") or 0.0)
        if completed <= 0 or now - completed > TERMINAL_NOTICE_RETRY_SECONDS:
            continue
        before = {
            int(event["event_seq"])
            for event in store.events(str(run["run_id"]), limit=2_000)
            if event["type"] in {"run.notification.dispatched", "run.notification.delivered"}
        }
        _terminal_outcome(store, run)
        after = {
            int(event["event_seq"])
            for event in store.events(str(run["run_id"]), limit=2_000)
            if event["type"] in {"run.notification.dispatched", "run.notification.delivered"}
        }
        return bool(after - before)
    return False


def _recover_outstanding_obligations(store: FleetStore) -> dict[str, Any]:
    """On boot, pick up every promise that died mid-delivery.

    Fleet's own notice retry above only knows about Fleet. This sweep reads the
    shared obligation ledger, so a spoken job result or a queued notice that was
    interrupted by a restart is handed back to the surface that owes it instead
    of disappearing.
    """

    try:
        from core.control_plane import ControlPlaneStore
        from core.control_recovery import reconcile_obligations

        control = ControlPlaneStore()

        def redeliver_fleet(obligation: object) -> bool:
            run_id = str(getattr(obligation, "job_id", "") or "")
            run = store.get_run(run_id) if run_id else None
            if run is None or run.get("state") not in {"completed", "failed"}:
                return False
            before = {
                int(event["event_seq"])
                for event in store.events(run_id, limit=2_000)
                if event["type"]
                in {"run.notification.dispatched", "run.notification.delivered"}
            }
            _terminal_outcome(store, run)
            after = {
                int(event["event_seq"])
                for event in store.events(run_id, limit=2_000)
                if event["type"]
                in {"run.notification.dispatched", "run.notification.delivered"}
            }
            return bool(after - before)

        def redeliver_notification(obligation: object) -> bool:
            from core.notification_authority import NotificationAuthority

            notification_id = str(getattr(obligation, "job_id", "") or "")
            if not notification_id:
                return False
            authority = NotificationAuthority(
                senders={"telegram": lambda request: _send_raghav_text(request.summary)},
                control_store=control,
            )
            result = authority.redeliver(notification_id)
            return bool(result and result.sent)

        report = reconcile_obligations(
            control,
            handlers={
                "fleet": redeliver_fleet,
                "notification": redeliver_notification,
            },
        )
        return report.to_dict()
    except Exception:
        return {"recovered": [], "abandoned": [], "skipped": [], "total": 0}


def serve_forever(
    poll_interval: float = POLL_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Resident queue owner used by ``serena-fleet.service``.

    Every queued run gets its own supervisor thread. There is deliberately no
    run-count ceiling here; provider capacity and the per-checkout coding lock
    remain the narrower safety boundaries.
    """

    stopper = stop_event or threading.Event()
    store = _store()
    recover_stale_runs()
    _reconcile_recent_runs(store)
    _retry_recent_terminal_notices(store)
    _recover_outstanding_obligations(store)
    next_capacity_probe = 0.0
    next_control_flush = 0.0
    active: dict[str, threading.Thread] = {}
    while not stopper.is_set():
        active = {run_id: thread for run_id, thread in active.items() if thread.is_alive()}
        monotonic_now = time.monotonic()
        if monotonic_now >= next_control_flush:
            with suppress(Exception):
                store.flush_control_outbox()
            next_control_flush = monotonic_now + CONTROL_PLANE_FLUSH_SECONDS
        if monotonic_now >= next_capacity_probe:
            with suppress(Exception):
                resume_ready_capacity_waits(store)
            next_capacity_probe = monotonic_now + CAPACITY_POLL_SECONDS
        launched = False
        while not stopper.is_set():
            run_id = store.next_queued_run()
            if run_id is None or run_id in active:
                break
            thread = threading.Thread(
                target=run_supervisor,
                args=(run_id,),
                name=f"fleet-{run_id[:8]}",
                daemon=True,
            )
            active[run_id] = thread
            thread.start()
            launched = True

            # The worker claims before expensive execution. Wait only long
            # enough for the queue head to advance, then launch the next run.
            claim_deadline = time.monotonic() + 5.0
            while thread.is_alive() and not stopper.is_set():
                snapshot = store.get_run(run_id)
                if snapshot is None or snapshot.get("state") != "queued":
                    break
                if time.monotonic() >= claim_deadline:
                    break
                stopper.wait(0.01)
            if store.next_queued_run() == run_id:
                break
        if not launched:
            stopper.wait(max(0.1, float(poll_interval)))


def doctor() -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        config = load_config()
        capacity = _read_start_capacity()
        coding = build_policy("coding", config=config, provider_capacity=capacity)
        research = build_policy("research", config=config, provider_capacity=capacity)
        adaptive_probe = "tasks:\n- probe one\n- probe two\n- probe three"
        adaptive_coding = build_policy(
            "coding", adaptive_probe, config=config, provider_capacity=capacity
        )
        adaptive_research = build_policy(
            "research", adaptive_probe, config=config, provider_capacity=capacity
        )
        checks["policy"] = {
            "ok": True,
            "phases": list(PHASES),
            "coding_session_mode": coding.session_mode,
            "research_session_mode": research.session_mode,
            "coding_workers_per_phase": len(coding.phases[0].workers),
            "research_workers_per_phase": len(research.phases[0].workers),
            "coding_adaptive_workers_per_phase": len(adaptive_coding.phases[0].workers),
            "research_adaptive_workers_per_phase": len(adaptive_research.phases[0].workers),
            "coding_max_parallel_writers": coding.max_parallel_writers,
            "research_max_parallel_writers": research.max_parallel_writers,
            "coding_provider_mode": coding.provider_mode,
            "research_provider_mode": research.provider_mode,
            "coding_phase_models": {
                phase.display_name: [
                    {
                        "provider": provider,
                        "model": model,
                        "effort": effort,
                    }
                    for provider, model, effort in PHASE_MODEL_POLICY["coding"][phase.name]
                ]
                for phase in coding.phases
            },
            "research_phase_models": {
                phase.display_name: [
                    {
                        "provider": provider,
                        "model": model,
                        "effort": effort,
                    }
                    for provider, model, effort in PHASE_MODEL_POLICY["research"][phase.name]
                ]
                for phase in research.phases
            },
        }
        checks["capacity"] = {
            provider: value.to_dict() if hasattr(value, "to_dict") else value
            for provider, value in capacity.items()
        }
    except Exception as exc:
        checks["policy"] = {"ok": False, "error": str(exc)}
    try:
        from core.control_plane import ControlPlaneStore

        store = _store()
        control = ControlPlaneStore()
        checks["store"] = {"ok": True, "path": str(store.path)}
        checks["control_plane"] = {
            "ok": True,
            "path": str(control.path),
            "pending_fleet_events": store.pending_control_events(),
            "open_fleet_obligations": len(
                control.obligations(state="open", surface="fleet", limit=500)
            ),
        }
    except Exception as exc:
        checks["store"] = {"ok": False, "error": str(exc)}
        checks["control_plane"] = {"ok": False, "error": str(exc)}
    checks["runtimes"] = runtime_doctor()
    service = _service_active()
    checks["service"] = {"ok": service, "active": service}
    return {
        "ok": bool(
            checks["policy"]["ok"]
            and checks["store"]["ok"]
            and checks["control_plane"]["ok"]
            and checks["runtimes"]["ok"]
        ),
        "checks": checks,
    }


def _leg_sort_key(leg: dict[str, Any]) -> tuple[int, str, str]:
    return (
        int(leg.get("ordinal") or 0),
        str(leg.get("worker_key") or ""),
        str(leg.get("leg_id") or ""),
    )


def _writer_is_disjoint_from(
    *,
    run: dict[str, Any],
    candidate: dict[str, Any],
    active: list[dict[str, Any]],
) -> bool:
    """Run only proven-disjoint writers together.

    Unknown ownership is a repository-wide claim. That is intentionally
    conservative: a serialized turn costs time, while two broad writers can
    destroy each other's work after both providers have already spent a turn.
    """

    from fleet.isolation import ROOT_CLAIM, normalise_claim_path, paths_overlap

    def claims(leg: dict[str, Any]) -> list[str]:
        raw = _effective_write_paths(run, leg)
        try:
            cleaned = list(dict.fromkeys(normalise_claim_path(path) for path in raw))
        except ValueError:
            return [ROOT_CLAIM]
        return cleaned

    candidate_paths = claims(candidate)
    return all(
        not paths_overlap(candidate_path, active_path)
        for leg in active
        for candidate_path in candidate_paths
        for active_path in claims(leg)
    )


def _writer_has_durable_claim_conflict(
    *, run: dict[str, Any], candidate: dict[str, Any]
) -> bool:
    """Check the cross-process claim registry before starting a writer."""

    from fleet.isolation import (
        ROOT_CLAIM,
        FleetIsolationStore,
        normalise_claim_path,
        paths_overlap,
    )

    raw_paths = _effective_write_paths(run, candidate)
    try:
        candidate_paths = list(
            dict.fromkeys(normalise_claim_path(path) for path in raw_paths)
        )
    except ValueError:
        candidate_paths = [ROOT_CLAIM]
    worker_key = _worker_key(candidate)
    return any(
        str(claim.get("worker_key") or "") != worker_key
        and any(
            paths_overlap(candidate_path, str(claim.get("path") or ROOT_CLAIM))
            for candidate_path in candidate_paths
        )
        for claim in FleetIsolationStore().active_claims(str(run["run_id"]))
    )


def _leg_working_directory(
    run: dict[str, Any],
    leg: dict[str, Any],
    *,
    attempt: dict[str, Any] | None = None,
) -> str:
    """Where this leg actually runs: its own worktree, or the shared checkout.

    Only write-capable coding legs are isolated. Review and verify legs must see
    the combined result in the real checkout, and research legs never write at
    all. A write leg fails closed when its workspace or claim cannot be proven;
    silently falling back to the shared checkout would defeat the boundary.
    """

    base = str(run["cwd"])
    if leg.get("access_mode") != "write":
        return base
    if not _isolation_enabled():
        return base
    from fleet.isolation import (
        FleetIsolationStore,
        IsolationError,
        assess_isolation,
        ensure_workspace,
        refresh_workspace_for_retry,
    )

    assessment = assess_isolation(base)
    if not assessment.safe:
        raise IsolationError(assessment.reason)
    isolation = FleetIsolationStore()
    if attempt is not None and int(attempt.get("attempt_number") or 0) > 1:
        workspace, recovery = refresh_workspace_for_retry(
            isolation,
            run_id=str(run["run_id"]),
            worker_key=_worker_key(leg),
            cwd=base,
            assessment=assessment,
        )
        attempt["workspace_recovery"] = recovery
    else:
        workspace = ensure_workspace(
            isolation,
            run_id=str(run["run_id"]),
            worker_key=_worker_key(leg),
            cwd=base,
            assessment=assessment,
        )
    claims = _effective_write_paths(run, leg)
    decision = isolation.claim_paths(
        run_id=str(run["run_id"]),
        worker_key=_worker_key(leg),
        paths=claims,
    )
    if not decision.ok:
        detail = decision.conflicts or decision.rejected
        raise IsolationError(f"supervisor write claim was refused: {detail[:8]}")
    return workspace.path


def _release_write_claims(
    store: FleetStore,
    run_id: str,
    leg: dict[str, Any],
    *,
    attempt_id: str | None = None,
) -> int:
    """Release one terminal write attempt's claims without hiding its result."""

    if leg.get("access_mode") != "write" or not _isolation_enabled():
        return 0
    worker_key = _worker_key(leg)
    try:
        from fleet.isolation import FleetIsolationStore

        released = FleetIsolationStore().release_claims(run_id, worker_key)
    except Exception as exc:
        with suppress(Exception):
            store.append_event(
                run_id,
                "worker.claim_release_failed",
                {"worker_key": worker_key, "error": str(exc)[:1_000]},
                leg_id=str(leg.get("leg_id") or "") or None,
                attempt_id=attempt_id,
            )
        return 0
    if released:
        with suppress(Exception):
            store.append_event(
                run_id,
                "worker.claims_released",
                {"worker_key": worker_key, "count": released},
                leg_id=str(leg.get("leg_id") or "") or None,
                attempt_id=attempt_id,
            )
    return released


def _release_orphaned_write_claims(store: FleetStore, run: dict[str, Any]) -> int:
    """Recover claims left by terminal attempts from older supervisors.

    A live attempt keeps its ownership. Every other claim is orphaned because
    Fleet only holds claims while a provider process is actively working; the
    integration gate and preserved worktree remain the durable safety boundary.
    """

    if not _isolation_enabled():
        return 0
    running_workers: set[str] = set()
    for phase in run.get("phases") or []:
        for leg in phase.get("legs") or []:
            attempt = leg.get("current_attempt")
            if (
                leg.get("access_mode") == "write"
                and isinstance(attempt, dict)
                and attempt.get("state") == "running"
            ):
                running_workers.add(_worker_key(leg))
    try:
        from fleet.isolation import FleetIsolationStore

        isolation = FleetIsolationStore()
        claimed_workers = {
            str(claim.get("worker_key") or "")
            for claim in isolation.active_claims(str(run["run_id"]))
            if str(claim.get("worker_key") or "")
        }
        orphaned = sorted(claimed_workers - running_workers)
        released = sum(
            isolation.release_claims(str(run["run_id"]), worker_key)
            for worker_key in orphaned
        )
    except Exception as exc:
        with suppress(Exception):
            store.append_event(
                str(run["run_id"]),
                "worker.claim_reconciliation_failed",
                {"error": str(exc)[:1_000]},
            )
        return 0
    if released:
        with suppress(Exception):
            store.append_event(
                str(run["run_id"]),
                "worker.claims_reconciled",
                {"worker_keys": orphaned, "count": released},
            )
    return released


def _declared_write_paths(run: dict[str, Any], leg: dict[str, Any]) -> list[str]:
    policy = run.get("policy")
    units = policy.get("work_units") if isinstance(policy, dict) else None
    assignment_ids = set(_string_list(leg.get("assignment_ids")))
    worker_key = _worker_key(leg)
    paths: list[str] = []
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        identifier = str(unit.get("id") or "")
        owner = str(unit.get("owner_worker_key") or "")
        if identifier not in assignment_ids and owner != worker_key:
            continue
        ownership = unit.get("file_ownership")
        if isinstance(ownership, dict):
            paths.extend(_string_list(ownership.get("declared_paths")))
    return list(dict.fromkeys(paths))


def _effective_write_paths(run: dict[str, Any], leg: dict[str, Any]) -> list[str]:
    """Exact supervisor-owned claim for a write leg.

    Declared paths retain useful parallelism. An empty declaration means Fleet
    cannot prove disjoint ownership, so the repository claim serializes the
    writer before any provider process starts.
    """

    from fleet.isolation import ROOT_CLAIM

    declared = _declared_write_paths(run, leg)
    return declared or [ROOT_CLAIM]


def _isolation_enabled() -> bool:
    """Isolation is the default; an explicit operator override is visible."""

    configured = os.environ.get("SERENA_FLEET_ISOLATION", "").strip().lower()
    return configured not in {"0", "false", "off", "shared"}


def _integration_test_gate() -> list[str] | None:
    configured = os.environ.get("SERENA_FLEET_INTEGRATION_TEST_COMMAND", "").strip()
    if not configured:
        return None
    command = shlex.split(configured)
    if not command:
        raise ValueError("Fleet integration test command is empty")
    return command


def _declared_integration_tests(
    output_text: str, cwd: str
) -> list[list[str]]:
    """Return the worker's own verification commands, safe to re-run on merge.

    Only commands the existing test allowlist already accepts are returned, so
    integration re-runs real test processes and never model-authored shell.
    """

    from fleet.completion import extract_envelope
    from fleet.completion_gate import safe_test_argv

    payload, _prose, error = extract_envelope(output_text)
    if error or not isinstance(payload, dict):
        return []
    commands: list[str] = []
    for unit in payload.get("units") or []:
        if not isinstance(unit, dict):
            continue
        for item in unit.get("tests") or []:
            if not isinstance(item, dict):
                continue
            command = " ".join(str(item.get("command") or "").split())
            if command and command not in commands:
                commands.append(command)
    argvs: list[list[str]] = []
    for command in commands:
        argv = safe_test_argv(command, cwd)
        if argv:
            argvs.append(argv)
    return argvs


def _integrate_completed_workspace(
    store: FleetStore,
    snapshot: dict[str, Any],
    leg: dict[str, Any],
    attempt: dict[str, Any],
    declared_tests: list[list[str]] | None = None,
    declared_paths: list[str] | None = None,
) -> Any:
    """Queue one completed writer and wait for stable phase-order integration."""

    run_id = str(snapshot["run_id"])
    worker_key = _worker_key(leg)
    phase_name = _phase_for_leg(snapshot, str(leg["leg_id"]))
    # A phase containing unknown ownership is already provider-serialized by a
    # repository-wide claim. Waiting for every queued writer before integrating
    # would deadlock: the next writer cannot start until this one integrates
    # and releases that claim. Integrate it immediately under the repository
    # mutex. Declared disjoint batches retain deterministic stable ordering.
    from fleet.isolation import ROOT_CLAIM, FleetIsolationStore, integrate_workspace

    phase_record = next(
        (
            phase
            for phase in snapshot.get("phases") or []
            if str(phase.get("name") or "") == phase_name
        ),
        None,
    )
    serial_phase = bool(
        phase_record
        and any(
            candidate.get("access_mode") == "write"
            and ROOT_CLAIM in _effective_write_paths(snapshot, candidate)
            for candidate in phase_record.get("legs") or []
        )
    )
    if serial_phase:
        integration = integrate_workspace(
            FleetIsolationStore(),
            run_id=run_id,
            worker_key=worker_key,
            cwd=str(snapshot["cwd"]),
            test_gate=_integration_test_gate(),
            declared_tests=declared_tests,
            declared_paths=declared_paths,
        )
        with suppress(Exception):
            store.append_event(
                run_id,
                (
                    "worker.integration.accepted"
                    if integration.ok
                    else "worker.integration.rejected"
                ),
                integration.to_dict(),
                leg_id=str(leg["leg_id"]),
                attempt_id=str(attempt["attempt_id"]),
            )
        return integration
    pending = {
        "event": threading.Event(),
        "leg": leg,
        "attempt": attempt,
        "phase_name": phase_name,
        "cwd": str(snapshot["cwd"]),
        "declared_tests": declared_tests,
        "declared_paths": declared_paths,
        "result": None,
        "error": None,
    }
    with _PENDING_INTEGRATIONS_LOCK:
        bucket = _PENDING_INTEGRATIONS.setdefault(run_id, {})
        if worker_key in bucket:
            raise RuntimeError(f"duplicate pending integration for {worker_key}")
        bucket[worker_key] = pending

    with _INTEGRATION_LOCK:
        _drain_pending_integrations_locked(store, snapshot, phase_name)

    pending["event"].wait()
    if pending["error"] is not None:
        raise pending["error"]
    if pending["result"] is None:
        raise RuntimeError(f"integration did not produce a result for {worker_key}")
    return pending["result"]


def _drain_pending_integrations_locked(
    store: FleetStore,
    snapshot: dict[str, Any],
    phase_name: str,
) -> None:
    """Integrate a complete phase batch deterministically under the global lock.

    Provider turns remain concurrent in their isolated worktrees. Integration
    waits until every writer in this phase is either queued here or terminal,
    then applies the ready patches one at a time in stable worker-key order.
    A failed writer therefore cannot deadlock successful peers.
    """

    from fleet.isolation import (
        FleetIsolationStore,
        integrate_workspace,
        plan_integration_order,
    )

    run_id = str(snapshot["run_id"])
    current_snapshot = store.get_run(run_id) or snapshot
    phase = next(
        (
            item
            for item in current_snapshot.get("phases") or []
            if str(item.get("name") or "") == phase_name
        ),
        None,
    )
    if phase is None:
        raise KeyError(f"Fleet phase {phase_name} is absent from run {run_id}")
    writers = sorted(
        (
            leg
            for leg in phase.get("legs") or []
            if str(leg.get("access_mode") or "") == "write"
        ),
        key=lambda item: _worker_key(item),
    )
    with _PENDING_INTEGRATIONS_LOCK:
        ready = {
            worker_key: item
            for worker_key, item in (_PENDING_INTEGRATIONS.get(run_id) or {}).items()
            if str(item.get("phase_name") or "") == phase_name
        }
    unsettled = [
        _worker_key(leg)
        for leg in writers
        if _worker_key(leg) not in ready
        and str(leg.get("state") or "") in {"queued", "running"}
    ]
    if unsettled:
        return

    isolation = FleetIsolationStore()
    stable_order = [
        workspace.worker_key
        for workspace in plan_integration_order(isolation.workspaces(run_id))
        if workspace.worker_key in ready
    ]
    stable_order.extend(sorted(set(ready) - set(stable_order)))
    for ready_worker in stable_order:
        with _PENDING_INTEGRATIONS_LOCK:
            current = (_PENDING_INTEGRATIONS.get(run_id) or {}).pop(
                ready_worker, None
            )
        if current is None:
            continue
        try:
            integration = integrate_workspace(
                isolation,
                run_id=run_id,
                worker_key=ready_worker,
                cwd=str(current["cwd"]),
                test_gate=_integration_test_gate(),
                declared_tests=current.get("declared_tests"),
                declared_paths=current.get("declared_paths"),
            )
            current["result"] = integration
            with suppress(Exception):
                store.append_event(
                    run_id,
                    (
                        "worker.integration.accepted"
                        if integration.ok
                        else "worker.integration.rejected"
                    ),
                    integration.to_dict(),
                    leg_id=str(current["leg"]["leg_id"]),
                    attempt_id=str(current["attempt"]["attempt_id"]),
                )
        except Exception as exc:
            current["error"] = exc
        finally:
            current["event"].set()
    with _PENDING_INTEGRATIONS_LOCK:
        if not _PENDING_INTEGRATIONS.get(run_id):
            _PENDING_INTEGRATIONS.pop(run_id, None)


def _wake_pending_integrations_after_terminal(
    store: FleetStore,
    snapshot: dict[str, Any],
    leg: dict[str, Any],
) -> None:
    """Let successful peers advance when this writer terminated without a patch."""

    if leg.get("access_mode") != "write" or not _isolation_enabled():
        return
    run_id = str(snapshot["run_id"])
    with _PENDING_INTEGRATIONS_LOCK:
        if not _PENDING_INTEGRATIONS.get(run_id):
            return
    phase_name = _phase_for_leg(snapshot, str(leg["leg_id"]))
    with _INTEGRATION_LOCK:
        _drain_pending_integrations_locked(store, snapshot, phase_name)


def _skip_finalize_leg(
    store: FleetStore, run_id: str, leg: dict[str, Any]
) -> WorkerResult | None:
    """Complete a Fix leg without a provider turn when it has nothing to fix.

    Every worker used to run all four phases unconditionally, so a slice its
    reviewers cleared still burned a full xhigh turn to report that there was
    nothing to do. A leg is only skipped when review actually ran, produced
    machine-readable findings, and raised none against this worker's units.
    One reporter per phase always runs so the run still ends with a real
    final response.
    """

    snapshot = store.get_run(run_id)
    if snapshot is None or str(snapshot.get("activity") or "") != "coding":
        return None
    phase_record = _phase_record_for_leg(snapshot, leg["leg_id"])
    if str(phase_record.get("name") or "") != "finalize":
        return None

    verify_outputs = [
        output
        for output in store.completed_outputs(run_id)
        if str(output.get("phase") or "") == "verify"
        and str(output.get("output_text") or "").strip()
    ]
    if not verify_outputs:
        return None

    # The designated reporter is the first leg in stable order. It runs even
    # with a clean review so the phase still produces a final response.
    members = sorted(
        (item for item in (phase_record.get("legs") or []) if _worker_key(item)),
        key=_worker_key,
    )
    if not members or _worker_key(members[0]) == _worker_key(leg):
        return None
    if not _review_reported_findings(store, snapshot):
        return None
    if _findings_for_worker(store, snapshot, leg):
        return None

    attempt = store.begin_attempt(leg["leg_id"])
    worker_key = _worker_key(leg)
    owned = ", ".join(_string_list(leg.get("assignment_ids"))) or "(shared)"
    output_text = (
        f"{_worker_label(leg)} skipped the Fix phase: the completed Review phase "
        f"raised no finding against this worker's units ({owned}). No change was "
        "made and no provider turn was spent."
    )
    store.append_event(
        run_id,
        "worker.finalize.skipped",
        {"worker_key": worker_key, "assignment_ids": _string_list(leg.get("assignment_ids"))},
        leg_id=str(leg["leg_id"]),
        attempt_id=str(attempt["attempt_id"]),
    )
    store.finish_attempt(
        attempt["attempt_id"],
        state="completed",
        output_text=output_text,
        session_id=attempt.get("resume_session_id"),
        actual_model=str(leg.get("model") or ""),
        exit_code=0,
    )
    return WorkerResult(True, output_text, attempt.get("resume_session_id"), None, None, 0)


def _execute_leg(store: FleetStore, run_id: str, leg: dict[str, Any]) -> WorkerResult:
    skipped = _skip_finalize_leg(store, run_id, leg)
    if skipped is not None:
        return skipped
    supervision = _retry_sqlite_busy(lambda: FleetSupervisionStore(store.path))
    _retry_sqlite_busy(lambda: supervision.reconcile_run(run_id))
    _retry_sqlite_busy(lambda: supervision.fence_expired_leg(leg["leg_id"]))
    attempt = _retry_sqlite_busy(lambda: store.begin_attempt(leg["leg_id"]))
    live: dict[str, Any] = {
        "pid": None,
        "session_id": attempt.get("resume_session_id"),
        "surfaced_session_id": None,
    }
    try:
        lease = _retry_sqlite_busy(lambda: supervision.acquire(attempt["attempt_id"]))
    except Exception as exc:
        result = WorkerResult(False, "", live.get("session_id"), None, None, -1, str(exc))
        store.finish_attempt(
            attempt["attempt_id"],
            state="failed",
            error=redact_text(str(exc))[0],
            session_id=result.session_id,
            exit_code=result.exit_code,
        )
        return result
    monitor = WorkerLeaseMonitor(supervision, lease)
    monitor.start()
    try:
        snapshot = store.get_run(run_id)
        if snapshot is None:
            raise KeyError(f"unknown Fleet run {run_id}")
        working_directory = _leg_working_directory(snapshot, leg, attempt=attempt)
        if leg.get("access_mode") == "write" and _isolation_enabled():
            from fleet.isolation import FleetIsolationStore

            claims = FleetIsolationStore().active_claims(
                run_id, worker_key=_worker_key(leg)
            )
            store.append_event(
                run_id,
                "worker.claims_acquired",
                {
                    "worker_key": _worker_key(leg),
                    "paths": [str(claim.get("path") or "") for claim in claims],
                    "owner": "fleet-supervisor",
                },
                leg_id=str(leg["leg_id"]),
                attempt_id=str(attempt["attempt_id"]),
            )
            recovery = attempt.get("workspace_recovery")
            if isinstance(recovery, dict) and str(recovery.get("action") or "") not in {
                "",
                "reused",
            }:
                store.append_event(
                    run_id,
                    "worker.workspace_refreshed_for_retry",
                    recovery,
                    leg_id=str(leg["leg_id"]),
                    attempt_id=str(attempt["attempt_id"]),
                )
        prompt = _worker_prompt(
            store,
            snapshot,
            leg,
            attempt,
            working_directory=working_directory,
        )
        request = WorkerRequest(
            run_id=run_id,
            leg_id=leg["leg_id"],
            attempt_id=attempt["attempt_id"],
            task=snapshot["task"],
            activity=snapshot["activity"],
            phase=_phase_for_leg(snapshot, leg["leg_id"]),
            role=leg["role"],
            provider=leg["runtime"],
            model=leg["model"],
            effort=leg["effort"],
            access_mode=leg["access_mode"],
            cwd=working_directory,
            prompt=prompt,
            worker_key=_worker_key(leg),
            worker_label=_worker_label(leg),
            assignment=_assignment_text(leg.get("assignment")),
            assignment_ids=tuple(_string_list(leg.get("assignment_ids"))),
            review_target_ids=tuple(_string_list(leg.get("review_target_ids"))),
            resume_session_id=attempt.get("resume_session_id"),
        )
    except Exception as exc:
        monitor.stop()
        result = WorkerResult(
            False,
            "",
            live.get("session_id"),
            None,
            None,
            -1,
            str(exc),
            store.run_cancel_requested(run_id),
        )
        safe_error, _error_redactions = redact_text(result.error or "")
        try:
            store.finish_attempt(
                attempt["attempt_id"],
                state="cancelled" if result.cancelled else "failed",
                error=safe_error or None,
                session_id=result.session_id,
                exit_code=result.exit_code,
            )
        finally:
            _release_write_claims(
                store,
                run_id,
                leg,
                attempt_id=attempt["attempt_id"],
            )
            supervision.release(
                attempt["attempt_id"],
                lease.lease_token,
                state="cancelled" if result.cancelled else "failed",
                reason=safe_error,
            )
        return result

    def surface_if_ready() -> None:
        sid = str(live.get("session_id") or "")
        pid = int(live.get("pid") or 0)
        if not sid or pid < 1 or live.get("surfaced_session_id") == sid:
            return
        if _surface_session(store, snapshot, request, sid, pid):
            live["surfaced_session_id"] = sid

    def on_event(event_type: str, payload: dict[str, Any]) -> None:
        if not monitor.progress():
            raise RuntimeError("Fleet worker lease was fenced")
        if event_type == "process.started":
            live["pid"] = int(payload["pid"])
            store.mark_attempt_process(
                attempt["attempt_id"],
                live["pid"],
                str(payload.get("event_log_path") or ""),
            )
            if not supervision.bind_process(
                attempt["attempt_id"], lease.lease_token, live["pid"]
            ):
                raise RuntimeError("Fleet worker lease was fenced before process binding")
            surface_if_ready()
        elif event_type == "session.started":
            live["session_id"] = str(payload["session_id"])
            store.mark_attempt_session(attempt["attempt_id"], live["session_id"])
            surface_if_ready()
        elif event_type == "worker.event":
            actual_model = _clean_optional(payload.get("model"))
            actual_effort = _clean_optional(payload.get("effort"))
            if actual_model or actual_effort:
                store.mark_attempt_identity(
                    attempt["attempt_id"],
                    actual_model=actual_model,
                    actual_effort=actual_effort,
                )
        safe_payload, _event_redactions = redact_value(payload)
        store.append_event(
            run_id,
            event_type,
            safe_payload,
            leg_id=leg["leg_id"],
            attempt_id=attempt["attempt_id"],
        )
        surface_if_ready()

    try:
        result = run_worker(
            request,
            cancel_requested=lambda: (
                store.run_cancel_requested(run_id) or monitor.should_cancel()
            ),
            on_event=on_event,
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        result = WorkerResult(
            ok=False,
            output_text="",
            session_id=live.get("session_id"),
            actual_model=None,
            actual_effort=None,
            exit_code=-1,
            error=str(exc),
            cancelled=store.run_cancel_requested(run_id),
        )
    finally:
        monitor.stop()
    stalled = monitor.stalled
    if stalled:
        result = WorkerResult(
            ok=False,
            output_text=result.output_text,
            session_id=result.session_id,
            actual_model=result.actual_model,
            actual_effort=result.actual_effort,
            exit_code=result.exit_code,
            error=(
                f"worker stalled after {lease.stall_after_seconds:.0f} seconds without progress"
            ),
            cancelled=False,
            event_log_path=result.event_log_path,
        )
    if (monitor.fenced or not supervision.owns(attempt["attempt_id"], lease.lease_token)) and not stalled:
        # A replacement generation owns the leg. The old process has been
        # stopped by its callback and must not overwrite the newer attempt.
        release_sid = str(result.session_id or live.get("session_id") or "")
        if release_sid and live.get("pid"):
            _release_session(release_sid, live["pid"])
        return WorkerResult(
            False,
            result.output_text,
            result.session_id,
            result.actual_model,
            result.actual_effort,
            result.exit_code,
            "worker lease was fenced by recovery or reassignment",
            False,
            result.event_log_path,
        )
    if result.ok and store.run_cancel_requested(run_id):
        result = WorkerResult(
            False,
            result.output_text,
            result.session_id,
            result.actual_model,
            result.actual_effort,
            result.exit_code,
            "cancelled by user",
            True,
            result.event_log_path,
        )
    state = (
        "interrupted"
        if stalled
        else "completed"
        if result.ok
        else "cancelled"
        if result.cancelled
        else "failed"
    )
    safe_output, _output_redactions = redact_text(result.output_text)
    safe_error, _result_error_redactions = redact_text(result.error or "")
    # A provider process exiting zero says the CLI ran, not that the work-unit
    # contract was met. When this leg was handed a contract, the final message
    # has to prove it before the leg is allowed to be recorded as completed.
    evidence_blocked = False
    verdict: CompletionVerdict | None = None
    if state == "completed":
        verdict = _completion_verdict(
            store,
            snapshot,
            leg,
            attempt,
            result.output_text,
            event_log_path=result.event_log_path,
        )
        if verdict is not None and verdict.blocked:
            evidence_blocked = True
            state = "failed"
            safe_error = redact_text(verdict.summary())[0] or "completion evidence rejected"
    integration_blocked = False
    if state == "completed" and request.access_mode == "write" and _isolation_enabled():
        try:
            integration = _integrate_completed_workspace(
                store,
                snapshot,
                leg,
                attempt,
                declared_tests=_declared_integration_tests(
                    result.output_text, str(snapshot["cwd"])
                ),
                declared_paths=(
                    sorted(
                        {
                            path
                            for unit in (verdict.units if verdict is not None else ())
                            for path in unit.changed_paths
                        }
                    )
                    if verdict is not None
                    else None
                ),
            )
            if not integration.ok:
                integration_blocked = True
                state = "failed"
                safe_error = redact_text(integration.reason)[0] or "integration was rejected"
        except Exception as exc:
            integration_blocked = True
            state = "failed"
            safe_error = redact_text(f"integration gate failed closed: {exc}")[0]
            store.append_event(
                run_id,
                "worker.integration.rejected",
                {"ok": False, "reason": safe_error},
                leg_id=leg["leg_id"],
                attempt_id=attempt["attempt_id"],
            )
        if integration_blocked:
            result = WorkerResult(
                False,
                result.output_text,
                result.session_id,
                result.actual_model,
                result.actual_effort,
                result.exit_code,
                safe_error,
                False,
                result.event_log_path,
            )
    try:
        store.finish_attempt(
            attempt["attempt_id"],
            state=state,
            output_text=safe_output,
            error=safe_error or None,
            session_id=result.session_id,
            actual_model=result.actual_model,
            actual_effort=result.actual_effort,
            exit_code=result.exit_code,
        )
        _wake_pending_integrations_after_terminal(store, snapshot, leg)
    finally:
        _release_write_claims(
            store,
            run_id,
            leg,
            attempt_id=attempt["attempt_id"],
        )
        surface_if_ready()
        release_sid = str(result.session_id or live.get("session_id") or "")
        if release_sid and live.get("pid"):
            _release_session(release_sid, live["pid"])
    if stalled:
        retry = supervision.schedule_stall_retry(
            attempt["attempt_id"], lease.lease_token
        )
        store.append_event(
            run_id,
            "worker.stalled",
            retry,
            leg_id=leg["leg_id"],
            attempt_id=attempt["attempt_id"],
        )
        return result
    supervision.release(
        attempt["attempt_id"],
        lease.lease_token,
        state=(
            "completed"
            if state == "completed"
            else "cancelled"
            if state == "cancelled"
            else "failed"
        ),
        reason=safe_error,
    )
    if (
        evidence_blocked
        and verdict is not None
        and _should_auto_repair_completion(verdict)
        and not store.has_event(
            run_id,
            "leg.completion_repair_requested",
            leg_id=str(leg["leg_id"]),
        )
    ):
        try:
            store.request_leg_retry(run_id, str(leg["leg_id"]))
            store.append_event(
                run_id,
                "leg.completion_repair_requested",
                {
                    "reason": (
                        redact_text(verdict.summary())[0][:400]
                        or "completion evidence rejected"
                    ),
                    "attempt_number": int(attempt.get("attempt_number") or 0),
                    "resume_session_id": str(result.session_id or "") or None,
                },
                leg_id=str(leg["leg_id"]),
                attempt_id=str(attempt["attempt_id"]),
            )
        except Exception as exc:
            store.append_event(
                run_id,
                "leg.completion_repair_failed",
                {"error": str(exc)[:1_000]},
                leg_id=str(leg["leg_id"]),
                attempt_id=str(attempt["attempt_id"]),
            )
    # A rejected contract is not provider exhaustion, so it must never be read
    # as a reason to hand this slot to the other provider.
    if state == "failed" and not evidence_blocked and not integration_blocked:
        try:
            _queue_automatic_capacity_handoff(store, snapshot, leg, result)
        except Exception as exc:
            store.append_event(
                run_id,
                "leg.handoff_detection_failed",
                {"error": str(exc)[:1_000]},
                leg_id=leg["leg_id"],
                attempt_id=attempt["attempt_id"],
            )
    return result


def _should_auto_repair_completion(verdict: CompletionVerdict) -> bool:
    """One corrective retry for any enforced evidence rejection.

    The retried attempt resumes the same durable session and its prompt now
    carries the rejection reasons, so a worker can fix a reporting defect (bad
    envelope shape, missing exit codes, an unverifiable command) instead of the
    run dying on it. The once-per-leg event guard at the call site prevents
    repair loops; a second rejection stays failed.
    """

    return verdict.enforced and not verdict.accepted


def _surface_session(
    store: FleetStore,
    run: dict[str, Any],
    request: WorkerRequest,
    session_id: str,
    pid: int,
) -> bool:
    surfaced = False
    with _METADATA_LOCK:
        try:
            from core import metadata as meta

            group_id = store.ensure_worker_group(run["run_id"])
            title = _worker_title(
                run,
                request.provider,
                request.model,
                request.effort,
                worker_label=request.worker_label,
                assignment=request.assignment,
            )
            meta.surface_fleet_worker(
                session_id,
                run_id=run["run_id"],
                leg_id=request.leg_id,
                phase=request.phase,
                provider=request.provider,
                model=request.model,
                effort=request.effort,
                worker_key=request.worker_key,
                worker_label=request.worker_label,
                assignment=request.assignment,
                worker_group_id=group_id,
                title=title[:180],
                origin_session_id=run.get("origin_session_id"),
                pid=pid,
                lease_seconds=24 * 60 * 60,
            )
            surfaced = True
        except Exception as exc:
            store.append_event(
                run["run_id"],
                "session.surface_warning",
                {"session_id": session_id, "error": str(exc)},
                leg_id=request.leg_id,
                attempt_id=request.attempt_id,
            )
    if surfaced:
        _refresh_index()
    return surfaced


def _reconcile_run_sessions(
    store: FleetStore,
    run: dict[str, Any],
    *,
    refresh_index: bool = True,
) -> bool:
    """Idempotently repair Fleet markers and grouping from durable run state."""

    if run.get("dry_run"):
        return False
    changed = False
    with _METADATA_LOCK:
        try:
            from core import metadata as meta

            group_id = store.ensure_worker_group(str(run["run_id"]))
            for worker in store.worker_sessions(str(run["run_id"])):
                changed = (
                    meta.surface_fleet_worker(
                        worker["session_id"],
                        run_id=str(run["run_id"]),
                        leg_id=worker["leg_id"],
                        phase=worker["phase"],
                        provider=worker["provider"],
                        model=worker["model"],
                        effort=worker["effort"],
                        worker_key=worker["worker_key"],
                        worker_label=worker.get("worker_label"),
                        assignment=worker.get("assignment"),
                        worker_group_id=group_id,
                        title=_worker_title(
                            run,
                            worker["provider"],
                            worker["model"],
                            worker["effort"],
                            worker_label=worker.get("worker_label"),
                            assignment=worker.get("assignment"),
                        )[:180],
                        origin_session_id=run.get("origin_session_id"),
                    )
                    or changed
                )
        except Exception as exc:
            store.append_event(
                str(run["run_id"]),
                "session.reconcile_warning",
                {"error": str(exc)},
            )
            return False
    if changed and refresh_index:
        _refresh_index()
    return changed


def _reconcile_recent_runs(store: FleetStore, limit: int = 100) -> int:
    """Repair recent Fleet session metadata once when the resident service boots."""

    repaired = 0
    for summary in store.list_runs(limit=limit):
        run = store.get_run(str(summary["run_id"]))
        if run is not None and _reconcile_run_sessions(store, run, refresh_index=False):
            repaired += 1
    if repaired:
        _refresh_index()
    return repaired


def _worker_title(
    run: dict[str, Any],
    provider: str,
    model: str,
    effort: str,
    *,
    worker_label: object = "",
    assignment: object = "",
) -> str:
    label = _clean_inline(worker_label, limit=48) or provider
    owned = _clean_inline(_assignment_text(assignment), limit=72)
    assignment_part = f" | {owned}" if owned else ""
    return f"Fleet {run['run_id'][:8]} | {label}{assignment_part} | {model} {effort}"


def _clean_inline(value: object, *, limit: int = 240) -> str:
    clean = " ".join(str(value or "").split())
    return clean if len(clean) <= limit else clean[: max(1, limit - 1)].rstrip() + "…"


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
    return [clean for item in values if (clean := _clean_inline(item, limit=120))]


def _assignment_text(value: object) -> str:
    if isinstance(value, dict):
        identifier = _clean_inline(value.get("id"), limit=80)
        detail = _clean_inline(
            value.get("title")
            or value.get("task")
            or value.get("description")
            or value.get("summary"),
            limit=600,
        )
        if identifier and detail:
            return f"{identifier}: {detail}"
        return detail or identifier
    if isinstance(value, (list, tuple)):
        parts = [_assignment_text(item) for item in value]
        return "; ".join(part for part in parts if part)
    return _clean_inline(value, limit=800)


def _worker_key(leg: dict[str, Any]) -> str:
    return str(
        leg.get("worker_key")
        or f"{leg.get('runtime') or leg.get('provider') or 'worker'}:{int(leg.get('ordinal') or 0)}"
    )


def _worker_label(leg: dict[str, Any]) -> str:
    return _clean_inline(leg.get("worker_label"), limit=80) or _worker_key(leg)


def _output_leg(run: dict[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    phase_index = int(output.get("phase_index", -1))
    ordinal = int(output.get("ordinal", -1))
    for phase in run.get("phases") or []:
        if int(phase.get("index", -1)) != phase_index:
            continue
        return next(
            (leg for leg in phase.get("legs") or [] if int(leg.get("ordinal", -1)) == ordinal),
            None,
        )
    return None


def _output_worker_key(output: dict[str, Any], run: dict[str, Any]) -> str:
    direct = str(output.get("worker_key") or "").strip()
    if direct:
        return direct
    source = _output_leg(run, output)
    if source is not None:
        return _worker_key(source)
    return f"{output.get('runtime') or 'worker'}:{int(output.get('ordinal', -1))}"


def _output_worker_label(output: dict[str, Any], run: dict[str, Any]) -> str:
    direct = _clean_inline(output.get("worker_label"), limit=80)
    if direct:
        return direct
    source = _output_leg(run, output)
    return _worker_label(source) if source is not None else ""


def _team_roster(phase: dict[str, Any]) -> str:
    rows: list[str] = []
    for member in sorted(phase.get("legs") or [], key=_leg_sort_key):
        assignment = _assignment_text(member.get("assignment")) or "shared task"
        assignment_ids = ", ".join(_string_list(member.get("assignment_ids"))) or "shared"
        identity = f"{member.get('runtime') or member.get('provider') or 'worker'} / {member.get('model') or 'model pending'}"
        rows.append(
            f"- {_worker_label(member)} ({_worker_key(member)}), {identity}, "
            f"assignment ids {assignment_ids}: {_clean_inline(assignment, limit=320)}"
        )
    return "\n".join(rows) or "(roster unavailable)"


def _review_targets(phase: dict[str, Any], leg: dict[str, Any]) -> str:
    target_ids = _string_list(leg.get("review_target_ids"))
    if not target_ids:
        return "(none assigned in this phase)"
    rows: list[str] = []
    members = list(phase.get("legs") or [])
    for target_id in target_ids:
        owners = [
            member
            for member in members
            if target_id in _string_list(member.get("assignment_ids"))
            or target_id == _worker_key(member)
        ]
        if not owners:
            rows.append(f"- {target_id}")
            continue
        owner_text = ", ".join(
            f"{_worker_label(owner)}: "
            f"{_clean_inline(_assignment_text(owner.get('assignment')) or 'shared task', limit=240)}"
            for owner in owners
        )
        rows.append(f"- {target_id}, owned by {owner_text}")
    return "\n".join(rows)


def _findings_for_worker(
    store: FleetStore, run: dict[str, Any], leg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the review findings raised against this worker's own units."""

    from fleet.completion import extract_envelope

    owned = {value for value in _string_list(leg.get("assignment_ids"))}
    if not owned:
        return []
    findings: list[dict[str, Any]] = []
    for output in store.completed_outputs(str(run["run_id"])):
        if str(output.get("phase") or "") != "verify":
            continue
        payload, _prose, error = extract_envelope(str(output.get("output_text") or ""))
        if error or not isinstance(payload, dict):
            continue
        reviewer = (
            output.get("worker_label")
            or output.get("worker_key")
            or output.get("runtime")
            or "reviewer"
        )
        for unit in payload.get("units") or []:
            if not isinstance(unit, dict):
                continue
            for item in unit.get("findings") or []:
                if not isinstance(item, dict):
                    continue
                unit_id = " ".join(str(item.get("unit_id") or "").split())
                if unit_id and unit_id in owned:
                    findings.append({**item, "reported_by": str(reviewer)})
    return findings


def _review_reported_findings(store: FleetStore, run: dict[str, Any]) -> bool:
    """True when a completed Review leg actually emitted a findings list.

    Absence of findings and absence of a structured review are different facts.
    Only the first one licenses skipping a Fix leg; the second means Fleet
    cannot tell, so the fixer runs.
    """

    from fleet.completion import extract_envelope

    for output in store.completed_outputs(str(run["run_id"])):
        if str(output.get("phase") or "") != "verify":
            continue
        payload, _prose, error = extract_envelope(str(output.get("output_text") or ""))
        if error or not isinstance(payload, dict):
            continue
        for unit in payload.get("units") or []:
            if isinstance(unit, dict) and isinstance(unit.get("findings"), list):
                return True
    return False


def _findings_block(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "(no review finding was raised against your units)"
    rows: list[str] = []
    for index, item in enumerate(findings, start=1):
        severity = _clean_inline(item.get("severity"), limit=16) or "unrated"
        unit_id = _clean_inline(item.get("unit_id"), limit=64) or "(unit)"
        summary = _clean_inline(item.get("summary"), limit=400) or "(no summary)"
        evidence = _clean_inline(item.get("evidence"), limit=400)
        location = _clean_inline(item.get("file"), limit=200)
        reporter = _clean_inline(item.get("reported_by"), limit=64)
        line = f"{index}. [{severity}] {unit_id}: {summary}"
        if location:
            line += f"\n   location: {location}"
        if evidence:
            line += f"\n   evidence: {evidence}"
        if reporter:
            line += f"\n   raised by: {reporter}"
        rows.append(line)
    return "\n".join(rows)


def _review_diff_block(run: dict[str, Any], leg: dict[str, Any]) -> str:
    """Return the integrated patches this reviewer is actually accountable for.

    Review used to be told to "inspect the combined implementation", which means
    re-reading the codebase to work out what changed. Fleet already stored the
    exact patch it applied for every writer, so the reviewer is handed the diff
    instead: cheaper to read and far harder to review vaguely.
    """

    if str(run.get("activity") or "") != "coding":
        return ""
    try:
        from fleet.isolation import FleetIsolationStore
    except ImportError:
        return ""
    try:
        records = FleetIsolationStore().integrations(str(run["run_id"]))
    except Exception:
        return ""

    targets = {value for value in _string_list(leg.get("review_target_ids"))}
    owners: set[str] = set()
    for phase in run.get("phases") or []:
        for member in phase.get("legs") or []:
            member_key = _worker_key(member)
            if not member_key or member_key == _worker_key(leg):
                continue
            if not targets or targets & set(_string_list(member.get("assignment_ids"))):
                owners.add(member_key)

    sections: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not record.get("ok"):
            continue
        worker_key = str(record.get("worker_key") or "")
        # With no rotation assigned (a solo run), the worker reviews its own
        # integrated work rather than nothing at all.
        if owners and worker_key not in owners:
            continue
        patch_path = str(record.get("patch_path") or "")
        if not patch_path or patch_path in seen:
            continue
        seen.add(patch_path)
        try:
            patch = Path(patch_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if patch.strip():
            sections.append(f"[integrated patch / {worker_key}]\n{patch.strip()}")

    if not sections:
        return ""
    body, _receipt = budget_context(
        [(f"patch {index}", section) for index, section in enumerate(sections, start=1)],
        budget_chars=MAX_REVIEW_DIFF_CHARS,
    )
    return body


def _work_unit_contract_block(
    run: dict[str, Any], leg: dict[str, Any], *, phase: str = ""
) -> str:
    policy = run.get("policy") or {}
    raw_units = policy.get("work_units") if isinstance(policy, dict) else None
    if not isinstance(raw_units, list) or not raw_units:
        return "(legacy run without explicit work-unit contracts)"
    own_ids = _string_list(leg.get("assignment_ids"))
    review_ids = _string_list(leg.get("review_target_ids"))
    selected_ids = list(dict.fromkeys([*own_ids, *review_ids]))
    units = {
        str(unit.get("id") or ""): unit
        for unit in raw_units
        if isinstance(unit, dict)
    }
    rows: list[str] = []
    first_contract: dict[str, Any] | None = None
    for identifier in selected_ids:
        unit = units.get(identifier)
        if unit is None:
            continue
        contract = unit.get("completion_contract")
        if first_contract is None and isinstance(contract, dict):
            first_contract = contract
        ownership = unit.get("file_ownership") or {}
        dependencies = ", ".join(_string_list(unit.get("dependency_ids"))) or "none"
        relationship = "own" if identifier in own_ids else "review"
        rows.append(
            f"- {identifier} [{relationship}], owner {unit.get('owner_worker_key') or 'unknown'}, "
            f"dependencies {dependencies}, file mode {ownership.get('mode') or 'unspecified'}: "
            f"{_clean_inline(unit.get('logical_scope') or unit.get('description'), limit=600)}"
        )
    if not rows:
        return "(no work-unit contract assigned)"
    criteria: list[str] = []
    if first_contract:
        criteria.append(
            "Acceptance criteria:\n"
            + "\n".join(f"- {item}" for item in _string_list(first_contract.get("acceptance_criteria")))
        )
        criteria.append(
            "Required evidence:\n"
            + "\n".join(f"- {item}" for item in _string_list(first_contract.get("required_evidence")))
        )
        criteria.append(
            "Stop conditions:\n"
            + "\n".join(f"- {item}" for item in _string_list(first_contract.get("stop_conditions")))
        )
    # The enforced envelope is generated from the same module that validates
    # it, so the worker is never gated on a rule it was not given.
    instructions = render_evidence_instructions(
        raw_units,
        completion_unit_ids(leg, phase),
        access_mode=str(leg.get("access_mode") or "read"),
        phase=phase,
    )
    return (
        "Assigned units:\n"
        + "\n".join(rows)
        + "\n\n"
        + "\n\n".join(criteria)
        + ("\n\n" + instructions if instructions else "")
    )


def _review_target_readiness_failure(
    run: dict[str, Any],
    leg: dict[str, Any],
    attempt: dict[str, Any],
) -> str:
    """Fail closed when a rotated Review predates its target Code receipt."""

    if str(leg.get("phase") or "") != "verify":
        return ""
    targets = _string_list(leg.get("review_target_ids"))
    if not targets:
        return ""
    try:
        review_started = float(attempt.get("started_at"))
    except (TypeError, ValueError):
        return "Review attempt has no trustworthy start timestamp"

    execute_legs = [
        item
        for phase in run.get("phases") or []
        if str(phase.get("name") or "") == "execute"
        for item in phase.get("legs") or []
    ]
    for target in targets:
        target_leg = next(
            (
                item
                for item in execute_legs
                if target in _string_list(item.get("assignment_ids"))
            ),
            None,
        )
        current = target_leg.get("current_attempt") if target_leg else None
        if not isinstance(current, dict) or str(current.get("state") or "") != "completed":
            return f"Review target {target} did not have a completed Code attempt at dispatch"
        try:
            code_completed = float(current.get("completed_at"))
        except (TypeError, ValueError):
            return f"Review target {target} has no trustworthy Code completion timestamp"
        if code_completed > review_started:
            return (
                f"Review started before target {target} Code completed; "
                "the immutable context receipt is stale"
            )
    return ""


def _completion_verdict(
    store: FleetStore,
    run: dict[str, Any],
    leg: dict[str, Any],
    attempt: dict[str, Any],
    output_text: str,
    event_log_path: str | None = None,
) -> CompletionVerdict | None:
    """Validate a successful leg against its contract and record the verdict.

    A contracted leg cannot complete when the evidence infrastructure fails.
    The provider result remains retryable, and the durable event distinguishes
    infrastructure failure from a worker's contradictory evidence.
    """

    run_id = str(run.get("run_id") or "")
    readiness_failure = _review_target_readiness_failure(run, leg, attempt)
    if readiness_failure:
        with suppress(Exception):
            store.append_event(
                run_id,
                "leg.review_target_not_ready",
                {"error": readiness_failure[:1_000], "retryable": True},
                leg_id=leg["leg_id"],
                attempt_id=attempt["attempt_id"],
            )
        return CompletionVerdict(
            accepted=False,
            enforced=True,
            reason="review target Code output was not ready before dispatch",
            envelope_present=False,
            failures=(readiness_failure[:400],),
        )
    try:
        verdict = evaluate_leg_completion(
            run,
            leg,
            output_text,
            event_log_path=event_log_path,
        )
    except Exception as exc:
        failure = f"completion evidence infrastructure failed closed: {exc}"
        with suppress(Exception):
            store.append_event(
                run_id,
                "leg.completion_gate_failed",
                {"error": str(exc)[:1_000], "retryable": True},
                leg_id=leg["leg_id"],
                attempt_id=attempt["attempt_id"],
            )
        return CompletionVerdict(
            accepted=False,
            enforced=True,
            reason="completion evidence infrastructure was unavailable",
            envelope_present=False,
            failures=(failure[:400],),
        )
    if not verdict.enforced:
        return verdict
    with suppress(Exception):
        event_type = (
            "leg.completion_evidence_stopped"
            if verdict.terminal_stop
            else "leg.completion_evidence_accepted"
            if verdict.accepted
            else "leg.completion_evidence_rejected"
        )
        store.append_event(
            run_id,
            event_type,
            {
                "accepted": verdict.accepted,
                "completion_allowed": verdict.completion_allowed,
                "terminal_stop": verdict.terminal_stop,
                "reason": verdict.reason,
                "failures": list(verdict.failures[:12]),
                "units": [unit.to_dict() for unit in verdict.units],
                "retryable": not verdict.terminal_stop,
            },
            leg_id=leg["leg_id"],
            attempt_id=attempt["attempt_id"],
        )
    return verdict


def _release_session(session_id: str, pid: int) -> None:
    with suppress(Exception):
        from core import metadata as meta

        meta.clear_external_runtime(session_id, pid=pid)


def _other_worker_session(run: dict[str, Any], current_sid: str) -> str | None:
    for phase in run.get("phases") or []:
        for leg in phase.get("legs") or []:
            attempt = leg.get("current_attempt") or {}
            sid = str(attempt.get("session_id") or "")
            if sid and sid != current_sid:
                return sid
    return None


def _worker_prompt(
    store: FleetStore,
    run: dict[str, Any],
    leg: dict[str, Any],
    attempt: dict[str, Any],
    *,
    working_directory: str | None = None,
) -> str:
    phase_record = _phase_record_for_leg(run, leg["leg_id"])
    phase = str(phase_record["name"])
    phase_display = str(phase_record.get("display_name") or phase)
    before_phase = int(phase_record["index"]) if phase_record["execution"] == "parallel" else None
    outputs = store.completed_outputs(run["run_id"], before_phase=before_phase)
    worker_key = _worker_key(leg)
    if (
        attempt.get("resume_session_id")
        and str(run.get("policy", {}).get("session_mode") or "per_leg") == "persistent_by_worker"
    ):
        previous_phase = int(phase_record["index"]) - 1
        outputs = [
            output
            for output in outputs
            if int(output.get("phase_index", -1)) == previous_phase
            and _output_worker_key(output, run) != worker_key
        ]
    steering = [
        message
        for message in store.steering_messages(run["run_id"])
        if not message.startswith("[fleet-control]")
    ]
    # Relevance, not equality. A peer working on a unit this worker depends on,
    # reviews, or shares ownership with earns the window; an unrelated peer's
    # full report used to arrive at the same size and get skimmed.
    related_units = set(_string_list(leg.get("assignment_ids")))
    related_units.update(_string_list(leg.get("review_target_ids")))
    for unit in (run.get("policy") or {}).get("work_units") or []:
        if not isinstance(unit, dict):
            continue
        if str(unit.get("id") or "") in related_units:
            related_units.update(_string_list(unit.get("dependency_ids")))
    units_by_worker: dict[str, set[str]] = {}
    for phase_entry in run.get("phases") or []:
        for member in phase_entry.get("legs") or []:
            member_key = _worker_key(member)
            if member_key:
                units_by_worker.setdefault(member_key, set()).update(
                    _string_list(member.get("assignment_ids"))
                )

    context_sources: list[tuple[str, str]] = []
    context_weights: list[float] = []
    for output in outputs:
        text = output["output_text"].strip()
        if not text:
            continue
        identity = (
            _clean_inline(output.get("worker_label"), limit=64)
            or _output_worker_label(output, run)
            or _output_worker_key(output, run)
            or str(output.get("runtime") or "worker")
        )
        context_sources.append(
            (f"{output['phase']} / {identity} / {output['role']}", text)
        )
        source_units = units_by_worker.get(_output_worker_key(output, run) or "", set())
        related = bool(related_units and source_units & related_units)
        context_weights.append(4.0 if related or not related_units else 1.0)
    context, context_receipt = budget_context(
        context_sources,
        budget_chars=MAX_CONTEXT_CHARS,
        weights=context_weights,
    )
    peer_count = max(0, len(phase_record.get("legs") or []) - 1)
    if peer_count == 1:
        shared_checkout = "Your Fleet peer works from the same base in a separate worktree."
    elif peer_count > 1:
        shared_checkout = (
            f"Your {peer_count} Fleet peers work from the same base in separate worktrees."
        )
    else:
        shared_checkout = "No other Fleet worker is assigned to this phase."
    review_access = (
        "Re-read your own completed implementation as a separate review pass. Do not edit files. "
        "Report concrete defects and exact fixes, and do not describe this as peer or independent review."
        if peer_count == 0
        else "Review the integrated patches printed below first, then read only the "
        "surrounding code those diffs actually touch. Do not edit files. Report "
        "concrete defects and exact fixes."
    )
    access = {
        "write": (
            f"Work as an active co-implementer. {shared_checkout} Fleet gives each writer an "
            "isolated worktree, runs only proven-disjoint ownership concurrently, and integrates "
            "validated patches one at a time. Unknown ownership is serialized repository-wide. "
            "Own only your assigned surface, re-read each file "
            "immediately before a narrow edit, preserve earlier team changes, and never reset, "
            "revert, checkout, or overwrite a conflict. If a safe merge is unclear, leave that file "
            "intact and report the conflict. Implement and verify real work."
        ),
        "review": review_access,
        "verify": "Verify independently with relevant tests and inspection. Do not intentionally edit source files.",
        "read_only": (
            "Work read-only. Do not edit files or change external state. This boundary applies "
            "only to the current Research phase: implementation belongs to the later Code phase. "
            "Never stop or block merely because this phase cannot write, commit, push, comment, "
            "or mutate external state. Complete the research and implementation handoff with the "
            "best available checkout, peer, and authenticated read evidence; record any genuinely "
            "unavailable source as a limitation rather than treating phase separation as failure."
        ),
    }[leg["access_mode"]]
    read_mcp = read_mcp_prompt_block(str(leg["access_mode"]))
    # A worker whose phase moved it to the other provider starts in a session
    # that saw none of the earlier work. Say so, and point it at the read map
    # instead of letting it rediscover the checkout from scratch.
    fresh_session_note = (
        ""
        if attempt.get("resume_session_id")
        else (
            "\nThis is a fresh session: nothing above happened in your context. "
            "Use the prior phase's 'Read map' to open the exact regions it names "
            "instead of searching the repository for them again. Re-read a file "
            "before you change it, but do not redo the survey work already done."
        )
    )
    handoff_context = attempt.get("handoff_context") or {}
    if attempt.get("resume_kind") == "provider_handoff":
        source_provider = _clean_inline(handoff_context.get("provider"), limit=32) or "previous"
        resume = (
            f"Take over this logical worker slot from {source_provider}. This is a fresh native "
            "provider session because Claude and Codex session ids cannot cross runtimes. "
            "Re-read the shared checkout, preserve completed edits, and continue instead of restarting."
        )
    elif attempt.get("resume_kind") == "phase_continuation":
        resume = (
            "Continue in your durable Fleet session from the prior phase. Keep useful native "
            "context, but obey this phase's role and access contract exactly."
        )
    elif attempt.get("resume_session_id"):
        resume = "This is a native retry of your interrupted phase turn. Continue from its durable session."
    else:
        resume = "This is a fresh independent worker session."
    workspace_recovery = attempt.get("workspace_recovery")
    if isinstance(workspace_recovery, dict):
        recovery_action = str(workspace_recovery.get("action") or "")
        recovery_patch = str(workspace_recovery.get("patch_path") or "")
        if recovery_action == "reforked_reapplied":
            resume += (
                "\nFleet reforked this workspace from the latest combined checkout and "
                "cleanly reapplied your preserved patch. Re-read the merged files before editing."
            )
        elif recovery_action == "reforked_conflict":
            resume += (
                "\nFleet reforked this workspace from the latest combined checkout, but your "
                "preserved patch overlaps newer team work and was not applied. Reconcile it "
                "manually without overwriting the newer base. Preserved patch: "
                + recovery_patch
            )
    # A retried worker must know WHY the last attempt failed, or it repeats the
    # same defect verbatim. Provider handoffs already carry the prior attempt's
    # bounded output and error through handoff_context.
    if attempt.get("resume_kind") != "provider_handoff" and int(
        attempt.get("attempt_number") or 0
    ) > 1:
        prior_error = store.previous_attempt_error(
            str(leg["leg_id"]),
            before_attempt_number=int(attempt.get("attempt_number") or 0),
        )
        if prior_error:
            safe_prior, _prior_redactions = redact_text(prior_error)
            resume += (
                "\nYour previous attempt was rejected. Correct exactly this before "
                "finishing, then re-emit a compliant evidence envelope:\n"
                + safe_prior.strip()[:1_500]
            )
    steering_block, _steering_redactions = redact_text("\n\n".join(steering))
    steering_block = steering_block.strip() or "(none)"
    if len(steering_block) > MAX_STEERING_CONTEXT_CHARS:
        steering_block = steering_block[-MAX_STEERING_CONTEXT_CHARS:]
    final_phase = run["phases"][-1]
    is_parallel_final = final_phase.get("execution") == "parallel"
    final_contract = ""
    if phase == final_phase["name"] and (is_parallel_final or leg["role"] == "final-auditor"):
        final_contract = (
            "\nFinal output contract:\n"
            "- Finish your assigned fixes, inspect the combined checkout, and report the actual final state.\n"
            "- Include concrete verification and any unresolved conflict or limitation.\n"
            "- Do not pretend you observed final responses from any Fleet workers still running alongside you.\n"
        )
    role_contract = _role_contract(
        run["activity"],
        phase,
        leg["role"],
        solo=peer_count == 0,
        research_depth=str((run.get("policy") or {}).get("research_depth") or "full"),
    )
    worker_label = _worker_label(leg)
    assignment = _assignment_text(leg.get("assignment")) or "the shared task within your role"
    assignment_ids = _string_list(leg.get("assignment_ids"))
    roster = _team_roster(phase_record)
    review_targets = _review_targets(phase_record, leg)
    work_unit_contracts = _work_unit_contract_block(run, leg, phase=phase)
    review_heading = "Self-review scope" if peer_count == 0 else "Rotated review targets"
    review_diff = _review_diff_block(run, leg) if phase == "verify" else ""
    assigned_findings = (
        _findings_for_worker(store, run, leg)
        if phase == "finalize" and run["activity"] == "coding"
        else []
    )
    review_evidence_contract = ""
    if phase == "verify":
        reviewed = ", ".join(completion_unit_ids(leg, phase)) or "(shared)"
        review_evidence_contract = (
            "\nReview evidence scope:\n"
            "- Inspect the rotated review targets above and report their findings in prose.\n"
            "- The envelope must also carry a machine-readable findings list on each "
            "of your units: objects with unit_id (the reviewed unit), severity "
            "(blocker, major, or minor), summary, and evidence. Report an empty "
            "list when you found nothing; Fleet routes these to the Fix phase and "
            "skips a fixer with nothing to fix.\n"
            f"- The completion envelope must contain only the reviewed unit ids: {reviewed}.\n"
            "- Never substitute your owned assignment_ids for the reviewed unit ids and never omit the "
            "completion envelope, even when there are no findings.\n"
            "- Review is read-only, so report no changed paths.\n"
            "- You may rely on machine-accepted test receipts from the completed Code "
            "phase. If the read-only tool policy refuses a fresh shell command, that is "
            "not a user denial or a blocker: cite the accepted prior receipt, complete "
            "the static review, and still emit the envelope.\n"
        )
    handoff_block = "(none)"
    _partial_redactions = 0
    _error_redactions = 0
    if handoff_context:
        source_identity = " ".join(
            value
            for value in (
                _clean_inline(handoff_context.get("provider"), limit=32),
                _clean_inline(handoff_context.get("model"), limit=96),
                _clean_inline(handoff_context.get("effort"), limit=32),
            )
            if value
        )
        partial, _partial_redactions = redact_text(
            str(handoff_context.get("output_text") or "").strip()
        )
        error, _error_redactions = redact_text(str(handoff_context.get("error") or "").strip())
        handoff_parts = [f"Previous runtime: {source_identity or 'unknown'}"]
        if partial:
            handoff_parts.append("Previous attempt output:\n" + partial[:32_000])
        if error:
            handoff_parts.append("Handoff reason or failure:\n" + error[:4_000])
        handoff_block = "\n\n".join(handoff_parts)
    actual_working_directory = str(working_directory or run["cwd"])
    workspace_constraint = ""
    if actual_working_directory != str(run["cwd"]):
        workspace_constraint = (
            "- Work only inside the exact isolated Working directory below. "
            "Never cd to or edit the base checkout named in the task or persona paths.\n"
        )
    prompt = f"""You are one worker inside Serena Fleet.

Read /home/raghav/Documents/Projects/serena/Persona.md and Tooling.md before acting. Stay in that voice internally, but focus only on this assigned leg.

Hard constraints:
- Do not spawn subagents, teams, workflows, Fleet runs, or delegate this task.
- Do not call chats ask-claude or chats ask-codex.
- Fleet's supervisor owns path claims. Never edit Fleet databases or register/release claims yourself.
- Preserve unrelated dirty work.
- Never claim a model or test result you did not actually verify.
- Never use pkill, killall, or pattern-based process termination. Record the exact PID or process group for any process you start and stop only that exact process.
- Never block on one sleep or wait longer than 60 seconds; use bounded polling so the run remains observable.
- After pushing a branch, read remote CI once. If checks are still pending, record that exact state and finish the leg; never sleep or poll waiting for hosted CI, and never rerun an unchanged full local gate solely to duplicate an existing receipt.
- For a background server, verify the actual listener or surviving child process group after launch. Package-manager wrappers can exit or reparent children, so after stopping the recorded group, prove that every process and listener you started is gone; if a child survived, resolve and stop only its exact PID or process group.
{workspace_constraint}- {access}
- {resume}

Run: {run["run_id"]}
Activity: {run["activity"]}
Phase: {phase_display} ({phase})
Worker: {worker_label} ({worker_key})
Role: {leg["role"]}
Role contract: {role_contract}
Requested model contract: {leg["model"]} at {leg["effort"]}
Working directory: {actual_working_directory}

Your assignment:
{assignment}

Assignment ids: {", ".join(assignment_ids) or "(shared)"}

{review_heading}:
{review_targets}
{review_evidence_contract}
Integrated changes under review (the exact patches Fleet applied):
{review_diff or "(no integrated patch is recorded; inspect the checkout directly)"}

Review findings assigned to you:
{_findings_block(assigned_findings) if phase == "finalize" and run["activity"] == "coding" else "(not applicable in this phase)"}

Durable work-unit contract:
{work_unit_contracts}

Full Fleet roster:
{roster}

Authenticated read-only tools:
{read_mcp or "(none; this leg has no account access beyond the checkout)"}

Original task:
{run["task"]}

Steering received for future work:
{steering_block}

Prior-phase peer outputs (the collaboration barrier):
{context or "(no earlier phase output)"}
{fresh_session_note}

Provider handoff context:
{handoff_block}
{final_contract}

Do this leg now. Return a concise, evidence-based result for the next Fleet phase.
"""
    safe_prompt, prompt_redactions = redact_text(prompt)
    receipt = context_receipt.to_dict()
    receipt["redaction_count"] = (
        int(receipt["redaction_count"])
        + _steering_redactions
        + _partial_redactions
        + _error_redactions
        + prompt_redactions
    )
    store.record_context_receipt(attempt["attempt_id"], receipt)
    return safe_prompt


def _phase_for_leg(run: dict[str, Any], leg_id: str) -> str:
    return str(_phase_record_for_leg(run, leg_id)["name"])


def _phase_record_for_leg(run: dict[str, Any], leg_id: str) -> dict[str, Any]:
    for phase in run["phases"]:
        if any(leg["leg_id"] == leg_id for leg in phase["legs"]):
            return phase
    raise KeyError(f"Fleet leg {leg_id} is absent from run")


def _role_contract(
    activity: str,
    phase: str,
    role: str,
    *,
    solo: bool = False,
    research_depth: str = "full",
) -> str:
    # Kept in the signature for persisted-call compatibility. Full online
    # research is now the only mandate, including legacy retries.
    del research_depth
    # The phase matrix moves every agent to a different provider between phases,
    # so Code starts in a session that never saw Research. Prose alone makes it
    # search the repository again for things Research already located, which was
    # the single largest input-token cost measured on a real Research leg. The
    # read map turns that rediscovery into a lookup.
    read_map = (
        "\n\nEnd your report with a section headed 'Read map': one line per file "
        "region you actually opened, as `path:start-end` followed by what is "
        "there and why the next phase will care. This is mandatory. The worker "
        "that implements this unit runs on a different provider in a fresh "
        "session and cannot see anything you did, so an unlisted file is a file "
        "it has to hunt for again."
    )
    coding_discovery = (
        "Research the codebase, constraints, and risks without editing. Also perform "
        "extensive current online research for this exact work unit: official guidance, "
        "best practices, recent technology, alternatives, and known failure modes. "
        "Produce attributable evidence and a concrete implementation plan."
    )
    if activity == "coding":
        contracts = {
            "discover": coding_discovery + read_map,
            "execute": "Code your assigned implementation slice now, using the prior-phase team handoff. Do not turn this phase into review-only work.",
            "verify": (
                "Review your completed implementation in a distinct self-review pass. Give numbered "
                "findings with severity, file and line, and proof; do not fix them."
                if solo
                else "Review the completed combined implementation and your rotated targets independently. "
                "Give numbered findings with severity, file and line, and proof; do not fix them."
            ),
            "finalize": "Fix the prior review findings in your assigned surface, preserve team edits, and run focused verification.",
        }
    else:
        contracts = {
            "discover": (
                "Gather complementary primary evidence and identify unknowns without changing "
                "external state. Perform extensive current online research for this exact work "
                "unit, including official guidance, best practices, recent technology, "
                "alternatives, and known failure modes."
            )
            + read_map,
            "execute": "Develop a rigorous answer from the gathered evidence in your response, using the prior-phase team handoff.",
            "verify": "Review claims and sources independently; report unsupported or overstated conclusions without editing files.",
            "finalize": "Refine the deliverable using the prior reviews and state remaining uncertainty precisely.",
        }
    return f"{contracts[phase]} Your fleet role is {role}."


def _combine_worker_outputs(outputs: list[dict[str, str]]) -> str:
    sections: list[str] = []
    for output in outputs:
        text = str(output.get("output_text") or "").strip()
        if not text:
            continue
        worker = (
            output.get("worker_label")
            or output.get("worker_key")
            or output.get("runtime", "worker")
        )
        identity = f"{worker} / {output.get('role', 'collaborator')}"
        sections.append(f"[{identity}]\n{text}")
    return "\n\n".join(sections)


def _refresh_index() -> None:
    with _INDEX_LOCK, suppress(Exception):
        from core.indexer import update_index

        update_index()


@contextmanager
def _coding_run_lock(run: dict[str, Any]) -> Iterator[None]:
    if run["activity"] != "coding" or os.name == "nt":
        yield
        return
    import fcntl

    digest = hashlib.sha256(str(Path(run["cwd"]).resolve()).encode("utf-8")).hexdigest()[:24]
    configured_state = os.environ.get("SERENA_FLEET_STATE_DIR", "").strip()
    configured_db = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
    if configured_state:
        state_root = Path(configured_state).expanduser()
    elif configured_db:
        state_root = Path(configured_db).expanduser().parent / "fleet"
    else:
        state_root = Path.home() / ".local" / "state" / "serena" / "fleet"
    lock_root = state_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _store().run_cancel_requested(str(run["run_id"])):
                    raise RuntimeError(
                        "Fleet run cancelled while waiting for the coding lock"
                    ) from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _wake_or_launch(run_id: str) -> bool:
    if os.environ.get("SERENA_FLEET_NO_AUTOSTART", "").lower() in {"1", "true", "on"}:
        return False
    if os.environ.get("SERENA_FLEET_INLINE", "").lower() in {"1", "true", "on"}:
        run_supervisor(run_id)
        return True
    systemctl = shutil.which("systemctl")
    if systemctl:
        with suppress(subprocess.SubprocessError, OSError):
            result = subprocess.run(
                [systemctl, "--user", "start", "serena-fleet.service"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return True
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    subprocess.Popen(
        [sys.executable, "-m", "fleet.supervisor", "--run", run_id],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def _service_active() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        result = subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", "serena-fleet.service"],
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _resolve_cwd(value: str | None) -> Path:
    candidate = Path(value).expanduser() if value else Path.cwd()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Fleet cwd does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"Fleet cwd is not a directory: {resolved}")
    return resolved


def _store() -> FleetStore:
    global _STORE_INSTANCE

    configured = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_DB_PATH
    key = str(path)
    with _STORE_LOCK:
        if _STORE_INSTANCE is None or _STORE_INSTANCE[0] != key:
            _STORE_INSTANCE = (key, FleetStore(path))
        return _STORE_INSTANCE[1]


def _require_id(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("Fleet run_id is required")
    return clean


def _clean_optional(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean or None


def _resolve_origin(
    session_id: str | None,
    agent: str | None,
) -> tuple[str | None, str | None]:
    return resolve_origin_session(session_id, agent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serena Fleet supervisor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", help="execute one persisted Fleet run")
    group.add_argument("--serve", action="store_true", help="serve queued Fleet runs forever")
    args = parser.parse_args(argv)
    if args.run:
        result = run_supervisor(args.run)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] in {"completed", "planned"} else 1
    stopper = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopper.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    serve_forever(stop_event=stopper)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
