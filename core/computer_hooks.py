"""Install exact per-turn context hooks without replacing the user's other hooks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path


def _is_context_hook(hook):
    if hook.get("type") != "command":
        return False
    try:
        argv = shlex.split(hook.get("command", ""))
    except ValueError:
        return False
    return (
        len(argv) == 6
        and Path(argv[1]).name == "cli.py"
        and argv[2:5] == ["computer", "context-hook", "--agent"]
        and argv[5] in {"codex", "claude"}
    )


def install_context_hooks(*, home=None, python=None, cli=None):
    home = Path(home or Path.home())
    python = str(python or sys.executable)
    cli = str(cli or Path(__file__).resolve().parents[1] / "cli.py")
    installed = []
    for agent, path in (
        ("codex", home / ".codex/hooks.json"),
        ("claude", home / ".claude/settings.json"),
    ):
        original = path.read_text() if path.exists() else "{}"
        settings = json.loads(original)
        hooks = settings.setdefault("hooks", {})
        groups = hooks.setdefault("UserPromptSubmit", [])
        command = shlex.join([python, cli, "computer", "context-hook", "--agent", agent])
        wanted = {"type": "command", "command": command, "timeout": 5}
        if agent == "codex":
            wanted["additionalContextLimit"] = 0
        # Replace only this integration's earlier exact hook, preserving siblings.
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if not _is_context_hook(h)]
        groups[:] = [group for group in groups if group.get("hooks")]
        groups.append({"hooks": [wanted]})
        updated = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        if updated != original:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists():
                backups = home / ".config/serena/computer/hook-backups"
                backups.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup = backups / f"{agent}-{time.time_ns()}.json"
                shutil.copyfile(path, backup)
                backup.chmod(0o600)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(updated)
            temporary.chmod(0o600)
            temporary.replace(path)
        installed.append({"agent": agent, "path": str(path), "command": command})
    return installed
