from types import SimpleNamespace

import pytest

from core import workspace_admission as admission


@pytest.fixture
def session(tmp_path, monkeypatch):
    from core import indexer, metadata
    from ui import pty_terminal

    transcript = tmp_path / "rollout-exact.jsonl"
    transcript.write_text("{}\n")
    row = {
        "session_id": "exact",
        "agent": "codex",
        "cwd": str(tmp_path),
        "file_path": str(transcript),
    }
    monkeypatch.setattr(indexer, "get_session", lambda sid: row)
    monkeypatch.setattr(metadata, "get_meta", lambda sid: {})
    monkeypatch.setattr(metadata, "external_runtime_active", lambda sid: False)
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda sid: None)
    monkeypatch.setattr(admission.psutil, "process_iter", lambda attrs: [])
    return row


def test_exact_session_only_no_cwd_guess_or_provider_substitution(session):
    assert admission.resolve_workspace_session("exact") == {
        "session_id": "exact",
        "provider": "codex",
        "cwd": session["cwd"],
    }
    with pytest.raises(ValueError, match="Exact"):
        admission.resolve_workspace_session("prefix")
    session["agent"] = "gemini"
    with pytest.raises(ValueError, match="interactive approvals"):
        admission.resolve_workspace_session("exact")
    session["agent"] = "codex"
    session["cwd"] = "/missing-workspace-directory"
    with pytest.raises(ValueError, match="unavailable"):
        admission.resolve_workspace_session("exact")


def test_existing_pty_and_external_owner_rejected(session, monkeypatch):
    from core import metadata
    from ui import pty_terminal

    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda sid: "live-pty")
    with pytest.raises(RuntimeError, match="live terminal"):
        admission.resolve_workspace_session("exact")
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda sid: None)
    monkeypatch.setattr(metadata, "external_runtime_active", lambda sid: True)
    with pytest.raises(RuntimeError, match="worker"):
        admission.resolve_workspace_session("exact")


def test_manual_exact_or_unknown_project_owner_blocks_migration(session, monkeypatch):
    class Process:
        pid = 12345
        info = {"name": "codex"}
        argv = ["codex", "resume", "exact"]

        def cmdline(self):
            return self.argv

        def cwd(self):
            return session["cwd"]

        def open_files(self):
            return []

    process = Process()
    monkeypatch.setattr(admission.psutil, "process_iter", lambda attrs: [process])
    with pytest.raises(RuntimeError, match="already has"):
        admission.resolve_workspace_session("exact")
    process.argv = ["codex", "app-server"]
    with pytest.raises(RuntimeError, match="unregistered"):
        admission.resolve_workspace_session("exact")
    process.argv = ["codex", "resume", "another-exact-id"]
    assert admission.resolve_workspace_session("exact")["session_id"] == "exact"
    process.open_files = lambda: [SimpleNamespace(path=session["file_path"])]
    with pytest.raises(RuntimeError, match="transcript"):
        admission.resolve_workspace_session("exact")


def test_registry_read_failure_does_not_assume_unowned(session, monkeypatch):
    from core import metadata

    def broken(sid):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(metadata, "get_meta", broken)
    with pytest.raises(OSError):
        admission.resolve_workspace_session("exact")


def test_gemini_fidelity_rejection_precedes_runtime_side_effects(session, monkeypatch):
    from core import metadata

    session["agent"] = "gemini"

    def unexpected(*args):
        raise AssertionError("Unsupported provider must not enter runtime admission")

    monkeypatch.setattr(metadata, "get_meta", unexpected)
    with pytest.raises(ValueError, match="has not been opened or changed"):
        admission.resolve_workspace_session("exact")


@pytest.mark.parametrize("switch", ["-r", "--resume", "--resume=exact"])
def test_claude_existing_process_and_unknown_owner_rejected(session, monkeypatch, switch):
    session["agent"] = "claude"
    assert admission.resolve_workspace_session("exact")["provider"] == "claude"
    process = SimpleNamespace(
        pid=12345,
        info={"name": "node"},
        cmdline=lambda: ["node", "/bin/claude", switch, "exact"],
        cwd=lambda: session["cwd"],
        open_files=lambda: [],
    )
    monkeypatch.setattr(admission.psutil, "process_iter", lambda attrs: [process])
    with pytest.raises(RuntimeError, match="already has"):
        admission.resolve_workspace_session("exact")
    process.cmdline = lambda: ["node", "/bin/claude", "--resume", "different"]
    assert admission.resolve_workspace_session("exact")["provider"] == "claude"
    process.cmdline = lambda: ["node", "/bin/claude", "--input-format", "stream-json"]
    with pytest.raises(RuntimeError, match="unregistered"):
        admission.resolve_workspace_session("exact")

    def denied():
        raise admission.psutil.AccessDenied(12345)

    process.open_files = denied
    with pytest.raises(RuntimeError, match="Cannot verify"):
        admission.resolve_workspace_session("exact")
