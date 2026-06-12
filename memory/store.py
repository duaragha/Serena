"""Filesystem-backed memory storage.

Memories live as Markdown files with YAML frontmatter under
``MEMORY_DIR/{type}/NNN-<slug>.md``. This is the single source of truth —
the web UI, the TUI, and the ``chats memory`` CLI all read and write the
same files.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from core.config import MEMORY_DIR


MEMORY_TYPES = ["task", "loop", "feedback", "user", "project", "reference", "general"]

# Order + human headers for the session digest. Tasks + open loops lead so
# every chat opens like someone who remembers what we were doing, not a rules
# dump. Tasks = Raghav's deliberate todo list (he owns + checks them off);
# loops = threads I auto-noted ("where we left off").
TYPE_ORDER = ["task", "loop", "user", "feedback", "project", "reference", "general"]
TYPE_HEADERS = {
    "task": "Raghav's tasks — nudge him on these",
    "loop": "Open loops — where we left off",
    "user": "About Raghav",
    "feedback": "How to work with him",
    "project": "Projects & context",
    "reference": "Reference",
    "general": "Other",
}


def _slugify(text: str, max_len: int = 50) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:max_len].rstrip("-")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: str) -> str:
    """Human relative time for open loops ('2d ago'). Empty on bad input."""
    try:
        then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    secs = (datetime.now() - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


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
        "snooze_until": meta.get("snooze", ""),
        "_path": fpath,
        "filename": fpath.name,
    }


def _is_snoozed(m: dict) -> bool:
    """True if this item is snoozed past now (deferred, don't surface it)."""
    su = m.get("snooze_until", "")
    if not su:
        return False
    try:
        return datetime.strptime(su, "%Y-%m-%d %H:%M:%S") > datetime.now()
    except (ValueError, TypeError):
        return False


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
                created: str = "", updated: str = "", snooze: str = "") -> Path:
    if not created:
        created = _now()
    if not updated:
        updated = _now()
    type_dir = MEMORY_DIR / mem_type
    type_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(content) or "memory"
    fpath = type_dir / f"{mem_id:03d}-{slug}.md"
    fm = f"---\nid: {mem_id}\ntype: {mem_type}\ncreated: {created}\nupdated: {updated}\n"
    if snooze:
        fm += f"snooze: {snooze}\n"
    fm += "---\n"
    fpath.write_text(f"{fm}\n{content}\n", encoding="utf-8")
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
    # Keep phone-Serena's brain in sync (fail-soft; Locket down = local only).
    try:
        from memory.locket_mirror import mirror_add
        mirror_add(content, mem_type)
    except Exception:
        pass
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
        snooze=existing.get("snooze_until", ""),
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
    existing = _parse_file(fpath)
    try:
        fpath.unlink()
    except OSError:
        return False
    _rewrite_index()
    # Remove the mirrored copy from Locket too (matched by exact content).
    if existing:
        try:
            from memory.locket_mirror import mirror_delete
            mirror_delete(existing["content"])
        except Exception:
            pass
    return True


def snooze_memory(memory_id: int, days: float = 7) -> bool:
    """Defer an item (task/loop): hide it from the nudge rail until `days`
    from now, so a different one surfaces instead. Content is untouched."""
    fpath = _find_path(memory_id)
    if not fpath:
        return False
    existing = _parse_file(fpath)
    if not existing:
        return False
    until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    new_path = _write_file(
        memory_id, existing["type"], existing["content"],
        created=existing["created_at"], updated=existing["updated_at"],
        snooze=until,
    )
    if fpath != new_path:
        try:
            fpath.unlink()
        except OSError:
            pass
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


def format_loops() -> str:
    """Just the open loops, freshest first, with age. Empty string if none.

    Injected every turn by the UserPromptSubmit hook so I stay grounded on
    what's live even after a long chat compacts the session digest away.
    """
    loops = [m for m in _scan_all() if m["type"] == "loop" and not _is_snoozed(m)]
    if not loops:
        return ""
    loops.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    lines = ["[open loops — where we left off. pick these up without making him re-explain; "
             "drop a natural callback when one's relevant:]"]
    for m in loops:
        age = _ago(m.get("updated_at", ""))
        suffix = f"  ({age})" if age else ""
        lines.append(f"- [{m['id']}] {m['content'].strip()}{suffix}")
    return "\n".join(lines)


def format_tasks() -> str:
    """Raghav's deliberate todo list, freshest first, with age (so I can tell
    what's gone stale and nudge harder). Empty string if none."""
    tasks = [m for m in _scan_all() if m["type"] == "task" and not _is_snoozed(m)]
    if not tasks:
        return ""
    tasks.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    lines = ["[Raghav's tasks. Surface the most relevant ONE when a chat opens and STEER him on "
             "it: tell him to do it, or give a strict this-or-that, never an open-ended 'what do "
             "you want to work on'. If he defers ('later', 'not now', 'skip it'), run "
             "`chats memory snooze <id>` so it goes quiet for ~a week and a different task surfaces "
             "next time. Don't pile on multiple tasks at once. Age = how stale:]"]
    for m in tasks:
        age = _ago(m.get("updated_at", ""))
        suffix = f"  ({age})" if age else ""
        lines.append(f"- [{m['id']}] {m['content'].strip()}{suffix}")
    return "\n".join(lines)


def format_active() -> str:
    """Tasks + open loops together — the per-turn payload injected by the
    UserPromptSubmit hook so I stay grounded on what's live every single turn,
    even deep into a long chat after the session digest compacts away."""
    parts = [format_tasks(), format_loops()]
    return "\n\n".join(p for p in parts if p)


def format_for_claude() -> str:
    memories = list_memories()
    if not memories:
        return ('No memories yet. Save one with '
                '`chats memory add "..." --type loop|user|feedback|project|reference`.')
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    lines = ["# Memory"]
    rendered = set()
    for t in TYPE_ORDER + sorted(k for k in by_type if k not in TYPE_ORDER):
        mems = by_type.get(t)
        if not mems or t in rendered:
            continue
        rendered.add(t)
        # Open loops lead, freshest first, with age so I pick up the thread.
        if t == "loop":
            mems = sorted(mems, key=lambda m: m.get("updated_at", ""), reverse=True)
        lines.append(f"\n## {TYPE_HEADERS.get(t, t.title())}")
        for m in mems:
            content = m["content"].strip()
            if t == "loop":
                age = _ago(m.get("updated_at", ""))
                content = f"{content}  ({age})" if age else content
            lines.append(f"- [{m['id']}] {content}")
    return "\n".join(lines)
