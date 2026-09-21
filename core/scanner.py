"""Scan ~/.claude/projects/ for session .jsonl files."""

import json
import re
from pathlib import Path

from core.config import PROJECTS_DIR


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Serena's headless utilities run with a cwd under ~/.cache/serena-headless-*,
# which is how they are told apart from real chats (core/frontdoor.py,
# core/projects.py). Their transcripts are machine chatter -- one front-door
# classification, one title -- and indexing them buries real chats in recall.
#
# The resident brain is the exception and is deliberately indexed: it is one
# long-lived conversation, it is what `chats recall` has to be able to find,
# and the indexer already hides it from the chat list rather than dropping it.
HEADLESS_PROJECT_MARKER = "cache-serena-headless"
INDEXED_HEADLESS_PROJECTS = ("cache-serena-headless-brain",)


def _is_hidden_headless_project(project_dir_name: str) -> bool:
    name = project_dir_name.casefold()
    if HEADLESS_PROJECT_MARKER not in name:
        return False
    return not any(kept in name for kept in INDEXED_HEADLESS_PROJECTS)


def _is_teammate_session(file_path: Path) -> bool:
    """Check if a session is a teammate/agent spawned by team mode."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "user":
                    content = record.get("message", {}).get("content", "")
                    if isinstance(content, str) and content.lstrip().startswith("<teammate-message"):
                        return True
                    return False
        return False
    except (OSError, PermissionError):
        return False


def scan_sessions(projects_dir: Path | None = None):
    """Yield (project_dir_name, session_file_path) for all session .jsonl files."""
    root = projects_dir or PROJECTS_DIR
    if not root.exists():
        return

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if _is_hidden_headless_project(project_dir.name):
            continue
        for session_file in project_dir.glob("*.jsonl"):
            # Only include UUID-named files (actual sessions)
            if not UUID_RE.match(session_file.stem):
                continue
            # Skip symlinks — they're cross-platform resume aids, not distinct sessions
            if session_file.is_symlink():
                continue
            if not _is_teammate_session(session_file):
                yield (project_dir.name, session_file)
