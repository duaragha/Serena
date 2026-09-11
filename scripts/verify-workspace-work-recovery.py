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
        record = journal.command_record("persisted-session", "work:accepted-item:dispatch")
        journal.append("persisted-session", {"method": "workspace/workSubmitted", "params": {
            "threadId": "persisted-session", "requestId": "work:accepted-item:dispatch",
            "payload": record["payload"], "receipt": {
                "ok": True, "committed": True, "session_id": "persisted-session",
                "turn_id": "exact-turn", "start_offset": 12}}})
        journal.append("persisted-session", {"method": "turn/completed", "params": {
            "threadId": "persisted-session", "turn": {"id": "exact-turn", "status": "completed"}}})
        recovered = subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from core.workspace_journal import WorkspaceJournal
journal = WorkspaceJournal(Path(sys.argv[1]))
assert journal.recover_completed_work('persisted-session') == 1
receipt = journal.command_record('persisted-session', 'work:accepted-item:dispatch')['result']
assert receipt['turn_id'] == 'exact-turn' and receipt['start_offset'] == 12
assert journal.recover_completed_work('persisted-session') == 0
print('PASS: fresh process recovered exact completed receipt from synthetic durable evidence; no provider launched')
""", str(path)], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        print(recovered.stdout, end="")
        print(recovered.stderr, end="", file=sys.stderr)
        assert recovered.returncode == 0, recovered.returncode
        assert not journal.has_pending_work("persisted-session")


if __name__ == "__main__":
    main()
