"""Serena's paired-device client for Raghav's Unified hub.

Unified already bridges iMessage, so Serena reaches his phone by holding an
ordinary device session on his own hub instead of a third-party bot. That keeps
the phone channel inside infrastructure he owns, and it gives her a reply path:
the same conversation she writes to is the one she reads commands from.

The hub is a normal paired client surface. Serena claims a short-lived pairing
offer once, then lives on rotating session tokens. Nothing here holds the hub's
administrator token; creating the offer is an operator step done on the hub.

State lives in one private JSON file outside every synced folder. A refresh
rotates the refresh token, so the new token set is written atomically before it
is used, or a crash mid-rotation would strand the device.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "v1"
DEFAULT_STATE_PATH = Path.home() / ".config" / "serena" / "unified-hub.json"
REQUEST_TIMEOUT_SECONDS = 20
# Refresh a little early so a slow request never carries an expired token.
ACCESS_REFRESH_MARGIN_SECONDS = 120
MAX_TEXT_CHARS = 4000
_LOCK = threading.RLock()


class UnifiedHubError(RuntimeError):
    """The hub refused, or Serena has no usable session."""


def state_path() -> Path:
    configured = os.environ.get("SERENA_UNIFIED_HUB_STATE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_STATE_PATH


def _load(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise UnifiedHubError(f"unreadable hub state: {type(error).__name__}") from error
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _expires(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _tls_context():
    """Prefer certifi's roots when present.

    The Windows dispatcher's system store carries an expired Let's Encrypt
    root that Python selects over the valid chain, so the stock context
    rejects the hub's perfectly valid tailnet certificate.
    """

    import ssl

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _post(url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS, context=_tls_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        with suppress(Exception):
            detail = json.loads(error.read()).get("error", {}).get("code", "")
        raise UnifiedHubError(f"hub returned {error.code} {detail}".strip()) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UnifiedHubError(f"hub unreachable: {type(error).__name__}") from error
    return json.loads(raw) if raw else {}


def _public_key() -> tuple[str, str]:
    """A real Ed25519 device key, raw and unpadded base64url as the hub expects."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    private = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return encode(public), encode(private)


def claim_pairing(offer: dict[str, Any], *, device_name: str, platform: str,
                  app_version: str, path: Path | None = None) -> dict[str, Any]:
    """Turn a one-time pairing offer into a durable device session."""

    hub_url = str(offer.get("hubUrl") or "").rstrip("/")
    if not hub_url.startswith("https://"):
        raise UnifiedHubError("pairing offer must name an https hub")
    public, private = _public_key()
    response = _post(f"{hub_url}/api/v1/pairing/claim", {
        "protocolVersion": PROTOCOL_VERSION,
        "pairingId": offer["pairingId"],
        "pairingSecret": offer["pairingSecret"],
        "device": {
            "name": device_name, "platform": platform,
            "appVersion": app_version, "publicKey": public,
        },
    })
    with _LOCK:
        state = _load(path)
        state.update({
            "hub_url": hub_url,
            "instance_id": offer.get("instanceId", ""),
            "session_id": response["sessionId"],
            "device_id": response["device"]["id"],
            "device_private_key": private,
            "tokens": response["tokens"],
            "paired_at": time.time(),
        })
        _save(state, path)
    return {"device_id": response["device"]["id"], "session_id": response["sessionId"]}


def _access_token(state: dict[str, Any], path: Path | None) -> str:
    tokens = state.get("tokens") or {}
    if not tokens or not state.get("session_id"):
        raise UnifiedHubError("Serena is not paired with the Unified hub")
    if _expires(tokens.get("accessTokenExpiresAt")) - ACCESS_REFRESH_MARGIN_SECONDS > time.time():
        return str(tokens["accessToken"])
    if _expires(tokens.get("refreshTokenExpiresAt")) <= time.time():
        raise UnifiedHubError("hub refresh token expired; pair Serena again")
    # One idempotency key per logical rotation, persisted first, so a retry
    # after a lost response replays the same rotation instead of burning it.
    key = state.get("pending_refresh_key") or uuid.uuid4().hex
    if state.get("pending_refresh_key") != key:
        state["pending_refresh_key"] = key
        _save(state, path)
    response = _post(f"{state['hub_url']}/api/v1/sessions/refresh", {
        "protocolVersion": PROTOCOL_VERSION,
        "sessionId": state["session_id"],
        "deviceId": state["device_id"],
        "idempotencyKey": key,
        "refreshToken": tokens["refreshToken"],
    })
    state["tokens"] = response["tokens"]
    state.pop("pending_refresh_key", None)
    _save(state, path)
    return str(response["tokens"]["accessToken"])


def call(endpoint: str, body: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    """POST one authenticated v1 request, rotating tokens when needed."""

    with _LOCK:
        state = _load(path)
        token = _access_token(state, path)
        return _post(f"{state['hub_url']}{endpoint}",
                     {"protocolVersion": PROTOCOL_VERSION, **body}, token)


def configure(path: Path | None = None, **fields: Any) -> dict[str, Any]:
    with _LOCK:
        state = _load(path)
        state.update(fields)
        _save(state, path)
        return state


def settings(path: Path | None = None) -> dict[str, Any]:
    """Non-secret view of the pairing, for status output."""

    state = _load(path)
    tokens = state.get("tokens") or {}
    return {
        "paired": bool(tokens),
        "hub_url": state.get("hub_url", ""),
        "device_id": state.get("device_id", ""),
        "conversation_id": state.get("conversation_id", ""),
        "refresh_expires_at": tokens.get("refreshTokenExpiresAt", ""),
    }


@dataclass(frozen=True)
class SentMessage:
    ok: bool
    message_id: str = ""
    error: str = ""


def send_text(text: str, *, conversation_id: str = "", idempotency_key: str = "",
              path: Path | None = None) -> SentMessage:
    """Send one iMessage into Raghav's configured Serena conversation.

    The message id is recorded so the inbound poller never mistakes Serena's
    own words, which the bridge reports as outgoing from his account, for a
    command he typed.
    """

    text = " ".join(str(text).split("\0"))[:MAX_TEXT_CHARS].strip()
    if not text:
        return SentMessage(False, error="empty message")
    try:
        with _LOCK:
            state = _load(path)
            target = conversation_id or str(state.get("conversation_id") or "")
            if not target:
                return SentMessage(False, error="no Serena conversation is configured")
            ack = call("/api/v1/commands", {
                "idempotencyKey": idempotency_key or f"serena-{uuid.uuid4().hex}",
                "command": {
                    "kind": "message.send",
                    "conversationId": target,
                    "content": {"text": text, "attachments": []},
                },
            }, path=path)
            message_id = str((ack.get("result") or {}).get("messageId") or "")
            if ack.get("disposition") not in {"accepted", "completed", "duplicate"}:
                return SentMessage(False, error=f"hub disposition {ack.get('disposition')}")
            state = _load(path)
            sent = [item for item in state.get("sent_message_ids", []) if item][-499:]
            if message_id:
                sent.append(message_id)
            state["sent_message_ids"] = sent
            state.setdefault("sent_texts", [])
            state["sent_texts"] = (state["sent_texts"] + [text[:200]])[-50:]
            _save(state, path)
            return SentMessage(True, message_id=message_id)
    except (UnifiedHubError, KeyError, ValueError) as error:
        return SentMessage(False, error=str(error))


def recent_messages(*, conversation_id: str = "", limit: int = 50,
                    path: Path | None = None) -> list[dict[str, Any]]:
    """Newest page of the configured conversation, oldest first."""

    state = _load(path)
    target = conversation_id or str(state.get("conversation_id") or "")
    if not target:
        raise UnifiedHubError("no Serena conversation is configured")
    page = call("/api/v1/history", {
        "conversationId": target, "limit": max(1, min(int(limit), 100)),
    }, path=path)
    messages = list(page.get("messages") or [])
    messages.sort(key=lambda item: str(item.get("createdAt") or ""))
    return messages


def search(query: str, *, limit: int = 20, path: Path | None = None) -> dict[str, Any]:
    return call("/api/v1/search", {"query": query, "limit": limit}, path=path)
