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
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LocketError(f"Locket {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LocketError(f"Locket {method} {path} failed: {exc}") from exc


def day_facts(day: str, tz: str = "America/Toronto") -> dict[str, Any]:
    query = urllib.parse.urlencode({"date": day, "tz": tz})
    data = _request("GET", f"/api/v1/journal/day-facts?{query}")
    if not data.get("success"):
        raise LocketError(f"day-facts refused: {data.get('error') or data}")
    return data


def write_entry(*, day: str, title: str, html: str, entry_id: int | None) -> int:
    """Create her entry for the day, or update the one she already wrote."""

    if entry_id:
        _request("PATCH", f"/api/v1/journal/{int(entry_id)}", {"title": title, "content": html})
        return int(entry_id)
    data = _request("POST", "/api/v1/journal", {
        "title": title, "content": html, "entryDate": day, "entryTime": "22:00",
        "tagNames": [SERENA_TAG],
    })
    entry = data.get("data") or {}
    if not entry.get("id"):
        raise LocketError(f"Locket did not return an entry id: {data}")
    return int(entry["id"])
