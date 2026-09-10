"""Crash boundaries and refusal rules for durable integration intent recovery."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fleet import integration_recovery as recovery, supervisor
from fleet.integration_journal import IntegrationJournal
from fleet.isolation import FleetIsolationStore, ensure_workspace, integrate_workspace, _workspace_patch, workspace_changed_paths
from fleet.resources import resume_ready_resource_waits
from test_fleet_integration_recovery import _failed
from test_fleet_isolation import _repo


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL fault injection")
@pytest.mark.parametrize("boundary", ["after_apply", "after_integration_receipt"])
def test_saved_replay_survives_after_apply_crash(tmp_path, monkeypatch, boundary):
    store, rid, *_ = _failed(tmp_path, monkeypatch)
    root = Path(store.get_run(rid)["cwd"])
    assert recovery.resume_saved_integrations(store) == [rid]
    normal_command = recovery.helper_command()
    monkeypatch.setattr(supervisor, "run_worker", lambda *a, **kw: pytest.fail("native provider dispatched"))
    script = "import os, signal\n"
    if boundary == "after_apply":
        script += (
            "from fleet import isolation\n"
            "def kill_at_gate(*a, **kw):\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
            "isolation.run_test_gate = kill_at_gate\n"
        )
    else:
        script += (
            "from fleet.store import FleetStore\n"
            "finish = FleetStore.finish_attempt\n"
            "def kill_at_completion(self, *a, **kw):\n"
            "    if kw.get('state') == 'completed':\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "    return finish(self, *a, **kw)\n"
            "FleetStore.finish_attempt = kill_at_completion\n"
        )
    script += "from fleet.integration_recovery import main\nraise SystemExit(main())\n"
    monkeypatch.setattr(recovery, "helper_command", lambda: [sys.executable, "-c", script])
    leg = store.get_run(rid)["phases"][3]["legs"][0]
    first = supervisor._execute_leg(store, rid, leg)
    assert first.exit_code == -9, first.error
    assert (root / "core/alpha.py").read_text() == "alpha = 2\n"
    assert resume_ready_resource_waits(store, now=time.time() + 31) == [leg["leg_id"]]
    monkeypatch.setattr(recovery, "helper_command", lambda: normal_command)
    retried = supervisor._execute_leg(store, rid, store.get_run(rid)["phases"][3]["legs"][0])
    current = store.get_run(rid)["phases"][3]["legs"][0]
    assert (root / "core/alpha.py").read_text() == "alpha = 2\n"
    assert current["current_attempt"]["actual_model"] is None
    assert retried.ok, f"{boundary}: {current['state']}: {retried.error}"
    assert current["state"] == "completed"
    accepted = [e for e in store.events(rid) if e["type"] == "worker.integration.accepted"]
    assert accepted[-1]["payload"]["test_gate"]["integration_journal"]["recovered_postimage"]


def _intent(tmp_path):
    root = _repo(tmp_path)
    store = FleetIsolationStore(tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "worktrees")
    store.claim_paths(run_id="journal", worker_key="agent:a", paths=["*"])
    workspace = ensure_workspace(store, run_id="journal", worker_key="agent:a", cwd=root)
    worker = Path(workspace.path)
    (worker / "core/alpha.py").write_text("alpha = 2\n")
    (worker / "new.bin").write_bytes(b"\x00\xffnew\r\n")
    paths = workspace_changed_paths(workspace)
    journal = IntegrationJournal(store, root, workspace, _workspace_patch(workspace, paths))
    journal.prepare(worker, paths, rollback_ref="retained-preimage-ref")
    return root, store, workspace, journal


def _integrate(root, store, **kwargs):
    return integrate_workspace(store, run_id="journal", worker_key="agent:a", cwd=root, **kwargs)


def test_mixed_exact_images_restore_then_apply_and_preserve_unrelated_files(tmp_path):
    root, store, _, journal = _intent(tmp_path)
    (root / "core/alpha.py").write_text("alpha = 2\n")
    (root / "unrelated.txt").write_text("user work")
    assert journal.state() == "mixed"
    result = _integrate(root, store)
    assert result.ok, result.reason
    assert result.test_gate["integration_journal"]["restored_mixed_preimage"]
    assert result.rollback_ref == "retained-preimage-ref"
    assert (root / "unrelated.txt").read_text() == "user work"
    assert (root / "new.bin").read_bytes() == b"\x00\xffnew\r\n"


def test_foreign_drift_refuses_before_restoring_any_paths(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    (root / "new.bin").write_bytes(b"\x00\xffnew\r\n")
    (root / "core/alpha.py").write_text("foreign edit")
    result = _integrate(root, store)
    assert not result.ok
    assert "foreign" in result.reason
    assert (root / "new.bin").exists()
    assert (root / "core/alpha.py").read_text() == "foreign edit"


def test_preview_never_restores_a_mixed_intent(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    (root / "core/alpha.py").write_text("alpha = 2\n")
    result = _integrate(root, store, apply_changes=False)
    assert not result.ok and "preview" in result.reason
    assert (root / "core/alpha.py").read_text() == "alpha = 2\n"
    assert not (root / "new.bin").exists()


def test_recovered_postimage_must_pass_gates_and_rolls_back_to_original_preimage(tmp_path):
    root, store, _, journal = _intent(tmp_path)
    assert _integrate(root, store).ok
    failed = _integrate(root, store, test_gate=[sys.executable, "-c", "raise SystemExit(7)"])
    assert not failed.ok and failed.test_gate["exit_code"] == 7
    assert journal.state() == "pre"
    assert (root / "core/alpha.py").read_text() == "alpha = 1\n"
    assert not (root / "new.bin").exists()


def test_gate_cannot_change_owned_bytes_and_claim_completion(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    result = _integrate(root, store, test_gate=[sys.executable, "-c",
                        "from pathlib import Path; Path('core/alpha.py').write_text('other edit')"])
    assert not result.ok
    assert (root / "core/alpha.py").read_text() == "other edit"


def test_corrupt_intent_refuses_without_touching_checkout(tmp_path):
    root, store, _, journal = _intent(tmp_path)
    with store._connect() as db:
        db.execute("UPDATE fleet_integration_intents SET digest='bad' WHERE intent_id=?", (journal.key,))
    result = _integrate(root, store)
    assert not result.ok and "fingerprint" in result.reason
    assert (root / "core/alpha.py").read_text() == "alpha = 1\n"


def test_failed_intent_commit_prevents_git_apply(tmp_path, monkeypatch):
    root, store, _, _ = _intent(tmp_path)
    with store._connect() as db:
        db.execute("DELETE FROM fleet_integration_intents")
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected journal write failure")
    monkeypatch.setattr(IntegrationJournal, "prepare", fail)
    with pytest.raises(sqlite3.OperationalError, match="journal write"):
        _integrate(root, store)
    assert (root / "core/alpha.py").read_text() == "alpha = 1\n"
    assert not (root / "new.bin").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symlink semantics")
def test_rollback_preserves_symlinks_modes_binary_and_deletions(tmp_path):
    root, store, workspace, journal = _intent(tmp_path)
    worker = Path(workspace.path)
    (root / "link").symlink_to("core/alpha.py")
    (worker / "link").symlink_to("new.bin")
    (root / "executable").write_bytes(b"\x00\xffbefore")
    (root / "executable").chmod(0o700)
    (worker / "executable").write_bytes(b"after")
    (root / "deleted").write_bytes(b"retain")
    paths = ["link", "executable", "deleted"]
    with store._connect() as db:
        db.execute("DELETE FROM fleet_integration_intents")
    journal.prepare(worker, paths)
    (root / "link").unlink()
    (root / "link").symlink_to("new.bin")
    (root / "executable").write_bytes(b"after")
    (root / "executable").chmod(0o644)
    (root / "deleted").unlink()
    assert journal.state() == "post"
    journal.restore_pre()
    assert (root / "link").is_symlink()
    assert os.readlink(root / "link") == "core/alpha.py"
    assert (root / "executable").stat().st_mode & 0o777 == 0o700
    assert (root / "executable").read_bytes() == b"\x00\xffbefore"
    assert (root / "deleted").read_bytes() == b"retain"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_parent_symlink_never_reads_or_writes_outside_checkout(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    (root / "core").rename(tmp_path / "outside")
    (root / "core").symlink_to(tmp_path / "outside", target_is_directory=True)
    result = _integrate(root, store)
    assert not result.ok and "redirected parent" in result.reason
    assert (tmp_path / "outside/alpha.py").read_text() == "alpha = 1\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL fault injection")
def test_death_during_rollback_recovers_remaining_mixed_images(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    assert _integrate(root, store).ok
    script = (
        "import os, signal, sys\n"
        "from fleet.isolation import FleetIsolationStore, integrate_workspace\n"
        "replace = os.replace\n"
        "def kill_after_replace(*args, **kwargs):\n"
        "    replace(*args, **kwargs)\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "os.replace = kill_after_replace\n"
        f"store = FleetIsolationStore({str(store.path)!r}, workspace_root={str(store.workspace_root)!r})\n"
        f"integrate_workspace(store, run_id='journal', worker_key='agent:a', cwd={str(root)!r}, "
        "test_gate=[sys.executable, '-c', 'raise SystemExit(7)'])\n"
    )
    child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert child.returncode == -9, child.stderr
    assert (root / "core/alpha.py").read_text() == "alpha = 1\n"
    assert (root / "new.bin").exists()
    reopened = FleetIsolationStore(store.path, workspace_root=store.workspace_root)
    result = _integrate(root, reopened)
    assert result.ok, result.reason
    assert result.test_gate["integration_journal"]["restored_mixed_preimage"]
    assert (root / "core/alpha.py").read_text() == "alpha = 2\n"


def test_explicit_run_deletion_removes_its_private_intent(tmp_path):
    _, store, _, journal = _intent(tmp_path)
    store.delete_run_records("journal")
    assert not journal.load()


def test_saved_patch_files_never_overwrite_an_earlier_receipt(tmp_path, monkeypatch):
    from fleet.isolation import _persist_patch
    _, store, _, _ = _intent(tmp_path)
    monkeypatch.setattr(time, "time", lambda: 1000)
    first = _persist_patch(store, "journal", "agent:a", "first patch\r\n")
    second = _persist_patch(store, "journal", "agent:a", "second patch\r\n")
    assert first and second and first != second
    assert Path(first).read_bytes() == b"first patch\r\n"
    assert Path(second).read_bytes() == b"second patch\r\n"
    if os.name != "nt":
        assert Path(first).stat().st_mode & 0o777 == 0o600


def test_unflushed_patch_cannot_be_referenced_as_durable(tmp_path, monkeypatch):
    from fleet.isolation import _persist_patch
    _, store, _, _ = _intent(tmp_path)
    def fail(fd):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(os, "fsync", fail)
    assert _persist_patch(store, "journal", "agent:a", "not durable") == ""


def test_target_branch_switch_cannot_create_a_new_intent(tmp_path):
    root, store, _, _ = _intent(tmp_path)
    subprocess.run(["git", "-C", str(root), "switch", "-c", "other-target"], check=True, capture_output=True)
    result = _integrate(root, store)
    assert not result.ok and "target branch changed" in result.reason
    assert (root / "core/alpha.py").read_text() == "alpha = 1\n"
    assert not (root / "new.bin").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("original,observed,cleans", [
    ("linux:1", "linux:1", True), ("linux:1", None, True),
    ("linux:1", "linux:2", False), (None, None, False),
])
def test_helper_cleanup_checks_pid_birth_identity(monkeypatch, original, observed, cleans):
    from fleet import workers
    monkeypatch.setattr("core.work_jobs.process_start_token", lambda pid: observed)
    calls = []
    monkeypatch.setattr(workers, "_terminate_process_group", lambda process: calls.append(process.pid))
    workers._cleanup_exited_group(SimpleNamespace(pid=987654), original)
    assert calls == ([987654] if cleans else [])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process-state verification")
@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("helper", [False, True])
def test_helper_exit_cleans_gates_with_private_pipes(tmp_path, monkeypatch, crash, helper):
    from fleet.workers import _stream_process
    from test_fleet_workers import _request
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('gate-ready').write_text('ready'); time.sleep(12)"
    )
    script = (
        "import os,signal,subprocess,sys,time; from pathlib import Path\n"
        "sys.stdin.read()\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "while not Path('gate-ready').exists(): time.sleep(0.01)\n"
        "print(child.pid,flush=True)\n"
        + ("os.kill(os.getpid(),signal.SIGKILL)\n" if crash else "")
    )
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "events"))
    seen = []
    result = _stream_process([sys.executable, "-c", script], request=_request(tmp_path, "codex"),
                             parse_stdout=lambda line: seen.append(int(line)),
                             cancel_requested=lambda: False, on_event=lambda *a: None,
                             **({"cleanup_exited_group": True} if helper else {}))
    assert result.exit_code == (-9 if crash else 0)
    assert len(seen) == 1
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{seen[0]}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            status = "gone"
        if status in {"Z", "gone"}:
            break
        time.sleep(0.02)
    assert status in {"Z", "gone"}, "private-pipe gate outlived its helper"
