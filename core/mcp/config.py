"""Master MCP config — Serena's single source of truth.

Stored at ~/.config/serena/mcp.json. Format:

{
  "version": 1,
  "servers": {
    "<name>": {
      "name": "<name>",
      "transport": "stdio" | "http",
      "command": "npx",            # stdio only
      "args": ["-y", "@org/pkg"],  # stdio only
      "url": "https://...",        # http only
      "env": {"KEY": "value"},     # plain env vars (no secrets)
      "secrets": ["API_KEY"],      # env-var names whose values live in keyring
      "headers": {"X-...": "..."}, # http only
      "cwd": "/path",              # optional, stdio only
      "targets": ["claude", "codex"],
      "enabled": true,
      "advanced": {                # codex-only extras, hidden behind UI Advanced panel
        "startup_timeout_sec": null,
        "tool_timeout_sec": null,
        "enabled_tools": [],
        "disabled_tools": []
      },
      "shared": true              # if true, ONE shared process serves every claude
                                   # (saves ~50 processes when many chats are open). Stateful
                                   # MCPs (codex, playwright) keep shared=false so each claude
                                   # gets its own isolated subprocess.
    }
  }
}
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.security_policy import (
    SecurityPolicyError,
    build_child_environment,
    validate_plain_environment,
    validate_protected_path,
)

CONFIG_PATH = Path.home() / ".config" / "serena" / "mcp.json"

DEFAULT_CONFIG: dict = {"version": 1, "servers": {}}

# MCPs that maintain per-session state — DON'T multiplex these. Each claude
# should get its own subprocess so e.g. playwright's browser state doesn't
# leak between chats, and codex's MCP server doesn't try to serve N parents.
_STATEFUL_DEFAULTS = {"codex", "playwright"}


def _ensure_dir() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
        data.setdefault("version", 1)
        data.setdefault("servers", {})
        return data
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)


def save(data: dict) -> None:
    _ensure_dir()
    CONFIG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def list_servers() -> list[dict]:
    data = load()
    out = []
    for name, entry in (data.get("servers") or {}).items():
        if not isinstance(entry, dict):
            continue
        merged = {"name": name, **entry}
        out.append(merged)
    return sorted(out, key=lambda s: s.get("name", ""))


def get_server(name: str) -> dict | None:
    data = load()
    entry = (data.get("servers") or {}).get(name)
    if not isinstance(entry, dict):
        return None
    return {"name": name, **entry}


def upsert_server(server: dict) -> dict:
    """Validate and persist a server entry. Returns the saved record."""
    name = (server.get("name") or "").strip()
    if not name:
        raise ValueError("Server name is required")
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Name must be alphanumeric (with - or _)")

    transport = (server.get("transport") or "stdio").lower()
    if transport not in ("stdio", "http"):
        raise ValueError("Transport must be 'stdio' or 'http'")

    if transport == "stdio":
        if not (server.get("command") or "").strip():
            raise ValueError("stdio servers require a command")
    else:
        url = (server.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("http servers require an https/http URL")
        if not urlsplit(url).hostname:
            raise ValueError("http servers require a valid hostname")

    targets = server.get("targets") or ["claude", "codex"]
    if not isinstance(targets, list) or not targets:
        targets = ["claude", "codex"]
    targets = [t for t in targets if t in ("claude", "codex")]
    if not targets:
        raise ValueError("Pick at least one target (claude or codex)")

    # `shared` defaults: True for stateless lookup MCPs, False for stateful ones
    # (codex, playwright). User-provided value wins if explicitly set.
    if "shared" in server:
        shared = bool(server.get("shared"))
    else:
        shared = transport == "stdio" and name not in _STATEFUL_DEFAULTS

    try:
        plain_env = validate_plain_environment(_string_map(server.get("env")))
    except SecurityPolicyError as exc:
        raise ValueError(str(exc)) from exc
    secret_names = _secret_names(server.get("secrets"))
    env_allowlist = _env_names(server.get("env_allowlist"))
    if not env_allowlist:
        env_allowlist = sorted({*plain_env, *secret_names})
    undeclared = sorted(({*plain_env, *secret_names}) - set(env_allowlist))
    if undeclared:
        raise ValueError("MCP environment values are not allowlisted: " + ", ".join(undeclared))
    try:
        ambient_names = sorted(set(env_allowlist) - set(plain_env) - set(secret_names))
        build_child_environment(
            {},
            plain=plain_env,
            secrets={name: "declared" for name in secret_names},
            inherit=ambient_names,
        )
    except SecurityPolicyError as exc:
        raise ValueError(str(exc)) from exc
    headers = _safe_headers(server.get("headers"), secret_names)
    url = (server.get("url") or "").strip() or None
    allowed_domains = _domains(server.get("allowed_domains"))
    if url and not allowed_domains:
        hostname = (urlsplit(url).hostname or "").lower()
        allowed_domains = [hostname] if hostname else []
    allow_http = bool(server.get("allow_http", False))
    if url and urlsplit(url).scheme == "http" and not allow_http:
        raise ValueError("plain HTTP MCP URLs require explicit allow_http")
    cwd = (server.get("cwd") or "").strip() or None
    if cwd and transport == "stdio":
        if not Path(cwd).expanduser().is_absolute():
            raise ValueError("MCP cwd must be an absolute path")
        try:
            cwd = str(
                validate_protected_path(
                    cwd,
                    allowed_roots=(Path.home(), Path(tempfile.gettempdir())),
                )
            )
        except SecurityPolicyError as exc:
            raise ValueError(str(exc)) from exc

    record = {
        "name": name,
        "transport": transport,
        "command": (server.get("command") or "").strip() or None,
        "args": _string_list(server.get("args")),
        "url": url,
        "env": plain_env,
        "env_allowlist": env_allowlist,
        "secrets": secret_names,
        "headers": headers,
        "allowed_domains": allowed_domains,
        "allow_private_network": bool(server.get("allow_private_network", False)),
        "allow_http": allow_http,
        "cwd": cwd if transport == "stdio" else None,
        "targets": targets,
        "enabled": bool(server.get("enabled", True)),
        "shared": shared,
        "advanced": _advanced(server.get("advanced")),
    }

    data = load()
    data.setdefault("servers", {})[name] = {k: v for k, v in record.items() if k != "name"}
    save(data)
    return {"name": name, **data["servers"][name]}


def delete_server(name: str) -> bool:
    data = load()
    servers = data.get("servers") or {}
    if name not in servers:
        return False
    servers.pop(name, None)
    data["servers"] = servers
    save(data)
    return True


def _string_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if isinstance(x, (str, int, float))]


def _string_map(v: Any) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    out: dict[str, str] = {}
    for k, val in v.items():
        if isinstance(k, str) and isinstance(val, (str, int, float)):
            out[k] = str(val)
    return out


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _env_names(value: Any) -> list[str]:
    names = _string_list(value)
    invalid = [name for name in names if not _ENV_NAME.fullmatch(name)]
    if invalid:
        raise ValueError("invalid MCP environment name: " + ", ".join(invalid))
    return sorted(set(names))


def _secret_names(value: Any) -> list[str]:
    return _env_names(value)


def _domains(value: Any) -> list[str]:
    domains = [item.rstrip(".").lower() for item in _string_list(value)]
    invalid = [domain for domain in domains if not _DOMAIN.fullmatch(domain)]
    if invalid:
        raise ValueError("invalid MCP allowed domain: " + ", ".join(invalid))
    return sorted(set(domains))


def _safe_headers(value: Any, secret_names: list[str]) -> dict[str, str]:
    headers = _string_map(value)
    declared = set(secret_names)
    for name, content in headers.items():
        references = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", content))
        undeclared = sorted(references - declared)
        if undeclared:
            raise ValueError(
                f"MCP header {name} references undeclared secrets: " + ", ".join(undeclared)
            )
        sensitive = any(
            marker in name.lower() for marker in ("authorization", "cookie", "token", "key")
        )
        if sensitive and not references:
            raise ValueError(f"MCP header {name} must use a named secret placeholder")
        if "authorization" in name.lower() and not re.fullmatch(
            r"(?:[A-Za-z][A-Za-z0-9._-]*\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*\}",
            content.strip(),
        ):
            raise ValueError(
                f"MCP header {name} may contain only an auth scheme and one named secret"
            )
    return headers


def _advanced(v: Any) -> dict:
    if not isinstance(v, dict):
        v = {}
    return {
        "startup_timeout_sec": v.get("startup_timeout_sec"),
        "tool_timeout_sec": v.get("tool_timeout_sec"),
        "enabled_tools": _string_list(v.get("enabled_tools")),
        "disabled_tools": _string_list(v.get("disabled_tools")),
    }
