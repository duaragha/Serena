"""Register one native fork without rescanning or pruning unrelated chats."""
import json
import os
from pathlib import Path
from uuid import UUID


class NativeTranscriptPending(ValueError):
    """The native result arrived before its matching transcript record."""


def list_saved_sessions(provider, query="", offset=0, *, archived=False):
    """Read bounded catalog pages without starting or attaching any owner."""
    from core.indexer import _get_db

    if provider not in {"claude", "codex"} or not isinstance(query, str) or len(query) > 200 or type(archived) is not bool:
        raise ValueError("Invalid session search")
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError("Invalid session page")
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT session_id, COALESCE(NULLIF(custom_title,''), title, 'Untitled chat') AS title, "
            "cwd, last_timestamp, is_archived FROM sessions WHERE agent = ? AND COALESCE(is_teammate,0) = 0 "
            "AND COALESCE(is_archived,0) = ? "
            "AND (COALESCE(custom_title,title,'') LIKE ? ESCAPE '\\' "
            "OR session_id LIKE ? ESCAPE '\\') ORDER BY last_timestamp DESC, session_id LIMIT 51 OFFSET ?",
            (provider, int(archived), pattern, pattern, offset),
        ).fetchall()
        return {"data": [dict(row) for row in rows[:50]], "nextOffset": offset + 50 if len(rows) > 50 else None}
    finally:
        conn.close()


def register_fork(target):
    from core.config import CLAUDE_DIR
    from core.indexer import _get_db, _index_update_lock, _upsert_session
    from core.parser import parse_metadata

    sid = target["session_id"]
    provider = target.get("provider")
    if provider not in {"claude", "codex"} or str(UUID(sid)) != sid:
        raise ValueError("An exact native fork is required")
    if provider == "claude":
        projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or CLAUDE_DIR) / "projects"
        candidates = list(projects.glob(f"*/{sid}.jsonl"))
    else:
        projects = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        candidates = [p for directory in (projects / "sessions", projects / "archived_sessions")
                      for p in directory.rglob(f"rollout-*{sid}.jsonl")]
    if not candidates:
        raise NativeTranscriptPending("Completed prompt transcript is not persisted yet")
    if len(candidates) != 1:
        raise ValueError("Native fork transcript is missing or ambiguous")
    path = candidates[0]
    if not path.resolve().is_relative_to(projects.resolve()):
        raise ValueError("Native fork transcript is outside the session store")
    if provider == "claude" and target.get("prompt_id"):
        found = False
        with path.open(encoding="utf-8") as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "user" and target["prompt_id"] in (record.get("uuid"), record.get("promptId")):
                    found = True
        if not found:
            raise NativeTranscriptPending("Completed prompt is not persisted yet")
    if provider == "claude":
        meta = parse_metadata(path, path.parent.name)
    else:
        from core.codex_records import iter_records
        from core.codex_scanner import parse_codex_metadata

        header = next(iter_records(path), {})
        payload = header.get("payload") or {}
        if header.get("type") != "session_meta" or payload.get("id", payload.get("session_id")) != sid:
            raise ValueError("Native fork transcript identity does not match")
        meta = parse_codex_metadata(path)
    if meta is None or meta.session_id != sid or not isinstance(meta.cwd, str) or Path(meta.cwd).resolve() != Path(target["cwd"]).resolve():
        raise ValueError("Native fork metadata does not match its project and identity")
    if "expected_native_title" in target and (provider != "claude" or meta.native_title != target["expected_native_title"]):
        raise NativeTranscriptPending("Confirmed native rename is not persisted yet")
    native_name = target.get('confirmed_native_name')
    if native_name is not None and (not isinstance(native_name, str) or not native_name.strip() or len(native_name) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in native_name)):
        raise ValueError('Invalid confirmed native name')
    if native_name is not None and provider == 'claude' and (
        target.get('expected_native_title') != native_name or meta.native_title != native_name
    ):
        raise ValueError('Claude rename requires matching persisted native confirmation')
    with _index_update_lock():
        if provider == "codex":
            from core.metadata import set_resident_work

            # Native app-server forks use an extension origin. Persist explicit
            # ownership so the normal scanner does not prune this admitted chat.
            set_resident_work(sid)
        if native_name is not None:
            from core.metadata import set_custom_title

            set_custom_title(sid, native_name)
        conn = _get_db()
        try:
            _upsert_session(conn, meta, agent=provider)
            conn.commit()
            if "expected_native_title" in target or native_name is not None:
                row = conn.execute("SELECT COALESCE(NULLIF(custom_title,''), title) FROM sessions WHERE session_id = ?", (sid,)).fetchone()
                result = {"display_title": row[0]}
                if native_name is not None and provider == 'claude':
                    result['native_rename'] = True
                return result
        finally:
            conn.close()
