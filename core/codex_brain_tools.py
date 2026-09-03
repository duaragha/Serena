"""Expose Serena's existing brokered tools to the Codex brain runtime."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class CodexBrainToolError(RuntimeError):
    """The dynamic tool contract or invocation is invalid."""


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    namespace: str
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Any


class CodexBrainToolRegistry:
    """Translate SDK tool objects into app-server dynamic tools."""

    def __init__(
        self,
        groups: Mapping[str, tuple[str, Iterable[Any]]],
    ) -> None:
        self._groups: dict[str, tuple[str, list[_RegisteredTool]]] = {}
        self._tools: dict[tuple[str, str], _RegisteredTool] = {}
        for namespace, (description, tools) in groups.items():
            clean_namespace = _clean_name(namespace)
            registered: list[_RegisteredTool] = []
            for item in tools:
                name = _clean_name(str(getattr(item, "name", "")))
                key = (clean_namespace, name)
                if key in self._tools:
                    raise CodexBrainToolError(
                        f"duplicate Codex brain tool {clean_namespace}.{name}"
                    )
                handler = getattr(item, "handler", None)
                if not callable(handler):
                    raise CodexBrainToolError(
                        f"Codex brain tool {clean_namespace}.{name} has no handler"
                    )
                tool = _RegisteredTool(
                    namespace=clean_namespace,
                    name=name,
                    description=str(getattr(item, "description", "") or ""),
                    input_schema=_json_schema(getattr(item, "input_schema", {})),
                    handler=handler,
                )
                self._tools[key] = tool
                registered.append(tool)
            self._groups[clean_namespace] = (str(description), registered)
        encoded = json.dumps(
            self.specs(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.contract_id = hashlib.sha256(encoded).hexdigest()[:20]

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "namespace",
                "name": namespace,
                "description": description,
                "tools": [
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                    for tool in tools
                ],
            }
            for namespace, (description, tools) in self._groups.items()
        ]

    async def call(
        self,
        *,
        namespace: object,
        tool: object,
        arguments: object,
    ) -> dict[str, Any]:
        clean_namespace = _clean_name(str(namespace or ""))
        clean_tool = _clean_name(str(tool or ""))
        registered = self._tools.get((clean_namespace, clean_tool))
        if registered is None:
            return _dynamic_result(
                False,
                f"TOOL NOT FOUND. {clean_namespace}.{clean_tool} is unavailable.",
            )
        try:
            values = _arguments(arguments)
            result = registered.handler(values)
            if inspect.isawaitable(result):
                result = await result
            return _handler_result(result)
        except Exception as exc:
            return _dynamic_result(
                False,
                "TOOL FAILED. NOTHING WAS COMPLETED. "
                f"Reason: {type(exc).__name__}: {exc}",
            )

    def names(self) -> list[str]:
        return [f"{namespace}.{name}" for namespace, name in self._tools]


def _clean_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character == "_" else "_" for character in value)
    cleaned = cleaned.strip("_")
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        raise CodexBrainToolError(f"invalid dynamic tool name {value!r}")
    return cleaned


def _json_schema(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping) and raw.get("type") == "object":
        return json.loads(json.dumps(raw))
    if not isinstance(raw, Mapping):
        raise CodexBrainToolError("tool input schema must be an object")
    properties = {
        str(name): _type_schema(value)
        for name, value in raw.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _type_schema(value: object) -> dict[str, Any]:
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: {"type": "object"},
        list: {"type": "array"},
    }.get(value, {"type": "string"})


def _arguments(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CodexBrainToolError("tool arguments are not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise CodexBrainToolError("tool arguments must be an object")
    return dict(value)


def _handler_result(result: object) -> dict[str, Any]:
    content = result.get("content") if isinstance(result, Mapping) else None
    texts: list[str] = []
    for item in content or []:
        text = (
            item.get("text")
            if isinstance(item, Mapping)
            else getattr(item, "text", None)
        )
        if text is not None:
            texts.append(str(text))
    if not texts:
        try:
            texts.append(json.dumps(result, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            texts.append(str(result))
    return {
        "success": True,
        "contentItems": [
            {"type": "inputText", "text": text}
            for text in texts
        ],
    }


def _dynamic_result(success: bool, text: str) -> dict[str, Any]:
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": text}],
    }


def build_serena_codex_brain_tools() -> CodexBrainToolRegistry:
    """Build the exact brokered tool surface used by the resident brain."""

    from core.brain_capability_tools import CAPABILITY_TOOLS
    from core.brain_document_tools import DOCUMENT_TOOLS
    from core.brain_fleet_tools import FLEET_TOOLS
    from core.brain_gideon_tools import GIDEON_TOOLS
    from core.brain_laptop_tools import LAPTOP_TOOLS
    from core.brain_memory_tools import MEMORY_TOOLS
    from core.brain_tools import BRAIN_TOOLS
    from core.brain_work_tools import WORK_TOOLS

    return CodexBrainToolRegistry(
        {
            "serena_ro": ("Read Serena's local recall and repository state.", BRAIN_TOOLS),
            "serena_laptop": ("Use Serena's brokered laptop controls.", LAPTOP_TOOLS),
            "serena_work": ("Start, inspect, and control Serena coding jobs.", WORK_TOOLS),
            "serena_memory": (
                "Write Serena memories and knowledge when authorized.",
                MEMORY_TOOLS,
            ),
            "serena_documents": ("Create and deliver Serena documents.", DOCUMENT_TOOLS),
            "serena_capabilities": (
                "Discover and use Serena's brokered computer capabilities.",
                CAPABILITY_TOOLS,
            ),
            "serena_fleet": ("Start, inspect, and control Serena Fleet runs.", FLEET_TOOLS),
            "serena_gideon": (
                "Use Serena's continuity, commitments, state, world, device, visual, and support APIs.",
                GIDEON_TOOLS,
            ),
        }
    )
