"""Home and work, worked out from where he actually spends his time.

He should never be asked where he was when the answer is home or work -- the
first real entry asked him three times, and all three were one of the two.
Neither needs a name from him: home is where he spends the night, work is
where he spends weekday daytime that is not home. Both come from his own
location history on this PC, so they follow him if either ever moves.

A name he gives a place himself always wins over these.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.journal.location import TZ, Visit, _distance_m, all_visits

CLUSTER_RADIUS_M = 200.0
LOOKBACK_DAYS = 60
# Enough to be sure without waiting weeks: one night at home, most of one
# working day at work.
MIN_OVERNIGHT_MINUTES = 60
MIN_WORKDAY_MINUTES = 180
NIGHT = (2, 5)        # 2am-5am: asleep, wherever he lives
WORKDAY = (10, 16)    # 10am-4pm on a weekday: at work, if he works somewhere


@dataclass
class _Cluster:
    lat: float
    lng: float
    visits: list[Visit]
    overnight: float = 0.0
    workday: float = 0.0


def _overlap(start: float, end: float, window: tuple[int, int], *, weekdays_only: bool) -> float:
    """Minutes of [start, end] that fall inside the daily local window."""

    total = 0.0
    day = datetime.fromtimestamp(start, TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, TZ).date()
    while day <= last:
        if not weekdays_only or day.weekday() < 5:
            lo = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=window[0])
            hi = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=window[1])
            total += max(0.0, min(end, hi.timestamp()) - max(start, lo.timestamp()))
        day += timedelta(days=1)
    return total / 60


def _clusters(visits: list[Visit], now: float) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for visit in visits:
        # A visit still in progress counts up to now, and an unending one no
        # further than a day, so one lost departure cannot crown a place.
        end = visit.departed or min(now, visit.arrived + 86400)
        home = next((c for c in clusters
                     if _distance_m(c.lat, c.lng, visit.lat, visit.lng) <= CLUSTER_RADIUS_M), None)
        if home is None:
            home = _Cluster(visit.lat, visit.lng, [])
            clusters.append(home)
        home.visits.append(visit)
        home.overnight += _overlap(visit.arrived, end, NIGHT, weekdays_only=False)
        home.workday += _overlap(visit.arrived, end, WORKDAY, weekdays_only=True)
    return clusters


def anchors(*, now: float | None = None, path: Path | None = None) -> dict[str, tuple[float, float]]:
    """{"home": (lat, lng), "work": (lat, lng)}, each only when the history shows it."""

    moment = time.time() if now is None else now
    clusters = _clusters(all_visits(since=moment - LOOKBACK_DAYS * 86400, path=path), moment)
    found: dict[str, tuple[float, float]] = {}
    home = max(clusters, key=lambda c: c.overnight, default=None)
    if home and home.overnight >= MIN_OVERNIGHT_MINUTES:
        found["home"] = (home.lat, home.lng)
    rest = [c for c in clusters if c is not home]
    work = max(rest, key=lambda c: c.workday, default=None)
    if work and work.workday >= MIN_WORKDAY_MINUTES:
        found["work"] = (work.lat, work.lng)
    return found


def anchor_for(lat: float, lng: float, *, now: float | None = None,
               path: Path | None = None) -> str:
    for name, (a_lat, a_lng) in anchors(now=now, path=path).items():
        if _distance_m(lat, lng, a_lat, a_lng) <= CLUSTER_RADIUS_M:
            return name
    return ""
