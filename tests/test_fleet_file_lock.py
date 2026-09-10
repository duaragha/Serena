"""Real subprocess contention and owner-death checks, including native Windows."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from fleet.file_lock import exclusive_lock

ROOT = Path(__file__).resolve().parents[1]


def test_lock_blocks_another_process_then_releases_on_owner_death(tmp_path):
    path = tmp_path / "integration.lock"
    ready = tmp_path / "ready"
    script = (
        "from pathlib import Path; import time\n"
        "from fleet.file_lock import exclusive_lock\n"
        f"with Path({str(path)!r}).open('a+b') as f, exclusive_lock(f):\n"
        f"    Path({str(ready)!r}).write_text('locked')\n"
        "    time.sleep(20)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "lock owner never became ready"
        with path.open("a+b") as other:
            with pytest.raises(TimeoutError):
                with exclusive_lock(other, timeout=0.1):
                    pytest.fail("overlapping writer entered")
        child.kill()
        child.wait(timeout=5)
        with path.open("a+b") as other, exclusive_lock(other, timeout=2):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_exception_releases_the_lock(tmp_path):
    path = tmp_path / "lock"
    with path.open("a+b") as handle:
        with pytest.raises(ValueError):
            with exclusive_lock(handle):
                raise ValueError("test body")
    with path.open("a+b") as other, exclusive_lock(other, timeout=0):
        pass


def test_replay_import_does_not_require_fcntl():
    code = "import sys; sys.modules['fcntl']=None; import fleet.integration_recovery"
    child = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
