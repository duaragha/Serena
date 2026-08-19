"""Durable local graph and normalized event stream for Serena's world state.

The graph is deliberately separate from personal memory. Memory records what
Serena believes and why. This store records current entities, relationships,
and the observations that produced those projections. Every update appends an
immutable event in the same SQLite transaction as the projection change.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import socket
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "state-graph.sqlite3"
SCHEMA_VERSION = 2

ENTITY_KINDS = frozenset(
    {
        "person",
        "device",
        "display",
        "room",
        "app",
        "service",
        "project",
        "capability",
        "permission",
        "location",
        "world_item",
    }
)

MAX_JSON_BYTES = 64 * 1024
MAX_EVENTS_LIMIT = 2_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PREDICATE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DISPLAY = re.compile(
    r"^(?P<connector>\S+) connected(?P<primary> primary)?"
    r"(?: (?P<width>\d+)x(?P<height>\d+)\+\d+\+\d+)?"
)


class StateGraphError(ValueError):
    """A graph update was malformed or unsafe to store."""


@dataclass(frozen=True, slots=True)
class GraphEntity:
    entity_id: str
    kind: str
    name: str
    attributes: dict[str, Any]
    source: str
    observed_at: float
    expires_at: float | None
    created_at: float
    updated_at: float

    def freshness(self, *, now: float | None = None) -> str:
        if self.expires_at is None:
            return "unknown"
        return "fresh" if float(time.time() if now is None else now) <= self.expires_at else "stale"

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        value = asdict(self)
        value["freshness"] = self.freshness(now=now)
        return value


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    subject_id: str
    predicate: str
    object_id: str
    attributes: dict[str, Any]
    source: str
    observed_at: float
    expires_at: float | None
    created_at: float
    updated_at: float

    def freshness(self, *, now: float | None = None) -> str:
        if self.expires_at is None:
            return "unknown"
        return "fresh" if float(time.time() if now is None else now) <= self.expires_at else "stale"

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        value = asdict(self)
        value["freshness"] = self.freshness(now=now)
        return value


@dataclass(frozen=True, slots=True)
class GraphEvent:
    sequence: int
    event_id: str
    event_type: str
    subject_id: str | None
    source: str
    payload: dict[str, Any]
    occurred_at: float
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateGraphStore:
    """SQLite authority for local entities, edges, and observation events."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_STATE_GRAPH_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._initialize()

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM graph_migrations").fetchone()
        return int(row[0] or 0)

    def upsert_entity(
        self,
        entity_id: str,
        *,
        kind: str,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        source: str,
        observed_at: float | None = None,
        ttl_seconds: float | None = None,
        event_id: str | None = None,
    ) -> GraphEntity:
        clean_id = _identifier(entity_id, "entity_id")
        clean_kind = _kind(kind)
        clean_name = _text(name, "name", 500)
        clean_source = _text(source, "source", 256)
        clean_attributes = _json_object(attributes or {}, "attributes")
        observed = _timestamp(observed_at)
        expires = _expiry(observed, ttl_seconds)
        clean_event = _identifier(event_id or str(uuid.uuid4()), "event_id")
        now = time.time()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._event_exists(connection, clean_event):
                row = connection.execute(
                    "SELECT * FROM graph_entities WHERE entity_id = ?", (clean_id,)
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        "idempotent graph event exists without its entity projection"
                    )
                return _entity_from_row(row)
            connection.execute(
                """
                INSERT INTO graph_entities(
                    entity_id, kind, name, attributes_json, source, observed_at,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    kind = excluded.kind,
                    name = excluded.name,
                    attributes_json = excluded.attributes_json,
                    source = excluded.source,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    clean_id,
                    clean_kind,
                    clean_name,
                    _json(clean_attributes),
                    clean_source,
                    observed,
                    now,
                    now,
                    expires,
                ),
            )
            self._insert_event(
                connection,
                event_id=clean_event,
                event_type="entity.upserted",
                subject_id=clean_id,
                source=clean_source,
                payload={
                    "kind": clean_kind,
                    "name": clean_name,
                    "attributes": clean_attributes,
                    "expires_at": expires,
                },
                occurred_at=observed,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM graph_entities WHERE entity_id = ?", (clean_id,)
            ).fetchone()
        assert row is not None
        return _entity_from_row(row)

    def upsert_edge(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        source: str,
        observed_at: float | None = None,
        ttl_seconds: float | None = None,
        edge_id: str | None = None,
        event_id: str | None = None,
    ) -> GraphEdge:
        subject = _identifier(subject_id, "subject_id")
        relation = _predicate(predicate)
        object_ = _identifier(object_id, "object_id")
        clean_edge = _identifier(edge_id or _stable_edge_id(subject, relation, object_), "edge_id")
        clean_event = _identifier(event_id or str(uuid.uuid4()), "event_id")
        clean_source = _text(source, "source", 256)
        clean_attributes = _json_object(attributes or {}, "attributes")
        observed = _timestamp(observed_at)
        expires = _expiry(observed, ttl_seconds)
        now = time.time()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._event_exists(connection, clean_event):
                row = connection.execute(
                    "SELECT * FROM graph_edges WHERE edge_id = ?", (clean_edge,)
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotent graph event exists without its edge projection")
                return _edge_from_row(row)
            self._require_entity(connection, subject)
            self._require_entity(connection, object_)
            connection.execute(
                """
                INSERT INTO graph_edges(
                    edge_id, subject_id, predicate, object_id, attributes_json,
                    source, observed_at, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    predicate = excluded.predicate,
                    object_id = excluded.object_id,
                    attributes_json = excluded.attributes_json,
                    source = excluded.source,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    clean_edge,
                    subject,
                    relation,
                    object_,
                    _json(clean_attributes),
                    clean_source,
                    observed,
                    now,
                    now,
                    expires,
                ),
            )
            self._insert_event(
                connection,
                event_id=clean_event,
                event_type="edge.upserted",
                subject_id=subject,
                source=clean_source,
                payload={
                    "edge_id": clean_edge,
                    "predicate": relation,
                    "object_id": object_,
                    "attributes": clean_attributes,
                    "expires_at": expires,
                },
                occurred_at=observed,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM graph_edges WHERE edge_id = ?", (clean_edge,)
            ).fetchone()
        assert row is not None
        return _edge_from_row(row)

    def record_event(
        self,
        event_type: str,
        *,
        source: str,
        payload: Mapping[str, Any] | None = None,
        subject_id: str | None = None,
        occurred_at: float | None = None,
        event_id: str | None = None,
    ) -> GraphEvent:
        clean_event = _identifier(event_id or str(uuid.uuid4()), "event_id")
        clean_type = _event_type(event_type)
        clean_source = _text(source, "source", 256)
        clean_subject = _identifier(subject_id, "subject_id") if subject_id else None
        clean_payload = _json_object(payload or {}, "payload")
        occurred = _timestamp(occurred_at)
        created = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM graph_events WHERE event_id = ?", (clean_event,)
            ).fetchone()
            if existing is not None:
                return _event_from_row(existing)
            self._insert_event(
                connection,
                event_id=clean_event,
                event_type=clean_type,
                subject_id=clean_subject,
                source=clean_source,
                payload=clean_payload,
                occurred_at=occurred,
                created_at=created,
            )
            row = connection.execute(
                "SELECT * FROM graph_events WHERE event_id = ?", (clean_event,)
            ).fetchone()
        assert row is not None
        return _event_from_row(row)

    def entity(self, entity_id: str) -> GraphEntity | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM graph_entities WHERE entity_id = ?",
                (_identifier(entity_id, "entity_id"),),
            ).fetchone()
        return _entity_from_row(row) if row is not None else None

    def entities(
        self,
        *,
        kind: str | None = None,
        online: bool | None = None,
        include_stale: bool = True,
        now: float | None = None,
    ) -> list[GraphEntity]:
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(_kind(kind))
        moment = float(time.time() if now is None else now)
        if not include_stale:
            clauses.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(moment)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM graph_entities" + where + " ORDER BY kind, name, entity_id",
                tuple(params),
            ).fetchall()
        entities = [_entity_from_row(row) for row in rows]
        if online is not None:
            entities = [item for item in entities if bool(item.attributes.get("online")) is online]
        return entities

    def edges(
        self,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        object_id: str | None = None,
        include_stale: bool = True,
        now: float | None = None,
    ) -> list[GraphEdge]:
        clauses: list[str] = []
        params: list[object] = []
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(_identifier(subject_id, "subject_id"))
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(_predicate(predicate))
        if object_id is not None:
            clauses.append("object_id = ?")
            params.append(_identifier(object_id, "object_id"))
        if not include_stale:
            clauses.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(float(time.time() if now is None else now))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM graph_edges" + where + " ORDER BY predicate, edge_id",
                tuple(params),
            ).fetchall()
        return [_edge_from_row(row) for row in rows]

    def neighbors(
        self,
        entity_id: str,
        *,
        predicate: str | None = None,
        direction: str = "out",
        include_stale: bool = True,
        now: float | None = None,
    ) -> list[GraphEntity]:
        clean_id = _identifier(entity_id, "entity_id")
        if direction not in {"out", "in", "both"}:
            raise StateGraphError("direction must be out, in, or both")
        ids: list[str] = []
        if direction in {"out", "both"}:
            ids.extend(
                edge.object_id
                for edge in self.edges(
                    subject_id=clean_id,
                    predicate=predicate,
                    include_stale=include_stale,
                    now=now,
                )
            )
        if direction in {"in", "both"}:
            ids.extend(
                edge.subject_id
                for edge in self.edges(
                    object_id=clean_id,
                    predicate=predicate,
                    include_stale=include_stale,
                    now=now,
                )
            )
        result: list[GraphEntity] = []
        for neighbor_id in dict.fromkeys(ids):
            item = self.entity(neighbor_id)
            if item is not None and (
                include_stale or item.expires_at is None or item.freshness(now=now) == "fresh"
            ):
                result.append(item)
        return result

    def events(
        self,
        *,
        after_sequence: int = 0,
        event_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 200,
    ) -> list[GraphEvent]:
        clauses = ["sequence > ?"]
        params: list[object] = [max(0, int(after_sequence))]
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(_event_type(event_type))
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(_identifier(subject_id, "subject_id"))
        params.append(min(MAX_EVENTS_LIMIT, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM graph_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def latest_sequence(self) -> int:
        """Return the stream cursor without loading historical event payloads."""

        with self._connect() as connection:
            row = connection.execute("SELECT MAX(sequence) FROM graph_events").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _event_exists(connection: sqlite3.Connection, event_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM graph_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _require_entity(connection: sqlite3.Connection, entity_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM graph_entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(f"unknown graph entity {entity_id}")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        event_type: str,
        subject_id: str | None,
        source: str,
        payload: Mapping[str, Any],
        occurred_at: float,
        created_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO graph_events(
                event_id, event_type, subject_id, source, payload_json,
                occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                subject_id,
                source,
                _json(_json_object(payload, "event payload")),
                occurred_at,
                created_at,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS graph_migrations("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                int(row[0]) for row in connection.execute("SELECT version FROM graph_migrations")
            }
            if 1 not in applied:
                connection.executescript(
                    """
                    CREATE TABLE graph_entities (
                        entity_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        attributes_json TEXT NOT NULL,
                        source TEXT NOT NULL,
                        observed_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE graph_edges (
                        edge_id TEXT PRIMARY KEY,
                        subject_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                        predicate TEXT NOT NULL,
                        object_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                        attributes_json TEXT NOT NULL,
                        source TEXT NOT NULL,
                        observed_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE graph_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        subject_id TEXT,
                        source TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        occurred_at REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO graph_migrations(version, applied_at) VALUES (1, ?)",
                    (time.time(),),
                )
            if 2 not in applied:
                entity_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(graph_entities)")
                }
                edge_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(graph_edges)")
                }
                if "expires_at" not in entity_columns:
                    connection.execute("ALTER TABLE graph_entities ADD COLUMN expires_at REAL")
                if "expires_at" not in edge_columns:
                    connection.execute("ALTER TABLE graph_edges ADD COLUMN expires_at REAL")
                connection.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS graph_entities_kind_idx
                        ON graph_entities(kind, updated_at);
                    CREATE INDEX IF NOT EXISTS graph_entities_freshness_idx
                        ON graph_entities(expires_at, kind);
                    CREATE INDEX IF NOT EXISTS graph_edges_subject_idx
                        ON graph_edges(subject_id, predicate);
                    CREATE INDEX IF NOT EXISTS graph_edges_object_idx
                        ON graph_edges(object_id, predicate);
                    CREATE INDEX IF NOT EXISTS graph_events_stream_idx
                        ON graph_events(sequence, event_type);
                    """
                )
                connection.execute(
                    "INSERT INTO graph_migrations(version, applied_at) VALUES (2, ?)",
                    (time.time(),),
                )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def register_current_system(
    store: StateGraphStore,
    *,
    runner: Callable[..., Any] = subprocess.run,
    hostname: str | None = None,
    observed_at: float | None = None,
) -> dict[str, Any]:
    """Register this laptop, its default browser, and connected displays.

    Native probes are bounded and optional. Missing desktop tools create an
    explicit unavailable projection instead of pretending no display exists.
    """

    observed = _timestamp(observed_at)
    host = _slug(hostname or socket.gethostname()) or "local"
    source = "local-system-discovery"
    laptop_id = f"device:{host}"
    person_id = "person:raghav"
    location_id = f"location:{host}"
    permission_id = "permission:local-device-control"

    store.upsert_entity(
        person_id,
        kind="person",
        name="Raghav",
        attributes={"local_user": True},
        source=source,
        observed_at=observed,
    )
    store.upsert_entity(
        laptop_id,
        kind="device",
        name=hostname or socket.gethostname(),
        attributes={
            "device_type": "laptop",
            "hostname": hostname or socket.gethostname(),
            "platform": platform.system().lower(),
            "platform_release": platform.release(),
            "online": True,
        },
        source=source,
        observed_at=observed,
        ttl_seconds=300,
    )
    store.upsert_entity(
        location_id,
        kind="location",
        name="Current laptop location",
        attributes={"precision": "unspecified"},
        source=source,
        observed_at=observed,
    )
    store.upsert_entity(
        permission_id,
        kind="permission",
        name="Local device control",
        attributes={"scope": "local", "authority_required": True},
        source=source,
        observed_at=observed,
    )
    store.upsert_edge(person_id, "owns", laptop_id, source=source, observed_at=observed)
    store.upsert_edge(laptop_id, "located_at", location_id, source=source, observed_at=observed)
    store.upsert_edge(person_id, "holds", permission_id, source=source, observed_at=observed)
    store.upsert_edge(
        permission_id,
        "applies_to",
        laptop_id,
        source=source,
        observed_at=observed,
    )

    default_browser = _probe(runner, ["xdg-settings", "get", "default-web-browser"]).strip()
    browser_slug = _slug(default_browser.removesuffix(".desktop")) or "unknown"
    browser_id = f"app:browser:{browser_slug}"
    store.upsert_entity(
        browser_id,
        kind="app",
        name=default_browser or "Default browser unavailable",
        attributes={
            "app_type": "browser",
            "desktop_id": default_browser,
            "online": bool(default_browser),
            "discovery_state": "available" if default_browser else "unavailable",
        },
        source=source,
        observed_at=observed,
        ttl_seconds=3_600,
    )
    capability_id = "capability:web-browsing"
    store.upsert_entity(
        capability_id,
        kind="capability",
        name="Web browsing",
        attributes={"local": True},
        source=source,
        observed_at=observed,
    )
    store.upsert_edge(
        laptop_id,
        "runs",
        browser_id,
        source=source,
        observed_at=observed,
        ttl_seconds=3_600,
    )
    store.upsert_edge(browser_id, "supports", capability_id, source=source, observed_at=observed)

    displays = _parse_displays(_probe(runner, ["xrandr", "--query", "--current"]))
    if not displays:
        displays = [
            {
                "connector": "unknown",
                "primary": False,
                "resolution": None,
                "online": False,
                "discovery_state": "unavailable",
            }
        ]
    display_ids: list[str] = []
    for display in displays:
        display_id = f"display:{host}:{_slug(display['connector']) or 'unknown'}"
        display_ids.append(display_id)
        store.upsert_entity(
            display_id,
            kind="display",
            name=str(display["connector"]),
            attributes=display,
            source=source,
            observed_at=observed,
            ttl_seconds=300,
        )
        store.upsert_edge(
            laptop_id,
            "connected_to",
            display_id,
            source=source,
            observed_at=observed,
            ttl_seconds=300,
        )

    return {
        "person_id": person_id,
        "laptop_id": laptop_id,
        "permission_id": permission_id,
        "browser_id": browser_id,
        "display_ids": display_ids,
        "event_sequence": store.latest_sequence(),
    }


def _probe(runner: Callable[..., Any], command: list[str]) -> str:
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if int(getattr(completed, "returncode", 1)) != 0:
        return ""
    return str(getattr(completed, "stdout", "") or "")[:32_000]


def _parse_displays(raw: str) -> list[dict[str, Any]]:
    displays: list[dict[str, Any]] = []
    for line in raw.splitlines():
        match = _DISPLAY.match(line)
        if match is None:
            continue
        width, height = match.group("width"), match.group("height")
        displays.append(
            {
                "connector": match.group("connector"),
                "primary": bool(match.group("primary")),
                "resolution": f"{width}x{height}" if width and height else None,
                "online": True,
                "discovery_state": "available",
            }
        )
    return displays


def _entity_from_row(row: sqlite3.Row) -> GraphEntity:
    return GraphEntity(
        entity_id=str(row["entity_id"]),
        kind=str(row["kind"]),
        name=str(row["name"]),
        attributes=json.loads(row["attributes_json"]),
        source=str(row["source"]),
        observed_at=float(row["observed_at"]),
        expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
    return GraphEdge(
        edge_id=str(row["edge_id"]),
        subject_id=str(row["subject_id"]),
        predicate=str(row["predicate"]),
        object_id=str(row["object_id"]),
        attributes=json.loads(row["attributes_json"]),
        source=str(row["source"]),
        observed_at=float(row["observed_at"]),
        expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> GraphEvent:
    return GraphEvent(
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        subject_id=str(row["subject_id"]) if row["subject_id"] is not None else None,
        source=str(row["source"]),
        payload=json.loads(row["payload_json"]),
        occurred_at=float(row["occurred_at"]),
        created_at=float(row["created_at"]),
    )


def _identifier(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(clean):
        raise StateGraphError(f"{label} must be a short stable identifier")
    return clean


def _kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in ENTITY_KINDS:
        raise StateGraphError(f"unknown entity kind {clean!r}")
    return clean


def _predicate(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _PREDICATE.fullmatch(clean):
        raise StateGraphError("predicate must be a short lowercase relation name")
    return clean


def _event_type(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _PREDICATE.fullmatch(clean):
        raise StateGraphError("event_type must be a short lowercase dotted name")
    return clean


def _text(value: object, label: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())[:limit]
    if not clean:
        raise StateGraphError(f"{label} is required")
    return clean


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StateGraphError(f"{label} must be an object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise StateGraphError(f"{label} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise StateGraphError(f"{label} exceeds {MAX_JSON_BYTES} bytes")
    return decoded


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _timestamp(value: float | None) -> float:
    moment = float(time.time() if value is None else value)
    if not math.isfinite(moment) or moment < 0:
        raise StateGraphError("timestamp must be a finite non-negative number")
    return moment


def _expiry(observed_at: float, ttl_seconds: float | None) -> float | None:
    if ttl_seconds is None:
        return None
    ttl = float(ttl_seconds)
    if not math.isfinite(ttl) or ttl <= 0:
        raise StateGraphError("ttl_seconds must be positive")
    return observed_at + ttl


def _stable_edge_id(subject: str, predicate: str, object_: str) -> str:
    digest = hashlib.sha256(f"{subject}\0{predicate}\0{object_}".encode()).hexdigest()[:32]
    return f"edge:{digest}"


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")[:64]
