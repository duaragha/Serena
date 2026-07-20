"""Keyring-backed secrets for MCP env vars.

Secret VALUES never touch agent config files or Serena's master config —
only the variable NAMES are stored. The actual secret lives in the OS
keyring (gnome-keyring on Linux, Credential Manager on Windows).

When Serena spawns claude/codex, secrets get injected as env vars at
launch time. If the user runs the agent outside Serena, the env vars are
just missing — the MCP server will fail to authenticate, which is the
correct behavior (no silent leak).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_SERVICE = "serena.mcp"
_FALLBACK_PATH = Path.home() / ".config" / "serena" / "mcp-secrets.json"


def _load_keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except ImportError:
        return None


def _load_fallback() -> dict[str, str]:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        data = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_fallback(data: dict[str, str]) -> None:
    _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FALLBACK_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(_FALLBACK_PATH, 0o600)
    except OSError:
        pass


def _key(server: str, var: str) -> str:
    return f"{server}::{var}"


def set_secret(server: str, var: str, value: str) -> str:
    """Store a secret. Returns where it was stored ('keyring' or 'fallback')."""
    kr = _load_keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, _key(server, var), value)
            return "keyring"
        except Exception as e:
            print(f"[mcp.secrets] keyring set failed: {e}", flush=True)
    data = _load_fallback()
    data[_key(server, var)] = value
    _save_fallback(data)
    return "fallback"


def get_secret(server: str, var: str) -> str | None:
    kr = _load_keyring()
    if kr is not None:
        try:
            v = kr.get_password(_SERVICE, _key(server, var))
            if v is not None:
                return v
        except Exception:
            pass
    return _load_fallback().get(_key(server, var))


def delete_secret(server: str, var: str) -> None:
    kr = _load_keyring()
    if kr is not None:
        try:
            kr.delete_password(_SERVICE, _key(server, var))
        except Exception:
            pass
    data = _load_fallback()
    if _key(server, var) in data:
        data.pop(_key(server, var), None)
        _save_fallback(data)


def list_secret_names(server: str) -> list[str]:
    """List secret VAR names registered for a server (no values)."""
    prefix = f"{server}::"
    return sorted(k[len(prefix):] for k in _load_fallback().keys() if k.startswith(prefix))


def resolve_env(server: str, env: dict[str, str], secret_names: list[str]) -> dict[str, str]:
    """Build the actual env-var dict to pass to a child process. Plain env
    values pass through; secret names are filled from keyring/fallback."""
    out = dict(env or {})
    for var in secret_names or []:
        v = get_secret(server, var)
        if v is not None:
            out[var] = v
    return out
