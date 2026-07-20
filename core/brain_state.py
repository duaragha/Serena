"""Canonical active-state snapshots for the resident brain.

The laptop memory files remain the source of truth. A brain running on another
machine reads an atomic, validated snapshot instead of a deployment-time copy
of ``memory/``. This module deliberately carries only active ledgers, tasks,
and loops. It never carries chat text, credentials, or raw voice data.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1
SNAPSHOT_ENV = "SERENA_BRAIN_STATE_SNAPSHOT"
DEFAULT_SNAPSHOT_PATH = (
    Path.home() / ".config" / "serena" / "canonical_state.json"
)
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_ACTIVE_TYPES = {"task", "ledger", "loop"}
_LEDGER_FIELDS = ("goal", "facts", "decision", "promise", "risk", "next_action")
_RECORD_FIELDS = (
    "id",
    "type",
    "content",
    "ledger_key",
    "goal",
    "facts",
    "decision",
    "promise",
    "risk",
    "next_action",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class ActiveState:
    records: tuple[dict[str, Any], ...]
    source: str
    generated_at: str = ""
    state_hash: str = ""
    source_host: str = ""
    error: str = ""


def snapshot_path() -> Path | None:
    """Return the configured snapshot path, or None for local canonical state."""
    configured = os.environ.get(SNAPSHOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else None


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key in _RECORD_FIELDS:
        value = record.get(key)
        if key == "id":
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                cleaned[key] = 0
        else:
            cleaned[key] = str(value or "")
    return cleaned


def _sort_key(record: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(record.get("type") or ""),
        int(record.get("id") or 0),
        str(record.get("ledger_key") or ""),
    )


def _records_hash(records: Iterable[dict[str, Any]]) -> str:
    encoded = json.dumps(
        list(records),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def local_active_records() -> tuple[dict[str, Any], ...]:
    """Read the source-of-truth memory files and return active public fields."""
    from memory.store import _is_snoozed, _scan_all

    records = [
        _clean_record(record)
        for record in _scan_all()
        if record.get("type") in _ACTIVE_TYPES and not _is_snoozed(record)
    ]
    records.sort(key=_sort_key)
    return tuple(records)


def build_snapshot(
    records: Iterable[dict[str, Any]] | None = None,
    *,
    generated_at: str | None = None,
    source_host: str | None = None,
) -> dict[str, Any]:
    cleaned = [
        _clean_record(record)
        for record in (local_active_records() if records is None else records)
        if record.get("type") in _ACTIVE_TYPES
    ]
    cleaned.sort(key=_sort_key)
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_host": source_host or socket.gethostname(),
        "state_hash": _records_hash(cleaned),
        "records": cleaned,
    }


def encode_snapshot(snapshot: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def atomic_write_snapshot(snapshot: dict[str, Any], path: Path) -> None:
    """Write a local snapshot with user-only permissions and atomic replace."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encode_snapshot(snapshot))
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp.unlink(missing_ok=True)


def _validate_snapshot(payload: Any) -> ActiveState:
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be an object")
    if payload.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported snapshot version {payload.get('version')!r}")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("snapshot records must be a list")
    records: list[dict[str, Any]] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise ValueError("snapshot record must be an object")
        record = _clean_record(raw)
        if record["type"] not in _ACTIVE_TYPES:
            raise ValueError(f"snapshot contains unsupported type {record['type']!r}")
        records.append(record)
    records.sort(key=_sort_key)
    expected_hash = str(payload.get("state_hash") or "")
    actual_hash = _records_hash(records)
    if not expected_hash or not secrets_compare(expected_hash, actual_hash):
        raise ValueError("snapshot state hash does not match its records")
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        raise ValueError("snapshot generated_at is missing")
    return ActiveState(
        records=tuple(records),
        source="snapshot",
        generated_at=generated_at,
        state_hash=actual_hash,
        source_host=str(payload.get("source_host") or ""),
    )


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time compare without making callers import the auth module."""
    import hmac

    return hmac.compare_digest(left, right)


def read_snapshot(path: Path) -> ActiveState:
    path = path.expanduser()
    size = path.stat().st_size
    if size <= 0 or size > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"snapshot size {size} is outside the accepted range")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return _validate_snapshot(payload)


def active_state() -> ActiveState:
    """Read configured canonical state without stale fallback on remote hosts."""
    configured = snapshot_path()
    if configured is not None:
        try:
            return read_snapshot(configured)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ActiveState(
                records=(),
                source="snapshot",
                error=f"canonical state snapshot unavailable: {exc}",
            )
    try:
        records = local_active_records()
    except Exception as exc:
        return ActiveState(
            records=(),
            source="local",
            error=f"local memory unavailable: {exc}",
        )
    return ActiveState(
        records=records,
        source="local",
        state_hash=_records_hash(records),
        source_host=socket.gethostname(),
    )


def _clip(value: str, length: int = 140) -> str:
    clean = " ".join((value or "").split())
    return clean if len(clean) <= length else clean[: length - 1] + "…"


def compact_active(state: ActiveState | None = None) -> str:
    """Render the same bounded task, ledger, and loop digest on every host."""
    state = state or active_state()
    if state.error:
        return f"# Canonical state unavailable\n{state.error}"
    tasks: list[str] = []
    ledgers: list[str] = []
    loops: list[str] = []
    for record in state.records:
        kind = record["type"]
        if kind == "task":
            tasks.append(f"- [{record['id']}] {_clip(record['content'])}")
        elif kind == "ledger":
            ledgers.append(
                f"- {record.get('ledger_key') or '?'}: "
                f"goal={_clip(record.get('goal', ''), 100)} | "
                f"facts={_clip(record.get('facts', ''), 180)} | "
                f"decision={_clip(record.get('decision', ''), 160)} | "
                f"risk={_clip(record.get('risk', ''), 140)} | "
                f"next={_clip(record.get('next_action', ''), 140)}"
            )
        elif kind == "loop":
            loops.append(f"- [{record['id']}] {_clip(record['content'])}")
    sections: list[str] = []
    if ledgers:
        sections.append("# Active ledgers (live thread state)\n" + "\n".join(ledgers))
    if tasks:
        sections.append("# His open tasks\n" + "\n".join(tasks[:15]))
    if loops:
        sections.append("# Open loops\n" + "\n".join(loops[:10]))
    return "\n\n".join(sections)


def format_ledgers(name: str = "", state: ActiveState | None = None) -> str:
    state = state or active_state()
    if state.error:
        return f"({state.error})"
    wanted = (name or "").strip()
    lines: list[str] = []
    for record in state.records:
        if record["type"] != "ledger":
            continue
        if wanted and record.get("ledger_key") != wanted:
            continue
        lines.append(f"## {record.get('ledger_key')}")
        for field in _LEDGER_FIELDS:
            value = str(record.get(field) or "").strip()
            if value:
                lines.append(f"{field}: {value}")
        lines.append("")
    return "\n".join(lines) or "(no matching ledgers)"


def state_status(state: ActiveState | None = None) -> dict[str, Any]:
    state = state or active_state()
    return {
        "source": state.source,
        "ok": not bool(state.error),
        "generated_at": state.generated_at or None,
        "source_host": state.source_host or None,
        "state_hash": state.state_hash or None,
        "records": len(state.records),
        "error": state.error or None,
    }
