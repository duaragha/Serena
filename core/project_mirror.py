"""Automated project mirror for Claude, Codex, and Gemini.

Maintains canonical nested project structures under:
  ~/.claude/projects/Projects/<group>/<project>/
  ~/.codex/projects/Projects/<group>/<project>/
  ~/.gemini/projects/Projects/<group>/<project>/

Leaves backward-compatible slug symlinks in ~/.claude/projects/ so Claude CLI
and resume hooks continue to resolve normally without breakage.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

CHATS_DB = Path.home() / ".local" / "share" / "chats" / "index.db"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_PROJECTS = Path.home() / ".codex" / "projects"
GEMINI_PROJECTS = Path.home() / ".gemini" / "projects"


# Group directories whose project names are spelled with underscores. The rest
# of the tree keeps whatever the directory on disk is called, so
# ``frameworth/shopify-free-gift-app`` stays hyphenated.
_UNDERSCORE_GROUPS = {"personal_projects", "money_making"}

# Everything after this segment is the canonical subpath. Both machines put
# their checkouts under a directory with this name (``~/Documents/Projects`` on
# Linux, ``C:\Users\ragha\Projects`` on Windows), which is why the same rule
# reads both.
_PROJECTS_SEGMENT = "projects"

# How deep a mirror goes: group plus project. A chat run in
# ``personal_projects/konpeki/apps/landing`` belongs to konpeki, not to a
# folder of its own.
_MAX_DEPTH = 2


def _segments(text: str) -> list[str]:
    """Split a cwd or a slug into path segments, whatever wrote it.

    A slug has already had its separators flattened to ``-``, and project names
    contain hyphens, so a slug cannot be split back apart reliably. Only real
    paths are read here; slugs fall through to the name table.
    """
    return [part for part in re.split(r"[\\/]+", text or "") if part not in ("", ".", "..")]


def _from_path(cwd: str | None) -> str | None:
    """Derive the canonical subpath from where the chat actually ran.

    The name table below cannot answer for a project nobody has added to it
    yet, and it silently returned None instead of saying so: full_tracker was
    missing, so twenty-eight chats simply had no mirror. The directory layout
    already carries the answer, so read it from there and keep the table for
    paths that have no Projects root to read.
    """
    parts = _segments(cwd or "")
    lowered = [part.lower() for part in parts]
    if _PROJECTS_SEGMENT not in lowered:
        return None
    tail = parts[lowered.index(_PROJECTS_SEGMENT) + 1 :][:_MAX_DEPTH]
    if not tail:
        return None
    if len(tail) > 1 and tail[0] in _UNDERSCORE_GROUPS:
        tail[1] = tail[1].replace("-", "_")
    return "Projects/" + "/".join(tail)


def canonical_subpath(project_dir: str, cwd: str | None) -> str | None:
    """Where this chat's mirror belongs, or None if it is not project work.

    The table is consulted first and is authoritative: it encodes decisions the
    directory layout does not carry, such as a chat sitting directly in the
    frameworth group belonging to ``frameworth/general`` rather than to the
    group directory itself, and a renamed project keeping its old home. Reading
    the path first re-answered seventeen chats that were already placed
    correctly, which is churn, not a fix.

    Derivation is the fallback, for the projects nobody has added to the table.
    That is the actual gap: full_tracker was missing, so twenty-eight chats had
    no mirror at all and the table had no way to say so.
    """
    txt = (cwd or "") + " " + (project_dir or "")

    # Check personal_projects
    for proj in [
        "locket", "unified", "atrium", "vantage", "konpeki", "overclock", "witline",
        "equity_agent", "equity-agent", "game_tracker", "game-tracker",
        "engagement_website", "engagement-website", "jobflow"
    ]:
        if proj in txt:
            canonical_proj = proj.replace("-", "_")
            return f"Projects/personal_projects/{canonical_proj}"

    # Check frameworth
    for proj in ["liquid", "hydrogen", "storefront", "shopify-free-gift-app", "frameworth-google-ads-auctions"]:
        if proj in txt:
            return f"Projects/frameworth/{proj}"
    if "frameworth" in txt:
        return "Projects/frameworth/general"

    # Check ad_sorcery
    if "ad_sorcery" in txt or "ad-sorcery" in txt:
        return "Projects/ad_sorcery"

    # Check money_making
    for proj in ["agent_pr_evidence", "agent-pr-evidence", "mcp_shield", "mcp-shield", "rovo_pir_agent", "rovo-pir-agent"]:
        if proj in txt:
            return f"Projects/money_making/{proj.replace('-', '_')}"

    # Check serena
    if "serena-chats" in txt or "/serena/chats" in txt:
        return "Projects/serena/chats"
    if "serena-knowledge" in txt or "/serena/knowledge" in txt:
        return "Projects/serena/knowledge"
    if "serena" in txt:
        return "Projects/serena"

    if "cybersec-tracker" in txt:
        return "Projects/cybersec-tracker"
    if "ai-automation-agency" in txt:
        return "Projects/ai-automation-agency"

    return _from_path(cwd)


def sync_mirrors() -> dict[str, int]:
    if not CHATS_DB.exists():
        return {"claude": 0, "codex": 0, "gemini": 0}
        
    conn = sqlite3.connect(CHATS_DB, timeout=30)
    c = conn.cursor()
    c.execute("SELECT session_id, agent, project_dir, cwd, file_path FROM sessions;")
    rows = c.fetchall()
    
    stats = {"claude": 0, "codex": 0, "gemini": 0, "slug_links": 0}
    
    for sid, agent, pdir, cwd, fp in rows:
        sub = canonical_subpath(pdir, cwd)
        if not sub:
            continue
            
        if agent == "claude":
            target_dir = CLAUDE_PROJECTS / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            if fp and os.path.exists(fp):
                src = Path(fp)
                dest = target_dir / src.name
                if src.resolve() != dest.resolve():
                    if not dest.exists():
                        try:
                            rel = os.path.relpath(src, target_dir)
                            dest.symlink_to(rel)
                            stats["claude"] += 1
                        except OSError:
                            pass
                if pdir and not pdir.startswith("Projects/"):
                    legacy_dir = CLAUDE_PROJECTS / pdir
                    if not legacy_dir.exists():
                        try:
                            rel = os.path.relpath(target_dir, CLAUDE_PROJECTS)
                            legacy_dir.symlink_to(rel)
                            stats["slug_links"] += 1
                        except OSError:
                            pass

        elif agent == "codex":
            target_dir = CODEX_PROJECTS / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            if fp and os.path.exists(fp):
                src = Path(fp)
                dest = target_dir / src.name
                if not dest.exists():
                    try:
                        dest.symlink_to(src)
                        stats["codex"] += 1
                    except OSError:
                        pass

        elif agent == "gemini":
            target_dir = GEMINI_PROJECTS / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            if fp and os.path.exists(fp):
                src = Path(fp)
                dest = target_dir / (f"{sid}.db" if src.suffix == ".db" else f"{sid}.jsonl")
                if not dest.exists():
                    try:
                        dest.symlink_to(src)
                        stats["gemini"] += 1
                    except OSError:
                        pass

                        
    return stats


if __name__ == "__main__":
    s = sync_mirrors()
    print("Mirror sync results:", s)
