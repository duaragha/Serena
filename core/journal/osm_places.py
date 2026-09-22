"""Name a place from offline OpenStreetMap data, on this machine only.

Built by scripts/build_osm_places.py. No coordinate is ever sent anywhere:
the lookup is a query against a local SQLite file, which is the whole point --
a geocoding service would learn every place he goes.

A named place within POI_RADIUS_M of a visit names it. Failing that, a street
address within ADDRESS_RADIUS_M says whose street it was on, which is enough
to ask him "whose place on Sandalwood Pkwy?" instead of a bare time range.
"""

from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

# iOS reports visits to within ~10-20m; a place's point is its entrance or the
# middle of its building. Further than this, it is a guess about a neighbour.
POI_RADIUS_M = 50.0
ADDRESS_RADIUS_M = 40.0


def db_path() -> Path:
    configured = os.environ.get("SERENA_OSM_PLACES", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "serena" / "osm-places.sqlite3")


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


def _nearest(db: sqlite3.Connection, table: str, columns: str, lat: float, lng: float,
             radius: float) -> tuple[sqlite3.Row, float] | None:
    dlat = radius / 111_320
    dlng = radius / (111_320 * max(0.2, math.cos(math.radians(lat))))
    rows = db.execute(
        f"SELECT {columns}, lat, lng FROM {table} WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
        (lat - dlat, lat + dlat, lng - dlng, lng + dlng)).fetchall()
    scored = [(row, _distance_m(lat, lng, row["lat"], row["lng"])) for row in rows]
    scored = [pair for pair in scored if pair[1] <= radius]
    return min(scored, key=lambda pair: pair[1]) if scored else None


def lookup(lat: float, lng: float) -> dict[str, Any] | None:
    """{"name", "kind", "distance_m"} for a place, {"street"} for a house, or None."""

    path = db_path()
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        poi = _nearest(db, "pois", "name, kind", lat, lng, POI_RADIUS_M)
        if poi:
            row, distance = poi
            return {"name": row["name"], "kind": row["kind"], "distance_m": round(distance)}
        address = _nearest(db, "addresses", "street", lat, lng, ADDRESS_RADIUS_M)
        if address:
            return {"street": address[0]["street"]}
    return None
