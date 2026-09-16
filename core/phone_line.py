"""Serena's text line to Raghav: one iMessage thread, both directions.

Outbound, every text starts with "serena:" and its hub message id is recorded.
Inbound, a message counts as a command only when it is new, is not one of
Serena's own, does not start with that prefix, and matches the small grammar
below. Everything else in the thread is conversation and is ignored, so a
stray text can never queue work.

The thread is his own number, so everything in it is authored by his account.
That is the authentication: the hub only shows this conversation to paired
devices, and only his phone can write into it. Commands are still bounded to
queue writes; dispatch stays with the reviewed scheduler.

Grammar (case-insensitive):
    task: <brief>            queue work (triaged like any phone brief)
    #<id> <answer>           answer the one question asked about task <id>
    status                   what is queued, running, and waiting on him
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

PREFIX = "serena:"
MAX_COMMANDS_PER_POLL = 10
# A self-addressed iMessage can surface twice (sent and received copies with
# different ids), so identical text inside this window is one command.
DUPLICATE_WINDOW_SECONDS = 600
_TASK = re.compile(r"^\s*task\s*[:\-]\s*(?P<brief>.+)$", re.IGNORECASE | re.DOTALL)
_ANSWER = re.compile(r"^\s*#(?P<id>\d{1,6})\s*[:\-]?\s*(?P<answer>.+)$", re.DOTALL)
_STATUS = re.compile(r"^\s*status\s*\??\s*$", re.IGNORECASE)


def send(text: str, *, key: str = "") -> bool:
    """Text Raghav. Returns True only when the hub accepted the message."""

    from core import unified_hub

    body = text.strip()
    if not body.lower().startswith(PREFIX):
        body = f"{PREFIX} {body}"
    return unified_hub.send_text(body, idempotency_key=key).ok


def available() -> bool:
    from core import unified_hub

    try:
        settings = unified_hub.settings()
    except unified_hub.UnifiedHubError:
        return False
    return bool(settings["paired"] and settings["conversation_id"])


def parse(text: str) -> tuple[str, dict[str, Any]] | None:
    text = (text or "").strip()
    if not text or text.lower().startswith(PREFIX):
        return None
    if match := _TASK.match(text):
        return "task", {"brief": match.group("brief").strip()}
    if match := _ANSWER.match(text):
        return "answer", {"task_id": int(match.group("id")),
                          "answer": match.group("answer").strip()}
    if _STATUS.match(text):
        return "status", {}
    return None


@dataclass
class PollReport:
    seen: int = 0
    commands: list[dict[str, Any]] = field(default_factory=list)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:24]


def _status_text() -> str:
    from memory import store

    lines = []
    for state, label in (("running", "running"), ("ready", "queued"),
                         ("needs_triage", "waiting on you"), ("blocked", "blocked")):
        rows = store.tasks_in_state(state)
        if rows:
            items = ", ".join(f"#{row['id']}" for row in rows[:8])
            lines.append(f"{label}: {items}")
    return "; ".join(lines) or "nothing queued"


def poll(*, now: float | None = None) -> PollReport:
    """Read new thread messages once and act on the commands among them."""

    from core import unified_hub
    from memory import store

    moment = time.time() if now is None else now
    report = PollReport()
    state = unified_hub._load()
    # The first poll only sets the watermark. History from before the line was
    # connected is never replayed as fresh commands.
    watermark = str(state.get("inbound_watermark") or "")
    messages = unified_hub.recent_messages(limit=50)
    if not watermark:
        newest = max((str(m.get("createdAt") or "") for m in messages), default="")
        unified_hub.configure(inbound_watermark=newest or "1970-01-01T00:00:00Z")
        return report
    ours = set(state.get("sent_message_ids") or [])
    handled = dict(state.get("inbound_handled") or {})
    newest = watermark
    for message in messages:
        created = str(message.get("createdAt") or "")
        if created <= watermark:
            continue
        message_id = str(message.get("id") or "")
        text = str(message.get("textPreview") or "")
        command = None
        if (message_id and message_id not in ours and not message.get("deletedAt")
                and message.get("kind") == "text"):
            command = parse(text)
        fingerprint = _fingerprint(text)
        if command is not None and len(report.commands) >= MAX_COMMANDS_PER_POLL:
            # The watermark stops before this one, so the next pass takes it.
            break
        newest = max(newest, created)
        report.seen += 1
        if command is None:
            continue
        if moment - float(handled.get(fingerprint, 0)) < DUPLICATE_WINDOW_SECONDS:
            continue
        handled[fingerprint] = moment
        kind, args = command
        outcome: dict[str, Any] = {"kind": kind, "message_id": message_id}
        try:
            if kind == "task":
                task = store.enqueue_task(args["brief"], source_id=f"imessage:{message_id}")
                outcome["task_id"] = task["id"]
                if task["state"] == "ready":
                    reply = f"got it, queued as #{task['id']}."
                else:
                    reply = (f"queued as #{task['id']} but that's too thin to hand off. "
                             f"what exactly should change, and in which project? "
                             f"reply \"#{task['id']} <details>\".")
                    store.mark_task_asked(task["id"])
            elif kind == "answer":
                task = store.answer_triage(args["task_id"], args["answer"])
                if task is None:
                    reply = f"#{args['task_id']} isn't waiting on an answer."
                elif task["state"] == "ready":
                    reply = f"thanks, #{task['id']} is queued."
                else:
                    reply = (f"#{task['id']} still isn't specific enough to hand off. "
                             f"it's parked; say what file or behaviour to change.")
                outcome["task_id"] = args["task_id"]
            else:
                reply = _status_text()
        except (ValueError, TimeoutError) as error:
            reply = f"couldn't take that: {error}"
            outcome["error"] = str(error)
        outcome["replied"] = send(reply, key=f"serena-reply-{message_id}")
        report.commands.append(outcome)
    cutoff = moment - DUPLICATE_WINDOW_SECONDS
    unified_hub.configure(
        inbound_watermark=newest,
        inbound_handled={k: v for k, v in handled.items() if float(v) >= cutoff},
    )
    return report
