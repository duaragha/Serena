"""Tool registration — wraps data tools into the Tool protocol and registers all tools."""

from __future__ import annotations

import json
from typing import Any

from serena.brain.tools import ToolRegistry
from serena.config import SerenaConfig
from serena.tools.code import CodeTool
from serena.tools.launcher import LauncherTool
from serena.tools.search import SearchTool
from serena.tools.system import SystemTool
from serena.tools.weather import WeatherTool
from serena.tools.news import NewsTool


class WeatherToolAdapter:
    """Adapts WeatherTool to the Tool protocol for Claude."""

    def __init__(self, config: SerenaConfig) -> None:
        self._tool = WeatherTool(config.weather)

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return (
            "Get current weather conditions and forecast. "
            "Use this when the user asks about the weather, temperature, or forecast."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["current", "forecast", "summary"],
                    "description": "What weather info to get. 'summary' gives a voice-friendly overview.",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of forecast days (only used with 'forecast' action).",
                    "default": 3,
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str = "summary", days: int = 3, **_: Any) -> str:
        if action == "current":
            data = await self._tool.get_current()
            return json.dumps(data, default=str)
        elif action == "forecast":
            data = await self._tool.get_forecast(days=days)
            return json.dumps(data, default=str)
        else:
            return await self._tool.get_summary()


class NewsToolAdapter:
    """Adapts NewsTool to the Tool protocol for Claude."""

    def __init__(self) -> None:
        self._tool = NewsTool()

    @property
    def name(self) -> str:
        return "news"

    @property
    def description(self) -> str:
        return (
            "Get the latest news headlines from major sources. "
            "Use this when the user asks about news, current events, or what's happening in the world."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of headlines to return.",
                    "default": 5,
                },
            },
        }

    async def execute(self, limit: int = 5, **_: Any) -> str:
        return await self._tool.get_summary()


class SystemToolAdapter:
    """Adapts SystemTool to the Tool protocol for Claude."""

    def __init__(self) -> None:
        self._tool = SystemTool()

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return (
            "Get system status information — CPU usage, memory, disk, battery, running processes. "
            "Use when the user asks about their computer's performance or status."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "processes", "summary"],
                    "description": "What system info to get. 'summary' for voice-friendly overview.",
                    "default": "summary",
                },
            },
        }

    async def execute(self, action: str = "summary", **_: Any) -> str:
        if action == "status":
            data = self._tool.get_status()
            return json.dumps(data, default=str)
        elif action == "processes":
            data = self._tool.get_processes()
            return json.dumps(data, default=str)
        else:
            return self._tool.get_summary()


def create_tool_registry(config: SerenaConfig) -> tuple[ToolRegistry, CodeTool]:
    """Create and populate the tool registry with all available tools.

    Returns:
        A tuple of (registry, code_tool).  The caller must wire up narration
        on code_tool via ``code_tool.set_narration(brain, tts, ipc)`` once
        those dependencies are available.
    """
    registry = ToolRegistry()

    registry.register(WeatherToolAdapter(config))
    registry.register(NewsToolAdapter())
    registry.register(SystemToolAdapter())
    registry.register(SearchTool())
    registry.register(LauncherTool())

    code_tool = CodeTool()
    registry.register(code_tool)

    return registry, code_tool
