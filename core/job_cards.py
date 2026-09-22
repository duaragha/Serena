"""One live Telegram message per dispatched job, edited as it moves.

A job used to text him on every phase and again when it finished: five
buzzes for one piece of work, scattered between everything else she sends.
Now each job owns a single card in the Jobs topic. Progress rewrites the card
in place, which Telegram never announces, so the phone only buzzes when the
card is first posted and when the job reaches an end (merged, PR ready,
failed, waiting on him) -- the end re-posts the card at the bottom of the
thread so it is where he looks.

Only the Telegram line can edit a message. On any other line ``show`` returns
False and the caller keeps its plain one-line texts.
"""

from __future__ import annotations

import hashlib
import html
import sqlite3
from contextlib import closing
from typing import Any

PHASES = ("discover", "execute", "verify", "finalize")
PHASE_NAMES = {"discover": "research", "execute": "code", "verify": "review",
               "finalize": "fixes"}
# Where the job is, as the card shows it. The terminal ones are announced.
STATUS = {
    "running": ("🟡", ""),
    "retrying": ("🔁", "rerunning"),
    "merged": ("🟢", "merged"),
    "pr": ("🟢", "PR ready"),
    "no_changes": ("⚪️", "nothing to change"),
    "failed": ("🔴", "failed"),
    "waiting": ("🟠", "waiting on you"),
    "undeliverable": ("🔴", "built, can't deliver"),
}
TERMINAL = frozenset({"merged", "pr", "no_changes", "failed", "waiting", "undeliverable"})
# The ones his "retry" button can move again.
RETRYABLE = frozenset({"failed", "undeliverable"})


def _db():
    from memory import store

    path = store.MEMORY_DIR / ".fleet-dispatch.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("CREATE TABLE IF NOT EXISTS job_cards (task_id INTEGER PRIMARY KEY, "
               "message_id INTEGER, fingerprint TEXT, status TEXT)")
    return db


def enabled() -> bool:
    try:
        from core import phone_line, telegram_line

        return phone_line.backend_name() == "telegram" and telegram_line.enabled()
    except Exception:
        return False


def _split(label: str) -> tuple[str, str]:
    """'In Unified (liquid glass ui)' -> ('Unified', 'liquid glass ui')."""

    text = " ".join(str(label or "").split())
    if text.startswith("In ") and "(" in text and text.endswith(")"):
        project, _, rest = text[3:].partition(" (")
        return project.strip(), rest[:-1].strip()
    return "", text.strip("()")


def _phase_states(run: dict[str, Any] | None) -> dict[str, str]:
    states = {}
    for phase in (run or {}).get("phases") or []:
        if isinstance(phase, dict) and phase.get("name") in PHASE_NAMES:
            states[str(phase["name"])] = str(phase.get("state") or "")
    return states


def _step(name: str, state: str) -> str:
    if state == "completed":
        return f"✅ {PHASE_NAMES[name]}"
    if state == "failed":
        return f"❌ <b>{PHASE_NAMES[name]}</b>"
    if state in {"running", "waiting_for_input"}:
        return f"⏳ <b>{PHASE_NAMES[name]}</b>"
    return f"▫️ {PHASE_NAMES[name]}"


def render(task_id: int, label: str, run: dict[str, Any] | None, status: str, *,
           url: str = "", reason: str = "", note: str = "") -> tuple[str, list]:
    """The card's HTML and its buttons."""

    emoji, word = STATUS.get(status, STATUS["running"])
    project, what = _split(label)
    head = f"{emoji} <b>#{task_id}{' · ' + html.escape(project) if project else ''}</b>"
    if word:
        head += f"  —  {word}"
    states = _phase_states(run)
    if status in {"merged", "pr"}:
        # Delivery happens after the last phase; show the job as whole.
        states = {name: "completed" for name in PHASES}
    steps = "   ".join(_step(name, states.get(name, "")) for name in PHASES)
    lines = [head, html.escape(what), "", steps]
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    if reason:
        lines.append(f"<blockquote expandable>{html.escape(reason[:900])}</blockquote>")
    buttons: list = []
    row = []
    if url:
        row.append({"text": "open PR" if "/pull/" in url else "open",
                    "url": url, "style": "primary"})
    if status in RETRYABLE:
        row.append({"text": "🔁 retry", "callback_data": f"retry:{task_id}",
                    "style": "success"})
    if row:
        buttons.append(row)
    return "\n".join(lines), buttons


def show(task: dict[str, Any], run: dict[str, Any] | None, status: str, label: str, *,
         url: str = "", reason: str = "", note: str = "") -> bool:
    """Post or update the job's card. False when the line cannot carry cards."""

    if not enabled():
        return False
    from core import telegram_line

    task_id = int(task["id"])
    text, buttons = render(task_id, label, run, status, url=url, reason=reason, note=note)
    fingerprint = hashlib.sha256(
        (text + repr(buttons)).encode("utf-8")).hexdigest()[:20]
    with closing(_db()) as db:
        row = db.execute("SELECT message_id, fingerprint, status FROM job_cards WHERE task_id = ?",
                         (task_id,)).fetchone()
    if row and row[1] == fingerprint:
        return True
    try:
        if row and row[0] and status not in TERMINAL:
            if telegram_line.edit_text(int(row[0]), text, buttons=buttons):
                message_id = int(row[0])
            else:
                message_id = telegram_line.send_text(text, topic="jobs", html=True,
                                                     buttons=buttons, silent=True)
        else:
            # A new card, or the job just ended: post it (the buzz he wants)
            # and drop the old one so a job never has two cards.
            message_id = telegram_line.send_text(text, topic="jobs", html=True,
                                                 buttons=buttons)
            if message_id and row and row[0]:
                telegram_line.delete_message(int(row[0]))
    except telegram_line.TelegramLineError:
        return False
    if not message_id:
        return False
    with closing(_db()) as db, db:
        db.execute("INSERT OR REPLACE INTO job_cards(task_id, message_id, fingerprint, status) "
                   "VALUES (?, ?, ?, ?)", (task_id, message_id, fingerprint, status))
    return True
