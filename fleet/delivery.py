"""Coordinator delivery receipts: evidence changes state, never an empty retry."""

import hashlib
import json
from typing import Any

PREFIX = "Fleet delivery evidence: "
INTEGRATION = "the change is integrated into the run's base checkout"

_WORKER_DELIVERY_TYPES = (
    "leg.completion_evidence_accepted",
    "leg.completion_evidence_stopped",
)


class DeliveryLedgerUnavailable(RuntimeError):
    """The delivery debts could not be read, so nothing may be concluded."""


def evidence_sha256(evidence):
    """Content key for one attested observation."""
    return hashlib.sha256(evidence.encode()).hexdigest()


HANDOFF_SOURCE = "fleet/delivery"
HANDOFF_MARKER = "completed_with_handoff"
HANDOFF_EVENT = "run.delivery.handed_off"

# Mirrors fleet.completion._ROOT_OWNERS: the gate canonicalises these to "root"
# before emitting evidence, but the terminal partition must not strand a debt
# that arrived under an alias.
ROOT_OWNER_ALIASES = frozenset({"root", "root coordinator", "coordinator", "parent", "origin"})


def is_root_owed(debt: dict[str, Any]) -> bool:
    """Whether a ledger debt is owed by the coordinator rather than a worker."""

    return str(debt.get("owner") or "").strip().casefold() in ROOT_OWNER_ALIASES


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
        receipts.append({"unit_id": unit_id, "requirement": requirement, "evidence": evidence.strip(),
                         "evidence_sha256": evidence_sha256(evidence.strip())})
    store.append_event(run_id, "run.delivery_verified", {
        "authority": "operator", "receipts": receipts, "receipt_key": receipt_key})
    return True


def consume_operator_steering(store, run_id):
    """Replay steering-carried operator receipts that are still live.

    Rolling-upgrade compatibility for MCP connections predating receipts: only
    operator steering is considered. A receipt is content-keyed by (unit_id,
    requirement, evidence-hash), so it survives later model attempts unchanged.
    It is invalidated only when a newer worker delivery entry for the same
    requirement exists — the worker re-answered, so the old attestation no
    longer describes the debt — or the requirement text no longer matches the
    frozen contract. A bare retry that starts new attempts leaves live receipts
    standing; repeating finalization never reapplies an old proof.
    """
    try:
        with store._connect() as db:
            messages = db.execute("SELECT message,created_at FROM fleet_steering WHERE run_id=? ORDER BY steering_seq", (run_id,)).fetchall()
            worker_rows = db.execute(
                "SELECT payload_json,created_at FROM fleet_events WHERE run_id=? AND type IN (?,?)",
                (run_id, *_WORKER_DELIVERY_TYPES)).fetchall()
    except Exception as exc:
        raise DeliveryLedgerUnavailable(
            f"could not read this run's delivery evidence: {exc}"
        ) from exc
    candidates = [(message, created) for message, created in messages if message.startswith(PREFIX)]
    if not candidates:
        return
    run = store.get_run(run_id)
    if run is None:
        return
    contracts = {unit["id"]: unit["completion_contract"] for unit in run["policy"]["work_units"]}
    superseded_after = _newest_worker_deferrals(worker_rows)
    for message, attested_at in candidates:
        if len(message) > 16000:
            continue
        try:
            entries = json.loads(message[len(PREFIX):])
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
            continue
        live = [entry for entry in entries
                if _receipt_still_live(entry, contracts, superseded_after, attested_at)]
        if live:
            accept_operator_evidence(store, run_id, PREFIX + json.dumps(live), allow_running=True)


def _newest_worker_deferrals(rows):
    """Newest deferral instant per (unit_id, requirement), in ledger terms."""
    newest = {}
    for payload_json, created in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for unit in payload.get("units") or []:
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("unit_id") or "")
            for item in unit.get("deferred_delivery") or []:
                if not isinstance(item, dict):
                    continue
                requirement = str(item.get("requirement") or "").strip()
                if not requirement:
                    continue
                scope = str(item.get("unit_id") or unit_id)
                key = (scope, requirement.casefold())
                if key not in newest or created >= newest[key]:
                    newest[key] = created
    return newest


def _receipt_still_live(entry, contracts, superseded_after, attested_at):
    """A steering receipt stands unless the worker re-answered or the text moved."""
    if not isinstance(entry, dict):
        return False
    unit_id = entry.get("unit_id")
    requirement = entry.get("requirement")
    evidence = entry.get("evidence")
    if unit_id not in contracts:
        return False
    if requirement not in contracts[unit_id].get("delivery_requirements", []):
        return False
    if not isinstance(evidence, str) or not 40 <= len(evidence.strip()) <= 4000:
        return False
    newest = superseded_after.get((unit_id, requirement.casefold()))
    superseded = newest is not None and newest >= attested_at
    return not superseded


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


def outstanding_delivery(store, run_id: str) -> list[dict[str, Any]]:
    """Delivery this run deferred and nobody has since verified.

    Three things this has to get right, each of which was wrong first time:

    Identity is the unit plus the full requirement text. The contract wording is
    generic on purpose, so every unit carries the same three sentences; keying
    on the text alone let one bridge's deployment discharge another bridge's
    debt.

    Order matters. Deferrals and verifications are applied in event order, so a
    verification that happened before a later deferral cannot cancel it. The
    previous set-subtraction cleared debts that were incurred afterwards.

    And it fails closed. An unreadable ledger used to return "nothing owed",
    which is the one answer that must never be a guess: the gate exists to stop
    a run completing undelivered, so losing the evidence blocks completion
    rather than waving it through.
    """

    owed: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[str, list[str]] = {}
    after = 0
    try:
        while True:
            events = store.events(run_id, after=after, limit=2_000)
            if not events:
                break
            for event in events:
                after = max(after, int(event.get("event_seq") or 0))
                reconcile_event(event, owed, attempts)
                if str(event.get("type") or "") not in {
                    "leg.completion_evidence_accepted",
                    "leg.completion_evidence_stopped",
                }:
                    continue
                for unit in (event.get("payload") or {}).get("units") or []:
                    if not isinstance(unit, dict):
                        continue
                    unit_id = str(unit.get("unit_id") or "")
                    for item in unit.get("deferred_delivery") or []:
                        if not isinstance(item, dict):
                            continue
                        requirement = str(item.get("requirement") or "").strip()
                        if not requirement:
                            continue
                        scope = str(item.get("unit_id") or unit_id)
                        owed[(scope, requirement.casefold())] = {
                            **dict(item),
                            "leg_id": event.get("leg_id"),
                            "attempt_id": event.get("attempt_id"),
                        }
                    for requirement in unit.get("verified_delivery") or []:
                        key = str(requirement or "").strip().casefold()
                        if key:
                            owed.pop((unit_id, key), None)
            if len(events) < 2_000:
                break
    except Exception as exc:  # noqa: BLE001 - re-raised as a blocking failure
        raise DeliveryLedgerUnavailable(
            f"could not read this run's delivery evidence: {exc}"
        ) from exc
    return list(owed.values())


def handoff_source_ref(run_id: str, unit_id: str, requirement: str) -> str:
    """Idempotency key for one handed-off debt: one source row, one commitment."""

    digest = hashlib.sha256(str(requirement or "").strip().casefold().encode("utf-8")).hexdigest()
    return f"{run_id}:{unit_id}:{digest}"


def _handoff_title(unit_id: str, requirement: str) -> str:
    short = " ".join(str(requirement or "").split())
    if len(short) > 200:
        short = short[:197].rstrip() + "..."
    return f"[{unit_id}] {short}"


def _handoff_detail(
    run_id: str,
    unit_id: str,
    requirement: str,
    reason: str,
    leg_id: Any,
    attempt_id: Any,
) -> str:
    return (
        f"Fleet run {run_id} completed with this delivery requirement handed off "
        f"to the operator ({HANDOFF_MARKER}).\n"
        f"unit: {unit_id}\n"
        f"requirement: {' '.join(str(requirement or '').split())}\n"
        f"deferred by: leg {leg_id or 'unknown'} attempt {attempt_id or 'unknown'}\n"
        f"deferral reason: \"{' '.join(str(reason or '').split())[:400]}\"\n"
        f"inspect: chats fleet inspect {run_id} --focus {unit_id}"
    )


def file_delivery_handoffs(
    store,
    run_id: str,
    debts: list[dict[str, Any]],
    *,
    commitments=None,
) -> list[dict[str, Any]]:
    """File one commitment per still-owed debt; refiles return the same rows.

    Owed is re-checked at file time, so operator evidence that landed after
    terminal evaluation started simply narrows what gets filed. Filing is
    idempotent via ``find_by_source``: a crash between filing and completion
    refiles nothing new on the next pass.
    """

    if commitments is None:
        from core.commitments import CommitmentStore

        commitments = CommitmentStore()
    still_owed = {
        (str(item.get("unit_id") or ""), str(item.get("requirement") or "").strip().casefold()): item
        for item in outstanding_delivery(store, run_id)
    }
    handoffs: list[dict[str, Any]] = []
    for debt in debts or []:
        if not isinstance(debt, dict):
            continue
        unit_id = str(debt.get("unit_id") or "").strip()
        requirement = str(debt.get("requirement") or "").strip()
        if not unit_id or not requirement:
            continue
        current = still_owed.get((unit_id, requirement.casefold()))
        if current is None:
            continue
        reason = str(current.get("reason") or debt.get("reason") or "").strip()
        owner = str(current.get("owner") or debt.get("owner") or "").strip()
        source_ref = handoff_source_ref(run_id, unit_id, requirement)
        existing = commitments.find_by_source(HANDOFF_SOURCE, source_ref)
        if existing is None:
            existing = commitments.propose(
                title=_handoff_title(unit_id, requirement),
                detail=_handoff_detail(
                    run_id,
                    unit_id,
                    requirement,
                    reason,
                    current.get("leg_id"),
                    current.get("attempt_id"),
                ),
                actor="fleet",
                source=HANDOFF_SOURCE,
                source_ref=source_ref,
            )
        handoffs.append(
            {
                "run_id": run_id,
                "unit_id": unit_id,
                "requirement": requirement,
                "reason": reason,
                "owner": owner,
                "commitment_id": existing.commitment_id,
                "title": existing.title,
                "source_ref": source_ref,
                "leg_id": current.get("leg_id"),
                "attempt_id": current.get("attempt_id"),
            }
        )
    return handoffs


def format_handoff_section(handoffs: list[dict[str, Any]]) -> str:
    """Result-text listing for a run that completed with handed-off debt."""

    lines = [
        "",
        "",
        f"Fleet delivery handoff ({HANDOFF_MARKER}): all agent steps finished "
        f"with {len(handoffs)} delivery requirement"
        + ("" if len(handoffs) == 1 else "s")
        + " handed to the operator as tracked commitments:",
    ]
    for item in handoffs:
        short = " ".join(str(item.get("requirement") or "").split())
        if len(short) > 160:
            short = short[:157].rstrip() + "..."
        lines.append(
            f"- [{item.get('unit_id')}] {short} (commitment {item.get('commitment_id')})"
        )
    return "\n".join(lines)


def handoff_report_actions(handoffs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Deterministic report actions for handed-off delivery, with commitment ids."""

    actions: list[dict[str, str]] = []
    for item in handoffs:
        short = " ".join(str(item.get("requirement") or "").split())
        if len(short) > 160:
            short = short[:157].rstrip() + "..."
        actions.append(
            {
                "action": (
                    f"Finish handed-off delivery [{item.get('unit_id')}]: {short} "
                    f"(commitment {item.get('commitment_id')})"
                ),
                "reason": (
                    f"Fleet run {item.get('run_id')} completed with this delivery "
                    f"requirement outstanding ({HANDOFF_MARKER}); it is tracked as "
                    f"commitment {item.get('commitment_id')}."
                ),
            }
        )
    return actions
