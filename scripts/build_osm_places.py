"""Build the offline place index the journal names his visits with.

Location gives the journal coordinates, not names, and the obvious fix -- a
geocoding service -- would send every place he goes to Apple or Google, which
is the thing he kept this history off the cloud to avoid. So names come from
OpenStreetMap data downloaded once and looked up on the PC. Nothing about
where he went leaves the machine; the download is the same public file for
everyone.

    python3 -m venv /tmp/osm && /tmp/osm/bin/pip install osmium
    curl -LO https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf
    /tmp/osm/bin/python scripts/build_osm_places.py ontario-latest.osm.pbf osm-places.sqlite3

Then copy the result to ~/.local/state/serena/osm-places.sqlite3 on the PC.
It holds every named place in Ontario (cafes, restaurants, shops, gyms,
parks, venues...) and, inside the GTA, street addresses, so a friend's house
can at least be "a house on Sandalwood Pkwy" and be asked about.

pyosmium is deliberately not a Serena dependency: this runs rarely, by hand,
and only the SQLite file it produces is needed at runtime.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import osmium

# Tags whose named features are places he could have been.
PLACE_KEYS = ("amenity", "shop", "leisure", "tourism", "office", "craft",
              "healthcare", "sport", "club", "historic", "public_transport")
# Named features of these kinds are areas, not destinations.
SKIP_KINDS = {"parking", "parking_space", "bench", "waste_basket", "vending_machine",
              "bicycle_parking", "post_box", "recycling", "toilets", "fire_hydrant",
              "atm", "telephone", "drinking_water", "shelter", "platform", "stop_position"}
# Addresses are kept only where he lives and works; province-wide they would
# be millions of rows for places he will never be.
GTA = (43.35, -80.20, 44.05, -78.90)  # south, west, north, east


def _in_gta(lat: float, lng: float) -> bool:
    return GTA[0] <= lat <= GTA[2] and GTA[1] <= lng <= GTA[3]


class Collector(osmium.SimpleHandler):
    def __init__(self, db: sqlite3.Connection) -> None:
        super().__init__()
        self.db = db
        self.pois = 0
        self.addresses = 0

    def _emit(self, tags, lat: float, lng: float) -> None:
        name = tags.get("name")
        if name:
            kind = next((f"{k}={tags[k]}" for k in PLACE_KEYS if k in tags), "")
            if not kind and "building" in tags:
                kind = f"building={tags['building']}"
            if kind and kind.split("=", 1)[1] not in SKIP_KINDS:
                self.db.execute("INSERT INTO pois(name, kind, lat, lng) VALUES (?, ?, ?, ?)",
                                (name, kind, lat, lng))
                self.pois += 1
                return
        street = tags.get("addr:street")
        if street and tags.get("addr:housenumber") and _in_gta(lat, lng):
            self.db.execute("INSERT INTO addresses(street, number, lat, lng) VALUES (?, ?, ?, ?)",
                            (street, tags.get("addr:housenumber"), lat, lng))
            self.addresses += 1

    def node(self, n) -> None:
        if n.tags and n.location.valid():
            self._emit(n.tags, n.location.lat, n.location.lon)

    def way(self, w) -> None:
        if not w.tags or not ("name" in w.tags or "addr:street" in w.tags):
            return
        points = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        if not points:
            return
        lat = sum(p[0] for p in points) / len(points)
        lng = sum(p[1] for p in points) / len(points)
        self._emit(w.tags, lat, lng)


def build(source: Path, target: Path) -> tuple[int, int]:
    if target.exists():
        target.unlink()
    db = sqlite3.connect(target)
    db.executescript(
        """
        CREATE TABLE pois (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                           lat REAL NOT NULL, lng REAL NOT NULL);
        CREATE TABLE addresses (id INTEGER PRIMARY KEY, street TEXT NOT NULL, number TEXT,
                                lat REAL NOT NULL, lng REAL NOT NULL);
        """)
    collector = Collector(db)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        # Node locations go to a file index: Ontario's ~150M nodes do not fit
        # beside everything else in a laptop's free memory.
        collector.apply_file(str(source), locations=True,
                             idx=f"sparse_file_array,{Path(scratch) / 'nodes.idx'}")
    db.executescript(
        """
        CREATE INDEX pois_lat ON pois(lat, lng);
        CREATE INDEX addresses_lat ON addresses(lat, lng);
        """)
    db.commit()
    db.execute("VACUUM")
    db.close()
    return collector.pois, collector.addresses


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: build_osm_places.py <ontario-latest.osm.pbf> <out.sqlite3>")
    pois, addresses = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"{pois} named places, {addresses} GTA addresses")
