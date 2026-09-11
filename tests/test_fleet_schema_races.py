"""Parallel startup must not race check-then-ALTER schema migrations."""

import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from fleet.collaboration import PeerStore
from fleet.isolation import FleetIsolationStore
from fleet.store import FleetStore
from fleet.supervision import FleetSupervisionStore
from fleet.sqlite_support import enable_wal


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_journal_mode_retries_only_bounded_lock_errors(monkeypatch, code):
    calls, sleeps = [], []
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code

    class Connection:
        def execute(self, statement):
            calls.append(statement)
            if len(calls) <= 3:
                raise error

    monkeypatch.setattr("fleet.sqlite_support.time.sleep", sleeps.append)
    enable_wal(Connection())
    assert calls == ["PRAGMA journal_mode = WAL"] * 4
    assert sleeps == [0.05, 0.1, 0.2]


@pytest.mark.parametrize("code, expected", [(sqlite3.SQLITE_BUSY, 6), (sqlite3.SQLITE_FULL, 1),
                                            (sqlite3.SQLITE_CORRUPT, 1), (None, 1)])
def test_journal_mode_exhaustion_or_nonlock_error_is_not_hidden(monkeypatch, code, expected):
    calls = []
    error = sqlite3.OperationalError("injected startup failure")
    if code is not None:
        error.sqlite_errorcode = code

    class Connection:
        def execute(self, statement):
            calls.append(statement)
            raise error

    monkeypatch.setattr("fleet.sqlite_support.time.sleep", lambda _: None)
    with pytest.raises(sqlite3.OperationalError) as caught:
        enable_wal(Connection())
    assert caught.value is error
    assert len(calls) == expected


@pytest.mark.parametrize("kind", ["supervision", "peers", "isolation", "store"])
@pytest.mark.parametrize("repetition", range(3))
def test_concurrent_schema_initialization(kind, tmp_path, repetition):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    barrier = threading.Barrier(8)

    def initialize(_):
        barrier.wait(timeout=10)
        if kind == "supervision":
            FleetSupervisionStore(store.path)
        elif kind == "peers":
            PeerStore(store)
        elif kind == "isolation":
            FleetIsolationStore(tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "workers")
        else:
            FleetStore(store.path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(initialize, range(8)))
    if kind == "supervision":
        with store._connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(fleet_worker_leases)")}
        assert {"progress_stage", "turn_deadline"} <= columns
