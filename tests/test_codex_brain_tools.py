from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.brain_work_tools import WORK_TOOLS
from core.codex_brain_tools import (
    CodexBrainToolRegistry,
    build_serena_codex_brain_tools,
)


def test_real_coding_tools_keep_their_existing_handlers_and_contracts() -> None:
    registry = CodexBrainToolRegistry(
        {"serena_work": ("Serena coding work.", WORK_TOOLS)}
    )

    assert registry.names() == [
        "serena_work.start_coding_work",
        "serena_work.coding_job_status",
        "serena_work.control_coding_work",
    ]
    namespace = registry.specs()[0]
    start = next(tool for tool in namespace["tools"] if tool["name"] == "start_coding_work")
    assert start["inputSchema"] == {
        "type": "object",
        "properties": {
            "request": {"type": "string"},
            "brief_json": {"type": "string"},
        },
        "required": ["request", "brief_json"],
        "additionalProperties": False,
    }
    assert "broker" in start["description"].lower()


def test_production_registry_exposes_every_serena_tool_group() -> None:
    registry = build_serena_codex_brain_tools()

    names = set(registry.names())
    assert {
        "serena_ro.recall_chats",
        "serena_laptop.laptop_action",
        "serena_work.start_coding_work",
        "serena_work.control_coding_work",
        "serena_memory.save_memory",
        "serena_documents.create_document",
        "serena_capabilities.use_pc_capability",
        "serena_fleet.start_fleet_run",
        "serena_gideon.gideon_status",
        "serena_gideon.gideon_commitments",
        "serena_gideon.gideon_device_scene",
    } <= names
    assert all(name.count(".") == 1 for name in names)


def test_registry_dispatches_and_converts_tool_results() -> None:
    async def scenario() -> None:
        received = None

        async def handler(arguments):
            nonlocal received
            received = arguments
            return {"content": [{"type": "text", "text": "DONE"}]}

        registry = CodexBrainToolRegistry(
            {
                "serena_test": (
                    "Test tools.",
                    [
                        SimpleNamespace(
                            name="act",
                            description="Act through the broker.",
                            input_schema={"count": int, "label": str},
                            handler=handler,
                        )
                    ],
                )
            }
        )
        result = await registry.call(
            namespace="serena_test",
            tool="act",
            arguments='{"count":2,"label":"now"}',
        )

        assert received == {"count": 2, "label": "now"}
        assert result == {
            "success": True,
            "contentItems": [{"type": "inputText", "text": "DONE"}],
        }

    asyncio.run(scenario())


def test_registry_returns_a_failed_tool_result_without_claiming_success() -> None:
    async def scenario() -> None:
        async def broken(_arguments):
            raise RuntimeError("broker refused")

        registry = CodexBrainToolRegistry(
            {
                "serena_test": (
                    "Test tools.",
                    [
                        SimpleNamespace(
                            name="act",
                            description="Act.",
                            input_schema={},
                            handler=broken,
                        )
                    ],
                )
            }
        )
        result = await registry.call(
            namespace="serena_test",
            tool="act",
            arguments={},
        )

        assert result["success"] is False
        assert "NOTHING WAS COMPLETED" in result["contentItems"][0]["text"]

    asyncio.run(scenario())
