from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from core import brain_state, brain_state_sync


def _ledger(**overrides):
    record = {
        "id": 396,
        "type": "ledger",
        "ledger_key": "gideon",
        "goal": "build the brain",
        "facts": "voice is local",
        "decision": "keep sonnet",
        "promise": "",
        "risk": "phone offline",
        "next_action": "calibrate wake word",
        "content": "",
        "created_at": "2026-07-16T10:00:00Z",
        "updated_at": "2026-07-16T11:00:00Z",
    }
    record.update(overrides)
    return record


def test_snapshot_round_trip_and_hash_validation(tmp_path: Path) -> None:
    snapshot = brain_state.build_snapshot(
        [_ledger(), {"id": 4, "type": "task", "content": "ship it"}],
        generated_at="2026-07-16T12:00:00+00:00",
        source_host="laptop",
    )
    path = tmp_path / "state.json"
    brain_state.atomic_write_snapshot(snapshot, path)

    loaded = brain_state.read_snapshot(path)

    assert loaded.source == "snapshot"
    assert loaded.source_host == "laptop"
    assert loaded.generated_at == "2026-07-16T12:00:00+00:00"
    assert [record["type"] for record in loaded.records] == ["ledger", "task"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["goal"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        brain_state.read_snapshot(path)
    except ValueError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("tampered state snapshot was accepted")


def test_explicit_snapshot_never_falls_back_to_stale_local_memory(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setenv(brain_state.SNAPSHOT_ENV, str(missing))
    monkeypatch.setattr(
        brain_state,
        "local_active_records",
        lambda: (_ledger(goal="stale deployment copy"),),
    )

    state = brain_state.active_state()

    assert state.records == ()
    assert state.source == "snapshot"
    assert "unavailable" in state.error
    assert "stale deployment copy" not in brain_state.compact_active(state)
    assert "unavailable" in brain_state.format_ledgers("gideon", state)


def test_compact_and_ledger_rendering_from_snapshot() -> None:
    records = (
        _ledger(),
        brain_state._clean_record(
            {"id": 7, "type": "task", "content": "test the physical phone"}
        ),
        brain_state._clean_record(
            {"id": 8, "type": "loop", "content": "week-long wake audit"}
        ),
    )
    state = brain_state.ActiveState(records=records, source="snapshot")

    compact = brain_state.compact_active(state)
    ledger = brain_state.format_ledgers("gideon", state)

    assert "gideon: goal=build the brain | facts=voice is local" in compact
    assert "decision=keep sonnet" in compact
    assert "risk=phone offline" in compact
    assert "next=calibrate wake word" in compact
    assert "test the physical phone" in compact
    assert "week-long wake audit" in compact
    assert "facts: voice is local" in ledger
    assert "risk: phone offline" in ledger


def test_sync_hash_must_match_remote_atomic_write(monkeypatch) -> None:
    payload = brain_state.encode_snapshot(
        brain_state.build_snapshot(
            [_ledger()], generated_at="2026-07-16T12:00:00+00:00"
        )
    )
    digest = __import__("hashlib").sha256(payload).hexdigest()
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"sha256": digest, "path": "state.json", "bytes": len(payload)}
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(brain_state_sync.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(brain_state_sync.subprocess, "run", run)

    result = brain_state_sync.push_snapshot("docker-pc", payload)

    assert captured["input"] == __import__("base64").b64encode(payload)
    assert captured["command"][0] == "/usr/bin/ssh"
    assert "-EncodedCommand" in captured["command"]
    assert result["sha256"] == digest


def test_sync_rejects_remote_hash_mismatch(monkeypatch) -> None:
    payload = b'{"version":1}\n'
    monkeypatch.setattr(brain_state_sync.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(
        brain_state_sync.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=b'{"sha256":"wrong","path":"state.json","bytes":14}',
            stderr=b"",
        ),
    )

    try:
        brain_state_sync.push_snapshot("docker-pc", payload)
    except RuntimeError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("remote hash mismatch was accepted")
