from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from core import fleet_read_mcp as read_mcp


class _Annotations:
    def __init__(self, read_only=None, destructive=None):
        self.readOnlyHint = read_only
        self.destructiveHint = destructive


class _Tool:
    def __init__(self, name, annotations=None, description="", schema=None):
        self.name = name
        self.annotations = annotations
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _Result:
    def __init__(self, tools):
        self.tools = tools


class _Content:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _CallResult:
    def __init__(self, text, is_error=False):
        self.content = [_Content(text)]
        self.isError = is_error


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "google-ads,context7")
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_COMMAND", "/opt/bin/chats fleet read-mcp")
    return tmp_path


def _write_catalog(tmp_path, tools, *, generated_at=None):
    payload = {
        "version": read_mcp.CATALOG_VERSION,
        "generated_at": generated_at if generated_at is not None else time.time(),
        "servers": {},
        "tools": tools,
    }
    read_mcp.catalog_path().write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _entry(server, tool):
    return {
        "name": read_mcp.exposed_name(server, tool),
        "server": server,
        "tool": tool,
        "description": "",
        "input_schema": {"type": "object", "properties": {}},
    }


def test_classification_denies_everything_it_cannot_prove_is_a_read():
    assert read_mcp.classify("google-ads", "search_search") == "read"
    assert read_mcp.classify("google-ads", "add_keywords") == "write"
    # camelCase and acronyms are split, so a getter is not read as one opaque word
    assert read_mcp.classify("hostinger-dns", "DNS_getDNSRecordsV1") == "read"
    assert read_mcp.classify("hostinger-dns", "DNS_updateDNSRecordsV1") == "write"
    # an unclassifiable name is denied rather than assumed harmless
    assert read_mcp.classify("frameworth-shopify", "wibble") == "unknown"


def test_curated_decisions_outrank_a_servers_own_annotation():
    lying = _Annotations(read_only=True)
    assert read_mcp.classify("frameworth-shopify", "graphql", lying) == "write"
    assert read_mcp.classify("google-ads", "keyword_research", None) == "read"
    # a destructive hint beats a read hint from the same server
    assert read_mcp.classify("x", "get_thing", _Annotations(True, True)) == "write"


def test_credential_reads_are_refused_even_though_they_are_reads():
    assert read_mcp.classify("Railway", "list_variables") == "sensitive"
    assert read_mcp.classify("x", "get_secret_token") == "sensitive"


def test_exposed_name_stays_unambiguous_for_permission_rules():
    name = read_mcp.exposed_name("google-ads", "search_search")
    assert name == "read_google_ads_search_search"
    assert "__" not in name
    rule = f"mcp__{read_mcp.GATEWAY_SERVER_NAME}__{name}"
    assert rule.split("__") == ["mcp", "serena_read", "read_google_ads_search_search"]


def test_no_catalog_means_no_mcp_at_all(tmp_path):
    assert read_mcp.load_catalog() is None
    assert read_mcp.claude_flags("read_only") == []
    assert read_mcp.codex_flags("review") == []
    assert read_mcp.prompt_block("read_only") == ""


def test_writers_never_receive_the_gateway(tmp_path):
    _write_catalog(tmp_path, [_entry("google-ads", "search_search")])
    assert read_mcp.read_access_enabled("write") is False
    assert read_mcp.claude_flags("write") == []
    assert read_mcp.codex_flags("write") == []
    assert read_mcp.prompt_block("write") == ""


def test_read_legs_receive_an_explicit_per_tool_allowlist(tmp_path):
    _write_catalog(
        tmp_path,
        [_entry("google-ads", "search_search"), _entry("context7", "query-docs")],
    )

    claude = read_mcp.claude_flags("read_only")
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    assert list(config["mcpServers"]) == ["serena_read"]
    assert config["mcpServers"]["serena_read"]["command"] == "/opt/bin/chats"
    allowed = claude[claude.index("--allowedTools") + 1].split(",")
    assert allowed == [
        "mcp__serena_read__read_context7_query-docs",
        "mcp__serena_read__read_google_ads_search_search",
    ]

    codex = read_mcp.codex_flags("review")
    joined = " ".join(codex)
    assert "mcp_servers.serena_read.command=" in joined
    enabled = json.loads(
        next(value for value in codex if value.startswith("mcp_servers.serena_read.enabled_tools="))
        .split("=", 1)[1]
    )
    assert enabled == ["read_context7_query-docs", "read_google_ads_search_search"]


def test_a_server_dropped_from_config_disappears_without_a_rebuild(tmp_path, monkeypatch):
    _write_catalog(
        tmp_path,
        [_entry("google-ads", "search_search"), _entry("context7", "query-docs")],
    )
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "context7")
    assert [tool["tool"] for tool in read_mcp.catalog_tools()] == ["query-docs"]
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "none")
    assert read_mcp.catalog_tools() == []
    assert read_mcp.claude_flags("read_only") == []


def test_prompt_block_tells_the_worker_what_it_can_actually_reach(tmp_path):
    _write_catalog(tmp_path, [_entry("google-ads", "search_search")])
    block = read_mcp.prompt_block("read_only")
    assert "serena_read" in block
    assert "google-ads: read_google_ads_search_search" in block
    assert "read-only" in block


def test_catalog_refresh_is_skipped_while_current_and_forced_when_stale(tmp_path, monkeypatch):
    calls: list[int] = []

    async def fake_build():
        calls.append(1)
        return {
            "version": read_mcp.CATALOG_VERSION,
            "generated_at": time.time(),
            "servers": {"context7": {"status": "ok", "exposed": 1, "denied": 0}},
            "tools": [_entry("context7", "query-docs")],
        }

    monkeypatch.setattr(read_mcp, "build_catalog", fake_build)

    assert read_mcp.refresh_catalog()["refreshed"] is True
    assert read_mcp.refresh_catalog()["refreshed"] is False
    assert len(calls) == 1

    stale = read_mcp.load_catalog()
    stale["generated_at"] = time.time() - (read_mcp.CATALOG_TTL_SECONDS + 60)
    read_mcp.catalog_path().write_text(json.dumps(stale), encoding="utf-8")
    assert read_mcp.refresh_catalog()["refreshed"] is True
    assert len(calls) == 2


def test_refresh_never_raises_when_a_server_is_unreachable(tmp_path, monkeypatch):
    async def explode():
        raise ConnectionError("tailnet is down")

    monkeypatch.setattr(read_mcp, "build_catalog", explode)
    summary = read_mcp.refresh_catalog(force=True)
    assert summary["refreshed"] is False
    assert "ConnectionError" in summary["reason"]
    assert read_mcp.load_catalog() is None


def test_build_catalog_records_denials_and_exposes_only_reads(monkeypatch):
    servers = [
        {"name": "google-ads", "transport": "http", "url": "https://example.test/mcp"},
        {"name": "context7", "transport": "http", "url": "https://example.test/c7"},
    ]
    monkeypatch.setattr(read_mcp, "_server_records", lambda: servers)

    async def fake_list(server):
        if server["name"] == "context7":
            raise TimeoutError("no route")
        return [
            _Tool("search_search"),
            _Tool("add_keywords"),
            _Tool("wibble"),
        ]

    monkeypatch.setattr(read_mcp, "_list_upstream_tools", fake_list)
    catalog = asyncio.run(read_mcp.build_catalog())
    assert [tool["tool"] for tool in catalog["tools"]] == ["search_search"]
    assert catalog["servers"]["google-ads"] == {
        "transport": "http",
        "status": "ok",
        "error": None,
        "exposed": 1,
        "denied": 2,
    }
    assert catalog["servers"]["context7"]["status"] == "unreachable"


def test_forward_refuses_a_tool_that_no_longer_classifies_as_a_read(monkeypatch):
    class _Session:
        async def list_tools(self):
            return _Result([_Tool("search_search", _Annotations(read_only=False))])

        async def call_tool(self, name, arguments):  # pragma: no cover - must not run
            raise AssertionError("a write-classified tool must never be forwarded")

    @asynccontextmanager
    async def fake_session(server):
        yield _Session()

    monkeypatch.setattr(read_mcp, "_upstream_session", fake_session)
    monkeypatch.setattr(
        "core.mcp.config.get_server",
        lambda name: {"name": name, "transport": "http", "url": "https://example.test/mcp"},
    )
    entry = _entry("google-ads", "search_search")
    with pytest.raises(PermissionError, match="classifies as write"):
        asyncio.run(read_mcp._forward(entry, {}))


def test_forward_truncates_an_oversized_upstream_result(monkeypatch):
    class _Session:
        async def list_tools(self):
            return _Result([_Tool("search_search")])

        async def call_tool(self, name, arguments):
            return _CallResult("x" * (read_mcp.MAX_RESULT_CHARS * 2))

    @asynccontextmanager
    async def fake_session(server):
        yield _Session()

    monkeypatch.setattr(read_mcp, "_upstream_session", fake_session)
    monkeypatch.setattr(
        "core.mcp.config.get_server",
        lambda name: {"name": name, "transport": "http", "url": "https://example.test/mcp"},
    )
    result = asyncio.run(read_mcp._forward(_entry("google-ads", "search_search"), {}))
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "truncated" in text
    assert len(text) < read_mcp.MAX_RESULT_CHARS + 200


def test_forward_refuses_a_server_the_config_no_longer_allows(monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "context7")
    with pytest.raises(PermissionError, match="not available"):
        asyncio.run(read_mcp._forward(_entry("google-ads", "search_search"), {}))
