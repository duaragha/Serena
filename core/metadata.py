"""Synced metadata storage for stars, tags, custom titles, groups, etc.

PER-SESSION layout: each chat's metadata lives in its own file at
~/.claude/projects/.chats-meta/<session_id>.json. Syncthing syncs the
directory; because each chat is a separate file, two devices renaming
DIFFERENT chats touch different files and merge cleanly. Only renaming the
SAME chat on both devices at once can conflict (Syncthing makes a visible
conflict file, which is recoverable — vs the old single-file store that
silently clobbered cross-device edits).

A one-time migration splits the legacy ~/.claude/projects/.chats-meta.json
single file into per-session files on first access.
"""

import json
import os
import socket
import threading
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.config import METADATA_DIR, METADATA_PATH
from core.file_lock import exclusive_lock
from core.process_probe import probe_process

_MIGRATION_LOCK = threading.Lock()
_migrated = False
_WRITE_LOCK = threading.RLock()
_WRITE_STATE = threading.local()


@contextmanager
def _metadata_write_lock():
    _ensure_migrated()
    with _WRITE_LOCK:
        if getattr(_WRITE_STATE, "active", False):
            yield
            return
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
        with (METADATA_DIR / ".write.lock").open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            with exclusive_lock(handle):
                _WRITE_STATE.active = True
                try:
                    yield
                finally:
                    _WRITE_STATE.active = False


def _effective_group(entry: dict) -> dict:
    # Older app versions can still restore a group in our shared metadata.
    # An explicit unlink remains authoritative until the user links again.
    if entry.get("group_unlinked"):
        entry.pop("group", None)
    return entry


def _ensure_migrated() -> None:
    """Split the legacy single-file store into per-session files. Idempotent;
    runs at most once per process."""
    global _migrated
    if _migrated:
        return
    with _MIGRATION_LOCK:
        if _migrated:
            return
        try:
            METADATA_DIR.mkdir(parents=True, exist_ok=True)
            if METADATA_PATH.exists():
                try:
                    legacy = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    legacy = {}
                for sid, entry in legacy.items():
                    if not isinstance(entry, dict):
                        continue
                    dst = METADATA_DIR / f"{sid}.json"
                    # Don't overwrite a per-session file that's already been
                    # written (it's newer / authoritative).
                    if dst.exists():
                        continue
                    try:
                        dst.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
                    except OSError:
                        pass
                # Rename the legacy file so it stops being the source of truth
                # (keep a backup rather than deleting).
                try:
                    METADATA_PATH.rename(METADATA_PATH.with_suffix(".json.migrated"))
                except OSError:
                    pass
        finally:
            _migrated = True


def _session_path(session_id: str) -> Path:
    return METADATA_DIR / f"{session_id}.json"


def _load_one(session_id: str) -> dict:
    _ensure_migrated()
    p = _session_path(session_id)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return _effective_group(d) if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_one(session_id: str, entry: dict, *, group_change: bool = False) -> None:
    with _metadata_write_lock():
        entry = dict(entry)
        if not group_change:
            # A rename/star write that began before an unlink must not undo it.
            latest = _load_one(session_id)
            for key in ("group", "group_unlinked"):
                entry.pop(key, None)
                if key in latest:
                    entry[key] = latest[key]
        p = _session_path(session_id)
        if not entry:
            p.unlink(missing_ok=True)
            return
        fd, name = tempfile.mkstemp(prefix=".meta-", suffix=".tmp", dir=METADATA_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, indent=2, sort_keys=True)
            os.replace(name, p)
        finally:
            Path(name).unlink(missing_ok=True)


def _load_all() -> dict:
    """Read every per-session file into {sid: entry}. Used by group scans +
    index rebuild. O(n files) but n is small and reads are cheap."""
    _ensure_migrated()
    out: dict = {}
    if not METADATA_DIR.exists():
        return out
    for p in METADATA_DIR.glob("*.json"):
        sid = p.stem
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                out[sid] = _effective_group(d)
        except (json.JSONDecodeError, OSError):
            continue
    return out


def get_meta(session_id: str) -> dict:
    """Get metadata for a session."""
    return _load_one(session_id)


def set_starred(session_id: str, starred: bool) -> None:
    entry = _load_one(session_id)
    entry["starred"] = starred
    _save_one(session_id, entry)


def set_custom_title(session_id: str, title: str) -> None:
    entry = _load_one(session_id)
    entry["custom_title"] = title
    _save_one(session_id, entry)


def set_muse_workspace(session_id: str, cwd: str) -> None:
    """Persist the workspace confirmed when a Muse pane first runs."""
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('An existing absolute Muse workspace is required')
    entry = _load_one(session_id)
    entry['muse_workspace'] = str(path.resolve())
    _save_one(session_id, entry)


def set_gemini_workspace(session_id: str, cwd: str) -> None:
    """Persist the workspace confirmed by a new native Gemini init event."""
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('An existing absolute Gemini workspace is required')
    entry = _load_one(session_id)
    entry['gemini_workspace'] = str(path.resolve())
    _save_one(session_id, entry)


def set_resident_work(session_id: str, resident: bool = True) -> None:
    """Mark a Codex exec session as user-visible Serena-owned work."""

    entry = _load_one(session_id)
    if resident:
        entry["resident_work"] = True
    else:
        entry.pop("resident_work", None)
    _save_one(session_id, entry)


def get_work_project_root(session_id: str) -> str | None:
    """Return the exact Git root bound to one interactive work session."""

    value = _load_one(session_id).get("work_project_root")
    clean = str(value or "").strip()
    return clean or None


def set_work_project_root(session_id: str, project_root: str | Path) -> str:
    """Immutably bind one session to one canonical Git repository root.

    A linked group is presentation metadata, not project identity. In
    particular, old linked threads can contain workers from several projects,
    so this writes only the named session and refuses to silently retarget it.
    """

    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("session id is required")

    from core.coding_job_contract import validate_repository_root

    canonical = str(validate_repository_root(project_root))
    entry = _load_one(session_id)
    existing = str(entry.get("work_project_root") or "").strip()
    if existing and existing != canonical:
        raise ValueError(
            f"session {session_id[:8]} is already bound to a different project"
        )
    if existing == canonical:
        return canonical
    entry["work_project_root"] = canonical
    _save_one(session_id, entry)
    return canonical


def set_fleet_worker(
    session_id: str,
    *,
    run_id: str,
    leg_id: str,
    phase: str,
    provider: str,
    model: str,
    effort: str,
    origin_session_id: str | None = None,
    worker_key: str | None = None,
    worker_label: str | None = None,
    assignment: str | None = None,
    worker_group_id: str | None = None,
) -> dict:
    """Permanently identify a real session as a Fleet worker.

    The marker remains after the external runtime lease ends. Interactive
    linked-chat resolution can therefore distinguish a finished worker from
    the user's original Claude/Codex sibling.
    """

    marker = {
        "run_id": str(run_id),
        "leg_id": str(leg_id),
        "phase": str(phase),
        "provider": str(provider),
        "model": str(model),
        "effort": str(effort),
    }
    if origin_session_id:
        marker["origin_session_id"] = str(origin_session_id)
    if worker_key:
        marker["worker_key"] = str(worker_key)
    if worker_label:
        marker["worker_label"] = str(worker_label)
    if assignment:
        marker["assignment"] = str(assignment)
    if worker_group_id:
        marker["worker_group_id"] = str(worker_group_id)
    entry = _load_one(session_id)
    entry["fleet_worker"] = marker
    _save_one(session_id, entry)
    return marker


def surface_fleet_worker(
    session_id: str,
    *,
    run_id: str,
    leg_id: str,
    phase: str,
    provider: str,
    model: str,
    effort: str,
    worker_key: str,
    worker_group_id: str,
    title: str,
    worker_label: str | None = None,
    assignment: str | None = None,
    origin_session_id: str | None = None,
    pid: int | None = None,
    lease_seconds: float = 24 * 60 * 60,
) -> bool:
    """Apply one complete Fleet metadata state in a single file write.

    Reconciliation calls omit ``pid`` so a completed transcript never regains
    a live-runtime lease. The authoritative Fleet group is assigned directly;
    ordinary linked-chat groups are never merged into it.
    """

    marker = {
        "run_id": str(run_id),
        "leg_id": str(leg_id),
        "phase": str(phase),
        "provider": str(provider),
        "model": str(model),
        "effort": str(effort),
        "worker_key": str(worker_key),
        "worker_group_id": str(worker_group_id),
    }
    if worker_label:
        marker["worker_label"] = str(worker_label)
    if assignment:
        marker["assignment"] = str(assignment)
    if origin_session_id:
        marker["origin_session_id"] = str(origin_session_id)
    with _metadata_write_lock():
        entry = _load_one(session_id)
        before = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        entry["resident_work"] = True
        entry["custom_title"] = str(title)
        entry["fleet_worker"] = marker
        if not entry.get("group_unlinked"):
            entry["group"] = str(worker_group_id)
        if pid is not None:
            now = time.time()
            entry["external_runtime"] = {
                "kind": "fleet-worker",
                "pid": int(pid),
                "host": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "lease_expires_at": now + max(30.0, float(lease_seconds)),
            }
        after = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        if after == before:
            return False
        _save_one(session_id, entry, group_change=True)
        return True


def set_external_runtime(
    session_id: str,
    *,
    kind: str,
    pid: int,
    lease_seconds: float,
) -> dict:
    """Claim a session that is currently owned by a non-interactive process."""

    now = time.time()
    runtime = {
        "kind": str(kind),
        "pid": int(pid),
        "host": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "lease_expires_at": now + max(30.0, float(lease_seconds)),
    }
    entry = _load_one(session_id)
    entry["external_runtime"] = runtime
    _save_one(session_id, entry)
    return runtime


def clear_external_runtime(
    session_id: str,
    *,
    pid: int | None = None,
    host: str | None = None,
) -> bool:
    """Release an external owner without clearing a newer owner's claim."""

    entry = _load_one(session_id)
    runtime = entry.get("external_runtime")
    if not isinstance(runtime, dict):
        return False
    if pid is not None and runtime.get("pid") != int(pid):
        return False
    if host is not None and runtime.get("host") != host:
        return False
    entry.pop("external_runtime", None)
    _save_one(session_id, entry)
    return True


def get_external_runtime(session_id: str) -> dict | None:
    runtime = _load_one(session_id).get("external_runtime")
    return dict(runtime) if isinstance(runtime, dict) else None


def external_runtime_active(session_id: str) -> bool:
    """Return whether another process still owns this session's transcript."""

    runtime = get_external_runtime(session_id)
    if not runtime:
        return False
    try:
        pid = int(runtime.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if runtime.get("host") == socket.gethostname():
        try:
            probe_process(pid)
            return True
        except PermissionError:
            return True
        except (ProcessLookupError, OSError):
            return False

    try:
        return float(runtime.get("lease_expires_at") or 0) > time.time()
    except (TypeError, ValueError):
        return False


def set_done(session_id: str, done: bool, done_at: str | None = None) -> None:
    """Mark or unmark a session as 'done'. done_at is the ISO timestamp when marked."""
    entry = _load_one(session_id)
    if done:
        entry["done"] = True
        if done_at:
            entry["done_at"] = done_at
    else:
        entry.pop("done", None)
        entry.pop("done_at", None)
    _save_one(session_id, entry)


def add_tag_meta(session_id: str, tag: str) -> None:
    entry = _load_one(session_id)
    tags = set(entry.get("tags", []))
    tags.add(tag)
    entry["tags"] = sorted(tags)
    _save_one(session_id, entry)


def remove_tag_meta(session_id: str, tag: str) -> None:
    entry = _load_one(session_id)
    tags = set(entry.get("tags", []))
    tags.discard(tag)
    entry["tags"] = sorted(tags)
    _save_one(session_id, entry)


def delete_meta(session_id: str) -> None:
    with _metadata_write_lock():
        _session_path(session_id).unlink(missing_ok=True)


def get_all_meta() -> dict:
    """Get all metadata. Used during index rebuild to apply synced state."""
    return _load_all()


# ─────────────────────────────────────────────────────────────────────────────
# === GROUP FEATURE START === (delete this block + the route block in
# ui/web.py and the JS block to remove the linked-chats / shared-thread
# feature.)
# ─────────────────────────────────────────────────────────────────────────────
import secrets


def _new_group_id() -> str:
    return "g_" + secrets.token_hex(6)


def get_group(session_id: str) -> str | None:
    return _load_one(session_id).get("group") or None


def set_group(session_id: str, group_id: str | None) -> None:
    with _metadata_write_lock():
        entry = _load_one(session_id)
        if group_id:
            entry["group"] = group_id
            entry.pop("group_unlinked", None)
        else:
            entry.pop("group", None)
            entry["group_unlinked"] = True
        _save_one(session_id, entry, group_change=True)


def list_group_members(group_id: str) -> list[str]:
    if not group_id:
        return []
    return [sid for sid, m in _load_all().items() if isinstance(m, dict) and m.get("group") == group_id]


def link_sessions(session_ids: list[str], *, automatic: bool = False) -> str | None:
    """Link N sessions into the same group. If any of them already have a
    group, that group wins (and any others get merged into it). Returns the
    final group_id."""
    session_ids = list(dict.fromkeys(s for s in session_ids if s))
    if len(session_ids) < 2:
        raise ValueError("link_sessions requires at least 2 session ids")
    with _metadata_write_lock():
        return _link_sessions_locked(session_ids, automatic=automatic)


def _link_sessions_locked(session_ids: list[str], *, automatic: bool) -> str | None:
    data = _load_all()
    if automatic and any((data.get(sid) or {}).get("group_unlinked") for sid in session_ids):
        return None
    existing: list[str] = []
    for sid in session_ids:
        g = (data.get(sid) or {}).get("group")
        if g and g not in existing:
            existing.append(g)
    target_group = existing[0] if existing else _new_group_id()
    # If multiple existing groups, sweep all members of each into target_group
    if len(existing) > 1:
        merge_groups = set(existing[1:])
        for sid, m in data.items():
            if isinstance(m, dict) and m.get("group") in merge_groups and m.get("group") != target_group:
                m["group"] = target_group
                _save_one(sid, m, group_change=True)
    # Apply to incoming sids
    for sid in session_ids:
        entry = _load_one(sid)
        entry["group"] = target_group
        entry.pop("group_unlinked", None)
        _save_one(sid, entry, group_change=True)
    return target_group


def unlink_session(session_id: str) -> None:
    """Remove this session from its group. Other members keep the group.
    If only one member remains afterward, also clear that one (singletons
    aren't groups)."""
    with _metadata_write_lock():
        g = get_group(session_id)
        set_group(session_id, None)
        remaining = list_group_members(g)
        if len(remaining) == 1:
            set_group(remaining[0], None)


def unlink_group(group_id: str) -> None:
    """Disband the group entirely — clear it from every member."""
    if not group_id:
        return
    with _metadata_write_lock():
        for sid in list_group_members(group_id):
            set_group(sid, None)
# === GROUP FEATURE END ===
