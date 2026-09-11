"""Probe the actual windowless sidecar's gate and pipes, without a provider."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.workspace_windows_job import WindowsJob  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    if os.name != "nt" or not args.binary.is_file():
        raise RuntimeError("An existing Windows sidecar binary is required")
    executable = getattr(sys, "_base_executable", sys.executable)
    payload = b'{"id":1,"text":"first\\nsecond"}\n'
    observations = []
    with tempfile.TemporaryDirectory(prefix="serena-frozen-gate-") as directory:
        for mode in ("no-gate", "echo", "descendant"):
            job = WindowsJob()
            process = None
            try:
                process = subprocess.Popen(
                    [str(args.binary.resolve()), "--workspace-child"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                job.assign(process.pid)
                wire = b""
                if mode != "no-gate":
                    program = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
                    if mode == "descendant":
                        program = (
                            "import subprocess,sys; "
                            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                            "print(p.pid,flush=True)"
                        )
                    wire = json.dumps([executable, "-I", "-S", "-c", program]).encode() + b"\n" + payload
                stdout, stderr = process.communicate(wire, timeout=30)
                assert process.returncode == (2 if mode == "no-gate" else 0), stderr
                if mode == "echo":
                    assert stdout == payload, (stdout, stderr)
                if mode == "no-gate":
                    assert not stdout
                remaining = job.active_processes()
                if mode == "descendant":
                    assert int(stdout.strip()) > 0 and remaining >= 1
                job.terminate()
                deadline = time.monotonic() + 10
                while job.active_processes() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert job.active_processes() == 0
                observations.append({"mode": mode, "exit_code": process.returncode,
                                     "remaining_before_cleanup": remaining, "cleaned": True})
            finally:
                job.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
    print(json.dumps({"frozen_binary": str(args.binary), "checks": observations,
                      "provider_started": False}))


if __name__ == "__main__":
    main()
