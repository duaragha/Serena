"""Guard the opt-in proof against wasting time after a terminal provider failure."""

import json
import runpy
import subprocess
import sys
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


@pytest.mark.parametrize("script", ["verify-workspace-live-agent.py", "verify-workspace-account.py"])
@pytest.mark.parametrize("source", [None, "default", "active", "linked", "hardlinked"])
def test_proof_never_reads_personal_auth_by_default(tmp_path, monkeypatch, source, script):
    monkeypatch.setenv("HOME", str(tmp_path))
    active = tmp_path / "active"
    monkeypatch.setenv("CODEX_HOME", str(active))
    normal = tmp_path / ".codex"
    normal.mkdir()
    (normal / "auth.json").write_text("not even parsed")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "auth.json").symlink_to(normal / "auth.json")
    hardlinked = tmp_path / "hardlinked"
    hardlinked.mkdir()
    (hardlinked / "auth.json").hardlink_to(normal / "auth.json")
    read_auth = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / script))["read_test_auth"]
    with pytest.raises(ValueError, match="separate"):
        read_auth({None: None, "default": normal, "active": active, "linked": linked, "hardlinked": hardlinked}[source])
    assert (normal / "auth.json").read_text() == "not even parsed"


@pytest.mark.parametrize("script", ["verify-workspace-live-agent.py", "verify-workspace-account.py"])
def test_proof_reads_explicit_separate_profile_without_rewriting(tmp_path, monkeypatch, script):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
    auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-test-token"}}
    raw = json.dumps(auth)
    (dedicated / "auth.json").write_text(raw)
    read_auth = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / script))["read_test_auth"]
    assert read_auth(dedicated) == auth
    assert (dedicated / "auth.json").read_text() == raw


@pytest.mark.parametrize("flag", ["--signed-limits", "--signed-apps"])
def test_signed_account_proof_rejects_missing_profile_before_launch(flag):
    script = Path(__file__).resolve().parents[1] / "scripts/verify-workspace-account.py"
    result = subprocess.run([sys.executable, str(script), flag], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--auth-home is required" in result.stderr


@pytest.mark.parametrize("flag", ["signed_limits", "signed_apps"])
def test_account_proof_rejects_personal_profile_before_native_owner(tmp_path, monkeypatch, flag):
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    script = Path(__file__).resolve().parents[1] / "scripts/verify-workspace-account.py"
    main = runpy.run_path(str(script))["main"]
    with pytest.raises(ValueError, match="separate"):
        asyncio.run(main(**{flag: True}, auth_home=tmp_path / ".codex"))


@pytest.mark.parametrize("script", [
    "verify-workspace-mcp.py", "verify-workspace-codex-roundtrip.py", "verify-workspace-native-work.py",
])
def test_other_inference_proofs_require_explicit_profile(script):
    path = Path(__file__).resolve().parents[1] / "scripts" / script
    result = subprocess.run([sys.executable, str(path), "--allow-inference"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--auth-home" in result.stderr
