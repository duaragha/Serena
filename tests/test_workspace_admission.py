import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import workspace_admission as admission


@pytest.mark.parametrize('owner', ['same', 'other', 'both', 'child', 'unknown'])
def test_muse_native_writer_locks_identify_exact_owner(tmp_path, monkeypatch, owner):
    from core import muse_scanner

    root = tmp_path / 'muse' / 'sessions'
    target = root / '2026/09/16/exact/session.jsonl'
    other = root / '2026/09/16/other/.session.lock'
    own = target.with_name('.session.lock')
    paths = {'same': [own], 'other': [other], 'both': [other, own],
             'child': [target.parent / 'subagent/child/.session.lock'],
             'unknown': [tmp_path / '.session.lock']}[owner]
    process = SimpleNamespace(pid=12345, info={'name': 'muse-bin-1.3.0'},
                              cmdline=lambda: ['/bin/muse-bin-1.3.0'],
                              cwd=lambda: str(tmp_path),
                              open_files=lambda: [SimpleNamespace(path=str(p)) for p in paths])
    monkeypatch.setattr(muse_scanner, 'SESSIONS_DIR', root)
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs: [process])
    if owner in {'same', 'both'}:
        with pytest.raises(RuntimeError, match='PID 12345.*current Muse terminal or Serena window.*Retry connection'):
            admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')
        # The lock is decisive even if the terminal changed directories.
        process.cwd = lambda: '/different-project'
        with pytest.raises(RuntimeError, match='still open'):
            admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')
    elif owner == 'other':
        admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')
        process.open_files = lambda: [SimpleNamespace(path=str(p)) for p in [other, target]]
        with pytest.raises(RuntimeError, match='transcript'):
            admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')
    else:
        with pytest.raises(RuntimeError, match='unregistered'):
            admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')


def test_muse_closed_terminal_allows_retry_without_backend_restart(tmp_path, monkeypatch):
    from core import muse_scanner

    root = tmp_path / 'muse/sessions'
    target = root / '2026/09/16/exact/session.jsonl'
    process = SimpleNamespace(pid=12345, info={'name': 'muse'}, cmdline=lambda: ['muse'],
                              cwd=lambda: str(tmp_path),
                              open_files=lambda: [SimpleNamespace(path=str(target.with_name('.session.lock')))])
    processes = [process]
    monkeypatch.setattr(muse_scanner, 'SESSIONS_DIR', root)
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs: processes)
    with pytest.raises(RuntimeError, match='still open'):
        admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')
    processes.clear()
    admission.reject_unregistered_provider('exact', tmp_path, target, 'muse')


@pytest.mark.parametrize('kind', ['root', 'child', 'helper'])
@pytest.mark.parametrize('lease_state', ['other', 'same', 'reused', 'dead_host'])
def test_registered_runtime_identity_does_not_block_unrelated_chat(session, monkeypatch, tmp_path, kind, lease_state):
    root = SimpleNamespace(pid=321, create_time=lambda: 1.0, parent=lambda: None, cmdline=lambda: ['node', '/bin/codex'])
    child = SimpleNamespace(pid=322, create_time=lambda: 2.0, parent=lambda: root, cmdline=lambda: ['codex', 'app-server'])
    process = root if kind == 'root' else child if kind == 'child' else SimpleNamespace(
        pid=323, create_time=lambda: 3.0, parent=lambda: child)
    process.info = {'name': 'codex-code-mode-host' if kind == 'helper' else 'codex'}
    if kind == 'helper':
        process.cmdline = lambda: ['codex-code-mode-host']
    process.cwd = lambda: session['cwd']
    process.open_files = lambda: []
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs: [process])
    monkeypatch.setattr(admission.psutil, 'Process', lambda pid: SimpleNamespace(
        create_time=lambda: 4.0, status=lambda: admission.psutil.STATUS_ZOMBIE if lease_state == 'dead_host' else 'running'))
    monkeypatch.setenv('SERENA_RUNTIME_LEASE_DIR', str(tmp_path))
    sid = 'exact' if lease_state == 'same' else 'other'
    (tmp_path / (hashlib.sha256(sid.encode()).hexdigest() + '.json')).write_text(json.dumps({
        'phase': 'bound', 'child': {'pid': 321, 'born': 9.0 if lease_state == 'reused' else 1.0},
        'owner': {'pid': 320, 'born': 4.0}}))
    if lease_state == 'other':
        assert admission.resolve_workspace_session('exact')['session_id'] == 'exact'
        process.open_files = lambda: [SimpleNamespace(path=session['file_path'])]
        with pytest.raises(RuntimeError, match='transcript'):
            admission.resolve_workspace_session('exact')
    else:
        with pytest.raises(RuntimeError, match='unregistered'):
            admission.resolve_workspace_session('exact')


@pytest.mark.parametrize('argv', [
    ['claude', '--resume=different'],
    ['node', str(Path(admission.__file__).with_name('workspace_claude_worker.mjs')), 'sdk', 'claude', 'different', '/project'],
])
def test_explicit_other_claude_runtime_does_not_block(session, monkeypatch, argv):
    session['agent'] = 'claude'
    process = SimpleNamespace(pid=12345, info={'name': 'claude'}, cmdline=lambda: argv,
                              cwd=lambda: session['cwd'], open_files=lambda: [])
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs: [process])
    assert admission.resolve_workspace_session('exact')['provider'] == 'claude'


def test_shell_command_mentioning_provider_is_not_a_runtime(session, monkeypatch):
    process = SimpleNamespace(pid=12345, info={'name': 'bash'},
                              cmdline=lambda: ['bash', '-lc', 'echo codex'],
                              cwd=lambda: session['cwd'], open_files=lambda: [])
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs: [process])
    assert admission.resolve_workspace_session('exact')['session_id'] == 'exact'


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
        "archived": False,
    }
    with pytest.raises(ValueError, match="Exact"):
        admission.resolve_workspace_session("prefix")
    session["agent"] = "gemini"
    with pytest.raises(ValueError, match="native Gemini conversation"):
        admission.resolve_workspace_session("exact")
    session["agent"] = "codex"
    session["cwd"] = "/missing-workspace-directory"
    with pytest.raises(ValueError, match="unavailable"):
        admission.resolve_workspace_session("exact")


def test_archive_state_is_preserved_for_host_admission(session):
    session["is_archived"] = 1
    assert admission.resolve_workspace_session("exact")["archived"] is True


def test_claude_storage_project_survives_working_directory_change(session, tmp_path):
    session['agent'] = 'claude'
    latest = tmp_path / 'nested-project'
    latest.mkdir()
    session['last_cwd'] = str(latest)
    target = admission.resolve_workspace_session('exact')
    assert target['cwd'] == str(latest)
    assert target['session_directory'] == session['cwd']


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


@pytest.mark.parametrize("name,argv", [("localharness_ex", ["worker"]),
    ("worker", ["/opt/google/localharness_external"]), ("agy_acp_server", ["agy_acp_server.par"])])
def test_surviving_google_harness_blocks_ambiguous_session_attachment(session, monkeypatch, name, argv):
    process = SimpleNamespace(pid=12345, info={"name": name}, cmdline=lambda: argv,
                              cwd=lambda: session["cwd"], open_files=lambda: [])
    monkeypatch.setattr(admission.psutil, "process_iter", lambda attrs: [process])
    cwd, transcript = Path(session["cwd"]), Path(session["file_path"])
    with pytest.raises(RuntimeError, match="unregistered"):
        admission.reject_unregistered_provider("exact", cwd, transcript, "agy")
    process.cwd = lambda: str(cwd / "another-project")
    admission.reject_unregistered_provider("exact", cwd, transcript, "agy")
    process.open_files = lambda: [SimpleNamespace(path=str(transcript))]
    with pytest.raises(RuntimeError, match="transcript"):
        admission.reject_unregistered_provider("exact", cwd, transcript, "agy")
    def denied():
        raise admission.psutil.AccessDenied(process.pid)
    process.open_files = denied
    with pytest.raises(RuntimeError, match="Cannot verify"):
        admission.reject_unregistered_provider("exact", cwd, transcript, "agy")


def test_gemini_native_store_is_admitted_without_acp_migration(session, monkeypatch):
    from core import gemini_scanner
    session["agent"] = "gemini"
    monkeypatch.setattr(gemini_scanner, 'resumable_conversation_path', lambda sid: Path(session['file_path']))
    assert admission.resolve_workspace_session('exact')['provider'] == 'gemini'


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
