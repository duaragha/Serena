from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import psutil

from voice.call.cost_audit import (
    analyze_cost_objective,
    baseline_delta_evidence,
    brain_daemon_evidence,
    brain_health_evidence,
    call_cost_evidence,
    capture_baseline,
    capture_metrics_cursor,
    load_metrics_append,
    subscription_auth_evidence,
)

_LIFECYCLE_ID = "11111111-1111-4111-8111-111111111111"
_SECOND_LIFECYCLE_ID = "22222222-2222-4222-8222-222222222222"


def _ready(
    at: int,
    *,
    tts_backend: str = "kokoro-onnx",
    lifecycle_id: str = _LIFECYCLE_ID,
) -> dict:
    return {
        "call_id": "cost-call",
        "lifecycle_id": lifecycle_id,
        "event": "call.ready",
        "monotonic_us": at,
        "ready": True,
        "call_host_pid": 456,
        "anthropic_api_key_present": False,
        "metered_auth_env_present": [],
        "speech_execution": "local_only",
        "models": {"stt": "ready", "tts": "ready", "vad": "ready"},
        "model_details": {
            "stt": {
                "backend": "faster-whisper",
                "execution": "local",
                "model_source": "local_path",
                "device": "cpu",
                "compute_type": "int8",
            },
            "tts": {
                "backend": tts_backend,
                "execution": "local",
                "model_source": "local_path",
                "provider": "CPUExecutionProvider",
            },
            "vad": {
                "backend": "silero-vad",
                "execution": "local",
                "model_source": "offline_cache",
            },
        },
    }


def _rows(
    *,
    backend: str = "brain.sock",
    lifecycle_id: str = _LIFECYCLE_ID,
) -> list[dict]:
    rows = [
        {
            "call_id": "cost-call",
            "lifecycle_id": lifecycle_id,
            "event": "call.start",
            "ts": "1970-01-01T00:00:20+00:00",
            "monotonic_us": 1,
            "call_host_pid": 456,
            "anthropic_api_key_present": False,
            "metered_auth_env_present": [],
            "speech_execution": "local_only",
        },
        _ready(2, lifecycle_id=lifecycle_id),
        {
            "call_id": "cost-call",
            "lifecycle_id": lifecycle_id,
            "event": "stt.done",
            "monotonic_us": 3,
            "generation": 1,
        },
        {
            "call_id": "cost-call",
            "lifecycle_id": lifecycle_id,
            "event": "brain.done",
            "monotonic_us": 4,
            "generation": 1,
            "backend": backend,
            "meta": {
                "billing_mode": "subscription_oauth_guarded",
                "daemon_pid": 123,
                "daemon_started": 10.0,
            },
        },
        {
            "call_id": "cost-call",
            "lifecycle_id": lifecycle_id,
            "event": "audio.first_content_send",
            "monotonic_us": 5,
            "generation": 1,
        },
        {
            "call_id": "cost-call",
            "lifecycle_id": lifecycle_id,
            "event": "call.end",
            "monotonic_us": 6,
            "clean_hangup": True,
        },
    ]
    for row in rows:
        row.setdefault("schema_version", 1)
        row.setdefault("clock_domain", "server_monotonic")
    return rows


def _good_auth() -> dict:
    return {
        "ok": True,
        "failures": [],
        "subscription_type": "max",
        "metered_auth_env_present": [],
    }


def _good_daemon() -> dict:
    return {
        "ok": True,
        "failures": [],
        "api_key_present": False,
        "metered_auth_env_present": [],
        "pid": 123,
        "started": 10.0,
        "process_created": 9.5,
        "boot_time": 1.0,
    }


def _good_health(*, turns: int) -> dict:
    return {
        "ok": True,
        "failures": [],
        "pid": 123,
        "started": 10.0,
        "process_created": 9.5,
        "boot_time": 1.0,
        "turns": turns,
        "notional_cost_usd": 0.5 + turns / 100,
    }


def _good_baseline() -> dict:
    baseline = capture_baseline(
        auth=_good_auth(),
        daemon=_good_daemon(),
        health=_good_health(turns=10),
        metrics_cursor=_good_metrics_cursor(),
    )
    baseline["captured_at"] = 10.0
    return baseline


def _good_metrics_cursor() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "path": "/tmp/call_metrics.jsonl",
        "device": 1,
        "inode": 2,
        "offset": 0,
        "prefix_sha256": "0" * 64,
        "failures": [],
    }


def _good_metrics_window() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "path": "/tmp/call_metrics.jsonl",
        "start_offset": 0,
        "end_offset": 1,
        "rows": 6,
        "failures": [],
    }


def _worker_billing_json() -> str:
    return json.dumps(
        {
            "schema_version": 3,
            "ok": True,
            "captured_at": 15.0,
            "auth_mode": "subscription_oauth_guarded",
            "logged_in": True,
            "auth_method": "claude.ai",
            "api_provider": "firstParty",
            "subscription_type": "max",
            "api_key_present": False,
            "metered_auth_env_present": [],
            "setting_sources": [],
            "gate_pid": 42,
            "worker_token": "token",
            "worker_session_id": "33333333-3333-4333-8333-333333333333",
            "command_sha256": "a" * 64,
            "environment_sha256": "b" * 64,
            "stage": "exec_ready",
            "exec_pid": 43,
            "exec_token": "exec-token",
            "containment": "linux_pdeathsig",
        }
    )


def test_cost_gate_separates_automated_proof_from_dashboard_attestation() -> None:
    pending = analyze_cost_objective(
        _rows(),
        call_id="cost-call",
        auth=_good_auth(),
        daemon=_good_daemon(),
        baseline=_good_baseline(),
        health=_good_health(turns=11),
        metrics_window=_good_metrics_window(),
    )
    assert pending["automated_pass"] is True
    assert pending["acceptance_claim"] is False
    assert pending["notional_sdk_cost_is_billing_evidence"] is False
    assert "billing dashboard" in pending["failures"][-1]

    accepted = analyze_cost_objective(
        _rows(),
        call_id="cost-call",
        auth=_good_auth(),
        daemon=_good_daemon(),
        baseline=_good_baseline(),
        health=_good_health(turns=11),
        metrics_window=_good_metrics_window(),
        billing_dashboard_clear=True,
    )
    assert accepted["ok"] is True
    assert accepted["acceptance_claim"] is True


def test_cost_gate_rejects_nonlocal_brain_or_speech_provenance() -> None:
    cloud_brain = call_cost_evidence(_rows(backend="anthropic-direct-api"), call_id="cost-call")
    assert cloud_brain["ok"] is False
    assert "bypassed" in " ".join(cloud_brain["failures"])

    cloud_tts = _rows()
    cloud_tts[1] = _ready(2, tts_backend="elevenlabs")
    result = call_cost_evidence(cloud_tts, call_id="cost-call")
    assert result["ok"] is False
    assert "TTS provenance" in " ".join(result["failures"])

    missing_vad = _rows()
    missing_vad[1]["model_details"].pop("vad")
    result = call_cost_evidence(missing_vad, call_id="cost-call")
    assert result["ok"] is False
    assert "VAD provenance" in " ".join(result["failures"])


def test_cost_gate_rejects_a_reused_call_id_in_one_append_window() -> None:
    old = _rows(backend="anthropic-direct-api")
    new = []
    for row in _rows(lifecycle_id=_SECOND_LIFECYCLE_ID):
        updated = dict(row)
        updated["monotonic_us"] = int(row["monotonic_us"]) + 10
        new.append(updated)
    result = call_cost_evidence([*old, *new], call_id="cost-call")
    assert result["ok"] is False
    assert "reused after a completed server lifecycle" in " ".join(result["failures"])
    assert "anthropic-direct-api" in result["brain_backends"]


def test_cost_gate_rejects_reuse_even_when_monotonic_clock_resets() -> None:
    old = []
    for row in _rows(backend="anthropic-direct-api"):
        updated = dict(row)
        updated["monotonic_us"] = int(row["monotonic_us"]) + 1_000
        old.append(updated)
    result = call_cost_evidence(
        [*old, *_rows(lifecycle_id=_SECOND_LIFECYCLE_ID)], call_id="cost-call"
    )
    assert result["ok"] is False
    assert "reused after a completed server lifecycle" in " ".join(result["failures"])


def test_cost_gate_accepts_an_abrupt_lifecycle_followed_by_clean_resume() -> None:
    interrupted = _rows()[:-1]
    resumed = []
    for row in _rows(lifecycle_id=_SECOND_LIFECYCLE_ID):
        updated = dict(row)
        updated["monotonic_us"] = int(row["monotonic_us"]) + 10
        resumed.append(updated)

    result = call_cost_evidence([*interrupted, *resumed], call_id="cost-call")

    assert result["ok"] is True
    assert result["lifecycle_ids"] == [_LIFECYCLE_ID, _SECOND_LIFECYCLE_ID]
    assert result["reconnects"] == 1


def test_cost_gate_rejects_malformed_or_backwards_lifecycle_clocks() -> None:
    backwards = _rows()
    backwards[-1]["monotonic_us"] = 0
    result = call_cost_evidence(backwards, call_id="cost-call")
    assert result["ok"] is False
    assert "moved backwards" in " ".join(result["failures"])

    malformed = _rows()
    malformed[2].pop("monotonic_us")
    result = call_cost_evidence(malformed, call_id="cost-call")
    assert result["ok"] is False
    assert "malformed monotonic" in " ".join(result["failures"])

    wrong_domain = _rows()
    wrong_domain[2]["clock_domain"] = "client_monotonic"
    result = call_cost_evidence(wrong_domain, call_id="cost-call")
    assert result["ok"] is False
    assert "untrusted clock domain" in " ".join(result["failures"])


def test_cost_gate_rejects_paid_lifecycle_hidden_by_reused_call_id() -> None:
    paid = _rows(backend="anthropic-direct-api")
    paid[0]["ts"] = "not-a-time"
    local = []
    for row in _rows(lifecycle_id=_SECOND_LIFECYCLE_ID):
        updated = dict(row)
        updated["monotonic_us"] = int(row["monotonic_us"]) + 10
        if updated.get("event") == "call.start":
            updated["ts"] = "1970-01-01T00:00:30+00:00"
        local.append(updated)
    result = analyze_cost_objective(
        [*paid, *local],
        call_id="cost-call",
        auth=_good_auth(),
        daemon=_good_daemon(),
        baseline=_good_baseline(),
        health=_good_health(turns=12),
        metrics_window=_good_metrics_window(),
        billing_dashboard_clear=True,
    )
    assert result["ok"] is False
    failures = " ".join(result["failures"])
    assert "reused after a completed server lifecycle" in failures
    assert "bypassed" in failures


def test_cost_gate_requires_call_task_worker_to_be_terminal(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(jobs) as connection:
        connection.execute(
            "CREATE TABLE work_jobs (job_id TEXT PRIMARY KEY, state TEXT, "
            "worker_pid INTEGER, worker_token TEXT, worker_session_id TEXT, "
            "billing_evidence_json TEXT)"
        )
        connection.execute(
            "INSERT INTO work_jobs VALUES ('job-1', 'running', 42, 'token', "
            "'33333333-3333-4333-8333-333333333333', ?)",
            (_worker_billing_json(),),
        )
    rows = _rows()
    rows.insert(
        -1,
        {
            "call_id": "cost-call",
            "lifecycle_id": _LIFECYCLE_ID,
            "schema_version": 1,
            "clock_domain": "server_monotonic",
            "event": "task.accepted",
            "monotonic_us": 5,
            "job_id": "job-1",
        },
    )
    running = call_cost_evidence(rows, call_id="cost-call", jobs_path=jobs)
    assert running["ok"] is False
    assert "still owns a worker" in " ".join(running["failures"])

    with sqlite3.connect(jobs) as connection:
        connection.execute(
            "UPDATE work_jobs SET state = 'artifact_ready', worker_pid = NULL, "
            "worker_token = NULL WHERE job_id = 'job-1'"
        )
    terminal = call_cost_evidence(rows, call_id="cost-call", jobs_path=jobs)
    assert terminal["ok"] is True
    assert terminal["terminal_task_jobs"] == 1

    with sqlite3.connect(jobs) as connection:
        connection.execute(
            "UPDATE work_jobs SET billing_evidence_json = NULL WHERE job_id = 'job-1'"
        )
    missing_evidence = call_cost_evidence(rows, call_id="cost-call", jobs_path=jobs)
    assert missing_evidence["ok"] is False
    assert "subscription worker evidence" in " ".join(missing_evidence["failures"])


def test_cost_gate_rejects_mixed_or_stale_daemon_evidence() -> None:
    rows = _rows()
    rows[3] = {
        **rows[3],
        "meta": {
            "billing_mode": "subscription_oauth_guarded",
            "daemon_pid": 999,
            "daemon_started": 1.0,
        },
    }
    result = analyze_cost_objective(
        rows,
        call_id="cost-call",
        auth=_good_auth(),
        daemon=_good_daemon(),
        baseline=_good_baseline(),
        health=_good_health(turns=11),
        metrics_window=_good_metrics_window(),
        billing_dashboard_clear=True,
    )
    assert result["ok"] is False
    assert "different daemon" in " ".join(result["failures"])


def test_cost_gate_requires_same_boot_preflight_and_postflight() -> None:
    missing = baseline_delta_evidence(None, daemon=_good_daemon(), health=_good_health(turns=11))
    assert missing["ok"] is False
    changed = {
        **_good_daemon(),
        "pid": 999,
    }
    result = baseline_delta_evidence(
        _good_baseline(), daemon=changed, health=_good_health(turns=11)
    )
    assert result["ok"] is False
    assert "changed" in " ".join(result["failures"])


def test_metrics_cursor_reads_only_verified_appends(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text(json.dumps({"event": "old"}) + "\n", encoding="utf-8")
    cursor = capture_metrics_cursor(metrics)
    with metrics.open("a", encoding="utf-8") as handle:
        for row in _rows():
            handle.write(json.dumps(row) + "\n")

    rows, window = load_metrics_append(metrics, cursor)

    assert window["ok"] is True
    assert rows == _rows()


def test_metrics_cursor_rejects_rotation_truncation_and_prefix_rewrite(tmp_path: Path) -> None:
    rotated = tmp_path / "rotated.jsonl"
    rotated.write_text("old\n", encoding="utf-8")
    rotated_cursor = capture_metrics_cursor(rotated)
    rotated.rename(tmp_path / "old.jsonl")
    rotated.write_text("new\n", encoding="utf-8")
    assert load_metrics_append(rotated, rotated_cursor)[1]["ok"] is False

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_text("old\n", encoding="utf-8")
    truncated_cursor = capture_metrics_cursor(truncated)
    truncated.write_text("", encoding="utf-8")
    assert load_metrics_append(truncated, truncated_cursor)[1]["ok"] is False

    rewritten = tmp_path / "rewritten.jsonl"
    rewritten.write_text("old\n", encoding="utf-8")
    rewritten_cursor = capture_metrics_cursor(rewritten)
    rewritten.write_text("bad\n", encoding="utf-8")
    assert load_metrics_append(rewritten, rewritten_cursor)[1]["ok"] is False


def test_cost_gate_rejects_malformed_baseline_time() -> None:
    baseline = _good_baseline()
    baseline.pop("captured_at")
    result = baseline_delta_evidence(
        baseline,
        daemon=_good_daemon(),
        health=_good_health(turns=11),
    )
    assert result["ok"] is False
    assert "capture time" in " ".join(result["failures"])


def test_subscription_auth_fails_closed_and_returns_no_identity_fields() -> None:
    blocked = subscription_auth_evidence(
        environ={"ANTHROPIC_API_KEY": "secret"},
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("auth command should not run with an API key")
        ),
    )
    assert blocked["ok"] is False
    assert blocked["api_key_present"] is True

    provider_override = subscription_auth_evidence(
        environ={"CLAUDE_CODE_USE_BEDROCK": "1"},
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("auth command should not run with a provider override")
        ),
    )
    assert provider_override["ok"] is False
    assert provider_override["metered_auth_env_present"] == ["CLAUDE_CODE_USE_BEDROCK"]

    mantle_override = subscription_auth_evidence(
        environ={"CLAUDE_CODE_USE_MANTLE": "1"},
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("auth command should not run with a provider override")
        ),
    )
    assert mantle_override["ok"] is False
    assert mantle_override["metered_auth_env_present"] == ["CLAUDE_CODE_USE_MANTLE"]

    payload = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
        "email": "private@example.com",
        "orgId": "private-org",
    }

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, json.dumps(payload), "")

    evidence = subscription_auth_evidence(environ={}, runner=runner)
    assert evidence["ok"] is True
    assert "email" not in evidence
    assert "orgId" not in evidence


def test_brain_daemon_evidence_requires_local_stream_and_keyless_process(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "brain.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    discovery = tmp_path / "brain.json"
    lock_path = tmp_path / "brain.lock"
    lock_path.write_text(str(os.getpid()), encoding="ascii")
    discovery.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": 10.0,
                "process_created": 9.5,
                "boot_time": 1.0,
                "lock": str(lock_path),
                "stream": {"transport": "unix", "path": str(socket_path)},
            }
        ),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == os.getpid()

        def is_running(self) -> bool:
            return True

        def cmdline(self) -> list[str]:
            return ["python", "-m", "core.brain_daemon"]

        def create_time(self) -> float:
            return 9.5

        def environ(self) -> dict[str, str]:
            return {"PATH": "/bin"}

    class Candidate:
        info = {
            "pid": os.getpid(),
            "cmdline": ["python", "-m", "core.brain_daemon"],
        }

    class UnreadableCandidate:
        info = {"pid": 999_999, "cmdline": None, "name": None}

    class ZombieCandidate:
        info = {
            "pid": 999_998,
            "cmdline": None,
            "name": "notify-send",
            "status": psutil.STATUS_ZOMBIE,
        }

    try:
        result = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate()],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
        )
        unreadable = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate(), UnreadableCandidate()],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
        )
        zombie = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate(), ZombieCandidate()],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
        )
        windows_scoped = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate(), UnreadableCandidate()],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
            uid_provider=lambda: None,
            process_sid=lambda pid: "S-1-user" if pid == os.getpid() else "S-1-system",
        )
        windows_same_user_unreadable = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate(), UnreadableCandidate()],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
            uid_provider=lambda: None,
            process_sid=lambda _pid: "S-1-user",
        )
    finally:
        server.close()
    assert result["ok"] is True
    assert result["stream_local"] is True
    assert result["api_key_present"] is False
    assert unreadable["ok"] is False
    assert unreadable["unreadable_python_pids"] == [999_999]
    assert zombie["ok"] is True
    assert zombie["unreadable_process_pids"] == []
    assert windows_scoped["ok"] is True
    assert windows_same_user_unreadable["ok"] is False


def test_brain_daemon_evidence_rejects_duplicate_daemons(tmp_path: Path) -> None:
    socket_path = tmp_path / "brain.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    discovery = tmp_path / "brain.json"
    lock_path = tmp_path / "brain.lock"
    lock_path.write_text(str(os.getpid()), encoding="ascii")
    discovery.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": 10.0,
                "process_created": 9.5,
                "boot_time": 1.0,
                "lock": str(lock_path),
                "stream": {"transport": "unix", "path": str(socket_path)},
            }
        ),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == os.getpid()

        def is_running(self) -> bool:
            return True

        def cmdline(self) -> list[str]:
            return ["python", "-m", "core.brain_daemon"]

        def create_time(self) -> float:
            return 9.5

        def environ(self) -> dict[str, str]:
            return {"PATH": "/bin"}

    class Candidate:
        def __init__(self, pid: int) -> None:
            self.info = {
                "pid": pid,
                "cmdline": ["python", "-m", "core.brain_daemon"],
            }

    try:
        result = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [Candidate(os.getpid()), Candidate(999_999)],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
        )
    finally:
        server.close()
    assert result["ok"] is False
    assert result["daemon_pids"] == sorted([os.getpid(), 999_999])
    assert "exactly one" in " ".join(result["failures"])


def test_brain_daemon_census_ignores_processes_merely_reading_the_source(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "brain.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    discovery = tmp_path / "brain.json"
    lock_path = tmp_path / "brain.lock"
    lock_path.write_text(str(os.getpid()), encoding="ascii")
    discovery.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": 10.0,
                "process_created": 9.5,
                "boot_time": 1.0,
                "lock": str(lock_path),
                "stream": {"transport": "unix", "path": str(socket_path)},
            }
        ),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == os.getpid()

        def is_running(self) -> bool:
            return True

        def cmdline(self) -> list[str]:
            return ["python", "-m", "core.brain_daemon"]

        def create_time(self) -> float:
            return 9.5

        def environ(self) -> dict[str, str]:
            return {"PATH": "/bin"}

    class Candidate:
        def __init__(self, pid: int, command: list[str]) -> None:
            self.info = {"pid": pid, "cmdline": command}

    try:
        result = brain_daemon_evidence(
            discovery,
            process_factory=Process,
            process_iter=lambda **kwargs: [
                Candidate(os.getpid(), ["python", "-m", "core.brain_daemon"]),
                Candidate(999_999, ["vim", "core/brain_daemon.py"]),
            ],
            lock_probe=lambda path: path == lock_path,
            boot_time=lambda: 1.0,
        )
    finally:
        server.close()
    assert result["ok"] is True
    assert result["daemon_pids"] == [os.getpid()]


def test_brain_health_labels_notional_cost_without_claiming_spend(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "brain.json"
    discovery.write_text(
        json.dumps(
            {
                "port": 8377,
                "pid": 123,
                "started": 10.0,
                "process_created": 9.5,
                "boot_time": 1.0,
                "token": "secret",
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "ok": True,
        "pid": 123,
        "started": 10.0,
        "process_created": 9.5,
        "boot_time": 1.0,
        "turns": 7,
        "billing": {
            "auth_mode": "subscription_oauth_guarded",
            "api_key_present": False,
            "metered_auth_env_present": [],
            "notional_cost_usd": 1.25,
            "notional_source": "sdk_result_model_price_estimate",
            "metered_cost_usd": None,
            "metered_source": "billing_dashboard_only",
        },
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit: int) -> bytes:
            assert limit == 256 * 1024
            return json.dumps(payload).encode()

    result = brain_health_evidence(
        discovery,
        opener=lambda *args, **kwargs: Response(),
    )
    assert result["ok"] is True
    assert result["notional_cost_usd"] == 1.25
    assert result["metered_cost_usd"] is None
