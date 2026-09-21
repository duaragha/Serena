"""Reach the journal on the PC from any other machine.

The journal is one store, on the PC: that is where the nightly draft runs, where
location arrives, and where his Telegram answers land. The laptop's orb talking
to a laptop copy would write to an empty journal nobody reads. So off the PC,
her journal tools run the same operation on the PC over ssh -- the same path
core.fleet_remote already uses to read PC runs -- and return its answer.

Configuration, ~/.config/serena/journal.json (optional):
    {"home": "local"}  run here (the default on the PC)
    {"home": "pc"}     proxy to the PC (the default everywhere else)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PC_HOST = "pc"
PC_PYTHON = r"C:\Users\ragha\serena-runtime\.venv\Scripts\python.exe"
PC_REPO = r"C:\Users\ragha\serena-runtime"
TIMEOUT_SECONDS = 240  # a first answer of the day can build a draft

_RUNNER = '''
import json, sys
sys.path.insert(0, {repo!r})
from core.journal import api
request = json.loads({request!r})
result = getattr(api, request["op"])(**request["args"])
print("JOURNAL-RESULT " + json.dumps(result))
'''


def home() -> str:
    path = Path(os.environ.get("SERENA_CONFIG_DIR", "") or Path.home() / ".config" / "serena") / "journal.json"
    try:
        configured = json.loads(path.read_text(encoding="utf-8")).get("home")
        if configured in ("local", "pc"):
            return configured
    except (OSError, ValueError, AttributeError):
        pass
    return "local" if sys.platform == "win32" else "pc"


def call(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run core.journal.api.<op>(**args) wherever the journal lives."""

    if op not in ("day", "answer"):
        raise ValueError(f"unknown journal operation {op!r}")
    if home() == "local":
        from core.journal import api

        return getattr(api, op)(**args)
    # Same shape as core.fleet_remote: the whole script on stdin to `python -`,
    # the request inside it as a string literal, nothing through cmd.exe quoting.
    script = _RUNNER.format(repo=PC_REPO, request=json.dumps({"op": op, "args": args}))
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", PC_HOST, PC_PYTHON, "-"],
            input=script, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"could not reach the journal on the PC: {type(exc).__name__}: {exc}"}
    for line in done.stdout.splitlines():
        if line.startswith("JOURNAL-RESULT "):
            return json.loads(line[len("JOURNAL-RESULT "):])
    return {"error": f"the PC did not answer (exit {done.returncode}): {done.stderr[-300:]}"}
