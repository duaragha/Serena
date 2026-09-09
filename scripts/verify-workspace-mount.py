"""Read-only app mount proof. Imports routes, never attaches a coding session."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    enabled = "--enabled" in sys.argv
    with tempfile.TemporaryDirectory(prefix="serena-mount-proof-") as directory:
        os.environ["CHATS_DATA_DIR"] = directory
        os.environ["SERENA_STRUCTURED_WORKSPACE"] = "1" if enabled else "0"
        from ui.web import app

        host = app.extensions.get("workspace_host")
        assert bool(host) == enabled
        if host:
            assert host._loop is None
        with app.test_client() as client:
            response = client.get("/api/workspace/no-session/events", base_url="http://127.0.0.1")
            assert response.status_code == (403 if enabled else 404)
            root = client.get("/", base_url="http://127.0.0.1")
            assert root.status_code == 200
            expected = '"structuredWorkspace": ' + ("true" if enabled else "false")
            assert expected in root.text
        if host:
            assert host._loop is None
            host.shutdown()
        print(f"PASS: actual app structured mount enabled={enabled}; bootstrap flag matches")
        print("PASS: unauthenticated controls rejected; no owner loop or provider process started")


if __name__ == "__main__":
    main()
