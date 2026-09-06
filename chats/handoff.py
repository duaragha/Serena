"""Cross-agent session handoff briefing generator.

Reads a Claude Code or Codex CLI session and emits a HANDOFF.md style
markdown briefing the receiving agent can read to pick up where the
prior agent left off.

This whole feature is intentionally isolated to one module + one route +
a JS block in ui/web.py marked with HANDOFF FEATURE banners. To remove
the feature: delete this file, the route, and the JS block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.indexer import get_session
from core.parser import parse_full as _parse_claude_full

# How many of the most recent user/assistant pairs to inline verbatim.
_TAIL_PAIRS = 10
# How many tool calls to summarize in the activity table.
_TOOL_TAIL = 30
# Cap on length of any single quoted message in the verbatim section.
_MSG_CHAR_CAP = 1200


@dataclass
class _Msg:
    role: str  # "user" | "assistant"
    text: str
    tool_name: str | None = None
    tool_input: str | None = None


def build_handoff_briefing(session_id: str) -> dict:
    """Return {ok, briefing, agent, cwd, title} or {ok: False, error}."""
    if session_id == "new" or session_id.startswith("new-"):
        return {
            "ok": False,
            "error": "New chat has not created a session yet. Send one message, then try again.",
        }
    session = get_session(session_id)
    if not session:
        return {"ok": False, "error": f"Session {session_id} not found"}

    file_path = Path(session.get("file_path") or "")
    if not file_path.exists():
        return {"ok": False, "error": f"Session file missing: {file_path}"}

    agent = (session.get("agent") or "claude").lower()
    cwd = session.get("cwd") or session.get("last_cwd") or ""
    title = session.get("display_title") or session.get("title") or "Untitled chat"

    if agent == "gemini":
        transcript = _gemini_transcript(file_path)
        if transcript is None:
            return {
                "ok": False,
                "error": (
                    "This Gemini chat has no readable transcript on disk, so there is "
                    "nothing to brief from this side. Hand off from the Claude or Codex "
                    "chat in this thread instead."
                ),
            }
        msgs = _parse_gemini(transcript)
    elif agent == "codex":
        msgs = _parse_codex(file_path)
    else:
        msgs = _parse_claude(file_path)

    if not msgs:
        return {"ok": False, "error": "No messages to summarize"}

    briefing = _render_briefing(
        agent_from=agent,
        title=title,
        cwd=cwd,
        msgs=msgs,
    )
    return {
        "ok": True,
        "briefing": briefing,
        "agent": agent,
        "cwd": cwd,
        "title": title,
    }


def _parse_claude(file_path: Path) -> list[_Msg]:
    out: list[_Msg] = []
    for m in _parse_claude_full(file_path):
        if m.role not in ("user", "assistant"):
            continue
        out.append(
            _Msg(
                role=m.role,
                text=(m.text or "").strip(),
                tool_name=m.tool_name,
                tool_input=m.tool_input,
            )
        )
    return out


def _gemini_transcript(file_path: Path) -> Path | None:
    """Antigravity indexes a conversation by a file it cannot read.

    A conversation that exists as both a ``.db`` and a ``brain/`` transcript is
    indexed by the ``.db``, since that is the file whose mtime tracks the
    conversation. The protobuf inside it has no published schema; the transcript
    beside it is plain JSONL of the same turns. So resolve through the id rather
    than reading whatever path the index happened to store.
    """
    from core.gemini_scanner import conversation_id_for, transcript_path

    if file_path.name == "transcript.jsonl":
        return file_path
    return transcript_path(conversation_id_for(file_path))


def _parse_gemini(file_path: Path) -> list[_Msg]:
    """Antigravity turns, via the shared reader in ``core.gemini_scanner``."""
    from core.gemini_scanner import read_turns

    return [
        _Msg(
            role=turn["role"],
            text=turn["text"],
            tool_name=turn["tool_name"],
            tool_input=turn["tool_input"],
        )
        for turn in read_turns(file_path)
    ]


def _parse_codex(file_path: Path) -> list[_Msg]:
    """Codex rollout records, via the shared reader in ``core.codex_records``.

    This used to walk the file itself looking for ``event_msg`` payloads named
    ``user_message`` and ``agent_message``. Codex has since moved turns into
    ``response_item`` records and wrapped the events in ``item_completed``, so
    on a current rollout that search matched nothing and the handoff failed with
    "No messages to summarize" -- the conversation was all there, in shapes this
    module had never been taught. ``codex_records`` already knew both, having
    been fixed for the sidebar; there is no reason for a second reader here.

    Turns follow that module's rule: the events win when a file carries both,
    or the same turn counts twice. Tool calls only ever appear as
    ``response_item``, so they are kept either way and merged in file order.
    """
    from core import codex_records

    events: list[_Msg] = []
    items: list[_Msg] = []
    saw_event_turn = False

    for record in codex_records.iter_records(file_path):
        found = codex_records.message_of(record)
        if found is not None:
            shape, role, text = found
            message = _Msg(role=role, text=text.strip())
            if shape == "event":
                saw_event_turn = True
                events.append(message)
            else:
                items.append(message)
            continue

        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") not in ("function_call", "custom_tool_call"):
            continue
        args = payload.get("arguments")
        if args is None:
            args = payload.get("input") or ""
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        call = _Msg(
            role="assistant",
            text="",
            tool_name=str(payload.get("name") or "tool"),
            tool_input=str(args)[:200],
        )
        # A tool call belongs to whichever transcript ends up being used, and
        # only one of them is returned.
        events.append(call)
        items.append(call)

    return events if saw_event_turn else items


def _render_briefing(*, agent_from: str, title: str, cwd: str, msgs: list[_Msg]) -> str:
    user_msgs = [m for m in msgs if m.role == "user" and m.text]
    asst_msgs = [m for m in msgs if m.role == "assistant" and m.text]
    tool_msgs = [m for m in msgs if m.tool_name]

    goal = user_msgs[0].text if user_msgs else ""
    last_asst = asst_msgs[-1].text if asst_msgs else ""

    lines: list[str] = []
    lines.append("# Handoff Briefing")
    lines.append("")
    lines.append(f"_Source agent_: **{agent_from.title()}**  ")
    lines.append(f"_Source chat_: {title}  ")
    if cwd:
        lines.append(f"_Working directory_: `{cwd}`  ")
    lines.append("")
    lines.append("> You're being handed an in-progress conversation from a previous AI session. ")
    lines.append("> Read this briefing, re-ground in the current state of the code (re-read any ")
    lines.append("> files mentioned below — don't trust stale tool output from the other agent), ")
    lines.append("> then continue from the **Next step** at the bottom.")
    lines.append("")

    # Goal
    if goal:
        lines.append("## Goal")
        lines.append("")
        lines.append(_blockquote(goal[:1500]))
        lines.append("")

    # Decisions / constraints — heuristic on assistant messages
    decisions = _extract_decisions(asst_msgs)
    if decisions:
        lines.append("## Decisions & constraints")
        lines.append("")
        for d in decisions[:10]:
            lines.append(f"- {d}")
        lines.append("")

    # Files touched
    files = _extract_files(tool_msgs)
    if files:
        lines.append("## Files touched")
        lines.append("")
        for f in files[:25]:
            lines.append(f"- `{f}`")
        lines.append("")

    # Tool activity (last N)
    if tool_msgs:
        recent = tool_msgs[-_TOOL_TAIL:]
        lines.append("## Recent tool activity")
        lines.append("")
        lines.append("| Tool | Target |")
        lines.append("|------|--------|")
        for t in recent:
            tgt = (t.tool_input or "").replace("|", "\\|").replace("\n", " ")
            tgt = tgt[:80]
            lines.append(f"| {t.tool_name} | {tgt} |")
        lines.append("")

    # Verbatim tail
    pairs = _last_pairs(msgs, _TAIL_PAIRS)
    if pairs:
        lines.append("## Last exchanges (verbatim)")
        lines.append("")
        for m in pairs:
            tag = "**You (user)**" if m.role == "user" else "**Previous agent**"
            text = m.text
            if len(text) > _MSG_CHAR_CAP:
                text = text[: _MSG_CHAR_CAP] + " …[truncated]"
            lines.append(f"### {tag}")
            lines.append("")
            lines.append(_blockquote(text))
            lines.append("")

    # Next step inferred
    next_step = _infer_next_step(last_asst)
    lines.append("## Next step")
    lines.append("")
    lines.append(next_step)
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _blockquote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.splitlines() if ln) or "> _empty_"


_DECISION_RE = re.compile(
    r"(?:^|[\.\n])\s*(?:I(?:'ll|'ve| will| have|)|we(?:'ll|'ve| will| have|)|let'?s|going to|decided to|chose to|switched to|instead of|because)\b[^.\n!?]{8,200}",
    re.IGNORECASE,
)


def _extract_decisions(asst_msgs: list[_Msg]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in asst_msgs:
        for match in _DECISION_RE.finditer(m.text or ""):
            chunk = match.group(0).strip(" .\n").lstrip(".")
            chunk = re.sub(r"\s+", " ", chunk).strip()
            if len(chunk) < 20:
                continue
            key = chunk.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(chunk[:200])
            if len(out) >= 12:
                return out
    return out


_FILE_TOOLS = {"Edit", "Write", "Read", "MultiEdit", "NotebookEdit"}


def _extract_files(tool_msgs: list[_Msg]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in tool_msgs:
        if m.tool_name not in _FILE_TOOLS:
            continue
        candidate = (m.tool_input or "").strip().splitlines()[0] if m.tool_input else ""
        candidate = candidate.split(" ")[0].strip("\"'`")
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def _last_pairs(msgs: list[_Msg], n_pairs: int) -> list[_Msg]:
    convo = [m for m in msgs if m.text and m.role in ("user", "assistant")]
    take = n_pairs * 2
    return convo[-take:]


def _infer_next_step(last_assistant_text: str) -> str:
    if not last_assistant_text:
        return "_No final assistant message — re-ask the user what they want next._"
    last = last_assistant_text.strip()
    candidates = [ln.strip() for ln in last.splitlines() if ln.strip()]
    for ln in reversed(candidates):
        low = ln.lower()
        if any(
            kw in low
            for kw in ("next", "todo", "should", "need to", "want me to", "left to do", "remaining")
        ):
            return _blockquote(ln[:600]).replace("> ", "")
    tail = "\n".join(candidates[-3:])
    return f"Pick up from where the previous agent left off:\n\n{_blockquote(tail[:800])}"
