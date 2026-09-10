"""Parallel startup must not race check-then-ALTER schema migrations."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fleet.collaboration import PeerStore
from fleet.isolation import FleetIsolationStore
from fleet.store import FleetStore
from fleet.supervision import FleetSupervisionStore


@pytest.mark.parametrize("kind", ["supervision", "peers", "isolation", "store"])
def test_concurrent_schema_initialization(kind, tmp_path):
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
