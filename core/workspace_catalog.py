"""Register one native fork without rescanning or pruning unrelated chats."""
import json
import os
from pathlib import Path
from uuid import UUID


class NativeTranscriptPending(ValueError):
    """The native result arrived before its matching transcript record."""


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
    with _index_update_lock():
        if provider == "codex":
            from core.metadata import set_resident_work

            # Native app-server forks use an extension origin. Persist explicit
            # ownership so the normal scanner does not prune this admitted chat.
            set_resident_work(sid)
        conn = _get_db()
        try:
            _upsert_session(conn, meta, agent=provider)
            conn.commit()
        finally:
            conn.close()
