"""Ring Raghav's phone: the PC side of Serena's SIP line.

The line itself (voice/phone/bridge.py) runs in a container on the PC's docker
VM and exposes a small authenticated API, forwarded to this machine's
loopback. This module asks it to place a call and say one line; after that
the call is an ordinary conversation with her.

Calls are an interruption on top of the text that always goes out, so they
are rate limited here: one call per `min_interval_seconds`, whatever the
reason. Anything that lands inside the window is still in the thread.

Configuration, ~/.config/serena/phone-call.json (all optional):

    {"url": "http://127.0.0.1:8796",
     "token_file": "~/.config/serena/phone-call-token",
     "min_interval_seconds": 600,
     "enabled": true}

The line is enabled when the token file exists and `enabled` is not false.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8796"
DEFAULT_MIN_INTERVAL_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 10


class PhoneCallError(RuntimeError):
    """The line refused the call or could not be reached."""


def _config_dir() -> Path:
    return Path.home() / ".config" / "serena"


def config_path() -> Path:
    configured = os.environ.get("SERENA_PHONE_CALL_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else _config_dir() / "phone-call.json"


def state_path() -> Path:
    return Path.home() / ".local" / "state" / "serena" / "phone-call-state.json"


def settings() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "url": str(data.get("url") or DEFAULT_URL).rstrip("/"),
        "token_file": str(data.get("token_file")
                          or _config_dir() / "phone-call-token"),
        "min_interval_seconds": float(
            data.get("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)),
        "enabled": data.get("enabled", True) is not False,
    }


def _token(config: dict[str, Any]) -> str:
    try:
        return Path(config["token_file"]).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def enabled() -> bool:
    config = settings()
    return bool(config["enabled"] and _token(config))


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict]:
    config = settings()
    token = _token(config)
    if not token:
        raise PhoneCallError("the phone line has no token on this machine")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{config['url']}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read() or b"{}")
        except ValueError:
            payload = {}
        return error.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PhoneCallError(f"the phone line is unreachable: {type(error).__name__}") from error


def health() -> dict[str, Any]:
    status, payload = _request("GET", "/health")
    if status != 200:
        raise PhoneCallError(f"health check returned {status}")
    return payload


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def place(text: str, *, key: str = "", force: bool = False,
          now: float | None = None) -> bool:
    """Ring him and say `text` when he picks up.

    Returns True only when the line accepted the request. Returns False when
    the rate limit held it back, or the same `key` already rang.
    """

    moment = time.time() if now is None else now
    line = " ".join(str(text).split())
    if not line:
        raise PhoneCallError("a call needs something to say")
    config = settings()
    state = _load_state()
    if key and key in (state.get("keys") or {}):
        return False
    last = float(state.get("last_call_at") or 0)
    if not force and moment - last < config["min_interval_seconds"]:
        return False
    status, payload = _request("POST", "/call", {"text": line})
    if status != 202:
        raise PhoneCallError(str(payload.get("error") or f"line returned {status}"))
    keys = dict(state.get("keys") or {})
    if key:
        keys[key] = moment
    # Keep a week of keys; enough to stop repeats, small enough to stay tidy.
    keys = {k: v for k, v in keys.items() if moment - float(v) < 7 * 86_400}
    _save_state({"last_call_at": moment, "keys": keys,
                 "last_request_id": payload.get("request_id", "")})
    return True


def hangup() -> None:
    _request("POST", "/hangup", {})
