"""Serena's text line to Raghav: one thread, both directions.

Three transports sit behind the same grammar, and ``phone-line.json`` says
which one owns the line:

- ``telegram`` — her bot in his private chat (core.telegram_line). The bot is a
  real second identity, so nothing is doubled and no prefix is needed.
- ``bluebubbles`` — her own Apple ID on her own server (core.bluebubbles_line):
  she is an ordinary contact, so "who wrote this" is just isFromMe. Pointed at
  his own self-thread instead, the prefix below tells her texts from his.
- the Unified hub self-thread, described below, as the floor.

Outbound on a self-thread, every text starts with "serena:" and its hub message
id is recorded.
Inbound, a message counts as a command only when it is new, is not one of
Serena's own, and does not start with that prefix. On a shared thread it must
also match the small grammar below: everything else there is conversation and
is answered by the resident brain (`core.text_conversation`), never queued,
so a stray text can never queue work.

A *dedicated* line has no such conversation to confuse. Her Telegram bot owns
its own chat and the only reason that chat exists is for him to reach her, so
prose there is addressed to her and gets read rather than dropped -- but read
by Serena herself, through `core.phone_intent`, not by a regex. A regex cannot
tell work from a question, and the one time it tried it filed "what's the cue
right now?" as a task. Silence is the other failure mode that matters here: a
text that queues nothing and answers nothing is indistinguishable from a dead
bot, so every message he sends gets an answer.

The thread is his own number, so everything in it is authored by his account.
That is the authentication: the hub only shows this conversation to paired
devices, and only his phone can write into it. Commands are still bounded to
queue writes; dispatch stays with the reviewed scheduler.

Grammar (case-insensitive):
    task: <brief>            queue work (triaged like any phone brief)
    status                   what is queued, running, and waiting on him
    how many tasks are left  any plain question about the queue reads as status
    #<id> <answer>           answer the one question asked about task <id>
    retry #<id>              rerun a blocked task's Fleet run from where it stopped
    swapped                  he just refreshed his number's registration
    yes <nonce> / no <nonce> answer a pending approval code (text only)
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
# He asks in English, not in grammar: anything that asks about the queue's
# state counts as `status`, because a missed question reads as her ignoring him.
_STATUS_QUESTION = re.compile(
    r"^\s*(?:how many|what|which|any|hows|how's|how is|how are)\b.{0,60}?"
    r"\b(tasks?|jobs?|work|queue|prs?)\b.*$", re.IGNORECASE | re.DOTALL)
_SWAPPED = re.compile(r"^\s*swapped\s*[.!]?\s*$", re.IGNORECASE)
_RETRY = re.compile(r"^\s*retry\s+#?(?P<id>\d{1,6})\s*$", re.IGNORECASE)
# Approval replies name their 4-digit code and nothing else: a bare "yes" on
# a line that also carries conversation is an accident waiting to happen.
_APPROVE = re.compile(r"^\s*yes\s+(?P<nonce>\d{4})\s*$", re.IGNORECASE)
_DENY = re.compile(r"^\s*no\s+(?P<nonce>\d{4})\s*$", re.IGNORECASE)


class _HubBackend:
    """His own number's self-thread, through the Unified hub."""

    name = "hub"
    initial_watermark = ""
    # A thread he also uses for his own notes: only the grammar queues work.
    dedicated = False
    # Hub history predates the line, so connecting must not replay it.
    replays_pending_on_connect = False

    def available(self) -> bool:
        from core import unified_hub

        try:
            settings = unified_hub.settings()
        except unified_hub.UnifiedHubError:
            return False
        return bool(settings["paired"] and settings["conversation_id"])

    def load_state(self) -> dict[str, Any]:
        from core import unified_hub

        return unified_hub._load()

    def save_state(self, **fields: Any) -> None:
        from core import unified_hub

        unified_hub.configure(**fields)

    def messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        from core import unified_hub

        ours = set(state.get("sent_message_ids") or [])
        rows = []
        for message in unified_hub.recent_messages(limit=50):
            message_id = str(message.get("id") or "")
            rows.append({
                "id": message_id,
                "text": str(message.get("textPreview") or ""),
                "created": str(message.get("createdAt") or ""),
                "own": not message_id or message_id in ours,
                "deleted": bool(message.get("deletedAt")),
                "kind": message.get("kind"),
            })
        return rows

    def send(self, text: str, key: str) -> bool:
        from core import unified_hub

        body = text.strip()
        if not body.lower().startswith(PREFIX):
            body = f"{PREFIX} {body}"
        return unified_hub.send_text(body, idempotency_key=key).ok


class _FileStateBackend:
    """Watermark file for transports that keep no hub state of their own."""

    state_file = "phone-line-state.json"

    def _state_path(self):
        from pathlib import Path

        return Path.home() / ".local" / "state" / "serena" / self.state_file

    def load_state(self) -> dict[str, Any]:
        import json

        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, **fields: Any) -> None:
        import json
        import os

        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.load_state()
        data.update(fields)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, path)


class _BlueBubblesBackend(_FileStateBackend):
    """Serena's own Apple ID; she is simply a contact in his Messages."""

    name = "bluebubbles"
    initial_watermark = 0
    dedicated = False
    replays_pending_on_connect = False

    def available(self) -> bool:
        from core import bluebubbles_line

        return bluebubbles_line.enabled()

    def messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        from core import bluebubbles_line

        # On his own thread every message is his, so hers are the prefixed ones.
        prefix = PREFIX if bluebubbles_line.self_thread() else ""
        return bluebubbles_line.recent_messages(limit=50, own_prefix=prefix)

    def send(self, text: str, key: str) -> bool:
        from core import bluebubbles_line

        body = text.strip()
        if bluebubbles_line.self_thread():
            # The prefix is what tells her messages from his in that thread.
            if not body.lower().startswith(PREFIX):
                body = f"{PREFIX} {body}"
        elif body.lower().startswith(PREFIX):
            body = body[len(PREFIX):].strip()
        try:
            bluebubbles_line.send_text(body)
        except bluebubbles_line.BlueBubblesLineError:
            return False
        return True


class _TelegramBackend(_FileStateBackend):
    """Her bot in his private Telegram chat; a real second identity."""

    name = "telegram"
    initial_watermark = 0
    state_file = "phone-line-telegram.json"
    # Her bot's chat exists for one purpose, so plain text is a brief.
    dedicated = True
    # getUpdates only ever returns updates no one has confirmed yet, so what is
    # waiting on connect is unhandled work, not history. Adopting it as a
    # watermark would eat the very message that made him set the line up.
    replays_pending_on_connect = True

    def available(self) -> bool:
        from core import telegram_line

        return telegram_line.enabled()

    def messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        from core import telegram_line

        try:
            offset = int(state.get("inbound_watermark") or 0)
        except (TypeError, ValueError):
            offset = 0
        return telegram_line.recent_messages(offset=offset)

    def send(self, text: str, key: str) -> bool:
        from core import telegram_line

        # The bot is its own sender, so the "serena:" disambiguator is noise.
        body = text.strip()
        if body.lower().startswith(PREFIX):
            body = body[len(PREFIX):].strip()
        try:
            return telegram_line.send_text(body)
        except telegram_line.TelegramLineError:
            return False


def _backend():
    """Whichever transport phone-line.json points at; the hub is the floor."""

    for candidate in (_TelegramBackend(), _BlueBubblesBackend()):
        if candidate.available():
            return candidate
    return _HubBackend()


def backend_name() -> str:
    return _backend().name


def send(text: str, *, key: str = "") -> bool:
    """Text Raghav. Returns True only when the transport accepted the message."""

    return _backend().send(text, key)


def send_fallback(text: str, *, key: str = "") -> bool:
    """Reach him through the hub self-thread even when her server is down."""

    hub = _HubBackend()
    return hub.available() and hub.send(text, key)


def available() -> bool:
    return _backend().available()


def parse(text: str) -> tuple[str, dict[str, Any]] | None:
    """Read one message as an explicit command, or None to let her read it.

    This is the deterministic half: the commands he types on purpose, matched
    for free. Anything it does not claim is not "not a command", it is prose,
    and prose goes to `core.phone_intent` rather than to a guess.
    """

    text = (text or "").strip()
    if not text or text.lower().startswith(PREFIX):
        return None
    if match := _TASK.match(text):
        return "task", {"brief": match.group("brief").strip()}
    if match := _ANSWER.match(text):
        return "answer", {"task_id": int(match.group("id")),
                          "answer": match.group("answer").strip()}
    if _STATUS.match(text) or _STATUS_QUESTION.match(text):
        return "status", {}
    if _SWAPPED.match(text):
        return "swapped", {}
    if match := _RETRY.match(text):
        return "retry", {"task_id": int(match.group("id"))}
    if match := _APPROVE.match(text):
        return "approve", {"nonce": match.group("nonce")}
    if match := _DENY.match(text):
        return "deny", {"nonce": match.group("nonce")}
    return None


@dataclass
class PollReport:
    seen: int = 0
    commands: list[dict[str, Any]] = field(default_factory=list)


def _read_with_her_brain(text: str) -> tuple[str, dict[str, Any]] | None:
    """Let Serena herself read prose he texted, and degrade safely if she can't.

    Her reply is either work to queue or the answer to send back. When the
    resident brain cannot be reached, `triage` still queues text that is plainly
    actionable and asks about anything else, because filing a question as a task
    is the mistake that wastes his time.
    """

    from core import phone_intent

    try:
        read = phone_intent.read(text, queue=_grounding())
    except Exception:
        read = None
    if read is None:
        read = phone_intent.triage(text)
    kind, body = read
    return ("task", {"brief": body}) if kind == "task" else ("say", {"say": body})


def _fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:24]


def _resolve_attachments(chat: list[dict[str, Any]], texts: list[str],
                         images: list[dict[str, str]]) -> int:
    """Download what he texted: photos ride to the brain, voice notes as text.

    Anything that fails to download, validate or transcribe is skipped: a
    broken attachment must not eat the rest of the burst. Returns how many
    failed, so a burst that lost everything can still say so out loud.
    """

    import base64
    import os
    import tempfile
    from contextlib import suppress
    from pathlib import Path

    from core import bluebubbles_line, image_input, text_conversation

    failed = 0
    for message in chat:
        for attachment in message.get("attachments") or []:
            guid = str((attachment or {}).get("guid") or "")
            mime = str((attachment or {}).get("mime") or "")
            if not guid:
                continue
            try:
                raw = bluebubbles_line.download_attachment(guid)
            except bluebubbles_line.BlueBubblesLineError:
                failed += 1
                continue
            if mime.startswith("image/"):
                if len(images) >= image_input.MAX_IMAGES_PER_TURN:
                    continue
                try:
                    images.append(image_input.clean_image_input({
                        "media_type": mime,
                        "data": base64.b64encode(raw).decode("ascii"),
                    }))
                except ValueError:
                    failed += 1
                    continue
            elif mime.startswith("audio/"):
                suffix = ".caf" if "caf" in mime else ".bin"
                fd, name = tempfile.mkstemp(suffix=suffix,
                                            prefix="serena-voice-note-")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(raw)
                    texts.append("[voice note] " + text_conversation.transcribe_audio(
                        Path(name)))
                except text_conversation.ConversationError:
                    failed += 1
                    continue
                finally:
                    with suppress(OSError):
                        os.unlink(name)
    return failed


def _bluebubbles_sender_ok(message: dict[str, Any], allowed: str) -> bool:
    """The poll-side twin of the webhook's sender gate.

    Same rule, same reason: the service field (never the chat prefix) must
    say iMessage, and the handle must be his allowlisted address — spelling
    aside, so a number and its +E.164 form are the same person. Rows that
    predate sender metadata (other backends, older rows) carry neither key
    and stay allowed — the gate only bites on rows that speak it. The caller
    reads the configured address once per poll and passes it in.
    """

    service = message.get("service")
    if service is not None and service != "iMessage":
        return False
    if "handle" not in message:
        return True
    from core import bluebubbles_line

    return bluebubbles_line.same_handle(
        str(message.get("handle") or ""), allowed)


def _tapback(line: Any, message_id: str) -> None:
    """A thumbs-up on his handled command. Best-effort, BlueBubbles only."""

    if line.name != "bluebubbles" or not message_id:
        return
    try:
        from core import bluebubbles_line

        if bluebubbles_line.enabled():
            bluebubbles_line.react(
                bluebubbles_line.chat_guid(), message_id, "like")
    except KeyError:
        pass


def _answer_approval(nonce: str, *, approved: bool,
                     outcome: dict[str, Any]) -> str:
    """Answer one pending approval by code, over text (never tier 4)."""

    from core import approvals

    try:
        record = approvals.ApprovalBroker().answer_nonce(
            nonce, approved=approved, surface="text")
    except approvals.ApprovalError as error:
        outcome["error"] = str(error)
        return f"{error}."
    outcome["confirmation_id"] = record.confirmation_id
    verb = "approved" if approved else "denied"
    return f"{verb}: {record.capability}."


def _answer_chat(line: Any, chat: list[dict[str, Any]],
                 messages: list[dict[str, Any]], handled: dict[str, Any],
                 moment: float) -> dict[str, Any] | None:
    """Answer one burst of his free-form texts as a single brain turn.

    Commands never land here: the poll loop only buffers texts the grammar
    did not claim. The joined burst shares the command duplicate window, so
    the sent + received copies of one self-addressed iMessage answer once.
    """

    import contextlib

    from core import text_conversation

    texts = [str(m["text"]).strip() for m in chat]
    texts = [t for t in texts if t]
    images: list[dict[str, str]] = []
    failed = 0
    if line.name == "bluebubbles":
        failed = _resolve_attachments(chat, texts, images)
    if not texts and not images:
        if not failed:
            return None
        # Everything he sent broke on the way in. Silence would read as a
        # dead line, so say the honest thing instead.
        outcome = {"kind": "chat", "message_ids": [m["id"] for m in chat],
                   "error": "attachments failed",
                   "replied": line.send(
                       "i couldn't open what you just sent — "
                       "try it again?",
                       key=f"serena-chat-{chat[0]['id']}-unreadable")}
        return outcome
    guids = sorted({str((a or {}).get("guid") or "")
                    for m in chat for a in (m.get("attachments") or [])}
                   - {""})
    fingerprint = _fingerprint("\n".join([*texts, *guids]) or "[attachments]")
    if (fingerprint in handled
            and moment - float(handled[fingerprint]) < DUPLICATE_WINDOW_SECONDS):
        return None
    handled[fingerprint] = moment
    outcome: dict[str, Any] = {
        "kind": "chat", "message_ids": [m["id"] for m in chat]}
    keepalive: Any = contextlib.nullcontext()
    if line.name == "bluebubbles":
        from core import bluebubbles_line

        if bluebubbles_line.enabled():
            chat_guid = bluebubbles_line.chat_guid()
            bluebubbles_line.mark_read(chat_guid)
            keepalive = bluebubbles_line.typing_keepalive(chat_guid)
    try:
        with keepalive:
            bubbles = text_conversation.answer(
                texts, context_messages=messages, images=images or None)
    except text_conversation.ConversationError as error:
        outcome["error"] = str(error)
        bubbles = ["i can't reach my own head right now, say that again in a bit."]
    if not bubbles:
        return None
    sent = [line.send(bubble, key=f"serena-chat-{chat[0]['id']}-{i}")
            for i, bubble in enumerate(bubbles)]
    outcome["replied"] = all(sent)
    return outcome


_TASK_LABELS = (("running", "running"), ("ready", "queued"),
                ("needs_triage", "waiting on you"), ("blocked", "blocked"))


def _task_rows() -> list[tuple[str, dict[str, Any]]]:
    from memory import store

    rows: list[tuple[str, dict[str, Any]]] = []
    for state, label in _TASK_LABELS:
        for row in store.tasks_in_state(state)[:8]:
            rows.append((label, row))
    return rows


def _status_text() -> str:
    """His `status` reply: one line a task, carrying why a failure failed.

    "blocked: #1054" is not a status, it is a riddle. The reason is already on
    the task row, put there by the dispatcher when the run ended.
    """

    lines = []
    for label, row in _task_rows():
        reason = " ".join(str(row.get("result") or "").split())
        reason = reason[len("failed:"):].strip() if reason.lower().startswith("failed:") else reason
        lines.append(f"#{row['id']} {label}" + (f" — {reason[:160]}" if reason else ""))
    return "\n".join(lines) or "nothing queued"


def _grounding() -> str:
    """The same facts, plus what each task actually asked for, for her turn.

    She cannot answer "how's 1054?" from a state word. Everything here is read
    off the task row, so a reply never depends on a tool call that might come
    back empty for a run that has already finished.
    """

    lines = []
    for label, row in _task_rows():
        bits = [f"#{row['id']} {label}"]
        run = str(row.get("run_id") or "")
        if run:
            bits.append(f"fleet {run[:8]}")
        reason = " ".join(str(row.get("result") or "").split())
        if reason:
            bits.append(reason[:200])
        brief = " ".join(str(row.get("content") or "").split())
        if brief:
            bits.append(f"he asked for: {brief[:160]}")
        lines.append(" | ".join(bits))
    return "\n".join(lines) or "nothing queued"


def _retry(task_id: int) -> str:
    from fleet.supervisor import retry_run
    from memory import store

    task = store.get_memory(task_id)
    if not task or task.get("type") != "task" or task.get("state") != "blocked":
        return f"#{task_id} isn't blocked, nothing to retry."
    run_id = str(task.get("run_id") or "")
    if not run_id:
        return f"#{task_id} never reached fleet; send it again as a new task."
    try:
        retry_run(run_id)
    except Exception as error:
        return f"fleet refused to retry #{task_id}: {str(error)[:200]}"
    if not store.reopen_task_run(task_id, run_id):
        return f"#{task_id} changed while retrying; check status."
    return f"retrying #{task_id} (fleet {run_id[:8]})."


def _record_swap(moment: float) -> str:
    import json
    from pathlib import Path

    path = Path.home() / ".local" / "state" / "serena" / "phone-health.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.update(number_swapped_at=moment, swap_reminded=False, number_alerted=False,
                 number_checked_at=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return "noted. next sim refresh reminder in 6 weeks; i'll check the number tomorrow."


def poll(*, now: float | None = None) -> PollReport:
    """Read new thread messages once and act on the commands among them."""

    from memory import store

    moment = time.time() if now is None else now
    report = PollReport()
    line = _backend()
    state = line.load_state()
    messages = line.messages(state)
    # On a shared thread the first poll only sets the watermark: history from
    # before the line was connected is never replayed as fresh commands. A
    # dedicated line has no such history -- what is waiting there is work he
    # sent her -- so it starts from zero and handles it.
    watermark = state.get("inbound_watermark")
    if watermark in (None, "") and line.replays_pending_on_connect:
        watermark = line.initial_watermark
    if watermark in (None, ""):
        newest = max((m["created"] for m in messages), default=line.initial_watermark)
        line.save_state(inbound_watermark=newest if newest else (
            "1970-01-01T00:00:00Z" if line.name == "hub" else 0))
        return report
    handled = dict(state.get("inbound_handled") or {})
    newest = watermark
    chat: list[dict[str, Any]] = []
    allowed_sender = ""
    if line.name == "bluebubbles":
        from core import bluebubbles_line

        allowed_sender = str(
            bluebubbles_line.settings().get("address") or "")
    for message in messages:
        created = message["created"]
        if created <= watermark:
            continue
        message_id = message["id"]
        text = message["text"]
        command = None
        his = bool(message_id) and not message["own"] and not message["deleted"]
        # Answerable means his and safe to read: a tapback echo is not a
        # message, and on the BlueBubbles line the sender gate applies — the
        # service field must say iMessage and the handle must be his, the
        # same rule the webhook enforces. SMS through the same Mac is
        # spoofable and refused outright.
        answerable = (
            his
            and not message.get("associated_guid")
            and (line.name != "bluebubbles"
                 or _bluebubbles_sender_ok(message, allowed_sender))
        )
        if answerable and message["kind"] == "text":
            command = parse(text)
            if command is None and line.dedicated:
                command = _read_with_her_brain(text)
        fingerprint = _fingerprint(text)
        if command is not None and len(report.commands) >= MAX_COMMANDS_PER_POLL:
            # The watermark stops before this one, so the next pass takes it.
            break
        newest = max(newest, created)
        report.seen += 1
        if command is not None and command[0] == "say":
            # She already wrote the answer; there is nothing to queue.
            if fingerprint in handled and moment - float(handled[fingerprint]) < DUPLICATE_WINDOW_SECONDS:
                continue
            handled[fingerprint] = moment
            report.commands.append({
                "kind": "say", "message_id": message_id,
                "replied": send(command[1]["say"], key=f"serena-reply-{message_id}"),
            })
            continue
        if command is None:
            # Only a dedicated line owes an answer to everything through the
            # intent reader. On a shared thread his texts, photos and voice
            # notes buffer into one conversation turn, answered after the
            # loop; a sticker or an empty caption is still nothing to read.
            if (answerable and message["kind"] in ("text", "image", "audio")
                    and not line.dedicated):
                chat.append(message)
            elif answerable and line.dedicated:
                send("i can only read text. type what you want done.",
                     key=f"serena-unreadable-{message_id}")
            continue
        if fingerprint in handled and moment - float(handled[fingerprint]) < DUPLICATE_WINDOW_SECONDS:
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
            elif kind == "swapped":
                reply = _record_swap(moment)
            elif kind == "retry":
                reply = _retry(args["task_id"])
                outcome["task_id"] = args["task_id"]
            elif kind in ("approve", "deny"):
                reply = _answer_approval(
                    args["nonce"], approved=kind == "approve",
                    outcome=outcome)
            else:
                reply = _status_text()
        except (ValueError, TimeoutError) as error:
            reply = f"couldn't take that: {error}"
            outcome["error"] = str(error)
        outcome["replied"] = send(reply, key=f"serena-reply-{message_id}")
        if "error" not in outcome:
            _tapback(line, message_id)
        report.commands.append(outcome)
    if chat:
        answered = _answer_chat(line, chat, messages, handled, moment)
        if answered is not None:
            report.commands.append(answered)
    cutoff = moment - DUPLICATE_WINDOW_SECONDS
    line.save_state(
        inbound_watermark=newest,
        inbound_handled={k: v for k, v in handled.items() if float(v) >= cutoff},
    )
    return report
