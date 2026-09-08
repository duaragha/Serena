"""Opt-in pytest plugin: export committed fault-test receipts, never production data."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def fleet_lab_receipts(request, monkeypatch):
    destination = os.environ.get("SERENA_FLEET_LAB_TRACES")
    if not destination:
        yield
        return
    from fleet.store import FleetStore

    original = FleetStore.__init__
    paths = set()

    def remember(self, *args, **kwargs):
        original(self, *args, **kwargs)
        paths.add(self.path)

    monkeypatch.setattr(FleetStore, "__init__", remember)
    yield
    events = []
    for path in paths:
        if not Path(path).exists():
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute(
                "SELECT run_id,event_seq AS seq,type,payload_json,created_at FROM fleet_events ORDER BY event_seq"
            ):
                events.append(
                    {
                        "run_id": row["run_id"],
                        "seq": row["seq"],
                        "type": row["type"],
                        "payload": json.loads(row["payload_json"]),
                        "created_at": row["created_at"],
                    }
                )
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20] + ".json"
    Path(destination, name).write_text(
        json.dumps({"test": request.node.nodeid, "events": events}, indent=2)
    )
