"""Windows entrypoint for the packaged Serena Flask sidecar.

Deliberately thin. ui.pty_terminal drives the terminals on both platforms: it
carries its own ConPTY branch and is what ui.web is written against, so there is
nothing here to substitute. An earlier version swapped in a Windows-only host
that implemented 16 of the 29 functions web.py calls, and opening any chat threw
AttributeError on the first one it reached.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from importlib import import_module
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SERENA_CALL_RUNTIME", "lazy")


_STREAM_SINKS = []


def _repair_standard_streams() -> None:
    """Replace detached Windows console streams before Flask writes to them.

    A ``console=False`` PyInstaller executable can inherit a Python stream whose
    CRT descriptor exists while its Windows handle does not.  Flask/Click writes
    the startup banner to that stream and aborts with ``OSError: [Errno 22]``.
    Electron supplies real pipes, which must be preserved for backend logging;
    only missing or unusable streams are replaced.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            os.fstat(stream.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            # It intentionally stays open for the lifetime of the sidecar.
            sink = open(  # noqa: SIM115
                os.devnull, "w", encoding="utf-8", buffering=1
            )
            _STREAM_SINKS.append(sink)
            setattr(sys, name, sink)


_repair_standard_streams()

def _web_runtime():
    """Load the resident web runtime only when the sidecar is serving it.

    The packaged PTY smoke path must remain a small, terminating process. Importing
    ``ui.web`` before argument dispatch starts resident background threads, so a
    successful ``SystemExit`` waits for those threads instead of returning to the
    release script.
    """
    return import_module("ui.web")


def __getattr__(name: str):
    """Preserve the entry point's lazy ``app`` and ``run_web`` exports."""
    if name in {"app", "run_web"}:
        return getattr(_web_runtime(), name)
    raise AttributeError(name)


def _loopback_host(value: str) -> str:
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise argparse.ArgumentTypeError("the sidecar may only bind to loopback")
    return value


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _pty_smoke() -> int:
    """Exercise WinPTY plus the batch-shim launch path used by Codex."""
    from ui import pty_terminal

    marker = b"SERENA_PTY_OK"
    with tempfile.TemporaryDirectory(
        prefix="serena-pty-smoke-", ignore_cleanup_errors=True
    ) as temp_dir:
        # npm exposes Codex as codex.cmd. A path containing spaces also proves
        # the wrapper's Windows quoting, not merely its extension detection.
        command = Path(temp_dir) / "serena command shim.cmd"
        command.write_text("@echo off\r\necho %1\r\n", encoding="ascii")
        tid = pty_terminal.spawn(
            [str(command), marker.decode("ascii")],
            str(Path.home()),
            cols=80,
            rows=24,
            agent="smoke",
        )
        spawned = pty_terminal.get(tid)
        requested = os.environ.get("SERENA_WINDOWS_PTY_BACKEND", "").lower()
        expected_backend = "winpty" if requested in {"1", "winpty", "legacy"} else "conpty"
        backend_ok = bool(spawned and spawned.pty_backend == expected_backend)
        output = bytearray()
        saw_marker = False
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                chunk = pty_terminal.read_available(tid, max_bytes=65536, timeout=0.1)
                if chunk:
                    output.extend(chunk)
                    if marker in output:
                        saw_marker = True
                if not pty_terminal.is_alive(tid) and not chunk:
                    break
            return 0 if saw_marker and backend_ok else 1
        finally:
            pty_terminal.kill(tid)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Serena Windows sidecar")
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=_port)
    parser.add_argument("--pty-smoke", action="store_true")
    args = parser.parse_args()
    if args.pty_smoke:
        raise SystemExit(_pty_smoke())
    if args.port is None:
        parser.error("--port is required unless --pty-smoke is used")
    _web_runtime().run_web(host=args.host, port=args.port, open_browser=False)


if __name__ == "__main__":
    main()
