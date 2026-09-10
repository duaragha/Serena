"""Prove pending native dispatch survives process replacement without a provider."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_journal import WorkspaceJournal


def main():
    with tempfile.TemporaryDirectory(prefix="serena-work-restart-proof-") as root:
        path = Path(root) / "workspace.db"
        journal = WorkspaceJournal(path)
        journal.claim_command("persisted-session", "work:accepted-item:dispatch",
                              {"action": "work_submit", "start_offset": 12})
        child = subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
host = WorkspaceHost(journal=WorkspaceJournal(Path(sys.argv[1])), resolve=None)
try:
    record = host.journal.command_record('persisted-session', 'work:accepted-item:dispatch')
    assert record['payload']['start_offset'] == 12 and record['result'] is None
    try:
        host.attach('persisted-session')
    except ValueError as error:
        assert 'work dispatch is unconfirmed' in str(error), str(error)
    else:
        raise AssertionError('Pending dispatch was admitted')
    assert not host._sessions
    print('PASS: fresh process rejected persisted pending dispatch before resolver/provider creation')
finally:
    host.shutdown()
assert host._thread is None or not host._thread.is_alive()
""", str(path)], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        print(child.stdout, end="")
        print(child.stderr, end="", file=sys.stderr)
        assert child.returncode == 0, child.returncode
        assert journal.has_pending_work("persisted-session")
        print("PASS: original transcript offset and pending receipt preserved; child exited 0; no provider launched")


if __name__ == "__main__":
    main()
