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

from flask import jsonify  # noqa: E402
from ui.web import app, run_web  # noqa: E402


@app.get("/api/health")
def electron_health():
    return jsonify({"ok": True, "pid": os.getpid()})


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
