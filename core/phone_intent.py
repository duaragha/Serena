"""What one text on her own line actually is: work to queue, or a question.

The grammar in ``core.phone_line`` is deterministic and free, so it still owns
the explicit commands. Everything else used to be guessed by regex, and a regex
cannot tell "enable workouts in Locket" (work) from "what's the cue right now?"
(a question whose voice-to-text lost the word "queue"). Guessing filed the
question as a task.

So anything the grammar does not claim goes to the resident brain daemon -- the
same Serena the app's front door and her voice calls talk to, with her persona,
memory digest and the live queue in front of her. She answers in her own words,
and marks work by opening her reply with ``queue:``. A line prefix survives a
conversational model far better than a JSON schema does.

When her brain cannot be reached the caller degrades to ``triage``: obviously
actionable text is queued as before, and anything else gets one short question
instead of being silently filed in the wrong place.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

QUEUE_PREFIX = "queue:"
TURN_TIMEOUT_SECONDS = 90
MAX_REPLY_CHARACTERS = 900


def brain_file() -> Path:
    configured = os.environ.get("SERENA_BRAIN_FILE", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".config" / "serena" / "brain.json")


def _endpoint() -> tuple[str, str] | None:
    try:
        info = json.loads(brain_file().read_text(encoding="utf-8"))
        return f"http://127.0.0.1:{int(info['port'])}/turn", str(info.get("token") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _post(url: str, payload: dict[str, Any], token: str) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    with urllib.request.urlopen(request, timeout=TURN_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def journal_grounding() -> str:
    """The journal questions she asked him that are still open, if any.

    Without this his "it was saad and sohaib" reads as a stray remark -- or
    worse, as work -- because the question it answers was sent hours ago.
    """

    try:
        from core.journal import store

        days = store.open_days(limit=2)
    except Exception:
        return ""
    lines = []
    for day in days:
        for q in store.unanswered(day):
            lines.append(f"{day['day']} | question_id={q['id']} | {q['text']}")
    return "\n".join(lines)


def envelope(text: str, *, queue: str = "", journal: str = "") -> str:
    """The instruction that turns one text into a decision plus a reply."""

    return (
        "This turn is a text Raghav just sent to your own line -- his private "
        "chat with your bot, which exists only for him to reach you. It is one "
        "of two things, and you decide which:\n\n"
        "1. Work he wants done. Then your ENTIRE reply is one line starting "
        f"with \"{QUEUE_PREFIX}\" followed by the brief, rewritten so a coding "
        "agent who cannot ask him anything could start: name the project and "
        "what should change. Do not add commentary around it.\n"
        "2. Anything else -- a question, a status check, a correction, talk. "
        "Then just answer him, as yourself, in one or two sentences. No "
        f"\"{QUEUE_PREFIX}\" prefix, no markdown, no lists.\n\n"
        "A question about the queue is never work. Neither is a message that "
        "only reacts to something you said.\n\n"
        "When he asks about a job, the facts below are the answer -- they come "
        "off the task itself, so they are current. Report them. Never tell him "
        "you cannot find a record, cannot verify, or that two sources disagree; "
        "if a job failed, say what failed in plain words. Never end by offering "
        "him a menu or asking him to choose -- take the position yourself and "
        "say what he should send, or what you have actually started. Never claim "
        "to be working on something the facts below do not show you working on; "
        "an invented \"i'm on it\" is worse than telling him it is stuck. "
        "The whole grammar he "
        "can text back is: \"task: <brief>\" to queue work, \"status\", "
        "\"retry #<id>\" to rerun a blocked job from where it stopped, and "
        "\"#<id> <answer>\" to answer a question you asked about that job.\n\n"
        + (f"His queue right now:\n{queue}\n\n" if queue else "")
        + (
            "You asked him these journal questions and he has not answered yet:\n"
            f"{journal}\n"
            "If his text answers any of them, it is neither work nor small talk: "
            "call your journal_answer tool once per question it answers, "
            "with his words as `answer`, the matching `question_id` and `day`, a "
            "short `place_name` for a where-question, and first names in `people`. "
            "Then reply in a few words. Only say it is saved if the tool said so.\n\n"
            if journal else "")
        + f"His text:\n{text}"
    )


def read(text: str, *, queue: str = "") -> tuple[str, str] | None:
    """``("task", brief)``, ``("say", reply)``, or None when her brain is down."""

    body = (text or "").strip()
    if not body:
        return None
    endpoint = _endpoint()
    if endpoint is None:
        return None
    url, token = endpoint
    payload: dict[str, Any] = {
        "text": envelope(body, queue=queue, journal=journal_grounding()),
        "memory_query": body,
        # Not "plain": the daemon routes a phone turn to its own model and
        # role, so a text can be reasoned through instead of answered at
        # spoken-turn speed. A daemon that does not know the protocol falls
        # back to the plain behaviour, so this is safe to send either way.
        "protocol": "phone",
    }
    try:
        answer = _post(url, payload, token)
    except Exception:
        return None
    if not isinstance(answer, dict) or not answer.get("ok"):
        return None
    said = " ".join(str(answer.get("say") or "").split())
    if not said:
        return None
    if said.lower().startswith(QUEUE_PREFIX):
        brief = said[len(QUEUE_PREFIX):].strip()
        # She answered with the prefix and nothing after it; the text he sent
        # is a better brief than an empty one.
        return "task", brief or body
    return "say", said[:MAX_REPLY_CHARACTERS]


def triage(text: str) -> tuple[str, str]:
    """Degraded read, used only when her brain could not be reached.

    Obviously actionable text is still queued. Everything else asks one
    question, because filing a question as work is the worse mistake.
    """

    from memory.store import classify_task

    body = (text or "").strip()
    try:
        actionable = classify_task(body) == "ready"
    except ValueError:
        actionable = False
    if actionable:
        return "task", body
    return "say", ("i can't reach my own head right now. if that's work, send it "
                   "as \"task: <what should change, and where>\".")
