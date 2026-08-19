"""Durable delivery of spoken work requests to Serena's coding worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_VOICE_INBOX_PATH = (
    Path.home() / ".local" / "state" / "serena" / "voice_inbox.sqlite3"
)
DEFAULT_VOICE_WORK_MARKER_PATH = (
    Path.home() / ".config" / "serena" / "voice_working"
)
MAX_VOICE_REQUEST_CHARS = 4_000
CLAIM_TTL_SECONDS = 30.0
WORK_TTL_SECONDS = 24 * 60 * 60
# A control can still land on a job that has not reached Codex yet. Refusing a
# cancel because the row does not say 'working' means the cancel arrives after
# the work does, which is the same as no cancel at all.
CONTROLLABLE_WORK_STATES = frozenset({"working", "resume_queued"})
CONTROLLABLE_QUEUE_STATES = frozenset({"queued", "claimed", "delivered"})
CANCELLABLE_STATES = CONTROLLABLE_WORK_STATES | CONTROLLABLE_QUEUE_STATES
CANCELLED_BEFORE_START_SUMMARY = "cancelled before the coding worker started"
# The bridge drops an oversized snapshot whole, so bound every list here.
MAX_OVERLAY_CHANGES = 200
MAX_OVERLAY_LIST = 40
# Under both the bridge's 55,000 snapshot gate and its 60,000 whole-event gate,
# with room for the wrapper.
MAX_OVERLAY_BYTES = 45_000
_ACCEPTED_BRIEF_LIST_FIELDS = (
    "relevant_conversation",
    "project_context",
    "memory_guidance",
    "ledger_guidance",
    "handoff_guidance",
    "acceptance_criteria",
    "authority_boundaries",
)
# Present on every brief this code writes, tolerated as absent so an older
# persisted brief still enqueues on a resume.
_ACCEPTED_BRIEF_OPTIONAL_LIST_FIELDS = ("likely_files",)
RESIDENT_LEASE_TTL_SECONDS = 5.0
_RESIDENT_CLAIM_ERROR = "resident voice worker owns queue"
AUTOMATIC_RECOVERY_EVENT = "automatic_recovery.queued"
AUTOMATIC_RECOVERY_TERMINAL_EVENT = "automatic_recovery.terminal"
AUTOMATIC_RECOVERY_LIMIT = 3
_AUTOMATIC_RECOVERY_BACKOFF = (2.0, 10.0, 30.0)
_RECOVERY_VOLATILE = re.compile(
    r"\b(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{16,}|\d+)\b",
    re.IGNORECASE,
)


def automatic_recovery_fingerprint(kind: object, error: object) -> str:
    normalized = " ".join(str(error or "").casefold().split())
    normalized = _RECOVERY_VOLATILE.sub("<volatile>", normalized)
    value = f"{str(kind).strip().casefold()}\n{normalized}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def automatic_recovery_backoff(recovery_no: int) -> float:
    index = max(0, min(int(recovery_no) - 1, len(_AUTOMATIC_RECOVERY_BACKOFF) - 1))
    return _AUTOMATIC_RECOVERY_BACKOFF[index]


def _canonical_resource_root(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:
        return ""


def _fit_overlay_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Shrink one snapshot until it fits the overlay transport.

    The bridge drops an event that is too large rather than truncating it, so a
    job touching a thousand files would blank the panel instead of showing him
    less. Drop the least load-bearing lists first and keep saying how much went.
    """

    def size() -> int:
        return len(json.dumps(snapshot, ensure_ascii=False, default=str).encode("utf-8"))

    if size() <= MAX_OVERLAY_BYTES:
        return snapshot
    for key in ("context", "authority_boundaries", "acceptance_criteria"):
        values = snapshot["brief"].get(key) or []
        while values and size() > MAX_OVERLAY_BYTES:
            values = values[: len(values) // 2]
            snapshot["brief"][key] = values
    while snapshot["changes"] and size() > MAX_OVERLAY_BYTES:
        keep = len(snapshot["changes"]) // 2
        snapshot["changes_truncated"] += len(snapshot["changes"]) - keep
        snapshot["changes"] = snapshot["changes"][:keep]
    for key in ("tests", "live_proof"):
        while snapshot[key] and size() > MAX_OVERLAY_BYTES:
            snapshot[key] = snapshot[key][: len(snapshot[key]) // 2]
    return snapshot


@dataclass(frozen=True, slots=True)
class VoiceInboxItem:
    item_id: str
    request: str
    call_id: str
    turn_id: str
    state: str
    created_at: float
    target_sid: str = ""
    brief: dict[str, Any] | None = None

    @property
    def prompt(self) -> str:
        if self.brief:
            from core.coding_job_contract import prompt_brief

            projected = prompt_brief(self.brief)
            orientation = (
                "The brief carries likely_files: Serena's guess at where this "
                "work lives, assembled before you started. Read those first "
                "rather than searching the repository from nothing. Treat every "
                "entry as unverified, confirm or discard each one as you read "
                "it, and search normally for anything it missed.\n\n"
                if projected.get("likely_files")
                else ""
            )
            return (
                "This is an accepted Serena coding job. The JSON brief is the "
                "durable source of truth. Do not broaden its authority. The frozen "
                "baseline patch is held in the durable record, not here; work from "
                "the live tree and leave every listed dirty path alone.\n\n"
                + orientation
                + json.dumps(projected, ensure_ascii=False, indent=2, sort_keys=True)
            )
        return (
            "Raghav said this aloud to me just now. Treat it exactly as a message "
            "he typed in this chat. Continue in the current project and act on it "
            "now, following the normal safety and wait-for-go rules. Do not ask him "
            "to repeat it.\n\nSpoken request:\n"
            + self.request
        )


class VoiceInboxStore:
    """Small SQLite outbox shared by the voice host and Serena desktop app."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        work_marker_path: str | Path | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_VOICE_INBOX_PATH")
        self.path = Path(path or configured or DEFAULT_VOICE_INBOX_PATH).expanduser()
        configured_marker = os.environ.get("SERENA_VOICE_WORK_MARKER_PATH")
        if work_marker_path is not None:
            marker = work_marker_path
        elif path is not None:
            marker = self.path.with_name("voice_working")
        else:
            marker = configured_marker or DEFAULT_VOICE_WORK_MARKER_PATH
        self.work_marker_path = Path(marker).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS voice_inbox (
                        item_id TEXT PRIMARY KEY,
                        request TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        claimed_at REAL,
                        delivered_at REAL,
                        target_sid TEXT NOT NULL DEFAULT '',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        UNIQUE(call_id, turn_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_inbox_state_created
                    ON voice_inbox(state, created_at);

                    CREATE TABLE IF NOT EXISTS voice_work (
                        item_id TEXT PRIMARY KEY,
                        target_sid TEXT NOT NULL,
                        cwd TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        summary TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS voice_work_state_target
                    ON voice_work(state, target_sid);

                    CREATE TABLE IF NOT EXISTS voice_job_brief (
                        item_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        project_root TEXT NOT NULL,
                        codex_model TEXT NOT NULL,
                        codex_effort TEXT NOT NULL,
                        review_model TEXT NOT NULL,
                        review_effort TEXT NOT NULL,
                        accepted_at REAL NOT NULL,
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );

                    CREATE TABLE IF NOT EXISTS voice_job_route (
                        item_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        preference TEXT NOT NULL,
                        project_root TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT '',
                        group_id TEXT NOT NULL DEFAULT '',
                        bridge_port INTEGER,
                        title TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        bound_focus INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'selected',
                        start_offset INTEGER,
                        end_offset INTEGER,
                        prompt_sha256 TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );

                    CREATE TABLE IF NOT EXISTS voice_job_attempt (
                        attempt_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        attempt_no INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        requested_model TEXT NOT NULL,
                        requested_effort TEXT NOT NULL,
                        reported_model TEXT NOT NULL DEFAULT '',
                        reported_effort TEXT NOT NULL DEFAULT '',
                        resume_session_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        exit_code INTEGER,
                        last_error TEXT NOT NULL DEFAULT '',
                        UNIQUE(item_id, attempt_no),
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_job_attempt_item
                    ON voice_job_attempt(item_id, attempt_no);

                    CREATE TABLE IF NOT EXISTS voice_job_recovery (
                        recovery_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        recovery_no INTEGER NOT NULL,
                        category TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        trigger_reason TEXT NOT NULL,
                        state TEXT NOT NULL,
                        eligible_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        terminal_reason TEXT NOT NULL DEFAULT '',
                        budget INTEGER NOT NULL DEFAULT 3,
                        private_route INTEGER NOT NULL DEFAULT 0,
                        resumed_session INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(item_id, recovery_no),
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_job_recovery_item
                    ON voice_job_recovery(item_id, recovery_no);
                    CREATE UNIQUE INDEX IF NOT EXISTS voice_job_recovery_active
                    ON voice_job_recovery(item_id)
                    WHERE state IN ('scheduled', 'running');

                    CREATE TABLE IF NOT EXISTS voice_job_event (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id TEXT NOT NULL,
                        attempt_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_job_event_item
                    ON voice_job_event(item_id, event_id);

                    CREATE TABLE IF NOT EXISTS voice_job_control (
                        control_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        applied_at REAL,
                        last_error TEXT NOT NULL DEFAULT '',
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_job_control_pending
                    ON voice_job_control(item_id, state, created_at);

                    CREATE TABLE IF NOT EXISTS voice_job_evidence (
                        item_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        complete INTEGER NOT NULL,
                        captured_at REAL NOT NULL,
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );

                    CREATE TABLE IF NOT EXISTS voice_job_review (
                        review_id TEXT PRIMARY KEY,
                        item_id TEXT NOT NULL,
                        required INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        effort TEXT NOT NULL DEFAULT '',
                        reported_model TEXT NOT NULL DEFAULT '',
                        reported_effort TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        approved INTEGER,
                        findings_json TEXT NOT NULL DEFAULT '[]',
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        finished_at REAL,
                        last_error TEXT NOT NULL DEFAULT '',
                        FOREIGN KEY(item_id) REFERENCES voice_inbox(item_id)
                    );

                    CREATE TABLE IF NOT EXISTS voice_worker_lease (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        owner_id TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        heartbeat REAL NOT NULL
                    );

                    """
                )
                connection.executescript(
                    """
                    DROP TRIGGER IF EXISTS voice_inbox_resident_owner;
                    CREATE TRIGGER voice_inbox_resident_owner
                    BEFORE UPDATE OF state, target_sid ON voice_inbox
                    WHEN NEW.state = 'claimed'
                         AND NEW.target_sid NOT LIKE 'headless-voice-%'
                    BEGIN
                        SELECT RAISE(ABORT, 'resident voice worker owns queue');
                    END;
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(voice_work)")
                }
                if "session_id" not in columns:
                    connection.execute(
                        "ALTER TABLE voice_work ADD COLUMN session_id TEXT "
                        "NOT NULL DEFAULT ''"
                    )
                if "summary" not in columns:
                    connection.execute(
                        "ALTER TABLE voice_work ADD COLUMN summary TEXT "
                        "NOT NULL DEFAULT ''"
                    )
                recovery_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(voice_job_recovery)"
                    )
                }
                for name, declaration in (
                    ("budget", "INTEGER NOT NULL DEFAULT 3"),
                    ("private_route", "INTEGER NOT NULL DEFAULT 0"),
                    ("resumed_session", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in recovery_columns:
                        connection.execute(
                            f"ALTER TABLE voice_job_recovery ADD COLUMN {name} {declaration}"
                        )
            with suppress(OSError):
                self.path.chmod(0o600)
            self._schema_ready = True
        self._expire_stale_work()
        self._sync_work_marker()

    def enqueue(self, request: str, *, call_id: str, turn_id: str) -> VoiceInboxItem:
        clean = " ".join(str(request).strip().split())
        if not clean:
            raise ValueError("spoken work request cannot be empty")
        if len(clean) > MAX_VOICE_REQUEST_CHARS:
            raise ValueError("spoken work request is too long")
        item_id = str(uuid.uuid4())
        created_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO voice_inbox(
                    item_id, request, call_id, turn_id, state, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (item_id, clean, call_id, turn_id, created_at),
            )
            row = connection.execute(
                "SELECT * FROM voice_inbox WHERE call_id=? AND turn_id=?",
                (call_id, turn_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("spoken work request was not persisted")
        return self._item(row)

    def enqueue_accepted(
        self,
        brief: Mapping[str, Any],
        *,
        call_id: str,
        turn_id: str,
    ) -> VoiceInboxItem:
        """Atomically enqueue one already validated and frozen coding brief."""

        payload = dict(brief)
        item_id = str(payload.get("item_id") or "").strip()
        exact = str(payload.get("exact_request") or "")
        trigger = str(payload.get("triggering_request") or "")
        root = str(payload.get("project_root") or "").strip()
        outcome = str(payload.get("requested_outcome") or "")
        if not item_id or not exact.strip() or not trigger.strip() or not root or not outcome.strip():
            raise ValueError("accepted coding brief is incomplete")
        if int(payload.get("schema_version") or 0) != 1:
            raise ValueError("accepted coding brief schema is unsupported")
        for field_name in _ACCEPTED_BRIEF_LIST_FIELDS:
            value = payload.get(field_name)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"accepted coding brief field {field_name} is invalid")
        for field_name in _ACCEPTED_BRIEF_OPTIONAL_LIST_FIELDS:
            if field_name not in payload:
                continue
            value = payload.get(field_name)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"accepted coding brief field {field_name} is invalid")
        if not payload["acceptance_criteria"] or not payload["authority_boundaries"]:
            raise ValueError("accepted coding brief criteria and boundaries cannot be empty")
        if not isinstance(payload.get("commit_authorized"), bool):
            raise ValueError("accepted coding brief must freeze commit authority")
        initial_git = payload.get("initial_git")
        if not isinstance(initial_git, Mapping) or not str(initial_git.get("tree") or ""):
            raise ValueError("accepted coding brief has no frozen Git tree")
        # Effort is tiered per job now, so the frozen value has to agree with
        # the complexity the brain judged. That keeps this a real policy check
        # instead of letting any effort string through.
        from core.coding_job_contract import frozen_implement_effort
        from core.coding_model_preferences import normalise_coding_model

        try:
            selected_model = normalise_coding_model(
                payload.get("coding_model"), strict=True
            )
        except ValueError as error:
            raise ValueError("accepted coding brief model selection is invalid") from error
        if "coding_model" in payload:
            payload["coding_model"] = selected_model

        model_policy = payload.get("model_policy")
        if isinstance(model_policy, Mapping) and model_policy:
            from core.serena_policy import SerenaPolicyError, validate_frozen_decision

            try:
                implement_decision = validate_frozen_decision(
                    model_policy.get("implement_decision"),
                    profile="coding",
                    role="implement",
                )
                review_decision = validate_frozen_decision(
                    model_policy.get("review_decision"),
                    profile="coding",
                    role="review",
                )
            except SerenaPolicyError as error:
                raise ValueError("accepted coding brief model policy is invalid") from error
            implement = model_policy.get("implement") or {}
            review = model_policy.get("review") or {}
            if (
                payload.get("implement_provider") != implement_decision["provider"]
                or payload.get("implement_model") != implement_decision["model"]
                or payload.get("implement_effort") != implement_decision["effort"]
                or payload.get("review_provider") != review_decision["provider"]
                or payload.get("review_model") != review_decision["model"]
                or payload.get("review_effort") != review_decision["effort"]
                or implement.get("model") != implement_decision["model"]
                or review.get("model") != review_decision["model"]
                or payload.get("codex_effort") != frozen_implement_effort(payload)
            ):
                raise ValueError("accepted coding brief model policy is inconsistent")
        elif (
            payload.get("codex_model") != "gpt-5.6-sol"
            or payload.get("codex_effort") != frozen_implement_effort(payload)
            or payload.get("review_model") != "claude-opus-5"
            or payload.get("review_effort") != "xhigh"
        ):
            raise ValueError("accepted coding brief model policy is invalid")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        created_at = float(payload.get("accepted_at") or time.time())
        raw_route = payload.get("work_route")
        route = dict(raw_route) if isinstance(raw_route, Mapping) else {}
        route_mode = str(route.get("mode") or "private")
        route_preference = str(route.get("preference") or "auto")
        if route_mode not in {"private", "reuse"}:
            raise ValueError("accepted coding brief route mode is invalid")
        if route_preference not in {"auto", "new", "existing"}:
            raise ValueError("accepted coding brief route preference is invalid")
        route_session = str(route.get("session_id") or "").strip()
        if route_mode == "reuse" and not route_session:
            raise ValueError("accepted reuse route has no Codex session")
        bridge_port = route.get("bridge_port")
        if bridge_port in (None, ""):
            bridge_port = None
        else:
            try:
                bridge_port = int(bridge_port)
            except (TypeError, ValueError) as error:
                raise ValueError("accepted coding brief bridge port is invalid") from error
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO voice_inbox(
                    item_id, request, call_id, turn_id, state, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (item_id, exact, str(call_id), str(turn_id), created_at),
            )
            row = connection.execute(
                "SELECT * FROM voice_inbox WHERE call_id=? AND turn_id=?",
                (str(call_id), str(turn_id)),
            ).fetchone()
            if row is None:
                raise RuntimeError("accepted coding job was not persisted")
            accepted_id = str(row["item_id"])
            if accepted_id == item_id:
                connection.execute(
                    """
                    INSERT INTO voice_job_brief(
                        item_id, schema_version, payload_json, project_root,
                        codex_model, codex_effort, review_model, review_effort,
                        accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        int(payload["schema_version"]),
                        encoded,
                        root,
                        str(payload.get("codex_model") or ""),
                        str(payload.get("codex_effort") or ""),
                        str(payload.get("review_model") or ""),
                        str(payload.get("review_effort") or ""),
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO voice_job_route(
                        item_id, mode, preference, project_root, session_id,
                        group_id, bridge_port, title, reason, bound_focus,
                        state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'selected', ?)
                    """,
                    (
                        item_id,
                        route_mode,
                        route_preference,
                        root,
                        route_session,
                        str(route.get("group_id") or ""),
                        bridge_port,
                        str(route.get("title") or "")[:500],
                        str(route.get("reason") or "")[:1_000],
                        1 if route.get("bound_focus") else 0,
                        created_at,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._item(row)

    def item_for_turn(self, *, call_id: str, turn_id: str) -> VoiceInboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_inbox WHERE call_id=? AND turn_id=?",
                (str(call_id), str(turn_id)),
            ).fetchone()
        return self._item(row) if row is not None else None

    def accepted_brief(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM voice_job_brief WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def route_record(self, item_id: str) -> dict[str, Any] | None:
        """Return the route frozen when this coding job was accepted."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_job_route WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_route_dispatch(
        self,
        item_id: str,
        state: str,
        *,
        start_offset: int | None = None,
        end_offset: int | None = None,
        prompt_sha256: str = "",
    ) -> bool:
        """Advance one reused-chat delivery without ever erasing its receipt."""

        if state not in {"selected", "committed", "completed", "uncertain"}:
            raise ValueError("invalid coding route dispatch state")
        updates = ["state=?", "updated_at=?"]
        values: list[Any] = [state, time.time()]
        if start_offset is not None:
            updates.append("start_offset=COALESCE(start_offset, ?)")
            values.append(max(0, int(start_offset)))
        if end_offset is not None:
            updates.append("end_offset=?")
            values.append(max(0, int(end_offset)))
        if prompt_sha256:
            updates.append(
                "prompt_sha256=CASE WHEN prompt_sha256='' THEN ? ELSE prompt_sha256 END"
            )
            values.append(str(prompt_sha256))
        values.append(str(item_id))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE voice_job_route SET {', '.join(updates)} WHERE item_id=?",
                values,
            )
        return cursor.rowcount == 1

    def prepare_route_dispatch(self, item_id: str, prompt_sha256: str) -> bool:
        """Start the next turn only after the prior reused turn completed."""

        digest = str(prompt_sha256).strip()
        if not digest:
            raise ValueError("coding route prompt digest is required")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_job_route
                SET state='selected', start_offset=NULL, end_offset=NULL,
                    prompt_sha256=?, updated_at=?
                WHERE item_id=? AND mode='reuse'
                  AND (
                    prompt_sha256=''
                    OR state='completed'
                    OR (state='selected' AND prompt_sha256 != ?)
                  )
                """,
                (digest, time.time(), str(item_id), digest),
            )
        return cursor.rowcount == 1

    def claim_next(
        self,
        target_sid: str,
        *,
        claim_ttl: float = CLAIM_TTL_SECONDS,
        excluded_project_roots: Collection[str] = (),
    ) -> VoiceInboxItem | None:
        target_sid = str(target_sid).strip()
        if not target_sid:
            raise ValueError("target session id is required")
        now = time.time()
        cutoff = now - max(1.0, claim_ttl)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, target_sid='',
                    last_error='delivery claim expired'
                WHERE state='claimed' AND claimed_at < ?
                """,
                (cutoff,),
            )
            excluded = {
                canonical
                for root in excluded_project_roots
                if (canonical := _canonical_resource_root(root))
            }
            rows = connection.execute(
                """
                SELECT inbox.*, brief.project_root AS resource_project_root,
                       (
                         SELECT recovery.eligible_at
                         FROM voice_job_recovery AS recovery
                         WHERE recovery.item_id=inbox.item_id
                           AND recovery.state='scheduled'
                         ORDER BY recovery.recovery_no DESC LIMIT 1
                       ) AS recovery_eligible_at
                FROM voice_inbox AS inbox
                LEFT JOIN voice_job_brief AS brief ON brief.item_id = inbox.item_id
                WHERE inbox.state='queued'
                ORDER BY inbox.created_at, inbox.item_id
                """
            ).fetchall()
            row = None
            for candidate in rows:
                if float(candidate["recovery_eligible_at"] or 0.0) > now:
                    continue
                stored_root = str(candidate["resource_project_root"] or "").strip()
                if not stored_root:
                    # Legacy work without a brief is allowed through only so
                    # _run_item can reject it before any process starts.
                    row = candidate
                    break
                canonical = _canonical_resource_root(stored_root)
                if not canonical:
                    # An unreadable resource cannot be proven independent of
                    # active work. Keep it queued until the checkout is idle.
                    if excluded:
                        continue
                    row = candidate
                    break
                if canonical not in excluded:
                    row = candidate
                    break
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='claimed', claimed_at=?, target_sid=?, attempts=attempts+1
                WHERE item_id=? AND state='queued'
                """,
                (now, target_sid, row["item_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM voice_inbox WHERE item_id=?",
                (row["item_id"],),
            ).fetchone()
            connection.commit()
        except sqlite3.DatabaseError as error:
            connection.rollback()
            if _RESIDENT_CLAIM_ERROR in str(error):
                return None
            raise
        finally:
            connection.close()
        return self._item(claimed) if claimed is not None else None

    def renew_resident_lease(
        self,
        owner_id: str,
        *,
        pid: int,
        heartbeat: float | None = None,
    ) -> None:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise ValueError("resident worker owner id is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_worker_lease(singleton, owner_id, pid, heartbeat)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    pid=excluded.pid,
                    heartbeat=excluded.heartbeat
                """,
                (owner_id, int(pid), time.time() if heartbeat is None else heartbeat),
            )

    def clear_resident_lease(self, owner_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_worker_lease WHERE singleton=1 AND owner_id=?",
                (str(owner_id).strip(),),
            )
        return cursor.rowcount == 1

    def resident_lease_active(self, *, now: float | None = None) -> bool:
        cutoff = (time.time() if now is None else now) - RESIDENT_LEASE_TTL_SECONDS
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM voice_worker_lease
                WHERE singleton=1 AND heartbeat >= ?
                """,
                (cutoff,),
            ).fetchone()
        return row is not None

    def acknowledge(self, item_id: str, *, target_sid: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=''
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (time.time(), item_id, target_sid),
            )
        return cursor.rowcount == 1

    def acknowledge_started(
        self,
        item_id: str,
        *,
        target_sid: str,
        cwd: str = "",
    ) -> bool:
        """Atomically mark pane delivery and register its running work turn."""

        target_sid = str(target_sid).strip()
        if not target_sid:
            return False
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=''
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (now, item_id, target_sid),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO voice_work(
                    item_id, target_sid, cwd, session_id, state, started_at,
                    finished_at, summary, last_error
                ) VALUES (?, ?, ?, '', 'working', ?, NULL, '', '')
                ON CONFLICT(item_id) DO UPDATE SET
                    target_sid=excluded.target_sid,
                    cwd=excluded.cwd,
                    session_id=CASE
                        WHEN voice_work.state='resume_queued' THEN voice_work.session_id
                        ELSE ''
                    END,
                    state='working',
                    started_at=excluded.started_at,
                    finished_at=NULL,
                    summary='',
                    last_error=''
                """,
                (item_id, target_sid, str(cwd).strip(), now),
            )
            connection.execute(
                """
                UPDATE voice_job_recovery
                SET state='running', started_at=COALESCE(started_at, ?)
                WHERE item_id=? AND state='scheduled' AND eligible_at <= ?
                """,
                (now, str(item_id), now),
            )
            connection.commit()
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def set_work_session(self, item_id: str, session_id: str) -> bool:
        session_id = str(session_id).strip()
        if not session_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work SET session_id=?
                WHERE item_id=? AND state='working'
                """,
                (session_id, item_id),
            )
        return cursor.rowcount == 1

    def work_record(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_work WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def attempt_provider_for_session(self, item_id: str, session_id: str) -> str:
        """Return the provider that actually persisted this job session."""

        session_id = str(session_id).strip()
        if not session_id:
            return ""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider
                FROM voice_job_attempt
                WHERE item_id=? AND session_id=?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (str(item_id), session_id),
            ).fetchone()
        return str(row["provider"] or "") if row is not None else ""

    def start_attempt(
        self,
        item_id: str,
        *,
        provider: str,
        model: str,
        effort: str,
        resume_session_id: str = "",
    ) -> tuple[str, int]:
        attempt_id = str(uuid.uuid4())
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS number "
                "FROM voice_job_attempt WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            number = int(row["number"] if row is not None else 1)
            connection.execute(
                """
                INSERT INTO voice_job_attempt(
                    attempt_id, item_id, attempt_no, provider, requested_model,
                    requested_effort, resume_session_id, state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    attempt_id,
                    str(item_id),
                    number,
                    str(provider),
                    str(model),
                    str(effort),
                    str(resume_session_id),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return attempt_id, number

    def warm_session_for_project(
        self,
        project_root: str | Path,
        *,
        provider: str,
        max_age_seconds: float,
        exclude_item_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """The newest healthy session this provider left in that exact repo.

        Orientation is the expensive part of a coding job and it is paid again
        on every cold start: on 2026-08-05 a worker spent 7.6 minutes and
        ninety Bash calls rediscovering a layout it had been told about many
        times. A session that already read this repository knows it.

        Bounded hard on purpose. Exact canonical project root, so a session
        can never carry knowledge of one repository into another. Exact
        provider, so a Codex thread id is never handed to Claude. Completed
        attempts only, because a failed or cancelled run may have died
        mid-edit. And a recency window, because a stale session's map of the
        tree is worse than no map.
        """

        root = str(project_root).strip()
        provider = str(provider).strip()
        if not root or not provider:
            return None
        cutoff = (time.time() if now is None else float(now)) - float(max_age_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt.session_id AS session_id,
                       attempt.item_id AS item_id,
                       attempt.started_at AS started_at,
                       attempt.finished_at AS finished_at,
                       attempt.requested_model AS requested_model
                FROM voice_job_attempt AS attempt
                JOIN voice_job_brief AS brief ON brief.item_id = attempt.item_id
                JOIN voice_work AS work ON work.item_id = attempt.item_id
                WHERE attempt.provider = ?
                  AND attempt.state = 'completed'
                  AND attempt.exit_code = 0
                  AND work.state = 'completed'
                  AND attempt.session_id != ''
                  AND attempt.item_id != ?
                  AND brief.project_root = ?
                  AND COALESCE(attempt.finished_at, attempt.started_at) >= ?
                ORDER BY COALESCE(attempt.finished_at, attempt.started_at) DESC
                LIMIT 1
                """,
                (provider, str(exclude_item_id), root, cutoff),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_attempt_session(self, attempt_id: str, session_id: str) -> bool:
        session_id = str(session_id).strip()
        if not session_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE voice_job_attempt SET session_id=? "
                "WHERE attempt_id=? AND state='running'",
                (session_id, str(attempt_id)),
            )
        return cursor.rowcount == 1

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        exit_code: int | None,
        reported_model: str = "",
        reported_effort: str = "",
        error: str = "",
    ) -> bool:
        if state not in {"completed", "failed", "cancelled", "steered"}:
            raise ValueError("invalid coding attempt state")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_job_attempt
                SET state=?, finished_at=?, exit_code=?, reported_model=?,
                    reported_effort=?, last_error=?
                WHERE attempt_id=? AND state='running'
                """,
                (
                    state,
                    time.time(),
                    exit_code,
                    str(reported_model),
                    str(reported_effort),
                    str(error)[:1_000],
                    str(attempt_id),
                ),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _close_running_attempts(
        connection: sqlite3.Connection,
        item_ids: list[str],
        *,
        state: str,
        error: str,
        finished_at: float,
    ) -> int:
        """Close orphan attempts in the same transaction as their work item."""

        clean_ids = [str(item_id) for item_id in item_ids if str(item_id)]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _item_id in clean_ids)
        cursor = connection.execute(
            f"""
            UPDATE voice_job_attempt
            SET state=?, finished_at=?, exit_code=NULL, last_error=?
            WHERE state='running' AND item_id IN ({placeholders})
            """,
            (
                state,
                float(finished_at),
                str(error).strip()[:1_000],
                *clean_ids,
            ),
        )
        return cursor.rowcount

    @staticmethod
    def _close_active_recoveries(
        connection: sqlite3.Connection,
        item_ids: list[str],
        *,
        state: str,
        reason: str,
        finished_at: float,
    ) -> int:
        clean_ids = [str(item_id) for item_id in item_ids if str(item_id)]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _item_id in clean_ids)
        cursor = connection.execute(
            f"""
            UPDATE voice_job_recovery
            SET state=?, finished_at=?, terminal_reason=?
            WHERE state IN ('scheduled', 'running')
              AND item_id IN ({placeholders})
            """,
            (
                str(state),
                float(finished_at),
                str(reason).strip()[:1_000],
                *clean_ids,
            ),
        )
        return cursor.rowcount

    def record_job_event(
        self,
        item_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        attempt_id: str = "",
    ) -> int:
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO voice_job_event(
                    item_id, attempt_id, kind, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(item_id), str(attempt_id), str(kind), encoded, time.time()),
            )
        return int(cursor.lastrowid)

    def record_evidence(self, item_id: str, evidence: Mapping[str, Any]) -> None:
        payload = dict(evidence)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_job_evidence(item_id, payload_json, complete, captured_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    complete=excluded.complete,
                    captured_at=excluded.captured_at
                """,
                (
                    str(item_id),
                    encoded,
                    1 if payload.get("complete") else 0,
                    float(payload.get("captured_at") or time.time()),
                ),
            )

    def evidence(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM voice_job_evidence WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def begin_review(
        self,
        item_id: str,
        *,
        required: bool,
        reason: str,
        model: str = "",
        effort: str = "",
    ) -> str:
        review_id = str(uuid.uuid4())
        state = "pending" if required else "skipped"
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_job_review(
                    review_id, item_id, required, reason, model, effort,
                    state, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    str(item_id),
                    1 if required else 0,
                    str(reason),
                    str(model),
                    str(effort),
                    state,
                    now,
                    None if required else now,
                ),
            )
        return review_id

    def finish_review(
        self,
        review_id: str,
        *,
        approved: bool,
        findings: list[Mapping[str, Any]] | list[str],
        reported_model: str,
        reported_effort: str,
        session_id: str = "",
        error: str = "",
    ) -> bool:
        state = "failed" if error else "completed"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_job_review
                SET reported_model=?, reported_effort=?, session_id=?,
                    approved=?, findings_json=?, state=?, finished_at=?, last_error=?
                WHERE review_id=? AND state='pending'
                """,
                (
                    str(reported_model),
                    str(reported_effort),
                    str(session_id),
                    1 if approved else 0,
                    json.dumps(findings, ensure_ascii=False, separators=(",", ":")),
                    state,
                    time.time(),
                    str(error)[:1_000],
                    str(review_id),
                ),
            )
        return cursor.rowcount == 1

    def request_control(self, item_id: str, action: str, *, text: str = "") -> str:
        action = str(action).strip().casefold()
        if action not in {"cancel", "steer"}:
            raise ValueError("unsupported coding job control")
        clean_text = str(text).strip()
        if action == "steer" and not clean_text:
            raise ValueError("steering text is required")
        if len(clean_text) > 4_000:
            raise ValueError("steering text is too long")
        record = self.work_record(item_id)
        work_state = str((record or {}).get("state") or "")
        if work_state not in CONTROLLABLE_WORK_STATES:
            with self._connect() as connection:
                queue = connection.execute(
                    "SELECT state FROM voice_inbox WHERE item_id=?", (str(item_id),)
                ).fetchone()
                recovery = connection.execute(
                    """
                    SELECT 1 FROM voice_job_recovery
                    WHERE item_id=? AND state IN ('scheduled', 'running')
                    LIMIT 1
                    """,
                    (str(item_id),),
                ).fetchone()
            queued_recovery = bool(
                work_state == "failed"
                and recovery is not None
                and queue is not None
                and str(queue["state"]) in CONTROLLABLE_QUEUE_STATES
            )
            if not queued_recovery and (
                work_state
                or queue is None
                or str(queue["state"]) not in CONTROLLABLE_QUEUE_STATES
            ):
                raise ValueError("coding job is not running")
        control_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_job_control(
                    control_id, item_id, action, text, state, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (control_id, str(item_id), action, clean_text, time.time()),
            )
        return control_id

    def request_cancel(self, item_id: str) -> str:
        """Interrupt a running job, or take a not-yet-started one out of the queue.

        Order matters. Record the durable control first, so a job that starts in
        the same instant still gets interrupted, then take the atomic pre-start
        transition for the far more common case where nothing is running yet.
        """

        control_id = self.request_control(item_id, "cancel")
        self.cancel_before_start(item_id)
        return control_id

    def add_steering(self, item_id: str, text: str) -> str:
        return self.request_control(item_id, "steer", text=text)

    def request_resume(self, item_id: str) -> str:
        control_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            work = connection.execute(
                "SELECT state, session_id FROM voice_work WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            if work is None or str(work["state"]) not in {"failed", "cancelled"}:
                raise ValueError("coding job is not resumable")
            if not str(work["session_id"] or "").strip():
                raise ValueError("coding job has no persisted Codex session")
            connection.execute(
                """
                INSERT INTO voice_job_control(
                    control_id, item_id, action, text, state, created_at, applied_at
                ) VALUES (?, ?, 'resume', '', 'applied', ?, ?)
                """,
                (control_id, str(item_id), time.time(), time.time()),
            )
            connection.execute(
                """
                UPDATE voice_work
                SET state='resume_queued', finished_at=NULL, last_error=''
                WHERE item_id=?
                """,
                (str(item_id),),
            )
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, delivered_at=NULL,
                    target_sid='', last_error=''
                WHERE item_id=?
                """,
                (str(item_id),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._sync_work_marker()
        return control_id

    def cancel_before_start(self, item_id: str) -> bool:
        """Atomically stop a job that has not reached Codex yet.

        A queued, claimed, or resume-queued job is about to run. It has no live
        process to interrupt, so cancelling it means taking it out of the queue
        in one transaction rather than waiting for a worker to notice.
        """

        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            queue = connection.execute(
                "SELECT state FROM voice_inbox WHERE item_id=?", (str(item_id),)
            ).fetchone()
            if queue is None:
                connection.rollback()
                return False
            work = connection.execute(
                "SELECT state FROM voice_work WHERE item_id=?", (str(item_id),)
            ).fetchone()
            work_state = str(work["state"]) if work is not None else ""
            active_recovery = connection.execute(
                """
                SELECT 1 FROM voice_job_recovery
                WHERE item_id=? AND state IN ('scheduled', 'running')
                LIMIT 1
                """,
                (str(item_id),),
            ).fetchone()
            recoverable_failed = bool(
                work_state == "failed"
                and active_recovery is not None
                and str(queue["state"] or "") in {"queued", "claimed"}
            )
            if work_state in {"working", "completed", "cancelled"} or (
                work_state == "failed" and not recoverable_failed
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO voice_work(
                    item_id, target_sid, cwd, session_id, state, started_at,
                    finished_at, summary, last_error
                ) VALUES (?, '', '', '', 'cancelled', ?, ?, ?, '')
                ON CONFLICT(item_id) DO UPDATE SET
                    state='cancelled', finished_at=?, summary=?, last_error=''
                """,
                (
                    str(item_id),
                    now,
                    now,
                    CANCELLED_BEFORE_START_SUMMARY,
                    now,
                    CANCELLED_BEFORE_START_SUMMARY,
                ),
            )
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=''
                WHERE item_id=?
                """,
                (now, str(item_id)),
            )
            connection.execute(
                """
                UPDATE voice_job_control
                SET state='applied', applied_at=?
                WHERE item_id=? AND state='pending' AND action='cancel'
                """,
                (now, str(item_id)),
            )
            self._close_running_attempts(
                connection,
                [str(item_id)],
                state="cancelled",
                error=CANCELLED_BEFORE_START_SUMMARY,
                finished_at=now,
            )
            self._close_active_recoveries(
                connection,
                [str(item_id)],
                state="cancelled",
                reason=CANCELLED_BEFORE_START_SUMMARY,
                finished_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def pending_controls(self, item_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM voice_job_control
                WHERE item_id=? AND state='pending'
                ORDER BY created_at, control_id
                """,
                (str(item_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_control(self, control_id: str, *, error: str = "") -> bool:
        state = "failed" if error else "applied"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_job_control
                SET state=?, applied_at=?, last_error=?
                WHERE control_id=? AND state='pending'
                """,
                (state, time.time(), str(error)[:500], str(control_id)),
            )
        return cursor.rowcount == 1

    def finish_work_item(
        self,
        item_id: str,
        *,
        error: str = "",
        summary: str = "",
        state: str | None = None,
        require_evidence: bool = False,
    ) -> bool:
        final_state = state or ("failed" if error else "completed")
        if final_state not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid coding job final state")
        accepted = self.accepted_brief(item_id) is not None
        if final_state == "completed" and (require_evidence or accepted):
            evidence = self.evidence(item_id)
            if not evidence or evidence.get("complete") is not True:
                raise ValueError("coding job cannot complete without mechanical evidence")
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state=?, finished_at=?, summary=?, last_error=?
                WHERE item_id=? AND state='working'
                """,
                (
                    final_state,
                    now,
                    str(summary).strip()[:2_000],
                    str(error).strip()[:500],
                    item_id,
                ),
            )
            if cursor.rowcount:
                orphan_state = "cancelled" if final_state == "cancelled" else "failed"
                orphan_error = str(error).strip() or (
                    f"job {final_state} while coding attempt was still running"
                )
                self._close_running_attempts(
                    connection,
                    [str(item_id)],
                    state=orphan_state,
                    error=orphan_error,
                    finished_at=now,
                )
                self._close_active_recoveries(
                    connection,
                    [str(item_id)],
                    state=(
                        "succeeded"
                        if final_state == "completed"
                        else "terminal" if final_state == "failed" else final_state
                    ),
                    reason=str(error).strip() or str(summary).strip(),
                    finished_at=now,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if cursor.rowcount:
            self._sync_work_marker()
        return cursor.rowcount == 1

    def recover_headless_work(self) -> int:
        """Bound continuations after an unclean resident-worker exit."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT item_id FROM voice_work
                WHERE state='working' AND target_sid LIKE 'headless-voice-%'
                """
            ).fetchall()
        recovered = 0
        error = "resident worker restarted"
        for row in rows:
            item_id = str(row["item_id"])
            result = self.queue_automatic_recovery(
                item_id,
                error=error,
                kind="supervisor",
                max_recoveries=AUTOMATIC_RECOVERY_LIMIT,
                recover_existing=True,
            )
            if result.get("queued") is True:
                recovered += 1
                continue
            if result.get("terminal") is True:
                continue
            terminal_error = error
            reason = str(result.get("reason") or "")
            if reason:
                terminal_error += "; " + reason
            self.finish_work_item(item_id, error=terminal_error)
        return recovered

    def requeue_work_item(self, item_id: str, *, error: str) -> bool:
        """Return one interrupted resident job to the durable queue."""

        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT session_id FROM voice_work WHERE item_id=? AND state='working'",
                (str(item_id),),
            ).fetchone()
            session_id = str(row["session_id"] or "") if row is not None else ""
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state=?, finished_at=?, last_error=?
                WHERE item_id=? AND state='working'
                """,
                (
                    "resume_queued" if session_id else "failed",
                    now,
                    str(error).strip()[:500],
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, delivered_at=NULL,
                    target_sid='', last_error=?
                WHERE item_id=?
                """,
                (str(error).strip()[:500], item_id),
            )
            self._close_running_attempts(
                connection,
                [str(item_id)],
                state="failed",
                error=str(error),
                finished_at=now,
            )
            connection.commit()
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def queue_automatic_recovery(
        self,
        item_id: str,
        *,
        error: str,
        kind: str,
        max_recoveries: int,
        force_private_route: bool = False,
        drop_uncommitted_session: bool = False,
        active_recovery_id: str = "",
        recover_existing: bool = False,
    ) -> dict[str, Any]:
        """Atomically persist one bounded continuation of the logical job."""

        item_id = str(item_id)
        clean_error = str(error).strip()[:500]
        clean_kind = str(kind).strip()[:100]
        budget = max(0, int(max_recoveries))
        active_recovery_id = str(active_recovery_id).strip()
        now = time.time()
        connection = self._connect()
        queued = False
        result: dict[str, Any]
        try:
            connection.execute("BEGIN IMMEDIATE")
            inbox = connection.execute(
                "SELECT state, target_sid FROM voice_inbox WHERE item_id=?",
                (item_id,),
            ).fetchone()
            work = connection.execute(
                "SELECT state, session_id, cwd FROM voice_work WHERE item_id=?",
                (item_id,),
            ).fetchone()
            route = connection.execute(
                "SELECT mode, preference, state, start_offset FROM voice_job_route "
                "WHERE item_id=?",
                (item_id,),
            ).fetchone()
            recovery_rows = connection.execute(
                """
                SELECT * FROM voice_job_recovery
                WHERE item_id=? ORDER BY recovery_no
                """,
                (item_id,),
            ).fetchall()
            terminal = next(
                (
                    row
                    for row in reversed(recovery_rows)
                    if str(row["state"] or "") == "terminal"
                ),
                None,
            )
            active = next(
                (
                    row
                    for row in reversed(recovery_rows)
                    if str(row["state"] or "") in {"scheduled", "running"}
                ),
                None,
            )
            if inbox is None:
                result = {
                    "queued": False,
                    "reason": "coding job no longer exists",
                }
            elif terminal is not None:
                result = {
                    "queued": False,
                    "terminal": True,
                    "reason": str(terminal["terminal_reason"] or "automatic recovery stopped"),
                    "recoveries": sum(
                        str(row["state"] or "") != "terminal" for row in recovery_rows
                    ),
                }
            elif active is not None and not (
                recover_existing
                or (
                    active_recovery_id
                    and str(active["recovery_id"] or "") == active_recovery_id
                )
            ):
                result = {
                    "queued": False,
                    "reason": "an automatic recovery is already queued",
                    "recoveries": sum(
                        str(row["state"] or "") != "terminal" for row in recovery_rows
                    ),
                }
            else:
                if active is not None:
                    connection.execute(
                        """
                        UPDATE voice_job_recovery
                        SET state='failed', finished_at=?, terminal_reason=?
                        WHERE recovery_id=? AND state IN ('scheduled', 'running')
                        """,
                        (now, clean_error, str(active["recovery_id"])),
                    )
                    recovery_rows = connection.execute(
                        """
                        SELECT * FROM voice_job_recovery
                        WHERE item_id=? ORDER BY recovery_no
                        """,
                        (item_id,),
                    ).fetchall()
                used = sum(
                    str(row["state"] or "") != "terminal" for row in recovery_rows
                )
                fingerprint = automatic_recovery_fingerprint(clean_kind, clean_error)
                repeated = any(
                    str(row["state"] or "") != "terminal"
                    and str(row["fingerprint"] or "") == fingerprint
                    for row in recovery_rows
                )
                terminal_reason = ""
                if str(inbox["state"] or "") == "queued":
                    result = {
                        "queued": False,
                        "reason": "an automatic recovery is already queued",
                        "recoveries": used,
                    }
                elif budget <= 0 or used >= budget:
                    terminal_reason = f"automatic recovery budget of {budget} exhausted"
                elif repeated:
                    terminal_reason = (
                        "automatic recovery stopped after the same failure repeated "
                        "without progress"
                    )
                elif work is not None and str(work["state"] or "") not in {
                    "working",
                    "failed",
                    "resume_queued",
                }:
                    result = {
                        "queued": False,
                        "reason": f"coding job is {str(work['state'] or 'not recoverable')}",
                        "recoveries": used,
                    }
                elif work is None and str(inbox["state"] or "") != "claimed":
                    result = {
                        "queued": False,
                        "reason": "coding job has no recoverable claim or work record",
                        "recoveries": used,
                    }
                elif force_private_route and route is not None and str(
                    route["mode"] or ""
                ) == "reuse" and str(route["preference"] or "auto") != "auto":
                    terminal_reason = (
                        "explicit existing-chat routing cannot fall back automatically"
                    )
                else:
                    session_id = (
                        str(work["session_id"] or "") if work is not None else ""
                    )
                    route_changed = False
                    uncommitted = bool(
                        route is not None
                        and str(route["state"] or "selected") == "selected"
                        and route["start_offset"] is None
                    )
                    if force_private_route and route is not None and str(
                        route["mode"] or ""
                    ) == "reuse":
                        if drop_uncommitted_session and uncommitted:
                            session_id = ""
                        cursor = connection.execute(
                            """
                            UPDATE voice_job_route
                            SET mode='private', bridge_port=NULL,
                                reason=?, updated_at=?
                            WHERE item_id=? AND mode='reuse' AND preference='auto'
                            """,
                            (
                                ("automatic private fallback after " + clean_error)[:1_000],
                                now,
                                item_id,
                            ),
                        )
                        route_changed = cursor.rowcount == 1
                    if work is not None:
                        connection.execute(
                            """
                            UPDATE voice_work
                            SET state=?, session_id=?, finished_at=?, last_error=?
                            WHERE item_id=?
                              AND state IN ('working', 'failed', 'resume_queued')
                            """,
                            (
                                "resume_queued" if session_id else "failed",
                                session_id,
                                now,
                                clean_error,
                                item_id,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE voice_inbox
                        SET state='queued', claimed_at=NULL, delivered_at=NULL,
                            target_sid='', last_error=?
                        WHERE item_id=?
                        """,
                        (clean_error, item_id),
                    )
                    self._close_running_attempts(
                        connection,
                        [item_id],
                        state="failed",
                        error=clean_error,
                        finished_at=now,
                    )
                    recovery_no = used + 1
                    delay = automatic_recovery_backoff(recovery_no)
                    eligible_at = now + delay
                    recovery_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO voice_job_recovery(
                            recovery_id, item_id, recovery_no, category,
                            fingerprint, trigger_reason, state, eligible_at,
                            created_at, budget, private_route, resumed_session
                        ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)
                        """,
                        (
                            recovery_id,
                            item_id,
                            recovery_no,
                            clean_kind,
                            fingerprint,
                            clean_error,
                            eligible_at,
                            now,
                            budget,
                            1 if route_changed else 0,
                            1 if session_id else 0,
                        ),
                    )
                    payload = json.dumps(
                        {
                            "recovery_id": recovery_id,
                            "recovery_no": recovery_no,
                            "budget": budget,
                            "kind": clean_kind,
                            "error": clean_error,
                            "fingerprint": fingerprint,
                            "eligible_at": eligible_at,
                            "private_route": route_changed,
                            "resumed_session": bool(session_id),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    connection.execute(
                        """
                        INSERT INTO voice_job_event(
                            item_id, attempt_id, kind, payload_json, created_at
                        ) VALUES (?, '', ?, ?, ?)
                        """,
                        (item_id, AUTOMATIC_RECOVERY_EVENT, payload, now),
                    )
                    queued = True
                    result = {
                        "queued": True,
                        "reason": "bounded automatic recovery queued",
                        "recoveries": recovery_no,
                        "budget": budget,
                        "private_route": route_changed,
                        "resumed_session": bool(session_id),
                    }

                if terminal_reason:
                    recovery_no = used + 1
                    terminal_error = clean_error
                    if terminal_reason:
                        terminal_error += "; " + terminal_reason
                    connection.execute(
                        """
                        INSERT INTO voice_job_recovery(
                            recovery_id, item_id, recovery_no, category,
                            fingerprint, trigger_reason, state, eligible_at,
                            created_at, finished_at, terminal_reason, budget
                        ) VALUES (?, ?, ?, ?, ?, ?, 'terminal', ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            item_id,
                            recovery_no,
                            clean_kind,
                            fingerprint,
                            clean_error,
                            now,
                            now,
                            now,
                            terminal_reason,
                            budget,
                        ),
                    )
                    if work is None:
                        connection.execute(
                            """
                            INSERT INTO voice_work(
                                item_id, target_sid, cwd, session_id, state,
                                started_at, finished_at, summary, last_error
                            ) VALUES (?, ?, '', '', 'failed', ?, ?, ?, ?)
                            ON CONFLICT(item_id) DO UPDATE SET
                                state='failed', finished_at=excluded.finished_at,
                                summary=excluded.summary, last_error=excluded.last_error
                            """,
                            (
                                item_id,
                                str(inbox["target_sid"] or ""),
                                now,
                                now,
                                terminal_reason[:2_000],
                                terminal_error[:500],
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE voice_work
                            SET state='failed', finished_at=?, summary=?, last_error=?
                            WHERE item_id=?
                              AND state IN ('working', 'failed', 'resume_queued')
                            """,
                            (
                                now,
                                terminal_reason[:2_000],
                                terminal_error[:500],
                                item_id,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE voice_inbox
                        SET state='delivered', delivered_at=COALESCE(delivered_at, ?),
                            claimed_at=NULL, last_error=?
                        WHERE item_id=?
                        """,
                        (now, terminal_error[:500], item_id),
                    )
                    self._close_running_attempts(
                        connection,
                        [item_id],
                        state="failed",
                        error=terminal_error,
                        finished_at=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO voice_job_event(
                            item_id, attempt_id, kind, payload_json, created_at
                        ) VALUES (?, '', ?, ?, ?)
                        """,
                        (
                            item_id,
                            AUTOMATIC_RECOVERY_TERMINAL_EVENT,
                            json.dumps(
                                {
                                    "kind": clean_kind,
                                    "error": clean_error,
                                    "fingerprint": fingerprint,
                                    "recoveries": used,
                                    "budget": budget,
                                    "reason": terminal_reason,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            now,
                        ),
                    )
                    result = {
                        "queued": False,
                        "terminal": True,
                        "reason": terminal_reason,
                        "recoveries": used,
                    }
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if queued or result.get("terminal") is True:
            self._sync_work_marker()
        return result

    @staticmethod
    def _recovery_entry(row: sqlite3.Row) -> dict[str, Any]:
        entry = dict(row)
        entry["kind"] = str(entry.pop("category") or "")
        entry["error"] = str(entry.pop("trigger_reason") or "")
        entry["private_route"] = bool(entry.get("private_route"))
        entry["resumed_session"] = bool(entry.get("resumed_session"))
        return entry

    def automatic_recoveries(self, item_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM voice_job_recovery
                WHERE item_id=? ORDER BY recovery_no
                """,
                (str(item_id),),
            ).fetchall()
        return [self._recovery_entry(row) for row in rows]

    def latest_automatic_recovery(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM voice_job_recovery
                WHERE item_id=? ORDER BY recovery_no DESC LIMIT 1
                """,
                (str(item_id),),
            ).fetchone()
        if row is None:
            return None
        return self._recovery_entry(row)

    def claim_automatic_recovery(self, item_id: str) -> dict[str, Any] | None:
        """Return the one active recovery, promoting it if its delay elapsed."""

        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM voice_job_recovery
                WHERE item_id=? AND state IN ('scheduled', 'running')
                ORDER BY recovery_no DESC LIMIT 1
                """,
                (str(item_id),),
            ).fetchone()
            if row is None or float(row["eligible_at"] or 0.0) > now:
                connection.commit()
                return None
            if str(row["state"] or "") == "scheduled":
                connection.execute(
                    """
                    UPDATE voice_job_recovery
                    SET state='running', started_at=COALESCE(started_at, ?)
                    WHERE recovery_id=? AND state='scheduled'
                    """,
                    (now, str(row["recovery_id"])),
                )
                row = connection.execute(
                    "SELECT * FROM voice_job_recovery WHERE recovery_id=?",
                    (str(row["recovery_id"]),),
                ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._recovery_entry(row) if row is not None else None

    def fail_claimed_item(
        self,
        item_id: str,
        *,
        target_sid: str,
        error: str,
        cwd: str = "",
    ) -> bool:
        """Make a pre-start failure terminal instead of reclaiming it forever."""

        item_id = str(item_id)
        target_sid = str(target_sid)
        clean_error = str(error).strip()[:500]
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=?
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (now, clean_error, item_id, target_sid),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO voice_work(
                    item_id, target_sid, cwd, session_id, state, started_at,
                    finished_at, summary, last_error
                ) VALUES (?, ?, ?, '', 'failed', ?, ?, '', ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    target_sid=excluded.target_sid,
                    cwd=excluded.cwd,
                    state='failed',
                    finished_at=excluded.finished_at,
                    summary='',
                    last_error=excluded.last_error
                """,
                (item_id, target_sid, str(cwd), now, now, clean_error),
            )
            self._close_active_recoveries(
                connection,
                [item_id],
                state="terminal",
                reason=clean_error,
                finished_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def migrate_work_target(self, old_sid: str, new_sid: str) -> int:
        old_sid = str(old_sid).strip()
        new_sid = str(new_sid).strip()
        if not old_sid or not new_sid or old_sid == new_sid:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work SET target_sid=?
                WHERE target_sid=? AND state='working'
                """,
                (new_sid, old_sid),
            )
        return cursor.rowcount

    def finish_work_target(self, target_sid: str, *, error: str = "") -> int:
        target_sid = str(target_sid).strip()
        if not target_sid:
            return 0
        state = "failed" if error else "completed"
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state=?, finished_at=?, last_error=?
                WHERE target_sid=? AND state='working'
                  AND (
                    ? != 'completed'
                    OR NOT EXISTS (
                        SELECT 1 FROM voice_job_brief
                        WHERE voice_job_brief.item_id=voice_work.item_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM voice_job_evidence
                        WHERE voice_job_evidence.item_id=voice_work.item_id
                          AND voice_job_evidence.complete=1
                    )
                  )
                """,
                (state, now, str(error)[:500], target_sid, state),
            )
            rows = connection.execute(
                """
                SELECT item_id FROM voice_work
                WHERE target_sid=? AND state=? AND finished_at=?
                """,
                (target_sid, state, now),
            ).fetchall()
            if rows:
                self._close_running_attempts(
                    connection,
                    [str(row["item_id"]) for row in rows],
                    state="failed",
                    error=str(error).strip()
                    or f"job {state} while coding attempt was still running",
                    finished_at=now,
                )
                self._close_active_recoveries(
                    connection,
                    [str(row["item_id"]) for row in rows],
                    state=(
                        "succeeded"
                        if state == "completed"
                        else "terminal" if state == "failed" else state
                    ),
                    reason=str(error).strip() or f"job {state}",
                    finished_at=now,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if cursor.rowcount:
            self._sync_work_marker()
        return cursor.rowcount

    def working_count(self) -> int:
        self._expire_stale_work()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_work WHERE state='working'"
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _expire_stale_work(self) -> int:
        cutoff = time.time() - WORK_TTL_SECONDS
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT item_id FROM voice_work WHERE state='working' AND started_at < ?",
                (cutoff,),
            ).fetchall()
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state='failed', finished_at=?, last_error='working lease expired'
                WHERE state='working' AND started_at < ?
                """,
                (now, cutoff),
            )
            self._close_running_attempts(
                connection,
                [str(row["item_id"]) for row in rows],
                state="failed",
                error="working lease expired",
                finished_at=now,
            )
            self._close_active_recoveries(
                connection,
                [str(row["item_id"]) for row in rows],
                state="terminal",
                reason="working lease expired",
                finished_at=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if cursor.rowcount:
            self._sync_work_marker()
        return cursor.rowcount

    def _sync_work_marker(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_work WHERE state='working'"
            ).fetchone()
        count = int(row["count"] if row is not None else 0)
        if count <= 0:
            with suppress(FileNotFoundError):
                self.work_marker_path.unlink()
            return
        self.work_marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.work_marker_path.with_name(
            f".{self.work_marker_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"state": "working", "count": count}) + "\n",
                encoding="utf-8",
            )
            with suppress(OSError):
                temporary.chmod(0o600)
            os.replace(temporary, self.work_marker_path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def release(self, item_id: str, *, target_sid: str, error: str = "") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, target_sid='', last_error=?
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (str(error)[:500], item_id, target_sid),
            )
        return cursor.rowcount == 1

    def pending_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_inbox WHERE state != 'delivered'"
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Newest jobs first, with just enough to name one out loud.

        He says "cancel that", not a UUID. Anything resolving a spoken
        reference to a job needs the ordered list plus the project and live
        state, so this stays a listing and leaves the judgement to the caller.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    inbox.item_id AS item_id,
                    inbox.request AS request,
                    inbox.state AS queue_state,
                    inbox.created_at AS created_at,
                    work.state AS work_state,
                    work.session_id AS session_id,
                    work.summary AS summary,
                    work.last_error AS last_error,
                    brief.project_root AS project_root,
                    brief.payload_json AS brief_json,
                    (
                        SELECT recovery.state
                        FROM voice_job_recovery AS recovery
                        WHERE recovery.item_id=inbox.item_id
                        ORDER BY recovery.recovery_no DESC LIMIT 1
                    ) AS recovery_state
                FROM voice_inbox AS inbox
                LEFT JOIN voice_work AS work ON work.item_id = inbox.item_id
                LEFT JOIN voice_job_brief AS brief ON brief.item_id = inbox.item_id
                ORDER BY inbox.created_at DESC, inbox.item_id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            payload = entry.pop("brief_json", None)
            brief: dict[str, Any] = {}
            if payload:
                try:
                    decoded = json.loads(str(payload))
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict):
                    brief = decoded
            project_root = str(entry.get("project_root") or brief.get("project_root") or "")
            recovery_active = str(entry.get("recovery_state") or "") in {
                "scheduled",
                "running",
            }
            visible_state = (
                str(entry.get("queue_state") or "")
                if recovery_active
                and str(entry.get("work_state") or "") == "failed"
                and str(entry.get("queue_state") or "") in {"queued", "claimed"}
                else str(entry.get("work_state") or entry.get("queue_state") or "")
            )
            jobs.append(
                {
                    "item_id": str(entry.get("item_id") or ""),
                    "request": str(brief.get("exact_request") or entry.get("request") or ""),
                    "trigger": str(brief.get("triggering_request") or ""),
                    "state": visible_state,
                    "queue_state": str(entry.get("queue_state") or ""),
                    "work_state": str(entry.get("work_state") or ""),
                    "project_root": project_root,
                    "project": Path(project_root).name if project_root else "",
                    "session_id": str(entry.get("session_id") or ""),
                    "summary": str(entry.get("summary") or ""),
                    "last_error": str(entry.get("last_error") or ""),
                    "created_at": float(entry.get("created_at") or 0.0),
                }
            )
        return jobs

    def job_snapshot(self, item_id: str, *, event_limit: int = 100) -> dict[str, Any] | None:
        """Return the durable status surface for CLI, overlay, and recovery."""

        with self._connect() as connection:
            inbox = connection.execute(
                "SELECT * FROM voice_inbox WHERE item_id=?", (str(item_id),)
            ).fetchone()
            if inbox is None:
                return None
            work = connection.execute(
                "SELECT * FROM voice_work WHERE item_id=?", (str(item_id),)
            ).fetchone()
            route = connection.execute(
                "SELECT * FROM voice_job_route WHERE item_id=?", (str(item_id),)
            ).fetchone()
            brief = connection.execute(
                "SELECT payload_json FROM voice_job_brief WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            evidence = connection.execute(
                "SELECT payload_json FROM voice_job_evidence WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            attempts = connection.execute(
                "SELECT * FROM voice_job_attempt WHERE item_id=? ORDER BY attempt_no",
                (str(item_id),),
            ).fetchall()
            controls = connection.execute(
                "SELECT * FROM voice_job_control WHERE item_id=? ORDER BY created_at",
                (str(item_id),),
            ).fetchall()
            reviews = connection.execute(
                "SELECT * FROM voice_job_review WHERE item_id=? ORDER BY created_at",
                (str(item_id),),
            ).fetchall()
            recoveries = connection.execute(
                """
                SELECT * FROM voice_job_recovery
                WHERE item_id=? ORDER BY recovery_no
                """,
                (str(item_id),),
            ).fetchall()
            events = connection.execute(
                """
                SELECT * FROM voice_job_event WHERE item_id=?
                ORDER BY event_id DESC LIMIT ?
                """,
                (str(item_id), max(0, int(event_limit))),
            ).fetchall()

        def decoded(row: sqlite3.Row | None) -> dict[str, Any] | None:
            if row is None:
                return None
            try:
                value = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None

        event_rows: list[dict[str, Any]] = []
        for row in reversed(events):
            entry = dict(row)
            try:
                entry["payload"] = json.loads(str(entry.pop("payload_json")))
            except json.JSONDecodeError:
                entry["payload"] = {}
            event_rows.append(entry)
        review_rows: list[dict[str, Any]] = []
        for row in reviews:
            entry = dict(row)
            try:
                entry["findings"] = json.loads(str(entry.pop("findings_json")))
            except json.JSONDecodeError:
                entry["findings"] = []
            review_rows.append(entry)
        return {
            "item_id": str(item_id),
            "queue": dict(inbox),
            "work": dict(work) if work is not None else None,
            "route": dict(route) if route is not None else None,
            "brief": decoded(brief),
            "attempts": [dict(row) for row in attempts],
            "controls": [dict(row) for row in controls],
            "evidence": decoded(evidence),
            "reviews": review_rows,
            "recoveries": [self._recovery_entry(row) for row in recoveries],
            "events": event_rows,
        }

    def overlay_snapshot(self, item_id: str) -> dict[str, Any] | None:
        """Return a bounded renderer-safe projection of one durable job."""

        snapshot = self.job_snapshot(item_id, event_limit=20)
        if snapshot is None:
            return None
        brief = snapshot.get("brief") or {}
        work = snapshot.get("work") or {}
        route = snapshot.get("route") or {}
        queue = snapshot.get("queue") or {}
        evidence = snapshot.get("evidence") or {}
        attempts = snapshot.get("attempts") or []
        current = attempts[-1] if attempts else {}
        reviews = snapshot.get("reviews") or []
        review = reviews[-1] if reviews else {}
        recoveries = snapshot.get("recoveries") or []
        recovery = recoveries[-1] if recoveries else {}
        tests = [
            {
                "command": str(entry.get("command") or "")[:1_000],
                "exit_code": entry.get("exit_code"),
            }
            for entry in evidence.get("tests") or []
        ]
        proof = [
            {
                "command": str(entry.get("command") or "")[:1_000],
                "exit_code": entry.get("exit_code"),
            }
            for entry in evidence.get("live_proof") or []
        ]
        # Note: the returned dict is fitted to the transport budget below.
        recovery_active = str(recovery.get("state") or "") in {
            "scheduled",
            "running",
        }
        state = str(
            queue.get("state")
            if recovery_active
            and str(work.get("state") or "") == "failed"
            and str(queue.get("state") or "") in {"queued", "claimed"}
            else work.get("state") or queue.get("state") or "queued"
        )
        # Every list here is bounded because the bridge drops an oversized
        # snapshot whole, which would silently blank the panel on exactly the
        # jobs worth watching. Truncate honestly and say by how much.
        all_changes = [str(path)[:400] for path in evidence.get("changed_files") or []]
        changes = all_changes[:MAX_OVERLAY_CHANGES]
        criteria = [str(value)[:1_000] for value in brief.get("acceptance_criteria") or []]
        boundaries = [str(value)[:1_000] for value in brief.get("authority_boundaries") or []]
        context = [str(value)[:1_000] for value in brief.get("relevant_conversation") or []]
        overlapping = [
            str(value)[:1_000] for value in evidence.get("overlapping_initial_dirty") or []
        ]
        session_id = str(work.get("session_id") or "")
        return _fit_overlay_snapshot({
            "item_id": str(item_id),
            "state": state,
            "project": Path(str(brief.get("project_root") or "project")).name,
            "project_root": str(brief.get("project_root") or "")[:2_000],
            "brief": {
                "request": str(brief.get("exact_request") or queue.get("request") or "")[:4_000],
                "trigger": str(brief.get("triggering_request") or "")[:4_000],
                "outcome": str(brief.get("requested_outcome") or "")[:4_000],
                "acceptance_criteria": criteria[:MAX_OVERLAY_LIST],
                "authority_boundaries": boundaries[:MAX_OVERLAY_LIST],
                "context": context[:MAX_OVERLAY_LIST],
            },
            "model": {
                "selection": str(brief.get("coding_model") or "auto"),
                "requested": str(
                    current.get("requested_model")
                    or (
                        brief.get("coding_model")
                        if brief.get("coding_model") not in (None, "", "auto")
                        else brief.get("implement_model") or brief.get("codex_model")
                    )
                    or ""
                ),
                "effort": str(
                    current.get("requested_effort")
                    or brief.get("implement_effort")
                    or brief.get("codex_effort")
                    or ""
                ),
                "reported": str(current.get("reported_model") or ""),
                "reported_effort": str(current.get("reported_effort") or ""),
            },
            "progress": {
                "attempt": int(current.get("attempt_no") or 0),
                "attempt_state": str(current.get("state") or "not_started"),
                "session_id": session_id,
                "route_state": str(route.get("state") or ""),
                "last_error": str(work.get("last_error") or "")[:1_000],
                "recovery": int(recovery.get("recovery_no") or 0),
                "recovery_state": str(recovery.get("state") or ""),
                "recovery_reason": str(
                    recovery.get("terminal_reason")
                    or recovery.get("error")
                    or ""
                )[:1_000],
            },
            "changes": changes,
            "changes_truncated": max(0, len(all_changes) - len(changes)),
            "tests": tests[:MAX_OVERLAY_LIST],
            "live_proof": proof[:MAX_OVERLAY_LIST],
            "evidence": {
                "complete": bool(evidence.get("complete")),
                "errors": [str(value)[:1_000] for value in evidence.get("errors") or []][
                    :MAX_OVERLAY_LIST
                ],
                "commit_state": evidence.get("commit_state") or {},
                "unrelated_dirty_count": len(evidence.get("unrelated_dirty_changes") or []),
                "overlapping_dirty": overlapping[:MAX_OVERLAY_LIST],
            },
            "review": {
                "state": str(review.get("state") or "not_decided"),
                "reason": str(review.get("reason") or "")[:1_000],
                "approved": review.get("approved"),
                "model": str(review.get("reported_model") or review.get("model") or ""),
                "effort": str(review.get("reported_effort") or review.get("effort") or ""),
            },
            "controls": {
                "can_cancel": state in CANCELLABLE_STATES,
                "can_steer": state in CANCELLABLE_STATES,
                "can_resume": state in {"failed", "cancelled"} and bool(session_id),
            },
            "summary": str(work.get("summary") or "")[:2_000],
        })

    def _item(self, row: sqlite3.Row) -> VoiceInboxItem:
        item_id = str(row["item_id"])
        return VoiceInboxItem(
            item_id=item_id,
            request=str(row["request"]),
            call_id=str(row["call_id"]),
            turn_id=str(row["turn_id"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            target_sid=str(row["target_sid"] or ""),
            brief=self.accepted_brief(item_id),
        )

_DEFAULT_STORE: VoiceInboxStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_voice_inbox() -> VoiceInboxStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is not None:
        return _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = VoiceInboxStore()
    return _DEFAULT_STORE
