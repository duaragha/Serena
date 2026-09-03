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
    def _codex_finished(record: dict) -> bool:
        return bool(
            record.get("type") == "event_msg"
            and (record.get("payload") or {}).get("type") in _CODEX_FINISHED
        )

    @staticmethod
    def _claude_finished(record: dict) -> bool:
        if (
            record.get("type") == "system"
            and record.get("subtype") == "turn_duration"
        ):
            return True
        return bool(
            record.get("type") == "assistant"
            and record.get("isApiErrorMessage") is True
            and record.get("error") == "rate_limit"
        )

    @staticmethod
    def _codex_state(records: list[dict]) -> bool | None:
        saw_work = False
        for record in reversed(records):
            record_type = record.get("type")
            if record_type == "event_msg":
                kind = (record.get("payload") or {}).get("type")
                if TurnActivityReader._codex_finished(record):
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
            if TurnActivityReader._claude_finished(record):
                return False
            if record_type in {"user", "assistant"}:
                return True
        return None

    def completed_since(
        self,
        file_path: str | Path | None,
        agent: str,
        start_offset: int,
    ) -> bool:
        """Return whether a completion marker was appended after ``start_offset``."""
        if not file_path or start_offset < 0:
            return False
        path = Path(file_path)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= start_offset:
            return False

        start = start_offset
        clipped = size - start > self.tail_bytes
        if clipped:
            start = size - self.tail_bytes
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                if clipped:
                    fh.readline()
                data = fh.read()
        except OSError:
            return False

        agent = (agent or "").lower()
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if agent == "claude" and self._claude_finished(record):
                return True
            if agent == "codex" and self._codex_finished(record):
                return True
        return False

    def waiting_for_usage_reset(
        self,
        file_path: str | Path | None,
        agent: str,
    ) -> bool:
        """Return whether Claude's latest turn ended at its usage-limit screen."""
        if not file_path or (agent or "").lower() != "claude":
            return False
        path = Path(file_path)
        try:
            size = path.stat().st_size
            start = max(0, size - self.tail_bytes)
            with path.open("rb") as fh:
                fh.seek(start)
                if start:
                    fh.readline()
                data = fh.read()
        except OSError:
            return False

        records: list[dict] = []
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)

        for record in reversed(records):
            if (
                record.get("type") == "assistant"
                and record.get("isApiErrorMessage") is True
                and record.get("error") == "rate_limit"
            ):
                return True
            if record.get("type") in {"user", "assistant"}:
                return False
        return False

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
