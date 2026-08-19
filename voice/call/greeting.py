"""Durable, reconnect-safe greeting state for Serena calls."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.daypart import DAYPARTS, daypart_for_hour

DEFAULT_STATE_PATH = (
    Path.home() / ".config" / "serena" / "call_state.json"
)


@dataclass(frozen=True, slots=True)
class GreetingContext:
    first_call_today: bool
    call_number_today: int
    local_time: str
    daypart: str = ""

    def prompt(self) -> str:
        first = "true" if self.first_call_today else "false"
        return (
            "<call-open "
            f"first-call-today=\"{first}\" "
            f"call-number-today=\"{self.call_number_today}\" "
            f"local-time=\"{self.local_time}\" "
            f"day-part=\"{self.daypart}\">\n"
            "Raghav just called you and is now listening. Greet him in one "
            "short, natural spoken line in your normal voice. The clock is "
            "real, so let the part of day color the wording when it lands "
            "naturally, in your own words rather than a stock phrase. Use the "
            "call context when it adds warmth, but never recite the metadata, "
            "never explain that this is automated, and never ask an "
            "open-ended question.\n"
            "</call-open>"
        )


class CallGreetingState:
    """Claim one greeting per call and track the first call of each day.

    A claim is persisted as pending before generation starts.  Playback then
    commits it as delivered.  If a socket dies before playback, releasing the
    in-process claim lets the reconnect reuse the same durable context without
    incrementing the day's call count or losing the greeting.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_CALL_STATE_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_STATE_PATH).expanduser()
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._delivered_runtime: set[str] = set()

    def claim(self, call_id: str) -> GreetingContext | None:
        call_id = call_id.strip()
        if not call_id:
            return None
        with self._lock:
            if call_id in self._active or call_id in self._delivered_runtime:
                return None
            state = self._read()
            delivered = [
                value
                for value in state.get("delivered_call_ids", state.get("recent_call_ids", []))
                if isinstance(value, str) and value
            ]
            if call_id in delivered:
                self._delivered_runtime.add(call_id)
                return None

            now = self.clock()
            date = now.date().isoformat()
            pending = self._pending(state)
            context = self._context_from_value(pending.get(call_id))
            seen = [
                value
                for value in state.get("seen_call_ids", [])
                if isinstance(value, str) and value
            ] if state.get("date") == date else []
            calls_today = (
                self._nonnegative_int(state.get("calls_today"))
                if state.get("date") == date
                else 0
            )
            if context is None:
                if call_id not in seen:
                    calls_today += 1
                    seen.append(call_id)
                context = GreetingContext(
                    first_call_today=calls_today == 1,
                    call_number_today=calls_today,
                    local_time=now.isoformat(timespec="minutes"),
                    daypart=daypart_for_hour(now.hour),
                )
                pending[call_id] = self._context_value(context)

            next_state = {
                "version": 2,
                "date": date,
                "calls_today": calls_today,
                "seen_call_ids": seen[-128:],
                "pending": pending,
                "delivered_call_ids": delivered[-128:],
            }
            if not self._write(next_state):
                return None
            self._active.add(call_id)
            return context

    def complete(self, call_id: str) -> bool:
        """Commit a greeting after content reaches the playback head."""
        call_id = call_id.strip()
        if not call_id:
            return False
        with self._lock:
            state = self._read()
            pending = self._pending(state)
            pending.pop(call_id, None)
            delivered = [
                value
                for value in state.get("delivered_call_ids", state.get("recent_call_ids", []))
                if isinstance(value, str) and value and value != call_id
            ]
            delivered.append(call_id)
            next_state = dict(state)
            next_state.update(
                {
                    "version": 2,
                    "pending": pending,
                    "delivered_call_ids": delivered[-128:],
                }
            )
            persisted = self._write(next_state)
            self._active.discard(call_id)
            self._delivered_runtime.add(call_id)
            return persisted

    def release(self, call_id: str) -> None:
        """Release an undelivered claim so a reconnect can retry it."""
        with self._lock:
            self._active.discard(call_id.strip())

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _context_value(context: GreetingContext) -> dict:
        return {
            "first_call_today": context.first_call_today,
            "call_number_today": context.call_number_today,
            "local_time": context.local_time,
            "daypart": context.daypart,
        }

    @classmethod
    def _context_from_value(cls, value: object) -> GreetingContext | None:
        if not isinstance(value, dict):
            return None
        local_time = value.get("local_time")
        call_number = cls._nonnegative_int(value.get("call_number_today"))
        if not isinstance(local_time, str) or not local_time or call_number < 1:
            return None
        return GreetingContext(
            first_call_today=bool(value.get("first_call_today")),
            call_number_today=call_number,
            local_time=local_time,
            daypart=cls._daypart(value.get("daypart"), local_time),
        )

    @staticmethod
    def _daypart(value: object, local_time: str) -> str:
        """Trust a recorded bucket, otherwise recover it from the claimed time."""
        if isinstance(value, str) and value in DAYPARTS:
            return value
        try:
            return daypart_for_hour(datetime.fromisoformat(local_time).hour)
        except ValueError:
            return ""

    @staticmethod
    def _pending(state: dict) -> dict[str, dict]:
        value = state.get("pending")
        if not isinstance(value, dict):
            return {}
        return {
            str(call_id): context
            for call_id, context in value.items()
            if isinstance(call_id, str) and isinstance(context, dict)
        }

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, value: dict) -> bool:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(value, separators=(",", ":")),
                encoding="utf-8",
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.path)
            return True
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            return False
