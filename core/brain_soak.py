"""Twenty-four hour acceptance harness for the resident Serena brain.

The harness samples daemon, SDK child, context, journal, lifetime, and JSONL
state once a minute. At hours 0, 6, 12, 18, and 24 it writes a unique marker
on one turn and asks for it on the next. The six-hour epoch policy therefore
forces the marker through a clean-session handoff before the recall check.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.brain_lifetime import append_json_line, secure_directory, write_json_atomic

DEFAULT_DISCOVERY = Path.home() / ".config" / "serena" / "brain.json"
DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "state" / "serena" / "soaks"
MIB = 1024 * 1024


class SoakError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SoakConfig:
    duration_seconds: float
    sample_seconds: float
    checkpoints_seconds: tuple[float, ...]
    discovery_path: Path
    events_path: Path
    summary_path: Path

    @property
    def eligible_24h(self) -> bool:
        return self.duration_seconds >= 24 * 60 * 60


class SoakLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> SoakLock:
        secure_directory(self.path.parent)
        descriptor = None
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                break
            except FileExistsError as exc:
                owner = "unknown"
                inode = None
                try:
                    inode = self.path.stat().st_ino
                    owner = self.path.read_text(encoding="utf-8").strip() or owner
                    owner_pid = int(owner)
                except (OSError, ValueError):
                    owner_pid = None
                if owner_pid is not None and _pid_alive(owner_pid):
                    raise SoakError(f"another brain soak owns {self.path} (pid {owner})") from exc
                try:
                    if inode is not None and self.path.stat().st_ino == inode:
                        self.path.unlink()
                except OSError:
                    pass
        if descriptor is None:
            raise SoakError(f"could not acquire brain soak lock {self.path}")
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.acquired = True
        return self

    def __exit__(self, *_args) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoakError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SoakError(f"expected a JSON object in {path}")
    return value


def _discovery(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    try:
        port = int(value["port"])
        token = str(value["token"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SoakError("brain discovery is missing port or token") from exc
    if not token or not 1 <= port <= 65535:
        raise SoakError("brain discovery contains an invalid port or token")
    return {"port": port, "token": token}


def _request(
    discovery_path: Path,
    *,
    method: str,
    route: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    discovery = _discovery(discovery_path)
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if route == "/turn":
        headers["Authorization"] = f"Bearer {discovery['token']}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{discovery['port']}{route}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SoakError(f"brain {route} request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise SoakError(f"brain {route} returned a non-object response")
    return value


def fetch_health(discovery_path: Path) -> dict[str, Any]:
    health = _request(
        discovery_path,
        method="GET",
        route="/health",
        timeout=15,
    )
    if not health.get("ok"):
        raise SoakError(f"brain health is not ok: {health.get('error') or health}")
    return health


def wait_for_brain(discovery_path: Path, *, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "brain has not answered"
    while time.monotonic() < deadline:
        try:
            health = fetch_health(discovery_path)
            lifetime = health.get("lifetime") or {}
            if lifetime.get("session_id") and not lifetime.get("rotation_in_progress"):
                return health
            last_error = "brain lifetime is not ready"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise SoakError(f"brain was not ready within {timeout:.0f}s: {last_error}")


def send_turn(
    discovery_path: Path,
    text: str,
    *,
    protocol: str = "plain",
    soak_force_rotation: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"protocol": protocol, "text": text}
    if soak_force_rotation:
        payload["soak_force_rotation"] = True
    response = _request(
        discovery_path,
        method="POST",
        route="/turn",
        payload=payload,
        timeout=180,
    )
    if not response.get("ok"):
        raise SoakError(f"brain turn failed: {response.get('error') or response}")
    return response


def wait_for_rotation(
    discovery_path: Path,
    *,
    session_before: str,
    rotations_before: int,
    timeout: float = 120.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_health = fetch_health(discovery_path)
        lifetime = last_health.get("lifetime") or {}
        if (
            lifetime.get("session_id")
            and lifetime.get("session_id") != session_before
            and int(lifetime.get("rotations") or 0) > rotations_before
            and not lifetime.get("rotation_in_progress")
        ):
            return last_health
        time.sleep(0.25)
    raise SoakError(
        "forced checkpoint rotation did not complete within "
        f"{timeout:.0f}s; last lifetime={last_health.get('lifetime')}"
    )


def run_continuity_checkpoint(
    discovery_path: Path,
    *,
    checkpoint_index: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    marker = f"serena-soak-{checkpoint_index}-{secrets.token_hex(6)}"
    before = fetch_health(discovery_path)
    before_lifetime = before.get("lifetime") or {}
    session_before = str(before_lifetime.get("session_id") or "")
    rotations_before = int(before_lifetime.get("rotations") or 0)
    if not session_before:
        raise SoakError("checkpoint cannot identify the active resident session")
    stored = send_turn(
        discovery_path,
        "For the resident-session soak, remember this marker for my immediately "
        f"next turn: {marker}. Reply exactly: stored {marker}",
        protocol="soak",
        soak_force_rotation=True,
    )
    after_store = wait_for_rotation(
        discovery_path,
        session_before=session_before,
        rotations_before=rotations_before,
    )
    recalled = send_turn(
        discovery_path,
        "What was the resident-session soak marker from my immediately previous "
        f"turn? Reply exactly with that marker. Expected format starts {marker[:14]}.",
    )
    after = fetch_health(discovery_path)
    stored_text = str(stored.get("say") or "")
    recalled_text = str(recalled.get("say") or "")
    session_after_store = str((after_store.get("lifetime") or {}).get("session_id") or "")
    session_after_recall = str((after.get("lifetime") or {}).get("session_id") or "")
    cross_rotation = bool(
        session_before
        and session_after_store
        and session_before != session_after_store
        and session_after_recall == session_after_store
    )
    stored_ok = marker.lower() in stored_text.lower()
    recalled_ok = marker.lower() in recalled_text.lower()
    return {
        "type": "checkpoint",
        "at": time.time(),
        "elapsed_seconds": elapsed_seconds,
        "checkpoint_index": checkpoint_index,
        "marker": marker,
        "ok": stored_ok and recalled_ok and cross_rotation,
        "stored_ok": stored_ok,
        "recalled_ok": recalled_ok,
        "cross_rotation": cross_rotation,
        "session_before": session_before,
        "session_after_store": session_after_store,
        "session_after": session_after_recall,
        "rotations_before": rotations_before,
        "rotations_after_store": (after_store.get("lifetime") or {}).get("rotations"),
        "rotations_after": (after.get("lifetime") or {}).get("rotations"),
        "response_elapsed": recalled.get("elapsed"),
    }


def _health_event(discovery_path: Path, *, elapsed_seconds: float) -> dict[str, Any]:
    try:
        health = fetch_health(discovery_path)
    except Exception as exc:
        return {
            "type": "health",
            "at": time.time(),
            "elapsed_seconds": elapsed_seconds,
            "ok": False,
            "error": str(exc),
        }
    return {
        "type": "health",
        "at": time.time(),
        "elapsed_seconds": elapsed_seconds,
        "ok": True,
        "health": health,
    }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _path_value(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def analyze_events(
    events: list[dict[str, Any]],
    *,
    duration_seconds: float,
    sample_seconds: float,
    checkpoints_seconds: tuple[float, ...],
) -> dict[str, Any]:
    if duration_seconds <= 0 or sample_seconds <= 0:
        raise ValueError("duration and sample interval must be positive")
    health_events = [row for row in events if row.get("type") == "health"]
    malformed_health = [
        row
        for row in health_events
        if row.get("ok") and not isinstance(row.get("health"), dict)
    ]
    successful_health = [
        row
        for row in health_events
        if row.get("ok") and isinstance(row.get("health"), dict)
    ]
    checkpoints = [row for row in events if row.get("type") == "checkpoint"]
    errors = [row for row in events if row.get("type") == "error"]
    starts = [row for row in events if row.get("type") == "run.start"]
    stops = [row for row in events if row.get("type") == "run.stop"]
    failures: list[str] = []

    event_elapsed = [_number(row.get("elapsed_seconds")) for row in events]
    event_at = [_number(row.get("at")) for row in events]
    elapsed_ordered = all(
        current is not None and previous is not None and current >= previous
        for previous, current in zip(event_elapsed, event_elapsed[1:], strict=False)
    )
    wall_ordered = all(
        current is not None and previous is not None and current >= previous
        for previous, current in zip(event_at, event_at[1:], strict=False)
    )
    events_complete = all(value is not None for value in event_elapsed) and all(
        value is not None for value in event_at
    )
    structure_valid = bool(
        events
        and len(starts) == 1
        and len(stops) == 1
        and events_complete
        and elapsed_ordered
        and wall_ordered
    )
    start_at = _number(starts[0].get("at")) if len(starts) == 1 else None
    stop_at = _number(stops[0].get("at")) if len(stops) == 1 else None
    stop_elapsed = _number(stops[0].get("elapsed_seconds")) if len(stops) == 1 else None
    observed_seconds = stop_elapsed or 0.0
    eligible_24h = duration_seconds >= 24 * 60 * 60
    wall_duration = stop_at - start_at if start_at is not None and stop_at is not None else None
    wall_alignment_tolerance = max(300.0, sample_seconds * 5)
    wall_aligned = bool(
        structure_valid
        and start_at is not None
        and stop_at is not None
        and wall_duration is not None
        and wall_duration >= duration_seconds * 0.99
        and all(
            abs((float(at) - start_at) - float(elapsed)) <= wall_alignment_tolerance
            for row in events
            if (at := _number(row.get("at"))) is not None
            and (elapsed := _number(row.get("elapsed_seconds"))) is not None
        )
    )
    duration_complete = bool(
        structure_valid
        and stop_elapsed is not None
        and stop_elapsed >= duration_seconds * 0.999
        and not bool(stops[0].get("interrupted"))
        and wall_aligned
    )

    expected_samples = max(1, int(duration_seconds // sample_seconds) + 1)
    covered_slots: set[int] = set()
    successful_elapsed = sorted(
        elapsed
        for row in successful_health
        if (elapsed := _number(row.get("elapsed_seconds"))) is not None
    )
    for elapsed in successful_elapsed:
        slot = int(round(elapsed / sample_seconds))
        if (
            0 <= slot < expected_samples
            and abs(elapsed - slot * sample_seconds) <= sample_seconds * 0.75
        ):
            covered_slots.add(slot)
    sample_coverage = len(covered_slots) / expected_samples
    sample_gaps = [
        current - previous
        for previous, current in zip(successful_elapsed, successful_elapsed[1:], strict=False)
    ]
    temporal_samples = bool(
        successful_elapsed
        and successful_elapsed[0] <= sample_seconds * 1.5
        and successful_elapsed[-1] >= duration_seconds - sample_seconds * 1.5
        and (not sample_gaps or max(sample_gaps) <= sample_seconds * 2.5)
        and len(set(successful_elapsed)) == len(successful_elapsed)
    )
    temporal_evidence = structure_valid and wall_aligned and temporal_samples
    if not structure_valid:
        failures.append("soak artifact is missing one ordered start or stop event")
    if not wall_aligned:
        failures.append("soak wall-clock and elapsed evidence do not align")
    if not duration_complete:
        failures.append("configured duration did not complete")
    if sample_coverage < 0.99:
        failures.append("health sample coverage fell below 99 percent")
    if not temporal_samples:
        failures.append("health samples are not distributed across the soak duration")
    if any(not row.get("ok") for row in health_events):
        failures.append("one or more health samples failed")
    if malformed_health:
        failures.append("one or more successful health samples are malformed")
    if errors:
        failures.append("the harness recorded runtime errors")

    health_values = [row["health"] for row in successful_health]
    pids = {value.get("pid") for value in health_values if value.get("pid")}
    daemon_restart_free = len(pids) == 1
    if not daemon_restart_free:
        failures.append("daemon pid was missing or changed during the soak")

    sessions = []
    for value in health_values:
        session_id = _path_value(value, "lifetime", "session_id")
        if session_id and session_id not in sessions:
            sessions.append(session_id)
    rotation_proven = len(sessions) >= 2
    if not rotation_proven:
        failures.append("no clean resident-session rotation was observed")

    checkpoint_by_index = {
        int(row["checkpoint_index"]): row
        for row in checkpoints
        if isinstance(row.get("checkpoint_index"), int)
    }
    checkpoint_tolerance = max(300.0, sample_seconds * 2)
    checkpoint_ok = bool(
        len(checkpoints) == len(checkpoints_seconds)
        and len(checkpoint_by_index) == len(checkpoints_seconds)
        and all(
            index in checkpoint_by_index
            and checkpoint_by_index[index].get("ok")
            and checkpoint_by_index[index].get("stored_ok")
            and checkpoint_by_index[index].get("recalled_ok")
            and checkpoint_by_index[index].get("cross_rotation")
            and (elapsed := _number(checkpoint_by_index[index].get("elapsed_seconds")))
            is not None
            and elapsed >= scheduled * 0.999
            and elapsed <= scheduled + checkpoint_tolerance
            for index, scheduled in enumerate(checkpoints_seconds)
        )
    )
    if not checkpoint_ok:
        failures.append("one or more continuity checkpoints failed or are missing")
    handoff_checkpoint_proven = bool(checkpoints) and all(
        row.get("ok")
        and row.get("cross_rotation")
        and row.get("session_before")
        and row.get("session_after_store")
        and row.get("session_after") == row.get("session_after_store")
        and row.get("session_before") != row.get("session_after_store")
        for row in checkpoints
    )
    if not handoff_checkpoint_proven:
        failures.append("no continuity marker was recalled across a clean rotation")
    final_checkpoint = max(
        checkpoints,
        key=lambda row: _number(row.get("elapsed_seconds")) or 0,
        default=None,
    )
    hour24_thread_awareness = bool(
        eligible_24h
        and final_checkpoint
        and final_checkpoint.get("ok")
        and final_checkpoint.get("cross_rotation")
        and final_checkpoint.get("recalled_ok")
        and final_checkpoint.get("session_before")
        != final_checkpoint.get("session_after_store")
        and (_number(final_checkpoint.get("elapsed_seconds")) or 0) >= 24 * 60 * 60 * 0.999
    )
    if eligible_24h and not hour24_thread_awareness:
        failures.append("hour 24 thread-awareness checkpoint did not pass")

    rss_values: list[int] = []
    child_values = []
    for item in health_values:
        process = _path_value(item, "lifetime", "process") or {}
        if not isinstance(process, dict):
            continue
        rss = process.get("rss_bytes")
        if rss is None:
            continue
        rss_values.append(int(rss))
        root_pid = process.get("root_pid")
        child_values.append(
            sum(
                int(row.get("rss_bytes") or 0)
                for row in process.get("processes", [])
                if isinstance(row, dict)
                if row.get("pid") != root_pid
            )
        )
    stable_memory = False
    if rss_values and len(rss_values) == len(health_values) == len(child_values):
        total_limit = max(rss_values[0] + 256 * MIB, rss_values[0] * 2)
        child_bounds_ok = []
        for item, child_value in zip(health_values, child_values, strict=True):
            child_baseline = _number(_path_value(item, "lifetime", "baseline_child_rss_bytes"))
            policy_growth = (
                _number(
                    _path_value(
                        item,
                        "lifetime",
                        "policy",
                        "max_child_rss_growth_bytes",
                    )
                )
                or 256 * MIB
            )
            policy_multiplier = (
                _number(
                    _path_value(
                        item,
                        "lifetime",
                        "policy",
                        "max_child_rss_multiplier",
                    )
                )
                or 2
            )
            child_limit = (
                max(
                    child_baseline + policy_growth,
                    child_baseline * policy_multiplier,
                )
                if child_baseline
                else None
            )
            child_bounds_ok.append(child_limit is not None and child_value <= child_limit)
        stable_memory = max(rss_values) <= total_limit and all(child_bounds_ok)
    if not stable_memory:
        failures.append("resident process memory did not stay within policy bounds")

    no_orphans = False
    jsonl_bounded = False
    if health_values:
        final_health = health_values[-1]
        ledger = _path_value(final_health, "lifetime", "ledger") or {}
        if not isinstance(ledger, dict):
            ledger = {}
        raw_epochs = ledger.get("epochs") or []
        epochs = [row for row in raw_epochs if isinstance(row, dict)]
        epochs_well_formed = len(epochs) == len(raw_epochs)
        retired = epochs[:-1] if len(epochs) >= 2 else []
        active_epoch = epochs[-1] if epochs else {}
        retired_closed = bool(
            retired
            and all(row.get("ended_at") and row.get("end_reason") for row in retired)
            and active_epoch.get("ended_at") is None
        )
        epoch_sessions = [row.get("session_id") for row in epochs]
        epoch_tokens = [row.get("process_token") for row in epochs]
        epoch_identity_valid = bool(
            epochs
            and epochs_well_formed
            and all(epoch_sessions)
            and all(epoch_tokens)
            and len(set(epoch_sessions)) == len(epoch_sessions)
            and len(set(epoch_tokens)) == len(epoch_tokens)
        )
        stable_health = [
            item
            for item in health_values
            if not _path_value(item, "lifetime", "rotation_in_progress")
        ]
        sdk_process_evidence = bool(stable_health) and all(
            isinstance((sdk := _path_value(item, "lifetime", "sdk_processes")), dict)
            and sdk.get("available")
            and int(sdk.get("active_processes") or 0) >= 1
            and int(sdk.get("stale_processes") or 0) == 0
            and int(sdk.get("token_count") or 0) == 1
            and sdk.get("tokens")
            == [_path_value(item, "lifetime", "process_token")]
            for item in stable_health
        )
        attached_sdk = bool(stable_health) and all(
            int(_path_value(item, "lifetime", "process", "descendants") or 0) >= 1
            for item in stable_health
        )
        current_jsonl = _path_value(final_health, "lifetime", "session_store", "current")
        rotations_recorded = int(_path_value(final_health, "lifetime", "rotations") or 0) >= 1
        no_orphans = bool(
            retired_closed
            and epoch_identity_valid
            and rotations_recorded
            and sdk_process_evidence
            and attached_sdk
            and current_jsonl
        )
        first_store = _path_value(health_values[0], "lifetime", "session_store") or {}
        final_store = _path_value(final_health, "lifetime", "session_store") or {}
        file_growth = int(final_store.get("jsonl_files") or 0) - int(
            first_store.get("jsonl_files") or 0
        )
        jsonl_bounded = file_growth == max(0, len(sessions) - 1)
        journals_clean = bool(stable_health) and all(
            _path_value(item, "lifetime", "journal", "pending_entries") == 0
            and _path_value(
                item,
                "lifetime",
                "journal",
                "transport_uncertain_entries",
            )
            == 0
            for item in stable_health
        )
        no_orphans = no_orphans and journals_clean
        if not no_orphans:
            failures.append("retired session or SDK process state is orphaned")
        if not jsonl_bounded:
            failures.append("session JSONL count grew faster than clean rotations")

    runtime_errors = [
        _path_value(value, "lifetime", "fatal_error")
        or _path_value(value, "lifetime", "last_error")
        for value in health_values
    ]
    runtime_errors = [str(value) for value in runtime_errors if value]
    if runtime_errors:
        failures.append("daemon lifetime telemetry reported errors")

    criteria = {
        "eligible_24h": eligible_24h,
        "duration_complete": duration_complete,
        "sample_coverage": round(sample_coverage, 4),
        "temporal_evidence": temporal_evidence,
        "daemon_restart_free": daemon_restart_free,
        "rotation_proven": rotation_proven,
        "continuity_checkpoints": checkpoint_ok,
        "cross_rotation_handoff": handoff_checkpoint_proven,
        "hour24_thread_awareness": hour24_thread_awareness,
        "stable_memory": stable_memory,
        "no_orphaned_sessions": no_orphans,
        "jsonl_growth_bounded": jsonl_bounded,
        "lifetime_errors_absent": not runtime_errors,
    }
    passed = eligible_24h and not failures and all(criteria.values())
    return {
        "status": "pass" if passed else ("fail" if eligible_24h else "inconclusive"),
        "passed": passed,
        "criteria": criteria,
        "failures": list(dict.fromkeys(failures)),
        "metrics": {
            "configured_seconds": duration_seconds,
            "observed_seconds": round(observed_seconds, 3),
            "wall_seconds": round(wall_duration, 3) if wall_duration is not None else None,
            "covered_sample_slots": len(covered_slots),
            "expected_sample_slots": expected_samples,
            "max_health_gap": round(max(sample_gaps), 3) if sample_gaps else None,
            "health_samples": len(health_events),
            "successful_health_samples": len(successful_health),
            "checkpoints": len(checkpoints),
            "sessions": sessions,
            "daemon_pids": sorted(pids),
            "rss_start": rss_values[0] if rss_values else None,
            "rss_final": rss_values[-1] if rss_values else None,
            "rss_peak": max(rss_values) if rss_values else None,
            "child_rss_final": child_values[-1] if child_values else None,
            "child_rss_peak": max(child_values) if child_values else None,
            "runtime_errors": runtime_errors,
        },
    }


def _progress_summary(
    events: list[dict[str, Any]], config: SoakConfig, *, running: bool
) -> dict[str, Any]:
    analysis = analyze_events(
        events,
        duration_seconds=config.duration_seconds,
        sample_seconds=config.sample_seconds,
        checkpoints_seconds=config.checkpoints_seconds,
    )
    if running:
        analysis["status"] = "running"
        analysis["passed"] = False
    analysis["events_path"] = str(config.events_path)
    analysis["summary_path"] = str(config.summary_path)
    analysis["updated_at"] = time.time()
    return analysis


def run_soak(config: SoakConfig) -> dict[str, Any]:
    stop_requested = False

    def request_stop(*_args) -> None:
        nonlocal stop_requested
        stop_requested = True

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    events: list[dict[str, Any]] = []
    start_wall = time.time()
    start_monotonic = time.monotonic()
    start_event = {
        "type": "run.start",
        "at": start_wall,
        "elapsed_seconds": 0.0,
        "pid": os.getpid(),
        "duration_seconds": config.duration_seconds,
        "sample_seconds": config.sample_seconds,
        "checkpoints_seconds": list(config.checkpoints_seconds),
    }
    events.append(start_event)
    append_json_line(config.events_path, start_event)
    next_sample = 0.0
    checkpoint_index = 0

    while not stop_requested:
        elapsed = time.monotonic() - start_monotonic
        while (
            checkpoint_index < len(config.checkpoints_seconds)
            and elapsed >= config.checkpoints_seconds[checkpoint_index]
        ):
            try:
                event = run_continuity_checkpoint(
                    config.discovery_path,
                    checkpoint_index=checkpoint_index,
                    elapsed_seconds=elapsed,
                )
            except Exception as exc:
                event = {
                    "type": "error",
                    "stage": "checkpoint",
                    "checkpoint_index": checkpoint_index,
                    "at": time.time(),
                    "elapsed_seconds": elapsed,
                    "error": str(exc),
                }
            events.append(event)
            append_json_line(config.events_path, event)
            checkpoint_index += 1
            elapsed = time.monotonic() - start_monotonic

        if elapsed >= next_sample:
            event = _health_event(
                config.discovery_path,
                elapsed_seconds=elapsed,
            )
            events.append(event)
            append_json_line(config.events_path, event)
            next_sample = elapsed + config.sample_seconds
            write_json_atomic(
                config.summary_path,
                _progress_summary(events, config, running=True),
            )

        checkpoints_done = checkpoint_index >= len(config.checkpoints_seconds)
        if elapsed >= config.duration_seconds and checkpoints_done:
            break
        deadlines = [next_sample, config.duration_seconds]
        if not checkpoints_done:
            deadlines.append(config.checkpoints_seconds[checkpoint_index])
        wait_seconds = max(0.05, min(deadlines) - (time.monotonic() - start_monotonic))
        time.sleep(min(wait_seconds, 1.0))

    finish_event = {
        "type": "run.stop",
        "at": time.time(),
        "elapsed_seconds": time.monotonic() - start_monotonic,
        "interrupted": stop_requested,
    }
    events.append(finish_event)
    append_json_line(config.events_path, finish_event)
    summary = _progress_summary(events, config, running=False)
    if stop_requested:
        summary["status"] = "interrupted"
        summary["passed"] = False
    write_json_atomic(config.summary_path, summary)
    return summary


def load_events(path: Path) -> list[dict[str, Any]]:
    events = []
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SoakError(f"cannot read soak events: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SoakError(f"bad JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise SoakError(f"expected an object at {path}:{line_number}")
        events.append(value)
    return events


def _float_list(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    if not values or any(value < 0 for value in values):
        raise argparse.ArgumentTypeError("checkpoint values must be non-negative")
    if tuple(sorted(set(values))) != values:
        raise argparse.ArgumentTypeError("checkpoint values must be unique and sorted")
    return values


def _default_paths(output_dir: Path) -> tuple[Path, Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (
        output_dir / f"brain-soak-{stamp}.jsonl",
        output_dir / f"brain-soak-{stamp}-summary.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--sample-seconds", type=float, default=60.0)
    parser.add_argument("--checkpoint-hours", type=_float_list, default=(0, 6, 12, 18, 24))
    parser.add_argument("--checkpoint-seconds", type=_float_list)
    parser.add_argument("--analyze", type=Path)
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    if args.analyze:
        events = load_events(args.analyze)
        start = next((row for row in events if row.get("type") == "run.start"), {})
        summary = analyze_events(
            events,
            duration_seconds=float(start.get("duration_seconds") or 0),
            sample_seconds=float(start.get("sample_seconds") or 60),
            checkpoints_seconds=tuple(start.get("checkpoints_seconds") or ()),
        )
        print(json.dumps(summary, indent=2))
        return 0 if summary["passed"] or not args.require_pass else 1

    duration_seconds = (
        args.duration_seconds
        if args.duration_seconds is not None
        else args.duration_hours * 60 * 60
    )
    checkpoints_seconds = (
        args.checkpoint_seconds
        if args.checkpoint_seconds is not None
        else tuple(value * 60 * 60 for value in args.checkpoint_hours)
    )
    if duration_seconds <= 0 or args.sample_seconds <= 0:
        parser.error("duration and sample interval must be positive")
    if args.ready_timeout <= 0:
        parser.error("ready timeout must be positive")
    if checkpoints_seconds[-1] > duration_seconds:
        parser.error("the last checkpoint cannot exceed the duration")
    default_events, default_summary = _default_paths(args.output_dir.expanduser())
    config = SoakConfig(
        duration_seconds=duration_seconds,
        sample_seconds=args.sample_seconds,
        checkpoints_seconds=checkpoints_seconds,
        discovery_path=args.discovery.expanduser().resolve(),
        events_path=(args.events or default_events).expanduser().resolve(),
        summary_path=(args.summary or default_summary).expanduser().resolve(),
    )
    lock_path = config.summary_path.parent / "brain-soak.lock"
    try:
        with SoakLock(lock_path):
            wait_for_brain(config.discovery_path, timeout=args.ready_timeout)
            summary = run_soak(config)
    except SoakError as exc:
        print(f"brain soak: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] or not args.require_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
