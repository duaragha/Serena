"""Register one native fork without rescanning or pruning unrelated chats."""
import os
from pathlib import Path
from uuid import UUID


def register_fork(target):
    from core.config import CLAUDE_DIR
    from core.indexer import _get_db, _index_update_lock, _upsert_session
    from core.parser import parse_metadata

    sid = target["session_id"]
    if target.get("provider") != "claude" or str(UUID(sid)) != sid:
        raise ValueError("An exact native Claude fork is required")
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or CLAUDE_DIR) / "projects"
    candidates = list(projects.glob(f"*/{sid}.jsonl"))
    if len(candidates) != 1:
        raise ValueError("Native fork transcript is missing or ambiguous")
    path = candidates[0]
    if not path.resolve().is_relative_to(projects.resolve()):
        raise ValueError("Native fork transcript is outside the session store")
    meta = parse_metadata(path, path.parent.name)
    if meta.session_id != sid or Path(meta.cwd).resolve() != Path(target["cwd"]).resolve():
        raise ValueError("Native fork metadata does not match its project and identity")
    with _index_update_lock():
        conn = _get_db()
        try:
            _upsert_session(conn, meta, agent="claude")
            conn.commit()
        finally:
            conn.close()
