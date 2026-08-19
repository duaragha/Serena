from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from core.state_graph import StateGraphError, StateGraphStore, register_current_system


def test_entity_edge_and_event_stream_are_durable_and_idempotent(tmp_path) -> None:
    path = tmp_path / "graph.sqlite3"
    store = StateGraphStore(path)

    first = store.upsert_entity(
        "person:raghav",
        kind="person",
        name="Raghav",
        attributes={"online": True},
        source="test",
        observed_at=100.0,
        ttl_seconds=10.0,
        event_id="event:person:1",
    )
    replay = store.upsert_entity(
        "person:raghav",
        kind="person",
        name="Changed by replay",
        attributes={"online": False},
        source="test",
        observed_at=101.0,
        event_id="event:person:1",
    )
    store.upsert_entity(
        "device:laptop",
        kind="device",
        name="Laptop",
        attributes={"online": True},
        source="test",
        observed_at=100.0,
    )
    edge = store.upsert_edge(
        "person:raghav",
        "owns",
        "device:laptop",
        source="test",
        observed_at=100.0,
        event_id="event:edge:1",
    )

    assert first.name == "Raghav"
    assert replay.name == "Raghav"
    assert first.freshness(now=109.0) == "fresh"
    assert first.freshness(now=111.0) == "stale"
    assert edge.predicate == "owns"
    assert [item.entity_id for item in store.neighbors("person:raghav")] == ["device:laptop"]
    events = store.events()
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert sum(event.event_id == "event:person:1" for event in events) == 1

    reopened = StateGraphStore(path)
    assert reopened.entity("person:raghav").attributes["online"] is True  # type: ignore[union-attr]
    assert reopened.edges(subject_id="person:raghav")[0].object_id == "device:laptop"


def test_queries_filter_online_and_freshness_and_validate_relationships(tmp_path) -> None:
    store = StateGraphStore(tmp_path / "graph.sqlite3")
    store.upsert_entity(
        "service:online",
        kind="service",
        name="Online",
        attributes={"online": True},
        source="test",
        observed_at=10,
        ttl_seconds=5,
    )
    store.upsert_entity(
        "service:offline",
        kind="service",
        name="Offline",
        attributes={"online": False},
        source="test",
        observed_at=10,
        ttl_seconds=50,
    )

    assert [item.entity_id for item in store.entities(kind="service", online=True)] == [
        "service:online"
    ]
    assert [
        item.entity_id for item in store.entities(kind="service", include_stale=False, now=20)
    ] == ["service:offline"]
    with pytest.raises(KeyError, match="unknown graph entity"):
        store.upsert_edge("service:online", "depends_on", "service:missing", source="test")
    with pytest.raises(StateGraphError, match="unknown entity kind"):
        store.upsert_entity("other:x", kind="other", name="X", source="test")


def test_schema_migration_upgrades_a_version_one_database(tmp_path) -> None:
    path = tmp_path / "graph.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE graph_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
            INSERT INTO graph_migrations(version, applied_at) VALUES (1, 1);
            CREATE TABLE graph_entities (
                entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                attributes_json TEXT NOT NULL, source TEXT NOT NULL,
                observed_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE graph_edges (
                edge_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                predicate TEXT NOT NULL,
                object_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
                attributes_json TEXT NOT NULL, source TEXT NOT NULL,
                observed_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE graph_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
                subject_id TEXT, source TEXT NOT NULL, payload_json TEXT NOT NULL,
                occurred_at REAL NOT NULL, created_at REAL NOT NULL
            );
            """
        )

    store = StateGraphStore(path)
    assert store.schema_version() == 2
    with sqlite3.connect(path) as connection:
        entity_columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_entities)")}
        edge_columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_edges)")}
    assert "expires_at" in entity_columns
    assert "expires_at" in edge_columns


def test_current_system_registration_discovers_browser_and_displays(tmp_path) -> None:
    store = StateGraphStore(tmp_path / "graph.sqlite3")

    def runner(command, **_kwargs):
        if command[0] == "xdg-settings":
            return SimpleNamespace(returncode=0, stdout="microsoft-edge.desktop\n")
        if command[0] == "xrandr":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "eDP-1 connected primary 1920x1080+0+0\n"
                    "HDMI-1 connected 2560x1440+1920+0\n"
                    "DP-1 disconnected\n"
                ),
            )
        raise AssertionError(command)

    result = register_current_system(
        store,
        runner=runner,
        hostname="raghavs-laptop",
        observed_at=100.0,
    )

    assert result["laptop_id"] == "device:raghavs-laptop"
    assert result["browser_id"] == "app:browser:microsoft-edge"
    assert result["permission_id"] == "permission:local-device-control"
    assert result["display_ids"] == [
        "display:raghavs-laptop:edp-1",
        "display:raghavs-laptop:hdmi-1",
    ]
    assert result["event_sequence"] == store.latest_sequence()
    assert store.entity(result["laptop_id"]).attributes["online"] is True  # type: ignore[union-attr]
    assert store.entity(result["browser_id"]).attributes["app_type"] == "browser"  # type: ignore[union-attr]
    assert len(store.edges(subject_id=result["laptop_id"], predicate="connected_to")) == 2
    assert (
        store.edges(subject_id="person:raghav", predicate="holds")[0].object_id
        == result["permission_id"]
    )


def test_missing_desktop_probes_are_explicitly_unavailable(tmp_path) -> None:
    store = StateGraphStore(tmp_path / "graph.sqlite3")

    def runner(_command, **_kwargs):
        raise OSError("probe unavailable")

    result = register_current_system(store, runner=runner, hostname="future-host", observed_at=1)

    browser = store.entity(result["browser_id"])
    display = store.entity(result["display_ids"][0])
    assert browser is not None and browser.attributes["discovery_state"] == "unavailable"
    assert display is not None and display.attributes["discovery_state"] == "unavailable"


def test_system_discovery_relationships_expire_when_hardware_changes(tmp_path) -> None:
    store = StateGraphStore(tmp_path / "graph.sqlite3")

    def first_runner(command, **_kwargs):
        if command[0] == "xdg-settings":
            return SimpleNamespace(returncode=0, stdout="firefox.desktop\n")
        return SimpleNamespace(returncode=0, stdout="HDMI-1 connected 1920x1080+0+0\n")

    def second_runner(command, **_kwargs):
        if command[0] == "xdg-settings":
            return SimpleNamespace(returncode=0, stdout="chromium.desktop\n")
        return SimpleNamespace(returncode=0, stdout="DP-1 connected 2560x1440+0+0\n")

    register_current_system(
        store,
        runner=first_runner,
        hostname="changing-host",
        observed_at=100,
    )
    register_current_system(
        store,
        runner=second_runner,
        hostname="changing-host",
        observed_at=4_000,
    )

    laptop_id = "device:changing-host"
    assert [
        item.entity_id
        for item in store.neighbors(
            laptop_id,
            predicate="runs",
            include_stale=False,
            now=4_000,
        )
    ] == ["app:browser:chromium"]
    assert [
        item.entity_id
        for item in store.neighbors(
            laptop_id,
            predicate="connected_to",
            include_stale=False,
            now=4_000,
        )
    ] == ["display:changing-host:dp-1"]
