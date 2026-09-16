"""Private writable scratch and test databases, never the operator's databases."""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def read_sandbox_flags(request) -> list[str]:
    """A read-only repository with one writable, attempt-private scratch root."""
    return ["-c", 'default_permissions="fleet_test_read"',
            "-c", 'permissions.fleet_test_read.extends=":read-only"',
            "-c", 'permissions.fleet_test_read.filesystem={' + json.dumps(str(runtime_directory(request))) + '="write"}']


def runtime_directory(request) -> Path:
    state = Path(os.environ.get("SERENA_FLEET_STATE_DIR") or
                 Path.home() / ".local/state/serena/fleet").expanduser().resolve()
    identity = hashlib.sha256(f"{request.run_id}:{request.attempt_id}".encode()).hexdigest()
    return state / "worker-runtime" / identity


def prepare_runtime(request) -> dict[str, str]:
    root = runtime_directory(request)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = root / "tmp"
    scratch.mkdir(exist_ok=True, mode=0o700)
    return {
        "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "SERENA_CONTROL_PLANE_DB_PATH": str(root / "control-plane.sqlite3"),
        "SERENA_NOTIFICATION_DB_PATH": str(root / "notifications.sqlite3"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SERENA_FLEET_TEST_RUNTIME": str(root),
    }


def preflight(request) -> dict:
    """Prove host provisioning before a paid turn; sandbox access is granted separately."""
    environment = prepare_runtime(request)
    # Use disposable probes, not the test databases: never migrate application
    # schemas here, and never touch inherited operator paths.
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=environment["TMPDIR"]) as probe:
        with open(Path(probe) / "write-check", "xb") as stream:
            stream.write(b"fleet-runtime-probe")
        db = sqlite3.connect(Path(probe) / "sqlite-check")
        try:
            db.execute("CREATE TABLE writable(value INTEGER)")
            db.execute("INSERT INTO writable VALUES (1)")
            db.commit()
            assert db.execute("SELECT value FROM writable").fetchone() == (1,)
        finally:
            db.close()
    return {"passed": True, "runtime_directory": environment["SERENA_FLEET_TEST_RUNTIME"],
            "checks": ["scratch_write", "sqlite_write"], "scope": "host_provisioning"}
