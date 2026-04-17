"""Tool framework for Serena's brain.

Defines the tool interface and registry that bridges Claude's tool_use
capability with concrete tool implementations.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Tool(Protocol):
    """Interface every Serena tool must implement."""

    @property
    def name(self) -> str:
        """Tool name as Claude sees it (e.g. 'web_search')."""
        ...

    @property
    def description(self) -> str:
        """What the tool does — Claude reads this to decide when to call it."""
        ...

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema describing the tool's input parameters."""
        ...

    async def execute(self, **kwargs: Any) -> str:
        """Run the tool and return a plain-text result string."""
        ...


class ToolRegistry:
    """Central registry of all available tools.

    Holds tool instances, converts them to the Anthropic API tool format,
    and dispatches execution by name.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool instance. Raises on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic API format.

        Each entry has 'name', 'description', and 'input_schema' keys,
        ready to pass directly to ``messages.create(tools=...)``.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Look up a tool by name and execute it.

        Returns:
            The tool's string result, or an error message if the tool
            is not found or execution fails.
        """
        tool = self._tools.get(name)
        if tool is None:
            msg = f"Unknown tool: {name}"
            logger.error(msg)
            return msg

        try:
            result = await tool.execute(**arguments)
            logger.info("Tool '%s' executed successfully", name)
            return result
        except Exception:
            logger.exception("Tool '%s' failed", name)
            return f"Tool '{name}' encountered an error. Check logs for details."
