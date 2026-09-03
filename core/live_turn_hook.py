"""claude-code Stop hook → twin-mode turn announcer.

Registered in ~/.claude/settings.json (per machine):

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "python <repo>/core/live_turn_hook.py"}]}]}}

When a turn completes, claude pipes hook JSON (with session_id) to stdin.
We POST to the local Serena relay, which fans out to peers so their twin
panes of this conversation refresh (see core/live_relay.py). Must be fast,
silent, and ALWAYS exit 0 — a hook failure should never bother claude.
Stdlib only: this runs under whatever python claude finds.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> int:
    try:
        cfg = json.loads(
            (Path.home() / ".config" / "serena" / "live.json").read_text(encoding="utf-8")
        )
        if not cfg.get("enabled"):
            return 0
        payload = json.load(sys.stdin)
        sid = (payload.get("session_id") or "").strip()
        if not sid:
            return 0
        # The relay binds the tailscale IP — resolve it the same way the app
        # does, without importing the repo (hook must run from any cwd/python).
        host = (cfg.get("bind") or "").strip()
        if not host:
            import subprocess
            out = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
            )
            lines = (out.stdout or "").strip().splitlines()
            host = lines[0] if out.returncode == 0 and lines else "127.0.0.1"
        port = int(cfg.get("port") or 8788)
        tok = urllib.parse.quote(str(cfg.get("token") or ""))
        req = urllib.request.Request(
            f"http://{host}:{port}/api/live/turn-done/{sid}?token={tok}",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # relay down / config missing — syncthing still syncs at rest
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
