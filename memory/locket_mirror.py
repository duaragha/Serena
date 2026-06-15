"""Mirror laptop memories into Locket's serena_memories store.

Phase-1 of Serena-in-Locket promised ONE memory store; in practice the
laptop kept writing only to its markdown files, so phone-Serena (the
Locket chat brain, which reads serena_memories per request) drifted onto
a stale seed. This module closes the loop:

  - add_memory()    -> mirror_add()    POSTs the same content to Locket
  - delete_memory() -> mirror_delete() removes the matching Locket row
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


def mirror_add(content: str, mem_type: str, memory_id: int | None = None) -> None:
    """Copy a new laptop memory into Locket and link the two by stamping the
    returned Locket row id into the local file's frontmatter (so later edits
    and deletes from the phone reconcile to the right file). Fail-soft."""
    resp = _request("POST", "/api/v1/serena/memories/", {
        "type": _TYPE_MAP.get(mem_type, "reference"),
        "content": content,
        "source": "laptop",
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


# Locket type -> laptop memory type (inverse of _TYPE_MAP, for pull).
_INV_TYPE_MAP = {
    "task": "task", "loop": "loop", "feedback": "feedback",
    "user": "user", "project": "project", "reference": "reference",
}


def pull() -> dict:
    """Reconcile bot-made changes (Locket serena_memories) back into the
    local markdown files, keyed by locket_id.

    - Locket app-sourced row with no matching local file -> create locally.
    - Local file whose locket_id row was edited on the phone -> update.
    - Local file whose locket_id row is gone -> the bot/phone deleted it
      -> remove locally.

    Laptop-sourced rows the phone never touched are left alone. Fail-soft:
    on any network error, returns zeros and changes nothing.
    """
    result = {"created": 0, "updated": 0, "deleted": 0}
    listing = _request("GET", "/api/v1/serena/memories/?include_snoozed=true")
    if listing is None:
        return result
    rows = listing.get("data") or []
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
            delete_memory(lm["id"])
            result["deleted"] += 1
        elif (row.get("content") or "") != lm["content"]:
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
        new_id = add_memory(row.get("content") or "", mem_type, _no_mirror=True)
        set_locket_id(new_id, lid)
        result["created"] += 1

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
