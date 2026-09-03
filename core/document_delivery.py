"""Capability broker for documents Serena creates and sends on voice turns.

Files are confined to one private, user-visible directory under Documents.
Telegram uses the existing bot credentials. Beeper is opt-in and talks only
to the official Desktop API on localhost with a chat id pinned in config.

The model's tool arguments are never treated as authority. Every write and
send independently re-reads the originating turn that the daemon bound to the
tool call. Audits contain a speech digest and filename only, never document
content, chat ids, tokens, request bodies, or remote error bodies.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import stat
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

DOCUMENT_ROOT = Path(
    os.environ.get(
        "SERENA_DOCUMENT_ROOT",
        str(Path.home() / "Documents" / "Serena"),
    )
).expanduser()
TELEGRAM_ENV_PATH = Path.home() / ".config" / "serena" / "telegram.env"
BEEPER_ENV_PATH = Path.home() / ".config" / "serena" / "beeper.env"
AUTHORITY_AUDIT_PATH = (
    Path.home() / ".local" / "state" / "serena" / "document-authority.jsonl"
)
BEEPER_API_BASE = "http://127.0.0.1:23373"

SPOKEN_PROTOCOLS = frozenset({"voice", "desk"})
MAX_DOCUMENT_CHARS = 200_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 1 * 1024 * 1024

_CREATE_SIGNAL = re.compile(
    r"\b(?:create|make|write|save|put|type|jot|draft|turn|export|give)\b",
    re.IGNORECASE,
)
_DOCUMENT_SIGNAL = re.compile(
    r"\b(?:document|word|docx|notepad|text\s+file|txt|file|notes?|list)\b",
    re.IGNORECASE,
)
_SEND_SIGNAL = re.compile(
    r"\b(?:send|share|message|attach|forward|deliver|text)\b",
    re.IGNORECASE,
)
_TELEGRAM_SIGNAL = re.compile(r"\btelegram(?:\s+bot)?\b", re.IGNORECASE)
_BEEPER_SIGNAL = re.compile(r"\bbeeper\b", re.IGNORECASE)
_INVALID_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)
_SAFE_STEM = re.compile(r"[^\w .()\[\]-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class AuthorityResult:
    allowed: bool
    reason: str
    action: str
    filename: str = ""


@dataclass(frozen=True, slots=True)
class DocumentResult:
    ok: bool
    reason: str
    filename: str = ""
    path: str = ""
    format: str = ""


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    reason: str
    filename: str = ""
    channel: str = ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def authority_denial(action: str, origin: Mapping[str, object]) -> str | None:
    """Return why a document action lacks live user authority, if it does."""

    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()
    if not spoken:
        return "no originating spoken turn is bound to this tool call"
    if protocol not in SPOKEN_PROTOCOLS:
        return (
            "document actions happen only on a live spoken turn, "
            f"not a {protocol or 'unknown'} turn"
        )
    if action == "create_document":
        if not _CREATE_SIGNAL.search(spoken) or not _DOCUMENT_SIGNAL.search(spoken):
            return "the spoken turn did not ask for a document to be created"
        return None
    if action == "send_telegram":
        if not _SEND_SIGNAL.search(spoken) or not _TELEGRAM_SIGNAL.search(spoken):
            return "the spoken turn did not ask to send a document on Telegram"
        return None
    if action == "send_beeper":
        if not _SEND_SIGNAL.search(spoken) or not _BEEPER_SIGNAL.search(spoken):
            return "the spoken turn did not ask to send a document on Beeper"
        return None
    return "unknown document action"


def authorize(
    action: str,
    *,
    origin: Mapping[str, object],
    filename: str = "",
    audit_path: Path = AUTHORITY_AUDIT_PATH,
) -> AuthorityResult:
    denial = authority_denial(action, origin)
    result = AuthorityResult(
        denial is None,
        "authorized" if denial is None else denial,
        action,
        _safe_audit_filename(filename),
    )
    _append_authority_audit(result, origin=origin, audit_path=audit_path)
    return result


def _safe_audit_filename(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or Path(raw).name != raw:
        return ""
    return raw[:120]


def _append_authority_audit(
    result: AuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    try:
        target = Path(audit_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            target.parent.chmod(0o700)
        payload = {
            **asdict(result),
            "created_at": time.time(),
            "protocol": str(origin.get("protocol") or ""),
            "call_id": str(origin.get("call_id") or ""),
            "turn_id": str(origin.get("turn_id") or ""),
            "origin_sha256": hashlib.sha256(
                str(origin.get("text") or "").encode("utf-8")
            ).hexdigest(),
        }
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            _write_all(
                descriptor,
                (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def create_document(
    filename: str,
    content: str,
    requested_format: str,
    *,
    origin: Mapping[str, object],
    root: Path = DOCUMENT_ROOT,
    audit_path: Path = AUTHORITY_AUDIT_PATH,
) -> DocumentResult:
    """Create one non-overwriting document under the dedicated document root."""

    decision = authorize(
        "create_document", origin=origin, filename=filename, audit_path=audit_path
    )
    if not decision.allowed:
        return DocumentResult(False, decision.reason)
    body = str(content or "").strip()
    if not body:
        return DocumentResult(False, "the document content was empty")
    if len(body) > MAX_DOCUMENT_CHARS:
        return DocumentResult(False, "the document content was too large")
    try:
        document_root = _secure_document_root(root)
        stem, format_name = _normalise_document_name(filename, requested_format)
        payload = (
            body.encode("utf-8") + b"\n"
            if format_name == "txt"
            else _render_docx(body)
        )
        target = _write_new_document(document_root, stem, format_name, payload)
    except (OSError, ValueError, zipfile.BadZipFile):
        return DocumentResult(False, "the document could not be written safely")
    return DocumentResult(
        True,
        "created",
        filename=target.name,
        path=str(target),
        format=format_name,
    )


def send_document_to_telegram(
    filename: str,
    *,
    origin: Mapping[str, object],
    root: Path = DOCUMENT_ROOT,
    env_path: Path = TELEGRAM_ENV_PATH,
    audit_path: Path = AUTHORITY_AUDIT_PATH,
    urlopen: Callable[..., object] | None = None,
) -> SendResult:
    decision = authorize(
        "send_telegram", origin=origin, filename=filename, audit_path=audit_path
    )
    if not decision.allowed:
        return SendResult(False, decision.reason, channel="telegram")
    try:
        target = _resolve_attachment(filename, root=root)
    except (OSError, ValueError):
        return SendResult(False, "the attachment is not a Serena document", channel="telegram")
    credentials = _read_env_file(Path(env_path), ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    token = credentials.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = credentials.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return SendResult(False, "Telegram delivery is not configured", target.name, "telegram")
    try:
        content = target.read_bytes()
        boundary = "----serena-" + secrets.token_hex(16)
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = _telegram_multipart(
            boundary=boundary,
            chat_id=chat_id,
            filename=target.name,
            mime_type=mime_type,
            content=content,
        )
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        response = _open_json(request, urlopen=urlopen, timeout=20.0)
    except Exception:
        return SendResult(False, "Telegram could not accept the attachment", target.name, "telegram")
    if response.get("ok") is not True:
        return SendResult(False, "Telegram rejected the attachment", target.name, "telegram")
    return SendResult(True, "sent", target.name, "telegram")


def send_document_to_beeper(
    filename: str,
    *,
    origin: Mapping[str, object],
    root: Path = DOCUMENT_ROOT,
    env_path: Path = BEEPER_ENV_PATH,
    audit_path: Path = AUTHORITY_AUDIT_PATH,
    urlopen: Callable[..., object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SendResult:
    """Upload and send one attachment through Beeper's official local API."""

    decision = authorize(
        "send_beeper", origin=origin, filename=filename, audit_path=audit_path
    )
    if not decision.allowed:
        return SendResult(False, decision.reason, channel="beeper")
    try:
        target = _resolve_attachment(filename, root=root)
    except (OSError, ValueError):
        return SendResult(False, "the attachment is not a Serena document", channel="beeper")
    credentials = _read_env_file(
        Path(env_path), ("BEEPER_ACCESS_TOKEN", "BEEPER_CHAT_ID")
    )
    token = credentials.get("BEEPER_ACCESS_TOKEN", "")
    chat_id = credentials.get("BEEPER_CHAT_ID", "")
    if not token or not chat_id:
        return SendResult(False, "Beeper delivery is not configured", target.name, "beeper")
    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        upload = _post_json(
            BEEPER_API_BASE + "/v1/assets/upload/base64",
            {
                "content": base64.b64encode(target.read_bytes()).decode("ascii"),
                "fileName": target.name,
                "mimeType": mime_type,
            },
            headers=headers,
            urlopen=urlopen,
        )
        upload_id = str(upload.get("uploadID") or "").strip()
        if not upload_id or upload.get("error"):
            return SendResult(False, "Beeper rejected the attachment upload", target.name, "beeper")
        quoted_chat = urllib.parse.quote(chat_id, safe="")
        sent = _post_json(
            f"{BEEPER_API_BASE}/v1/chats/{quoted_chat}/messages",
            {
                "attachment": {
                    "uploadID": upload_id,
                    "fileName": target.name,
                    "mimeType": mime_type,
                    "type": "file",
                }
            },
            headers=headers,
            urlopen=urlopen,
        )
        pending_id = str(sent.get("pendingMessageID") or "").strip()
        if not pending_id:
            return SendResult(False, "Beeper did not accept the attachment", target.name, "beeper")
        quoted_message = urllib.parse.quote(pending_id, safe="")
        retrieve_url = (
            f"{BEEPER_API_BASE}/v1/chats/{quoted_chat}/messages/{quoted_message}"
        )
        for attempt in range(8):
            try:
                message = _get_json(retrieve_url, headers=headers, urlopen=urlopen)
            except Exception:
                if attempt < 7:
                    sleep(0.25)
                    continue
                raise
            if isinstance(message.get("message"), dict):
                message = message["message"]
            send_status = message.get("sendStatus")
            status_name = (
                str(send_status.get("status") or "").upper()
                if isinstance(send_status, dict)
                else ""
            )
            if status_name == "SUCCESS":
                return SendResult(True, "sent", target.name, "beeper")
            if status_name in {"FAIL_RETRIABLE", "FAIL_PERMANENT"}:
                return SendResult(False, "Beeper reported that the attachment failed", target.name, "beeper")
            if attempt < 7:
                sleep(0.25)
    except Exception:
        return SendResult(False, "the local Beeper API could not send the attachment", target.name, "beeper")
    return SendResult(False, "Beeper did not confirm the attachment send", target.name, "beeper")


def _secure_document_root(root: Path) -> Path:
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("document root is not a real directory")
    root.chmod(0o700)
    return root.resolve()


def _normalise_document_name(filename: str, requested_format: str) -> tuple[str, str]:
    raw = str(filename or "").strip()
    if raw and Path(raw).name != raw:
        raise ValueError("filename must not contain a path")
    suffix = Path(raw).suffix.lower()
    format_name = str(requested_format or "auto").strip().lower()
    if format_name == "auto":
        format_name = "txt" if suffix == ".txt" else "docx"
    if format_name not in {"docx", "txt"}:
        raise ValueError("unsupported document format")
    stem = Path(raw).stem if raw else "Serena Notes"
    stem = _SAFE_STEM.sub("_", stem).strip(" ._")[:100]
    if not stem:
        stem = "Serena Notes"
    return stem, format_name


def _write_new_document(root: Path, stem: str, format_name: str, payload: bytes) -> Path:
    extension = "." + format_name
    for number in range(1, 1001):
        suffix = "" if number == 1 else f" ({number})"
        target = root / f"{stem}{suffix}{extension}"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            target.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        return target
    raise OSError("too many documents with the same name")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _render_docx(content: str) -> bytes:
    paragraphs: list[str] = []
    for raw_line in content.splitlines() or [content]:
        line = _INVALID_XML.sub("", raw_line)
        if line:
            paragraphs.append(
                '<w:p><w:r><w:t xml:space="preserve">'
                + xml_escape(line)
                + "</w:t></w:r></w:p>"
            )
        else:
            paragraphs.append("<w:p/>")
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


def _resolve_attachment(filename: str, *, root: Path) -> Path:
    raw = str(filename or "").strip()
    if not raw or Path(raw).name != raw:
        raise ValueError("attachment must be a plain filename")
    document_root = _secure_document_root(root)
    candidate = document_root / raw
    if candidate.is_symlink():
        raise ValueError("symlink attachments are not allowed")
    target = candidate.resolve(strict=True)
    if target.parent != document_root:
        raise ValueError("attachment escaped document root")
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("attachment is not a regular file")
    if info.st_size <= 0 or info.st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment size is not allowed")
    return target


def _read_env_file(path: Path, wanted: tuple[str, ...]) -> dict[str, str]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _telegram_multipart(
    *,
    boundary: str,
    chat_id: str,
    filename: str,
    mime_type: str,
    content: bytes,
) -> bytes:
    safe_filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    chunks = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="chat_id"\r\n\r\n',
        chat_id.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("ascii"),
        (
            'Content-Disposition: form-data; name="document"; filename="'
            + safe_filename
            + '"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(chunks)


def _post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    headers: Mapping[str, str],
    urlopen: Callable[..., object] | None,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    return _open_json(request, urlopen=urlopen, timeout=20.0)


def _get_json(
    url: str,
    *,
    headers: Mapping[str, str],
    urlopen: Callable[..., object] | None,
) -> dict:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    return _open_json(request, urlopen=urlopen, timeout=5.0)


def _open_json(
    request: urllib.request.Request,
    *,
    urlopen: Callable[..., object] | None,
    timeout: float,
) -> dict:
    opener = urlopen or urllib.request.urlopen
    with opener(request, timeout=timeout) as response:
        raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise ValueError("oversized response")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("non-object response")
    return payload
