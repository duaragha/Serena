"""Read-only app mount proof. Imports routes, never attaches a coding session."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    if "--enabled" in sys.argv and "--disabled" in sys.argv:
        raise SystemExit("choose at most one of --enabled or --disabled")
    if "--enabled" in sys.argv:
        os.environ["SERENA_STRUCTURED_WORKSPACE"] = "1"
        expected_enabled = True
        mode = "explicit-on"
    elif "--disabled" in sys.argv:
        os.environ["SERENA_STRUCTURED_WORKSPACE"] = "0"
        expected_enabled = False
        mode = "explicit-off"
    else:
        os.environ.pop("SERENA_STRUCTURED_WORKSPACE", None)
        expected_enabled = True
        mode = "default"
    with tempfile.TemporaryDirectory(prefix="serena-mount-proof-") as directory:
        os.environ["CHATS_DATA_DIR"] = directory
        from ui.web import app

        host = app.extensions.get("workspace_host")
        assert bool(host) == expected_enabled
        if host:
            assert host._loop is None
        with app.test_client() as client:
            response = client.get("/api/workspace/no-session/events", base_url="http://127.0.0.1")
            assert response.status_code == (403 if expected_enabled else 404)
            root = client.get("/", base_url="http://127.0.0.1")
            assert root.status_code == 200
            expected = '"structuredWorkspace": ' + (
                "true" if expected_enabled else "false"
            )
            assert expected in root.text
        if host:
            assert host._loop is None
            host.shutdown()
        print(
            f"PASS: actual app structured mount mode={mode} "
            f"enabled={expected_enabled}; bootstrap flag matches"
        )
        print("PASS: unauthenticated controls rejected; no owner loop or provider process started")


if __name__ == "__main__":
    main()
