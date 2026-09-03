"""Read Gemini's rate limits out of the Antigravity CLI.

Antigravity keeps quota in memory and renders it in its own TUI; nothing lands
on disk, so there is no file to tail the way Codex's usage is tailed. What it
does have is a print mode that answers slash commands, and `/usage` there emits
four tab-separated rows:

    Gemini Models          Weekly Limit Remaining      100%   2026-09-10T18:27:58Z
    Gemini Models          Five Hour Limit Remaining    98%   2026-09-03T23:27:58Z
    Claude and GPT models  Weekly Limit Remaining      100%   2026-09-10T18:38:54Z
    Claude and GPT models  Five Hour Limit Remaining   100%   2026-09-03T23:38:54Z

Two details matter. The number is what is LEFT, and Serena's panel everywhere
else shows what has been USED, so it is inverted here rather than at the point
of display. And the second family is Antigravity's own Claude and GPT access,
which is a different subscription from the user's Claude and Codex CLIs; folding
it into their pills would silently mix two accounts, so only the Gemini rows are
read.

Running the CLI is not free, so the result is cached and refreshed on a timer
rather than on every page poll.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

SOURCE = "gemini-cli-usage"

# Antigravity may be installed as `agy`; `gemini` is the familiar alias for it.
_BINARIES = ("agy", "gemini")

# The row we want, and the window it maps onto. "Weekly" is Serena's seven_day.
_WINDOW_BY_LABEL = {
    "five hour limit remaining": "five_hour",
    "weekly limit remaining": "seven_day",
}

_GEMINI_FAMILY = "gemini models"

# One call every few minutes. The limits move slowly and the CLI is a process
# spawn, not a file read.
REFRESH_SECONDS = 300.0
TIMEOUT_SECONDS = 45.0

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"at": 0.0, "data": None}


def _binary() -> str | None:
    for name in _BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _epoch(value: str) -> int | None:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def parse_usage(text: str, *, now: float | None = None) -> dict:
    """Turn `/usage` output into the service shape the panel renders."""
    moment = int(now if now is not None else time.time())
    out: dict[str, object] = {"available": False, "source": SOURCE, "model": "gemini"}

    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 3:
            continue
        family, label, percent = parts[0], parts[1], parts[2]
        if family.lower() != _GEMINI_FAMILY:
            continue
        window = _WINDOW_BY_LABEL.get(label.lower())
        if not window:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", percent)
        if not match:
            continue

        remaining = float(match.group(1))
        entry: dict[str, object] = {
            # The CLI reports what is left; every other pill shows what is used.
            "used_percentage": max(0.0, min(100.0, 100.0 - remaining)),
            "observed_at": moment,
            "source": SOURCE,
        }
        resets_at = _epoch(parts[3]) if len(parts) > 3 else None
        if resets_at:
            entry["resets_at"] = resets_at
        out[window] = entry
        out["available"] = True

    if out["available"]:
        out["updated_at"] = moment
    return out


def read_gemini_usage(*, now: float | None = None, force: bool = False) -> dict:
    """Current Gemini limits, cached. Never raises: the panel must still draw."""
    moment = now if now is not None else time.time()
    with _LOCK:
        cached = _CACHE.get("data")
        if not force and cached is not None and moment - float(_CACHE["at"]) < REFRESH_SECONDS:
            return dict(cached)  # type: ignore[arg-type]

        binary = _binary()
        if not binary:
            result = {"available": False, "source": SOURCE, "reason": "antigravity CLI not installed"}
        else:
            try:
                completed = subprocess.run(
                    [binary, "--print", "/usage"],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
                result = parse_usage(completed.stdout, now=moment)
                if not result.get("available"):
                    result["reason"] = (completed.stderr or "no usage rows returned").strip()[:200]
            except (OSError, subprocess.SubprocessError) as exc:
                result = {
                    "available": False,
                    "source": SOURCE,
                    "reason": f"{type(exc).__name__}: {exc}"[:200],
                }

        _CACHE["at"] = moment
        _CACHE["data"] = result
        return dict(result)
