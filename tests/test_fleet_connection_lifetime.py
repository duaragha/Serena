"""Database operations must release handles without waiting for cyclic GC."""

import sqlite3
import gc
import os

import pytest

from core.control_plane import ControlPlaneStore, SurfaceOutbox
from fleet.isolation import FleetIsolationStore
from fleet.store import FleetStore
from fleet.supervision import FleetSupervisionStore
from core.sqlite_connection import connect_database


@pytest.mark.parametrize("kind", ["fleet", "isolation", "supervision", "control", "outbox"])
def test_store_transaction_scope_closes_connection(tmp_path, kind):
    path = tmp_path / "state.sqlite3"
    if kind == "outbox":
        store = SurfaceOutbox("fleet", path)
    elif kind == "supervision":
        FleetStore(path)
        store = FleetSupervisionStore(path)
    else:
        store = {"fleet": FleetStore, "isolation": FleetIsolationStore, "control": ControlPlaneStore}[kind](path)
    with store._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("outcome", ["commit", "body_error", "commit_error"])
def test_scope_preserves_commit_and_rollback_and_closes_on_error(tmp_path, outcome):
    path = tmp_path / "transaction.sqlite3"
    with connect_database(path, foreign_keys=True) as connection:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    connection = connect_database(path, foreign_keys=True)
    try:
        with connection:
            connection.execute("INSERT INTO child VALUES(1)")
            if outcome == "body_error":
                raise RuntimeError("abort body")
            if outcome == "commit":
                connection.execute("INSERT INTO parent VALUES(1)")
    except RuntimeError:
        assert outcome == "body_error"
    except sqlite3.IntegrityError:
        assert outcome == "commit_error"
    else:
        assert outcome == "commit"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with connect_database(path) as check:
        assert check.execute("SELECT COUNT(*) FROM child").fetchone()[0] == (1 if outcome == "commit" else 0)


def test_explicit_connection_owner_can_commit_and_reuse_until_close(tmp_path):
    connection = connect_database(tmp_path / "raw.sqlite3")
    try:
        connection.execute("CREATE TABLE item(id INTEGER)")
        connection.execute("INSERT INTO item VALUES(1)")
        connection.commit()
        assert connection.execute("SELECT id FROM item").fetchone()[0] == 1
        connection.execute("INSERT INTO item VALUES(2)")
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux descriptor accounting")
def test_finished_reads_release_descriptors_even_without_garbage_collection(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    gc.collect()
    was_enabled = gc.isenabled()
    baseline = len(os.listdir("/proc/self/fd"))
    connections = []
    gc.disable()
    try:
        for _ in range(20):
            with store._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            connections.append(connection)  # deliberately retain every object
        assert len(os.listdir("/proc/self/fd")) <= baseline
    finally:
        for connection in connections:
            connection.close()
        if was_enabled:
            gc.enable()


def test_configuration_failure_closes_new_connection(tmp_path, monkeypatch):
    original = sqlite3.connect
    opened = []

    class BrokenPragma(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.startswith("PRAGMA"):
                raise sqlite3.OperationalError("injected configuration failure")
            return super().execute(statement, *args, **kwargs)

    def broken_connect(*args, **kwargs):
        kwargs["factory"] = BrokenPragma
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError, match="configuration"):
        connect_database(tmp_path / "failure.sqlite3")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
