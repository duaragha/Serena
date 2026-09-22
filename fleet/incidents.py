"""Deterministic incident projection of Fleet's durable event journal.

An observed recovery is never a diagnosis or a verified remedy. The journal is
the retry source if the optional projection fails; no model participates in capture.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import suppress

from fleet.context import redact_text, redact_value
from fleet.project_identity import (
    project_identity,
    report_identity,
    repository_identity,
    stored_identity,
    terms,
)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fleet_incidents (
            id TEXT PRIMARY KEY, project TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
            leg_id TEXT, attempt_id TEXT, event_seq INTEGER NOT NULL,
            source_key TEXT NOT NULL, category TEXT NOT NULL, fingerprint TEXT NOT NULL,
            summary TEXT NOT NULL, provider TEXT, phase TEXT,
            created REAL NOT NULL, expires REAL NOT NULL,
            recovery_event INTEGER, recovery_attempt TEXT,
            UNIQUE(run_id, source_key)
        );
        CREATE INDEX IF NOT EXISTS fleet_incident_project ON fleet_incidents(project, created);
        CREATE TABLE IF NOT EXISTS fleet_incident_cursor (
            id INTEGER PRIMARY KEY CHECK(id=1), event_seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fleet_incident_uses (
            run_id TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
            attempt_id TEXT NOT NULL, incident_id TEXT NOT NULL REFERENCES fleet_incidents ON DELETE CASCADE,
            created REAL NOT NULL, PRIMARY KEY(run_id, attempt_id, incident_id)
        );
        CREATE TABLE IF NOT EXISTS fleet_lesson_incidents (
            lesson_id TEXT NOT NULL, incident_id TEXT NOT NULL,
            PRIMARY KEY(lesson_id, incident_id)
        );
    """)


def failure(kind, payload):
    if kind.startswith(("learning.", "incident.")):
        return None
    code = payload.get("exit_code")
    failed = (isinstance(code, int) and code != 0) or payload.get("is_error") is True
    failed |= bool(payload.get("error")) or payload.get("status") in {"failed", "error"}
    failed |= kind.endswith(("failed", "rejected", "recovery_exhausted"))
    failed |= payload.get("accepted") is False
    failed |= str(payload.get("type", "")) in {"error", "turn.failed"}
    if not failed:
        return None
    if "integration" in kind or "completion" in kind:
        return "gate"
    if "recover" in kind:
        return "recovery"
    if code is not None or payload.get("item_type") in {"mcp_tool_call", "command_execution"}:
        return "tool_observation"
    return "provider_or_runtime"


def capture(db, event):
    payload = json.loads(event["payload_json"])
    if not isinstance(payload, dict):
        return
    kind = event["type"]
    # Legacy journal rows predate snapshots; do not move them to a repository
    # observed only by a later event.
    project = event.get("repository_identity") or stored_identity(db, event["run_id"], at_creation=True)
    if kind in {"worker.integration.accepted", "attempt.completed"}:
        gate = payload.get("test_gate") or {}
        if kind == "attempt.completed" or (gate.get("ran") and gate.get("ok")):
            db.execute("UPDATE fleet_incidents SET recovery_event=?, recovery_attempt=? "
                       "WHERE run_id=? AND leg_id=? AND recovery_event IS NULL AND event_seq<? AND project=?",
                       (event["event_seq"], event["attempt_id"], event["run_id"],
                        event["leg_id"], event["event_seq"], project))
    category = failure(kind, payload)
    if not category:
        return
    run = db.execute("SELECT cwd FROM fleet_runs WHERE run_id=?", (event["run_id"],)).fetchone()
    if not run:
        return
    leg = db.execute("SELECT runtime,phase FROM fleet_legs WHERE leg_id=?", (event["leg_id"],)).fetchone()
    safe = redact_value(payload)[0]
    summary = redact_text(kind + ": " + json.dumps(safe, sort_keys=True, default=str))[0][:3000]
    # Provider item/event identities deduplicate transport replay, but equal output
    # without an identity is a distinct observation, never silently collapsed.
    native = payload.get("event_id") or payload.get("item_id")
    key = f"{event['attempt_id']}:{kind}:{native}" if native else f"event:{event['event_seq']}"
    if native and payload.get("help_id"):
        # Native item IDs restart in each fresh consultant process. Legacy
        # events without a dispatch cannot prove replay, so retain each event.
        dispatch = payload.get("consultation_dispatch")
        key = (json.dumps(["consultation", payload["help_id"], dispatch, kind, native])
               if dispatch is not None else f"event:{event['event_seq']}")
    incident_id = hashlib.sha256((event["run_id"] + ":" + key).encode()).hexdigest()
    diagnostic = {k: v for k, v in safe.items() if k not in {"event_id", "item_id", "attempt_id", "session_id", "help_id", "consultation_dispatch"}}
    fingerprint = hashlib.sha256((category + ":" + json.dumps(diagnostic, sort_keys=True)).encode()).hexdigest()
    db.execute("""INSERT OR IGNORE INTO fleet_incidents
        (id,project,run_id,leg_id,attempt_id,event_seq,source_key,category,fingerprint,
         summary,provider,phase,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (incident_id, project or repository_identity(run["cwd"]), event["run_id"], event["leg_id"],
         event["attempt_id"], event["event_seq"], key, category, fingerprint, summary,
         leg["runtime"] if leg else None, leg["phase"] if leg else None,
         event["created_at"], event["created_at"] + 30 * 86400))


def capture_safely(db, event):
    started = False
    try:
        db.execute("SAVEPOINT incident_capture")
        started = True
        capture(db, event)
        db.execute("RELEASE incident_capture")
        started = False
    except Exception:
        if started:
            with suppress(Exception):
                db.execute("ROLLBACK TO incident_capture")
                db.execute("RELEASE incident_capture")
        logging.getLogger(__name__).exception("Fleet incident projection failed; journal retained for reconciliation")
        return False
    return True


def reconcile(store, limit=200):
    """Advance a durable cursor by at most 200 journal events per invocation."""
    try:
        _reconcile(store, limit)
    except Exception:
        logging.getLogger(__name__).exception("Optional incident reconciliation unavailable; cursor retained")


def _reconcile(store, limit):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute("SELECT event_seq FROM fleet_incident_cursor WHERE id=1").fetchone()
        rows = db.execute("SELECT * FROM fleet_events WHERE event_seq>? ORDER BY event_seq LIMIT ?",
                          (cursor[0] if cursor else 0, min(max(limit, 1), 200))).fetchall()
        for row in rows:
            if not capture_safely(db, dict(row)):
                break
            db.execute("INSERT INTO fleet_incident_cursor VALUES(1,?) ON CONFLICT(id) "
                       "DO UPDATE SET event_seq=excluded.event_seq", (row["event_seq"],))


def recall(store, run, attempt_id, query=""):
    try:
        return _recall(store, run, attempt_id, query)
    except Exception:
        logging.getLogger(__name__).exception("Optional incident recall unavailable")
        return []


def _recall(store, run, attempt_id, query):
    reconcile(store)
    wanted = terms(query or run["task"])
    with store._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM fleet_incidents WHERE project=? "
                "AND expires>? ORDER BY created DESC LIMIT 100",
                (project_identity(run), time.time()))]
        rows.sort(key=lambda r: len(wanted & terms(r["summary"])), reverse=True)
        selected = [r for r in rows if r["run_id"] == run["run_id"] or wanted & terms(r["summary"])][:5]
        for row in selected:
            db.execute("INSERT OR IGNORE INTO fleet_incident_uses VALUES(?,?,?,?)",
                       (run["run_id"], attempt_id, row["id"], time.time()))
        return selected


def report(store, project):
    reconcile(store)
    with store._connect() as db:
        identity = report_identity(db, project)
        rows = [dict(r) for r in db.execute("SELECT i.*, (SELECT COUNT(*) FROM fleet_incidents x "
                "WHERE x.project=i.project AND x.fingerprint=i.fingerprint) AS recurrence "
                "FROM fleet_incidents i WHERE project=? ORDER BY created DESC LIMIT 100",
                (identity,))]
        uses = db.execute("SELECT COUNT(*) FROM fleet_incident_uses u JOIN fleet_incidents i "
                          "ON i.id=u.incident_id WHERE i.project=?", (identity,)).fetchone()[0]
        totals = db.execute("SELECT COUNT(*), SUM(CASE WHEN recovery_event IS NULL THEN 1 ELSE 0 END) "
                            "FROM fleet_incidents WHERE project=?", (identity,)).fetchone()
        resolutions = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='fleet_lessons'").fetchone():
            resolutions = [dict(r) for r in db.execute(
                "SELECT x.incident_id,l.id AS lesson_id,l.state,l.expires,l.source_run FROM fleet_lesson_incidents x "
                "JOIN fleet_lessons l ON l.id=x.lesson_id WHERE l.project=? ORDER BY l.created DESC LIMIT 100",
                (identity,))]
        return {"incidents": rows, "supplied_advice": uses,
                "resolution_links": resolutions,
                "total_incidents": totals[0], "unresolved": totals[1] or 0, "display_limit": 100,
                "recovery_note": "Observed subsequent success is not proof of cause or a verified remedy."}


def linked(db, lesson_id):
    return [dict(r) for r in db.execute("SELECT i.* FROM fleet_lesson_incidents l LEFT JOIN fleet_incidents i "
            "ON i.id=l.incident_id WHERE l.lesson_id=?", (lesson_id,))]


def links_valid(db, lesson_id, project):
    return all(r["id"] and r["project"] == project and r["expires"] > time.time()
               for r in linked(db, lesson_id))
