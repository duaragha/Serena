"""A frozen runtime must fail its release probe if calling cannot load."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("entrypoint", ["sidecar.py", "windows/sidecar-win.py"])
def test_runtime_probe_refuses_a_missing_voice_dependency(entrypoint):
    source = Path(__file__).resolve().parents[1] / entrypoint
    # Simulate the released AppImage's missing numpy without touching this
    # machine's installed environment. The lazy package import used to miss it.
    probe = """
import importlib.abc
import runpy
import sys

class MissingNumpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'numpy' or fullname.startswith('numpy.'):
            raise ModuleNotFoundError('packaging fixture: numpy is absent')

sys.meta_path.insert(0, MissingNumpy())
source = sys.argv[1]
sys.argv = [source, '--workspace-runtime-check']
runpy.run_path(source, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(source)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "packaging fixture: numpy is absent" in result.stderr
