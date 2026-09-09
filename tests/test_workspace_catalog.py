import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_catalog import register_fork


def test_codex_registration_marks_owned_before_upsert(tmp_path, monkeypatch):
    sid = str(uuid4())
    home = tmp_path / "codex"
    path = home / "sessions" / f"rollout-2026-09-09T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}) + "\n")
    calls = []
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("core.metadata.set_resident_work", lambda target: calls.append(("owned", target)))
    monkeypatch.setattr("core.indexer._index_update_lock", nullcontext)
    monkeypatch.setattr("core.indexer._get_db", lambda: SimpleNamespace(commit=lambda: calls.append("commit"), close=lambda: calls.append("close")))
    monkeypatch.setattr("core.indexer._upsert_session", lambda conn, meta, agent: calls.append(("index", meta.session_id, agent)))
    register_fork({"session_id": sid, "provider": "codex", "cwd": str(tmp_path)})
    assert calls == [("owned", sid), ("index", sid, "codex"), "commit", "close"]


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-project", "wrong-id", "outside", "missing-history"])
def test_invalid_codex_fork_never_writes_catalog(tmp_path, monkeypatch, case):
    sid = str(uuid4())
    home = tmp_path / "codex"
    path = home / "sessions" / "2026" / f"rollout-2026-09-09T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    payload = {"id": str(uuid4()) if case == "wrong-id" else sid,
               "cwd": str(tmp_path / "wrong") if case == "wrong-project" else str(tmp_path)}
    if case == "missing-history":
        payload["history_base"] = {"thread_id": str(uuid4()), "end_byte_offset": 1, "end_ordinal_exclusive": 1}
    if case != "missing":
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
    if case == "ambiguous":
        other = home / "archived_sessions" / path.name
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
    if case == "outside":
        outside = tmp_path / "outside.jsonl"
        path.rename(outside)
        path.symlink_to(outside)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("core.indexer._get_db", lambda: pytest.fail("Invalid fork reached catalog write"))
    with pytest.raises(ValueError):
        register_fork({"session_id": sid, "provider": "codex", "cwd": str(tmp_path)})


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-project", "wrong-id", "outside"])
def test_invalid_native_fork_never_writes_catalog(tmp_path, monkeypatch, case):
    sid = str(uuid4())
    config = tmp_path / "config"
    projects = config / "projects"
    path = projects / "project" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    if case != "missing":
        path.write_text("{}\n")
    if case == "ambiguous":
        other = projects / "other" / path.name
        other.parent.mkdir()
        other.write_text("{}\n")
    if case == "outside":
        outside = tmp_path / "external.jsonl"
        outside.write_text("{}\n")
        path.unlink()
        path.symlink_to(outside)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr("core.parser.parse_metadata", lambda *args: SimpleNamespace(
        session_id=str(uuid4()) if case == "wrong-id" else sid,
        cwd=str(tmp_path / "wrong") if case == "wrong-project" else str(tmp_path),
    ))
    monkeypatch.setattr("core.indexer._get_db", lambda: pytest.fail("Invalid fork reached catalog write"))
    with pytest.raises(ValueError):
        register_fork({"session_id": sid, "provider": "claude", "cwd": str(tmp_path)})
