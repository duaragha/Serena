"""Attempt-scoped dependency declarations; no authority to retry arbitrary work."""

import hashlib
import json
import time

from fleet.context import redact_text
from fleet.contracts import _assert_acyclic


def declare_dependency(peer, token: str, unit_id: str, dependency_id: str, reason: str) -> dict:
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError("dependency reason must contain 1–2000 characters")
    store = peer.store
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        identity = peer._identity(db, token)
        if identity.get("help_id"):
            raise PermissionError("helpers cannot change dependency contracts")
        leg = db.execute("SELECT * FROM fleet_legs WHERE leg_id=?", (identity["leg_id"],)).fetchone()
        if leg["access_mode"] == "review":
            raise PermissionError("reviewers cannot change implementation dependencies")
        run_id = identity["run_id"]
        run = db.execute("SELECT policy_json FROM fleet_runs WHERE run_id=?", (run_id,)).fetchone()
        policy = json.loads(run[0])
        units = {u["id"]: u for u in policy["work_units"]}
        if unit_id not in units or dependency_id not in units or unit_id == dependency_id:
            raise ValueError("dependency must name two distinct units in this run")
        unit = units[unit_id]
        if unit["owner_worker_key"] != identity["worker_key"]:
            raise PermissionError("only the unit owner can declare its dependencies")
        if units[dependency_id]["owner_worker_key"] == identity["worker_key"]:
            raise ValueError("co-owned work must be ordered within its worker, not a self-blocking phase edge")
        dependencies = list(dict.fromkeys([*unit["dependency_ids"], dependency_id]))
        unit["dependency_ids"] = dependencies
        unit["dependency_mode"] = "phase_barrier"
        _assert_acyclic(units)
        at = time.time()
        db.execute("UPDATE fleet_runs SET policy_json=?,updated_at=? WHERE run_id=?",
                   (json.dumps(policy), at, run_id))
        db.execute("UPDATE fleet_work_units SET contract_json=?,updated_at=? WHERE run_id=? AND unit_id=?",
                   (json.dumps(unit), at, run_id, unit_id))
        db.execute("INSERT OR IGNORE INTO fleet_work_unit_dependencies VALUES (?,?,?,?)",
                   (run_id, unit_id, dependency_id, at))
        marker = f"dependency:{dependency_id}"
        recorded = any(
            (value := json.loads(row[0])).get("unit_id") == unit_id and value.get("dependency_id") == dependency_id
            for row in db.execute("SELECT payload_json FROM fleet_events WHERE attempt_id=? "
                                  "AND type='worker.dependency_declared'", (identity["attempt_id"],))
        )
        if not recorded:
            store._insert_event(db, run_id=run_id, leg_id=identity["leg_id"],
                                attempt_id=identity["attempt_id"], event_type="worker.dependency_declared",
                                payload={"unit_id": unit_id, "dependency_id": dependency_id,
                                         "reason": redact_text(reason.strip())[0], "stop_condition": marker})
        return {"unit_id": unit_id, "dependency_id": dependency_id,
                "stop_condition": marker,
                "instruction": "If this is your only blocker, report blocked with this exact stop_condition. Fleet owns waiting, refreshing and resuming; do not edit the peer's files."}


def declared_wait(db, attempt_id: str, verdict) -> bool:
    """Only exact, capability-backed dependency stops qualify for automatic wakeup."""
    if verdict is None or not verdict.terminal_stop:
        return False
    declarations = {
        (p["unit_id"], p["stop_condition"])
        for row in db.execute("SELECT payload_json FROM fleet_events WHERE attempt_id=? "
                              "AND type='worker.dependency_declared'", (attempt_id,))
        if isinstance(p := json.loads(row[0]), dict)
    }
    blocked = [u for u in verdict.units if u.claimed_status in {"blocked", "stopped"}]
    valid = bool(blocked) and all(
        u.claimed_status == "blocked" and (u.unit_id, u.stop_condition) in declarations
        for u in blocked
    )
    if not valid:
        return False
    leg_id = db.execute("SELECT leg_id FROM fleet_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
    fingerprint = ready_fingerprint(db, leg_id)
    if fingerprint:
        for row in db.execute("SELECT payload_json FROM fleet_events WHERE leg_id=? "
                              "AND type='leg.dependencies_resumed'", (leg_id,)):
            if json.loads(row[0]).get("fingerprint") == fingerprint:
                return False  # already refreshed against these exact peer outputs
    return True


def ready_fingerprint(db, leg_id: str) -> str | None:
    rows = db.execute(
        "SELECT d.dependency_id,p.attempt_id,p.state FROM fleet_work_unit_phases own "
        "JOIN fleet_work_unit_dependencies d ON d.run_id=own.run_id AND d.unit_id=own.unit_id "
        "LEFT JOIN fleet_work_unit_phases p ON p.run_id=d.run_id AND p.unit_id=d.dependency_id "
        "AND p.phase_index=own.phase_index WHERE own.leg_id=? ORDER BY d.dependency_id",
        (leg_id,),
    ).fetchall()
    if not rows or any(row["state"] != "completed" or not row["attempt_id"] for row in rows):
        return None
    return hashlib.sha256(json.dumps([(r["dependency_id"], r["attempt_id"]) for r in rows]).encode()).hexdigest()


def pending_wait(db, leg_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM fleet_events e JOIN fleet_attempts a ON a.attempt_id=e.attempt_id "
        "JOIN fleet_legs l ON l.leg_id=a.leg_id AND l.current_attempt=a.attempt_number "
        "WHERE l.leg_id=? AND e.type='leg.waiting_for_dependencies' LIMIT 1", (leg_id,),
    ).fetchone() is not None
