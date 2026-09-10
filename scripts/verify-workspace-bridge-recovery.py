"""Crash an isolated host with a real paused CLI; restore only unsent work."""
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


def main():
    sdk, cli, node, root, sid = sys.argv[1:6]
    assert Path(os.environ["HOME"]).resolve() == Path(root).resolve()
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    root = Path(root)
    journal = WorkspaceJournal(root / "crash-bridge.db")
    ready = root / "crash-bridge-ready.json"
    def client_factory(*, options):
        options.cli_path = cli
        return ClaudeTypeScriptClient(options=options, sdk_path=sdk, node_path=node)
    def factory(**kwargs):
        return ClaudeWorkspace(**kwargs, client_factory=client_factory,
                               lease_factory=lambda target: SessionLease(target, directory=root / "crash-leases"))
    def host():
        return WorkspaceHost(journal=journal, resolve=lambda target: {"session_id": target, "provider": "claude", "cwd": str(root)},
                             factories={"claude": factory})
    if "--worker" in sys.argv:
        value = host()
        assert value.attach(sid)["ok"]
        pid = value._sessions[sid][0].client.owned_pid
        os.kill(pid, signal.SIGSTOP)
        assert value.bridge(sid, "claude", "/effort low", "in-flight", timeout=0.02)["pending"]
        assert value.bridge(sid, "claude", "/effort high", "queued", timeout=1)["queued"]
        children = [{"pid": child.pid, "born": child.create_time()} for child in psutil.Process().children(recursive=True)]
        ready.write_text(json.dumps({"native": pid, "children": children}))
        while True:
            time.sleep(1)
    process = subprocess.Popen([sys.executable, __file__, *sys.argv[1:6], "--worker"], env=dict(os.environ), start_new_session=True)
    children = []
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert process.poll() is None, "Crash worker exited before ready"
            assert time.monotonic() < deadline, "Crash worker did not become ready"
            time.sleep(0.02)
        snapshot = json.loads(ready.read_text())
        children = snapshot["children"]
        assert journal.recoverable_bridge_queue(sid, "claude") == [{"id": "queued", "prompt": "/effort high"}]
    finally:
        if process.poll() is None:
            # Capture even startup failures, before terminating the owning host.
            if not children:
                children = [{"pid": child.pid, "born": child.create_time()} for child in psutil.Process(process.pid).children(recursive=True)]
            process.kill()
        process.wait(timeout=5)
        for identity in reversed(children):
            with suppress(psutil.NoSuchProcess):
                child = psutil.Process(identity["pid"])
                if child.create_time() == identity["born"]:
                    child.kill()
        deadline = time.monotonic() + 5
        for identity in children:
            while True:
                try:
                    child = psutil.Process(identity["pid"])
                    if child.create_time() != identity["born"] or child.status() == psutil.STATUS_ZOMBIE:
                        break
                except psutil.NoSuchProcess:
                    break
                assert time.monotonic() < deadline, "Crash child remained live"
                time.sleep(0.02)
    restored = host()
    native_pid = None
    try:
        assert restored.bridge(sid, "claude", "/effort high", "queued")["pending"]
        assert restored._loop is None and not restored._sessions
        assert restored.attach(sid)["ok"]
        native_pid = restored._sessions[sid][0].client.owned_pid
        assert native_pid != snapshot["native"]
        deadline = time.monotonic() + 20
        while True:
            result = restored.bridge(sid, "claude", "/effort high", "queued")
            if not result.get("pending"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert result["ok"] and "high" in result["response"].lower() and result["session_id"] == sid
        assert restored.bridge(sid, "claude", "/effort low", "in-flight")["pending"]
        complete = [entry["event"] for entry in journal.read(sid)["events"] if entry["event"]["method"] == "turn/completed"]
        assert len(complete) == 1 and complete[0]["params"]["turn"]["providerOriginal"]["total_cost_usd"] == 0
        assert journal.recoverable_bridge_queue(sid, "claude") == []
        print("PASS: killed real host with paused native CLI; explicit exact-session resume restored only queued local turn, never uncertain input; zero inference")
    finally:
        restored.shutdown()
    assert native_pid and not psutil.pid_exists(native_pid)
    print("PASS: recovery owner and crash children are no longer live")


main()
