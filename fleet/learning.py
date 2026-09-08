"""Project-scoped operational lessons with independent review and test provenance.

This is a Fleet playbook, not personal memory and not model-weight training.
No lesson can execute commands or change model, permission, or acceptance policy.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from fleet.context import redact_text
from fleet.store import FleetStore


def fingerprints(cwd: str, paths: list[str]) -> dict[str, str]:
    root = Path(cwd).resolve()
    if not 1 <= len(paths) <= 8:
        raise ValueError("provide 1–8 project-relative evidence paths")
    result = {}
    for name in paths:
        path = root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or not path.resolve().is_relative_to(root):
            raise ValueError("evidence must stay inside the project")
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError("evidence must be an existing file under 1 MB")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class FleetLearning:
    def __init__(self, store: FleetStore):
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fleet_lessons (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL,
                    source_run TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
                    source_attempt TEXT NOT NULL, author TEXT NOT NULL, summary TEXT NOT NULL,
                    evidence TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'candidate',
                    reviewer TEXT, review_attempt TEXT, review_reason TEXT,
                    created REAL NOT NULL, expires REAL NOT NULL, promoted REAL, reason TEXT,
                    UNIQUE(source_run, author, summary)
                );
                CREATE TABLE IF NOT EXISTS fleet_lesson_uses (
                    run_id TEXT NOT NULL REFERENCES fleet_runs ON DELETE CASCADE,
                    lesson_id TEXT NOT NULL REFERENCES fleet_lessons ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL, outcome TEXT,
                    PRIMARY KEY(run_id, lesson_id, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS fleet_learning_outcomes (
                    run_id TEXT PRIMARY KEY REFERENCES fleet_runs ON DELETE CASCADE,
                    project TEXT NOT NULL, state TEXT NOT NULL, duration REAL,
                    attempts INTEGER NOT NULL, retries INTEGER NOT NULL, lessons_used INTEGER NOT NULL,
                    test_gates INTEGER NOT NULL, created REAL NOT NULL
                );
            """)

    def propose(self, who: dict, summary: str, evidence_paths: list[str]) -> dict:
        if who.get("help_id") or not 20 <= len(summary.strip()) <= 1200:
            raise ValueError("ordinary workers may propose a 20–1200 character evidence-backed lesson")
        run = self.store.get_run(who["run_id"])
        evidence = fingerprints(run["cwd"], evidence_paths)
        summary = redact_text(summary.strip())[0]
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM fleet_lessons WHERE source_run = ?", (who["run_id"],)).fetchone()[0]
            existing = db.execute("SELECT * FROM fleet_lessons WHERE source_run = ? AND author = ? AND summary = ?",
                                  (who["run_id"], who["worker_key"], summary)).fetchone()
            if existing:
                return dict(existing)
            if count >= 8:
                raise ValueError("run lesson budget exhausted")
            lesson_id, now = str(uuid.uuid4()), time.time()
            db.execute("""INSERT INTO fleet_lessons
                (id,project,source_run,source_attempt,author,summary,evidence,created,expires)
                VALUES (?,?,?,?,?,?,?,?,?)""", (lesson_id, str(Path(run["cwd"]).resolve()), who["run_id"],
                    who["attempt_id"], who["worker_key"], summary, json.dumps(evidence), now, now + 30 * 86400))
            self.store._insert_event(db, run_id=who["run_id"], event_type="learning.proposed", payload={"lesson_id": lesson_id})
            return {"lesson_id": lesson_id, "state": "candidate", "evidence": evidence}

    def review(self, who: dict, lesson_id: str, approve: bool, reason: str) -> dict:
        if who.get("help_id") or not 10 <= len(reason.strip()) <= 1000:
            raise ValueError("an independent Review worker must explain its evidence check")
        run = self.store.get_run(who["run_id"])
        leg = next(item for p in run["phases"] for item in p["legs"] if item["leg_id"] == who["leg_id"])
        phase = next(p["name"] for p in run["phases"] if leg in p["legs"])
        if phase != "verify":
            raise PermissionError("only Review can endorse lessons")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM fleet_lessons WHERE id = ? AND source_run = ?", (lesson_id, who["run_id"])).fetchone()
            if not row or row["author"] == who["worker_key"] or row["state"] != "candidate":
                raise PermissionError("review must be independent and candidate must belong to this run")
            evidence = json.loads(row["evidence"])
            if fingerprints(run["cwd"], list(evidence)) != evidence:
                raise ValueError("candidate evidence changed; propose a fresh lesson")
            state = "endorsed" if approve else "rejected"
            db.execute("UPDATE fleet_lessons SET state = ?, reviewer = ?, review_attempt = ?, review_reason = ? WHERE id = ?",
                       (state, who["worker_key"], who["attempt_id"], redact_text(reason)[0], lesson_id))
            self.store._insert_event(db, run_id=who["run_id"], event_type="learning.reviewed",
                                    payload={"lesson_id": lesson_id, "state": state})
            return {"lesson_id": lesson_id, "state": state}

    def retrieve(self, run: dict, attempt_id: str) -> list[dict]:
        project = str(Path(run["cwd"]).resolve())
        selected = []
        with self.store._connect() as db:
            rows = db.execute("""SELECT * FROM fleet_lessons WHERE project = ? AND state = 'verified'
                AND expires > ? ORDER BY promoted DESC LIMIT 40""", (project, time.time())).fetchall()
            for row in rows:
                evidence = json.loads(row["evidence"])
                try:
                    current = fingerprints(project, list(evidence))
                except (ValueError, OSError):
                    continue
                if current != evidence:
                    continue
                # Exact file applicability, not a generic bag of all previous advice.
                if not any(path in run["task"] or Path(path).name in run["task"] for path in evidence):
                    continue
                selected.append({"id": row["id"], "summary": row["summary"], "evidence": evidence,
                                 "source_run": row["source_run"]})
                db.execute("INSERT OR IGNORE INTO fleet_lesson_uses(run_id,lesson_id,attempt_id) VALUES (?,?,?)",
                           (run["run_id"], row["id"], attempt_id))
                if len(selected) == 3:
                    break
        return selected

    def finish(self, run: dict) -> None:
        """Idempotent terminal promotion. A CLI's successful exit alone is not evidence."""
        run_id = run["run_id"]
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            events = [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM fleet_events WHERE run_id = ? AND type = 'worker.integration.accepted'", (run_id,))]
            gates = sum(bool(e.get("test_gate", {}).get("ran") and e.get("test_gate", {}).get("ok")) for e in events)
            attempts = db.execute("SELECT COUNT(*) FROM fleet_attempts a JOIN fleet_legs l ON a.leg_id = l.leg_id WHERE l.run_id = ?", (run_id,)).fetchone()[0]
            used = db.execute("SELECT COUNT(DISTINCT lesson_id) FROM fleet_lesson_uses WHERE run_id = ?", (run_id,)).fetchone()[0]
            total_legs = sum(len(p["legs"]) for p in run["phases"])
            duration = max(0, float(run.get("completed_at") or time.time()) - float(run["created_at"]))
            db.execute("INSERT OR REPLACE INTO fleet_learning_outcomes VALUES (?,?,?,?,?,?,?,?,?)",
                       (run_id, str(Path(run["cwd"]).resolve()), run["state"], duration, attempts,
                        max(0, attempts - total_legs), used, gates, time.time()))
            db.execute("UPDATE fleet_lesson_uses SET outcome = ? WHERE run_id = ?", (run["state"], run_id))
            if run["state"] != "completed" or not gates:
                return
            for row in db.execute("SELECT * FROM fleet_lessons WHERE source_run = ? AND state = 'endorsed'", (run_id,)).fetchall():
                reviewer = db.execute("SELECT state FROM fleet_attempts WHERE attempt_id = ?", (row["review_attempt"],)).fetchone()
                author = db.execute("SELECT state FROM fleet_attempts WHERE attempt_id = ?", (row["source_attempt"],)).fetchone()
                evidence = json.loads(row["evidence"])
                try:
                    unchanged = fingerprints(run["cwd"], list(evidence)) == evidence
                except (ValueError, OSError):
                    unchanged = False
                if not unchanged or not reviewer or reviewer[0] != "completed" or not author or author[0] != "completed":
                    continue
                db.execute("UPDATE fleet_lessons SET state = 'verified', promoted = ? WHERE id = ?", (time.time(), row["id"]))
                self.store._insert_event(db, run_id=run_id, event_type="learning.promoted",
                                        payload={"lesson_id": row["id"], "test_gates": gates})

    def rollback(self, lesson_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("rollback requires a reason")
        with self.store._connect() as db:
            row = db.execute("SELECT source_run FROM fleet_lessons WHERE id = ?", (lesson_id,)).fetchone()
            if not row:
                raise KeyError(lesson_id)
            db.execute("UPDATE fleet_lessons SET state = 'revoked', reason = ? WHERE id = ?", (redact_text(reason)[0][:1000], lesson_id))
            self.store._insert_event(db, run_id=row[0], event_type="learning.revoked", payload={"lesson_id": lesson_id})

    def projection(self, run_id: str) -> dict:
        with self.store._connect() as db:
            return {"candidates": [dict(r) for r in db.execute("SELECT * FROM fleet_lessons WHERE source_run = ?", (run_id,))],
                    "uses": [dict(r) for r in db.execute("SELECT * FROM fleet_lesson_uses WHERE run_id = ?", (run_id,))],
                    "outcomes": [dict(r) for r in db.execute("SELECT * FROM fleet_learning_outcomes WHERE run_id = ?", (run_id,))]}

    def report(self, project: str) -> dict:
        """Observed cohorts, not a causal speedup claim or automatic policy mutation."""
        with self.store._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM fleet_learning_outcomes WHERE project = ? ORDER BY created DESC LIMIT 100",
                                                   (str(Path(project).resolve()),))]
            for row in rows:
                usage = []
                for event in db.execute("SELECT payload_json FROM fleet_events WHERE run_id = ? AND type IN ('worker.event','peer.worker.event')",
                                        (row["run_id"],)):
                    payload = json.loads(event[0])
                    if isinstance(payload.get("usage"), dict):
                        usage.append(payload["usage"])
                row["reported_tokens"] = ({key: sum(item.get(key, 0) for item in usage)
                    for key in {key for item in usage for key in item}} if usage else None)
        return {"runs": rows, "interpretation": "Observed outcomes only. Different tasks/models confound comparisons; "
                "no guarantee of speedup, no measured escaped-defect rate, no automatic model/policy changes. "
                "Token counts are provider-reported and null when unavailable."}
