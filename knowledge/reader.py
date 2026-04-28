"""Knowledge base browser for ~/Documents/Projects/knowledge/.

Reads topic folders and INDEX.md to provide a browsable, deletable
interface for research saved by Claude Code sessions.
"""

import re
import shutil
from pathlib import Path

from core.config import KNOWLEDGE_DIR

INDEX_PATH = KNOWLEDGE_DIR / "INDEX.md"


def _parse_index() -> dict[str, str]:
    """Parse INDEX.md to extract slug -> description mapping."""
    if not INDEX_PATH.exists():
        return {}

    descriptions: dict[str, str] = {}
    text = INDEX_PATH.read_text()

    for match in re.finditer(r"- \[([^\]]+)\]\(\./([^/]+)/?\)\s*-\s*(.+)", text):
        _title, slug, desc = match.groups()
        # Strip trailing date like (2026-03-26) from description for cleaner display
        descriptions[slug] = desc.strip()

    return descriptions


def list_topics() -> list[dict]:
    """List all knowledge topics with metadata.

    Returns list of dicts with: slug, title, description, file_count, modified, total_size
    """
    if not KNOWLEDGE_DIR.exists():
        return []

    descriptions = _parse_index()
    topics = []

    skip = {"__pycache__", "__init__.py"}
    for entry in sorted(KNOWLEDGE_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
            continue

        # Get title from README.md first line, fallback to slug
        readme = entry / "README.md"
        title = entry.name
        if readme.exists():
            first_line = readme.read_text().split("\n", 1)[0]
            if first_line.startswith("# "):
                title = first_line[2:].strip()

        # Collect file info
        md_files = list(entry.glob("*.md"))
        total_size = sum(f.stat().st_size for f in md_files if f.exists())
        latest_mod = max(
            (f.stat().st_mtime for f in md_files if f.exists()),
            default=0,
        )

        topics.append({
            "slug": entry.name,
            "title": title,
            "description": descriptions.get(entry.name, ""),
            "file_count": len(md_files),
            "total_size": total_size,
            "modified": latest_mod,
        })

    # Sort by most recently modified first
    topics.sort(key=lambda t: t["modified"], reverse=True)
    return topics


def get_topic_files(slug: str) -> list[dict]:
    """List all markdown files in a topic folder."""
    topic_dir = KNOWLEDGE_DIR / slug
    if not topic_dir.exists():
        return []

    files = []
    for f in sorted(topic_dir.glob("*.md")):
        files.append({
            "name": f.name,
            "path": str(f),
            "size": f.stat().st_size,
        })
    return files


def get_topic_content(slug: str) -> str:
    """Read all markdown content from a topic, concatenating all files."""
    topic_dir = KNOWLEDGE_DIR / slug
    if not topic_dir.exists():
        return "Topic not found."

    parts = []
    files = sorted(topic_dir.glob("*.md"))

    # Put README.md first if it exists
    readme = topic_dir / "README.md"
    if readme in files:
        files.remove(readme)
        files.insert(0, readme)

    for f in files:
        if len(files) > 1:
            parts.append(f"{'─' * 40}\n📄 {f.name}\n{'─' * 40}\n")
        parts.append(f.read_text())
        parts.append("")

    return "\n".join(parts).strip()


def get_file_content(slug: str, filename: str) -> str:
    """Read a single markdown file from a topic."""
    f = KNOWLEDGE_DIR / slug / filename
    if not f.exists():
        return "File not found."
    return f.read_text()


def delete_topic(slug: str) -> bool:
    """Delete a topic folder and remove its entry from INDEX.md."""
    topic_dir = KNOWLEDGE_DIR / slug
    if not topic_dir.exists() or not topic_dir.is_dir():
        return False

    # Remove the directory
    shutil.rmtree(topic_dir)

    # Remove from INDEX.md
    if INDEX_PATH.exists():
        lines = INDEX_PATH.read_text().splitlines()
        pattern = re.compile(rf"- \[[^\]]+\]\(\./\s*{re.escape(slug)}\s*/?\)")
        filtered = [line for line in lines if not pattern.search(line)]
        # Clean up double blank lines left behind
        cleaned = []
        for line in filtered:
            if line.strip() == "" and cleaned and cleaned[-1].strip() == "":
                continue
            cleaned.append(line)
        INDEX_PATH.write_text("\n".join(cleaned) + "\n")

    return True


def format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes}B"
