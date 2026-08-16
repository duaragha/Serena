"""Windows entrypoint for the packaged Serena Flask sidecar."""

from __future__ import annotations

import argparse
import os
import sys
from importlib import import_module
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SERENA_CALL_RUNTIME", "lazy")

pty_windows = import_module("ui.pty_windows")
pty_windows.activate_backend()

flask = import_module("flask")
web = import_module("ui.web")
app = web.app
jsonify = flask.jsonify
run_web = web.run_web


@app.get("/api/health")
def health() -> object:
    """Report readiness to the Electron process without triggering app work."""
    return jsonify(ok=True, pid=os.getpid())


def _loopback_host(value: str) -> str:
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise argparse.ArgumentTypeError("the sidecar may only bind to loopback")
    return value


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Serena Windows sidecar")
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=_port, required=True)
    args = parser.parse_args()
    run_web(host=args.host, port=args.port, open_browser=False)


if __name__ == "__main__":
    main()
