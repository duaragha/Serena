"""Real atomic Windows job creation, no production processes or provider calls."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from fleet.workers import _stream_process
from test_fleet_workers import _request

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows job creation")


@pytest.mark.parametrize("exit_code,cancel,timeout", [(0, False, False), (7, False, False),
                                                     (1, True, False), (1, False, True)])
def test_native_immediate_child_is_reaped(tmp_path, monkeypatch, exit_code, cancel, timeout):
    monkeypatch.setenv("SERENA_FLEET_EVENT_DIR", str(tmp_path / "events"))
    if timeout:
        monkeypatch.setenv("SERENA_FLEET_WORKER_TIMEOUT_SECONDS", "1")
    child = None
    source = (
        "import subprocess,sys,time,os\n"
        # Intentionally launch BEFORE reading stdin. Post-launch assignment
        # cannot enforce the contract this test exercises.
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "print(child.pid,flush=True)\n"
        f"time.sleep({60 if cancel or timeout else 1})\n"
        f"os._exit({exit_code})\n"
    )
    def observe(line):
        nonlocal child
        child = psutil.Process(int(line.strip()))
    try:
        result = _stream_process([sys.executable, "-c", source], request=_request(tmp_path, "codex"),
                                 parse_stdout=observe, cancel_requested=lambda: cancel and child is not None,
                                 on_event=lambda *args: None)
        assert result.exit_code == exit_code
        assert result.cancelled == cancel
        assert ("timed out" in result.stderr) == timeout
        assert child is not None
        deadline = time.monotonic() + 5
        while child.is_running() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not child.is_running(), "native worker left its child running"
    finally:
        if child is not None:
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass


def test_child_is_in_job_at_its_first_instruction_and_environment_roundtrips(tmp_path):
    from fleet.windows_process import WindowsProcess
    source = (
        "import ctypes,os,sys,json; from ctypes import wintypes as w; "
        "api=ctypes.WinDLL('kernel32'); "
        "api.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]; "
        "inside=w.BOOL(); assert api.IsProcessInJob(w.HANDLE(-1),None,ctypes.byref(inside)); "
        "print(json.dumps([bool(inside.value),os.getcwd(),os.environ['FLEET_TEST_VALUE'],sys.argv[1:]]),flush=True)"
    )
    env = dict(os.environ, FLEET_TEST_VALUE="snowman-\u2603")
    args = ['with spaces', 'quote"value', 'trailing\\', '']
    process = WindowsProcess([sys.executable, "-c", source, *args], cwd=str(tmp_path), env=env)
    try:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        inside = wintypes.BOOL()
        assert api.IsProcessInJob(process._handle, process._job._handle, ctypes.byref(inside))
        assert inside.value, "worker was not created in its exact Fleet-owned job"
        process.stdin.close()
        import json
        values = json.loads(process.stdout.readline())
        assert values == [True, str(tmp_path), env['FLEET_TEST_VALUE'], args]
        assert process.wait(timeout=10) == 0
    finally:
        process.close()


def test_owner_death_reaps_atomically_owned_worker(tmp_path):
    marker = tmp_path / "worker.pid"
    source = (
        "import os,sys,time; from pathlib import Path; from fleet.windows_process import WindowsProcess; "
        "p=WindowsProcess([sys.executable,'-c','import time; time.sleep(60)'],cwd=os.getcwd(),env=dict(os.environ)); "
        f"marker=Path({str(marker)!r}); pending=marker.with_suffix('.tmp'); "
        "pending.write_text(str(os.getpid())+':'+str(p.pid)); pending.replace(marker); time.sleep(60)"
    )
    owner = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    child = None
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists(), "job owner did not publish its worker"
        actual_owner_pid, child_pid = map(int, marker.read_text().split(':'))
        actual_owner = psutil.Process(actual_owner_pid)
        launched_owner = psutil.Process(owner.pid)
        assert actual_owner == launched_owner or actual_owner in launched_owner.children(recursive=True)
        child = psutil.Process(child_pid)
        assert child in actual_owner.children(recursive=True)
        actual_owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while child.is_running() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not child.is_running(), "worker survived loss of its noninherited job handle"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        owner.stderr.close()
        if child is not None:
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass


def test_repeated_creation_does_not_retain_process_or_pipe_handles(tmp_path):
    from fleet.windows_process import WindowsProcess
    import gc
    def create_closed():
        process = WindowsProcess([sys.executable, "-c", "pass"], cwd=str(tmp_path), env=dict(os.environ))
        try:
            process.stdin.close()
            assert process.wait(timeout=10) == 0
        finally:
            process.close()
        assert process._handle is None and process._job._handle is None
        assert process.stdin is process.stdout is process.stderr is None
        process.close()  # deterministic cleanup is also idempotent
        return process
    # Native diagnostics showed first CreatePipe/CreateProcess initialization
    # adds 1/2 handles, then 40 launches stay flat. Measure steady state, not
    # that one-time initialization; retain closed objects and disable cyclic GC
    # so collection cannot conceal per-launch resource retention.
    closed = [create_closed()]
    baseline = psutil.Process().num_handles()
    counts = []
    gc_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(20):
            closed.append(create_closed())
            counts.append(psutil.Process().num_handles())
        assert max(counts) <= baseline + 2, (baseline, counts)
    finally:
        if gc_enabled:
            gc.enable()


def test_stdio_uses_utf8_with_parent_utf8_mode_disabled(tmp_path):
    # The packaging job enables PYTHONUTF8, so force a separate non-UTF8
    # interpreter to exercise the normal Windows desktop locale as well.
    payload = "\u4f60\u597d \U0001f9ea\n"
    child_source = (
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'bad-byte: \\xff\\n')"
    )
    source = (
        "import os,sys; from fleet.windows_process import WindowsProcess; "
        "assert sys.flags.utf8_mode == 0; "
        f"p=WindowsProcess([sys.executable,'-c',{child_source!r}],cwd=os.getcwd(),env=dict(os.environ))\n"
        "try:\n"
        f" p.stdin.write({ascii(payload)}); p.stdin.close()\n"
        f" assert p.stdout.read() == {ascii(payload)}\n"
        " assert p.stderr.read() == 'bad-byte: \\ufffd\\n'\n"
        " assert p.wait(10) == 0\n"
        "finally: p.close()\n"
    )
    result = subprocess.run([sys.executable, "-X", "utf8=0", "-c", source],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_creation_failure_releases_every_allocated_handle(tmp_path):
    from fleet.windows_process import WindowsProcess
    baseline = psutil.Process().num_handles()
    for _ in range(10):
        with pytest.raises(OSError):
            WindowsProcess([str(tmp_path / 'nonexistent-worker.exe')], cwd=str(tmp_path), env=dict(os.environ))
    assert psutil.Process().num_handles() <= baseline + 2


def test_job_attribute_failure_cannot_launch_unowned_work(tmp_path, monkeypatch):
    from fleet import windows_process
    real = windows_process._api()
    class RefuseJobAttribute:
        def __getattr__(self, name):
            return getattr(real, name)
        def UpdateProcThreadAttribute(self, *args):
            if args[2] == 0x2000D:
                raise OSError('atomic job admission refused')
            return real.UpdateProcThreadAttribute(*args)
    monkeypatch.setattr(windows_process, '_api', lambda: RefuseJobAttribute())
    marker = tmp_path / 'must-not-start'
    source = f'from pathlib import Path; Path({str(marker)!r}).touch()'
    baseline = psutil.Process().num_handles()
    for _ in range(10):
        with pytest.raises(OSError, match='atomic job admission refused'):
            windows_process.WindowsProcess([sys.executable, '-c', source], cwd=str(tmp_path), env=dict(os.environ))
    assert not marker.exists()
    assert psutil.Process().num_handles() <= baseline + 2
