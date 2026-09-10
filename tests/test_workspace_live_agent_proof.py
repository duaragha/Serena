"""Guard the opt-in proof against wasting time after a terminal provider failure."""

import json
import runpy
from pathlib import Path

import pytest


@pytest.fixture
def reject_finished_parent():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify-workspace-live-agent.py"))["reject_finished_parent"]


@pytest.mark.parametrize("status", ["failed", "completed", "interrupted"])
def test_terminal_parent_without_child_fails_immediately(reject_finished_parent, status):
    events = [{"method": "turn/completed", "params": {"turn": {
        "id": "parent-turn", "status": status, "error": {"message": "Authentication expired"}}}}]
    with pytest.raises(RuntimeError, match="Parent finished without a child.*Authentication expired"):
        reject_finished_parent(events, "parent-turn")


def test_child_or_unfinished_events_do_not_end_proof(reject_finished_parent):
    events = [
        {"method": "turn/started", "params": {"turn": {"id": "parent-turn"}}},
        {"method": "turn/completed", "params": {"turn": {"id": "other"}}},
        {"method": "workspace/agentEvent", "params": {"event": {"method": "turn/completed"}}},
    ]
    assert reject_finished_parent(events, "parent-turn") is None


@pytest.mark.parametrize("source", [None, "default", "active", "linked"])
def test_proof_never_reads_personal_auth_by_default(tmp_path, monkeypatch, source):
    monkeypatch.setenv("HOME", str(tmp_path))
    active = tmp_path / "active"
    monkeypatch.setenv("CODEX_HOME", str(active))
    normal = tmp_path / ".codex"
    normal.mkdir()
    (normal / "auth.json").write_text("not even parsed")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "auth.json").symlink_to(normal / "auth.json")
    read_auth = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify-workspace-live-agent.py"))["read_test_auth"]
    with pytest.raises(ValueError, match="separate"):
        read_auth({None: None, "default": normal, "active": active, "linked": linked}[source])
    assert (normal / "auth.json").read_text() == "not even parsed"


def test_proof_reads_explicit_separate_profile_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
    auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-test-token"}}
    raw = json.dumps(auth)
    (dedicated / "auth.json").write_text(raw)
    read_auth = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify-workspace-live-agent.py"))["read_test_auth"]
    assert read_auth(dedicated) == auth
    assert (dedicated / "auth.json").read_text() == raw
