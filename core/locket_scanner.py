"""Scanner + sync for Locket in-app Serena chats.

Locket (Raghav's life-tracker app) hosts an in-app Serena chat whose
conversations live in its Postgres. `sync_locket_chats()` pulls them over
the authed REST API and writes one JSONL file per conversation under
LOCKET_SYNC_ROOT using the *Claude session schema* — so the existing
claude-format parsers (parse_messages_for_search etc.) index them with
zero changes. The indexer treats these files as agent="locket".

Credentials live in ~/.config/serena/locket.env:
    LOCKET_URL=https://locket-production-cfbc.up.railway.app
    LOCKET_API_KEY=ft_...
"""

import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .parser import SessionMeta, _extract_text

# Lives inside ~/.claude/projects/ ON PURPOSE, with bare-UUID filenames:
# the claude scanner (UUID-strict) also discovers these files, so
# long-running processes loaded with pre-locket code (desktop app) don't
# zombie-prune the sessions between restarts. Both scanners yield them;
# in new code the locket upsert runs last and wins the agent tag. The
# claude-native JSONL schema makes both parsers happy.
LOCKET_SYNC_ROOT = Path.home() / ".claude" / "projects" / "locket-chat"

_UUID_RE_STR = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
LOCKET_ENV = Path.home() / ".config" / "serena" / "locket.env"
LOCKET_PROJECT_DIR = "locket-chat"


def _load_env() -> tuple[str, str] | None:
    """Read LOCKET_URL + LOCKET_API_KEY; None when unconfigured."""
    if not LOCKET_ENV.exists():
        return None
    url = key = ""
    try:
        for line in LOCKET_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("LOCKET_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("LOCKET_API_KEY="):
                key = line.split("=", 1)[1].strip()
    except OSError:
        return None
    if not url or not key:
        return None
    return url, key


def sync_locket_chats(timeout: int = 10) -> int:
    """Pull conversations from the Locket API into LOCKET_SYNC_ROOT.

    Full overwrite per conversation (files are small); returns the number
    of conversations written. Fail-soft: any error returns 0 — the index
    just serves the last synced state.
    """
    env = _load_env()
    if env is None:
        return 0
    base, key = env

    req = urllib.request.Request(
        f"{base}/api/v1/serena/conversations?limit=200",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception:
        return 0

    conversations = (payload.get("data") or []) if isinstance(payload, dict) else []
    if not conversations:
        return 0

    LOCKET_SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    written = 0
    for conv in conversations:
        conv_id = conv.get("id")
        messages = conv.get("messages") or []
        if conv_id is None or not messages:
            continue
        lines = []
        for m in messages:
            role = "assistant" if m.get("role") == "assistant" else "user"
            ts = m.get("createdAt") or conv.get("updatedAt") or ""
            # Claude session schema so claude-format parsers Just Work.
            lines.append(json.dumps({
                "type": role,
                "timestamp": ts,
                "message": {
                    "role": role,
                    "content": [{"type": "text", "text": str(m.get("content") or "")}],
                },
            }, ensure_ascii=False))
        out = LOCKET_SYNC_ROOT / f"{conv_id}.jsonl"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written += 1
    return written


def scan_locket_sessions() -> Iterator[tuple[str, Path]]:
    """Yield (project_dir, jsonl_path) for synced Locket conversations."""
    if not LOCKET_SYNC_ROOT.exists():
        return
    uuid_re = re.compile(_UUID_RE_STR, re.I)
    for jsonl in sorted(LOCKET_SYNC_ROOT.glob("*.jsonl")):
        if jsonl.is_file() and uuid_re.match(jsonl.stem):
            yield LOCKET_PROJECT_DIR, jsonl


def parse_locket_metadata(file_path: Path) -> SessionMeta | None:
    """Build SessionMeta from a synced Locket conversation file."""
    first_text = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    count = 0
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") not in ("user", "assistant"):
                    continue
                count += 1
                ts = _coerce_dt(rec.get("timestamp"))
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if not first_text and rec.get("type") == "user":
                    text = _extract_text((rec.get("message") or {}).get("content"))
                    first_text = text.strip()[:500]
    except OSError:
        return None
    if count == 0:
        return None

    try:
        stat = file_path.stat()
    except OSError:
        return None

    return SessionMeta(
        session_id=file_path.stem,
        project_dir=LOCKET_PROJECT_DIR,
        cwd=None,
        device="locket",
        first_message=first_text or "(locket chat)",
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=count,
        raw_message_count=count,
        file_path=str(file_path),
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
    )


def _coerce_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
