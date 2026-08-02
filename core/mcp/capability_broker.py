"""On-demand access to Serena's configured HTTP MCP servers.

The resident model sees two small broker tools. Underlying tool schemas are
loaded only when Serena searches for a capability, and credentials never
enter model context.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.mcp.config import list_servers
from core.mcp.secrets import get_secret

_CACHE_SECONDS = 300.0
_CONNECT_SECONDS = 12.0
_RESULT_CHARS = 48_000
_SEARCH_LIMIT = 8

_READ_WORDS = {
    "audit",
    "auth",
    "calculate",
    "check",
    "describe",
    "details",
    "docs",
    "fetch",
    "find",
    "get",
    "inspect",
    "list",
    "lookup",
    "metrics",
    "preview",
    "query",
    "read",
    "report",
    "resolve",
    "search",
    "show",
    "status",
    "validate",
    "view",
    "whoami",
}
_WRITE_WORDS = {
    "add",
    "archive",
    "buy",
    "cancel",
    "clear",
    "connect",
    "create",
    "delete",
    "deploy",
    "disconnect",
    "edit",
    "execute",
    "generate",
    "invite",
    "link",
    "mark",
    "notify",
    "order",
    "partition",
    "place",
    "post",
    "publish",
    "remove",
    "reset",
    "restart",
    "restore",
    "retry",
    "scale",
    "send",
    "set",
    "start",
    "stop",
    "strip",
    "subscribe",
    "unarchive",
    "update",
    "upload",
    "write",
}
_ACTION_ALIASES = {
    "add": {"add", "attach", "include"},
    "archive": {"archive"},
    "buy": {"buy", "order", "purchase"},
    "cancel": {"cancel"},
    "clear": {"clear", "remove"},
    "connect": {"connect", "link"},
    "create": {"add", "create", "draft", "generate", "make", "start"},
    "delete": {"clear", "delete", "remove"},
    "deploy": {"deploy", "publish", "release", "redeploy"},
    "disconnect": {"disconnect", "unlink"},
    "edit": {"change", "edit", "update"},
    "generate": {"create", "generate", "make"},
    "invite": {"add", "invite"},
    "link": {"connect", "link"},
    "mark": {"mark"},
    "notify": {"notify"},
    "order": {"buy", "order", "purchase"},
    "place": {"book", "buy", "order", "place", "purchase"},
    "post": {"post", "publish", "send"},
    "publish": {"deploy", "post", "publish", "release"},
    "remove": {"clear", "delete", "remove"},
    "reset": {"reset"},
    "restart": {"restart"},
    "restore": {"restore"},
    "retry": {"retry"},
    "scale": {"scale"},
    "send": {"message", "send", "text"},
    "set": {"change", "set", "update"},
    "start": {"create", "launch", "start"},
    "stop": {"stop"},
    "strip": {"remove", "strip"},
    "subscribe": {"subscribe"},
    "unarchive": {"restore", "unarchive"},
    "update": {"change", "edit", "set", "update"},
    "upload": {"attach", "send", "upload"},
    "write": {"create", "edit", "save", "write"},
}
_BEEPER_WRITE_WORDS = {
    "add",
    "archive",
    "create",
    "delete",
    "edit",
    "focus",
    "mark",
    "mute",
    "notify",
    "pin",
    "react",
    "remind",
    "rename",
    "send",
    "start",
    "unarchive",
    "update",
    "upload",
}


@dataclass(frozen=True)
class Capability:
    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]
    access: str


_catalog: tuple[Capability, ...] = ()
_unavailable: tuple[str, ...] = ()
_catalog_at = 0.0
_catalog_lock: asyncio.Lock | None = None


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("_", " ")))


def _tool_access(tool: Any) -> str:
    annotations = getattr(tool, "annotations", None)
    read_hint = getattr(annotations, "readOnlyHint", None) if annotations else None
    if read_hint is True:
        return "read"
    if read_hint is False:
        return "write"
    words = _words(str(getattr(tool, "name", "")))
    if words & _WRITE_WORDS:
        return "write"
    if words & _READ_WORDS:
        return "read"
    return "unknown"


def _configured_http_servers() -> list[dict[str, Any]]:
    servers = []
    for server in list_servers():
        if not server.get("enabled", True):
            continue
        if server.get("transport") != "http" or not server.get("url"):
            continue
        servers.append(server)
    return servers


def _resolved_headers(server: Mapping[str, Any]) -> dict[str, str]:
    headers = dict(server.get("headers") or {})
    secret_values = {
        name: get_secret(str(server["name"]), name)
        for name in (server.get("secrets") or [])
    }
    for header, value in tuple(headers.items()):
        for name, secret in secret_values.items():
            if secret:
                value = value.replace("${" + name + "}", secret)
        headers[header] = value
    if "Authorization" not in headers:
        access_token = next(
            (
                value
                for name, value in secret_values.items()
                if name.endswith("_ACCESS_TOKEN") and value
            ),
            None,
        )
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
    return headers


async def _with_session(server: Mapping[str, Any], operation):
    timeout = httpx.Timeout(_CONNECT_SECONDS, read=_CONNECT_SECONDS)
    headers = _resolved_headers(server)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(
            str(server["url"]),
            http_client=client,
        ) as (reader, writer, _session_id):
            async with ClientSession(
                reader,
                writer,
                read_timeout_seconds=timedelta(seconds=_CONNECT_SECONDS),
            ) as session:
                await session.initialize()
                return await operation(session)


async def _load_server(server: Mapping[str, Any]) -> tuple[list[Capability], str | None]:
    async def operation(session: ClientSession):
        return await session.list_tools()

    try:
        result = await _with_session(server, operation)
    except Exception as exc:
        return [], f"{server['name']}: {type(exc).__name__}"

    capabilities = []
    enabled = set((server.get("advanced") or {}).get("enabled_tools") or [])
    disabled = set((server.get("advanced") or {}).get("disabled_tools") or [])
    for item in result.tools:
        if enabled and item.name not in enabled:
            continue
        if item.name in disabled:
            continue
        capabilities.append(
            Capability(
                server=str(server["name"]),
                tool=item.name,
                description=" ".join((item.description or "").split()),
                input_schema=dict(item.inputSchema or {}),
                access=_tool_access(item),
            )
        )
    return capabilities, None


async def catalog(*, refresh: bool = False) -> tuple[tuple[Capability, ...], tuple[str, ...]]:
    """Return live capabilities while caching only public schemas and names."""

    global _catalog, _catalog_at, _catalog_lock, _unavailable
    if not refresh and _catalog and time.monotonic() - _catalog_at < _CACHE_SECONDS:
        return _catalog, _unavailable
    if _catalog_lock is None:
        _catalog_lock = asyncio.Lock()
    async with _catalog_lock:
        if not refresh and _catalog and time.monotonic() - _catalog_at < _CACHE_SECONDS:
            return _catalog, _unavailable
        loaded = await asyncio.gather(*(_load_server(s) for s in _configured_http_servers()))
        _catalog = tuple(item for items, _error in loaded for item in items)
        _unavailable = tuple(error for _items, error in loaded if error)
        _catalog_at = time.monotonic()
        return _catalog, _unavailable


def _score(capability: Capability, query: str) -> int:
    wanted = _words(query)
    if not wanted:
        return 1
    tool_words = _words(capability.tool)
    server_words = _words(capability.server)
    description_words = _words(capability.description)
    score = 5 * len(wanted & tool_words)
    score += 3 * len(wanted & server_words)
    score += len(wanted & description_words)
    compact_query = "".join(sorted(wanted))
    compact_tool = "".join(sorted(tool_words))
    if compact_query and compact_query in compact_tool:
        score += 3
    return score


async def find_capabilities(query: str, *, refresh: bool = False) -> dict[str, Any]:
    capabilities, unavailable = await catalog(refresh=refresh)
    ranked = sorted(
        ((item, _score(item, query)) for item in capabilities),
        key=lambda row: (-row[1], row[0].server.lower(), row[0].tool.lower()),
    )
    positive = [row for row in ranked if row[1] > 0]
    selected = positive[:_SEARCH_LIMIT] if positive else ranked[:_SEARCH_LIMIT]
    return {
        "matches": [
            {
                "server": item.server,
                "tool": item.tool,
                "description": item.description[:800],
                "access": item.access,
                "input_schema": item.input_schema,
            }
            for item, _score_value in selected
        ],
        "available_servers": sorted({item.server for item in capabilities}),
        "temporarily_unavailable": list(unavailable),
    }


def _dynamic_access(capability: Capability, arguments: Mapping[str, Any]) -> str:
    access = capability.access
    if capability.server.lower() == "beeper":
        if _words(capability.tool) & _BEEPER_WRITE_WORDS:
            return "write"
    if capability.tool.lower() in {"graphql", "execute_query", "run_query"}:
        query = str(arguments.get("query") or arguments.get("sql") or "")
        if re.search(r"\b(mutation|insert|update|delete|alter|drop|create|grant|revoke)\b", query, re.I):
            return "write"
        if query.strip():
            return "read"
    return access


def _write_is_authorized(
    capability: Capability,
    arguments: Mapping[str, Any],
    origin: Mapping[str, object] | None,
) -> bool:
    text = " ".join(str((origin or {}).get("text") or "").split())
    if not text:
        return False
    origin_words = _words(text)
    action_words = _words(capability.tool) & _WRITE_WORDS
    query = str(arguments.get("query") or arguments.get("sql") or "")
    action_words |= _words(query) & _WRITE_WORDS
    if not action_words:
        return False
    if not any(
        origin_words & _ACTION_ALIASES.get(action, {action})
        for action in action_words
    ):
        return False
    subject_words = _words(capability.server) | _words(capability.tool)
    for value in arguments.values():
        if isinstance(value, (str, int, float)):
            subject_words |= _words(str(value))
    generic = {"add", "create", "delete", "edit", "get", "list", "remove", "send", "set", "update"}
    return bool(origin_words & (subject_words - generic))


def _serialize_result(result: Any) -> str:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json", exclude_none=True)
    else:
        payload = result
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(value) <= _RESULT_CHARS:
        return value
    return value[:_RESULT_CHARS] + "...[result truncated by Serena]"


async def invoke_capability(
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    origin: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    capabilities, _unavailable_now = await catalog()
    capability = next(
        (
            item
            for item in capabilities
            if item.server == server_name and item.tool == tool_name
        ),
        None,
    )
    if capability is None:
        capabilities, _unavailable_now = await catalog(refresh=True)
        capability = next(
            (
                item
                for item in capabilities
                if item.server == server_name and item.tool == tool_name
            ),
            None,
        )
    if capability is None:
        return {"ok": False, "called": False, "error": "unknown or unavailable capability"}

    access = _dynamic_access(capability, arguments)
    if access == "unknown":
        return {
            "ok": False,
            "called": False,
            "error": "capability has no trustworthy read or write classification",
        }
    if access == "write" and not _write_is_authorized(capability, arguments, origin):
        return {
            "ok": False,
            "called": False,
            "error": "the current user turn did not directly authorize this write",
        }

    server = next(
        (item for item in _configured_http_servers() if item["name"] == server_name),
        None,
    )
    if server is None:
        return {"ok": False, "called": False, "error": "server is no longer configured"}

    async def operation(session: ClientSession):
        return await session.call_tool(tool_name, dict(arguments))

    try:
        result = await _with_session(server, operation)
    except Exception as exc:
        return {
            "ok": False,
            "called": True,
            "error": f"{type(exc).__name__}: capability call failed",
        }
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "called": True,
        "server": server_name,
        "tool": tool_name,
        "access": access,
        "result": _serialize_result(result),
    }


def reset_catalog_cache() -> None:
    """Clear discovery state for tests and configuration changes."""

    global _catalog, _catalog_at, _catalog_lock, _unavailable
    _catalog = ()
    _unavailable = ()
    _catalog_at = 0.0
    _catalog_lock = None
