"""One model call for the journal, that survives a dead login.

The first live run on the PC lost everything the chats could say -- who he
spent Sunday with -- because the PC's Claude login had expired ("OAuth
session expired and could not be refreshed") and the extraction had nowhere
else to go. Her brain rides out the same failure on its Codex fallback; this
gives the journal the same second path, so an expired login costs a draft its
polish, not its facts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile

CLAUDE_MODEL = "sonnet"
CODEX_TIMEOUT_SECONDS = 300


class ModelUnavailable(RuntimeError):
    """Neither provider answered."""


async def _claude(prompt: str, system: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(model=CLAUDE_MODEL, tools=[], allowed_tools=[], setting_sources=[],
                                 max_turns=1, system_prompt=system)
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            chunks.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage) and message.is_error:
            raise ModelUnavailable(f"claude: {message.result}")
    return "".join(chunks).strip()


def _codex(prompt: str, system: str) -> str:
    codex = shutil.which("codex") or shutil.which("codex.cmd")
    if not codex:
        raise ModelUnavailable("codex is not installed on this machine")
    with tempfile.TemporaryDirectory() as scratch:
        done = subprocess.run(
            [codex, "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", scratch,
             f"{system}\n\n{prompt}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CODEX_TIMEOUT_SECONDS, check=False)
    last = ""
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            last = str(item.get("text") or "")
    if not last:
        raise ModelUnavailable(f"codex returned nothing (exit {done.returncode}): {done.stderr[-300:]}")
    return last.strip()


def ask(prompt: str, *, system: str) -> str:
    """Claude first; Codex when Claude cannot answer at all."""

    try:
        text = asyncio.run(_claude(prompt, system))
        if text:
            return text
    except Exception:
        pass
    return _codex(prompt, system)
