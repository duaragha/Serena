"""Incrementally read account-wide Codex rate-limit observations from rollouts."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.usage_aggregator import (
    WINDOWS,
    codex_window_name,
    merge_observation,
)


class CodexUsageReader:
    """Bounded, single-flight reader for Codex rollout rate-limit events.

    Rollouts can grow past 100 MB. New files are bootstrapped from a small tail,
    then only appended bytes are read. Discovery remains periodic because a
    resumed old session can live in an old date directory while receiving new
    writes today.
    """

    def __init__(
        self,
        *,
        discovery_interval: float = 30.0,
        tracked_files: int = 24,
        tail_bytes: int = 2 * 1024 * 1024,
        max_increment_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.discovery_interval = discovery_interval
        self.tracked_files = tracked_files
        self.tail_bytes = tail_bytes
        self.max_increment_bytes = max_increment_bytes
        self._lock = threading.Lock()
        self._last_discovery = 0.0
        self._paths: list[Path] = []
        self._offsets: dict[str, int] = {}
        self._state: dict[str, Any] = {}
        self._latest_observation: dict[str, Any] | None = None
        self._version: str | None = None
        self._bytes_read = 0

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    def _discover(self, sessions_dir: Path, now: float) -> None:
        files: list[tuple[float, Path]] = []
        try:
            for path in sessions_dir.rglob("*.jsonl"):
                try:
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            files = []
        files.sort(key=lambda item: item[0], reverse=True)
        self._paths = [path for _mtime, path in files[: self.tracked_files]]
        keep = {str(path) for path in self._paths}
        self._offsets = {
            path: offset for path, offset in self._offsets.items() if path in keep
        }
        self._last_discovery = now
        if self._paths:
            self._read_version(self._paths[0])

    def _read_version(self, path: Path) -> None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            obj = json.loads(first) if first else {}
            payload = obj.get("payload") or {}
            version = payload.get("cli_version")
            if isinstance(version, str) and version:
                self._version = version
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    @staticmethod
    def _observed_at(obj: dict[str, Any], fallback: float) -> float:
        timestamp = obj.get("timestamp")
        if isinstance(timestamp, str):
            try:
                return datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _observation(
        obj: dict[str, Any], *, fallback_time: float, model: str
    ) -> dict[str, Any] | None:
        payload = obj.get("payload") or {}
        limits = payload.get("rate_limits") or {}
        if not isinstance(limits, dict):
            return None
        if limits.get("limit_id") not in (None, "codex"):
            return None
        primary = limits.get("primary")
        secondary = limits.get("secondary")
        if not isinstance(primary, dict) and not isinstance(secondary, dict):
            return None

        windows: dict[str, dict[str, Any]] = {}
        if isinstance(primary, dict):
            windows[codex_window_name(primary, "five_hour")] = {
                "used_percentage": primary.get("used_percent"),
                "resets_at": primary.get("resets_at"),
            }
        if isinstance(secondary, dict):
            windows[codex_window_name(secondary, "seven_day")] = {
                "used_percentage": secondary.get("used_percent"),
                "resets_at": secondary.get("resets_at"),
            }
        return {
            "provider": "codex",
            "source": "codex-jsonl",
            "observed_at": CodexUsageReader._observed_at(obj, fallback_time),
            "model": model,
            "plan_type": limits.get("plan_type"),
            "limit_id": limits.get("limit_id"),
            "windows": windows,
        }

    def _read_appended(self, path: Path, model: str) -> None:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return
        size = stat.st_size
        previous = self._offsets.get(key)
        if previous is not None and size == previous:
            return

        partial_start = False
        if previous is None or size < previous:
            start = max(0, size - self.tail_bytes)
            partial_start = start > 0
        else:
            start = previous
            if size - start > self.max_increment_bytes:
                start = max(0, size - self.tail_bytes)
                partial_start = start > 0

        try:
            with path.open("rb") as fh:
                fh.seek(start)
                if partial_start:
                    fh.readline()
                data = fh.read()
                self._offsets[key] = fh.tell()
        except OSError:
            return
        self._bytes_read += len(data)

        for raw in data.splitlines():
            if b"token_count" not in raw or b"rate_limits" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            observation = self._observation(
                obj, fallback_time=stat.st_mtime, model=model
            )
            if observation is None:
                continue
            self._state = merge_observation(self._state, observation)
            latest_at = (self._latest_observation or {}).get("observed_at") or 0
            if (observation.get("observed_at") or 0) >= latest_at:
                self._latest_observation = observation

    def read(
        self, sessions_dir: Path, *, model: str = "codex", now: float | None = None
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        out: dict[str, Any] = {
            "available": False,
            "source": "codex-jsonl",
            "model": model,
        }
        if not sessions_dir.exists():
            return out

        with self._lock:
            if not self._paths or now - self._last_discovery >= self.discovery_interval:
                self._discover(sessions_dir, now)
            for path in self._paths:
                self._read_appended(path, model)

            reduced = dict(self._state.get("codex") or {})
            latest = self._latest_observation or {}
            active_windows = set(latest.get("windows") or {})
            for name in WINDOWS:
                if name not in active_windows:
                    reduced.pop(name, None)
            out.update(reduced)
            out["model"] = model
            if self._version:
                out["version"] = self._version
            return out
