"""Durable, policy-gated notices for work that cannot recover without input."""

import hashlib
import json
import time
from dataclasses import replace

from core.notification_authority import NotificationRequest


def notify_blocked_run(store, run, authority_factory):
    if run.get("dry_run") or run.get("cancel_requested") or run.get("state") in {
        "completed", "failed", "cancelled", "stopping",
    }:
        return
    blocked = [leg for phase in run.get("phases", []) for leg in phase.get("legs", [])
               if leg.get("state") == "waiting_for_input"]
    if not blocked:
        return
    identities = sorted((str(leg["leg_id"]), str((leg.get("current_attempt") or {}).get("attempt_id")
                        or leg.get("updated_at") or "")) for leg in blocked)
    notice_id = "attention:" + hashlib.sha256(json.dumps(identities).encode()).hexdigest()
    run_id = run["run_id"]
    # Query by identity, not a bounded event window: busy siblings must not
    # push an earlier successful delivery out of deduplication history.
    with store._connect() as db:
        rows = db.execute("SELECT payload_json FROM fleet_events WHERE run_id=? "
                          "AND type='run.attention.result' ORDER BY event_seq DESC", (run_id,)).fetchall()
    prior = next((value for row in rows if (value := json.loads(row[0])).get("notice_id") == notice_id), None)
    if prior and prior["decision"] in {"sent", "pending_approval"}:
        return
    if prior and prior["decision"] == "suppressed":
        if time.time() - prior.get("recorded_at", 0) < 3600:
            return
        prior = None
    labels = ", ".join(str(leg.get("worker_label") or leg.get("worker_key") or leg["leg_id"])
                       for leg in blocked)
    snapshot = dict(run, state="waiting_for_input", error=f"{labels} reported blocked work. "
                    "Open the Fleet tab for the recorded blocker and current recovery state.")
    authority = authority_factory(snapshot, notice_id)
    if prior:
        result = authority.redeliver(prior["notification_id"])
        if result is None:
            return  # not due, held, already delivered, or retry budget exhausted
    else:
        request = NotificationRequest(
            kind="fleet.run.waiting_for_input", summary=f"fleet {run_id[:8]}: {snapshot['error']}",
            channel="voice", urgency="normal", dedupe_key=f"fleet:{run_id}:{notice_id}",
            source_surface="fleet", job_id=run_id,
            metadata={"fleet_notice_id": notice_id, "fleet_state": "waiting_for_input"},
        )
        result = authority.request(request)
        if result.decision == "failed":
            result = authority.request(replace(request, channel="telegram",
                                               dedupe_key=request.dedupe_key + ":telegram"))
    store.append_event(run_id, "run.attention.result", {
        "notice_id": notice_id, "notification_id": result.notification_id,
        "decision": result.decision, "channel": result.channel,
        "reason": result.reason, "attempts": result.attempts, "recorded_at": time.time(),
    })


def notify_blocked_runs(store, authority_factory):
    with store._connect() as db:
        ids = [row[0] for row in db.execute(
            "SELECT DISTINCT run_id FROM fleet_legs WHERE state='waiting_for_input'")]
    for run_id in ids:
        try:
            run = store.get_run(run_id)
            if run:
                notify_blocked_run(store, run, authority_factory)
        except Exception as exc:
            store.append_event(run_id, "run.attention.error", {"error": str(exc)[:500]})
