"""Small status file for Locket memory/chat sync visibility."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_PATH = Path.home() / ".config" / "serena" / "locket-sync-state.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_success(kind: str, counts: dict[str, Any]) -> None:
    data = load_state()
    data[kind] = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def last_success(kind: str) -> str:
    value = (load_state().get(kind) or {}).get("last_success_at")
    return str(value) if value else "never"
