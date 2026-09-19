"""The last ~20 minutes as a compact turn context string.

Own token budget, always: this block is appended alongside memory retrieval,
never carved out of it. Apps with dwell, switch count, activity class, and
the last error seen — enough for her to know what he was just doing.
"""

from __future__ import annotations

import re
import time

from core.ambient_store import AmbientEvent, AmbientStore

CONTEXT_SECONDS = 1_200.0
MAX_CONTEXT_CHARS = 800

ERROR_TITLE = re.compile(
    r"\b(404|500|5\d\d|error|exception|traceback|failed|failure)\b",
    re.IGNORECASE,
)


def summarize(
    events: list[AmbientEvent], *, now: float | None = None,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """One compact block over the trailing window. Empty string when quiet."""

    from core.ambient_classify import classify

    moment = time.time() if now is None else float(now)
    windowed = sorted(
        (
            event
            for event in events
            if not event.agent_driven
            and moment - CONTEXT_SECONDS <= event.started_at <= moment
        ),
        key=lambda item: item.started_at,
    )
    foreground = [event for event in windowed if event.kind in {"window", "tab"}]
    if not foreground:
        return ""
    dwell: dict[str, float] = {}
    for event in foreground:
        dwell[event.app or "?"] = min(
            CONTEXT_SECONDS,
            dwell.get(event.app or "?", 0.0)
            + max(event.duration, min(60.0, moment - event.started_at)),
        )
    top = sorted(dwell.items(), key=lambda item: item[1], reverse=True)[:4]
    apps = ", ".join(f"{app} ({int(seconds // 60)}m)" for app, seconds in top)
    switches = sum(
        1 for first, second in zip(foreground, foreground[1:])
        if first.app != second.app
    )
    activity_class = classify(windowed, now=moment).activity_class
    errors = [event.title for event in foreground if ERROR_TITLE.search(event.title)]
    lines = [f"last 20 min: {apps}", f"switches: {switches}; state: {activity_class}"]
    if errors:
        lines.append(f"last error: {errors[-1][:120]}")
    return "\n".join(lines)[: max(0, int(max_chars))]


def ambient_context_block(
    *, now: float | None = None, max_chars: int = MAX_CONTEXT_CHARS
) -> str:
    """The block the brain appends to a turn. Fail-soft, budgeted."""

    moment = time.time() if now is None else float(now)
    try:
        events = AmbientStore().recent_from_disk(
            now=moment, limit=200, seconds=CONTEXT_SECONDS)
    except Exception:
        return ""
    try:
        return summarize(events, now=moment, max_chars=max_chars)
    except Exception:
        return ""
