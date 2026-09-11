"""Autonomous readiness probes preserve blockers, cancellation and work receipts."""

import pytest
import threading

from fleet.ready_resume import resume_ready_input_runs
from fleet.store import FleetStore
from test_fleet_policy_store import _create


def _park(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, worker_count=2)
    rid = run["run_id"]
    store.claim_run(rid)
    for leg in run["phases"][0]["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error="missing authorized evidence",
                             input_blocker_reason="missing authorized evidence")
    store.resolve_phase_failure(rid, "discover", "missing authorized evidence")
    return store, rid


def test_unchanged_blockers_are_not_retried_or_rewritten(tmp_path):
    store, rid = _park(tmp_path)
    with store._connect() as db:
        before = list(db.iterdump())
    assert resume_ready_input_runs(store) == []
    with store._connect() as db:
        assert list(db.iterdump()) == before
    assert store.get_run(rid)["state"] == "waiting_for_input"


def test_cancelled_run_cannot_be_woken_even_with_selector(tmp_path):
    store, rid = _park(tmp_path)
    store.request_cancel(rid)
    def should_not_select(_):
        pytest.fail("cancelled work reached readiness selection")
    assert not store.resume_ready_input_work(rid, should_not_select)
    assert resume_ready_input_runs(store) == []
    assert store.get_run(rid)["state"] == "cancelled"


def test_probe_failure_does_not_starve_another_run(monkeypatch):
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, _): return [("bad",), ("ready",)]
    events = []
    class Store:
        def _connect(self): return Connection()
        def resume_ready_input_work(self, rid, selector):
            if rid == "bad": raise ValueError("malformed legacy record")
            return True
        def append_event(self, *args): events.append(args)
    assert resume_ready_input_runs(Store()) == ["ready"]
    assert events == [("bad", "run.ready_work_probe_failed", {"error_type": "ValueError"})]


def test_resident_service_probes_parked_work_on_boot(monkeypatch):
    from fleet import supervisor
    stop = threading.Event()
    calls = []
    class Store:
        def recover_stale_runs(self, **kwargs): return []
        def flush_control_outbox(self): return 0
    store = Store()
    monkeypatch.setattr(supervisor, "_store", lambda: store)
    monkeypatch.setattr(supervisor, "recover_stale_runs", lambda: [])
    for name in ("_reconcile_recent_runs", "_retry_recent_terminal_notices", "_recover_outstanding_obligations", "resume_ready_capacity_waits"):
        monkeypatch.setattr(supervisor, name, lambda *_: None)
    monkeypatch.setattr("fleet.resources.resume_ready_resource_waits", lambda *_: [])
    def probe(actual_store):
        calls.append(actual_store)
        stop.set()
        return []
    monkeypatch.setattr("fleet.ready_resume.resume_ready_input_runs", probe)
    supervisor.serve_forever(poll_interval=0.01, stop_event=stop)
    assert calls == [store]
