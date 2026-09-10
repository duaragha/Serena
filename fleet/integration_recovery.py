"""Bounded, durable replay of a saved writer result after codegen gate failure."""

from contextlib import suppress
import hashlib
import json
from pathlib import Path
import time

from fleet.isolation import _generated_types_preparation


def resume_saved_integrations(store):
    """Queue at most one replay per leg; never wake cancelled or live runs."""
    with store._connect() as db:
        candidates = db.execute(
            "SELECT l.run_id,l.leg_id,a.attempt_id FROM fleet_legs l "
            "JOIN fleet_runs r ON r.run_id=l.run_id "
            "JOIN fleet_attempts a ON a.leg_id=l.leg_id AND a.attempt_number=l.current_attempt "
            "WHERE r.state IN ('failed','waiting_for_input','waiting_for_resources','waiting_for_capacity') "
            "AND r.cancel_requested=0 AND r.dry_run=0 AND r.activity='coding' "
            "AND l.state IN ('failed','waiting_for_input') AND l.access_mode='write' AND a.state='failed' AND a.exit_code=0"
        ).fetchall()
    resumed = []
    for row in candidates:
        try:
            if _queue(store, *row):
                resumed.append(str(row[0]))
        except Exception as error:
            with suppress(Exception):
                store.append_event(str(row[0]), "leg.integration_replay_probe_failed",
                                   {"error_type": type(error).__name__}, leg_id=str(row[1]))
    return resumed


def _queue(store, run_id, leg_id, attempt_id):
    from fleet.store import reset_work_unit_leg_for_retry

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        run = store._require_run(db, run_id)
        if run["cancel_requested"] or run["dry_run"] or run["activity"] != "coding" or run["state"] not in {
            "failed", "waiting_for_input", "waiting_for_resources", "waiting_for_capacity"
        }:
            return False
        if db.execute("SELECT 1 FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id=a.leg_id "
                      "WHERE l.run_id=? AND a.state='running'", (run_id,)).fetchone():
            return False
        attempt = db.execute(
            "SELECT a.* FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id=a.leg_id "
            "WHERE a.attempt_id=? AND l.run_id=? AND l.leg_id=? AND l.state IN ('failed','waiting_for_input') "
            "AND l.access_mode='write' AND a.attempt_number=l.current_attempt "
            "AND a.state='failed' AND a.exit_code=0", (attempt_id, run_id, leg_id),
        ).fetchone()
        if not attempt or db.execute("SELECT 1 FROM fleet_events WHERE leg_id=? AND "
                                     "type='leg.integration_replay_queued'", (leg_id,)).fetchone():
            return False
        accepted = db.execute("SELECT payload_json FROM fleet_events WHERE attempt_id=? AND "
                              "type='leg.completion_evidence_accepted' ORDER BY event_seq DESC LIMIT 1",
                              (attempt_id,)).fetchone()
        rejected = db.execute("SELECT payload_json FROM fleet_events WHERE attempt_id=? AND "
                              "type='worker.integration.rejected' ORDER BY event_seq DESC LIMIT 1",
                              (attempt_id,)).fetchone()
        if not accepted or not rejected:
            return False
        verdict, integration = json.loads(accepted[0]), json.loads(rejected[0])
        if verdict.get("completion_allowed") is not True or integration.get("delivery_mode") != "local_patch":
            return False
        gate = integration.get("test_gate") or {}
        checkout = db.execute("SELECT path,state FROM fleet_run_checkouts WHERE run_id=?", (run_id,)).fetchone()
        if checkout and checkout["state"] != "ready":
            return False
        cwd = checkout["path"] if checkout else run["cwd"]
        if not _generated_types_preparation(cwd, gate.get("command") or [], gate):
            return False
        patch = Path(integration.get("patch_path") or "")
        if not patch.is_file():
            return False
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        payload = {"source_attempt_id": attempt_id, "patch_sha256": digest,
                   "reason": "saved integration can rebuild missing generated types",
                   "native_turn": False}
        now = time.time()
        db.execute("UPDATE fleet_legs SET state='queued',updated_at=? WHERE leg_id=?", (now, leg_id))
        reset_work_unit_leg_for_retry(db, leg_id=leg_id, now=now)
        db.execute("UPDATE fleet_runs SET state='queued',owner_pid=NULL,owner_token=NULL,error=NULL,"
                   "result_text=NULL,completed_at=NULL,updated_at=? WHERE run_id=?", (now, run_id))
        store._insert_event(db, run_id=run_id, leg_id=leg_id, attempt_id=attempt_id,
                            event_type="leg.integration_replay_queued", payload=payload)
        return True


def execute_saved_integration(store, run_id, leg):
    """Use normal leases/claims/gates but spend no provider turn for a replay."""
    with store._connect() as db:
        event = db.execute("SELECT payload_json FROM fleet_events WHERE leg_id=? AND "
                           "type='leg.integration_replay_queued' ORDER BY event_seq DESC LIMIT 1",
                           (leg["leg_id"],)).fetchone()
        if not event:
            return None
        receipt = json.loads(event[0])
        source = db.execute("SELECT a.* FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id=a.leg_id "
                            "WHERE a.attempt_id=? AND a.attempt_number=l.current_attempt AND l.state='queued' "
                            "AND a.state='failed' AND l.leg_id=? AND l.run_id=?",
                            (receipt["source_attempt_id"], leg["leg_id"], run_id)).fetchone()
        if source is None:
            return None
        source = dict(source)

    from fleet import supervisor
    from fleet.isolation import FleetIsolationStore, integrate_workspace
    from fleet.supervision import FleetSupervisionStore, WorkerLeaseMonitor
    from fleet.workers import WorkerResult

    supervision = supervisor._retry_sqlite_busy(lambda: FleetSupervisionStore(store.path))
    attempt = supervisor._retry_sqlite_busy(lambda: store.begin_attempt(leg["leg_id"]))
    lease = None
    monitor = None
    error = ""
    state = "failed"
    output = source["output_text"] or ""
    try:
        lease = supervisor._retry_sqlite_busy(lambda: supervision.acquire(attempt["attempt_id"]))
        monitor = WorkerLeaseMonitor(supervision, lease)
        monitor.start()
        store.append_event(run_id, "worker.integration_replay_started", receipt,
                           leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
        snapshot = store.get_run(run_id)
        isolation = FleetIsolationStore()
        claims = isolation.claim_paths(run_id=run_id, worker_key=supervisor._worker_key(leg),
                                       paths=supervisor._effective_write_paths(snapshot, leg))
        if not claims.ok:
            raise RuntimeError("saved integration replay is waiting for its write claim")
        if store.run_cancel_requested(run_id) or monitor.should_cancel():
            raise RuntimeError("saved integration replay was cancelled or fenced")
        verdict = supervisor._completion_verdict(store, snapshot, leg, attempt, output,
                                                event_log_path=source.get("event_log_path"))
        if verdict is None or not verdict.completion_allowed:
            detail = verdict.summary() if verdict is not None else "completion verdict unavailable"
            raise RuntimeError("saved integration completion evidence no longer passes: " + detail)
        integration = integrate_workspace(
            isolation, run_id=run_id, worker_key=supervisor._worker_key(leg), cwd=snapshot["cwd"],
            test_gate=supervisor._integration_test_gate(),
            declared_tests=supervisor._declared_integration_tests(output, snapshot["cwd"]),
            declared_paths=sorted({path for unit in verdict.units for path in unit.changed_paths}),
            expected_patch_sha256=receipt["patch_sha256"],
        )
        store.append_event(run_id, "worker.integration.accepted" if integration.ok else "worker.integration.rejected",
                           integration.to_dict(), leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
        if not integration.ok:
            raise RuntimeError(integration.reason)
        if not supervision.owns(attempt["attempt_id"], lease.lease_token):
            raise RuntimeError("saved integration replay lease was fenced")
        state = "completed"
    except Exception as exc:
        error = str(exc)
    finally:
        if monitor:
            monitor.stop()
        if store.run_cancel_requested(run_id):
            state = "cancelled"
        try:
            store.finish_attempt(attempt["attempt_id"], state=state, output_text=output,
                                 error=error or None, exit_code=0 if state == "completed" else -1,
                                 input_blocker_reason=error if state == "failed" else None)
        finally:
            supervisor._release_write_claims(store, run_id, leg, attempt_id=attempt["attempt_id"])
            if lease:
                supervision.release(attempt["attempt_id"], lease.lease_token, state=state, reason=error)
    return WorkerResult(state == "completed", output, None, None, None,
                        0 if state == "completed" else -1, error or None, state == "cancelled")
