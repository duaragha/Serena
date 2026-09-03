"""Read-only durable coding-job projection for the Electron coding panel."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

DEFAULT_DATABASE = Path.home() / ".local" / "state" / "serena" / "voice_inbox.sqlite3"
ACTIVE_STATES = {"queued", "claimed", "delivered", "resume_queued", "working"}
CONTROLLABLE_STATES = ACTIVE_STATES
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _default_metadata_dir() -> Path:
    return Path.home() / ".claude" / "projects" / ".chats-meta"


def _default_codex_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _default_claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _database_path(value: str = "") -> Path:
    configured = value or os.environ.get("SERENA_VOICE_INBOX_PATH", "")
    return Path(configured).expanduser() if configured else DEFAULT_DATABASE


def _brief(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _session_file(
    provider: str,
    session_id: str,
    *,
    codex_sessions_root: Path,
    claude_projects_root: Path,
) -> Path | None:
    if provider == "codex":
        root = codex_sessions_root.expanduser().resolve()
        matches = list(root.glob(f"**/rollout-*-{session_id}.jsonl")) if root.is_dir() else []
    elif provider == "claude":
        root = claude_projects_root.expanduser().resolve()
        matches = list(root.glob(f"**/{session_id}.jsonl")) if root.is_dir() else []
    else:
        return None
    safe: list[Path] = []
    for candidate in matches:
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_file() and resolved.is_relative_to(root):
                safe.append(resolved)
        except OSError:
            continue
    return max(safe, key=lambda path: path.stat().st_mtime_ns) if safe else None


def _terminal_target_from_values(
    *,
    item_id: object,
    state: object,
    session_id: object,
    project_root: object,
    provider: object,
    route_mode: object,
    session_has_active_job: object,
    session_has_reuse_route: object,
    attempt_session_id: object,
    attempt_started_at: object,
    metadata_dir: Path,
    codex_sessions_root: Path,
    claude_projects_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    item = str(item_id or "").strip().lower()
    session = str(session_id or "").strip().lower()
    attempt_session = str(attempt_session_id or "").strip().lower()
    actual_provider = str(provider or "").strip().lower()
    actual_route_mode = str(route_mode or "").strip().lower()
    if not UUID_RE.fullmatch(item):
        return None, "job id is not an exact persisted id"
    if not UUID_RE.fullmatch(session) or session != attempt_session:
        return None, "job session metadata is incomplete or stale"
    if actual_provider not in {"codex", "claude"}:
        return None, "job session provider is not supported"
    if str(state or "").strip().lower() != "completed":
        if str(state or "").strip().lower() in ACTIVE_STATES:
            return None, "background coding session is still active"
        return None, "only completed coding jobs can be opened interactively"
    if actual_route_mode != "private":
        if actual_route_mode == "reuse":
            return None, "session belongs to an already-open app chat"
        return None, "job session route metadata is incomplete"
    if bool(session_has_active_job):
        return None, "persisted session is active in another coding job"
    if bool(session_has_reuse_route):
        return None, "session belongs to an already-open app chat"

    configured_root = Path(str(project_root or "")).expanduser()
    try:
        canonical_root = configured_root.resolve(strict=True)
    except OSError:
        return None, "job project no longer exists"
    if (
        not configured_root.is_absolute()
        or str(canonical_root) != str(configured_root)
        or not canonical_root.is_dir()
        or not (canonical_root / ".git").exists()
    ):
        return None, "job project metadata is unsafe or stale"

    metadata_path = metadata_dir.expanduser().resolve() / f"{session}.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "session project binding is missing"
    if not isinstance(metadata, dict):
        return None, "session project binding is invalid"
    bound_root = str(metadata.get("work_project_root") or "").strip()
    if bound_root != str(canonical_root):
        return None, "session is bound to a different project"
    runtime = metadata.get("external_runtime")
    if isinstance(runtime, dict):
        try:
            runtime_pid = int(runtime.get("pid") or 0)
        except (TypeError, ValueError):
            runtime_pid = 0
        runtime_active = False
        if runtime_pid > 0 and runtime.get("host") == socket.gethostname():
            try:
                os.kill(runtime_pid, 0)
                runtime_active = True
            except PermissionError:
                runtime_active = True
            except (ProcessLookupError, OSError):
                pass
        elif runtime_pid > 0:
            with suppress(TypeError, ValueError):
                runtime_active = float(runtime.get("lease_expires_at") or 0) > time.time()
        if runtime_active:
            return None, "persisted session already has an active runtime"

    transcript = _session_file(
        actual_provider,
        session,
        codex_sessions_root=codex_sessions_root,
        claude_projects_root=claude_projects_root,
    )
    if transcript is None:
        return None, "persisted session transcript is missing"
    try:
        started_at = float(attempt_started_at or 0.0)
        modified_at = transcript.stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None, "persisted session transcript is unreadable"
    if started_at > 0 and modified_at + 5.0 < started_at:
        return None, "persisted session transcript is stale"

    return (
        {
            "item_id": item,
            "state": str(state or ""),
            "session_id": session,
            "provider": actual_provider,
            "project_root": str(canonical_root),
            "transcript": str(transcript),
        },
        "",
    )


def resolve_live_terminal_target(
    database: Path,
    item_id: str,
    *,
    metadata_dir: Path | None = None,
    codex_sessions_root: Path | None = None,
    claude_projects_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve one job to its exact persisted transcript without any writes."""

    item = str(item_id or "").strip().lower()
    if not UUID_RE.fullmatch(item):
        return None, "job id is invalid"
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        return None, "coding job store is unavailable"
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT work.state,
                   work.session_id,
                   brief.project_root,
                   route.mode AS route_mode,
                   EXISTS(
                     SELECT 1
                     FROM voice_work AS active_work
                     WHERE active_work.session_id = work.session_id
                       AND active_work.item_id != work.item_id
                       AND active_work.state IN (
                         'queued', 'claimed', 'delivered', 'resume_queued', 'working'
                       )
                   ) AS session_has_active_job,
                   EXISTS(
                     SELECT 1
                     FROM voice_job_route AS reused_route
                     WHERE reused_route.session_id = work.session_id
                       AND reused_route.mode = 'reuse'
                   ) AS session_has_reuse_route,
                   attempt.provider,
                   attempt.session_id AS attempt_session_id,
                   attempt.started_at AS attempt_started_at
            FROM voice_work AS work
            JOIN voice_job_brief AS brief ON brief.item_id = work.item_id
            LEFT JOIN voice_job_route AS route ON route.item_id = work.item_id
            LEFT JOIN voice_job_attempt AS attempt
              ON attempt.attempt_id = (
                SELECT bound.attempt_id
                FROM voice_job_attempt AS bound
                WHERE bound.item_id = work.item_id
                  AND bound.session_id = work.session_id
                ORDER BY bound.attempt_no DESC
                LIMIT 1
              )
            WHERE work.item_id = ?
            """,
            (item,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, "coding job metadata is incomplete"
    finally:
        connection.close()
    if row is None:
        return None, "coding job no longer exists"
    return _terminal_target_from_values(
        item_id=item,
        state=row["state"],
        session_id=row["session_id"],
        project_root=row["project_root"],
        provider=row["provider"],
        route_mode=row["route_mode"],
        session_has_active_job=row["session_has_active_job"],
        session_has_reuse_route=row["session_has_reuse_route"],
        attempt_session_id=row["attempt_session_id"],
        attempt_started_at=row["attempt_started_at"],
        metadata_dir=metadata_dir or _default_metadata_dir(),
        codex_sessions_root=codex_sessions_root or _default_codex_sessions_root(),
        claude_projects_root=claude_projects_root or _default_claude_projects_root(),
    )


def read_coding_jobs(
    database: Path,
    *,
    recent_limit: int = 20,
    metadata_dir: Path | None = None,
    codex_sessions_root: Path | None = None,
    claude_projects_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every active job plus the newest bounded history without writes."""

    database = Path(database).expanduser().resolve()
    if not database.is_file():
        return []
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                inbox.item_id,
                inbox.request,
                inbox.state AS queue_state,
                inbox.created_at,
                work.state AS work_state,
                work.session_id,
                work.summary,
                work.last_error,
                brief.project_root,
                brief.payload_json AS brief_json,
                route.state AS route_state,
                route.mode AS route_mode,
                EXISTS(
                  SELECT 1
                  FROM voice_work AS active_work
                  WHERE active_work.session_id = work.session_id
                    AND active_work.item_id != work.item_id
                    AND active_work.state IN (
                      'queued', 'claimed', 'delivered', 'resume_queued', 'working'
                    )
                ) AS session_has_active_job,
                EXISTS(
                  SELECT 1
                  FROM voice_job_route AS reused_route
                  WHERE reused_route.session_id = work.session_id
                    AND reused_route.mode = 'reuse'
                ) AS session_has_reuse_route,
                attempt.attempt_no,
                attempt.state AS attempt_state,
                attempt.requested_model,
                attempt.requested_effort,
                attempt.reported_model,
                attempt.reported_effort,
                session_attempt.provider AS session_provider,
                session_attempt.session_id AS session_attempt_id,
                session_attempt.started_at AS session_attempt_started_at
            FROM voice_inbox AS inbox
            LEFT JOIN voice_work AS work ON work.item_id = inbox.item_id
            LEFT JOIN voice_job_brief AS brief ON brief.item_id = inbox.item_id
            LEFT JOIN voice_job_route AS route ON route.item_id = inbox.item_id
            LEFT JOIN voice_job_attempt AS attempt
              ON attempt.attempt_id = (
                SELECT latest.attempt_id
                FROM voice_job_attempt AS latest
                WHERE latest.item_id = inbox.item_id
                ORDER BY latest.attempt_no DESC
                LIMIT 1
              )
            LEFT JOIN voice_job_attempt AS session_attempt
              ON session_attempt.attempt_id = (
                SELECT bound.attempt_id
                FROM voice_job_attempt AS bound
                WHERE bound.item_id = inbox.item_id
                  AND bound.session_id = work.session_id
                ORDER BY bound.attempt_no DESC
                LIMIT 1
              )
            WHERE COALESCE(work.state, inbox.state) IN ('queued', 'claimed', 'delivered',
                                                       'resume_queued', 'working')
               OR inbox.item_id IN (
                    SELECT recent.item_id
                    FROM voice_inbox AS recent
                    ORDER BY recent.created_at DESC, recent.item_id DESC
                    LIMIT ?
               )
            """,
            (max(1, int(recent_limit)),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()

    jobs: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        brief = _brief(entry.get("brief_json"))
        state = str(entry.get("work_state") or entry.get("queue_state") or "queued")
        project_root = str(entry.get("project_root") or brief.get("project_root") or "")
        session_id = str(entry.get("session_id") or "")
        terminal_target, terminal_error = _terminal_target_from_values(
            item_id=entry.get("item_id"),
            state=state,
            session_id=session_id,
            project_root=project_root,
            provider=entry.get("session_provider"),
            route_mode=entry.get("route_mode"),
            session_has_active_job=entry.get("session_has_active_job"),
            session_has_reuse_route=entry.get("session_has_reuse_route"),
            attempt_session_id=entry.get("session_attempt_id"),
            attempt_started_at=entry.get("session_attempt_started_at"),
            metadata_dir=metadata_dir or _default_metadata_dir(),
            codex_sessions_root=codex_sessions_root or _default_codex_sessions_root(),
            claude_projects_root=claude_projects_root or _default_claude_projects_root(),
        )
        jobs.append(
            {
                "item_id": str(entry.get("item_id") or ""),
                "state": state,
                "created_at": float(entry.get("created_at") or 0.0),
                "project": Path(project_root).name if project_root else "project",
                "project_root": project_root,
                "brief": {
                    "request": str(
                        brief.get("exact_request") or entry.get("request") or ""
                    )[:4000],
                    "trigger": str(brief.get("triggering_request") or "")[:4000],
                },
                "model": {
                    "selection": str(brief.get("coding_model") or "auto"),
                    "requested": str(
                        entry.get("requested_model")
                        or (
                            brief.get("coding_model")
                            if brief.get("coding_model") not in (None, "", "auto")
                            else brief.get("implement_model") or brief.get("codex_model")
                        )
                        or ""
                    ),
                    "effort": str(
                        entry.get("requested_effort")
                        or brief.get("implement_effort")
                        or brief.get("codex_effort")
                        or ""
                    ),
                    "reported": str(entry.get("reported_model") or ""),
                    "reported_effort": str(entry.get("reported_effort") or ""),
                },
                "progress": {
                    "attempt": int(entry.get("attempt_no") or 0),
                    "attempt_state": str(entry.get("attempt_state") or "not_started"),
                    "session_id": session_id,
                    "route_state": str(entry.get("route_state") or ""),
                    "last_error": str(entry.get("last_error") or "")[:1000],
                },
                "changes": [],
                "tests": [],
                "live_proof": [],
                "evidence": {"complete": False, "errors": []},
                "review": {"state": "not_decided"},
                "controls": {
                    "can_cancel": state in CONTROLLABLE_STATES,
                    "can_steer": state in CONTROLLABLE_STATES,
                    "can_resume": state in {"failed", "cancelled"} and bool(session_id),
                },
                "terminal": {
                    "can_open": terminal_target is not None,
                    "session_id": str((terminal_target or {}).get("session_id") or ""),
                    "provider": str((terminal_target or {}).get("provider") or ""),
                    "reason": terminal_error,
                },
                "summary": str(entry.get("summary") or "")[:2000],
            }
        )

    return sorted(
        jobs,
        key=lambda job: (
            0 if job["state"] in ACTIVE_STATES else 1,
            -float(job.get("created_at") or 0.0),
            str(job.get("item_id") or ""),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--terminal-target", default="")
    args = parser.parse_args()
    if args.terminal_target:
        target, error = resolve_live_terminal_target(
            _database_path(args.database), args.terminal_target
        )
        print(
            json.dumps(
                {"ok": target is not None, "target": target, "error": error},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    jobs = read_coding_jobs(_database_path(args.database), recent_limit=args.limit)
    print(json.dumps(jobs, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
