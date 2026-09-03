from __future__ import annotations

import pytest

from core.artifacts import ArtifactRegistry
from core.operator_workspace import (
    OperatorWorkspaceStore,
    inspect_session,
    steering_capability,
)


def test_prompt_journal_supports_edit_pause_resume_stash_and_dispatch(tmp_path):
    store = OperatorWorkspaceStore(tmp_path / "operator.sqlite3")
    prompt = store.queue_prompt(
        session_id="codex-session-1",
        provider="codex",
        text="first draft",
    )

    edited = store.edit_prompt(prompt.prompt_id, "better prompt")
    assert edited.revision == 2
    assert edited.text == "better prompt"
    assert store.transition(prompt.prompt_id, "pause").state == "paused"
    assert store.transition(prompt.prompt_id, "stash").state == "stashed"
    assert store.transition(prompt.prompt_id, "resume").state == "queued"
    dispatching = store.begin_dispatch(prompt.prompt_id, "terminal-1")
    assert dispatching.state == "dispatching"
    sent = store.finish_dispatch(prompt.prompt_id, delivered=True)
    assert sent.state == "sent"
    assert sent.dispatched_at is not None
    with pytest.raises(RuntimeError, match="sent prompt cannot be edited"):
        store.edit_prompt(prompt.prompt_id, "too late")


def test_prompt_search_escapes_sql_wildcards_and_failed_write_requeues(tmp_path):
    store = OperatorWorkspaceStore(tmp_path / "operator.sqlite3")
    literal = store.queue_prompt(
        session_id="claude-session",
        provider="claude",
        text="literal 100% _ marker",
    )
    store.queue_prompt(
        session_id="claude-other",
        provider="claude",
        text="ordinary prompt",
    )

    assert [item.prompt_id for item in store.list_prompts(query="100% _")] == [
        literal.prompt_id
    ]
    store.begin_dispatch(literal.prompt_id, "terminal-2")
    assert store.finish_dispatch(literal.prompt_id, delivered=False).state == "queued"


def test_mid_turn_correction_is_only_exposed_for_safe_native_codex_state():
    live_codex = {
        "alive": True,
        "state": "live",
        "agent": "codex",
        "busy": True,
        "draft": False,
        "reserved": False,
    }
    assert steering_capability(live_codex, "correction")["supported"] is True

    claude = {**live_codex, "agent": "claude"}
    refused = steering_capability(claude, "correction")
    assert refused["supported"] is False
    assert "no verified safe" in refused["reason"]

    reserved = {**live_codex, "reserved": True}
    assert steering_capability(reserved, "correction")["supported"] is False
    paused_idle = {**live_codex, "state": "paused", "busy": False}
    next_turn = steering_capability(paused_idle, "next_turn")
    assert next_turn == {
        "supported": True,
        "reason": "idle native runtime can accept the next turn",
        "wakes_runtime": True,
    }


def test_inspection_combines_focus_runtime_and_honest_context_breakdown(monkeypatch):
    from core import indexer, metadata
    from ui import pty_terminal

    monkeypatch.setattr(
        indexer,
        "get_session",
        lambda sid: {
            "session_id": sid,
            "agent": "codex",
            "display_title": "operator test",
            "last_cwd": "/tmp/project",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 20,
            "cache_create_tokens": 3,
        },
    )
    monkeypatch.setattr(metadata, "get_meta", lambda _sid: {})
    monkeypatch.setattr(
        pty_terminal,
        "runtime_context_snapshot",
        lambda: {
            "focused_sid": "session-1",
            "split_pair": ["session-1", "session-2"],
            "runtimes": [
                {
                    "sid": "session-1",
                    "terminal_id": "terminal-1",
                    "agent": "codex",
                    "alive": True,
                    "state": "live",
                    "busy": True,
                    "draft": False,
                    "reserved": False,
                }
            ],
        },
    )

    inspection = inspect_session("session-1")

    assert inspection["focus"] == {
        "focused": True,
        "focused_session_id": "session-1",
        "split_pair": ["session-1", "session-2"],
    }
    assert inspection["context_usage"]["observed_tokens"] == 38
    assert inspection["context_usage"]["billable_tokens"] == 18
    assert inspection["context_usage"]["context_window_tokens"] is None
    assert inspection["capabilities"]["correction"]["supported"] is True


def test_artifact_gallery_search_preserves_chat_and_fleet_provenance(tmp_path):
    job_id = "11111111-1111-4111-8111-111111111111"
    registry = ArtifactRegistry(
        root=tmp_path / "artifacts",
        db_path=tmp_path / "artifacts.sqlite3",
        key_path=tmp_path / "artifact.key",
    )
    path = registry.write_job_artifact(
        job_id=job_id, name="review.txt", content="review evidence"
    )
    registered = registry.register(
        job_id=job_id,
        path=path,
        name="review.txt",
        origin_session_id="chat-1",
        fleet_run_id="run-1",
        fleet_worker_key="codex:a",
    )

    by_chat = registry.search("chat-1")
    by_worker = registry.search("codex:a")
    assert by_chat == by_worker == [registered]
    assert registry.search(origin_session_id="other") == []
    payload = registry.read(registered.token)
    assert payload is not None
    assert payload.link.origin_session_id == "chat-1"
    assert payload.link.fleet_worker_key == "codex:a"

    assert registry.attach_provenance(
        registered.artifact_id,
        origin_session_id="chat-2",
    )
    assert registry.search(origin_session_id="chat-1") == []
    assert registry.search(origin_session_id="chat-2")[0].artifact_id == registered.artifact_id


def test_artifact_schema_migrates_existing_registry_without_losing_rows(tmp_path):
    import sqlite3

    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "artifacts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, name TEXT NOT NULL,
                path TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE artifact_receipts (
                nonce TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, job_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, issued_at INTEGER NOT NULL, consumed_at INTEGER
            )
            """
        )
    registry = ArtifactRegistry(
        root=root,
        db_path=database,
        key_path=tmp_path / "key",
    )
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(artifacts)")}
    assert {"origin_session_id", "fleet_run_id", "fleet_worker_key"} <= columns
    assert registry.search() == []


def test_late_voice_origin_link_updates_gallery_provenance(tmp_path):
    from core.work_jobs import WorkJobStore
    from voice.call.tasking import CallTaskDispatcher, DraftResult

    class InlineExecutor:
        def submit(self, function, *args, **kwargs):
            function(*args, **kwargs)
            return object()

    class Runner:
        def run(
            self,
            job,
            *,
            worker_session_id,
            adopt_worker,
            record_billing=None,
        ):
            return DraftResult(content=f"# {job.request}\n", worker_session_id=worker_session_id)

    registry = ArtifactRegistry(
        root=tmp_path / "artifacts",
        db_path=tmp_path / "artifacts.sqlite3",
        key_path=tmp_path / "artifact.key",
    )
    dispatcher = CallTaskDispatcher(
        store=WorkJobStore(tmp_path / "jobs.sqlite3"),
        artifacts=registry,
        runner=Runner(),
        executor=InlineExecutor(),
        recover=False,
    )
    job = dispatcher.submit_if_explicit(
        "draft the gallery note and send me a link",
        call_id="call-1",
        turn_id="call-1:1",
    )
    assert job is not None
    assert registry.search(origin_session_id="chat-late") == []

    dispatcher.link_origin_session(job.job_id, "chat-late")

    linked = registry.search(origin_session_id="chat-late")
    assert len(linked) == 1
    assert linked[0].job_id == job.job_id
