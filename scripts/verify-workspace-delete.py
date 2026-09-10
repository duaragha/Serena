"""Exercise deletion against a real cross-process lease and disposable catalog."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def main():
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    with tempfile.TemporaryDirectory(prefix="workspace-delete-proof-") as temporary:
        root = Path(temporary)
        os.environ.update(HOME=str(root), XDG_DATA_HOME=str(root / "data"),
                          SERENA_RUNTIME_LEASE_DIR=str(root / "leases"))
        from core import indexer, metadata
        from core.parser import parse_metadata
        from core.workspace_lease import SessionOwnedError

        assert indexer.DB_PATH.resolve().is_relative_to(root)
        assert metadata.METADATA_DIR.resolve().is_relative_to(root)
        sid = str(uuid4())
        transcript = root / f"{sid}.jsonl"
        contents = json.dumps({"type": "user", "sessionId": sid, "cwd": str(root),
                               "timestamp": "2026-09-09T12:00:00Z",
                               "message": {"role": "user", "content": "Deletion proof"}}) + "\n"
        transcript.write_text(contents)
        metadata.set_custom_title(sid, "Recoverable proof")
        conn = indexer._get_db()
        try:
            indexer._upsert_session(conn, parse_metadata(transcript, "proof-project"))
            conn.commit()
        finally:
            conn.close()
        child = subprocess.Popen([sys.executable, "-c",
            "import sys; from core.workspace_lease import SessionLease; "
            "lease=SessionLease(sys.argv[1]); print('owned',flush=True); "
            "sys.stdin.readline(); lease.release()", sid], cwd=repo,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "owned"
            try:
                indexer.delete_session(sid, source="isolated-proof")
            except SessionOwnedError:
                pass
            else:
                raise AssertionError("Live owner did not prevent deletion")
            assert transcript.read_text() == contents and indexer.get_session(sid)
            assert metadata.get_meta(sid)["custom_title"] == "Recoverable proof"
            child.communicate("release\n", timeout=5)
            assert child.returncode == 0
            indexer.delete_session(sid, source="isolated-proof")
            assert not transcript.exists() and indexer.get_session(sid) is None
            recovery = indexer.DATA_DIR / "deleted-sessions" / sid
            assert (recovery / transcript.name).read_text() == contents
            assert json.loads((recovery / "recovery.json").read_text())["metadata"]["custom_title"] == "Recoverable proof"
            print("PASS: real cross-process owner blocked deletion without mutation; released owner allowed recoverable deletion from real SQLite catalog")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    main()
