"""Serena's text line over a Telegram bot: one private chat, both directions.

The bot is a real second identity, so "who wrote this" is `from.is_bot` rather
than a text prefix, and her replies never come back as his own messages. That
is the whole reason this transport exists alongside the iMessage ones.

Credentials live in one 0600 env file (``~/.config/serena/telegram.env``), the
same file the legacy ``chats text`` notifier already reads:

    TELEGRAM_BOT_TOKEN=<from @BotFather>
    TELEGRAM_CHAT_ID=<his private chat with the bot>

``TELEGRAM_CHAT_ID`` is the authentication: inbound updates from any other chat
are dropped before the grammar ever sees them, so a stranger who finds the bot
can type at it all day and never queue work.

Long polling (``getUpdates``) is deliberate: it needs no public URL, no webhook
secret and no inbound port, so it works unchanged from the laptop, the PC, or
behind any NAT.

It is also what makes the line feel immediate. The scheduler cannot tick
faster than ``MIN_INTERVAL_SECONDS``, so a poll that asked Telegram "anything
new?" and returned instantly left his text sitting for up to a minute before
anyone looked. Asking Telegram to *hold the connection open* until something
arrives spends that minute waiting on him instead of sleeping, so a text is
picked up about as fast as it is sent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_ROOT = "https://api.telegram.org"
TIMEOUT_SECONDS = 20
# How long Telegram holds a getUpdates connection open waiting for a message.
# Shorter than the scheduler's 60s floor, so consecutive polls cover most of
# the interval, and the socket must outlive it or urlopen would raise first.
LONG_POLL_SECONDS = 45
SOCKET_MARGIN_SECONDS = 15
MAX_UPDATES = 50


class TelegramLineError(RuntimeError):
    """The bot could not be reached or refused the call."""


def env_path() -> Path:
    configured = os.environ.get("SERENA_TELEGRAM_ENV", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".config" / "serena" / "telegram.env")


def credentials() -> dict[str, str]:
    try:
        raw = env_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def configured() -> bool:
    values = credentials()
    return bool(values.get("TELEGRAM_BOT_TOKEN") and values.get("TELEGRAM_CHAT_ID"))


def line_config_path() -> Path:
    """``phone-line.json``: the one switch that says who owns the text line."""

    configured_path = os.environ.get("SERENA_PHONE_LINE_CONFIG", "").strip()
    return Path(configured_path).expanduser() if configured_path else (
        Path.home() / ".config" / "serena" / "phone-line.json")


def enabled() -> bool:
    """True only when the line is both pointed at Telegram and credentialed."""

    try:
        data = json.loads(line_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("backend") == "telegram" and configured()


def chat_id() -> str:
    return credentials().get("TELEGRAM_CHAT_ID", "")


def _call(method: str, payload: dict[str, Any] | None = None,
          *, timeout: float | None = None) -> Any:
    token = credentials().get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise TelegramLineError("telegram.env has no TELEGRAM_BOT_TOKEN")
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{API_ROOT}/bot{token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
                request, timeout=TIMEOUT_SECONDS if timeout is None else timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # The token is in the URL, so the message carries the code and Telegram's
        # own description ("chat not found", "Unauthorized") but never the URL.
        detail = ""
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = str(body.get("description") or "")[:200]
        except (OSError, ValueError, AttributeError):
            detail = ""
        raise TelegramLineError(
            f"telegram {method} failed with HTTP {error.code}"
            + (f": {detail}" if detail else "")) from error
    except (OSError, ValueError) as error:
        raise TelegramLineError(f"telegram {method} unreachable") from error
    if not isinstance(answer, dict) or not answer.get("ok"):
        detail = ""
        if isinstance(answer, dict):
            detail = str(answer.get("description") or "")[:200]
        raise TelegramLineError(f"telegram {method} rejected: {detail}" if detail
                                else f"telegram {method} rejected")
    return answer.get("result")


def ping() -> bool:
    """True when the token is live."""

    try:
        result = _call("getMe")
    except TelegramLineError:
        return False
    return isinstance(result, dict) and bool(result.get("id"))


def identity() -> dict[str, Any]:
    result = _call("getMe")
    return result if isinstance(result, dict) else {}


def updates(*, offset: int = 0, wait_seconds: float = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
    """Confirmed-and-drained updates, oldest first.

    ``offset`` is the last handled ``update_id``; Telegram drops everything up
    to it server-side, which is a second line of defence under the caller's own
    watermark.

    ``wait_seconds`` is how long Telegram may hold the connection open before
    answering with nothing. Zero makes this a plain "anything new?" and returns
    at once; the default waits, which is what keeps his texts from sitting in a
    queue nobody is looking at.
    """

    wait = max(0, int(wait_seconds))
    payload: dict[str, Any] = {
        "limit": MAX_UPDATES,
        "timeout": wait,
        "allowed_updates": ["message"],
    }
    if offset:
        payload["offset"] = int(offset) + 1
    # The socket has to outlive the server-side wait, or urlopen raises just
    # before Telegram would have answered -- and every poll looks like a
    # network fault.
    result = _call("getUpdates", payload,
                   timeout=max(TIMEOUT_SECONDS, wait + SOCKET_MARGIN_SECONDS))
    return [row for row in (result or []) if isinstance(row, dict)]


def recent_messages(*, offset: int = 0,
                    wait_seconds: float = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
    """Normalised rows for core.phone_line: only his private chat survives."""

    mine = str(chat_id())
    rows: list[dict[str, Any]] = []
    for update in updates(offset=offset, wait_seconds=wait_seconds):
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("id") or "") != mine:
            continue
        author = message.get("from") if isinstance(message.get("from"), dict) else {}
        text = message.get("text")
        rows.append({
            "id": str(message.get("message_id") or ""),
            "text": str(text or ""),
            "created": int(update.get("update_id") or 0),
            "own": bool(author.get("is_bot")),
            "deleted": False,
            "kind": "text" if isinstance(text, str) and text.strip() else "other",
        })
    return rows


def send_text(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    target = chat_id()
    if not target:
        raise TelegramLineError("telegram.env has no TELEGRAM_CHAT_ID")
    result = _call("sendMessage", {
        "chat_id": target,
        "text": body[:4096],
        "disable_web_page_preview": True,
    })
    return isinstance(result, dict) and bool(result.get("message_id"))
