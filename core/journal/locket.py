"""Locket's side of the journal: the day's recorded facts in, her entry out.

Locket is where he reads his journal, so her entry is written there -- tagged
`serena` so it is always clear which entries she drafted. It runs on Railway,
which is fine for the entry he chose to keep there; the raw location history
deliberately does not go there (see core.journal.location).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 20
# Every Locket route answers the bare path with a 308 to the trailing-slash
# one. urllib follows that for GET but not for POST or PATCH, so the first
# live run drafted a whole day and then failed to save it. Paths carry the
# slash themselves.
SERENA_TAG = "serena"


class LocketError(RuntimeError):
    """Locket could not be read or written. Never the same as an empty day."""


def _credentials() -> tuple[str, str]:
    from core.locket_scanner import _load_env

    found = _load_env()
    if found is None:
        raise LocketError("Locket is not configured (~/.config/serena/locket.env)")
    return found[0].rstrip("/"), found[1]


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base, key = _credentials()
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
        method=method)
    try:
        from core.unified_hub import _tls_context

        # The PC's system store has an expired Let's Encrypt root that Python
        # picks over the valid chain; the first live run failed on exactly that.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                    context=_tls_context()) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LocketError(f"Locket {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LocketError(f"Locket {method} {path} failed: {exc}") from exc


def day_facts(day: str, tz: str = "America/Toronto") -> dict[str, Any]:
    query = urllib.parse.urlencode({"date": day, "tz": tz})
    data = _request("GET", f"/api/v1/journal/day-facts/?{query}")
    if not data.get("success"):
        raise LocketError(f"day-facts refused: {data.get('error') or data}")
    return data


PLACEHOLDER_TITLES = {"", "auto-logged"}


def entries_on(day: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"dateFrom": day, "dateTo": day, "limit": 50})
    data = _request("GET", f"/api/v1/journal/?{query}")
    rows = [e for e in data.get("data") or [] if str(e.get("entryDate") or "")[:10] == day]
    return sorted(rows, key=lambda e: int(e.get("id") or 0))


# The first version marked her section with a visible "Drafted by Serena" line.
# Entries written then still carry it; everything from it on was hers.
LEGACY_MARKER = "<p><em>Drafted by Serena"


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _content(entry_id: int) -> str:
    data = _request("GET", f"/api/v1/journal/{int(entry_id)}/")
    return str((data.get("data") or {}).get("content") or "")


def write_entry(*, day: str, title: str, html: str, entry_id: int | None,
                base: str | None = None, written: str | None = None) -> dict[str, Any]:
    """Write her draft into the day's entry. One entry per day, always.

    There is no visible line saying which part is hers -- he did not want her
    taking credit in his own journal -- so the boundary lives here instead:
    `base` is whatever the entry held before she first wrote, and `written` is
    exactly what Locket stored after her last write. If the entry still reads
    `written`, it is replaced with `base` + the new draft. If it does not, he
    has edited it, and she leaves it alone rather than overwrite his words.

    Returns {"id", "base", "written", "skipped"} for the caller to keep.
    """

    existing = entries_on(day)
    target = next((e for e in existing if int(e.get("id") or 0) == int(entry_id or 0)), None)
    target = target or (existing[0] if existing else None)
    if target is None:
        data = _request("POST", "/api/v1/journal/", {
            "title": title, "content": html, "entryDate": day, "entryTime": "22:00",
            "tagNames": [SERENA_TAG],
        })
        entry = data.get("data") or {}
        if not entry.get("id"):
            raise LocketError(f"Locket did not return an entry id: {data}")
        new_id = int(entry["id"])
        return {"id": new_id, "base": "", "written": _content(new_id), "skipped": False}

    target_id = int(target["id"])
    current = str(target.get("content") or "")
    if written is None:
        cut = current.find(LEGACY_MARKER)
        base = (current if cut < 0 else current[:cut]).rstrip()
    elif not _same(current, written):
        return {"id": target_id, "base": base or "", "written": written, "skipped": True}

    update: dict[str, Any] = {"content": f"{base or ''}{html}"}
    tags = [str(t.get("name")) for t in target.get("tags") or [] if t.get("name")]
    if SERENA_TAG not in tags:
        update["tagNames"] = [*tags, SERENA_TAG]
    if str(target.get("title") or "").strip().lower() in PLACEHOLDER_TITLES:
        update["title"] = title
    _request("PATCH", f"/api/v1/journal/{target_id}/", update)
    return {"id": target_id, "base": base or "", "written": _content(target_id), "skipped": False}
