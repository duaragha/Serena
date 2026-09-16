"""Read Muse's subscription snapshot without running or attaching a chat.

The native account exchange returns ``subs_usage`` alongside credentials.
Only the quota snapshot is retained; credentials are never written or logged.
MSP usage/read is process-local and is empty in a newly started host, so it
cannot supply the stable app's native-terminal limits ribbon.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = "muse-cli-usage"
DEFAULT_MODEL = "muse-spark"

REFRESH_SECONDS = 300.0
RETRY_SECONDS = 30.0
REQUEST_TIMEOUT = 8.0
ACCOUNT_URL = "https://api.meta.ai/muse-code/key"

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"at": 0.0, "data": None}
_REFRESHING = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the native credential to a redirected origin.
        return None


def _unavailable(reason: str) -> dict:
    return {"available": False, "source": SOURCE, "model": selected_model(), "reason": reason}


def selected_model() -> str:
    """The Serena identity a Muse turn runs under."""
    return DEFAULT_MODEL


def _binary() -> str | None:
    found = shutil.which("muse")
    if found:
        return found
    # Desktop services inherit a smaller PATH than interactive terminals.
    home = Path.home()
    for directory in (home / ".local" / "bin", home / "bin"):
        found = shutil.which("muse", path=str(directory))
        if found:
            return found
    return None


def parse_usage(text: str, *, now: float | None = None) -> dict:
    """Normalize the native account response; absent/invalid is not zero."""
    moment = int(now if now is not None else time.time())
    try:
        response = json.loads(text)
        usage = response["subs_usage"]
        windows = []
        for name in ("window", "weekly"):
            window = usage[name]
            used, resets = window["used_percent"], window["resets_at"]
            if type(used) is not int or used < 0 or type(resets) is not int or resets <= 0:
                raise ValueError("Invalid usage window")
            windows.append({"used_percentage": used, "resets_at": resets})
        duration = usage["window"]["window_duration_mins"]
        if type(duration) is not int or duration <= 0:
            raise ValueError("Invalid window duration")
    except (ValueError, TypeError, KeyError):
        return _unavailable("Muse has not reported subscription usage")
    return {
        "available": True,
        "source": SOURCE,
        "model": selected_model(),
        "updated_at": moment,
        "five_hour": windows[0],
        "seven_day": windows[1],
        "window_minutes": duration,
    }


def _auth_path() -> Path:
    override = os.environ.get("MUSE_AUTH_PATH", "").strip()
    root = Path(os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config")
    return Path(override).expanduser() if override else root / "muse" / "auth.json"


def _auth_signature() -> tuple:
    path = _auth_path()
    try:
        stat = path.stat()
        return str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return (str(path),)


def _fetch_usage(moment: float) -> dict:
    if _binary() is None:
        return _unavailable("Muse CLI is not installed")
    try:
        credential = json.loads(_auth_path().read_text(encoding="utf-8"))["providers"]["meta"]
        token = credential.get("access_token")
        if credential.get("mechanism") != "oauth" or not isinstance(token, str) or not token.strip():
            return _unavailable("Sign in to a Muse subscription to read its limits")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return _unavailable("Sign in to Muse to read its subscription limits")

    request = urllib.request.Request(ACCOUNT_URL, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "x-api-version": "1.0.0",
    })
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                return _unavailable("Muse usage response was invalid")
            return parse_usage(raw.decode("utf-8"), now=moment)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            return _unavailable("Muse sign-in needs refreshing before limits can be read")
        return _unavailable("Muse usage refresh failed (HTTP " + str(error.code) + ")")
    except (OSError, ValueError):
        return _unavailable("Muse usage refresh failed; retrying shortly")


def _refresh(moment: float, signature: tuple) -> None:
    global _REFRESHING
    try:
        result = _fetch_usage(moment)
    except Exception:
        # Never leak an upstream exception/response containing credentials.
        result = _unavailable("Muse usage refresh failed; retrying shortly")
    with _LOCK:
        _REFRESHING = False
        if signature != _auth_signature():
            return
        cached = _CACHE.get("data")
        if not result["available"] and cached and cached.get("available"):
            result = {**cached, "stale": True, "reason": result["reason"]}
        _CACHE["at"] = moment
        _CACHE["data"] = result
        _CACHE["retry"] = bool(result.get("reason"))


def read_muse_usage(*, now: float | None = None, force: bool = False) -> dict:
    """Return immediately and refresh in one background worker.

    ``force`` is a synchronous diagnostic/test hook, never used by the UI.
    A failed refresh retains the observation's original timestamp.
    """
    global _REFRESHING
    moment = now if now is not None else time.time()
    signature = _auth_signature()
    with _LOCK:
        if _CACHE.get("auth") != signature:
            _CACHE.update(at=0.0, data=None, auth=signature, retry=False)
        cached = _CACHE.get("data")
        interval = RETRY_SECONDS if _CACHE.get("retry") else REFRESH_SECONDS
        if _REFRESHING or (not force and cached is not None and moment - float(_CACHE["at"]) < interval):
            return copy.deepcopy(cached) if cached else {**_unavailable(""), "loading": True}
        _REFRESHING = True
    if force:
        _refresh(moment, signature)
        with _LOCK:
            return copy.deepcopy(_CACHE.get("data") or _unavailable("Muse sign-in changed; retrying"))
    threading.Thread(target=_refresh, args=(moment, signature), daemon=True, name="muse-usage").start()
    return copy.deepcopy(cached) if cached else {**_unavailable(""), "loading": True}
