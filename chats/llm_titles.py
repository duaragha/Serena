"""LLM-based session title generation using the claude CLI in headless mode.

Batches multiple sessions into one LLM call to amortize startup cost —
`claude -p` takes ~8-12s per invocation regardless of prompt size, so
doing one call for 10 sessions is ~10× faster than one-at-a-time.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Iterable


BATCH_PROMPT_HEAD = """You are generating concise titles for Claude Code conversations.

For each numbered item below, generate a title that:
- Is 3 to 6 words
- Uses Title Case
- Captures the SPECIFIC topic (not generic like "Code Session")
- Omits filler verbs like "Can you", "How do I", "I want to"
- Prefers nouns/objects over verbs when possible
- Does NOT include quotes, emojis, or punctuation at the end

Return ONLY a valid JSON object mapping each id to its title. No prose, no
code fences, no comments.

Example output format:
{"abc12345": "Fix Syncthing Watcher Limits", "def67890": "Resume Button Terminal Launch"}

Items:
"""


def _snippet(item: dict, max_user: int = 700, max_asst: int = 350) -> str:
    sid = item["id"]
    user = (item.get("first_message") or "").strip()
    asst = (item.get("first_response") or "").strip()
    user = user[:max_user]
    asst = asst[:max_asst]
    out = f"\n--- {sid} ---\nUser: {user}\n"
    if asst:
        out += f"Claude: {asst}\n"
    return out


def _parse_response(output: str) -> dict[str, str]:
    if not output:
        return {}
    # Strip common wrappers
    output = output.strip()
    # Remove markdown fences
    output = re.sub(r"^```(?:json)?\s*|\s*```$", "", output, flags=re.DOTALL).strip()
    # Find the first { and matching }
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    blob = output[start : end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(v, str):
            continue
        title = v.strip().strip('"').strip("'").rstrip(".,;:!?")
        if not title:
            continue
        # Cap at a reasonable length
        if len(title) > 70:
            title = title[:70].rsplit(" ", 1)[0]
        cleaned[str(k)] = title
    return cleaned


def generate_titles_batch(
    items: Iterable[dict],
    model: str = "haiku",
    timeout: int = 120,
) -> dict[str, str]:
    """Generate titles for a batch of sessions.

    items: iterable of dicts with keys 'id', 'first_message', and optional 'first_response'.
    Returns: {id: title}. Returns empty dict on failure.
    """
    items = list(items)
    if not items:
        return {}
    claude = shutil.which("claude")
    if not claude:
        return {}

    prompt = BATCH_PROMPT_HEAD + "".join(_snippet(it) for it in items) + "\n\nReturn the JSON object:"

    try:
        result = subprocess.run(
            [claude, "-p", "--model", model, "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {}
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    return _parse_response(result.stdout)
