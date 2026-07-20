"""Scan ~/.claude/projects/ for session .jsonl files."""

import re
from pathlib import Path

from core.config import PROJECTS_DIR


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def scan_sessions(projects_dir: Path | None = None):
    """Yield (project_dir_name, session_file_path) for all session .jsonl files.

    Teammate/subagent sessions are yielded too — the parser flags them via
    `is_teammate` so the chat browser can filter them while the usage
    dashboard still counts them.
    """
    root = projects_dir or PROJECTS_DIR
    if not root.exists():
        return

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        # Skip container-staging slugs that aren't real projects:
        #  - *serena-resume*: claude --resume cwd-staging copies
        #  - *serena-headless*: the cwd for headless `claude -p` utility calls
        #    (smart-spawn router, AI title generation) — never real chats.
        #  - -root: the container resolves unknown cwds to /root, so web-UI
        #    terminal resumes land dupes here. Never a real project on a
        #    user (non-root) machine.
        is_headless_utility = (
            "serena-headless" in project_dir.name
            and "serena-headless-brain" not in project_dir.name
        )
        if (
            "serena-resume" in project_dir.name
            or is_headless_utility
            or project_dir.name == "-root"
        ):
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if not UUID_RE.match(session_file.stem):
                continue
            # Skip symlinks — they're cross-platform resume aids, not distinct sessions
            if session_file.is_symlink():
                continue
            yield (project_dir.name, session_file)
