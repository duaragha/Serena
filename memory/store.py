"""Filesystem-backed memory storage.

Memories live as Markdown files with YAML frontmatter under
``MEMORY_DIR/{type}/NNN-<slug>.md``. This is the single source of truth —
the web UI, the TUI, and the ``chats memory`` CLI all read and write the
same files.
"""

import re
from datetime import datetime
from pathlib import Path

from core.config import MEMORY_DIR


MEMORY_TYPES = ["feedback", "user", "project", "general", "reference"]


def _slugify(text: str, max_len: int = 50) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:max_len].rstrip("-")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_file(fpath: Path) -> dict | None:
    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = parts[2].strip()
    try:
        mid = int(meta.get("id", 0))
    except ValueError:
        return None
    return {
        "id": mid,
        "type": meta.get("type", "general"),
        "content": body,
        "created_at": meta.get("created", ""),
        "updated_at": meta.get("updated", ""),
        "_path": fpath,
        "filename": fpath.name,
    }


def _scan_all() -> list[dict]:
    out = []
    for t in MEMORY_TYPES:
        d = MEMORY_DIR / t
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            m = _parse_file(f)
            if m:
                out.append(m)
    return out


def _find_path(memory_id: int) -> Path | None:
    prefix = f"{memory_id:03d}-"
    for t in MEMORY_TYPES:
        d = MEMORY_DIR / t
        if not d.exists():
            continue
        for f in d.glob(f"{prefix}*.md"):
            return f
    for m in _scan_all():
        if m["id"] == memory_id:
            return m["_path"]
    return None


def _write_file(mem_id: int, mem_type: str, content: str,
                created: str = "", updated: str = "") -> Path:
    if not created:
        created = _now()
    if not updated:
        updated = _now()
    type_dir = MEMORY_DIR / mem_type
    type_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(content) or "memory"
    fpath = type_dir / f"{mem_id:03d}-{slug}.md"
    fpath.write_text(
        f"---\nid: {mem_id}\ntype: {mem_type}\ncreated: {created}\nupdated: {updated}\n---\n\n{content}\n",
        encoding="utf-8",
    )
    return fpath


def _next_id() -> int:
    return max((m["id"] for m in _scan_all()), default=0) + 1


def _rewrite_index():
    """Regenerate MEMORY_DIR/INDEX.md grouped by type."""
    memories = sorted(_scan_all(), key=lambda m: (m["type"], m["id"]))
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)
    lines = ["# Memory", "", "Persistent memories grouped by type. Each file is one memory.", ""]
    for t in MEMORY_TYPES:
        mems = by_type.get(t, [])
        if not mems:
            continue
        lines.append(f"## {t.title()} ({len(mems)})")
        lines.append("")
        for m in mems:
            summary = m["content"].split("\n")[0][:80]
            lines.append(f"- [#{m['id']}](./{m['type']}/{m['filename']}) \u2014 {summary}")
        lines.append("")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def list_memories(type_filter: str | None = None) -> list[dict]:
    memories = _scan_all()
    if type_filter:
        memories = [m for m in memories if m["type"] == type_filter]
    memories.sort(key=lambda m: (m["type"], -m["id"]))
    return [_clean(m) for m in memories]


def add_memory(content: str, mem_type: str = "general") -> int:
    if mem_type not in MEMORY_TYPES:
        mem_type = "general"
    mid = _next_id()
    _write_file(mid, mem_type, content)
    _rewrite_index()
    return mid


def update_memory(memory_id: int, content: str | None = None, mem_type: str | None = None):
    fpath = _find_path(memory_id)
    if not fpath:
        return
    existing = _parse_file(fpath)
    if not existing:
        return
    new_content = content if content is not None else existing["content"]
    new_type = mem_type if mem_type is not None else existing["type"]
    if new_type not in MEMORY_TYPES:
        new_type = existing["type"]
    # Move to new type folder (or rename slug) by writing fresh and removing old
    new_path = _write_file(
        memory_id, new_type, new_content,
        created=existing["created_at"],
        updated=_now(),
    )
    if fpath != new_path:
        try:
            fpath.unlink()
        except OSError:
            pass
    _rewrite_index()


def delete_memory(memory_id: int) -> bool:
    fpath = _find_path(memory_id)
    if not fpath:
        return False
    try:
        fpath.unlink()
    except OSError:
        return False
    _rewrite_index()
    return True


def get_memory(memory_id: int) -> dict | None:
    fpath = _find_path(memory_id)
    if not fpath:
        return None
    m = _parse_file(fpath)
    return _clean(m) if m else None


def search_memories(query: str) -> list[dict]:
    q = query.lower()
    results = [m for m in _scan_all() if q in m["content"].lower()]
    results.sort(key=lambda m: m["updated_at"], reverse=True)
    return [_clean(m) for m in results]


def format_for_claude() -> str:
    memories = list_memories()
    if not memories:
        return 'No memories stored yet. Use `chats memory add "content"` to save one.'
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)
    lines = ["# Memories"]
    for t in sorted(by_type):
        lines.append(f"\n## {t.title()}")
        for m in by_type[t]:
            lines.append(f"- [{m['id']}] {m['content']}")
    return "\n".join(lines)
