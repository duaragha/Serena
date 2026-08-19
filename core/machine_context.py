"""Tell every agent which machine it woke up on, before it touches anything.

Raghav works the same repos from two machines, and the paths differ:
``~/Documents/Projects`` on the Linux laptop, ``C:\\Users\\ragha\\Projects`` on
the Windows PC. Nothing announced that, so a session resumed on the other
machine would confidently run Linux paths on Windows, fail, and cost a round of
"which box am I on" before any real work started. This prints a short banner as
a SessionStart hook so the answer arrives before the first command.

It is deliberately cheap and dependency-free: a hook that is slow or throws is
a hook that gets removed.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sys
from pathlib import Path

# Hostname -> the name Raghav actually calls the machine.
KNOWN_MACHINES = {
    "raghavslaptop": "laptop",
    "raghavsgamingpc": "PC",
}


def machine_name() -> str:
    host = socket.gethostname().split(".")[0].lower()
    return KNOWN_MACHINES.get(host, host or "unknown")


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.is_dir():
            return candidate
    return None


def projects_root() -> Path | None:
    """Where the checkouts actually live on THIS machine.

    Both candidate roots can exist at once (the PC has a nearly empty
    ``Documents/Projects`` beside the real ``C:/Users/ragha/Projects``), so
    guessing by name reported the wrong one. The directory holding this repo is
    the authoritative answer; the name check is only a fallback.
    """

    repo = serena_root()
    if repo is not None:
        return repo.parent
    home = Path.home()
    return _first_existing(home / "Documents" / "Projects", home / "Projects")


def serena_root() -> Path | None:
    """This file lives in <repo>/core, so the repo is its parent."""

    here = Path(__file__).resolve().parents[1]
    return here if (here / "cli.py").is_file() else None


def python_for_repo(repo: Path | None) -> str:
    if repo is None:
        return sys.executable
    venv = repo / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    return str(venv) if venv.is_file() else sys.executable


def describe() -> dict[str, str]:
    repo = serena_root()
    projects = projects_root()
    return {
        "machine": machine_name(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "shell": "PowerShell + Git Bash" if os.name == "nt" else "bash",
        "projects_root": str(projects) if projects else "(not found)",
        "serena_repo": str(repo) if repo else "(not found)",
        "python": python_for_repo(repo),
        "git_bash_paths": "yes" if os.name == "nt" and shutil.which("bash") else "no",
    }


def banner() -> str:
    facts = describe()
    lines = [
        f"[machine] You are on Raghav's {facts['machine']} ({facts['os']}).",
        f"[machine] Projects live at {facts['projects_root']}",
        f"[machine] Serena repo: {facts['serena_repo']}",
        f"[machine] Use this Python: {facts['python']}",
    ]
    if facts["machine"] == "PC":
        lines.append(
            "[machine] Windows: paths are C:\\Users\\ragha\\..., NOT /home/raghav/... . "
            "Git Bash sees them as /c/Users/ragha/... . Do not assume Linux paths."
        )
    else:
        lines.append(
            "[machine] Linux: paths are /home/raghav/... . The PC's copy of the same "
            "repo lives at C:\\Users\\ragha\\Projects and is reached over the tailnet."
        )
    return "\n".join(lines)


def main() -> int:
    # Codex has no SessionStart hook, so it runs this itself and reads the
    # output. Plain text for a human or an agent reading a terminal; the JSON
    # envelope only exists to satisfy Claude's hook contract.
    if "--text" in sys.argv[1:]:
        print(banner())
        return 0

    # SessionStart hooks may be handed JSON on stdin; nothing here needs it, but
    # draining it keeps the caller from blocking on an unread pipe.
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except Exception:
            pass
    text = banner()
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
