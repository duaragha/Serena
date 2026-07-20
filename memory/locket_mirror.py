"""Mirror laptop memories into Locket's serena_memories store.

Phase-1 of Serena-in-Locket promised ONE memory store; in practice the
laptop kept writing only to its markdown files, so phone-Serena (the
Locket chat brain, which reads serena_memories per request) drifted onto
a stale seed. This module closes the loop:

  - add_memory()    -> mirror_add()    POSTs the same content to Locket
  - delete_memory() -> archives completed tasks/loops and removes other rows
  - fetch_observations() pulls phone-side observations back for the
    session digest, so terminal sessions see what she noticed.

Everything is fail-soft with short timeouts: Locket being unreachable
must never break local memory operations.

Credentials: ~/.config/serena/locket.env (LOCKET_URL, LOCKET_API_KEY).
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

LOCKET_ENV = Path.home() / ".config" / "serena" / "locket.env"

# laptop type -> locket serena_memories type (CHECK-constrained)
_TYPE_MAP = {
    "task": "task",
    "loop": "loop",
    "feedback": "feedback",
    "user": "user",
    "project": "project",
    "reference": "reference",
    "general": "reference",
}


def _creds() -> tuple[str, str] | None:
    if not LOCKET_ENV.exists():
        return None
    url = key = ""
    try:
        for line in LOCKET_ENV.read_text().splitlines():
            if line.startswith("LOCKET_URL="):
                url = line.split("=", 1)[1].strip().rstrip("/")
            elif line.startswith("LOCKET_API_KEY="):
                key = line.split("=", 1)[1].strip()
    except OSError:
        return None
    return (url, key) if url and key else None


def _request(method: str, path: str, body: dict | None = None, timeout: int = 4):
    creds = _creds()
    if not creds:
        return None
    base, key = creds
    req = urllib.request.Request(
        f"{base}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:
        return None


def mirror_add(
    content: str,
    mem_type: str,
    memory_id: int | None = None,
    *,
    source_session_id: str = "",
    source_agent: str = "",
    source_title: str = "",
    source_message_timestamp: str = "",
) -> None:
    """Copy a new laptop memory into Locket and link the two by stamping the
    returned Locket row id into the local file's frontmatter (so later edits
    and deletes from the phone reconcile to the right file). Fail-soft."""
    resp = _request("POST", "/api/v1/serena/memories/", {
        "type": _TYPE_MAP.get(mem_type, "reference"),
        "content": content,
        "source": "laptop",
        "sourceSessionId": source_session_id or None,
        "sourceAgent": source_agent or None,
        "sourceTitle": source_title or None,
        "sourceMessageTimestamp": source_message_timestamp or None,
    })
    locket_id = ((resp or {}).get("data") or {}).get("id")
    if locket_id and memory_id is not None:
        try:
            from memory.store import set_locket_id
            set_locket_id(memory_id, int(locket_id))
        except Exception:
            pass


def mirror_delete(content: str, locket_id: str = "") -> None:
    """Remove the Locket row mirroring a deleted local memory. Prefer the
    stored locket_id; fall back to exact-content match for legacy rows."""
    if locket_id:
        _request("DELETE", f"/api/v1/serena/memories/{locket_id}/")
        return
    q = urllib.parse.quote(content[:120])
    listing = _request("GET", f"/api/v1/serena/memories/?q={q}")
    rows = (listing or {}).get("data") or []
    for row in rows:
        if row.get("content") == content and row.get("source") == "laptop":
            _request("DELETE", f"/api/v1/serena/memories/{row['id']}/")
            return


def mirror_archive(content: str, locket_id: str = "") -> None:
    """Archive a completed task in Locket instead of erasing its history."""
    if locket_id:
        _request(
            "PATCH",
            f"/api/v1/serena/memories/{locket_id}/",
            {"archived": True},
        )
        return
    q = urllib.parse.quote(content[:120])
    listing = _request("GET", f"/api/v1/serena/memories/?q={q}")
    rows = (listing or {}).get("data") or []
    for row in rows:
        if row.get("content") == content and row.get("source") == "laptop":
            _request(
                "PATCH",
                f"/api/v1/serena/memories/{row['id']}/",
                {"archived": True},
            )
            return


def mirror_update(old_content: str, new_content: str, locket_id: str = "") -> None:
    """Update the Locket copy of an edited local memory."""
    if locket_id:
        _request(
            "PATCH",
            f"/api/v1/serena/memories/{locket_id}/",
            {"content": new_content},
        )
        return
    q = urllib.parse.quote(old_content[:120])
    listing = _request("GET", f"/api/v1/serena/memories/?q={q}")
    rows = (listing or {}).get("data") or []
    for row in rows:
        if row.get("content") == old_content and row.get("source") == "laptop":
            _request(
                "PATCH",
                f"/api/v1/serena/memories/{row['id']}/",
                {"content": new_content},
            )
            return


# Locket type -> laptop memory type (inverse of _TYPE_MAP, for pull).
_INV_TYPE_MAP = {
    "task": "task", "loop": "loop", "feedback": "feedback",
    "user": "user", "project": "project", "reference": "reference",
}


def _remote_rows(include_archived: bool = True) -> list[dict] | None:
    path = "/api/v1/serena/memories/?include_snoozed=true"
    if include_archived:
        path += "&include_archived=true"
    listing = _request("GET", path)
    if listing is None:
        return None
    return listing.get("data") or []


def push_local(
    prune_stale_laptop: bool = False,
    dry_run: bool = False,
    type_filter: str | None = None,
) -> dict:
    """Push unlinked local markdown memories into Locket.

    This is the backfill half of "one memory store": older laptop files may
    predate mirroring and therefore have no locket_id. Exact remote matches are
    linked instead of duplicated. Optional stale pruning removes remote
    source=laptop rows that do not correspond to any local memory.
    """
    result = {
        "ok": False,
        "dry_run": dry_run,
        "pushed": 0,
        "linked": 0,
        "pruned": 0,
        "remote": 0,
    }
    rows = _remote_rows(include_archived=True)
    if rows is None:
        return result
    result["remote"] = len(rows)

    from memory.store import _scan_all, set_locket_id

    if type_filter and type_filter not in _TYPE_MAP:
        return result

    remote_by_id = {
        int(row["id"]): row
        for row in rows
        if row.get("id") is not None
    }
    remote_exact: dict[tuple[str, str], dict] = {}
    for row in rows:
        content = row.get("content")
        rtype = row.get("type")
        if isinstance(content, str) and isinstance(rtype, str):
            remote_exact.setdefault((rtype, content), row)

    local_seen_locket: set[int] = set()
    for memory in _scan_all():
        local_type = memory.get("type", "general")
        if type_filter and local_type != type_filter:
            continue
        remote_type = _TYPE_MAP.get(local_type)
        if not remote_type:
            continue
        locket_id = (memory.get("locket_id") or "").strip()
        if locket_id:
            try:
                local_seen_locket.add(int(locket_id))
            except ValueError:
                pass
            continue

        content = memory.get("content") or ""
        exact = remote_exact.get((remote_type, content))
        if exact and exact.get("id") is not None:
            if not dry_run:
                set_locket_id(memory["id"], int(exact["id"]))
            local_seen_locket.add(int(exact["id"]))
            result["linked"] += 1
            continue

        if not dry_run:
            resp = _request("POST", "/api/v1/serena/memories/", {
                "type": remote_type,
                "content": content,
                "source": "laptop",
                "sourceSessionId": memory.get("source_session_id") or None,
                "sourceAgent": memory.get("source_agent") or None,
                "sourceTitle": memory.get("source_title") or None,
                "sourceMessageTimestamp": memory.get("source_message_timestamp") or None,
            })
            new_id = ((resp or {}).get("data") or {}).get("id")
            if new_id is not None:
                set_locket_id(memory["id"], int(new_id))
                local_seen_locket.add(int(new_id))
        result["pushed"] += 1

    if prune_stale_laptop:
        for rid, row in remote_by_id.items():
            if row.get("source") != "laptop" or rid in local_seen_locket:
                continue
            if type_filter and row.get("type") != _TYPE_MAP.get(type_filter):
                continue
            if not dry_run:
                _request("PATCH", f"/api/v1/serena/memories/{rid}/", {"archived": True})
            result["pruned"] += 1

    result["ok"] = True
    if not dry_run:
        try:
            from core.locket_sync_state import record_success
            record_success("memory_push", {
                "pushed": result["pushed"],
                "linked": result["linked"],
                "pruned": result["pruned"],
                "remote": result["remote"],
            })
        except Exception:
            pass
    return result


def pull(dry_run: bool = False) -> dict:
    """Reconcile bot-made changes (Locket serena_memories) back into the
    local markdown files, keyed by locket_id.

    - Locket app-sourced row with no matching local file -> create locally.
    - Local file whose locket_id row was edited on the phone -> update.
    - Local file whose locket_id row is gone -> the bot/phone deleted it
      -> remove locally.

    Laptop-sourced rows the phone never touched are left alone. Fail-soft:
    on any network error, returns zeros and changes nothing.
    """
    result = {"created": 0, "updated": 0, "deleted": 0, "dry_run": dry_run, "ok": False}
    listing = _request("GET", "/api/v1/serena/memories/?include_snoozed=true")
    if listing is None:
        return result
    rows = listing.get("data") or []
    result["remote"] = len(rows)
    by_locket_id = {}
    for r in rows:
        rid = r.get("id")
        if rid is not None:
            by_locket_id[int(rid)] = r

    from memory.store import (
        _scan_all, add_memory, update_memory, delete_memory,
        set_locket_id, _parse_file, MEMORY_TYPES,
    )

    # Index local files that carry a locket_id.
    local_by_locket = {}
    for m in _scan_all():
        lid = m.get("locket_id", "")
        if lid:
            try:
                local_by_locket[int(lid)] = m
            except ValueError:
                pass

    # 1. Deletions + edits driven from the phone.
    for lid, lm in list(local_by_locket.items()):
        row = by_locket_id.get(lid)
        if row is None:
            # Gone on the always-on store -> deleted from the phone.
            if not dry_run:
                delete_memory(lm["id"])
            result["deleted"] += 1
        elif (row.get("content") or "") != lm["content"]:
            if not dry_run:
                update_memory(lm["id"], content=row.get("content") or lm["content"])
            result["updated"] += 1

    # 2. New memories the bot created on the phone (source=app, unseen).
    for lid, row in by_locket_id.items():
        if lid in local_by_locket:
            continue
        if row.get("source") != "app":
            continue
        # Only real memory types become local files. Observations and any
        # other phone-only types surface via the digest, not the store.
        rtype = row.get("type", "")
        if rtype not in _INV_TYPE_MAP:
            continue
        mem_type = _INV_TYPE_MAP[rtype]
        if mem_type not in MEMORY_TYPES:
            mem_type = "reference"
        # add_memory will re-mirror; suppress that by writing directly is
        # overkill — instead create then stamp the existing locket_id and
        # let the dup-guard on the next add be a no-op. Simpler: create a
        # local file and stamp it, without re-POSTing.
        if not dry_run:
            new_id = add_memory(
                row.get("content") or "",
                mem_type,
                _no_mirror=True,
                source_session_id=row.get("sourceSessionId") or "",
                source_agent=row.get("sourceAgent") or "",
                source_title=row.get("sourceTitle") or "",
                source_message_timestamp=row.get("sourceMessageTimestamp") or "",
            )
            set_locket_id(new_id, lid)
        result["created"] += 1

    result["ok"] = True
    if not dry_run:
        try:
            from core.locket_sync_state import record_success
            record_success("memory", {
                "created": result["created"],
                "updated": result["updated"],
                "deleted": result["deleted"],
                "remote": result.get("remote", 0),
            })
        except Exception:
            pass

    return result


def fetch_observations(limit: int = 10) -> list[str]:
    """Latest phone-side observations (newest first) for the digest."""
    listing = _request("GET", "/api/v1/serena/memories/?type=observation")
    rows = (listing or {}).get("data") or []
    out = []
    for row in rows[:limit]:
        content = row.get("content")
        if content:
            out.append(content)
    return out
