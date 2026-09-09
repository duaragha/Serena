"""Crash real disposable processes; prove no lease while orphan tools survive."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_lease import SessionLease, SessionOwnedError, _runtime_alive


def refused(directory):
    try:
        lease = SessionLease("crash-proof", directory=directory)
    except SessionOwnedError:
        return
    lease.release()
    raise AssertionError("A surviving process allowed a competing writer")


if os.name == "nt":
    raise SystemExit("This proof requires POSIX process groups; Windows is not verified")

leader_code = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
time.sleep(60)
"""
owner_code = """
import json, os, subprocess, sys
from pathlib import Path
from core.workspace_lease import SessionLease
lease = SessionLease('crash-proof', directory=Path(sys.argv[1]))
lease.launching()
leader = subprocess.Popen([sys.executable, '-c', sys.argv[2]], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
tool = int(leader.stdout.readline())
lease.bind(leader.pid)
print(json.dumps({'leader': leader.pid, 'tool': tool, 'record': lease.record}), flush=True)
os._exit(0)
"""
with tempfile.TemporaryDirectory(prefix="serena-lease-crash-proof-") as temporary:
    directory = Path(temporary)
    group = None
    try:
        result = subprocess.run([sys.executable, "-c", owner_code, temporary, leader_code],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        state = json.loads(result.stdout)
        group = state["leader"]
        assert state["record"]["process_group"] == group
        refused(directory)
        print("PASS: owner crashed without releasing its lease; surviving isolated runtime blocks reacquisition")
        os.kill(group, signal.SIGKILL)
        deadline = time.monotonic() + 5
        from core.workspace_lease import _alive
        while _alive(state["record"]["child"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _alive(state["record"]["child"])
        refused(directory)
        print("PASS: dead runtime leader with a live orphan tool still blocks a second writer")
        os.killpg(group, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _runtime_alive(state["record"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _runtime_alive(state["record"])
        lease = SessionLease("crash-proof", directory=directory)
        lease.release()
        print("PASS: exact session lease recovers only after every live group member exits; no provider or user session launched")
    finally:
        if group:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
