import json
import os
import socket
import sqlite3
import time
from pathlib import Path

from voice.desktop.coding_jobs_query import (
    read_coding_jobs,
    resolve_live_terminal_target,
)

JOB_ID = "b62f3779-9bc6-4d19-b1d9-6384fc4743e9"
OTHER_JOB_ID = "86bc7c5e-0f2f-46fc-9fb6-fe0ddc56d1af"
SESSION_ID = "019fc3b5-2acb-7492-8d76-21f3007f8bdb"


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE voice_inbox (
          item_id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE voice_work (
          item_id TEXT PRIMARY KEY, state TEXT, session_id TEXT, summary TEXT, last_error TEXT
        );
        CREATE TABLE voice_job_brief (
          item_id TEXT PRIMARY KEY, project_root TEXT, payload_json TEXT
        );
        CREATE TABLE voice_job_route (
          item_id TEXT PRIMARY KEY,
          mode TEXT NOT NULL,
          preference TEXT NOT NULL DEFAULT 'auto',
          project_root TEXT NOT NULL DEFAULT '',
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
          updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE voice_job_attempt (
          attempt_id TEXT PRIMARY KEY, item_id TEXT, attempt_no INTEGER, state TEXT,
          provider TEXT, session_id TEXT, started_at REAL,
          requested_model TEXT, requested_effort TEXT, reported_model TEXT,
          reported_effort TEXT
        );
        """
    )
    return connection


def test_read_coding_jobs_keeps_every_active_job_and_recent_history(tmp_path: Path) -> None:
    database = tmp_path / "voice.sqlite3"
    connection = _database(database)
    try:
        for item_id, state, created_at in (
            ("weather-job", "working", 10.0),
            ("coding-ui-job", "working", 20.0),
            ("older-job", "completed", 30.0),
        ):
            connection.execute(
                "INSERT INTO voice_inbox VALUES (?, ?, 'delivered', ?)",
                (item_id, f"request for {item_id}", created_at),
            )
            connection.execute(
                "INSERT INTO voice_work VALUES (?, ?, ?, '', '')",
                (item_id, state, f"session-{item_id}"),
            )
        connection.execute(
            "INSERT INTO voice_job_brief VALUES (?, ?, ?)",
            (
                "coding-ui-job",
                "/tmp/serena",
                json.dumps({"exact_request": "show both coding jobs"}),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    jobs = read_coding_jobs(database, recent_limit=1)

    assert [job["item_id"] for job in jobs] == ["coding-ui-job", "weather-job", "older-job"]
    assert jobs[0]["brief"]["request"] == "show both coding jobs"
    assert [job["state"] for job in jobs[:2]] == ["working", "working"]


def test_read_coding_jobs_does_not_create_or_modify_the_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    assert read_coding_jobs(missing) == []
    assert not missing.exists()

    database = tmp_path / "voice.sqlite3"
    connection = _database(database)
    connection.close()
    before = database.stat().st_mtime_ns

    assert read_coding_jobs(database) == []
    assert database.stat().st_mtime_ns == before


def _safe_terminal_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    database = tmp_path / "voice.sqlite3"
    project = tmp_path / "serena"
    project.mkdir()
    (project / ".git").mkdir()
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    codex_sessions = tmp_path / "codex-sessions"
    rollout_dir = codex_sessions / "2026" / "08" / "12"
    rollout_dir.mkdir(parents=True)
    claude_projects = tmp_path / "claude-projects"
    claude_projects.mkdir()
    transcript = rollout_dir / f"rollout-2026-08-12T00-00-00-{SESSION_ID}.jsonl"
    transcript.write_text('{"type":"thread.started"}\n', encoding="utf-8")
    (metadata_dir / f"{SESSION_ID}.json").write_text(
        json.dumps({"work_project_root": str(project)}), encoding="utf-8"
    )

    connection = _database(database)
    try:
        connection.execute(
            "INSERT INTO voice_inbox VALUES (?, ?, 'delivered', ?)",
            (JOB_ID, "open its live terminal", time.time()),
        )
        connection.execute(
            "INSERT INTO voice_work VALUES (?, 'completed', ?, '', '')",
            (JOB_ID, SESSION_ID),
        )
        connection.execute(
            """
            INSERT INTO voice_job_route(item_id, mode, session_id, state)
            VALUES (?, 'private', ?, 'completed')
            """,
            (JOB_ID, SESSION_ID),
        )
        connection.execute(
            "INSERT INTO voice_job_brief VALUES (?, ?, ?)",
            (JOB_ID, str(project), json.dumps({"exact_request": "open it"})),
        )
        connection.execute(
            """
            INSERT INTO voice_job_attempt(
              attempt_id, item_id, attempt_no, state, provider, session_id, started_at,
              requested_model, requested_effort, reported_model, reported_effort
            ) VALUES ('attempt-1', ?, 1, 'running', 'codex', ?, ?,
                      'gpt-5.6-sol', 'xhigh', '', '')
            """,
            (JOB_ID, SESSION_ID, time.time() - 1),
        )
        connection.commit()
    finally:
        connection.close()
    return database, project, metadata_dir, codex_sessions, claude_projects


def test_live_terminal_requires_and_returns_the_exact_persisted_job_session(
    tmp_path: Path,
) -> None:
    database, project, metadata, codex_sessions, claude_projects = _safe_terminal_fixture(
        tmp_path
    )

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    jobs = read_coding_jobs(
        database,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )

    assert error == ""
    assert target is not None
    assert target["item_id"] == JOB_ID
    assert target["session_id"] == SESSION_ID
    assert target["project_root"] == str(project)
    assert target["provider"] == "codex"
    assert jobs[0]["terminal"] == {
        "can_open": True,
        "session_id": SESSION_ID,
        "provider": "codex",
        "reason": "",
    }


def test_stale_or_mismatched_terminal_metadata_is_not_launchable(tmp_path: Path) -> None:
    database, _project, metadata, codex_sessions, claude_projects = _safe_terminal_fixture(
        tmp_path
    )
    transcript = next(codex_sessions.glob("**/*.jsonl"))
    os.utime(transcript, (1, 1))

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    jobs = read_coding_jobs(
        database,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )

    assert target is None
    assert error == "persisted session transcript is stale"
    assert jobs[0]["terminal"]["can_open"] is False
    assert jobs[0]["terminal"]["reason"] == error


def test_active_or_reused_session_is_never_attachable(tmp_path: Path) -> None:
    database, _project, metadata, codex_sessions, claude_projects = _safe_terminal_fixture(
        tmp_path
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE voice_work SET state='working' WHERE item_id=?", (JOB_ID,))
        connection.commit()
    finally:
        connection.close()

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    assert target is None
    assert error == "background coding session is still active"

    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE voice_work SET state='completed' WHERE item_id=?", (JOB_ID,))
        connection.execute("UPDATE voice_job_route SET mode='reuse' WHERE item_id=?", (JOB_ID,))
        connection.commit()
    finally:
        connection.close()

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    assert target is None
    assert error == "session belongs to an already-open app chat"


def test_session_with_an_external_owner_is_never_attachable(tmp_path: Path) -> None:
    database, _project, metadata, codex_sessions, claude_projects = _safe_terminal_fixture(
        tmp_path
    )
    metadata_path = metadata / f"{SESSION_ID}.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["external_runtime"] = {
        "kind": "voice-work",
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "lease_expires_at": time.time() + 60,
    }
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    assert target is None
    assert error == "persisted session already has an active runtime"


def test_session_identity_is_guarded_across_all_job_records(tmp_path: Path) -> None:
    database, _project, metadata, codex_sessions, claude_projects = _safe_terminal_fixture(
        tmp_path
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO voice_inbox VALUES (?, 'other job', 'delivered', ?)",
            (OTHER_JOB_ID, time.time()),
        )
        connection.execute(
            "INSERT INTO voice_work VALUES (?, 'working', ?, '', '')",
            (OTHER_JOB_ID, SESSION_ID),
        )
        connection.commit()
    finally:
        connection.close()

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    assert target is None
    assert error == "persisted session is active in another coding job"

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE voice_work SET state='completed' WHERE item_id=?", (OTHER_JOB_ID,)
        )
        connection.execute(
            """
            INSERT INTO voice_job_route(item_id, mode, session_id, state)
            VALUES (?, 'reuse', ?, 'completed')
            """,
            (OTHER_JOB_ID, SESSION_ID),
        )
        connection.commit()
    finally:
        connection.close()

    target, error = resolve_live_terminal_target(
        database,
        JOB_ID,
        metadata_dir=metadata,
        codex_sessions_root=codex_sessions,
        claude_projects_root=claude_projects,
    )
    assert target is None
    assert error == "session belongs to an already-open app chat"


def test_clean_extension_bootstraps_the_renderer_without_touching_daemon_paths() -> None:
    desktop = Path(__file__).resolve().parents[1]
    package = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
    main = (desktop / "coding-main.js").read_text(encoding="utf-8")
    preload = (desktop / "coding-preload.js").read_text(encoding="utf-8")
    css = (desktop / "renderer" / "coding-jobs.css").read_text(encoding="utf-8")

    assert package["main"] == "coding-main.js"
    assert "session.defaultSession.setPreloads" in main
    assert "voice.desktop.coding_jobs_query" in main
    assert "contextBridge.exposeInMainWorld('serenaCodingJobs'" in preload
    assert "right: 14px" in css
    assert "left: auto" in css
    assert "padding-right: 118px" in css
