"""Gated subprocess entry point; never import the web app or launch on import."""

import json
import os
import subprocess
import sys


def main():
    if os.name != "nt":
        raise RuntimeError("Windows worker bootstrap requires Windows")
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    # Read exactly the gate, never buffer the provider's subsequent JSONL input.
    data = bytearray()
    while len(data) <= 1024 * 1024:
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte:
            return 2  # Owner disappeared before authorizing a child.
        if byte == b"\n":
            break
        data.extend(byte)
    else:
        raise ValueError("Worker launch command is too large")
    command = json.loads(data)
    if not isinstance(command, list) or not command or any(
        not isinstance(arg, str) or "\0" in arg for arg in command
    ):
        raise ValueError("Invalid worker command")
    child = subprocess.Popen(
        command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        close_fds=True,
    )
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
