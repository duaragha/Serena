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


def mirror_add(content: str, mem_type: str) -> None:
    """Best-effort copy of a new laptop memory into Locket."""
    _request("POST", "/api/v1/serena/memories/", {
        "type": _TYPE_MAP.get(mem_type, "reference"),
        "content": content,
        "source": "laptop",
    })


def mirror_delete(content: str) -> None:
    """Best-effort removal of the Locket row mirroring `content`.

    Matched by exact content among laptop-sourced rows (adds mirror the
    content verbatim, so equality is a reliable key).
    """
    q = urllib.parse.quote(content[:120])
    listing = _request("GET", f"/api/v1/serena/memories/?q={q}")
    rows = (listing or {}).get("data") or []
    for row in rows:
        if row.get("content") == content and row.get("source") == "laptop":
            _request("DELETE", f"/api/v1/serena/memories/{row['id']}/")
            return


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
