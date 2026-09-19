"""Fleet runs on the other machine, because there is no shared ledger.

Each machine keeps its own store -- the laptop at
``~/.local/state/serena/fleet.sqlite3`` and the PC at the Windows equivalent --
and nothing merges them. That is fine for the supervisor, which only ever drives
its own runs, and wrong for her, because he asks "what is happening with that
run" without caring which box it landed on.

Asked about a Locket run dispatched to the PC on 2026-09-19, she searched the
laptop, found nothing, and concluded it was "either not dispatched under that ID
or it already fell off completed history". Both plausible, both wrong: it was
running on the PC the whole time. Not being able to see a machine is a different
thing from a run not existing, and only this module can tell them apart.

Read-only on purpose. Controlling a remote run means steering a supervisor that
is mid-write in a database this machine does not own.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

PC_HOST = "pc"
PC_PYTHON = r"C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe"
PC_REPO = r"C:\Users\ragha\Projects\serena"
TIMEOUT_SECONDS = 25
# A spoken turn cannot wait on ssh every time she mentions Fleet.
CACHE_SECONDS = 20.0

_PROBE = f'''
import json, sys
sys.path.insert(0, r"{PC_REPO}")
from core.fleet_store import FleetStore
runs = FleetStore().list_runs()
out = []
for run in runs[:50]:
    out.append({{
        "run_id": str(run.get("run_id") or ""),
        "state": run.get("state"),
        "task": str(run.get("task") or "")[:400],
        "project": run.get("project"),
        "created": run.get("created_at") or run.get("created"),
        "machine": "pc",
    }})
print(json.dumps(out))
'''

_cache: tuple[float, list[dict[str, Any]]] | None = None


def pc_runs(*, force: bool = False) -> list[dict[str, Any]]:
    """Recent runs from the PC, or an empty list when it cannot be reached.

    Unreachable and empty are deliberately the same value here; the caller that
    needs to tell him which it was should use :func:`pc_reachable`.
    """

    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", PC_HOST, PC_PYTHON, "-"],
            input=_PROBE, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False,
        )
        runs = json.loads(done.stdout.strip() or "[]")
        if not isinstance(runs, list):
            runs = []
    except (OSError, subprocess.SubprocessError, ValueError):
        runs = []
    _cache = (now, runs)
    return runs


def pc_reachable() -> bool:
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", PC_HOST, "cmd /c echo ok"],
            capture_output=True, text=True, timeout=12, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "ok" in done.stdout


def find_pc_run(reference: str) -> dict[str, Any] | None:
    """The PC run a reference points at, matched the way he speaks about them.

    He says "the Locket one", not a run id, so a word from the task counts.
    """

    needle = (reference or "").strip().casefold()
    if not needle:
        return None
    runs = pc_runs()
    for run in runs:
        if str(run.get("run_id", "")).casefold().startswith(needle):
            return run
    # Every distinctive word has to appear. A looser "any word matches" pass
    # was here briefly and would have answered "a run about penguins on mars"
    # with a real run, because "run" appears in most of them -- which is the
    # confabulation this whole lookup exists to prevent.
    noise = {"run", "the", "one", "job", "task", "about", "fleet", "that", "this"}
    words = [w for w in needle.split() if len(w) > 2 and w not in noise]
    if not words:
        return None
    for run in runs:
        haystack = f"{run.get('task', '')} {run.get('project', '')}".casefold()
        if all(word in haystack for word in words):
            return run
    return None
