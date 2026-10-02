"""The release probe also exercises the real sidecar entry point from source."""

from pathlib import Path
import runpy
import sys


def test_chat_history_release_probe():
    root = Path(__file__).resolve().parents[1]
    probe = runpy.run_path(str(root / "scripts/verify-chat-index.py"))
    probe["verify"]([sys.executable, str(root / "apps/desktop/sidecar.py")])
