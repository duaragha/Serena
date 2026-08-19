"""Authenticated read-only MCP access for Serena Fleet's non-writing legs.

Fleet's Research and Review legs are labelled read-only, and that label used to
be enforced by handing every worker an empty MCP config. That stopped writes,
but it also stopped the only thing those phases exist to do: find out what is
actually true outside the checkout. A Research leg could read our notes about
the Google Ads account and never read the account.

This module reopens exactly that door and nothing else:

* a leg whose access mode is not ``write`` gets one stdio MCP server, this
  gateway, and nothing from the user's own MCP configuration;
* the gateway exposes only tools classified ``read``, per tool, from a curated
  server list. Unknown classification is denied, never assumed safe;
* the classification is re-checked against the upstream server at call time, so
  a stale catalog cannot widen access;
* writers are untouched. They already have a shell and full permissions, so
  nothing here changes what a Code or Fix leg can do.

The catalog is built out of band (network) and read synchronously (no network)
by ``worker_command`` and by the gateway itself. No catalog means no MCP flags
at all: the failure mode is a worker that runs exactly as it did before, never
one that silently gets more than it should.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CATALOG_VERSION = 1
GATEWAY_SERVER_NAME = "serena_read"
CATALOG_TTL_SECONDS = 6 * 60 * 60
CONNECT_TIMEOUT_SECONDS = 15.0
LIST_TIMEOUT_SECONDS = 25.0
CALL_TIMEOUT_SECONDS = 120.0
BUILD_TIMEOUT_SECONDS = 60.0
MAX_RESULT_CHARS = 48_000
MAX_TOOL_NAME_CHARS = 96

# The accounts and documentation a Research or Review leg actually needs to
# check a claim against reality. Deliberately not "every configured server":
# every exposed tool costs schema in a worker's context, and a server nobody
# researches against is pure cost. Widen it in Fleet config, not by accident.
DEFAULT_READ_SERVERS: tuple[str, ...] = (
    "context7",
    "google-ads",
    "google-analytics",
    "google-merchant",
    "frameworth-shopify",
    "amazon-seller-central",
    "Railway",
)

# Curated per-tool decisions that outrank both the server's own annotation and
# the name heuristic. Keys are "<server>.<tool>".
CURATED_READ: frozenset[str] = frozenset(
    {
        "Railway.http_error_rate",
        "Railway.http_requests",
        "Railway.http_response_time",
        "amazon-shopping.amazon_product_reviews",
        "frameworth-shopify.mc_listings",
        "google-ads.keyword_research",
    }
)
CURATED_WRITE: frozenset[str] = frozenset(
    {
        # Arbitrary GraphQL is a mutation surface wearing a read-shaped name.
        "frameworth-shopify.graphql",
        "frameworth-shopify.push_theme_asset",
        # Auth flows mutate stored credentials on the server side.
        "amazon-shopping.amazon_login",
        "netsuite.authenticate",
        "supabase.authenticate",
        "claude_ai_Google_Calendar.authenticate",
    }
)

# Reads that hand back credentials. Refusing these is not about write safety:
# a research leg has no reason to pull secret values into its context or its
# event log, and "it is technically a read" is not a good enough reason to.
CURATED_SENSITIVE: frozenset[str] = frozenset({"Railway.list_variables"})
_SENSITIVE_WORDS = frozenset(
    {"credential", "credentials", "password", "secret", "secrets", "token", "variables"}
)

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


# --- classification -------------------------------------------------------


def _words(value: str) -> set[str]:
    """Tokenise a tool name, splitting camelCase and acronyms too.

    The shared broker tokeniser only splits on separators, which reads
    ``DNS_getDNSRecordsV1`` as one opaque word and classifies it unknown. Fleet
    needs the finer split, but the vocabulary stays shared so there is one
    definition of what a read word is.
    """

    return {match.group(0).lower() for match in _CAMEL.finditer(value.replace("_", " "))}


def classify(server: str, tool: str, annotations: Any = None) -> str:
    """Return ``read``, ``write``, ``sensitive`` or ``unknown`` for one tool.

    Only ``read`` is ever exposed. ``unknown`` is denied rather than guessed,
    which is the whole reason this is a per-tool decision instead of a per-leg
    label.
    """

    # Imported here so building a worker argv does not drag in the resident
    # broker's HTTP stack; the vocabulary itself stays shared on purpose.
    from core.mcp.capability_broker import _READ_WORDS, _WRITE_WORDS

    key = f"{server}.{tool}"
    words = _words(tool)
    if key in CURATED_SENSITIVE or (words & _SENSITIVE_WORDS):
        return "sensitive"
    if key in CURATED_WRITE:
        return "write"
    if key in CURATED_READ:
        return "read"
    read_hint = getattr(annotations, "readOnlyHint", None) if annotations else None
    destructive = getattr(annotations, "destructiveHint", None) if annotations else None
    if read_hint is True and destructive is not True:
        return "read"
    if read_hint is False or destructive is True:
        return "write"
    if words & _WRITE_WORDS:
        return "write"
    if words & _READ_WORDS:
        return "read"
    return "unknown"


def exposed_name(server: str, tool: str) -> str:
    """Flatten a server/tool pair into one gateway tool name.

    Deliberately avoids ``__`` so a Claude permission rule of the form
    ``mcp__serena_read__<name>`` stays unambiguous.
    """

    slug = _SAFE_NAME.sub("_", server).strip("_").replace("-", "_").lower()
    name = _SAFE_NAME.sub("_", f"read_{slug}_{tool}").strip("_")
    return name[:MAX_TOOL_NAME_CHARS]


# --- configuration --------------------------------------------------------


def read_access_enabled(access_mode: str) -> bool:
    """Writers keep the empty MCP config they already had."""

    return str(access_mode or "").lower() != "write"


def allowed_servers() -> tuple[str, ...]:
    raw = os.environ.get("SERENA_FLEET_READ_MCP_SERVERS", "").strip()
    if raw:
        if raw.lower() in {"none", "off", "disabled"}:
            return ()
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    try:
        from core.fleet_policy import load_config

        block = (load_config().get("defaults") or {}).get("mcp_read_access")
    except Exception:
        block = None
    if isinstance(block, Mapping):
        if block.get("enabled") is False:
            return ()
        servers = block.get("servers")
        if isinstance(servers, list):
            names = tuple(str(item).strip() for item in servers if str(item).strip())
            if names:
                return names
    return DEFAULT_READ_SERVERS


def catalog_path() -> Path:
    configured = os.environ.get("SERENA_FLEET_STATE_DIR", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state" / "serena" / "fleet"
    )
    return root / "mcp-read-catalog.json"


def gateway_command() -> list[str]:
    """The argv that starts this gateway as a stdio MCP server."""

    override = os.environ.get("SERENA_FLEET_READ_MCP_COMMAND", "").strip()
    if override:
        parts = json.loads(override) if override.startswith("[") else override.split()
        return [str(part) for part in parts]
    import shutil
    import sys

    sibling = Path(sys.executable).with_name("chats")
    binary = str(sibling) if sibling.exists() else (shutil.which("chats") or "chats")
    return [binary, "fleet", "read-mcp"]


# --- catalog --------------------------------------------------------------


def load_catalog() -> dict[str, Any] | None:
    path = catalog_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
        return None
    if not isinstance(data.get("tools"), list):
        return None
    return data


def catalog_tools(catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Exposed tools, filtered to servers the current config still allows."""

    data = catalog if catalog is not None else load_catalog()
    if not data:
        return []
    allowed = set(allowed_servers())
    # Sorted so a worker's allowlist and enabled_tools are byte-stable across
    # runs; a churning argv is noise in every diff and every event log.
    return sorted(
        (
            tool
            for tool in data["tools"]
            if isinstance(tool, dict) and str(tool.get("server")) in allowed
        ),
        key=lambda tool: str(tool.get("name") or ""),
    )


def catalog_is_stale(catalog: dict[str, Any] | None) -> bool:
    if not catalog:
        return True
    try:
        generated = float(catalog.get("generated_at") or 0)
    except (TypeError, ValueError):
        return True
    return (time.time() - generated) > CATALOG_TTL_SECONDS


def _server_records() -> list[dict[str, Any]]:
    from core.mcp.config import list_servers

    allowed = set(allowed_servers())
    records = []
    for server in list_servers():
        if server.get("name") not in allowed or not server.get("enabled", True):
            continue
        transport = server.get("transport") or "stdio"
        if transport == "http" and not server.get("url"):
            continue
        if transport == "stdio" and not server.get("command"):
            continue
        records.append(server)
    return records


def _http_policy_server(server: Mapping[str, Any]) -> dict[str, Any]:
    """Pin the URL policy to the exact host Serena's own config names.

    The master config is trusted input, so its host is the tightest correct
    allowlist. Redirects away from it are still refused by the shared policy.
    """

    resolved = dict(server)
    if not resolved.get("allowed_domains"):
        host = urlsplit(str(resolved.get("url") or "")).hostname or ""
        resolved["allowed_domains"] = [host] if host else []
    if "allow_private_network" not in resolved:
        resolved["allow_private_network"] = True
    return resolved


@asynccontextmanager
async def _upstream_session(server: Mapping[str, Any]):
    """Open one short-lived client session against an upstream MCP server.

    HTTP servers go through the same URL policy and redirect refusal the
    resident broker uses; stdio servers are spawned with their configured
    secrets, which never enter a worker's context.
    """

    from datetime import timedelta

    from mcp.client.session import ClientSession

    transport = server.get("transport") or "stdio"
    if transport == "http":
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        from core.mcp.secrets import get_secret, resolved_headers
        from core.security_policy import SecurityPolicyError, URLPolicy

        resolved = _http_policy_server(server)
        policy = URLPolicy(
            allowed_domains=tuple(resolved.get("allowed_domains") or ()),
            allow_private_network=bool(resolved.get("allow_private_network", False)),
            allow_http=bool(resolved.get("allow_http", False)),
        )
        policy.validate(str(resolved["url"]))

        async def validate_redirect(response: httpx.Response) -> None:
            if response.is_redirect:
                policy.validate_redirect(
                    str(response.request.url), response.headers.get("location", "")
                )
                raise SecurityPolicyError(
                    "MCP redirects are refused; configure the validated final URL explicitly"
                )

        async with (
            httpx.AsyncClient(
            headers=resolved_headers(resolved, secret_getter=get_secret),
            timeout=httpx.Timeout(CONNECT_TIMEOUT_SECONDS, read=CALL_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            event_hooks={"response": [validate_redirect]},
        ) as client, streamable_http_client(str(resolved["url"]), http_client=client) as (
                reader,
                writer,
                _session_id,
            ),
            ClientSession(
                reader,
                writer,
                read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
            ) as session,
        ):
            await session.initialize()
            yield session
        return

    from mcp.client.stdio import StdioServerParameters, stdio_client

    from core.mcp.secrets import server_environment

    parameters = StdioServerParameters(
        command=str(server.get("command") or ""),
        args=[str(item) for item in (server.get("args") or [])],
        env=server_environment(dict(server)),
        cwd=server.get("cwd") or None,
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(
            reader,
            writer,
            read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
        ) as session,
    ):
        await session.initialize()
        yield session


async def _list_upstream_tools(server: Mapping[str, Any]) -> list[Any]:
    async with _upstream_session(server) as session:
        result = await asyncio.wait_for(session.list_tools(), timeout=LIST_TIMEOUT_SECONDS)
        return list(result.tools)


async def build_catalog() -> dict[str, Any]:
    """Connect to every allowed server and record its read-classified tools."""

    servers: dict[str, Any] = {}
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    records = _server_records()
    # One unreachable account server must not serialise behind the others; the
    # whole build is meant to be a short bounded step at the start of a run.
    listings = await asyncio.gather(
        *(_list_upstream_tools(server) for server in records),
        return_exceptions=True,
    )
    for server, upstream in zip(records, listings, strict=True):
        name = str(server["name"])
        if isinstance(upstream, BaseException):
            servers[name] = {
                "transport": server.get("transport") or "stdio",
                "status": "unreachable",
                "error": f"{type(upstream).__name__}: {upstream}"[:200],
                "exposed": 0,
                "denied": 0,
            }
            continue
        exposed = 0
        denied = 0
        for tool in upstream:
            tool_name = str(getattr(tool, "name", "") or "")
            if not tool_name:
                continue
            access = classify(name, tool_name, getattr(tool, "annotations", None))
            if access != "read":
                denied += 1
                continue
            flattened = exposed_name(name, tool_name)
            if flattened in seen:
                continue
            seen.add(flattened)
            exposed += 1
            tools.append(
                {
                    "name": flattened,
                    "server": name,
                    "tool": tool_name,
                    "description": str(getattr(tool, "description", "") or "")[:2_000],
                    "input_schema": getattr(tool, "inputSchema", None)
                    or {"type": "object", "properties": {}},
                }
            )
        servers[name] = {
            "transport": server.get("transport") or "stdio",
            "status": "ok",
            "error": None,
            "exposed": exposed,
            "denied": denied,
        }
    return {
        "version": CATALOG_VERSION,
        "generated_at": time.time(),
        "servers": servers,
        "tools": sorted(tools, key=lambda item: item["name"]),
    }


def refresh_catalog(*, force: bool = False, allow_background: bool = False) -> dict[str, Any]:
    """Rebuild the catalog on disk. Bounded, best effort, never raises.

    ``allow_background`` lets a caller that already has a usable catalog keep
    going while the rebuild happens behind it. Only the very first build, when
    there is nothing on disk to work from, is allowed to make a run wait.
    """

    summary: dict[str, Any] = {"refreshed": False, "reason": "", "tool_count": 0}
    if not allowed_servers():
        summary["reason"] = "no servers are configured for Fleet read access"
        return summary
    existing = load_catalog()
    if not force and not catalog_is_stale(existing):
        summary["reason"] = "catalog is current"
        summary["tool_count"] = len(catalog_tools(existing))
        summary["servers"] = (existing or {}).get("servers", {})
        return summary
    if allow_background and catalog_tools(existing):
        import threading

        threading.Thread(
            target=lambda: refresh_catalog(force=True),
            name="fleet-read-mcp-refresh",
            daemon=True,
        ).start()
        summary["reason"] = "stale catalog is refreshing in the background"
        summary["tool_count"] = len(catalog_tools(existing))
        summary["servers"] = (existing or {}).get("servers", {})
        return summary
    try:
        catalog = _run_async(asyncio.wait_for(build_catalog(), timeout=BUILD_TIMEOUT_SECONDS))
    except Exception as exc:
        summary["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return summary
    path = catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        summary["reason"] = f"could not write catalog: {exc}"[:200]
        return summary
    summary["refreshed"] = True
    summary["tool_count"] = len(catalog["tools"])
    summary["servers"] = catalog["servers"]
    return summary


def _run_async(coroutine) -> Any:
    """Run one coroutine whether or not the caller already owns a loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


# --- worker wiring --------------------------------------------------------


def claude_flags(access_mode: str) -> list[str]:
    """MCP flags for one Claude worker, or nothing when access is not granted."""

    tools = catalog_tools() if read_access_enabled(access_mode) else []
    if not tools:
        return []
    config = {
        "mcpServers": {
            GATEWAY_SERVER_NAME: {
                "type": "stdio",
                "command": gateway_command()[0],
                "args": gateway_command()[1:],
            }
        }
    }
    allowed = ",".join(f"mcp__{GATEWAY_SERVER_NAME}__{tool['name']}" for tool in tools)
    return [
        "--mcp-config",
        json.dumps(config, separators=(",", ":")),
        "--allowedTools",
        allowed,
    ]


def codex_flags(access_mode: str) -> list[str]:
    """Config overrides that give one Codex worker the same gateway."""

    tools = catalog_tools() if read_access_enabled(access_mode) else []
    if not tools:
        return []
    command = gateway_command()
    prefix = f"mcp_servers.{GATEWAY_SERVER_NAME}"
    enabled = json.dumps([tool["name"] for tool in tools])
    return [
        "-c",
        f"{prefix}.command={json.dumps(command[0])}",
        "-c",
        f"{prefix}.args={json.dumps(command[1:])}",
        "-c",
        f"{prefix}.startup_timeout_sec=30",
        "-c",
        f"{prefix}.tool_timeout_sec={int(CALL_TIMEOUT_SECONDS)}",
        "-c",
        f"{prefix}.enabled_tools={enabled}",
    ]


def prompt_block(access_mode: str) -> str:
    """The worker-facing description of what this leg can actually reach."""

    tools = catalog_tools() if read_access_enabled(access_mode) else []
    if not tools:
        return ""
    by_server: dict[str, list[str]] = {}
    for tool in tools:
        by_server.setdefault(str(tool["server"]), []).append(str(tool["name"]))
    lines = [
        "You have authenticated read-only access to Raghav's real accounts and "
        f"documentation through the {GATEWAY_SERVER_NAME} MCP server. Check claims "
        "against the account itself instead of reasoning from repository notes, "
        "and cite what you read. Every exposed tool is read-classified and the "
        "gateway refuses anything else, so a refusal is a policy answer, not a "
        "blocker to work around.",
    ]
    for server in sorted(by_server):
        lines.append(f"- {server}: {', '.join(sorted(by_server[server]))}")
    return "\n".join(lines)


# --- the gateway itself ---------------------------------------------------


async def _forward(entry: Mapping[str, Any], arguments: dict[str, Any]) -> Any:
    from mcp import types

    from core.mcp.config import get_server

    server_name = str(entry["server"])
    tool_name = str(entry["tool"])
    if server_name not in set(allowed_servers()):
        raise PermissionError(f"{server_name} is not available to Fleet read access")
    server = get_server(server_name)
    if server is None or not server.get("enabled", True):
        raise PermissionError(f"{server_name} is not configured")
    async with _upstream_session(server) as session:
        # Re-classify against the live server. The catalog decides what is
        # offered; this decides what is actually allowed to run, so a stale
        # catalog can never widen access.
        listed = await asyncio.wait_for(session.list_tools(), timeout=LIST_TIMEOUT_SECONDS)
        match = next((item for item in listed.tools if item.name == tool_name), None)
        if match is None:
            raise PermissionError(f"{server_name}.{tool_name} no longer exists")
        access = classify(server_name, tool_name, getattr(match, "annotations", None))
        if access != "read":
            raise PermissionError(
                f"{server_name}.{tool_name} classifies as {access}; Fleet read access refuses it"
            )
        result = await asyncio.wait_for(
            session.call_tool(tool_name, arguments),
            timeout=CALL_TIMEOUT_SECONDS,
        )
    content = []
    budget = MAX_RESULT_CHARS
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and len(text) > budget:
            block = types.TextContent(
                type="text",
                text=text[:budget] + f"\n[truncated at {MAX_RESULT_CHARS} characters]",
            )
            budget = 0
        elif isinstance(text, str):
            budget -= len(text)
        content.append(block)
        if budget <= 0:
            break
    return types.CallToolResult(content=content, isError=bool(result.isError))


async def _serve() -> None:
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    entries = {tool["name"]: tool for tool in catalog_tools()}
    server: Any = Server(GATEWAY_SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=str(entry["name"]),
                description=str(entry.get("description") or ""),
                inputSchema=dict(entry.get("input_schema") or {"type": "object"}),
                annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
            )
            for entry in entries.values()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        entry = entries.get(name)
        if entry is None:
            raise PermissionError(f"{name} is not an allowed Fleet read tool")
        return await _forward(entry, arguments or {})

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def run_gateway() -> None:
    """Entrypoint for ``chats fleet read-mcp``."""

    asyncio.run(_serve())


def main() -> None:
    run_gateway()


if __name__ == "__main__":
    main()
