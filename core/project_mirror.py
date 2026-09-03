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


def canonical_subpath(project_dir: str, cwd: str | None) -> str | None:
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
        
    return None


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
