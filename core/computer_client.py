"""Authenticated loopback client; no image or credential logging."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from core.computer_platform import ComputerError, desktop_environment


def state_dir():
    return Path(
        os.environ.get("SERENA_COMPUTER_STATE", str(Path.home() / ".config/serena/computer"))
    )


def child_command(operation):
    if getattr(sys, "frozen", False):
        return [sys.executable, "computer", operation]
    return [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "cli.py"),
        "computer",
        operation,
    ]


class ComputerClient:
    def __init__(self, *, timeout=25):
        self.timeout = timeout

    def call(self, method, **params):
        try:
            info = json.loads((state_dir() / "service.json").read_text())
            port = int(info["port"])
            token = (
                info["operator_token"] if method in {"begin", "run", "confirm"} else info["token"]
            )
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/rpc",
                data=json.dumps({"method": method, "params": params}, allow_nan=False).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            # Ignore proxies even when inherited from a remote pane.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=self.timeout) as response:
                value = json.loads(response.read(12 * 1024 * 1024))
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            raise ComputerError(
                "computer service unavailable; run chats computer serve or install"
            ) from exc
        if not value.get("ok"):
            raise ComputerError(value.get("error", "computer operation failed"))
        return value["result"]

    def ensure_running(self):
        try:
            return self.call("status")
        except ComputerError:
            pass
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = directory / "service.log"
        with log.open("ab") as output:
            log.chmod(0o600)
            subprocess.Popen(
                child_command("serve"),
                cwd=Path(__file__).resolve().parents[1],
                env=desktop_environment(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=os.name != "nt",
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return self.call("status")
            except ComputerError:
                time.sleep(0.1)
        raise ComputerError(f"computer service failed to start; inspect {log}")
