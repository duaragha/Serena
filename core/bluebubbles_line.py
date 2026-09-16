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
"""

from __future__ import annotations

import json
import mimetypes
import os
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


def _password(data: dict[str, Any]) -> str:
    try:
        return Path(str(data.get("password_file") or "")).expanduser().read_text(
            encoding="utf-8").strip()
    except OSError as error:
        raise BlueBubblesLineError("Serena's BlueBubbles password file is missing") from error


def chat_guid(data: dict[str, Any] | None = None) -> str:
    data = data or settings()
    return f"iMessage;-;{data['address']}"


def _request(method: str, endpoint: str, *, body: bytes | None = None,
             content_type: str = "application/json", query: dict[str, str] | None = None,
             timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    data = settings()
    if not data.get("url"):
        raise BlueBubblesLineError("Serena's phone line is not configured")
    params = {"password": _password(data), **(query or {})}
    url = f"{str(data['url']).rstrip('/')}/api/v1/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": content_type, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = str(json.loads(error.read()).get("message") or "")[:200]
        except Exception:
            pass
        raise BlueBubblesLineError(f"BlueBubbles returned {error.code} {detail}".strip()) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BlueBubblesLineError(f"Serena's server is unreachable: {type(error).__name__}") from error
    try:
        return json.loads(raw) if raw else {}
    except ValueError as error:
        raise BlueBubblesLineError("Serena's server answered with something other than JSON") from error


def ping() -> bool:
    try:
        return _request("GET", "ping", timeout=10).get("message") == "pong"
    except BlueBubblesLineError:
        return False


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


def recent_messages(limit: int = 50) -> list[dict[str, Any]]:
    """Newest messages in Serena's chat with Raghav, oldest first, normalized."""

    try:
        response = _request("GET", f"chat/{urllib.parse.quote(chat_guid(), safe='')}/message",
                            query={"limit": str(max(1, min(int(limit), 200))), "sort": "DESC"})
    except BlueBubblesLineError as error:
        if " 404" in f" {error}" or "not found" in str(error).lower():
            return []
        raise
    rows = []
    for message in response.get("data") or []:
        created = int(message.get("dateCreated") or 0)
        rows.append({
            "id": str(message.get("guid") or ""),
            "text": str(message.get("text") or ""),
            "created": created,
            "own": bool(message.get("isFromMe")),
            "deleted": bool(message.get("dateRetracted")),
            "kind": "text" if message.get("text") else "other",
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
