from pathlib import Path
from types import SimpleNamespace

import pytest

from core import workspace_admission as admission


@pytest.mark.parametrize('argv', [['codex'], ['node', '/bin/codex']])
def test_idle_interactive_cli_is_not_project_ownership(monkeypatch, tmp_path, argv):
    transcript = tmp_path / 'exact.jsonl'
    process = SimpleNamespace(pid=999999, info={'name':'codex'}, cmdline=lambda:argv,
        terminal=lambda:'/dev/pts/1', cwd=lambda:str(tmp_path), open_files=lambda:[])
    monkeypatch.setattr(admission.psutil, 'process_iter', lambda attrs:[process])
    monkeypatch.setattr(admission, '_registered_other_runtime', lambda *args:False)
    admission.reject_unregistered_codex('exact', tmp_path, transcript)
    process.open_files = lambda:[SimpleNamespace(path=str(transcript))]
    with pytest.raises(RuntimeError, match='transcript is already open'):
        admission.reject_unregistered_codex('exact', tmp_path, transcript)
    process.open_files = lambda:[]
    process.terminal = lambda:None
    with pytest.raises(RuntimeError, match='unregistered'):
        admission.reject_unregistered_codex('exact', tmp_path, transcript)
    process.terminal = lambda:'/dev/pts/1'
    process.cmdline = lambda:['codex', 'app-server']
    with pytest.raises(RuntimeError, match='unregistered'):
        admission.reject_unregistered_codex('exact', tmp_path, transcript)
