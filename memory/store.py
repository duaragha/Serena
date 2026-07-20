"""Filesystem-backed memory storage.

Memories live as Markdown files with YAML frontmatter under
``MEMORY_DIR/{type}/NNN-<slug>.md``. This is the single source of truth —
the web UI, the TUI, and the ``chats memory`` CLI all read and write the
same files.
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from core.config import MEMORY_DIR


MEMORY_TYPES = ["task", "ledger", "loop", "feedback", "user", "project", "reference", "general"]

# Order + human headers for the session digest. Tasks + open loops lead so
# every chat opens like someone who remembers what we were doing, not a rules
# dump. Tasks = Raghav's deliberate todo list (he owns + checks them off);
# ledgers = structured current-state cards for active threads (one per thread,
# updated in place — the thing either agent reads before answering and writes
# after acting, so a handoff doesn't mean re-guessing where things stand);
# loops = threads I auto-noted ("where we left off").
TYPE_ORDER = ["task", "ledger", "loop", "user", "feedback", "project", "reference", "general"]
TYPE_HEADERS = {
    "task": "Raghav's tasks — nudge him on these",
    "ledger": "Active ledgers — exact state of live threads",
    "loop": "Open loops — where we left off",
    "user": "About Raghav",
    "feedback": "How to work with him",
    "project": "Projects & context",
    "reference": "Reference",
    "general": "Other",
}

# Ledger fields, in the fixed order they're always shown. Each ledger is one
# mutable card per active thread (keyed by ledger_key), not an append-only log.
LEDGER_FIELDS = ["goal", "facts", "decision", "promise", "risk", "next_action"]


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
    out = {
        "id": mid,
        "type": meta.get("type", "general"),
        "content": body,
        "created_at": meta.get("created", ""),
        "updated_at": meta.get("updated", ""),
        "snooze_until": meta.get("snooze", ""),
        "locket_id": meta.get("locket_id", ""),
        "source_session_id": meta.get("source_session_id", ""),
        "source_agent": meta.get("source_agent", ""),
        "source_title": meta.get("source_title", ""),
        "source_message_timestamp": meta.get("source_message_timestamp", ""),
        "_path": fpath,
        "filename": fpath.name,
    }
    if meta.get("type") == "ledger":
        out["ledger_key"] = meta.get("ledger_key", "")
        for f in LEDGER_FIELDS:
            out[f] = meta.get(f, "")
    return out


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
                created: str = "", updated: str = "", snooze: str = "",
                locket_id: str = "", source_session_id: str = "",
                source_agent: str = "", source_title: str = "",
                source_message_timestamp: str = "",
                ledger_key: str = "", ledger_fields: dict | None = None) -> Path:
    if not created:
        created = _now()
    if not updated:
        updated = _now()
    type_dir = MEMORY_DIR / mem_type
    type_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(ledger_key if mem_type == "ledger" and ledger_key else content) or "memory"
    fpath = type_dir / f"{mem_id:03d}-{slug}.md"
    fm = f"---\nid: {mem_id}\ntype: {mem_type}\ncreated: {created}\nupdated: {updated}\n"
    if snooze:
        fm += f"snooze: {snooze}\n"
    if locket_id:
        # Link to the always-on Locket serena_memories row, so bot edits
        # and deletes from the phone reconcile to the right local file.
        fm += f"locket_id: {locket_id}\n"
    if source_session_id:
        fm += f"source_session_id: {source_session_id}\n"
    if source_agent:
        fm += f"source_agent: {source_agent}\n"
    if source_title:
        fm += f"source_title: {source_title.replace(chr(10), ' ')[:500]}\n"
    if source_message_timestamp:
        fm += f"source_message_timestamp: {source_message_timestamp}\n"
    if mem_type == "ledger":
        fm += f"ledger_key: {ledger_key}\n"
        for f in LEDGER_FIELDS:
            v = (ledger_fields or {}).get(f, "")
            fm += f"{f}: {v.replace(chr(10), ' ').strip()}\n"
    fm += "---\n"
    fpath.write_text(f"{fm}\n{content}\n", encoding="utf-8")
    return fpath


def set_locket_id(memory_id: int, locket_id: int) -> None:
    """Stamp an existing local memory with its Locket row id (rewrites the
    file in place, preserving everything else)."""
    fpath = _find_path(memory_id)
    if not fpath:
        return
    m = _parse_file(fpath)
    if not m:
        return
    _write_file(memory_id, m["type"], m["content"],
                created=m["created_at"], updated=m["updated_at"],
                snooze=m.get("snooze_until", ""), locket_id=str(locket_id),
                source_session_id=m.get("source_session_id", ""),
                source_agent=m.get("source_agent", ""),
                source_title=m.get("source_title", ""),
                source_message_timestamp=m.get("source_message_timestamp", ""))


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
            if t == "ledger":
                summary = f"[{m.get('ledger_key', '?')}] {m.get('goal', '').strip()}"[:80]
            else:
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


def _source_context() -> tuple[str, str, str, str]:
    sid = (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or ""
    ).strip()
    if not sid:
        return "", "", "", ""
    agent = "claude" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "codex"
    title = ""
    try:
        from core.indexer import get_session

        row = get_session(sid)
        if row:
            title = str(row.get("display_title") or row.get("title") or "")
    except Exception:
        pass
    return sid, agent, title, datetime.now().astimezone().isoformat()


def add_memory(
    content: str,
    mem_type: str = "general",
    _no_mirror: bool = False,
    source_session_id: str = "",
    source_agent: str = "",
    source_title: str = "",
    source_message_timestamp: str = "",
) -> int:
    if mem_type not in MEMORY_TYPES:
        mem_type = "general"
    mid = _next_id()
    if not source_session_id and not _no_mirror:
        (
            source_session_id,
            source_agent,
            source_title,
            source_message_timestamp,
        ) = _source_context()
    _write_file(
        mid,
        mem_type,
        content,
        source_session_id=source_session_id,
        source_agent=source_agent,
        source_title=source_title,
        source_message_timestamp=source_message_timestamp,
    )
    _rewrite_index()
    # Keep phone-Serena's brain in sync (fail-soft; Locket down = local only).
    # _no_mirror=True when the row is being created BY a pull from Locket,
    # so we don't echo it straight back and duplicate.
    if not _no_mirror:
        try:
            from memory.locket_mirror import mirror_add
            mirror_add(
                content,
                mem_type,
                mid,
                source_session_id=source_session_id,
                source_agent=source_agent,
                source_title=source_title,
                source_message_timestamp=source_message_timestamp,
            )
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
        locket_id=existing.get("locket_id", ""),
        source_session_id=existing.get("source_session_id", ""),
        source_agent=existing.get("source_agent", ""),
        source_title=existing.get("source_title", ""),
        source_message_timestamp=existing.get("source_message_timestamp", ""),
    )
    if fpath != new_path:
        try:
            fpath.unlink()
        except OSError:
            pass
    _rewrite_index()
    try:
        if new_type == existing["type"]:
            from memory.locket_mirror import mirror_update
            mirror_update(
                existing["content"],
                new_content,
                existing.get("locket_id", ""),
            )
        else:
            from memory.locket_mirror import mirror_add, mirror_delete
            mirror_delete(existing["content"], existing.get("locket_id", ""))
            mirror_add(
                new_content,
                new_type,
                memory_id,
                source_session_id=existing.get("source_session_id", ""),
                source_agent=existing.get("source_agent", ""),
                source_title=existing.get("source_title", ""),
                source_message_timestamp=existing.get("source_message_timestamp", ""),
            )
    except Exception:
        pass


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
    # Completed tasks and loops stay in Locket's history. Other memory types
    # retain their explicit hard-delete behavior.
    if existing:
        try:
            if existing["type"] in {"task", "loop"}:
                from memory.locket_mirror import mirror_archive
                mirror_archive(existing["content"], existing.get("locket_id", ""))
            else:
                from memory.locket_mirror import mirror_delete
                mirror_delete(existing["content"], existing.get("locket_id", ""))
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
        snooze=until, locket_id=existing.get("locket_id", ""),
        source_session_id=existing.get("source_session_id", ""),
        source_agent=existing.get("source_agent", ""),
        source_title=existing.get("source_title", ""),
        source_message_timestamp=existing.get("source_message_timestamp", ""),
    )
    if fpath != new_path:
        try:
            fpath.unlink()
        except OSError:
            pass
    return True


def find_ledger(key: str) -> dict | None:
    """Find the ledger card for this key, if one exists."""
    key = key.strip()
    for m in _scan_all():
        if m["type"] == "ledger" and m.get("ledger_key") == key:
            return _clean(m)
    return None


def upsert_ledger(key: str, **fields) -> int:
    """Create or update the ledger card for `key`. Only fields actually
    passed are changed — omitted fields keep their current value. `key`
    should be a short stable slug for the thread (e.g. 'persona-tuning'),
    not a free-text description. Fields: goal, facts, decision, promise,
    risk, next_action."""
    key = key.strip()
    if not key:
        raise ValueError("ledger key required")
    existing = None
    for m in _scan_all():
        if m["type"] == "ledger" and m.get("ledger_key") == key:
            existing = m
            break
    merged = {f: fields.get(f, existing.get(f, "") if existing else "") for f in LEDGER_FIELDS}
    if existing:
        mid = existing["id"]
        fpath = existing["_path"]
        new_path = _write_file(
            mid, "ledger", "", created=existing["created_at"], updated=_now(),
            source_session_id=existing.get("source_session_id", ""),
            source_agent=existing.get("source_agent", ""),
            source_title=existing.get("source_title", ""),
            ledger_key=key, ledger_fields=merged,
        )
        if fpath != new_path:
            try:
                fpath.unlink()
            except OSError:
                pass
    else:
        mid = _next_id()
        source_session_id, source_agent, source_title, _ts = _source_context()
        _write_file(
            mid, "ledger", "", ledger_key=key, ledger_fields=merged,
            source_session_id=source_session_id, source_agent=source_agent,
            source_title=source_title,
        )
    _rewrite_index()
    return mid


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


def format_ledgers() -> str:
    """Structured current-state cards for active threads. Check these before
    answering on a thread that has one, and update via `chats memory ledger`
    after deciding/promising/acting — this is what a handoff actually reads
    from, whichever agent picks the thread up next. Empty string if none."""
    ledgers = [m for m in _scan_all() if m["type"] == "ledger" and not _is_snoozed(m)]
    if not ledgers:
        return ""
    ledgers.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    lines = ["[active ledgers — read the relevant one before answering on that thread, "
             "update it (`chats memory ledger <key> --field ...`) after you decide, promise, "
             "or act. Whichever agent, claude or codex, picks this thread up next reads this, "
             "not a re-guess:]"]
    for m in ledgers:
        age = _ago(m.get("updated_at", ""))
        suffix = f"  (updated {age})" if age else ""
        lines.append(f"- [{m['id']}] ledger: {m.get('ledger_key', '?')}{suffix}")
        for f in LEDGER_FIELDS:
            v = (m.get(f) or "").strip()
            if v:
                lines.append(f"    {f}: {v}")
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
    parts = [format_tasks(), format_ledgers(), format_loops()]
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
        if t == "ledger":
            mems = sorted(mems, key=lambda m: m.get("updated_at", ""), reverse=True)
            for m in mems:
                age = _ago(m.get("updated_at", ""))
                suffix = f"  (updated {age})" if age else ""
                lines.append(f"- [{m['id']}] ledger: {m.get('ledger_key', '?')}{suffix}")
                for f in LEDGER_FIELDS:
                    v = (m.get(f) or "").strip()
                    if v:
                        lines.append(f"    {f}: {v}")
            continue
        for m in mems:
            content = m["content"].strip()
            if t == "loop":
                age = _ago(m.get("updated_at", ""))
                content = f"{content}  ({age})" if age else content
            lines.append(f"- [{m['id']}] {content}")
    return "\n".join(lines)
