"""Why her brain was down, written down while it happens.

On 2026-09-19 the brain stopped at 15:37 and stayed dead for two days. The
only record was end_reason "shutdown" -- the word every exit path wrote, crash
or kill or clean stop alike -- in a log whose lines carried no timestamps,
while the traceback that explained exit code 1 went nowhere. Nobody could say
whether Windows shut it down, it crashed, or something killed it.

This keeps one line per start and one per stop, each with a wall-clock time,
the process id, the real reason, and on every start how long she was gone and
whether the machine rebooted in between. It is append-only JSONL so a process
dying mid-write costs at most its own line, and `chats brain history` reads it
back as sentences.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER = Path.home() / ".local" / "state" / "serena" / "brain-uptime.jsonl"
# A start and a stop a day is ~700 lines a year; this is years of history and
# still a file anyone can open.
MAX_LINES = 4000
DETAIL_LIMIT = 600


def boot_time() -> float | None:
    """When this machine last booted, or None if the platform won't say."""

    try:
        import psutil

        return float(psutil.boot_time())
    except Exception:
        return None


def read(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or LEDGER
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            # A line torn by a process that died mid-write is noise, not history.
            continue
        if isinstance(event, dict) and event.get("event") in {"start", "stop"}:
            events.append(event)
    return events


def _append(event: dict[str, Any], path: Path | None = None) -> None:
    path = path or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > MAX_LINES:
        path.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")


def record_start(pid: int, *, path: Path | None = None, now: float | None = None,
                 booted_at: float | None = None) -> dict[str, Any]:
    """Note a start, and say what happened to the run before it."""

    now = time.time() if now is None else now
    booted_at = boot_time() if booted_at is None else booted_at
    events = read(path)
    last_start = next((e for e in reversed(events) if e["event"] == "start"), None)
    last_stop = next((e for e in reversed(events) if e["event"] == "stop"), None)
    entry: dict[str, Any] = {"event": "start", "at": now, "pid": pid}
    if booted_at is not None:
        entry["booted_at"] = booted_at
    if last_start is not None:
        stopped_cleanly = last_stop is not None and last_stop["at"] >= last_start["at"]
        if stopped_cleanly:
            entry["down_for"] = max(0.0, now - float(last_stop["at"]))
            entry["previous_stop"] = last_stop.get("reason", "unknown")
        elif booted_at is not None and booted_at > float(last_start["at"]):
            # No stop line and the machine came up after the last start: it
            # went down with the machine -- power loss, crash, or a hard reset.
            entry["previous_stop"] = "machine_rebooted"
            entry["down_for"] = max(0.0, now - booted_at)
        else:
            # No stop line and no reboot: the process was killed outright, so
            # nothing got the chance to write why.
            entry["previous_stop"] = "killed_without_a_trace"
    _append(entry, path)
    return entry


def record_stop(pid: int, reason: str, *, detail: str = "", started_at: float | None = None,
                path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    entry: dict[str, Any] = {"event": "stop", "at": now, "pid": pid, "reason": reason}
    if detail:
        entry["detail"] = " ".join(str(detail).split())[:DETAIL_LIMIT]
    if started_at is not None:
        entry["uptime"] = max(0.0, now - started_at)
    _append(entry, path)
    return entry


def _span(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_PLAIN = {
    "machine_rebooted": "the PC rebooted (power loss, crash, or restart)",
    "killed_without_a_trace": "it was killed outright; nothing got to say why",
}


def explain(events: list[dict[str, Any]] | None = None, *, limit: int = 20) -> list[str]:
    """The ledger as sentences, newest last."""

    events = read() if events is None else events
    lines = []
    for event in events[-limit:]:
        when = datetime.fromtimestamp(float(event["at"])).strftime("%a %b %d %H:%M")
        if event["event"] == "start":
            text = f"{when}  started (pid {event.get('pid')})"
            if "previous_stop" in event:
                gone = f"down {_span(event['down_for'])}, " if "down_for" in event else ""
                cause = _PLAIN.get(event["previous_stop"], event["previous_stop"])
                text += f" -- {gone}last stop: {cause}"
        else:
            ran = f" after {_span(event['uptime'])}" if "uptime" in event else ""
            text = f"{when}  stopped{ran}: {event.get('reason', 'unknown')}"
            if event.get("detail"):
                text += f" -- {event['detail']}"
        lines.append(text)
    return lines
