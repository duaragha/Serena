"""His chats, read through Unified's own MCP endpoint.

Unified already collects every network he talks on -- WhatsApp, iMessage,
Instagram, Messenger, Telegram, Discord -- into one hub, and exposes it
read-only over MCP. That is the whole reason chats work as a journal source:
plans get made wherever his friends happen to be, and the hub has already done
the work of putting them in one place.

Read-only by construction: the hub's MCP surface has no write tools, and the
token in ~/.config/serena/unified-mcp.token is a dedicated connection labelled
"Serena journal" so it can be revoked without touching his other agents.

Configuration, ~/.config/serena/unified-mcp.json (optional):

    {"url": "https://pc.tail4d6220.ts.net:8787/mcp",
     "token_file": "~/.config/serena/unified-mcp.token"}

The PC reaches the hub on loopback through the VM's NAT forward; the laptop
reaches it over the tailnet. Both are the same hub.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TAILNET_URL = "https://pc.tail4d6220.ts.net:8787/mcp"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8787/mcp"
PAGE_SIZE = 200  # the hub's own maximum per call
# A day of his chats is a few hundred to ~1,600 messages. Anything past this
# is a runaway page loop, not a busy day.
MAX_PAGES = 40
CALL_TIMEOUT_SECONDS = 45


class UnifiedSourceError(RuntimeError):
    """The hub could not be read. Never the same thing as "no messages"."""


@dataclass(frozen=True)
class ChatMessage:
    id: str
    chat: str
    chat_id: str
    network: str
    author: str
    outgoing: bool
    text: str
    at: datetime

    def line(self, tz) -> str:
        who = "ME" if self.outgoing else self.author
        stamp = self.at.astimezone(tz).strftime("%a %H:%M")
        return f"{stamp} | {who}: {self.text}"


def _config_dir() -> Path:
    return Path(os.environ.get("SERENA_CONFIG_DIR", "") or Path.home() / ".config" / "serena")


def settings() -> dict[str, str]:
    path = _config_dir() / "unified-mcp.json"
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    # The PC sits next to the hub and reaches it on loopback; everything else
    # goes over the tailnet.
    default_url = DEFAULT_LOCAL_URL if sys.platform == "win32" else DEFAULT_TAILNET_URL
    token_file = Path(os.path.expanduser(
        str(data.get("token_file") or _config_dir() / "unified-mcp.token")))
    return {"url": str(data.get("url") or default_url), "token_file": str(token_file)}


def _token() -> str:
    path = Path(settings()["token_file"])
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UnifiedSourceError(f"no Unified token at {path}") from exc
    if not token:
        raise UnifiedSourceError(f"Unified token file {path} is empty")
    return token


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(row: dict[str, Any]) -> ChatMessage | None:
    try:
        return ChatMessage(
            id=str(row["id"]),
            chat=str(row.get("conversationTitle") or ""),
            chat_id=str(row.get("conversationId") or ""),
            network=str(row.get("network") or ""),
            author=str(row.get("author") or "?"),
            outgoing=row.get("direction") == "outgoing",
            text=" ".join(str(row.get("text") or f"[{row.get('kind') or 'message'}]").split()),
            at=_parse_time(str(row["createdAt"])),
        )
    except (KeyError, ValueError):
        return None


async def _fetch(start: datetime, end: datetime) -> list[ChatMessage]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    config = settings()
    headers = {"Authorization": f"Bearer {_token()}"}
    seen: dict[str, ChatMessage] = {}
    async with (
        streamablehttp_client(config["url"], headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        before = end
        for _ in range(MAX_PAGES):
            result = await session.call_tool("list_messages", {
                "after": _iso(start), "before": _iso(before), "limit": PAGE_SIZE})
            if result.isError:
                text = result.content[0].text if result.content else "unknown error"
                raise UnifiedSourceError(f"hub refused list_messages: {text}")
            rows = json.loads(result.content[0].text) if result.content else []
            page = [m for m in (_record(r) for r in rows) if m is not None]
            fresh = [m for m in page if m.id not in seen]
            for message in fresh:
                seen[message.id] = message
            # The hub returns the newest `limit` rows before `before`,
            # inclusive, so a short page is the start of the window and a
            # page with nothing new means every row on it shared one
            # timestamp we have already passed.
            if len(page) < PAGE_SIZE or not fresh:
                break
            before = min(m.at for m in page)
        else:
            raise UnifiedSourceError(
                f"more than {MAX_PAGES * PAGE_SIZE} messages in one window; refusing to guess")
    return sorted(seen.values(), key=lambda m: (m.at, m.id))


def messages_between(start: datetime, end: datetime) -> list[ChatMessage]:
    """Every message across every chat in [start, end], oldest first."""

    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("messages_between needs timezone-aware datetimes")
    try:
        return asyncio.run(asyncio.wait_for(_fetch(start, end), CALL_TIMEOUT_SECONDS * 4))
    except UnifiedSourceError:
        raise
    except Exception as exc:  # transport, auth, JSON -- all mean "could not read"
        raise UnifiedSourceError(f"could not read Unified: {type(exc).__name__}: {exc}") from exc
