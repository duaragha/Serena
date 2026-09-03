"""Push Serena's searchable chats and knowledge to Locket's always-on cache.

The local JSONL and knowledge files remain the source of truth. Uploads are
sanitized, hash-addressed, incremental, and bounded in memory. Vault files are
not scanned by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.locket_sync_state import STATE_PATH, load_state
from memory.locket_mirror import _creds

CHUNK_SIZE = 2000
MAX_BATCH_BYTES = 1_500_000
MAX_SESSIONS_PER_BATCH = 10
MAX_KNOWLEDGE_PER_BATCH = 24
MAX_SESSION_PASSAGES = 9000
MAX_SESSION_CHARS = 3_000_000
MAX_KNOWLEDGE_BYTES = 500_000
AUTO_SYNC_SECONDS = 30 * 60

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\brw_[A-Za-z0-9_-]{20,}\b"), "[REDACTED_RAILWAY_TOKEN]"),
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/\-]{16,}", re.I), "Bearer [REDACTED_TOKEN]"),
    (
        re.compile(r"\b(?:postgres|postgresql)://[^\s\"']+", re.I),
        "[REDACTED_DATABASE_URL]",
    ),
    (
        re.compile(r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))\s*=\s*[^\s\"']+", re.I),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(
            r"(\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*)[\"'][^\"']{12,}[\"']",
            re.I,
        ),
        r'\1"[REDACTED]"',
    ),
]

_MACHINE_TAGS = (
    "system-reminder",
    "environment_context",
    "permissions",
    "local-command-caveat",
    "ide_opened_file",
)

_auto_thread: threading.Thread | None = None
_auto_lock = threading.Lock()
_queued_timer: threading.Timer | None = None


def sanitize_recall_text(value: str) -> str:
    text = value.replace("\x00", "")
    for tag in _MACHINE_TAGS:
        text = re.sub(
            rf"<{tag}[^>]*>[\s\S]*?</{tag}>",
            " ",
            text,
            flags=re.I,
        )
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(
        r"[A-Za-z0-9_-]{40,}",
        lambda match: "[REDACTED_HIGH_ENTROPY_TOKEN]"
        if (
            re.search(r"[A-Z]", match.group(0))
            and re.search(r"[a-z]", match.group(0))
            and re.search(r"\d", match.group(0))
        )
        else match.group(0),
        text,
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_recall_text(value: str) -> list[str]:
    text = sanitize_recall_text(value)
    if not text:
        return []
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + CHUNK_SIZE)
        if end < len(text):
            boundary = max(text.rfind("\n", cursor, end), text.rfind(" ", cursor, end))
            if boundary > cursor + CHUNK_SIZE // 2:
                end = boundary
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        cursor = end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return chunks


def _valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _hash_payload(meta: dict[str, Any], passages: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(meta, sort_keys=True, separators=(",", ":")).encode())
    for passage in passages:
        digest.update(json.dumps(passage, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _cap_passages(
    passages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    total_chars = sum(len(str(passage.get("content") or "")) for passage in passages)
    if len(passages) <= MAX_SESSION_PASSAGES and total_chars <= MAX_SESSION_CHARS:
        return passages, False

    head: list[dict[str, Any]] = []
    head_chars = 0
    for passage in passages:
        size = len(str(passage.get("content") or ""))
        if len(head) >= MAX_SESSION_PASSAGES // 2 or head_chars + size > MAX_SESSION_CHARS // 2:
            break
        head.append(passage)
        head_chars += size

    tail: list[dict[str, Any]] = []
    tail_chars = 0
    head_ids = {id(passage) for passage in head}
    for passage in reversed(passages):
        if id(passage) in head_ids:
            break
        size = len(str(passage.get("content") or ""))
        if len(head) + len(tail) >= MAX_SESSION_PASSAGES:
            break
        if head_chars + tail_chars + size > MAX_SESSION_CHARS:
            break
        tail.append(passage)
        tail_chars += size
    tail.reverse()
    return head + tail, True


def _session_fingerprint(row: dict[str, Any]) -> str:
    values = {
        "file_size": row.get("file_size"),
        "file_mtime": row.get("file_mtime"),
        "title": row.get("display_title"),
        "project": row.get("last_cwd") or row.get("cwd") or row.get("project_dir"),
        "device": row.get("device"),
        "agent": row.get("agent"),
        "tags": sorted(str(tag).lower() for tag in row.get("tags", [])),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _knowledge_fingerprint(row: dict[str, Any]) -> str:
    values = {
        "file_size": row.get("file_size"),
        "file_mtime": row.get("file_mtime"),
        "title": row.get("title"),
        "filename": row.get("filename"),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _build_session(row: dict[str, Any]) -> dict[str, Any] | None:
    from core.parser import parse_messages_for_search

    path = Path(str(row.get("file_path") or ""))
    if not path.exists():
        return None
    messages = parse_messages_for_search(path)
    passages: list[dict[str, Any]] = []
    for position, (role, text, timestamp) in enumerate(messages):
        if role not in {"user", "assistant"}:
            continue
        for chunk_index, chunk in enumerate(chunk_recall_text(text)):
            passages.append(
                {
                    "messagePosition": position,
                    "chunkIndex": chunk_index,
                    "role": role,
                    "content": chunk,
                    "messageTimestamp": _valid_timestamp(timestamp),
                }
            )

    passages, is_truncated = _cap_passages(passages)

    meta = {
        "sessionId": str(row["session_id"]),
        "sourceKind": "laptop",
        "agent": str(row.get("agent") or "claude"),
        "title": sanitize_recall_text(str(row.get("display_title") or "Untitled chat"))[:500]
        or "Untitled chat",
        "project": str(row.get("last_cwd") or row.get("cwd") or row.get("project_dir") or "")
        or None,
        "device": str(row.get("device") or "") or None,
        "firstTimestamp": _valid_timestamp(row.get("first_timestamp")),
        "lastTimestamp": _valid_timestamp(row.get("last_timestamp")),
        "messageCount": len(messages),
        "isTruncated": is_truncated,
    }
    return {**meta, "contentHash": _hash_payload(meta, passages), "passages": passages}


def _knowledge_rows() -> list[dict[str, Any]]:
    from core.indexer import _get_db

    conn = _get_db()
    try:
        rows = conn.execute(
            """
            SELECT f.topic_slug, t.title, f.filename, f.file_path,
                   f.file_size, f.file_mtime
            FROM knowledge_files f
            JOIN knowledge_topics t ON t.slug = f.topic_slug
            ORDER BY f.topic_slug, f.filename
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _build_knowledge(row: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(str(row.get("file_path") or ""))
    try:
        content = sanitize_recall_text(path.read_text(encoding="utf-8", errors="replace"))
        content = content.encode("utf-8")[:MAX_KNOWLEDGE_BYTES].decode(
            "utf-8", errors="ignore"
        )
    except OSError:
        return None
    meta = {
        "documentKey": f"{row['topic_slug']}/{row['filename']}",
        "topicSlug": str(row["topic_slug"]),
        "topicTitle": sanitize_recall_text(str(row.get("title") or row["topic_slug"]))[:500],
        "filename": str(row["filename"]),
        "modifiedAt": datetime.fromtimestamp(
            float(row.get("file_mtime") or 0), timezone.utc
        ).isoformat()
        if row.get("file_mtime")
        else None,
    }
    content_hash = hashlib.sha256(
        json.dumps(meta, sort_keys=True).encode() + content.encode()
    ).hexdigest()
    return {**meta, "content": content, "contentHash": content_hash}


def _request(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 45):
    creds = _creds()
    if not creds:
        return None
    base, key = creds
    req = urllib.request.Request(
        f"{base}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "serena-archive-sync/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8", errors="replace"))
        except (ValueError, OSError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["_http_status"] = error.code
        return payload
    except (OSError, ValueError):
        return None


def _save_archive_state(
    session_entries: dict[str, dict[str, str]],
    knowledge_entries: dict[str, dict[str, str]],
    counts: dict[str, int],
    remote_check_at: str = "",
) -> None:
    state = load_state()
    state["archive"] = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "sessions": session_entries,
        "knowledge": knowledge_entries,
        "last_remote_check_at": remote_check_at,
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _result_ids(response: dict[str, Any] | None, key: str) -> set[str]:
    rows = (((response or {}).get("data") or {}).get(key) or [])
    return {
        str(row["id"])
        for row in rows
        if row.get("status") in {"created", "updated", "unchanged", "deleted", "missing"}
    }


def _remote_hashes_if_due(
    archive_state: dict[str, Any],
    force: bool,
) -> tuple[dict[str, str], dict[str, str], str, bool]:
    last_raw = archive_state.get("last_remote_check_at") or ""
    due = force or not last_raw
    if last_raw and not force:
        try:
            last = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
            due = datetime.now(timezone.utc) - last >= timedelta(days=7)
        except ValueError:
            due = True
    if not due:
        return {}, {}, str(last_raw), False
    response = _request("GET", "/api/v1/serena/archive/sync/?hashes=true")
    data = (response or {}).get("data") or {}
    if not isinstance(data.get("sessionHashes"), dict):
        return {}, {}, str(last_raw), False
    return (
        {str(key): str(value) for key, value in data["sessionHashes"].items()},
        {str(key): str(value) for key, value in (data.get("knowledgeHashes") or {}).items()},
        datetime.now(timezone.utc).isoformat(),
        True,
    )


def _send_batches(
    kind: str,
    items: list[tuple[str, dict[str, Any], dict[str, str]]],
    max_items: int,
) -> tuple[set[str], int]:
    successful: set[str] = set()
    requests = 0
    batch: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    batch_bytes = 0

    def flush() -> None:
        nonlocal batch, batch_bytes, requests
        if not batch:
            return
        payload = {
            "sessions": [item[1] for item in batch] if kind == "sessions" else [],
            "knowledge": [item[1] for item in batch] if kind == "knowledge" else [],
        }
        response = _request("POST", "/api/v1/serena/archive/sync/", payload)
        requests += 1
        if response and response.get("_http_status") in {400, 413} and len(batch) > 1:
            for single in batch:
                single_payload = {
                    "sessions": [single[1]] if kind == "sessions" else [],
                    "knowledge": [single[1]] if kind == "knowledge" else [],
                }
                single_response = _request(
                    "POST",
                    "/api/v1/serena/archive/sync/",
                    single_payload,
                )
                requests += 1
                successful.update(_result_ids(single_response, kind))
        else:
            successful.update(_result_ids(response, kind))
        batch = []
        batch_bytes = 0

    for item in items:
        item_bytes = len(json.dumps(item[1], separators=(",", ":")).encode())
        if batch and (len(batch) >= max_items or batch_bytes + item_bytes > MAX_BATCH_BYTES):
            flush()
        batch.append(item)
        batch_bytes += item_bytes
    flush()
    return successful, requests


def sync_locket_archive(
    *,
    dry_run: bool = False,
    force: bool = False,
    refresh_index: bool = True,
) -> dict[str, Any]:
    """Push changed chat sessions and knowledge files to Locket."""
    if refresh_index:
        from core.indexer import update_index, update_knowledge_index

        update_index(skip_if_running=True)
        update_knowledge_index()

    from core.indexer import list_sessions

    archive_state = load_state().get("archive") or {}
    old_sessions = archive_state.get("sessions") or {}
    old_knowledge = archive_state.get("knowledge") or {}
    if dry_run:
        remote_sessions, remote_knowledge = {}, {}
        remote_check_at = str(archive_state.get("last_remote_check_at") or "")
        remote_checked = False
    else:
        remote_sessions, remote_knowledge, remote_check_at, remote_checked = (
            _remote_hashes_if_due(archive_state, force)
        )
    new_sessions: dict[str, dict[str, str]] = dict(old_sessions)
    new_knowledge: dict[str, dict[str, str]] = dict(old_knowledge)

    session_rows = list_sessions(limit=100_000)
    visible_rows = []
    for row in session_rows:
        tags = {str(tag).lower() for tag in row.get("tags", [])}
        # Locket conversations are indexed directly from their encrypted
        # Postgres chat store. Their local mirror would create duplicate hits.
        if "noindex" not in tags and row.get("agent") != "locket":
            visible_rows.append(row)
    current_session_ids = {str(row["session_id"]) for row in visible_rows}
    deleted_session_ids = sorted(set(old_sessions) - current_session_ids)

    session_items: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    for row in visible_rows:
        sid = str(row["session_id"])
        fingerprint = _session_fingerprint(row)
        old_entry = old_sessions.get(sid) or {}
        remote_drifted = remote_checked and remote_sessions.get(sid) != old_entry.get("content_hash")
        if not force and old_entry.get("fingerprint") == fingerprint and not remote_drifted:
            continue
        payload = _build_session(row)
        if payload is None:
            continue
        entry = {"fingerprint": fingerprint, "content_hash": payload["contentHash"]}
        session_items.append((sid, payload, entry))

    knowledge_rows = _knowledge_rows()
    current_knowledge_keys = {
        f"{row['topic_slug']}/{row['filename']}" for row in knowledge_rows
    }
    deleted_knowledge_keys = sorted(set(old_knowledge) - current_knowledge_keys)
    knowledge_items: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    for row in knowledge_rows:
        key = f"{row['topic_slug']}/{row['filename']}"
        fingerprint = _knowledge_fingerprint(row)
        old_entry = old_knowledge.get(key) or {}
        remote_drifted = remote_checked and remote_knowledge.get(key) != old_entry.get("content_hash")
        if not force and old_entry.get("fingerprint") == fingerprint and not remote_drifted:
            continue
        payload = _build_knowledge(row)
        if payload is None:
            continue
        entry = {"fingerprint": fingerprint, "content_hash": payload["contentHash"]}
        knowledge_items.append((key, payload, entry))

    result = {
        "ok": bool(_creds()),
        "dry_run": dry_run,
        "sessions_changed": len(session_items),
        "sessions_truncated": sum(
            1 for _sid, payload, _entry in session_items if payload.get("isTruncated")
        ),
        "sessions_deleted": len(deleted_session_ids),
        "knowledge_changed": len(knowledge_items),
        "knowledge_deleted": len(deleted_knowledge_keys),
        "requests": 1 if remote_checked else 0,
    }
    if dry_run or not result["ok"]:
        return result

    successful_sessions, session_requests = _send_batches(
        "sessions", session_items, MAX_SESSIONS_PER_BATCH
    )
    successful_knowledge, knowledge_requests = _send_batches(
        "knowledge", knowledge_items, MAX_KNOWLEDGE_PER_BATCH
    )
    result["requests"] += session_requests + knowledge_requests

    for sid, _payload, entry in session_items:
        if sid in successful_sessions:
            new_sessions[sid] = entry
    for key, _payload, entry in knowledge_items:
        if key in successful_knowledge:
            new_knowledge[key] = entry

    if deleted_session_ids or deleted_knowledge_keys:
        deletion_response = _request(
            "POST",
            "/api/v1/serena/archive/sync/",
            {
                "deletedSessionIds": deleted_session_ids,
                "deletedKnowledgeKeys": deleted_knowledge_keys,
            },
        )
        result["requests"] += 1
        deleted_ok = _result_ids(deletion_response, "deletions")
        for sid in deleted_session_ids:
            if sid in deleted_ok:
                new_sessions.pop(sid, None)
        for key in deleted_knowledge_keys:
            if key in deleted_ok:
                new_knowledge.pop(key, None)

    result["sessions_synced"] = len(successful_sessions)
    result["knowledge_synced"] = len(successful_knowledge)
    result["ok"] = (
        len(successful_sessions) == len(session_items)
        and len(successful_knowledge) == len(knowledge_items)
    )
    _save_archive_state(
        new_sessions,
        new_knowledge,
        result,
        remote_check_at=remote_check_at,
    )
    return result


def start_auto_sync(interval_seconds: int = AUTO_SYNC_SECONDS) -> None:
    """Start one fail-soft archive sync loop for the desktop process."""
    global _auto_thread
    with _auto_lock:
        if _auto_thread and _auto_thread.is_alive():
            return

        def worker() -> None:
            time.sleep(15)
            while True:
                try:
                    sync_locket_archive()
                except Exception as error:
                    print(f"[archive-sync] {error}", flush=True)
                time.sleep(interval_seconds)

        _auto_thread = threading.Thread(
            target=worker,
            name="locket-archive-sync",
            daemon=True,
        )
        _auto_thread.start()


def queue_archive_sync(delay_seconds: float = 12.0) -> None:
    """Debounce a fail-soft sync after a Claude or Codex turn finishes."""
    global _queued_timer
    with _auto_lock:
        if _queued_timer is not None:
            _queued_timer.cancel()

        def run() -> None:
            try:
                sync_locket_archive()
            except Exception as error:
                print(f"[archive-sync] {error}", flush=True)

        _queued_timer = threading.Timer(delay_seconds, run)
        _queued_timer.daemon = True
        _queued_timer.start()
