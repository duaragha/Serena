"""Focused tests for Serena's on-demand PC MCP access."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from core import brain_capability_tools, brain_daemon
from core.mcp import capability_broker


def _capability(
    *,
    server: str = "context7",
    tool: str = "query-docs",
    access: str = "read",
) -> capability_broker.Capability:
    return capability_broker.Capability(
        server=server,
        tool=tool,
        description=f"Use {tool} for live information",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        access=access,
    )


def test_discovery_returns_matching_schema_without_connection_secrets(monkeypatch) -> None:
    """Discovery must never put URLs, headers, or credentials into model context."""

    async def fake_catalog(*, refresh=False):
        del refresh
        return (
            _capability(),
            _capability(server="Railway", tool="list_projects"),
        ), ()

    monkeypatch.setattr(capability_broker, "catalog", fake_catalog)
    result = asyncio.run(capability_broker.find_capabilities("package documentation"))
    encoded = json.dumps(result)

    assert result["matches"][0]["server"] == "context7"
    assert result["matches"][0]["input_schema"]["properties"]["query"]
    assert "url" not in encoded.lower()
    assert "header" not in encoded.lower()
    assert "token" not in encoded.lower()


def test_bearer_secret_is_resolved_only_at_connection_time(monkeypatch) -> None:
    """A bearer token must stay in the secret store instead of MCP catalog JSON."""

    monkeypatch.setattr(
        capability_broker,
        "get_secret",
        lambda server, name: "private-token" if (server, name) == ("beeper", "BEEPER_ACCESS_TOKEN") else None,
    )
    server = {
        "name": "beeper",
        "headers": {},
        "secrets": ["BEEPER_ACCESS_TOKEN"],
    }

    assert capability_broker._resolved_headers(server) == {
        "Authorization": "Bearer private-token"
    }
    assert "private-token" not in json.dumps(server)


def test_read_capability_runs_without_repeating_context_or_confirmation(monkeypatch) -> None:
    """A normal read must run directly from the current request."""

    async def fake_catalog(*, refresh=False):
        del refresh
        return (_capability(),), ()

    async def fake_session(_server, operation):
        class Session:
            async def call_tool(self, name, arguments):
                assert name == "query-docs"
                assert arguments == {"query": "httpx timeouts"}
                return SimpleNamespace(
                    isError=False,
                    model_dump=lambda **_kwargs: {"content": [{"type": "text", "text": "answer"}]},
                )

        return await operation(Session())

    monkeypatch.setattr(capability_broker, "catalog", fake_catalog)
    monkeypatch.setattr(
        capability_broker,
        "_configured_http_servers",
        lambda: [{"name": "context7", "transport": "http", "url": "https://private.invalid"}],
    )
    monkeypatch.setattr(capability_broker, "_with_session", fake_session)

    result = asyncio.run(
        capability_broker.invoke_capability(
            "context7",
            "query-docs",
            {"query": "httpx timeouts"},
            origin={"text": "what are httpx's timeout rules?"},
        )
    )

    assert result["ok"] is True
    assert result["called"] is True
    assert result["access"] == "read"
    assert "answer" in result["result"]


def test_write_capability_is_not_called_without_fresh_direct_authority(monkeypatch) -> None:
    """A model-selected write must not become authority to change an external service."""

    called = False

    async def fake_catalog(*, refresh=False):
        del refresh
        return (_capability(server="Railway", tool="deploy", access="write"),), ()

    async def fake_session(_server, _operation):
        nonlocal called
        called = True
        raise AssertionError("blocked writes must not reach the MCP")

    monkeypatch.setattr(capability_broker, "catalog", fake_catalog)
    monkeypatch.setattr(capability_broker, "_with_session", fake_session)

    result = asyncio.run(
        capability_broker.invoke_capability(
            "Railway",
            "deploy",
            {"project": "locket"},
            origin={"text": "how is locket doing?"},
        )
    )

    assert result == {
        "ok": False,
        "called": False,
        "error": "the current user turn did not directly authorize this write",
    }
    assert called is False


def test_explicit_current_turn_can_authorize_the_matching_write(monkeypatch) -> None:
    """The broker must use the ask itself instead of forcing a second permission loop."""

    async def fake_catalog(*, refresh=False):
        del refresh
        return (_capability(server="Railway", tool="deploy", access="write"),), ()

    async def fake_session(_server, operation):
        class Session:
            async def call_tool(self, name, arguments):
                assert (name, arguments) == ("deploy", {"project": "locket"})
                return SimpleNamespace(
                    isError=False,
                    model_dump=lambda **_kwargs: {"content": [{"type": "text", "text": "deployed"}]},
                )

        return await operation(Session())

    monkeypatch.setattr(capability_broker, "catalog", fake_catalog)
    monkeypatch.setattr(
        capability_broker,
        "_configured_http_servers",
        lambda: [{"name": "Railway", "transport": "http", "url": "https://private.invalid"}],
    )
    monkeypatch.setattr(capability_broker, "_with_session", fake_session)

    result = asyncio.run(
        capability_broker.invoke_capability(
            "Railway",
            "deploy",
            {"project": "locket"},
            origin={"text": "deploy the locket project on Railway"},
        )
    )

    assert result["ok"] is True
    assert result["called"] is True
    assert result["access"] == "write"


def test_unrelated_write_verb_cannot_authorize_a_different_mutation(monkeypatch) -> None:
    """A subject match must not turn a report request into delete authority."""

    item = _capability(server="Railway", tool="delete_project", access="write")
    assert capability_broker._write_is_authorized(
        item,
        {"project": "locket"},
        {"text": "make me a report about the locket project"},
    ) is False


def test_beeper_send_is_treated_as_a_write_even_without_annotations() -> None:
    """Beeper message mutations must never inherit read-only treatment by accident."""

    item = _capability(server="beeper", tool="send_message", access="unknown")
    assert capability_broker._dynamic_access(item, {"text": "hello"}) == "write"


def test_resident_options_mount_only_the_small_capability_surface(monkeypatch, tmp_path) -> None:
    """Connecting every MCP must not mount every underlying schema into the brain."""

    class CapturedOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", tmp_path / "prompt.md")
    server = brain_capability_tools.capability_tools_server()
    options = brain_daemon._build_agent_options(
        CapturedOptions,
        {},
        [],
        capability_tools=server,
        capability_tool_names=brain_capability_tools.CAPABILITY_TOOL_NAMES,
    )

    assert set(options.mcp_servers) == {"serena-ro", "serena-capabilities"}
    assert options.allowed_tools == [
        "mcp__serena-capabilities__find_pc_capability",
        "mcp__serena-capabilities__use_pc_capability",
    ]
    assert all("context7" not in item for item in options.allowed_tools)
    assert all("Railway" not in item for item in options.allowed_tools)
