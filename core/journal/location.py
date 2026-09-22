"""Where he was, kept on his PC and nowhere else.

Locket's native plugin posts iOS visits ("arrived 2:14pm, left 3:40pm") and
significant-location-change fixes here, over the tailnet, signed with a key
that only this route accepts. He asked for exactly this arrangement: the
permission is already granted to Locket, and the history stays off every
cloud -- Locket's own server is on Railway, which is why this does not go
there even though drives do.

Visits arrive twice from iOS: once on arrival with no departure, and again
when he leaves. The phone gives both the same id, so the second write fills
in the departure instead of creating a second visit.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
KINDS = ("visit", "significant")
MAX_POINTS_PER_POST = 500
MAX_ID_CHARS = 128
MAX_PLACE_CHARS = 200
SECRET_FILE_NAME = "location-ingest.secret"


class LocationPayloadError(ValueError):
    """The phone sent something this store will not take."""


@dataclass(frozen=True)
class Visit:
    lat: float
    lng: float
    arrived: float
    departed: float | None
    place: str
    accuracy_m: float | None
    # True when iOS never reported the departure and it was read off the next
    # place he turned up instead.
    departed_inferred: bool = False

    def minutes(self, until: float | None = None) -> int:
        end = self.departed or until or time.time()
        return max(0, int((end - self.arrived) // 60))


def db_path() -> Path:
    configured = os.environ.get("SERENA_LOCATION_DB", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "serena" / "location.sqlite3")


def secret_path() -> Path:
    return Path(os.environ.get("SERENA_CONFIG_DIR", "") or Path.home() / ".config" / "serena") / SECRET_FILE_NAME


def route_secret() -> str:
    try:
        return secret_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS points ("
        " id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
        " lat REAL NOT NULL, lng REAL NOT NULL, accuracy_m REAL,"
        " at REAL NOT NULL, arrived REAL, departed REAL,"
        " place TEXT NOT NULL DEFAULT '', received_at REAL NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS points_at_idx ON points(at)")
    if os.name != "nt":
        with suppress(OSError):
            target.chmod(0o600)
    return connection


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LocationPayloadError(f"{field} must be a finite number")
    if not low <= float(value) <= high:
        raise LocationPayloadError(f"{field} is out of range")
    return float(value)


def _optional_time(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _number(value, field, 946_684_800, 4_102_444_800)  # 2000 .. 2100


def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
    points = payload.get("points")
    if not isinstance(points, list) or not points:
        raise LocationPayloadError("points must be a non-empty list")
    if len(points) > MAX_POINTS_PER_POST:
        raise LocationPayloadError(f"at most {MAX_POINTS_PER_POST} points per post")
    clean = []
    for raw in points:
        if not isinstance(raw, dict):
            raise LocationPayloadError("each point must be an object")
        point_id = str(raw.get("id") or "").strip()
        if not point_id or len(point_id) > MAX_ID_CHARS:
            raise LocationPayloadError("each point needs a short id")
        kind = str(raw.get("kind") or "")
        if kind not in KINDS:
            raise LocationPayloadError(f"kind must be one of {KINDS}")
        at = _optional_time(raw.get("at"), "at")
        if at is None:
            raise LocationPayloadError("each point needs a time")
        clean.append({
            "id": point_id, "kind": kind,
            "lat": _number(raw.get("lat"), "lat", -90, 90),
            "lng": _number(raw.get("lng"), "lng", -180, 180),
            "accuracy_m": None if raw.get("accuracy") is None
            else _number(raw.get("accuracy"), "accuracy", 0, 100_000),
            "at": at,
            "arrived": _optional_time(raw.get("arrived"), "arrived"),
            "departed": _optional_time(raw.get("departed"), "departed"),
            "place": str(raw.get("place") or "")[:MAX_PLACE_CHARS],
        })
    return clean


def store(points: list[dict[str, Any]], *, now: float | None = None, path: Path | None = None) -> int:
    moment = float(time.time() if now is None else now)
    with closing(_connect(path)) as connection, connection:
        for p in points:
            connection.execute(
                "INSERT INTO points(id, kind, lat, lng, accuracy_m, at, arrived, departed, place, received_at)"
                " VALUES (:id, :kind, :lat, :lng, :accuracy_m, :at, :arrived, :departed, :place, :received_at)"
                " ON CONFLICT(id) DO UPDATE SET"
                # A departure never un-happens, and a later geocode is never
                # replaced by an empty one.
                "  departed = COALESCE(excluded.departed, points.departed),"
                "  place = CASE WHEN excluded.place <> '' THEN excluded.place ELSE points.place END,"
                "  received_at = excluded.received_at",
                {**p, "received_at": moment})
    return len(points)


# His day runs until 5am, not midnight: getting home at 12:27am is the end of
# Monday, not the start of Tuesday.
DAY_STARTS_HOUR = 5
# A departure is only inferred from what came next when that is close enough
# to mean "he left, then turned up there" -- not "the phone was off for a day".
MAX_INFERRED_GAP_SECONDS = 12 * 3600
LEFT_RADIUS_M = 300.0


def day_window(day: date) -> tuple[float, float]:
    start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=DAY_STARTS_HOUR)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def all_visits(*, since: float = 0.0, path: Path | None = None) -> list[Visit]:
    """Every visit, oldest first, with a missing departure read off what came next.

    iOS drops a departure whenever it cannot deliver it -- the phone died, say,
    which is exactly what happened on the first night this ran. The next thing
    he did still happened: the first fix far enough away, or his arrival
    somewhere else, is when he had left by. Nothing is inferred across a gap
    long enough to mean the phone was simply off.
    """

    with closing(_connect(path)) as connection:
        visits = connection.execute(
            "SELECT * FROM points WHERE kind = 'visit' AND COALESCE(departed, arrived, at) >= ?"
            " ORDER BY COALESCE(arrived, at)", (since,)).fetchall()
        fixes = connection.execute(
            "SELECT lat, lng, at FROM points WHERE kind = 'significant' AND at >= ? ORDER BY at",
            (since,)).fetchall()
    out: list[Visit] = []
    for i, row in enumerate(visits):
        arrived = row["arrived"] or row["at"]
        departed, inferred = row["departed"], False
        if departed is None:
            later = [v["arrived"] or v["at"] for v in visits[i + 1:]]
            away = [f["at"] for f in fixes if f["at"] > arrived and
                    _distance_m(row["lat"], row["lng"], f["lat"], f["lng"]) > LEFT_RADIUS_M]
            candidates = [t for t in (later[:1] + away[:1]) if t - arrived <= MAX_INFERRED_GAP_SECONDS]
            if candidates:
                departed, inferred = min(candidates), True
        out.append(Visit(lat=row["lat"], lng=row["lng"], arrived=arrived, departed=departed,
                         place=row["place"], accuracy_m=row["accuracy_m"],
                         departed_inferred=inferred))
    return out


def visits_on(day: date, *, path: Path | None = None) -> list[Visit]:
    """Visits that overlap his day (5am to 5am), oldest first."""

    start, end = day_window(day)
    return [v for v in all_visits(since=start - 2 * 86400, path=path)
            if v.arrived < end and (v.departed or end) > start]


def route_location(payload: dict[str, Any], _request: Any):
    from core.webhook_ingress import RouteOutcome

    try:
        saved = store(validate(payload))
    except LocationPayloadError as exc:
        return RouteOutcome(False, str(exc))
    return RouteOutcome(True, f"stored {saved} location points")


def validate_payload(payload: dict[str, Any]) -> None:
    validate(payload)
