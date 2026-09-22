"""Project-scoped proactive advice and recipient usefulness receipts."""
import json
import time
import uuid

from fleet.context import redact_text
from fleet.project_identity import project_identity, run_identity, terms


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fleet_findings (
            id TEXT PRIMARY KEY, project TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
            worker TEXT NOT NULL, attempt_id TEXT NOT NULL, dedupe TEXT NOT NULL,
            summary TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            UNIQUE(run_id,worker,dedupe)
        );
        CREATE TABLE IF NOT EXISTS fleet_finding_uses (
            finding_id TEXT NOT NULL REFERENCES fleet_findings ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
            worker TEXT NOT NULL, supplied REAL NOT NULL, useful INTEGER, reason TEXT,
            PRIMARY KEY(finding_id,run_id,worker)
        );
    """)


def publish(peer, token, summary, evidence_paths, dedupe):
    from fleet.learning import fingerprints
    if not 20 <= len(summary) <= 1200 or not 1 <= len(dedupe) <= 100:
        raise ValueError("finding needs a 20-1200 character summary and stable dedupe key")
    with peer.store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        who = peer._identity(db, token)
        if who.get("help_id"):
            raise PermissionError("consultants may only answer their assigned request")
        run = peer.store.get_run(who["run_id"])
        evidence = fingerprints(run["cwd"], evidence_paths)
        prior = db.execute("SELECT * FROM fleet_findings WHERE run_id=? AND worker=? AND dedupe=?",
                           (who["run_id"], who["worker_key"], dedupe)).fetchone()
        if prior:
            return dict(prior)
        if db.execute("SELECT COUNT(*) FROM fleet_findings WHERE run_id=?", (who["run_id"],)).fetchone()[0] >= 8:
            raise ValueError("run finding budget exhausted")
        identifier, now = str(uuid.uuid4()), time.time()
        db.execute("INSERT INTO fleet_findings VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (identifier, project_identity(run), who["run_id"], who["worker_key"], who["attempt_id"],
                    dedupe, redact_text(summary)[0], json.dumps(evidence), now, now + 30 * 86400))
        return {"id": identifier, "state": "unverified_advice"}


def recall(peer, token, query=""):
    from fleet.learning import fingerprints
    with peer.store._connect() as db:
        who = peer._identity(db, token)
        run = peer.store.get_run(who["run_id"])
        wanted = terms(query or run["task"])
        rows = [dict(r) for r in db.execute("SELECT f.*, COALESCE((SELECT SUM(useful) FROM fleet_finding_uses u "
                    "WHERE u.finding_id=f.id),0) AS useful_count FROM fleet_findings f WHERE project=? AND expires>? "
                    "ORDER BY created DESC LIMIT 100", (project_identity(run), time.time()))]
        rows.sort(key=lambda r: (len(wanted & terms(r["summary"])), r["useful_count"]), reverse=True)
        selected = []
        for row in rows:
            source = db.execute("SELECT state,cancel_requested FROM fleet_runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if not source or source["cancel_requested"] or source["state"] == "cancelled":
                continue
            if run_identity(db, row["run_id"]) != row["project"]:
                continue
            if not wanted & terms(row["summary"]):
                continue
            try:
                evidence = json.loads(row["evidence"])
                if fingerprints(run["cwd"], list(evidence)) != evidence:
                    continue
            except (ValueError, OSError):
                continue
            selected.append({**row, "state": "unverified_advice"})
            db.execute("INSERT OR IGNORE INTO fleet_finding_uses(finding_id,run_id,worker,supplied) VALUES(?,?,?,?)",
                       (row["id"], who["run_id"], who["worker_key"], time.time()))
            if len(selected) == 3:
                break
        return selected


def feedback(peer, token, finding_id, useful, reason):
    if not 10 <= len(reason) <= 1000:
        raise ValueError("explain the observed usefulness in 10-1000 characters")
    with peer.store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        who = peer._identity(db, token)
        if who.get("help_id"):
            raise PermissionError("consultants cannot confirm requester outcomes")
        row = db.execute("SELECT f.* FROM fleet_findings f JOIN fleet_finding_uses u ON u.finding_id=f.id "
                         "WHERE f.id=? AND u.run_id=? AND u.worker=?", (finding_id, who["run_id"], who["worker_key"])).fetchone()
        if not row or row["project"] != project_identity(peer.store.get_run(who["run_id"])):
            raise PermissionError("finding was not supplied to this requester")
        if row["run_id"] == who["run_id"] and row["worker"] == who["worker_key"]:
            raise PermissionError("authors cannot rate their own findings")
        db.execute("UPDATE fleet_finding_uses SET useful=?,reason=? WHERE finding_id=? AND run_id=? AND worker=?",
                   (int(bool(useful)), redact_text(reason)[0], finding_id, who["run_id"], who["worker_key"]))
        return {"id": finding_id, "useful": bool(useful), "verified_lesson": False}
