"""Coordinator delivery receipts: evidence changes state, never an empty retry."""

import hashlib
import json

PREFIX = "Fleet delivery evidence: "
INTEGRATION = "the change is integrated into the run's base checkout"


def accept_operator_evidence(store, run_id, message, *, allow_running=False):
    """Only the operator steering entrypoint may attest external delivery.

    Peer capabilities do not expose this operation. Evidence must name a frozen
    unit and its exact requirement; it never changes contracts or completes legs.
    """
    if not message.startswith(PREFIX):
        return False
    if len(message) > 16000:
        raise ValueError("delivery evidence exceeds 16000 characters")
    entries = json.loads(message[len(PREFIX):])
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise ValueError("delivery evidence requires 1–12 receipts")
    run = store.get_run(run_id)
    states = {"failed", "waiting_for_input"} | ({"running"} if allow_running else set())
    if not run or run["cancel_requested"] or run["state"] not in states:
        raise ValueError("delivery evidence requires an uncancelled parked run")
    receipt_key = hashlib.sha256(message.encode()).hexdigest()
    with store._connect() as db:
        recorded = db.execute("SELECT payload_json FROM fleet_events WHERE run_id=? AND type='run.delivery_verified'", (run_id,))
        if any(json.loads(row[0]).get("receipt_key") == receipt_key for row in recorded):
            return True
    contracts = {unit["id"]: unit["completion_contract"] for unit in run["policy"]["work_units"]}
    receipts = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid delivery receipt")
        unit_id = entry.get("unit_id")
        requirement = entry.get("requirement")
        evidence = entry.get("evidence")
        if unit_id not in contracts or requirement not in contracts[unit_id].get("delivery_requirements", []):
            raise ValueError("receipt must name an exact frozen delivery requirement")
        if not isinstance(evidence, str) or not 40 <= len(evidence.strip()) <= 4000:
            raise ValueError("receipt requires 40–4000 characters of observed evidence")
        receipts.append({"unit_id": unit_id, "requirement": requirement, "evidence": evidence.strip()})
    store.append_event(run_id, "run.delivery_verified", {
        "authority": "operator", "receipts": receipts, "receipt_key": receipt_key})
    return True


def consume_operator_steering(store, run_id):
    """Rolling-upgrade compatibility for MCP connections predating receipts.

    Only operator steering is considered. A receipt from before a new model
    attempt is stale; repeating finalization never reapplies an old proof.
    """
    with store._connect() as db:
        messages = db.execute("SELECT message,created_at FROM fleet_steering WHERE run_id=? ORDER BY steering_seq", (run_id,)).fetchall()
        latest = db.execute("SELECT MAX(a.started_at) FROM fleet_attempts a JOIN fleet_legs l ON l.leg_id=a.leg_id WHERE l.run_id=?", (run_id,)).fetchone()[0] or 0
    for message, created in messages:
        if message.startswith(PREFIX) and created >= latest:
            accept_operator_evidence(store, run_id, message, allow_running=True)


def reconcile_event(event, owed, attempts):
    """Apply trusted coordinator facts in event order, scoped to their unit."""
    payload = event.get("payload") or {}
    kind = event.get("type")
    if kind == "run.delivery_verified" and payload.get("authority") == "operator":
        for receipt in payload.get("receipts", []):
            owed.pop((receipt["unit_id"], receipt["requirement"].casefold()), None)
    if kind == "leg.completion_evidence_accepted" and event.get("attempt_id"):
        attempts[event["attempt_id"]] = [u["unit_id"] for u in payload.get("units", []) if u.get("accepted")]
    if kind == "worker.integration.accepted" and payload.get("ok") is True and payload.get("applied") is True:
        for unit_id in attempts.get(event.get("attempt_id"), []):
            for key in list(owed):
                if key[0] == unit_id and key[1].startswith(INTEGRATION):
                    owed.pop(key)
