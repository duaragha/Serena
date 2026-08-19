"""On-demand access to Serena's configured HTTP MCP servers.

The resident model sees two small broker tools. Underlying tool schemas are
loaded only when Serena searches for a capability, and credentials never
enter model context.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.mcp.config import list_servers
from core.mcp.secrets import get_secret, resolved_headers
from core.security_policy import (
    DenialCircuitBreaker,
    SecurityPolicyError,
    URLPolicy,
    bounded_output,
    recovery_advice,
    suggest_approval,
)

_CACHE_SECONDS = 300.0
_CONNECT_SECONDS = 12.0
_RESULT_CHARS = 48_000
_SEARCH_LIMIT = 8
_DIRECT_RETRY_MAX_AGE_SECONDS = 240.0
_PREVIOUS_USER_TURN_KEY = "_previous_user_turn"
_DENIAL_DB_PATH = Path(
    os.environ.get("SERENA_SECURITY_DB_PATH", "")
    or Path.home() / ".local" / "state" / "serena" / "security.sqlite3"
)
_denial_breaker: DenialCircuitBreaker | None = None

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


def _global_stop_denial() -> str:
    """Serena's one global stop, reaching the MCP write path too.

    Every existing check here stays exactly as it was. This only adds the
    ability to halt outbound writes from one place, so a stop does not have to
    be issued per surface and per server.
    """

    try:
        from core.action_authority import default_authority

        state = default_authority().lock_state()
    except ImportError:
        return ""
    except Exception:
        return "Serena's action authority could not be consulted, so writes are refused"
    if state.get("engaged"):
        return f"every action is stopped right now: {state.get('reason') or 'global stop'}"
    return ""


def _breaker() -> DenialCircuitBreaker:
    global _denial_breaker
    if _denial_breaker is None:
        _denial_breaker = DenialCircuitBreaker(_DENIAL_DB_PATH, threshold=3)
    return _denial_breaker


def _denial_subject(server_name: str, tool_name: str) -> str:
    return f"mcp:{server_name}.{tool_name}"


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
_DIRECT_RETRY = re.compile(
    r"(?:^|[,.!?]\s*)"
    r"(?:ok(?:ay)?\s+|please\s+|now\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:"
    r"try(?:\s+(?:it|that|this))?\s+again"
    r"|retry\s+(?:it|that|this|the\s+(?:send|message)|the\s+beeper\s+message)"
    r")"
    r"(?:\s+(?:now|please))?[.!]?\s*$",
    re.IGNORECASE,
)


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
_consecutive_denials: dict[tuple[str, str], int] = {}


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
    return resolved_headers(dict(server), secret_getter=get_secret)


async def _with_session(server: Mapping[str, Any], operation):
    timeout = httpx.Timeout(_CONNECT_SECONDS, read=_CONNECT_SECONDS)
    headers = _resolved_headers(server)
    policy = URLPolicy(
        allowed_domains=tuple(server.get("allowed_domains") or ()),
        allow_private_network=bool(server.get("allow_private_network", False)),
        allow_http=bool(server.get("allow_http", False)),
    )
    policy.validate(str(server["url"]))

    async def validate_redirect(response: httpx.Response) -> None:
        if response.is_redirect:
            location = response.headers.get("location", "")
            policy.validate_redirect(str(response.request.url), location)
            raise SecurityPolicyError(
                "MCP redirects are refused; configure the validated final URL explicitly"
            )

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"response": [validate_redirect]},
    ) as client:
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
        if re.search(
            r"\b(mutation|insert|update|delete|alter|drop|create|grant|revoke)\b", query, re.I
        ):
            return "write"
        if query.strip():
            return "read"
    return access


def _turn_authorizes_write(
    capability: Capability,
    arguments: Mapping[str, Any],
    text: str,
) -> bool:
    text = " ".join(str(text or "").split())
    if not text:
        return False
    origin_words = _words(text)
    action_words = _words(capability.tool) & _WRITE_WORDS
    query = str(arguments.get("query") or arguments.get("sql") or "")
    action_words |= _words(query) & _WRITE_WORDS
    if not action_words:
        return False
    if not any(origin_words & _ACTION_ALIASES.get(action, {action}) for action in action_words):
        return False
    subject_words = _words(capability.server) | _words(capability.tool)
    for value in arguments.values():
        if isinstance(value, (str, int, float)):
            subject_words |= _words(str(value))
    generic = {"add", "create", "delete", "edit", "get", "list", "remove", "send", "set", "update"}
    return bool(origin_words & (subject_words - generic))


def _direct_retry_previous_text(
    text: str,
    origin: Mapping[str, object] | None,
) -> str:
    if not _DIRECT_RETRY.search(text):
        return ""
    previous = (origin or {}).get(_PREVIOUS_USER_TURN_KEY)
    if not isinstance(previous, Mapping):
        return ""
    try:
        age = time.time() - float(previous.get("at"))
    except (TypeError, ValueError):
        return ""
    if not 0.0 <= age <= _DIRECT_RETRY_MAX_AGE_SECONDS:
        return ""
    return " ".join(str(previous.get("text") or "").split())


def _previous_turn_authorizes_retry(
    capability: Capability,
    arguments: Mapping[str, Any],
    previous_text: str,
) -> bool:
    # Keep this exception as narrow as the incident: retrying a Beeper message.
    # Other writes still need their action and subject in the current turn.
    if capability.server.lower() != "beeper" or "send" not in _words(capability.tool):
        return False
    message_parts = [
        _words(str(value))
        for key, value in arguments.items()
        if isinstance(value, str)
        and _words(str(key)) & {"body", "content", "message", "text"}
        and _words(value)
    ]
    if not message_parts:
        return False
    previous_words = _words(previous_text)
    return _turn_authorizes_write(capability, arguments, previous_text) and all(
        words <= previous_words for words in message_parts
    )


def _write_is_authorized(
    capability: Capability,
    arguments: Mapping[str, Any],
    origin: Mapping[str, object] | None,
) -> bool:
    text = " ".join(str((origin or {}).get("text") or "").split())
    if _turn_authorizes_write(capability, arguments, text):
        return True
    previous_text = _direct_retry_previous_text(text, origin)
    return bool(
        previous_text and _previous_turn_authorizes_retry(capability, arguments, previous_text)
    )


def _serialize_result(result: Any) -> tuple[str, dict[str, Any]]:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json", exclude_none=True)
    else:
        payload = result
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    bounded = bounded_output(value, limit=_RESULT_CHARS)
    receipt = {
        key: bounded[key]
        for key in ("sha256", "characters", "omitted_characters", "truncated")
        if key in bounded
    }
    return str(bounded["text"]), receipt


async def invoke_capability(
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    origin: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    capabilities, _unavailable_now = await catalog()
    capability = next(
        (item for item in capabilities if item.server == server_name and item.tool == tool_name),
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
    if access == "write":
        stop = _global_stop_denial()
        if stop:
            return {"ok": False, "called": False, "error": stop}
    if access == "write" and not _write_is_authorized(capability, arguments, origin):
        denial_state = _breaker().record_denial(_denial_subject(server_name, tool_name))
        count = int(denial_state["consecutive"])
        refusal = {
            "ok": False,
            "called": False,
            "error": "the current user turn did not directly authorize this write",
        }
        if count >= 3:
            refusal["circuit_breaker"] = "tripped after three consecutive denials"
            refusal["approval_suggestion"] = suggest_approval(
                f"invoke {server_name}.{tool_name}",
                "the capability needs fresh direct authority from Raghav",
                ("tool.invoke",),
            ).to_dict()
        return refusal

    denial_key = (server_name, tool_name)
    denial_subject = _denial_subject(server_name, tool_name)
    if access == "write" and _breaker().is_tripped(denial_subject):
        return {
            "ok": False,
            "called": False,
            "error": "the capability denial circuit breaker is tripped",
            "approval_suggestion": suggest_approval(
                f"reset and invoke {server_name}.{tool_name}",
                "three consecutive denied attempts require an explicit operator reset",
                ("tool.invoke",),
            ).to_dict(),
        }
    if access == "write":
        _breaker().record_success(denial_subject)

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
            "recovery": recovery_advice(exc, idempotent=access == "read"),
        }
    _consecutive_denials.pop(denial_key, None)
    serialized, output_receipt = _serialize_result(result)
    output_was_truncated = bool(output_receipt["truncated"])
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "called": True,
        "server": server_name,
        "tool": tool_name,
        "access": access,
        "result": serialized,
        "output_receipt": output_receipt,
        **(
            {"recovery": recovery_advice("", idempotent=access == "read", truncated=True)}
            if output_was_truncated
            else {}
        ),
    }


def reset_catalog_cache() -> None:
    """Clear discovery state for tests and configuration changes."""

    global _catalog, _catalog_at, _catalog_lock, _unavailable
    _catalog = ()
    _unavailable = ()
    _catalog_at = 0.0
    _catalog_lock = None
    _consecutive_denials.clear()


def reset_denial_circuit(server_name: str, tool_name: str, *, explicit_approval: bool) -> bool:
    """Reset only from an explicit operator approval path, never from a suggestion."""

    if not explicit_approval:
        return False
    _consecutive_denials.pop((str(server_name), str(tool_name)), None)
    return _breaker().record_explicit_approval(
        _denial_subject(str(server_name), str(tool_name))
    )
