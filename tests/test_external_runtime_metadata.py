import json
import os
import socket
import time

from core import metadata


def _isolated_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(metadata, "_migrated", True)


def test_external_runtime_claim_is_active_until_owner_releases(monkeypatch, tmp_path):
    _isolated_metadata(monkeypatch, tmp_path)
    sid = "session-owned"

    claim = metadata.set_external_runtime(
        sid,
        kind="codex-exec",
        pid=os.getpid(),
        lease_seconds=120,
    )

    assert claim["host"] == socket.gethostname()
    assert metadata.external_runtime_active(sid) is True
    assert metadata.clear_external_runtime(sid, pid=os.getpid() + 1) is False
    assert metadata.external_runtime_active(sid) is True
    assert metadata.clear_external_runtime(sid, pid=os.getpid()) is True
    assert metadata.external_runtime_active(sid) is False


def test_dead_local_owner_and_expired_foreign_lease_are_inactive(monkeypatch, tmp_path):
    _isolated_metadata(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metadata.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    local_sid = "dead-local"
    foreign_sid = "expired-foreign"
    metadata.set_external_runtime(
        local_sid,
        kind="codex-exec",
        pid=424_242,
        lease_seconds=120,
    )
    metadata._save_one(
        foreign_sid,
        {
            "external_runtime": {
                "kind": "codex-exec",
                "pid": 123,
                "host": "another-device",
                "lease_expires_at": time.time() - 1,
            }
        },
    )

    assert metadata.external_runtime_active(local_sid) is False
    assert metadata.external_runtime_active(foreign_sid) is False


def test_external_runtime_metadata_is_serializable(monkeypatch, tmp_path):
    _isolated_metadata(monkeypatch, tmp_path)
    sid = "serializable"
    metadata.set_external_runtime(
        sid,
        kind="codex-exec",
        pid=os.getpid(),
        lease_seconds=120,
    )

    stored = json.loads((metadata.METADATA_DIR / f"{sid}.json").read_text())
    assert stored["external_runtime"]["kind"] == "codex-exec"


def test_fleet_marker_survives_runtime_release(monkeypatch, tmp_path):
    _isolated_metadata(monkeypatch, tmp_path)
    sid = "fleet-worker"
    marker = metadata.set_fleet_worker(
        sid,
        run_id="fleet-123",
        leg_id="verify-codex",
        phase="verify",
        provider="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        origin_session_id="original-chat",
        worker_label="Codex 1",
        assignment="review parser safety",
    )
    metadata.set_external_runtime(
        sid,
        kind="fleet-worker",
        pid=os.getpid(),
        lease_seconds=120,
    )

    assert metadata.clear_external_runtime(sid, pid=os.getpid()) is True
    assert metadata.get_meta(sid)["fleet_worker"] == marker
    assert marker["worker_label"] == "Codex 1"
    assert marker["assignment"] == "review parser safety"


def test_surface_fleet_worker_applies_stable_identity_and_group_once(monkeypatch, tmp_path):
    _isolated_metadata(monkeypatch, tmp_path)
    changed = metadata.surface_fleet_worker(
        "durable-worker",
        run_id="fleet-123",
        leg_id="discover-codex",
        phase="discover",
        provider="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        worker_key="codex:0",
        worker_label="Codex 1",
        assignment="parser ownership",
        worker_group_id="g_fleet123",
        title="Fleet fleet-12 | codex | gpt-5.6-sol xhigh",
        origin_session_id="origin-chat",
    )
    assert changed is True
    stored = metadata.get_meta("durable-worker")
    assert stored["group"] == "g_fleet123"
    assert stored["fleet_worker"]["worker_key"] == "codex:0"
    assert stored["fleet_worker"]["worker_label"] == "Codex 1"
    assert stored["fleet_worker"]["assignment"] == "parser ownership"
    assert stored["fleet_worker"]["worker_group_id"] == "g_fleet123"
    assert metadata.surface_fleet_worker(
        "durable-worker",
        run_id="fleet-123",
        leg_id="discover-codex",
        phase="discover",
        provider="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        worker_key="codex:0",
        worker_label="Codex 1",
        assignment="parser ownership",
        worker_group_id="g_fleet123",
        title="Fleet fleet-12 | codex | gpt-5.6-sol xhigh",
        origin_session_id="origin-chat",
    ) is False
