"""Mailbox authority, bounded autonomous recovery and verified learning contracts."""

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet.collaboration import PeerStore, worker_key
from fleet.learning import FleetLearning
from fleet.peer_runtime import PeerCoordinator
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore
from fleet.workers import WorkerRequest, WorkerResult, _worker_environment, worker_command


@pytest.fixture
def team(tmp_path, monkeypatch):
    from fleet import supervisor

    monkeypatch.setattr(
        supervisor,
        "_read_start_capacity",
        lambda: {"codex": {"usable": True}, "claude": {"usable": True}},
    )
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "none")
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = store.create_run(
        task="update rules.txt in two independent lanes",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=build_policy("coding", config=builtin_config(), worker_count=2).to_dict(),
    )
    peers = PeerStore(store)
    legs = run["phases"][0]["legs"]
    attempts = [store.begin_attempt(leg["leg_id"]) for leg in legs]
    tokens = [
        peers.issue(run["run_id"], leg, a["attempt_id"])
        for leg, a in zip(legs, attempts, strict=True)
    ]
    return store, run, peers, legs, attempts, tokens


def test_delivery_ack_dedupe_and_generation_fence(team):
    store, run, peers, legs, attempts, tokens = team
    message = peers.send(
        tokens[0], worker_key(legs[1]), "Use the exact parser contract", dedupe="finding-1"
    )
    assert (
        peers.send(tokens[0], worker_key(legs[1]), "duplicate", dedupe="finding-1")["id"]
        == message["id"]
    )
    assert len(peers.inbox(tokens[1])["messages"]) == 1
    assert len(peers.inbox(tokens[1])["messages"]) == 1  # delivery is not consumption
    assert peers.inbox(tokens[1], acknowledge=[message["id"]])["messages"] == []
    store.finish_attempt(attempts[0]["attempt_id"], state="failed", error="stopped")
    with pytest.raises(PermissionError):
        peers.send(tokens[0], worker_key(legs[1]), "stale", dedupe="stale")
    assert PeerStore(FleetStore(store.path)).projection(run["run_id"])["messages"][0][
        "acknowledged"
    ]


def test_cross_run_spoof_reply_and_message_limits(team):
    _store, _run, peers, legs, _attempts, tokens = team
    with pytest.raises(ValueError):
        peers.send(tokens[0], "foreign-worker", "hello", dedupe="foreign")
    with pytest.raises(PermissionError):
        peers.send("invalid", worker_key(legs[1]), "hello", dedupe="invalid")
    with pytest.raises(ValueError):
        peers.send(tokens[0], worker_key(legs[1]), "x" * 3001, dedupe="large")
    original = peers.send(tokens[0], worker_key(legs[1]), "hello", dedupe="first")
    with pytest.raises(PermissionError):
        peers.send(tokens[0], worker_key(legs[1]), "spoof", dedupe="spoof", reply_to=original["id"])
    for index in range(4):
        sender = 1 if index % 2 == 0 else 0
        original = peers.send(
            tokens[sender],
            worker_key(legs[1 - sender]),
            "reply",
            dedupe=f"hop-{index}",
            reply_to=original["id"],
        )
    with pytest.raises(ValueError, match="hop budget"):
        peers.send(tokens[1], worker_key(legs[0]), "loop", dedupe="loop", reply_to=original["id"])


def test_help_dedupe_no_recursion_and_cancellation(team):
    store, run, peers, legs, _attempts, tokens = team
    job = peers.request_help(
        tokens[0], worker_key(legs[1]), "How should I fix the failing parser?", dedupe="parser"
    )
    assert (
        peers.request_help(tokens[0], worker_key(legs[1]), "same", dedupe="parser")["id"]
        == job["id"]
    )
    with pytest.raises(ValueError, match="outstanding"):
        peers.request_help(tokens[0], worker_key(legs[1]), "another", dedupe="another")
    with store._connect() as db:
        db.execute("UPDATE fleet_peer_help SET state='running' WHERE id=?", (job["id"],))
    helper_token = peers.issue(run["run_id"], legs[1], job["id"], help_id=job["id"])
    with pytest.raises(PermissionError):
        peers.request_help(helper_token, worker_key(legs[0]), "recurse", dedupe="recurse")
    with pytest.raises(PermissionError):
        peers.send(helper_token, worker_key(legs[0]), "unassigned message", dedupe="unassigned")
    store.request_cancel(run["run_id"])
    with pytest.raises(PermissionError):
        peers.inbox(helper_token)


def drain(coordinator):
    deadline = time.monotonic() + 5
    while coordinator.pump():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_unattended_failed_worker_gets_peer_reply_and_exactly_one_retry(team):
    store, run, peers, legs, attempts, _tokens = team
    store.finish_attempt(attempts[0]["attempt_id"], state="failed", error="AssertionError")
    assert peers.failure_help(run, legs[0], attempts[0], "AssertionError: parse empty input")
    calls = []

    def worker(request, *, cancel_requested, on_event):
        calls.append(request)
        assert request.access_mode == "read" and request.resume_session_id is None
        assert not cancel_requested()
        inbox = peers.inbox(request.peer_token)
        assert inbox["messages"][0]["kind"] == "help"
        return WorkerResult(
            True,
            "Check empty input before indexing the first character.",
            "helper-session",
            request.model,
            request.effort,
            0,
        )

    with PeerCoordinator(store, run["run_id"], runner=worker) as coordinator:
        drain(coordinator)
    changed = store.get_run(run["run_id"])
    assert changed["phases"][0]["legs"][0]["state"] == "queued"
    assert changed["phases"][0]["legs"][1]["state"] == "running"
    assert len(calls) == 1
    projection = peers.projection(run["run_id"])
    assert projection["help"][0]["retry_applied"] == 1
    assert projection["messages"][-1]["kind"] == "reply"
    assert not peers.failure_help(run, legs[0], attempts[0], "same error")
    with PeerCoordinator(store, run["run_id"], runner=worker) as recovered:
        drain(recovered)
    assert len(calls) == 1


def test_active_owner_help_does_not_restart_owner_and_helper_failure_is_bounded(team):
    store, run, peers, legs, _attempts, tokens = team
    peers.request_help(tokens[0], worker_key(legs[1]), "Need a scoped diagnosis", dedupe="blocker")

    def broken(request, **_kwargs):
        return WorkerResult(False, "", None, request.model, request.effort, 1, "provider failure")

    with PeerCoordinator(store, run["run_id"], runner=broken) as coordinator:
        drain(coordinator)
    assert peers.projection(run["run_id"])["help"][0]["state"] == "failed"
    assert store.get_run(run["run_id"])["phases"][0]["legs"][0]["state"] == "running"


def test_expired_help_does_not_launch_or_retry(team):
    store, run, peers, legs, _attempts, tokens = team
    peers.request_help(tokens[0], worker_key(legs[1]), "Need diagnosis", dedupe="deadline")
    with store._connect() as db:
        db.execute("UPDATE fleet_peer_help SET deadline=0")
    with PeerCoordinator(
        store, run["run_id"], runner=lambda *_a, **_k: pytest.fail("must not launch")
    ) as coordinator:
        drain(coordinator)
    assert peers.projection(run["run_id"])["help"][0]["state"] == "expired"


def test_capability_not_in_command_or_repr_and_mcp_is_available_to_both_providers(team):
    store, run, peers, legs, attempts, tokens = team
    request = WorkerRequest(
        run["run_id"],
        legs[0]["leg_id"],
        attempts[0]["attempt_id"],
        "task",
        "coding",
        "execute",
        "coder",
        "codex",
        "gpt-6-astra",
        "medium",
        "write",
        run["cwd"],
        "prompt",
        peer_token=tokens[0],
        fleet_db_path=str(store.path),
    )
    for provider in ("codex", "claude"):
        changed = replace(request, provider=provider)
        command = worker_command(changed)
        assert tokens[0] not in repr(command) and tokens[0] not in repr(changed)
        assert "serena_peer" in str(command)
        assert _worker_environment(changed)["SERENA_FLEET_PEER_TOKEN"] == tokens[0]
        if provider == "claude":
            assert "--safe-mode" not in command
            assert "--strict-mcp-config" in command


def test_lesson_requires_independent_review_success_and_real_gate(team, tmp_path):
    store, run, peers, legs, attempts, tokens = team
    (tmp_path / "rules.txt").write_text("empty input returns an empty list\n")
    learning = FleetLearning(store)
    lesson = learning.propose(
        peers.identity(tokens[0]),
        "Empty input returns an empty list; never index before checking length.",
        ["rules.txt"],
    )
    with pytest.raises(PermissionError):
        learning.review(
            peers.identity(tokens[1]), lesson["lesson_id"], True, "Read the evidence file"
        )
    reviewer = run["phases"][2]["legs"][1]
    review_attempt = store.begin_attempt(reviewer["leg_id"])
    review_token = peers.issue(run["run_id"], reviewer, review_attempt["attempt_id"])
    learning.review(
        peers.identity(review_token),
        lesson["lesson_id"],
        True,
        "Independently read rules.txt and confirmed the invariant.",
    )
    store.finish_attempt(attempts[0]["attempt_id"], state="completed", output_text="done")
    store.finish_attempt(review_attempt["attempt_id"], state="completed", output_text="reviewed")
    terminal = {**store.get_run(run["run_id"]), "state": "completed"}
    learning.finish(terminal)
    assert learning.projection(run["run_id"])["candidates"][0]["state"] == "endorsed"
    store.append_event(
        run["run_id"], "worker.integration.accepted", {"test_gate": {"ran": True, "ok": True}}
    )
    learning.finish(terminal)
    assert learning.projection(run["run_id"])["candidates"][0]["state"] == "verified"
    assert len(learning.retrieve(run, "next-attempt")) == 1
    (tmp_path / "rules.txt").write_text("changed contract\n")
    assert learning.retrieve(run, "changed-attempt") == []
    learning.rollback(lesson["lesson_id"], "contract changed")
    assert learning.projection(run["run_id"])["candidates"][0]["state"] == "revoked"


def test_lesson_path_escape_and_unreviewed_text_never_reused(team, tmp_path):
    store, run, peers, _legs, _attempts, tokens = team
    learning = FleetLearning(store)
    with pytest.raises(ValueError):
        learning.propose(
            peers.identity(tokens[0]), "A bogus lesson trying to leave the project", ["../secret"]
        )
    (tmp_path / "rules.txt").write_text("evidence")
    learning.propose(
        peers.identity(tokens[0]),
        "A candidate alone is not reliable evidence for future fleets",
        ["rules.txt"],
    )
    assert learning.retrieve(run, "unused") == []


@pytest.mark.parametrize("repair", [True, False])
def test_supervisor_recovers_through_peer_advice_with_real_git_and_test_gate(
    fleet_env, monkeypatch, repair  # noqa: F811
):
    from fleet import peer_runtime, supervisor

    root = fleet_env / "autonomous-repo"
    root.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True)
    (root / "value.txt").write_text("good\n")
    (root / "test_value.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'good\\n'\n"
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    monkeypatch.delenv("SERENA_FLEET_ISOLATION", raising=False)
    monkeypatch.setenv("SERENA_FLEET_WORKSPACE_ROOT", str(fleet_env / "worktrees"))
    monkeypatch.setenv("SERENA_FLEET_INTEGRATION_TEST_COMMAND", f"{sys.executable} test_value.py")
    calls = []

    def worker(request, *, cancel_requested, on_event):
        calls.append((request.role, request.phase, request.worker_key))
        if request.role == "peer-consultant":
            return WorkerResult(
                True,
                "Restore the exact good newline-terminated value; the assertion specifies it.",
                "peer-session",
                request.model,
                request.effort,
                0,
            )
        if request.phase == "execute" and request.worker_key == "agent:a":
            advice = "Restore the exact good" in request.prompt
            (Path(request.cwd) / "value.txt").write_text("good\n" if advice and repair else "bad\n")
        return WorkerResult(True, "done", None, request.model, request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    monkeypatch.setattr(peer_runtime, "run_worker", worker)
    run = supervisor.start_run(
        "Workstreams:\n1. Repair malformed input.\n2. Independently inspect the contract.",
        activity="coding",
        worker_count=2,
        cwd=str(root),
    )
    outcome = supervisor.run_supervisor(run["run_id"])
    assert outcome["state"] == ("completed" if repair else "failed"), outcome.get("error")
    state = PeerStore(FleetStore()).projection(run["run_id"])
    assert any(job["auto_retry"] and job["retry_applied"] for job in state["help"]), (calls, state)
    assert (root / "value.txt").read_text() == "good\n"
    assert len([call for call in calls if call[0] == "peer-consultant"]) == 1
    assert len([call for call in calls if call[1:] == ("execute", "agent:a")]) == (
        2 if repair else 3
    )
    subprocess.run([sys.executable, "test_value.py"], cwd=root, check=True)


def test_restart_recovers_lost_helper_generation_once(team):
    store, run, peers, legs, _attempts, tokens = team
    job = peers.request_help(
        tokens[0], worker_key(legs[1]), "Need bounded restart recovery", dedupe="restart"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE fleet_peer_help SET state='running', dispatches=1 WHERE id=?", (job["id"],)
        )
    old_token = peers.issue(run["run_id"], legs[1], job["id"], help_id=job["id"])

    def worker(request, **_kwargs):
        with pytest.raises(PermissionError):
            peers.inbox(old_token)
        return WorkerResult(True, "Recovered diagnosis", None, request.model, request.effort, 0)

    with PeerCoordinator(store, run["run_id"], runner=worker) as coordinator:
        drain(coordinator)
    assert peers.projection(run["run_id"])["help"][0]["dispatches"] == 2


def test_run_delete_cascades_mailboxes_and_includes_helper_sessions(team):
    store, run, peers, legs, _attempts, tokens = team
    job = peers.request_help(tokens[0], worker_key(legs[1]), "Need advice", dedupe="delete")
    with store._connect() as db:
        db.execute(
            "UPDATE fleet_peer_help SET session_id='helper-owned-session' WHERE id=?", (job["id"],)
        )
    store.cancel_run(run["run_id"])
    deleted = store.delete_run(run["run_id"])
    assert "helper-owned-session" in deleted["session_ids"]
    assert peers.projection(run["run_id"])["messages"] == []


def test_concurrent_resends_persist_once_and_message_budget_is_atomic(team):
    _store, run, peers, legs, _attempts, tokens = team

    def send(_index):
        return peers.send(tokens[0], worker_key(legs[1]), "same delivery", dedupe="concurrent")[
            "id"
        ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(send, range(12)))
    assert len(set(ids)) == 1
    for index in range(95):
        peers.send(tokens[0], worker_key(legs[1]), "bounded finding", dedupe=f"budget-{index}")
    with pytest.raises(ValueError, match="message budget"):
        peers.send(tokens[0], worker_key(legs[1]), "too many", dedupe="overflow")
    assert len(peers.projection(run["run_id"])["messages"]) == 96


def test_held_out_lesson_retrieval_is_project_hash_and_time_scoped(team, tmp_path):
    store, run, peers, _legs, _attempts, tokens = team
    (tmp_path / "rules.txt").write_text("A stable project invariant.")
    learning = FleetLearning(store)
    lesson = learning.propose(
        peers.identity(tokens[0]),
        "Read the stable rules.txt contract before implementing parsing.",
        ["rules.txt"],
    )
    # Seed a verified fixture; the separate promotion test exercises the actual gate.
    with store._connect() as db:
        db.execute(
            "UPDATE fleet_lessons SET state='verified', promoted=? WHERE id=?",
            (time.time(), lesson["lesson_id"]),
        )
    held_out = store.create_run(
        task="add separate validation for rules.txt",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=build_policy("coding", config=builtin_config(), worker_count=2).to_dict(),
    )
    assert learning.retrieve(held_out, "held-out-attempt")[0]["id"] == lesson["lesson_id"]
    assert learning.retrieve({**held_out, "cwd": str(tmp_path / "other-project")}, "foreign") == []
    assert (
        learning.retrieve({**held_out, "task": "unrelated database migration"}, "irrelevant") == []
    )
    with store._connect() as db:
        db.execute("UPDATE fleet_lessons SET expires=0")
    assert learning.retrieve(held_out, "expired") == []
    assert len(learning.projection(held_out["run_id"])["uses"]) == 1


def test_missing_usage_is_not_invented_and_helpers_are_counted(team):
    store, run, _peers, _legs, _attempts, _tokens = team
    learning = FleetLearning(store)
    learning.finish({**run, "state": "failed"})
    assert learning.report(run["cwd"])["runs"][0]["reported_tokens"] is None
    store.append_event(
        run["run_id"], "worker.event", {"usage": {"input_tokens": 10, "output_tokens": 3}}
    )
    store.append_event(
        run["run_id"], "peer.worker.event", {"usage": {"input_tokens": 5, "output_tokens": 2}}
    )
    assert learning.report(run["cwd"])["runs"][0]["reported_tokens"] == {
        "input_tokens": 15,
        "output_tokens": 5,
    }


def test_extra_consultation_refuses_unknown_capacity(team, monkeypatch):
    from fleet import supervisor

    store, run, peers, legs, _attempts, tokens = team
    monkeypatch.setattr(supervisor, "_read_start_capacity", lambda: {})
    peers.request_help(tokens[0], worker_key(legs[1]), "Need advice", dedupe="capacity")
    with PeerCoordinator(
        store, run["run_id"], runner=lambda *_a, **_k: pytest.fail("unknown is not availability")
    ) as coordinator:
        drain(coordinator)
    assert peers.projection(run["run_id"])["help"][0]["state"] == "failed"
    assert "positively available" in peers.inbox(tokens[0])["help_requests"][0]["error"]


def test_frozen_peer_command_does_not_treat_sidecar_as_python(monkeypatch):
    from fleet.workers import _peer_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert _peer_command() == [sys.executable, "--fleet-peer-mcp"]


@pytest.mark.parametrize("entrypoint", ["native", "sidecar.py", "windows/sidecar-win.py"])
def test_real_stdio_peer_tools_from_isolated_working_directory(team, entrypoint):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from fleet.workers import _peer_command

    store, run, _peers, legs, _attempts, tokens = team
    command = (
        _peer_command()
        if entrypoint == "native"
        else [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "apps" / "desktop" / entrypoint),
            "--fleet-peer-mcp",
        ]
    )

    async def exercise():
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            cwd=run["cwd"],
            env={
                **os.environ,
                "SERENA_FLEET_DB_PATH": str(store.path),
                "SERENA_FLEET_PEER_TOKEN": tokens[0],
            },
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "read_messages",
                "send_message",
                "request_help",
                "resolve_request",
                "propose_lesson",
                "review_lesson",
            }
            response = await session.call_tool("read_messages", {})
            assert not response.isError
            data = response.structuredContent or json.loads(
                next(item.text for item in response.content if item.type == "text")
            )
            assert data["worker_key"] == worker_key(legs[0])
            sent = await session.call_tool(
                "send_message",
                {
                    "recipient": worker_key(legs[1]),
                    "body": "real MCP transport",
                    "dedupe": "transport",
                },
            )
            assert not sent.isError

    asyncio.run(exercise())
