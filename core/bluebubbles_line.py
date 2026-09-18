"""Serena's own iMessage identity: a BlueBubbles server signed into her Apple ID.

The hub self-thread worked, but it was Raghav texting himself, so every message
showed twice on his phone. With her own Apple ID on a second macOS user in the
BlueBubbles VM, Serena is simply a contact: she writes to his Apple ID and his
replies arrive as ordinary incoming messages, so there is no ambiguity about
who said what.

Configuration lives in ~/.config/serena/phone-line.json:

    {"backend": "bluebubbles",
     "url": "http://127.0.0.1:1236",
     "password_file": "~/.config/serena/serena-bluebubbles-password.txt",
     "address": "raghavdua1999@gmail.com"}

The address is his Apple ID email rather than his number, so the line keeps
working whether or not his number is currently registered with iMessage.

Until her own Apple ID is activated, the same client also drives *his* server
and his self-thread, with `"self_thread": true` and his own number or email as
the address. In that mode every message in the chat is `isFromMe`, so hers are
the ones carrying her `serena:` prefix and his are everything else. That is
how the phone line reads his briefs without the Unified bridge, which does not
sync his own self-thread messages inward.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REQUEST_TIMEOUT_SECONDS = 30
SEND_TIMEOUT_SECONDS = 90
MAX_TEXT_CHARS = 4000
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# Her typing indicator expires after a few seconds; refresh inside that.
TYPING_REFRESH_SECONDS = 2.5
REACTIONS = ("love", "like", "dislike", "laugh", "emphasize", "question")


class BlueBubblesLineError(RuntimeError):
    """Serena's server refused, or is not reachable."""


def config_path() -> Path:
    configured = os.environ.get("SERENA_PHONE_LINE_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".config" / "serena" / "phone-line.json")


def settings() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled() -> bool:
    data = settings()
    return data.get("backend") == "bluebubbles" and bool(data.get("url")) and bool(
        data.get("address"))


def self_thread(data: dict[str, Any] | None = None) -> bool:
    """True when this line is his own thread on his own server."""

    data = data if data is not None else settings()
    return bool(data.get("self_thread"))


def _password(data: dict[str, Any]) -> str:
    try:
        return Path(str(data.get("password_file") or "")).expanduser().read_text(
            encoding="utf-8").strip()
    except OSError as error:
        raise BlueBubblesLineError("Serena's BlueBubbles password file is missing") from error


def chat_guid(data: dict[str, Any] | None = None) -> str:
    data = data or settings()
    return f"iMessage;-;{data['address']}"


def same_handle(first: str, second: str) -> bool:
    """True when two sender addresses name the same handle.

    Exact match after lowercasing covers emails. Numbers additionally match
    on their last 10 digits, so +E.164, national, and formatted spellings of
    one number are the same person. An email never digit-matches a number,
    and short codes match only exactly.
    """

    left = str(first or "").strip().lower()
    right = str(second or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if "@" in left or "@" in right:
        return False
    left_digits = "".join(char for char in left if char.isdigit())
    right_digits = "".join(char for char in right if char.isdigit())
    return (len(left_digits) >= 10 and len(right_digits) >= 10
            and left_digits[-10:] == right_digits[-10:])


def _endpoint_url(endpoint: str, query: dict[str, str] | None = None) -> str:
    data = settings()
    if not data.get("url"):
        raise BlueBubblesLineError("Serena's phone line is not configured")
    params = {"password": _password(data), **(query or {})}
    return f"{str(data['url']).rstrip('/')}/api/v1/{endpoint}?{urllib.parse.urlencode(params)}"


def _read_url(method: str, url: str, *, body: bytes | None,
              content_type: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": content_type, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = str(json.loads(error.read()).get("message") or "")[:200]
        except Exception:
            pass
        raise BlueBubblesLineError(f"BlueBubbles returned {error.code} {detail}".strip()) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BlueBubblesLineError(f"Serena's server is unreachable: {type(error).__name__}") from error


def _request(method: str, endpoint: str, *, body: bytes | None = None,
             content_type: str = "application/json", query: dict[str, str] | None = None,
             timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    raw = _read_url(method, _endpoint_url(endpoint, query), body=body,
                    content_type=content_type, timeout=timeout)
    try:
        return json.loads(raw) if raw else {}
    except ValueError as error:
        raise BlueBubblesLineError("Serena's server answered with something other than JSON") from error


def _request_bytes(method: str, endpoint: str, *,
                   query: dict[str, str] | None = None,
                   timeout: float = SEND_TIMEOUT_SECONDS) -> bytes:
    """GET one endpoint as raw bytes, for inbound attachments."""

    return _read_url(method, _endpoint_url(endpoint, query), body=None,
                     content_type="application/json", timeout=timeout)


def ping() -> bool:
    """True when the server answers its health probe.

    BlueBubbles replies `{"message": "Ping received!", "data": "pong"}`, so the
    pong lives in `data`; reading only `message` reported a healthy server as
    down and had the health check crying wolf.
    """

    try:
        response = _request("GET", "ping", timeout=10)
    except BlueBubblesLineError:
        return False
    return "pong" in {str(response.get("data") or "").lower(),
                      str(response.get("message") or "").lower()}


def server_info() -> dict[str, Any]:
    return dict(_request("GET", "server/info").get("data") or {})


def _chat_exists() -> bool:
    try:
        _request("GET", f"chat/{urllib.parse.quote(chat_guid(), safe='')}")
        return True
    except BlueBubblesLineError as error:
        if " 404" in f" {error}" or "not found" in str(error).lower():
            return False
        raise


def send_text(text: str, *, temp_guid: str = "") -> str:
    """Send one iMessage to Raghav; returns the sent message GUID."""

    body_text = " ".join(str(text).split("\0"))[:MAX_TEXT_CHARS].strip()
    if not body_text:
        raise BlueBubblesLineError("empty message")
    data = settings()
    temp = temp_guid or f"temp-{uuid.uuid4()}"
    if _chat_exists():
        payload = {"chatGuid": chat_guid(data), "tempGuid": temp, "message": body_text,
                   "method": "private-api"}
        endpoint = "message/text"
    else:
        # The first message creates the conversation.
        payload = {"addresses": [data["address"]], "message": body_text,
                   "method": "private-api", "service": "iMessage", "tempGuid": temp}
        endpoint = "chat/new"
    response = _request("POST", endpoint, body=json.dumps(payload).encode("utf-8"),
                        timeout=SEND_TIMEOUT_SECONDS)
    result = response.get("data") or {}
    guid = str(result.get("guid") or "")
    if not guid and endpoint == "chat/new":
        guid = str(((result.get("messages") or [{}])[0] or {}).get("guid") or temp)
    if int(response.get("status") or 200) >= 400:
        raise BlueBubblesLineError(str(response.get("message") or "send failed"))
    return guid or temp


def send_attachment(path: Path, *, name: str = "") -> str:
    """Send one file to Raghav as an iMessage attachment."""

    if not _chat_exists():
        send_text(f"serena: {name or path.name}")
    boundary = f"----serena-{uuid.uuid4().hex}"
    file_name = name or path.name
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    fields = {"chatGuid": chat_guid(), "tempGuid": f"temp-{uuid.uuid4()}",
              "name": file_name, "method": "private-api"}
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                     f"{value}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; "
        f"filename=\"{file_name}\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8")
        + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    response = _request("POST", "message/attachment", body=b"".join(parts),
                        content_type=f"multipart/form-data; boundary={boundary}",
                        timeout=SEND_TIMEOUT_SECONDS)
    return str((response.get("data") or {}).get("guid") or "")


def typing(chat_guid: str, on: bool) -> bool:
    """Show (or hide) her typing indicator. False without the Private API."""

    try:
        _request("POST" if on else "DELETE",
                 f"chat/{urllib.parse.quote(chat_guid, safe='')}/typing")
    except BlueBubblesLineError:
        return False
    return True


@contextlib.contextmanager
def typing_keepalive(chat_guid: str,
                     interval: float = TYPING_REFRESH_SECONDS):
    """Show typing now, refresh while the block runs, hide at its end.

    Every refresh is best-effort: without the Private API this is a silent
    no-op and the conversation still works, just plainer.
    """

    stop = threading.Event()
    typing(chat_guid, True)

    def _refresh() -> None:
        while not stop.wait(interval):
            typing(chat_guid, True)

    worker = threading.Thread(target=_refresh, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        typing(chat_guid, False)


def mark_read(chat_guid: str) -> bool:
    """Mark the chat read: the cheapest "I've got it" there is."""

    try:
        _request("POST", f"chat/{urllib.parse.quote(chat_guid, safe='')}/read")
    except BlueBubblesLineError:
        return False
    return True


def react(chat_guid: str, message_guid: str, reaction: str) -> bool:
    """Tap back on one message. False without the Private API."""

    if reaction not in REACTIONS:
        return False
    try:
        _request("POST", "message/react", body=json.dumps({
            "chatGuid": chat_guid, "selectedMessageGuid": message_guid,
            "reaction": reaction,
        }).encode("utf-8"))
    except BlueBubblesLineError:
        return False
    return True


def download_attachment(attachment_guid: str) -> bytes:
    """Fetch one inbound attachment's bytes: a photo, a voice note."""

    guid = str(attachment_guid or "").strip()
    if not guid:
        raise BlueBubblesLineError("the attachment has no guid")
    raw = _request_bytes(
        "GET", f"attachment/{urllib.parse.quote(guid, safe='')}/download")
    if not raw:
        raise BlueBubblesLineError("the attachment is empty")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise BlueBubblesLineError("the attachment is too large")
    return raw


def recent_messages(limit: int = 50, *, own_prefix: str = "") -> list[dict[str, Any]]:
    """Newest messages in the chat with Raghav, oldest first, normalized.

    `own_prefix` marks hers by text instead of by `isFromMe`, which is what a
    self-thread needs: there, every message is from his account.
    """

    try:
        response = _request("GET", f"chat/{urllib.parse.quote(chat_guid(), safe='')}/message",
                            query={"limit": str(max(1, min(int(limit), 200))), "sort": "DESC"})
    except BlueBubblesLineError as error:
        if " 404" in f" {error}" or "not found" in str(error).lower():
            return []
        raise
    rows = []
    prefix = own_prefix.strip().lower()
    for message in response.get("data") or []:
        created = int(message.get("dateCreated") or 0)
        text = str(message.get("text") or "")
        own = (text.strip().lower().startswith(prefix) if prefix
               else bool(message.get("isFromMe")))
        handle = message.get("handle")
        handle = handle if isinstance(handle, dict) else {}
        attachments = []
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            guid = str(attachment.get("guid") or "")
            if not guid:
                continue
            attachments.append({
                "guid": guid,
                "mime": str(attachment.get("mimeType")
                            or attachment.get("mime") or ""),
            })
        if message.get("text"):
            kind = "text"
        elif any(a["mime"].startswith("image/") for a in attachments):
            kind = "image"
        elif any(a["mime"].startswith("audio/") for a in attachments):
            kind = "audio"
        else:
            kind = "other"
        rows.append({
            "id": str(message.get("guid") or ""),
            "text": text,
            "created": created,
            "own": own,
            "deleted": bool(message.get("dateRetracted")),
            "kind": kind,
            "handle": str(handle.get("address") or ""),
            "service": str(handle.get("service") or ""),
            "attachments": attachments,
            "associated_guid": str(message.get("associatedMessageGuid") or ""),
        })
    rows.sort(key=lambda row: row["created"])
    return rows


def imessage_available(address: str) -> bool | None:
    """Whether Apple currently has `address` registered for iMessage.

    None means the check itself failed, which is not the same as "no".
    """

    try:
        response = _request("GET", "handle/availability/imessage", query={"address": address})
    except BlueBubblesLineError:
        return None
    data = response.get("data") or {}
    available = data.get("available")
    return bool(available) if isinstance(available, bool) else None


def now_ms() -> int:
    return int(time.time() * 1000)
