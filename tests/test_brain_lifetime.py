from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import brain_daemon, brain_lifetime
from core.brain_lifetime import (
    BRAIN_SDK_ROLE,
    BRAIN_SDK_ROLE_ENV,
    BRAIN_SDK_TOKEN_ENV,
    LifetimeLedger,
    ProcessIdentity,
    RecentThreadJournal,
    RotationPolicy,
    brain_sdk_process_identities,
    brain_sdk_process_snapshot,
    process_identities,
    reap_processes,
    rotation_reason,
    secure_directory,
    write_text_atomic,
)


@pytest.fixture(autouse=True)
def _isolate_brain_system_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        brain_daemon,
        "BRAIN_SYSTEM_PROMPT_FILE",
        tmp_path / "brain-system-prompt.md",
    )


class CapturedOptions:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def _message(kind: str, **values):
    return type(kind, (), values)()


class FakeSDKClient:
    instances: list[FakeSDKClient] = []
    block_connect_number: int | None = None
    connect_gate: asyncio.Event | None = None

    def __init__(self, *, options) -> None:
        self.options = options
        self.queries: list[str] = []
        self.responses: list[list[object]] = []
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.models: list[str] = []
        type(self).instances.append(self)

    async def connect(self) -> None:
        number = len(type(self).instances)
        if number == type(self).block_connect_number and type(self).connect_gate:
            await type(self).connect_gate.wait()
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, text: str) -> None:
        self.queries.append(text)
        if "daemon session warm-up" in text:
            messages = [_message("ResultMessage", session_id=self.options.session_id)]
        else:
            block = SimpleNamespace(text=f"answer to {text.rsplit(chr(10), 1)[-1]}")
            messages = [
                _message("AssistantMessage", content=[block]),
                _message(
                    "ResultMessage",
                    session_id=self.options.session_id,
                    total_cost_usd=0,
                ),
            ]
        self.responses.append(messages)

    async def receive_response(self):
        for message in self.responses.pop(0):
            yield message

    async def set_model(self, model: str) -> None:
        self.models.append(model)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_context_usage(self) -> dict:
        return {
            "totalTokens": 100,
            "maxTokens": 1_000,
            "rawMaxTokens": 1_000,
            "percentage": 10,
            "model": "sonnet",
            "isAutoCompactEnabled": True,
        }


def _fake_process_snapshot() -> dict:
    return {
        "available": True,
        "root_pid": os.getpid(),
        "rss_bytes": 300,
        "descendants": 1,
        "processes": [
            {
                "pid": os.getpid(),
                "ppid": 1,
                "name": "python",
                "create_time": 1.0,
                "rss_bytes": 200,
                "threads": 1,
                "fds": 1,
            },
            {
                "pid": 444_444,
                "ppid": os.getpid(),
                "name": "claude",
                "create_time": 2.0,
                "rss_bytes": 100,
                "threads": 1,
                "fds": 1,
            },
        ],
    }


def _manager(tmp_path: Path) -> brain_daemon.ResidentClientManager:
    return brain_daemon.ResidentClientManager(
        CapturedOptions,
        FakeSDKClient,
        lambda: {},
        [],
        journal=RecentThreadJournal(tmp_path / "thread.json"),
        lifetime=LifetimeLedger(tmp_path / "lifetime.json"),
    )


def test_secure_directory_uses_one_inheritable_windows_acl(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "state"
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(brain_lifetime, "_SECURED_DIRECTORIES", set())
    monkeypatch.setattr(brain_lifetime.os, "name", "nt")
    monkeypatch.setattr(
        brain_lifetime,
        "_windows_current_principal",
        lambda: "RAGHAVSGAMINGPC\\Raghav",
    )
    monkeypatch.setattr(brain_lifetime.subprocess, "run", fake_run)

    assert secure_directory(path) == path.resolve()
    assert secure_directory(path) == path.resolve()
    assert calls == [
        [
            "icacls",
            str(path.resolve()),
            "/inheritance:r",
            "/grant:r",
            "RAGHAVSGAMINGPC\\Raghav:(OI)(CI)(F)",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)(F)",
            "/grant:r",
            "*S-1-5-32-544:(OI)(CI)(F)",
        ]
    ]


def test_private_text_atomic_replaces_content_with_user_only_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "prompt.md"

    assert write_text_atomic(path, "first") == path.resolve()
    assert write_text_atomic(path, "second\n") == path.resolve()

    assert path.read_text(encoding="utf-8") == "second\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_private_text_atomic_restricts_windows_temp_and_final_files(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "private" / "prompt.md"
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(brain_lifetime, "_SECURED_DIRECTORIES", set())
    monkeypatch.setattr(brain_lifetime.os, "name", "nt")
    monkeypatch.setattr(
        brain_lifetime,
        "_windows_current_principal",
        lambda: "RAGHAVSGAMINGPC\\Raghav",
    )
    monkeypatch.setattr(brain_lifetime.subprocess, "run", fake_run)

    write_text_atomic(path, "secret")

    assert path.read_text(encoding="utf-8") == "secret\n"
    assert len(calls) == 3
    assert calls[0][0] == "icacls"
    assert calls[0][1] == str(path.parent.resolve())
    assert calls[1][0] == "icacls"
    assert calls[1][1].endswith(".tmp")
    assert calls[2][:2] == ["icacls", str(path.resolve())]


def test_journal_handoff_contains_only_delivered_bounded_turns(tmp_path: Path) -> None:
    path = tmp_path / "thread.json"
    journal = RecentThreadJournal(path, max_entries=2, max_characters=200)
    first = journal.append_pending(
        user_text="first question",
        assistant_text="first answer",
        protocol="voice",
        model="sonnet",
        session_id="session-1",
        now=1,
    )
    journal.mark_delivered(first)
    journal.append_pending(
        user_text="not delivered",
        assistant_text="must not hydrate",
        protocol="plain",
        model="sonnet",
        session_id="session-1",
        now=2,
    )
    third = journal.append_pending(
        user_text="latest question",
        assistant_text="latest answer",
        protocol="frontdoor",
        model="sonnet",
        session_id="session-1",
        now=3,
    )
    journal.mark_delivered(third)

    handoff = journal.render_handoff()
    assert handoff.startswith("<recent-resident-thread>")
    assert handoff.endswith("</recent-resident-thread>")
    assert len(handoff) <= 200
    assert "latest question" in handoff
    assert "latest answer" in handoff
    assert "not delivered" not in handoff
    assert "first question" not in handoff
    assert journal.snapshot()["entries"] == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_journal_discards_responses_that_never_reached_a_surface(tmp_path: Path) -> None:
    journal = RecentThreadJournal(tmp_path / "thread.json")
    stale = journal.append_pending(
        user_text="lost question",
        assistant_text="lost answer",
        protocol="plain",
        model="sonnet",
        session_id="session-1",
    )
    delivered = journal.append_pending(
        user_text="kept question",
        assistant_text="kept answer",
        protocol="voice",
        model="sonnet",
        session_id="session-1",
    )
    assert journal.mark_delivered(delivered)
    assert journal.discard(stale)
    assert journal.snapshot()["pending_entries"] == 0
    assert "kept question" in journal.render_handoff()
    assert "lost question" not in journal.render_handoff()


def test_journal_clear_removes_an_inspected_synthetic_handoff(tmp_path: Path) -> None:
    journal = RecentThreadJournal(tmp_path / "thread.json")
    entry = journal.append_pending(
        user_text="synthetic probe",
        assistant_text="synthetic reply",
        protocol="voice",
        model="sonnet",
        session_id="session",
    )
    assert journal.mark_delivered(entry)

    assert journal.clear() == 1
    assert journal.clear() == 0
    assert journal.snapshot()["entries"] == 0
    assert journal.render_handoff() == ""


def test_committed_reply_survives_an_uncertain_transport_result(tmp_path: Path) -> None:
    journal = RecentThreadJournal(tmp_path / "thread.json")
    entry = journal.append_pending(
        user_text="did this reach me",
        assistant_text="the durable answer",
        protocol="voice",
        model="sonnet",
        session_id="session-1",
    )
    assert journal.mark_delivered(entry)
    assert journal.mark_transport_uncertain(entry)
    assert journal.discard_undelivered() == 0
    assert journal.snapshot()["transport_uncertain_entries"] == 1
    assert "delivery-uncertain" in journal.render_handoff()
    assert "the durable answer" in journal.render_handoff()


def test_lifetime_ledger_closes_superseded_epoch_and_stays_bounded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifetime.json"
    lifetime = LifetimeLedger(path, max_epochs=2)
    lifetime.start_epoch("one", reason="boot", now=10)
    epoch = lifetime.record_turn(
        context={"percentage": 12},
        process={"rss_bytes": 100},
        compact_boundary_seen=True,
    )
    assert epoch["completed_turns"] == 1
    assert epoch["compactions"] == 1
    lifetime.start_epoch("two", reason="rotation", now=20)
    lifetime.start_epoch("three", reason="rotation", now=30)

    document = json.loads(path.read_text())
    assert [row["session_id"] for row in document["epochs"]] == ["two", "three"]
    assert document["epochs"][0]["end_reason"] == "superseded"
    assert document["epochs"][0]["ended_at"] == 30


def test_rotation_priority_and_rss_thresholds() -> None:
    policy = RotationPolicy(
        max_age_seconds=100,
        max_completed_turns=10,
        max_context_percentage=70,
        max_child_rss_growth_bytes=100,
        max_child_rss_multiplier=2,
    )
    common = {
        "policy": policy,
        "age_seconds": 0,
        "completed_turns": 0,
        "context_percentage": 0,
        "compact_boundary_seen": False,
        "child_rss_bytes": 100,
        "baseline_child_rss_bytes": 100,
    }
    assert rotation_reason(**common) is None
    assert rotation_reason(**{**common, "compact_boundary_seen": True}) == "compact_boundary"
    assert rotation_reason(**{**common, "context_percentage": 70}) == "context_percentage"
    assert rotation_reason(**{**common, "completed_turns": 10}) == "completed_turns"
    assert rotation_reason(**{**common, "age_seconds": 100}) == "session_age"
    assert rotation_reason(**{**common, "child_rss_bytes": 200}) == "child_rss_growth"


def test_process_identity_extraction_excludes_the_daemon() -> None:
    identities = process_identities(_fake_process_snapshot())
    assert identities == [ProcessIdentity(pid=444_444, create_time=2.0)]


def test_session_store_snapshot_uses_claudes_dot_munging(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / ".cache" / "serena-headless-brain"
    monkeypatch.setattr(brain_daemon, "BRAIN_CWD", cwd)
    slug = str(cwd).replace("/", "-").replace(".", "-")
    project = tmp_path / ".claude" / "projects" / slug
    project.mkdir(parents=True)
    session_id = "11111111-1111-4111-8111-111111111111"
    (project / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")

    snapshot = brain_daemon._session_store_snapshot(session_id)

    assert snapshot["project_dir"] == str(project)
    assert snapshot["jsonl_files"] == 1
    assert snapshot["current"]["session_id"] == session_id


def test_reaper_never_signals_a_reused_pid() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import psutil

        created = psutil.Process(process.pid).create_time()
        survivors = asyncio.run(
            reap_processes(
                [ProcessIdentity(process.pid, created + 1000)],
                timeout_seconds=0,
            )
        )
        assert survivors == []
        assert process.poll() is None
        assert (
            asyncio.run(
                reap_processes(
                    [ProcessIdentity(process.pid, created)],
                    timeout_seconds=0,
                )
            )
            == []
        )
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_sdk_marker_finds_and_reaps_a_process_outside_tree_assumptions() -> None:
    token = "test-sdk-process-token"
    environment = {
        **os.environ,
        BRAIN_SDK_ROLE_ENV: BRAIN_SDK_ROLE,
        BRAIN_SDK_TOKEN_ENV: token,
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,time; "
            "ctypes.CDLL(None).prctl(15,b'claude',0,0,0); time.sleep(30)",
            token,
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        snapshot = {}
        for _ in range(100):
            snapshot = brain_sdk_process_snapshot(token, known_tokens=(token,))
            if process.pid in [row["pid"] for row in snapshot["processes"]]:
                break
            time.sleep(0.01)
        assert process.pid in [row["pid"] for row in snapshot["processes"]]
        assert snapshot["active_processes"] >= 1
        identities = brain_sdk_process_identities(token)
        assert process.pid in [identity.pid for identity in identities]
        assert asyncio.run(reap_processes(identities, timeout_seconds=0)) == []
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_committed_reply_survives_restart_cleanup_window(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)

        async def fake_reap(*_args, **_kwargs):
            return []

        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        await manager.start()
        async with brain_daemon._turn_lock:
            out = await brain_daemon._run_turn(
                manager, {"protocol": "plain", "text": "survive this crash"}
            )
            assert out["session_id"] == manager.session_id
            assert out["_session_id"] == manager.session_id
            assert out["_session_turns"] == 1
            await manager.response_committed(out)
            await manager.response_not_delivered(out)

        restarted_journal = RecentThreadJournal(tmp_path / "thread.json")
        assert restarted_journal.discard_undelivered() == 0
        assert restarted_journal.snapshot()["transport_uncertain_entries"] == 1
        handoff = restarted_journal.render_handoff()
        assert "survive this crash" in handoff
        assert "answer to survive this crash" in handoff
        await manager.stop()

    asyncio.run(scenario())


def test_stuck_sdk_interrupt_is_bounded(monkeypatch) -> None:
    async def scenario() -> None:
        class StuckClient:
            async def interrupt(self) -> None:
                await asyncio.Event().wait()

        async def turn() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(brain_daemon, "INTERRUPT_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(brain_daemon, "TURN_CANCEL_TIMEOUT_SECONDS", 0.02)
        task = asyncio.create_task(turn())
        await asyncio.wait_for(
            brain_daemon._interrupt_active_turn(StuckClient(), task),
            timeout=0.2,
        )
        assert task.done()

    asyncio.run(scenario())


def test_manager_shutdown_has_a_bounded_turn_lock_wait(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        brain_daemon._turn_lock = asyncio.Lock()
        await brain_daemon._turn_lock.acquire()
        monkeypatch.setattr(brain_daemon, "TURN_CANCEL_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(brain_daemon, "INTERRUPT_TIMEOUT_SECONDS", 0.02)
        manager = _manager(tmp_path)
        await asyncio.wait_for(manager.stop(), timeout=0.2)
        assert "could not acquire" in str(manager.last_error)
        brain_daemon._turn_lock.release()

    asyncio.run(scenario())


def test_manager_rotates_to_fresh_session_with_delivered_handoff(
    monkeypatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        FakeSDKClient.block_connect_number = None
        FakeSDKClient.connect_gate = None
        reaped: list[list[ProcessIdentity]] = []

        async def fake_reap(identities, **kwargs):
            reaped.append(list(identities))
            return []

        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)
        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        monkeypatch.setattr(brain_daemon, "MODEL", "sonnet")
        monkeypatch.setattr(brain_daemon, "VOICE_MODEL", "sonnet")
        monkeypatch.setattr(brain_daemon, "_active_model", "sonnet")
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        manager.policy = RotationPolicy(max_completed_turns=1)

        await manager.start()
        first_client = FakeSDKClient.instances[0]
        first_session = manager.session_id
        async with brain_daemon._turn_lock:
            out = await brain_daemon._run_turn(
                manager, {"protocol": "plain", "text": "first question"}
            )
            assert manager.client is first_client
            await manager.response_committed(out)
            await manager.response_delivered(out)

        assert len(FakeSDKClient.instances) == 2
        second_client = FakeSDKClient.instances[1]
        assert first_client.disconnected
        assert manager.client is second_client
        assert manager.session_id != first_session
        assert second_client.options.session_id != first_client.options.session_id
        assert getattr(second_client.options, "resume", None) is None
        assert "first question" in second_client.queries[0]
        assert "answer to first question" in second_client.queries[0]
        assert manager.journal.snapshot()["pending_entries"] == 0
        assert manager.rotation_count == 1
        assert reaped and reaped[0] == [ProcessIdentity(444_444, 2.0)]
        json.dumps(manager.snapshot())
        await manager.stop()

    asyncio.run(scenario())


def test_undelivered_turn_never_hydrates_or_rotates(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)

        async def fake_reap(*args, **kwargs):
            return []

        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        manager.policy = RotationPolicy(max_completed_turns=1)
        await manager.start()
        async with brain_daemon._turn_lock:
            out = await brain_daemon._run_turn(
                manager, {"protocol": "voice", "text": "lost on the wire"}
            )
            await manager.response_not_delivered(out)

        assert len(FakeSDKClient.instances) == 1
        assert manager.rotation_count == 0
        assert manager.journal.snapshot()["entries"] == 0
        await manager.stop()

    asyncio.run(scenario())


def test_non_journaled_probe_never_enters_rotation_handoff(
    monkeypatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)

        async def fake_reap(*args, **kwargs):
            return []

        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        manager.policy = RotationPolicy(max_completed_turns=1)
        await manager.start()
        async with brain_daemon._turn_lock:
            out = await brain_daemon._run_turn(
                manager,
                {
                    "protocol": "voice",
                    "text": "synthetic probe",
                    "journal": False,
                },
            )
            await manager.response_committed(out)
            await manager.response_delivered(out)

        assert manager.journal.snapshot()["entries"] == 0
        assert manager.rotation_count == 0
        await manager.stop()

    asyncio.run(scenario())


def test_queued_turn_waits_until_rotation_is_fully_warm(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        FakeSDKClient.block_connect_number = 2
        FakeSDKClient.connect_gate = asyncio.Event()
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)

        async def fake_reap(*args, **kwargs):
            return []

        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        manager.policy = RotationPolicy(max_completed_turns=1)
        await manager.start()

        first_acquired = asyncio.Event()
        second_acquired = asyncio.Event()

        async def first_exchange() -> None:
            async with brain_daemon._turn_lock:
                first_acquired.set()
                out = await brain_daemon._run_turn(
                    manager, {"protocol": "plain", "text": "rotate now"}
                )
                await manager.response_committed(out)
                await manager.response_delivered(out)

        async def queued_exchange() -> None:
            async with brain_daemon._turn_lock:
                second_acquired.set()
                assert manager.client is FakeSDKClient.instances[1]

        first_task = asyncio.create_task(first_exchange())
        await first_acquired.wait()
        for _ in range(100):
            if len(FakeSDKClient.instances) == 2:
                break
            await asyncio.sleep(0)
        second_task = asyncio.create_task(queued_exchange())
        await asyncio.sleep(0.02)
        assert not second_acquired.is_set()
        FakeSDKClient.connect_gate.set()
        await asyncio.gather(first_task, second_task)
        assert second_acquired.is_set()
        await manager.stop()

    asyncio.run(scenario())


def test_surviving_sdk_child_trips_daemon_fatal_event(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        FakeSDKClient.instances = []
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "ledger")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(brain_daemon, "process_tree_snapshot", _fake_process_snapshot)
        calls = 0

        async def fake_reap(*args, **kwargs):
            nonlocal calls
            calls += 1
            return [444_444] if calls == 1 else []

        monkeypatch.setattr(brain_daemon, "reap_processes", fake_reap)
        brain_daemon._turn_lock = asyncio.Lock()
        manager = _manager(tmp_path)
        manager.policy = RotationPolicy(max_completed_turns=1)
        await manager.start()
        async with brain_daemon._turn_lock:
            out = await brain_daemon._run_turn(
                manager, {"protocol": "plain", "text": "force rotation"}
            )
            await manager.response_committed(out)
            await manager.response_delivered(out)

        assert manager.fatal_event.is_set()
        assert "survived teardown" in str(manager.fatal_error)
        assert manager.client is None
        await manager.stop()

    asyncio.run(scenario())
