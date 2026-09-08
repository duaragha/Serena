"""Antigravity stream contract for the opt-in Gemini research adapter.

Kept separate from default routing. A CLI exit of zero is not completion:
WAITING, missing results, model drift and unsafe tool exposure fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

MODEL = "gemini-3.8-flash-high"
AGENT = "serena-fleet-research"
READ_TOOLS = frozenset({"view_file", "list_dir", "find_by_name", "grep_search",
                        "search_web", "read_url_content", "finish"})


@dataclass
class GeminiStream:
    session_id: str | None = None
    model: str | None = None
    output: str = ""
    status: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    completed_steps: set[int] = field(default_factory=set)
    tool_steps: set[int] = field(default_factory=set)
    initialized: bool = False

    def accept(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        payload = event.get(str(kind))
        if not isinstance(payload, dict):
            return
        if kind == "init":
            self.initialized = True
            self.session_id = str(event.get("conversation_id") or "") or None
            self.model = str(payload.get("model") or "") or None
            # agy 1.1.27 reports the global tool catalog here, not the custom
            # agent's filtered tools. Verify the selected, pinned definition.
            if payload.get("agent") != AGENT:
                self.error = "Gemini did not select the Fleet read-only agent"
            if self.model != MODEL:
                self.error = "Gemini model identity does not match the pinned Flash high model"
        elif kind == "step_update":
            if payload.get("step_type") == "tool" and payload.get("tool_name") not in READ_TOOLS | {"manage_task"}:
                self.error = "Gemini attempted a tool outside the Fleet read-only boundary"
            index = payload.get("step_index")
            if type(index) is int and payload.get("state") == "DONE":
                if payload.get("step_type") != "user_input":
                    self.completed_steps.add(index)
                if payload.get("step_type") == "tool":
                    self.tool_steps.add(index)
        elif kind == "result":
            self.status = str(payload.get("status") or "")
            self.output = str(payload.get("response") or "").strip()
            self.usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            sid = str(payload.get("conversation_id") or "")
            if sid and self.session_id and sid != self.session_id:
                self.error = "Gemini result belongs to a different conversation"
            if self.status != "SUCCESS":
                self.error = self.error or str(payload.get("error") or f"Gemini ended with {self.status}")

    def completion_error(self, exit_code: int) -> str | None:
        if self.error:
            return self.error
        if exit_code:
            return f"Gemini exited with status {exit_code}"
        if not self.initialized or not self.session_id or self.model != MODEL:
            return "Gemini did not establish a verified session"
        if self.status != "SUCCESS" or not self.output:
            return "Gemini completed without a successful final response"
        return None


def verify_agent(cwd: str) -> None:
    """Never overwrite user config or accept a workspace shadow definition."""
    template = Path(__file__).with_name("gemini_research_agent.md").read_bytes()
    installed = Path.home() / ".gemini/config/agents" / AGENT / "agent.md"
    if not installed.is_file() or installed.read_bytes() != template:
        raise ValueError(f"Install the exact Fleet research agent template at {installed}")
    for parent in (Path(cwd).resolve(), *Path(cwd).resolve().parents):
        root = parent / ".agents/agents"
        if (root / f"{AGENT}.md").exists() or (root / AGENT).exists():
            raise ValueError("Workspace definition shadows the Fleet research agent")
