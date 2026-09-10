"""Private service-loop fault injection; no model/account calls or live Fleet."""

import os
from pathlib import Path
import shlex
import sys
import threading
import time

import psutil

from core.work_jobs import process_start_token
from fleet import supervisor, workers, peer_runtime, lesson_review
from test_fleet_integration_recovery import _failed


def test_resident_timer_recovers_killed_helper_without_operator_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "none")
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setenv("SERENA_NOTIFICATION_DB_PATH", str(tmp_path / "notices.sqlite3"))
    store, rid, leg, *_ = _failed(tmp_path, monkeypatch, phase_index=1)
    # Stop the disposable run only AFTER Code recovers: its newly runnable
    # independent Review is outside this helper-only probe's scope.
    before = store.get_run(rid)
    root = Path(before["cwd"])
    marker = tmp_path / "gate-entered"
    gate = (
        "import os,time; from pathlib import Path; "
        f"p=Path({str(marker)!r}); already=p.exists(); pending=p.with_suffix('.tmp'); "
        "pending.write_text(str(os.getpid())) if not already else None; "
        "pending.replace(p) if not already else None; "
        "time.sleep(20) if not already else None"
    )
    monkeypatch.setenv("SERENA_FLEET_INTEGRATION_TEST_COMMAND", shlex.join([sys.executable, "-c", gate]))
    monkeypatch.setattr(supervisor, "_store", lambda: store)
    # Disable unrelated account catalog, session projection and notification
    # delivery, not scheduling, recovery, isolation or completion validation.
    for name in ("_reconcile_recent_runs", "_retry_recent_terminal_notices",
                 "_recover_outstanding_obligations", "_reconcile_run_sessions",
                 "_refresh_read_mcp_catalog"):
        monkeypatch.setattr(supervisor, name, lambda *a, **kw: None)
    monkeypatch.setattr(supervisor, "_terminal_outcome", lambda store, run: run)
    capacity = lambda: {"codex": {"usable": True}, "claude": {"usable": True}}
    monkeypatch.setattr(supervisor, "_read_start_capacity", capacity)
    monkeypatch.setattr(supervisor, "read_fleet_capacity", capacity)
    unexpected = []
    def no_model(*args, **kwargs):
        unexpected.append("native model dispatch")
        raise RuntimeError("native model dispatch forbidden in private timer test")
    for module in (supervisor, workers, peer_runtime, lesson_review):
        monkeypatch.setattr(module, "run_worker", no_model)
    stopper = threading.Event()
    service = threading.Thread(target=supervisor.serve_forever,
                               kwargs={"stop_event": stopper}, daemon=True)
    helper = gate_process = None
    started = time.monotonic()
    service.start()
    try:
        deadline = started + 30
        while not marker.exists() and time.monotonic() < deadline:
            assert service.is_alive()
            time.sleep(.05)
        assert marker.exists(), store.get_run(rid)
        current = store.get_run(rid)["phases"][1]["legs"][0]["current_attempt"]
        with store._connect() as db:
            lease = db.execute("SELECT owner_pid,owner_token FROM fleet_worker_leases "
                               "WHERE attempt_id=? AND state='active'", (current["attempt_id"],)).fetchone()
        assert lease
        helper = psutil.Process(lease["owner_pid"])
        launched = psutil.Process(current["pid"])
        assert helper == launched or helper in launched.children(recursive=True)
        assert helper.pid not in {os.getpid(), os.getppid()}
        assert process_start_token(helper.pid) == lease["owner_token"]
        gate_process = psutil.Process(int(marker.read_text()))
        assert gate_process in helper.children(recursive=True)
        assert (root / "core/alpha.py").read_bytes() == b"alpha = 2\n"
        helper.kill()
        # Observe only: no retry/resume calls, altered clock or shortened poll.
        not_before = None
        final = None
        deadline = started + 110
        while time.monotonic() < deadline:
            final = store.get_run(rid)
            with store._connect() as db:
                wait = db.execute("SELECT not_before FROM fleet_resource_waits WHERE leg_id=?",
                                  (leg["leg_id"],)).fetchone()
            if wait:
                not_before = float(wait["not_before"])
            if final["phases"][1]["legs"][0]["state"] == "completed":
                stopper.set()
                store.request_cancel(rid)
                break
            assert not unexpected
            time.sleep(.1)
        assert final["phases"][1]["legs"][0]["state"] == "completed", final
        assert not_before is not None and time.time() >= not_before
        assert final["phases"][1]["legs"][0]["attempt_count"] == 3
        assert final["phases"][1]["legs"][0]["current_attempt"]["actual_model"] is None
        for phase, index in ((0, 0), (0, 1), (1, 1)):
            assert final["phases"][phase]["legs"][index]["current_attempt"]["attempt_id"] == before["phases"][phase]["legs"][index]["current_attempt"]["attempt_id"]
        assert (root / "core/alpha.py").read_bytes() == b"alpha = 2\n"
        events = store.events(rid)
        retries = [e for e in events if e["type"] == "leg.process_retry_started"]
        assert len(retries) == 1
        assert retries[0]["created_at"] >= not_before
        accepted = [e for e in events if e["type"] == "worker.integration.accepted"]
        assert accepted[-1]["payload"]["test_gate"]["integration_journal"]["recovered_postimage"]
        assert not unexpected
        assert not gate_process.is_running() or gate_process.status() == psutil.STATUS_ZOMBIE
        print(f"resident-only helper recovery: {time.monotonic() - started:.2f}s")
    finally:
        stopper.set()
        if (store.get_run(rid) or {}).get("state") not in {"waiting_for_input", "completed", "cancelled"}:
            store.request_cancel(rid)
        for process in (gate_process, helper):
            if process is not None:
                try:
                    if process.is_running():
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
        service.join(timeout=10)
        deadline = time.monotonic() + 15
        while any(t.name == f"fleet-{rid[:8]}" and t.is_alive() for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not service.is_alive()
        assert not any(t.name == f"fleet-{rid[:8]}" and t.is_alive() for t in threading.enumerate())
        assert not unexpected
