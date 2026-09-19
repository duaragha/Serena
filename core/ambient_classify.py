"""Local activity classification over the ambient buffer.

Small rules first, model second: a cheap classifier runs constantly and a
model is woken only after "stuck" fires. Stuck needs two or more distinct
struggle signals inside a 10–15 minute window — a single grumble is not
stuck. Agent-driven periods are removed before any counting, so Serena's
own working never reads as him struggling.

Struggle signals (all metadata, no content):
- search_reword: repeated rewordings of the same search
- error_revisit: the same error page opened again and again
- window_bounce: flapping between the same 2–3 windows
- failing_runs: repeated failing test or build runs in titles
- input_churn: a burst of title updates on one window (undo/edit churn proxy)
- long_dwell: one window for 25+ minutes with almost no input
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from core.ambient_store import AmbientEvent

ACTIVITY_CLASSES = ("focused", "browsing", "stuck", "idle")
STUCK_WINDOW_SECONDS = 900.0
LONG_DWELL_WINDOW_SECONDS = 1_800.0
STUCK_SIGNALS_REQUIRED = 2
LATCH_COOLDOWN_SECONDS = 1_800.0

SEARCH_HOSTS = (
    "google.com",
    "www.google.com",
    "bing.com",
    "www.bing.com",
    "duckduckgo.com",
    "kagi.com",
    "search.brave.com",
    "perplexity.ai",
)
ERROR_TITLE = re.compile(
    r"\b(404|500|5\d\d|error|exception|traceback|failed|failure|not found)\b",
    re.IGNORECASE,
)
FAIL_TITLE = re.compile(
    r"(FAILED|Traceback|build failed|exit code [1-9]|✗|\bERR[A-Z_]*\b)",
)


@dataclass(frozen=True, slots=True)
class StruggleSignal:
    name: str
    detail: str
    at: float


@dataclass(frozen=True, slots=True)
class Classification:
    activity_class: str
    signals: tuple[StruggleSignal, ...]


def _window(
    events: list[AmbientEvent], now: float, seconds: float = STUCK_WINDOW_SECONDS
) -> list[AmbientEvent]:
    cutoff = now - seconds
    return sorted(
        (
            event
            for event in events
            if not event.agent_driven and cutoff <= event.started_at <= now
        ),
        key=lambda item: item.started_at,
    )


def detect_search_reword(events: list[AmbientEvent]) -> StruggleSignal | None:
    seen: dict[str, set[str]] = {}
    latest = 0.0
    for event in events:
        if not event.url:
            continue
        try:
            parsed = urlparse(event.url)
        except ValueError:
            continue
        if parsed.hostname not in SEARCH_HOSTS:
            continue
        query = (parse_qs(parsed.query).get("q") or [""])[0].strip()
        if not query:
            continue
        seen.setdefault(parsed.hostname, set()).add(query)
        latest = max(latest, event.started_at)
    for host, queries in seen.items():
        if len(queries) >= 3:
            return StruggleSignal(
                "search_reword", f"{len(queries)} rewordings on {host}", latest
            )
    return None


def detect_error_revisit(events: list[AmbientEvent]) -> StruggleSignal | None:
    visits: dict[str, int] = {}
    latest: dict[str, float] = {}
    for event in events:
        if not ERROR_TITLE.search(event.title) and not ERROR_TITLE.search(event.url):
            continue
        key = event.url or f"{event.app} {event.title}"
        visits[key] = visits.get(key, 0) + 1
        latest[key] = event.started_at
    for key, count in visits.items():
        if count >= 3:
            return StruggleSignal(
                "error_revisit", f"{count} visits to {key[:80]}", latest[key]
            )
    return None


def detect_window_bounce(events: list[AmbientEvent]) -> StruggleSignal | None:
    apps = [
        event.app
        for event in events
        if event.kind in {"window", "tab"} and event.app
    ]
    switches = sum(
        1 for first, second in zip(apps, apps[1:]) if first != second
    )
    if switches >= 6 and len(set(apps)) <= 3:
        return StruggleSignal(
            "window_bounce",
            f"{switches} switches across {len(set(apps))} windows",
            events[-1].started_at if events else 0.0,
        )
    return None


def detect_failing_runs(events: list[AmbientEvent]) -> StruggleSignal | None:
    failures = [
        event
        for event in events
        if event.kind in {"window", "tab"} and FAIL_TITLE.search(event.title)
    ]
    if len(failures) >= 3:
        return StruggleSignal(
            "failing_runs",
            f"{len(failures)} failing runs",
            failures[-1].started_at,
        )
    return None


def detect_input_churn(events: list[AmbientEvent]) -> StruggleSignal | None:
    by_app: dict[str, list[float]] = {}
    for event in events:
        if event.kind in {"window", "tab"} and event.app:
            by_app.setdefault(event.app, []).append(event.started_at)
    for app, stamps in by_app.items():
        stamps.sort()
        for index in range(len(stamps)):
            burst = [stamp for stamp in stamps[index:] if stamp - stamps[index] <= 120.0]
            if len(burst) >= 5:
                return StruggleSignal(
                    "input_churn", f"{len(burst)} updates on {app} in 2 min", burst[-1]
                )
    return None


def detect_long_dwell(events: list[AmbientEvent]) -> StruggleSignal | None:
    # "Little input" is per window, not per slice: a quiet 25-minute dwell
    # on one app still counts when the other signals fired elsewhere.
    foreground = [event for event in events if event.kind in {"window", "tab"}]
    if not foreground:
        return None
    spans: dict[str, list[float]] = {}
    for event in foreground:
        spans.setdefault(event.app, []).append(event.started_at)
    for app, stamps in spans.items():
        if len(stamps) < 5 and max(stamps) - min(stamps) >= 1_500.0:
            return StruggleSignal(
                "long_dwell", f"{app} for 25+ min with little input", max(stamps)
            )
    return None


SIGNAL_DETECTORS = (
    detect_search_reword,
    detect_error_revisit,
    detect_window_bounce,
    detect_failing_runs,
    detect_input_churn,
    detect_long_dwell,
)


def classify(
    events: list[AmbientEvent], *, now: float | None = None
) -> Classification:
    """One verdict over the trailing window. Pure: no store, no side effects."""

    moment = time.time() if now is None else float(now)
    windowed = _window(events, moment)
    if not windowed:
        return Classification("idle", ())
    if windowed[-1].kind == "idle":
        return Classification("idle", ())
    # Long dwell needs a 25+ minute span, which cannot fit inside the
    # 15-minute stuck window: it reads a wider slice of the same buffer.
    wide = _window(events, moment, seconds=LONG_DWELL_WINDOW_SECONDS)
    detectors = [
        (detect, windowed if detect is not detect_long_dwell else wide)
        for detect in SIGNAL_DETECTORS
    ]
    signals = tuple(
        signal for signal in (detect(slice_) for detect, slice_ in detectors)
        if signal is not None
    )
    if len({signal.name for signal in signals}) >= STUCK_SIGNALS_REQUIRED:
        return Classification("stuck", signals)
    foreground = [event for event in windowed if event.kind in {"window", "tab"}]
    tabs = [event for event in foreground if event.kind == "tab"]
    if (
        foreground
        and len(tabs) / len(foreground) >= 0.6
        and len({event.url for event in tabs if event.url}) >= 5
    ):
        return Classification("browsing", signals)
    return Classification("focused", signals)


class StuckLatch:
    """Stuck fires once, then stays quiet until a real cooldown passes.

    Replaying a stuck session must not page a model on every tick, and
    offering help at every stuck moment is exactly the advertisement failure
    mode. The latch reports the rising edge only.
    """

    def __init__(self, cooldown_seconds: float = LATCH_COOLDOWN_SECONDS) -> None:
        self._cooldown = cooldown_seconds
        self._last_stuck: float | None = None

    def check(self, events: list[AmbientEvent], now: float) -> bool:
        result = classify(events, now=now)
        if result.activity_class != "stuck":
            return False
        if self._last_stuck is None or now - self._last_stuck >= self._cooldown:
            self._last_stuck = now
            return True
        self._last_stuck = now
        return False
