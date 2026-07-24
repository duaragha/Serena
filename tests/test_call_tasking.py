from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from core.artifacts import (
    ArtifactReceiptCapacityError,
    ArtifactRegistry,
    artifact_client_allowed,
)
from core.voice_inbox import VoiceInboxStore
from core.work_jobs import WorkJobStore, process_start_token
from voice.call.task_acceptance import analyze_tasking
from voice.call.tasking import (
    MAX_WORKER_STDOUT_BYTES,
    CallTaskDispatcher,
    DraftResult,
    HeadlessClaudeDraftRunner,
    parse_code_panel_intent,
    parse_draft_link_intent,
    parse_live_work_intent,
)


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future: Future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class FakeDraftRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, job, *, worker_session_id, adopt_worker, record_billing=None):
        self.calls += 1
        assert record_billing is not None
        assert adopt_worker(os.getpid()) is True
        assert record_billing(_good_worker_attestation(os.getpid(), worker_session_id)) is True
        return DraftResult(
            content=f"# Draft\n\n{job.request}\n",
            worker_session_id=worker_session_id,
        )


def _good_worker_attestation(pid: int, worker_session_id: str) -> dict:
    return {
        "schema_version": 3,
        "captured_at": time.time(),
        "ok": True,
        "failures": [],
        "logged_in": True,
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
        "subscription_type": "max",
        "api_key_present": False,
        "metered_auth_env_present": [],
        "auth_mode": "subscription_oauth_guarded",
        "setting_sources": [],
        "gate_pid": pid,
        "worker_token": process_start_token(pid),
        "worker_session_id": worker_session_id,
        "command_sha256": "a" * 64,
        "environment_sha256": "b" * 64,
        "stage": "exec_ready",
        "exec_pid": pid,
        "exec_token": process_start_token(pid),
        "containment": "linux_pdeathsig",
    }


def _registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(
        root=tmp_path / "artifacts",
        db_path=tmp_path / "artifacts.sqlite3",
        key_path=tmp_path / "artifact.key",
    )


@pytest.mark.parametrize(
    "text,expected_request",
    [
        ("draft a launch note and send me a link", "a launch note"),
        (
            "okay, draft the short partner brief and send me a link.",
            "the short partner brief",
        ),
        ("please draft something up and send me a link", "something up"),
    ],
)
def test_explicit_draft_link_parser(text: str, expected_request: str) -> None:
    intent = parse_draft_link_intent(text)
    assert intent is not None
    assert intent.request == expected_request


@pytest.mark.parametrize(
    "text",
    [
        "draft a launch note",
        "can you brainstorm the launch note",
        "send me a link",
        "what if we wrote a launch note and sent a link",
        "don't draft a launch note and send me a link",
        "what happens if you draft a launch note and send me a link?",
        "i did not mean draft a launch note and send me a link",
        "write a launch note and send me a link",
        "put together a launch note and give me the link",
        "draft a release note but do not create it and send me a link",
        "draft a release note, then stop, and send me a link",
        "draft a release note but skip creating it and send me a link",
        "draft a release note but avoid creating it and send me a link",
        "draft a release note but hold off on creating it and send me a link",
        "draft a release note but leave it unwritten and send me a link",
        "draft a release note but no need to create it and send me a link",
    ],
)
def test_task_parser_rejects_non_go_language(text: str) -> None:
    assert parse_draft_link_intent(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix the voice pacing", "fix the voice pacing"),
        ("hey Serena, build the spoken work bridge", "build the spoken work bridge"),
        ("can you please update the desktop loop", "update the desktop loop"),
        ("go ahead and test the live call", "test the live call"),
        ("go ahead and do it", "do it"),
        (
            "send this to my coding session: review the current diff",
            "review the current diff",
        ),
        ("tell Codex to fix the voice glossary", "fix the voice glossary"),
    ],
)
def test_live_work_parser_accepts_direct_build_language(text: str, expected: str) -> None:
    intent = parse_live_work_intent(text)
    assert intent is not None
    assert intent.request == expected


@pytest.mark.parametrize(
    "text",
    [
        "how can we fix the voice pacing?",
        "tell me why the desktop loop is slow",
        "don't update the service",
        "what if we built a voice bridge?",
        "can you explain the current implementation?",
    ],
)
def test_live_work_parser_does_not_queue_questions_or_cancellations(text: str) -> None:
    assert parse_live_work_intent(text) is None


@pytest.mark.parametrize(
    "text,action",
    [
        ("Can you open a coding panel now?", "open"),
        ("hey Serena, show the code terminal", "open"),
        ("hide the coding panel", "hide"),
        ("please close the code window", "hide"),
    ],
)
def test_code_panel_parser_matches_spoken_controls(text: str, action: str) -> None:
    intent = parse_code_panel_intent(text)
    assert intent is not None
    assert intent.action == action


def test_live_work_delivery_carries_recent_voice_context(tmp_path: Path) -> None:
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=os.getpid())
    dispatcher = CallTaskDispatcher(
        store=WorkJobStore(tmp_path / "jobs.sqlite3"),
        voice_inbox=inbox,
        recover=False,
    )

    item = dispatcher.submit_live_work_if_explicit(
        "go ahead and do it",
        call_id="call-context",
        turn_id="call-context:2",
        context=["the response pauses between every sentence"],
    )

    assert item is not None
    claimed = inbox.claim_next("headless-voice-test")
    assert claimed is not None
    assert "the response pauses between every sentence" in claimed.prompt
    assert "Current spoken request: do it" in claimed.prompt


def test_live_work_delivery_keeps_serenas_previous_spoken_answer(tmp_path: Path) -> None:
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=os.getpid())
    dispatcher = CallTaskDispatcher(
        store=WorkJobStore(tmp_path / "jobs.sqlite3"),
        voice_inbox=inbox,
        recover=False,
    )

    item = dispatcher.submit_live_work_if_explicit(
        "do that for me",
        call_id="call-antecedent",
        turn_id="call-antecedent:2",
        context=[
            "Raghav: what is next?",
            "Serena: test whether a spoken command reaches a coding pane.",
        ],
    )

    assert item is not None
    claimed = inbox.claim_next("headless-voice-test")
    assert claimed is not None
    assert "Serena: test whether a spoken command reaches a coding pane." in claimed.prompt


def test_desk_work_queues_private_resident_workspace(tmp_path: Path) -> None:
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=os.getpid())
    dispatcher = CallTaskDispatcher(
        store=WorkJobStore(tmp_path / "jobs.sqlite3"),
        voice_inbox=inbox,
        recover=False,
    )

    item = dispatcher.submit_live_work_if_explicit(
        "fix the voice routing in serena",
        call_id="desk-live-test",
        turn_id="desk-live-test:1",
    )

    assert item is not None
    assert item.state == "queued"
    assert inbox.pending_count() == 1
    claimed = inbox.claim_next("headless-voice-test")
    assert claimed is not None
    assert "fix the voice routing in serena" in claimed.prompt


def test_live_work_does_not_claim_it_started_without_private_runtime(tmp_path: Path) -> None:
    dispatcher = CallTaskDispatcher(
        store=WorkJobStore(tmp_path / "jobs.sqlite3"),
        voice_inbox=VoiceInboxStore(tmp_path / "voice.sqlite3"),
        recover=False,
    )

    with pytest.raises(RuntimeError, match="private coding runtime is not available"):
        dispatcher.submit_live_work_if_explicit(
            "fix the voice routing in serena",
            call_id="desk-offline-test",
            turn_id="desk-offline-test:1",
        )


def test_dispatch_is_idempotent_and_artifact_is_resolvable(tmp_path: Path) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    artifacts = _registry(tmp_path)
    runner = FakeDraftRunner()
    dispatcher = CallTaskDispatcher(
        store=store,
        artifacts=artifacts,
        runner=runner,
        executor=ImmediateExecutor(),
    )

    first = dispatcher.submit_if_explicit(
        "draft a launch note and send me a link",
        call_id="call-1",
        turn_id="call-1:7",
    )
    second = dispatcher.submit_if_explicit(
        "draft a different note and send me a link",
        call_id="call-1",
        turn_id="call-1:7",
    )

    assert first is not None and second is not None
    assert first.job_id == second.job_id
    assert first.state == second.state == "artifact_ready"
    assert first.billing_evidence is not None
    assert first.billing_evidence["auth_mode"] == "subscription_oauth_guarded"
    assert first.billing_evidence["setting_sources"] == []
    assert runner.calls == 1
    events = dispatcher.events_for_call("call-1")
    assert [event.type for event in events] == [
        "job.accepted",
        "job.progress",
        "artifact.ready",
    ]
    assert [event.event_seq for event in events] == sorted(event.event_seq for event in events)
    ready = events[-1].control()
    assert ready["url"].startswith("/artifacts/")
    artifact = artifacts.read(ready["url"].removeprefix("/artifacts/"))
    assert artifact is not None
    assert artifact.data == b"# Draft\n\na launch note\n"


def test_job_events_replay_and_origin_session_link_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    store = WorkJobStore(path)
    job, created = store.create_draft_link(
        call_id="call-replay",
        turn_id="call-replay:2",
        request="a replay note",
    )
    assert created is True
    worker_id = str(uuid.uuid4())
    assert store.mark_running(job.job_id, worker_id) is not None
    assert store.mark_running(job.job_id, str(uuid.uuid4())) is None
    store.link_origin_session(job.job_id, "origin-session")

    reopened = WorkJobStore(path)
    replay = reopened.events_for_call("call-replay", after=1)
    assert [event.type for event in replay] == ["job.progress"]
    restored = reopened.get(job.job_id)
    assert restored is not None
    assert restored.origin_session_id == "origin-session"
    assert restored.worker_session_id == worker_id

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE work_jobs SET worker_pid = ? WHERE job_id = ?",
            (2_147_483_647, job.job_id),
        )
    recovered = WorkJobStore(path).recoverable()
    assert [item.job_id for item in recovered] == [job.job_id]


def test_running_job_is_owned_by_the_live_child_not_the_daemon(tmp_path: Path) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.create_draft_link(
        call_id="call-child",
        turn_id="call-child:1",
        request="a child ownership note",
    )
    worker_id = str(uuid.uuid4())
    assert store.mark_running(job.job_id, worker_id) is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert store.adopt_worker(
            job.job_id,
            worker_session_id=worker_id,
            owner_pid=os.getpid(),
            worker_pid=child.pid,
        )
        owned = store.get(job.job_id)
        assert owned is not None
        assert owned.worker_pid == child.pid
        assert owned.worker_token
        assert store.recoverable() == []
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert [item.job_id for item in store.recoverable()] == [job.job_id]


def test_artifact_capability_rejects_tampering_expiry_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    output = registry.job_directory(job_id) / "draft.md"
    output.write_text("safe draft\n", encoding="utf-8")
    link = registry.register(job_id=job_id, path=output, name="draft.md", ttl_seconds=60)

    assert registry.resolve(link.token) is not None
    assert registry.resolve(link.token[:-1] + ("A" if link.token[-1] != "A" else "B")) is None
    monkeypatch.setattr("core.artifacts.time.time", lambda: link.expires_at + 1)
    assert registry.resolve(link.token) is None

    target = registry.root / "target.md"
    target.write_text("target\n", encoding="utf-8")
    symlink = registry.job_directory(str(uuid.uuid4())) / "linked.md"
    try:
        symlink.symlink_to(target)
    except OSError:
        return
    with pytest.raises(ValueError, match="symlink"):
        registry.register(
            job_id=str(uuid.uuid4()),
            path=symlink,
            name="linked.md",
        )


def test_artifact_fetch_receipt_is_bound_recent_and_unforgeable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    output = registry.write_job_artifact(
        job_id=job_id,
        name="draft.md",
        content="receipt draft\n",
    )
    link = registry.register(job_id=job_id, path=output, name="draft.md")
    receipt = registry.issue_receipt(link)

    assert registry.verify_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    assert not registry.verify_receipt(
        receipt + "x",
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    assert not registry.verify_receipt(
        receipt,
        job_id=str(uuid.uuid4()),
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    assert registry.consume_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    assert not registry.consume_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    monkeypatch.setattr(
        "core.artifacts.time.time",
        lambda: link.expires_at + 1,
    )
    assert not registry.verify_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )


def test_artifact_receipt_history_is_pruned_without_dropping_live_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    output = registry.write_job_artifact(
        job_id=job_id,
        name="draft.md",
        content="receipt cleanup\n",
    )
    link = registry.register(job_id=job_id, path=output, name="draft.md")
    start = int(time.time())
    monkeypatch.setattr("core.artifacts.time.time", lambda: start)
    stale = registry.issue_receipt(link)
    assert registry.consume_receipt(
        stale,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )

    monkeypatch.setattr("core.artifacts.time.time", lambda: start + 301)
    live = registry.issue_receipt(link)
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_receipts").fetchone()[0] == 1
    assert registry.consume_receipt(
        live,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )


def test_artifact_receipt_burst_reuses_one_live_row(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    output = registry.write_job_artifact(
        job_id=job_id,
        name="draft.md",
        content="receipt burst\n",
    )
    link = registry.register(job_id=job_id, path=output, name="draft.md")

    receipts = {registry.issue_receipt(link) for _ in range(1_500)}

    assert len(receipts) == 1
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_receipts").fetchone()[0] == 1
    receipt = receipts.pop()
    assert registry.consume_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_receipts").fetchone()[0] == 0


def test_artifact_receipt_capacity_fails_without_evicting_live_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.artifacts.MAX_ACTIVE_ARTIFACT_RECEIPTS", 1)
    registry = _registry(tmp_path)
    first_job = str(uuid.uuid4())
    first_path = registry.write_job_artifact(
        job_id=first_job,
        name="first.md",
        content="first\n",
    )
    first = registry.register(job_id=first_job, path=first_path, name="first.md")
    receipt = registry.issue_receipt(first)
    second_job = str(uuid.uuid4())
    second_path = registry.write_job_artifact(
        job_id=second_job,
        name="second.md",
        content="second\n",
    )
    second = registry.register(job_id=second_job, path=second_path, name="second.md")

    with pytest.raises(ArtifactReceiptCapacityError):
        registry.issue_receipt(second)
    assert registry.consume_receipt(
        receipt,
        job_id=first_job,
        artifact_id=first.artifact_id,
        sha256=first.sha256,
    )


def test_atomic_artifact_write_replaces_a_precreated_symlink(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    victim = tmp_path / "victim.md"
    victim.write_text("do not touch\n", encoding="utf-8")
    output = registry.job_directory(job_id) / "draft.md"
    output.symlink_to(victim)

    written = registry.write_job_artifact(
        job_id=job_id,
        name="draft.md",
        content="safe draft\n",
    )

    assert victim.read_text(encoding="utf-8") == "do not touch\n"
    assert not written.is_symlink()
    assert written.read_text(encoding="utf-8") == "safe draft\n"


def test_artifact_client_scope_is_loopback_or_tailnet_only() -> None:
    assert artifact_client_allowed("127.0.0.1") is True
    assert artifact_client_allowed("100.100.100.100") is True
    assert artifact_client_allowed("fd7a:115c:a1e0::12") is True
    assert artifact_client_allowed("192.168.1.20") is False
    assert artifact_client_allowed("8.8.8.8") is False


def test_standalone_call_host_serves_only_valid_tailnet_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voice.call import server

    registry = _registry(tmp_path)
    job_id = str(uuid.uuid4())
    output = registry.job_directory(job_id) / "draft.md"
    output.write_text("served draft\n", encoding="utf-8")
    link = registry.register(job_id=job_id, path=output, name="draft.md")
    monkeypatch.setattr(server, "get_default_artifact_registry", lambda: registry)
    original_read = registry.read

    def swap_after_snapshot(token: str):
        payload = original_read(token)
        if payload is not None:
            outside = tmp_path / "outside.md"
            outside.write_text("outside secret\n", encoding="utf-8")
            output.unlink()
            output.symlink_to(outside)
        return payload

    monkeypatch.setattr(registry, "read", swap_after_snapshot)
    client = server.app.test_client()

    response = client.get(link.url, environ_base={"REMOTE_ADDR": "100.80.0.4"})
    assert response.status_code == 200
    assert response.data == b"served draft\n"
    assert response.headers["Cache-Control"] == "private, no-store"
    receipt = response.headers["X-Serena-Artifact-Receipt"]
    assert registry.verify_receipt(
        receipt,
        job_id=job_id,
        artifact_id=link.artifact_id,
        sha256=link.sha256,
    )
    denied = client.get(link.url, environ_base={"REMOTE_ADDR": "192.168.1.4"})
    assert denied.status_code == 404
    tampered = client.get(link.url + "x", environ_base={"REMOTE_ADDR": "100.80.0.4"})
    assert tampered.status_code == 404


def test_headless_runner_has_no_tools_and_scrubs_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.create_draft_link(
        call_id="call-worker",
        turn_id="call-worker:1",
        request="a worker note",
    )
    worker_session_id = str(uuid.uuid4())
    job_cwd = tmp_path / "headless"
    capture_path = tmp_path / "capture.json"
    fake_claude = tmp_path / "claude-fake"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if 'auth' in sys.argv and 'status' in sys.argv:\n"
        " print(json.dumps({'loggedIn':True,'authMethod':'claude.ai',"
        "'apiProvider':'firstParty','subscriptionType':'max'})); raise SystemExit\n"
        "Path(os.environ['TEST_CAPTURE']).write_text(json.dumps({"
        "'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'has_api_key': 'ANTHROPIC_API_KEY' in os.environ, "
        "'has_provider_override': 'CLAUDE_CODE_USE_BEDROCK' in os.environ}))\n"
        "sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        "print(json.dumps({'result': '# worker', 'session_id': sid}))\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o700)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("TEST_CAPTURE", str(capture_path))
    monkeypatch.setattr("voice.call.tasking.HEADLESS_JOB_CWD", job_cwd)
    adopted: list[int] = []
    runner = HeadlessClaudeDraftRunner(claude_bin=str(fake_claude))
    recorded: list[dict] = []
    result = runner.run(
        job,
        worker_session_id=worker_session_id,
        adopt_worker=lambda pid: adopted.append(pid) is None,
        record_billing=lambda evidence: recorded.append(evidence) is None,
    )

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    tools_index = capture["argv"].index("--tools")
    assert capture["argv"][tools_index + 1] == ""
    assert "--safe-mode" not in capture["argv"]
    settings_index = capture["argv"].index("--setting-sources")
    assert capture["argv"][settings_index + 1] == ""
    assert capture["has_api_key"] is False
    assert capture["has_provider_override"] is False
    assert capture["cwd"] == str(job_cwd)
    assert len(adopted) == 1 and adopted[0] != os.getpid()
    assert [item["stage"] for item in recorded] == ["authenticated", "exec_ready"]
    assert recorded[-1]["exec_pid"] != recorded[-1]["gate_pid"]
    assert recorded[-1]["containment"] == "linux_pdeathsig"
    assert result.content == "# worker\n"
    assert result.worker_session_id == worker_session_id

    replay = runner.run(
        replace(job, billing_evidence=recorded[-1]),
        worker_session_id=worker_session_id,
        adopt_worker=lambda _pid: pytest.fail("completed work launched twice"),
    )
    assert replay == result


def test_headless_runner_caps_worker_stdout_before_it_fills_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.create_draft_link(
        call_id="call-bounded",
        turn_id="call-bounded:1",
        request="a bounded worker note",
    )
    worker_session_id = str(uuid.uuid4())
    job_cwd = tmp_path / "headless"
    fake_claude = tmp_path / "claude-oversized"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if 'auth' in sys.argv and 'status' in sys.argv:\n"
        " print(json.dumps({'loggedIn':True,'authMethod':'claude.ai',"
        "'apiProvider':'firstParty','subscriptionType':'max'})); raise SystemExit\n"
        "sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        "print(json.dumps({'result': 'x' * 600000, 'session_id': sid}))\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o700)
    monkeypatch.setattr("voice.call.tasking.HEADLESS_JOB_CWD", job_cwd)
    runner = HeadlessClaudeDraftRunner(claude_bin=str(fake_claude))

    with pytest.raises(RuntimeError, match="exceeded byte limit"):
        runner.run(
            job,
            worker_session_id=worker_session_id,
            adopt_worker=lambda _pid: True,
        )

    result_path, error_path = runner._spool_paths(job, worker_session_id)
    assert result_path.stat().st_size <= MAX_WORKER_STDOUT_BYTES
    assert error_path.stat().st_size < 64 * 1024 + 100
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death gate")
def test_gate_crash_kills_the_contained_claude_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.create_draft_link(
        call_id="call-gate-crash",
        turn_id="call-gate-crash:1",
        request="a containment note",
    )
    worker_session_id = str(uuid.uuid4())
    started = tmp_path / "exec-started"
    fake_claude = tmp_path / "claude-contained"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "if 'auth' in sys.argv and 'status' in sys.argv:\n"
        " print(json.dumps({'loggedIn':True,'authMethod':'claude.ai',"
        "'apiProvider':'firstParty','subscriptionType':'max'})); raise SystemExit\n"
        "Path(os.environ['TEST_STARTED']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o700)
    monkeypatch.setenv("TEST_STARTED", str(started))
    monkeypatch.setattr("voice.call.tasking.HEADLESS_JOB_CWD", tmp_path / "headless")
    runner = HeadlessClaudeDraftRunner(claude_bin=str(fake_claude), timeout=15)
    adopted: list[int] = []
    evidence: list[dict] = []
    exec_ready = threading.Event()
    errors: list[BaseException] = []

    def record(item: dict) -> bool:
        evidence.append(item)
        if item.get("stage") == "exec_ready":
            exec_ready.set()
        return True

    def run() -> None:
        try:
            runner.run(
                job,
                worker_session_id=worker_session_id,
                adopt_worker=lambda pid: adopted.append(pid) is None,
                record_billing=record,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert exec_ready.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    final = evidence[-1]
    gate_pid = adopted[0]
    exec_pid = int(final["exec_pid"])
    assert exec_pid == int(started.read_text())
    os.kill(gate_pid, signal.SIGKILL)
    thread.join(timeout=5)
    deadline = time.monotonic() + 5
    while process_start_token(exec_pid) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert process_start_token(exec_pid) is None
    assert errors


def test_windows_force_stop_uses_taskkill_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    class Process:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("taskkill should handle the Windows tree")

        def kill(self):
            raise AssertionError("taskkill should handle the Windows tree")

    monkeypatch.setattr("voice.call.tasking.os.name", "nt")
    monkeypatch.setattr(
        "voice.call.tasking.subprocess.run",
        lambda command, **kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )

    HeadlessClaudeDraftRunner._stop_gate(Process(), force=True)

    assert commands == [["taskkill", "/PID", "1234", "/T", "/F"]]


def test_daemon_kill_does_not_duplicate_a_surviving_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs_path = tmp_path / "jobs.sqlite3"
    artifact_root = tmp_path / "artifacts"
    artifact_db = tmp_path / "artifacts.sqlite3"
    artifact_key = tmp_path / "artifact.key"
    job_cwd = tmp_path / "headless"
    counter = tmp_path / "worker-starts.txt"
    fake_claude = tmp_path / "slow-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "if 'auth' in sys.argv and 'status' in sys.argv:\n"
        " print(json.dumps({'loggedIn':True,'authMethod':'claude.ai',"
        "'apiProvider':'firstParty','subscriptionType':'max'})); raise SystemExit\n"
        "with Path(os.environ['TEST_COUNTER']).open('a') as handle:\n"
        "    handle.write('start\\n')\n"
        "    handle.flush()\n"
        "time.sleep(1.5)\n"
        "sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        "print(json.dumps({'result': '# survived', 'session_id': sid}))\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o700)
    environment = {
        **os.environ,
        "TEST_COUNTER": str(counter),
        "TEST_JOBS": str(jobs_path),
        "TEST_ARTIFACT_ROOT": str(artifact_root),
        "TEST_ARTIFACT_DB": str(artifact_db),
        "TEST_ARTIFACT_KEY": str(artifact_key),
        "TEST_JOB_CWD": str(job_cwd),
        "TEST_CLAUDE": str(fake_claude),
    }
    launcher = (
        "import os,time; from pathlib import Path; "
        "import voice.call.tasking as t; "
        "from core.artifacts import ArtifactRegistry; "
        "from core.work_jobs import WorkJobStore; "
        "t.HEADLESS_JOB_CWD=Path(os.environ['TEST_JOB_CWD']); "
        "d=t.CallTaskDispatcher("
        "store=WorkJobStore(Path(os.environ['TEST_JOBS'])),"
        "artifacts=ArtifactRegistry(root=Path(os.environ['TEST_ARTIFACT_ROOT']),"
        "db_path=Path(os.environ['TEST_ARTIFACT_DB']),"
        "key_path=Path(os.environ['TEST_ARTIFACT_KEY'])),"
        "runner=t.HeadlessClaudeDraftRunner(claude_bin=os.environ['TEST_CLAUDE']),"
        "recover=False); "
        "d.submit_if_explicit('draft a survival note and send me a link',"
        "call_id='call-survive',turn_id='call-survive:1'); time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", launcher],
        cwd=Path(__file__).parents[1],
        env=environment,
    )
    worker_pid: int | None = None
    try:
        store = WorkJobStore(jobs_path)
        deadline = time.monotonic() + 5
        job = None
        while time.monotonic() < deadline:
            jobs = store.jobs_for_call("call-survive")
            job = jobs[0] if jobs else None
            if (
                job is not None
                and job.worker_pid not in {None, parent.pid}
                and job.billing_evidence is not None
                and counter.exists()
            ):
                worker_pid = job.worker_pid
                break
            time.sleep(0.02)
        assert job is not None and worker_pid is not None

        parent.kill()
        parent.wait(timeout=5)
        monkeypatch.setattr("voice.call.tasking.HEADLESS_JOB_CWD", job_cwd)
        dispatcher = CallTaskDispatcher(
            store=store,
            artifacts=ArtifactRegistry(
                root=artifact_root,
                db_path=artifact_db,
                key_path=artifact_key,
            ),
            runner=HeadlessClaudeDraftRunner(claude_bin=str(fake_claude)),
        )
        deadline = time.monotonic() + 8
        restored = None
        while time.monotonic() < deadline:
            dispatcher.events_for_call("call-survive")
            restored = store.get(job.job_id)
            if restored is not None and restored.state == "artifact_ready":
                break
            time.sleep(0.05)
        assert restored is not None and restored.state == "artifact_ready"
        assert counter.read_text(encoding="utf-8").splitlines() == ["start"]
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if worker_pid is not None:
            with suppress(OSError):
                os.kill(worker_pid, signal.SIGKILL)


def test_task_acceptance_requires_delivery_linkage_and_phone_open(
    tmp_path: Path,
) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    artifacts = _registry(tmp_path)
    dispatcher = CallTaskDispatcher(
        store=store,
        artifacts=artifacts,
        runner=FakeDraftRunner(),
        executor=ImmediateExecutor(),
    )
    job = dispatcher.submit_if_explicit(
        "draft the acceptance note and send me a link",
        call_id="call-accept",
        turn_id="call-accept:1",
    )
    assert job is not None
    dispatcher.link_origin_session(job.job_id, "origin-session")
    restored = store.get(job.job_id)
    assert restored is not None and restored.worker_session_id
    projects = tmp_path / "projects"
    worker_file = projects / "headless" / f"{restored.worker_session_id}.jsonl"
    worker_file.parent.mkdir(parents=True)
    worker_file.write_text("{}\n", encoding="utf-8")
    ready = next(
        event for event in store.events_for_call("call-accept") if event.type == "artifact.ready"
    )
    rows = [
        {
            "call_id": "call-accept",
            "event": "call.start",
            "monotonic_us": 0,
        },
        {
            "call_id": "call-accept",
            "event": "task.accepted",
            "job_id": job.job_id,
            "monotonic_us": 10,
        },
        {
            "call_id": "call-accept",
            "event": "task.event_acknowledged",
            "event_type": "artifact.ready",
            "event_seq": ready.event_seq,
            "monotonic_us": 50,
        },
        {
            "call_id": "call-accept",
            "event": "task.artifact_opened",
            "event_type": "artifact.ready",
            "event_seq": ready.event_seq,
            "job_id": job.job_id,
            "receipt_verified": True,
            "monotonic_us": 75,
        },
        {
            "call_id": "call-accept",
            "event": "call.end",
            "clean_hangup": True,
            "monotonic_us": 100,
        },
    ]

    passed = analyze_tasking(
        rows,
        call_id="call-accept",
        store=store,
        artifacts=artifacts,
        projects_root=projects,
    )
    assert passed["acceptance_claim"] is True
    assert passed["verified_jobs"] == [job.job_id]

    server_send_only = analyze_tasking(
        [
            rows[0],
            rows[1],
            {
                **rows[2],
                "event": "task.event_delivered",
            },
            rows[-1],
        ],
        call_id="call-accept",
        store=store,
        artifacts=artifacts,
        projects_root=projects,
    )
    assert server_send_only["acceptance_claim"] is False
    assert "not acknowledged by the phone" in server_send_only["failures"][0]

    ack_without_open = analyze_tasking(
        [rows[0], rows[1], rows[2], rows[-1]],
        call_id="call-accept",
        store=store,
        artifacts=artifacts,
        projects_root=projects,
    )
    assert ack_without_open["acceptance_claim"] is False
    assert "not opened in-app" in ack_without_open["failures"][0]

    rows[2]["monotonic_us"] = 100
    late = analyze_tasking(
        rows,
        call_id="call-accept",
        store=store,
        artifacts=artifacts,
        projects_root=projects,
    )
    assert late["acceptance_claim"] is False
    assert "not acknowledged by the phone" in late["failures"][0]


def test_task_acceptance_does_not_merge_reused_call_lifecycles(
    tmp_path: Path,
) -> None:
    store = WorkJobStore(tmp_path / "jobs.sqlite3")
    artifacts = _registry(tmp_path)
    dispatcher = CallTaskDispatcher(
        store=store,
        artifacts=artifacts,
        runner=FakeDraftRunner(),
        executor=ImmediateExecutor(),
    )
    job = dispatcher.submit_if_explicit(
        "draft the old note and send me a link",
        call_id="reused-call",
        turn_id="reused-call:1",
    )
    assert job is not None
    dispatcher.link_origin_session(job.job_id, "origin-session")
    restored = store.get(job.job_id)
    assert restored is not None and restored.worker_session_id
    projects = tmp_path / "projects"
    worker_file = projects / f"{restored.worker_session_id}.jsonl"
    worker_file.parent.mkdir(parents=True)
    worker_file.write_text("{}\n", encoding="utf-8")
    ready = next(
        event for event in store.events_for_call("reused-call") if event.type == "artifact.ready"
    )
    rows = [
        {
            "call_id": "reused-call",
            "event": "call.start",
            "monotonic_us": 1,
        },
        {
            "call_id": "reused-call",
            "event": "task.accepted",
            "job_id": job.job_id,
            "monotonic_us": 2,
        },
        {
            "call_id": "reused-call",
            "event": "call.end",
            "clean_hangup": True,
            "monotonic_us": 10,
        },
        {
            "call_id": "reused-call",
            "event": "call.start",
            "monotonic_us": 20,
        },
        {
            "call_id": "reused-call",
            "event": "task.event_acknowledged",
            "event_type": "artifact.ready",
            "event_seq": ready.event_seq,
            "monotonic_us": 21,
        },
        {
            "call_id": "reused-call",
            "event": "task.artifact_opened",
            "event_type": "artifact.ready",
            "event_seq": ready.event_seq,
            "job_id": job.job_id,
            "receipt_verified": True,
            "monotonic_us": 22,
        },
        {
            "call_id": "reused-call",
            "event": "call.end",
            "clean_hangup": True,
            "monotonic_us": 30,
        },
    ]

    report = analyze_tasking(
        rows,
        call_id="reused-call",
        store=store,
        artifacts=artifacts,
        projects_root=projects,
    )

    assert report["acceptance_claim"] is False
    assert report["jobs"] == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        # Natural spoken imperatives that silently never became jobs before:
        # they were not refused, they never reached the inbox at all.
        ("go fix the phev deep link", "fix the phev deep link"),
        ("let's fix the phev deep link", "fix the phev deep link"),
        ("start working on the deep link", "start working on the deep link"),
        ("get started on the konpeki launch", "get started on the konpeki launch"),
        ("keep going on the voice work", "keep going on the voice work"),
        ("look into the trash purge bug", "look into the trash purge bug"),
        ("sort out the deep link", "sort out the deep link"),
        ("take care of the phev notification", "take care of the phev notification"),
        ("clean up the voice stack", "clean up the voice stack"),
        ("tackle the wake word threshold", "tackle the wake word threshold"),
        (
            "jump into the locket repo and fix the macros screen",
            "jump into the locket repo and fix the macros screen",
        ),
        ("alright, continue the voice work", "continue the voice work"),
        # Passive, target-first phrasing.
        ("i need the trash purge bug fixed", "the trash purge bug fixed"),
        ("i need the deep link working", "the deep link working"),
    ],
)
def test_live_work_parser_accepts_natural_spoken_imperatives(
    text: str, expected: str
) -> None:
    intent = parse_live_work_intent(text)
    assert intent is not None
    assert intent.request == expected


@pytest.mark.parametrize(
    "text",
    [
        "what's the status of the phev work",
        "did you fix the deep link",
        "why is the wake word so twitchy",
        "don't fix the deep link",
        "i need to think about the architecture",
        "i need you to know the deep link is broken",
    ],
)
def test_widened_parser_still_refuses_questions_and_cancellations(text: str) -> None:
    assert parse_live_work_intent(text) is None


def test_cancellation_guard_outranks_widened_lead_ins() -> None:
    # "why don't you patch it" contains a cancellation token; the guard wins
    # on purpose. Never weaken the guard to win a phrasing.
    assert parse_live_work_intent("why don't you patch the endpoint") is None


def test_claude_binary_resolves_without_a_login_path(monkeypatch, tmp_path):
    """A boot-time systemd --user PATH must not make the CLI 'not installed'."""
    from core import voice_work_supervisor as vws

    home = tmp_path
    installed = home / ".local" / "bin" / "claude"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)

    monkeypatch.setattr(vws, "HOME", home)
    monkeypatch.setattr(vws.shutil, "which", lambda _name: None)
    assert vws._claude_binary() == str(installed)


def test_claude_binary_still_raises_when_genuinely_absent(monkeypatch, tmp_path):
    from core import voice_work_supervisor as vws

    monkeypatch.setattr(vws, "HOME", tmp_path)
    monkeypatch.setattr(vws.shutil, "which", lambda _name: None)
    monkeypatch.setattr(vws.os, "access", lambda *_a, **_k: False)
    with pytest.raises(FileNotFoundError):
        vws._claude_binary()
