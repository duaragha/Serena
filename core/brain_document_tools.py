"""Brokered document creation and attachment delivery for the resident brain."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn
from core.document_delivery import (
    create_document as _create_document,
)
from core.document_delivery import (
    send_document_to_beeper as _send_document_to_beeper,
)
from core.document_delivery import (
    send_document_to_telegram as _send_document_to_telegram,
)

_DOCUMENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_DOCUMENT_SEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def _text(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "create_document",
    "Create a private, visible document in Raghav's ~/Documents/Serena folder "
    "when his current spoken turn asks for a document, Word file, Notepad text "
    "file, notes file, or list. format must be auto, docx, or txt. Use docx for "
    "a Word document and txt for Notepad or plain text. This creates a new file "
    "without overwriting an existing one. A broker independently re-reads his "
    "actual words. Never claim creation unless this returns CREATED.",
    {"filename": str, "content": str, "format": str},
    annotations=_DOCUMENT_WRITE,
)
async def create_document(args):
    result = await asyncio.to_thread(
        _create_document,
        str(args.get("filename") or "Serena Notes"),
        str(args.get("content") or ""),
        str(args.get("format") or "auto"),
        origin=current_turn(),
    )
    if not result.ok:
        return _text(
            "NOT CREATED. Reason: "
            + result.reason
            + ". Tell Raghav plainly that no document was created."
        )
    return _text(
        f"CREATED. filename={result.filename!r}; format={result.format}; "
        f"path={result.path!r}. The file now exists in Documents/Serena."
    )


@tool(
    "send_document_to_telegram",
    "Send one file previously created in ~/Documents/Serena to Raghav as a "
    "Telegram attachment, but only when his current spoken turn explicitly asks "
    "for Telegram. filename must be the plain filename returned by "
    "create_document, never a path. The broker confines attachments to that "
    "folder and independently re-reads his actual words. Never claim it was sent "
    "unless this returns SENT.",
    {"filename": str},
    annotations=_DOCUMENT_SEND,
)
async def send_document_to_telegram(args):
    result = await asyncio.to_thread(
        _send_document_to_telegram,
        str(args.get("filename") or ""),
        origin=current_turn(),
    )
    if not result.ok:
        return _text(
            "NOT SENT TO TELEGRAM. Reason: "
            + result.reason
            + ". Tell Raghav the attachment was not sent and why."
        )
    return _text(f"SENT TO TELEGRAM. attachment={result.filename!r}.")


@tool(
    "send_document_to_beeper",
    "Send one file previously created in ~/Documents/Serena through the "
    "official Beeper Desktop API, but only when his current spoken turn "
    "explicitly asks for Beeper. filename must be the plain filename returned "
    "by create_document, never a path. Delivery uses only the chat id pinned in "
    "~/.config/serena/beeper.env. It never searches for recipients. Never claim "
    "it was sent unless this returns SENT.",
    {"filename": str},
    annotations=_DOCUMENT_SEND,
)
async def send_document_to_beeper(args):
    result = await asyncio.to_thread(
        _send_document_to_beeper,
        str(args.get("filename") or ""),
        origin=current_turn(),
    )
    if not result.ok:
        return _text(
            "NOT SENT TO BEEPER. Reason: "
            + result.reason
            + ". Tell Raghav the attachment was not sent and why."
        )
    return _text(f"SENT TO BEEPER. attachment={result.filename!r}.")


DOCUMENT_TOOLS = (
    create_document,
    send_document_to_telegram,
    send_document_to_beeper,
)
DOCUMENT_TOOL_NAMES = [f"mcp__serena-documents__{item.name}" for item in DOCUMENT_TOOLS]


def document_tools_server():
    return create_sdk_mcp_server(name="serena-documents", tools=list(DOCUMENT_TOOLS))
