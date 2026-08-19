"""Packaged and development entry point for Serena's local Flask UI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ui.web reads this at import time. Desktop startup should not eagerly allocate
# the voice stack just to paint the first window.
os.environ.setdefault("SERENA_CALL_RUNTIME", "lazy")

# `app` is re-exported so the sidecar's own tests (and anything embedding it)
# can reach the Flask app; /api/health now lives in ui.web itself so the
# long-running mobile_host serves it too.
from ui.web import app, run_web  # noqa: E402,F401


def main() -> None:
    parser = argparse.ArgumentParser(description="Serena desktop web sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("desktop sidecar must bind to 127.0.0.1")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    run_web(host=args.host, port=args.port, open_browser=False)


if __name__ == "__main__":
    main()
