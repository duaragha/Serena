"""Serena's own hands for her memory and knowledge base, on Raghav's word.

Kept out of core/brain_tools.py on purpose: that module is read-only by
construction and its docstring promises so. These are brokered writes,
gated by core.memory_authority against the real spoken turn, in the same
pattern as core/brain_work_tools.py.

Raghav's instruction, verbatim: "should be able to add, delete, edit
memories, knowledge everything honestly if i tell her to." The broker holds
the "if i tell her to" part; everything else is hers.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn
from core.memory_authority import authorize

_BROKERED_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_BROKERED_DELETE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

_REFUSAL = (
    "NOT DONE. Reason: {reason}. Tell Raghav plainly that you could not do it "
    "and why. Do not say it is saved, changed, or deleted."
)

_MEMORY_TYPES = ("user", "feedback", "project", "reference", "task", "loop", "general")


def _text(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "save_memory",
    "Save one new memory about Raghav when he asks you to remember something "
    "on a spoken turn. type is one of user, feedback, project, reference, "
    "task, loop, general. Write the content as a complete standalone fact "
    "with absolute dates. A broker independently re-reads his actual words, "
    "so never claim it is saved unless this returns SAVED.",
    {"content": str, "type": str},
    annotations=_BROKERED_WRITE,
)
async def save_memory(args):
    content = str(args.get("content") or "").strip()
    mem_type = str(args.get("type") or "general").strip().lower()
    if mem_type not in _MEMORY_TYPES:
        mem_type = "general"
    if not content:
        return _text(_REFUSAL.format(reason="empty content"))
    decision = authorize(
        "save_memory", origin=current_turn(), destructive=False, detail=content[:200]
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _save() -> int:
        from memory.store import add_memory

        return add_memory(content, mem_type)

    memory_id = await asyncio.to_thread(_save)
    return _text(f"SAVED. memory [{memory_id}] ({mem_type}).")


@tool(
    "edit_memory",
    "Rewrite an existing memory by id when Raghav asks you on a spoken turn "
    "to correct, update, or change something you have saved. Look the id up "
    "with search_memory first; the new content replaces the old entirely, so "
    "carry forward anything still true. A broker independently re-reads his "
    "actual words.",
    {"memory_id": int, "content": str},
    annotations=_BROKERED_WRITE,
)
async def edit_memory(args):
    content = str(args.get("content") or "").strip()
    try:
        memory_id = int(args.get("memory_id"))
    except (TypeError, ValueError):
        return _text(_REFUSAL.format(reason="no valid memory id"))
    if not content:
        return _text(_REFUSAL.format(reason="empty replacement content"))
    decision = authorize(
        "edit_memory",
        origin=current_turn(),
        destructive=True,
        detail=f"[{memory_id}] {content[:160]}",
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _edit() -> bool:
        from memory.store import get_memory, update_memory

        if get_memory(memory_id) is None:
            return False
        update_memory(memory_id, content=content)
        return True

    if not await asyncio.to_thread(_edit):
        return _text(_REFUSAL.format(reason=f"no memory with id {memory_id}"))
    return _text(f"UPDATED. memory [{memory_id}] now holds the new content.")


@tool(
    "delete_memory",
    "Delete a memory by id when Raghav asks you on a spoken turn to forget "
    "or remove it. Look the id up with search_memory first and tell him what "
    "you are deleting. Permanent. A broker independently re-reads his actual "
    "words, so never claim it is gone unless this returns DELETED.",
    {"memory_id": int},
    annotations=_BROKERED_DELETE,
)
async def delete_memory(args):
    try:
        memory_id = int(args.get("memory_id"))
    except (TypeError, ValueError):
        return _text(_REFUSAL.format(reason="no valid memory id"))
    decision = authorize(
        "delete_memory", origin=current_turn(), destructive=True, detail=f"[{memory_id}]"
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _delete() -> tuple[bool, str]:
        from memory.store import delete_memory as store_delete
        from memory.store import get_memory

        record = get_memory(memory_id)
        if record is None:
            return False, ""
        summary = " ".join(str(record.get("content", "")).split())[:120]
        return store_delete(memory_id), summary

    deleted, summary = await asyncio.to_thread(_delete)
    if not deleted:
        return _text(_REFUSAL.format(reason=f"no memory with id {memory_id}"))
    return _text(f"DELETED. memory [{memory_id}] ({summary!r}) is gone.")


@tool(
    "save_knowledge",
    "Write or replace one markdown note inside a knowledge topic when Raghav "
    "asks you on a spoken turn to save research or notes. topic is a "
    "kebab-case slug (a new slug creates the topic), filename is a .md name "
    "like notes.md. A broker independently re-reads his actual words.",
    {"topic": str, "filename": str, "content": str},
    annotations=_BROKERED_WRITE,
)
async def save_knowledge(args):
    topic = str(args.get("topic") or "").strip().strip("/").lower()
    filename = str(args.get("filename") or "notes.md").strip()
    content = str(args.get("content") or "").strip()
    if not topic or not content:
        return _text(_REFUSAL.format(reason="topic and content are both required"))
    if "/" in filename or not filename.endswith(".md"):
        return _text(_REFUSAL.format(reason="filename must be a plain .md name"))
    if not all(part.isalnum() for part in topic.replace("-", " ").split()):
        return _text(_REFUSAL.format(reason=f"{topic!r} is not a clean topic slug"))
    decision = authorize(
        "save_knowledge",
        origin=current_turn(),
        destructive=False,
        detail=f"{topic}/{filename}",
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _write() -> str:
        from core.config import KNOWLEDGE_DIR

        directory = KNOWLEDGE_DIR / topic
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(content + "\n", encoding="utf-8")
        return str(directory / filename)

    path = await asyncio.to_thread(_write)
    return _text(f"SAVED. knowledge note at {path}.")


@tool(
    "delete_knowledge_topic",
    "Delete an entire knowledge topic by slug when Raghav asks you on a "
    "spoken turn to remove it. Confirm the slug with search_knowledge first "
    "and tell him what you are deleting. Permanent. A broker independently "
    "re-reads his actual words.",
    {"topic": str},
    annotations=_BROKERED_DELETE,
)
async def delete_knowledge_topic(args):
    topic = str(args.get("topic") or "").strip().strip("/")
    if not topic:
        return _text(_REFUSAL.format(reason="no topic given"))
    decision = authorize(
        "delete_knowledge_topic", origin=current_turn(), destructive=True, detail=topic
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _delete() -> bool:
        from knowledge.reader import delete_topic

        return delete_topic(topic)

    if not await asyncio.to_thread(_delete):
        return _text(_REFUSAL.format(reason=f"no knowledge topic called {topic!r}"))
    return _text(f"DELETED. knowledge topic {topic!r} is gone.")


MEMORY_TOOLS = (
    save_memory,
    edit_memory,
    delete_memory,
    save_knowledge,
    delete_knowledge_topic,
)
MEMORY_TOOL_NAMES = [f"mcp__serena-memory__{item.name}" for item in MEMORY_TOOLS]


def memory_tools_server():
    return create_sdk_mcp_server(name="serena-memory", tools=list(MEMORY_TOOLS))
