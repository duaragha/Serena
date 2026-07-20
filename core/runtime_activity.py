"""Bounded transcript checks for whether a Claude or Codex turn is active."""

from __future__ import annotations

import json
import threading
from pathlib import Path


_CODEX_FINISHED = {
    "task_complete",
    "task_completed",
    "task_cancelled",
    "turn_aborted",
    "turn_cancelled",
}


class TurnActivityReader:
    """Read only the tail of a transcript and cache results by file version."""

    def __init__(self, tail_bytes: int = 512 * 1024) -> None:
        self.tail_bytes = tail_bytes
        self._cache: dict[tuple[str, str], tuple[int, int, bool | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _codex_state(records: list[dict]) -> bool | None:
        saw_work = False
        for record in reversed(records):
            record_type = record.get("type")
            if record_type == "event_msg":
                kind = (record.get("payload") or {}).get("type")
                if kind in _CODEX_FINISHED:
                    return False
                if kind == "task_started":
                    return True
                if kind in {
                    "agent_message",
                    "mcp_tool_call_begin",
                    "mcp_tool_call_end",
                    "token_count",
                    "user_message",
                }:
                    saw_work = True
            elif record_type in {"response_item", "turn_context"}:
                saw_work = True
        return True if saw_work else None

    @staticmethod
    def _claude_state(records: list[dict]) -> bool | None:
        for record in reversed(records):
            record_type = record.get("type")
            if (
                record_type == "system"
                and record.get("subtype") == "turn_duration"
            ):
                return False
            if record_type in {"user", "assistant"}:
                return True
        return None

    def read(self, file_path: str | Path | None, agent: str) -> bool | None:
        if not file_path:
            return None
        path = Path(file_path)
        try:
            stat = path.stat()
        except OSError:
            return None

        agent = (agent or "").lower()
        key = (str(path), agent)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                return cached[2]

        start = max(0, stat.st_size - self.tail_bytes)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                if start:
                    fh.readline()
                data = fh.read()
        except OSError:
            return None

        records: list[dict] = []
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)

        if agent == "codex":
            state = self._codex_state(records)
        elif agent == "claude":
            state = self._claude_state(records)
        else:
            state = None

        with self._lock:
            self._cache[key] = (stat.st_size, stat.st_mtime_ns, state)
        return state

