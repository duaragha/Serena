"""Disposable source/frozen-helper crash probe; never targets a live Fleet."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import sys
import time

import psutil
import pytest

from core.work_jobs import process_start_token
from fleet import integration_recovery as recovery, supervisor
from fleet.resources import resume_ready_resource_waits
from test_fleet_integration_recovery import _failed


def test_external_helper_kill_preserves_applied_patch_and_reaps_gate(tmp_path, monkeypatch):
    binary = os.environ.get('SERENA_FLEET_TEST_REPLAY_BINARY')
    if binary:
        assert Path(binary).is_file()
        monkeypatch.setattr(recovery, 'helper_command', lambda: [binary, '--fleet-integration-replay'])
    store, run_id, *_ = _failed(tmp_path, monkeypatch)
    root = Path(store.get_run(run_id)['cwd'])
    assert recovery.resume_saved_integrations(store) == [run_id]
    marker = tmp_path / 'gate-entered'
    gate = (
        'import os,time; from pathlib import Path; '
        f'p=Path({str(marker)!r}); already=p.exists(); pending=p.with_suffix(".tmp"); '
        'pending.write_text(str(os.getppid())+":"+str(os.getpid())) if not already else None; '
        'pending.replace(p) if not already else None; '
        'time.sleep(20) if not already else None'
    )
    monkeypatch.setenv('SERENA_FLEET_INTEGRATION_TEST_COMMAND', shlex.join([sys.executable, '-c', gate]))
    monkeypatch.setattr(supervisor, 'run_worker', lambda *a, **kw: pytest.fail('native model dispatched'))
    leg = store.get_run(run_id)['phases'][3]['legs'][0]
    gate_process = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supervisor._execute_leg, store, run_id, leg)
            deadline = time.monotonic() + 30
            while not marker.exists() and not future.done() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), future.result(timeout=5) if future.done() else 'gate never entered'
            current = store.get_run(run_id)['phases'][3]['legs'][0]['current_attempt']
            helper_pid, gate_pid = map(int, marker.read_text().split(':'))
            helper = psutil.Process(helper_pid)
            gate_process = psutil.Process(gate_pid)
            with store._connect() as db:
                lease = db.execute('SELECT owner_pid,owner_token FROM fleet_worker_leases WHERE attempt_id=? AND state=?',
                                   (current['attempt_id'], 'active')).fetchone()
                process_token = db.execute('SELECT process_token FROM fleet_attempts WHERE attempt_id=?',
                                           (current['attempt_id'],)).fetchone()[0]
            assert lease is not None
            assert helper.pid == lease['owner_pid']
            launched = psutil.Process(current['pid'])
            # Windows venv python.exe can be a launcher whose child owns the
            # lease. Verify this exact ancestry rather than assuming one PID.
            assert helper == launched or helper in launched.children(recursive=True)
            assert helper.pid not in {os.getpid(), os.getppid()}
            assert launched.pid not in {os.getpid(), os.getppid()}
            assert process_start_token(launched.pid) == process_token
            assert process_start_token(helper.pid) == lease['owner_token']
            assert gate_process.ppid() == helper.pid
            assert (root / 'core/alpha.py').read_bytes() == b'alpha = 2\n'
            helper.kill()  # psutil fences PID reuse against this process instance
            killed = future.result(timeout=30)
        assert not killed.ok
        if os.name == 'nt':
            assert killed.exit_code > 0, killed.error
        else:
            assert killed.exit_code == -9, killed.error
        deadline = time.monotonic() + 5
        while gate_process.is_running() and time.monotonic() < deadline:
            if gate_process.status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.02)
        assert not gate_process.is_running() or gate_process.status() == psutil.STATUS_ZOMBIE
        assert resume_ready_resource_waits(store, now=time.time() + 31) == [leg['leg_id']]
        result = supervisor._execute_leg(store, run_id, store.get_run(run_id)['phases'][3]['legs'][0])
        assert result.ok, result.error
        final = store.get_run(run_id)['phases'][3]['legs'][0]
        assert final['state'] == 'completed'
        assert final['current_attempt']['actual_model'] is None
        assert (root / 'core/alpha.py').read_bytes() == b'alpha = 2\n'
        accepted = [e for e in store.events(run_id) if e['type'] == 'worker.integration.accepted']
        assert accepted[-1]['payload']['test_gate']['integration_journal']['recovered_postimage']
    finally:
        if gate_process is not None and gate_process.is_running():
            try:
                gate_process.kill()
            except psutil.NoSuchProcess:
                pass
