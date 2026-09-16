"""Explicit-only Muse worker contract for Serena Fleet.

Muse is Muse Code (Meta Muse Spark), the CLI in this very session. It joins
Fleet the way the Gemini research adapter did: explicit-only, never part of
the automatic or balanced pipeline. A run uses Muse only through
``provider_mode="muse"`` or a muse-only task directive, and handoffs may move
a worker to or from Muse through the normal escape-hatch stack. Nothing
silently substitutes Muse for another provider.

CLI surface this contract is pinned to (``muse exec --help``, 1.3.0):

* ``muse exec`` runs one prompt headlessly. The prompt is a positional
  argument or ``--prompt-file``; stdin is NOT read, so the worker argv must
  carry the prompt itself.
* ``--json`` emits machine-readable JSONL events on stdout.
* ``--model`` selects the Meta model id. When Serena's own identity
  (``muse-spark``) is requested the flag is omitted and the CLI default
  applies, so Serena never invents a Meta catalog id.
* ``--reasoning-effort`` accepts none|minimal|low|medium|high|xhigh|max|ultra,
  which covers Serena's low|medium|high|xhigh|max effort range directly.
* ``--workspace PATH`` roots policy-gated workspace tools at the checkout.
* ``--session-id`` fixes the transcript identity for the leg.
* ``--approval-mode never`` plus ``--disable-write`` on non-writing legs
  bound authority the way Fleet's Codex/Claude isolation flags do. ``--yolo``
  is never used here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODEL = "muse-spark"
EFFORT = "high"

_SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId")
_MODEL_KEYS = ("model", "model_id", "modelId")
_EFFORT_KEYS = ("reasoning_effort", "reasoningEffort", "effort")
_TEXT_KEYS = ("result", "response", "output", "text", "answer")


def _search_scopes(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the event plus one level of common envelope dicts to inspect."""

    scopes = [event]
    for key in ("message", "payload", "item", "data"):
        nested = event.get(key)
        if isinstance(nested, dict):
            scopes.append(nested)
    return scopes


def _first_text(value: object, *, depth: int = 0) -> str:
    """Extract assistant-shaped text from one event scope, best effort."""

    if depth > 2:
        return ""
    if isinstance(value, dict):
        # Claude-style content blocks and Codex-style agent messages first,
        # since those carry the actual answer rather than status labels.
        if value.get("type") == "agent_message":
            text = value.get("text")
            return str(text).strip() if isinstance(text, str) and text.strip() else ""
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        for key in _TEXT_KEYS:
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                # Skip echoing back structured blobs under text-ish keys.
                stripped = text.strip()
                if not (stripped.startswith("{") and stripped.endswith("}")):
                    return stripped
        return ""
    return ""


@dataclass
class MuseStream:
    """Tolerant fold over ``muse exec --json`` event lines.

    A zero exit is not completion: missing output, an explicit error marker,
    or a session-identity contradiction fails closed, mirroring the Gemini
    adapter's contract.
    """

    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    output: str = ""
    status: str = ""
    error: str = ""
    initialized: bool = False
    texts: list[str] = field(default_factory=list)

    def accept(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        self.initialized = True
        if self.error:
            return
        kind = str(event.get("type") or event.get("event") or "")
        if kind == "error":
            detail = event.get("error") or event.get("message")
            self.error = str(detail).strip() or "Muse reported an error"
            return
        if event.get("is_error") is True:
            detail = (
                event.get("result")
                or event.get("error")
                or event.get("message")
                or "Muse reported an error"
            )
            self.error = str(detail).strip() or "Muse reported an error"
            return
        status = event.get("status")
        if isinstance(status, str) and status:
            self.status = status
            # Only fail on an explicit failure status, never on an
            # unfamiliar progress label from a newer CLI.
            if status.upper() in {"FAILED", "FAILURE", "ERROR", "CANCELLED", "WAITING"}:
                self.error = str(event.get("error") or f"Muse ended with {status}").strip()
                return
        for scope in _search_scopes(event):
            if self.session_id is None:
                for key in _SESSION_KEYS:
                    value = scope.get(key)
                    if isinstance(value, str) and value.strip():
                        self.session_id = value.strip()
                        break
            if self.model is None:
                for key in _MODEL_KEYS:
                    value = scope.get(key)
                    if isinstance(value, str) and value.strip():
                        self.model = value.strip()
                        break
            if self.effort is None:
                for key in _EFFORT_KEYS:
                    value = scope.get(key)
                    if isinstance(value, str) and value.strip():
                        self.effort = value.strip()
                        break
        text = ""
        for scope in _search_scopes(event):
            text = _first_text(scope)
            if text:
                break
        if text and (not self.texts or self.texts[-1] != text):
            self.texts.append(text)
            self.output = text

    def completion_error(self, exit_code: int) -> str | None:
        if self.error:
            return self.error
        if exit_code:
            return f"Muse exited with status {exit_code}"
        if not self.initialized or not self.output:
            return "Muse completed without a successful final response"
        return None
