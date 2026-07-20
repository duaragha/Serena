"""Install the ONNX-only openWakeWord desk runtime into the active venv."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    requirements = Path(__file__).with_name("requirements-desk.txt")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        check=True,
    )
    # openWakeWord 0.6.0 declares Linux tflite-runtime unconditionally. There is
    # no Python 3.12 wheel. Serena only loads an explicit ONNX model, so install
    # the package itself after its ONNX dependency set has been resolved above.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "openwakeword==0.6.0",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import openwakeword, onnxruntime, sounddevice; "
            "print('desk wake runtime ready')",
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
