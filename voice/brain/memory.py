"""Memory integration bridging Serena to the chats CLI.

Provides async access to the persistent memory system, conversation
search, and knowledge base via subprocess calls to the chats CLI tool.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)

CHATS_PATH = "/home/raghav/.local/bin/chats"

VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference", "general"}


async def _run_chats(*args: str) -> str:
    """Execute a chats CLI command and return its stdout.

    Returns an empty string on any error (missing binary, non-zero exit,
    timeout) and logs a warning rather than raising.
    """
    chats_bin = shutil.which("chats") or CHATS_PATH

    try:
        proc = await asyncio.create_subprocess_exec(
            chats_bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except FileNotFoundError:
        logger.warning("chats CLI not found at %s — memory features unavailable", chats_bin)
        return ""
    except asyncio.TimeoutError:
        logger.warning("chats command timed out: chats %s", " ".join(args))
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except ProcessLookupError:
            pass
        return ""
    except OSError:
        logger.exception("Failed to run chats CLI")
        return ""

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "chats %s exited with code %d: %s",
            " ".join(args),
            proc.returncode,
            stderr_text or "(no stderr)",
        )
        return ""

    return stdout.decode("utf-8", errors="replace").strip()


class MemoryManager:
    """Bridge to the chats CLI for persistent memory, search, and knowledge.

    All methods are async and call the chats binary via subprocess.
    Errors are handled gracefully — if the CLI is unavailable, methods
    return empty strings and log a warning.
    """

    async def load_memories(self) -> str:
        """Load all persistent memories.

        Equivalent to running `chats memory` on the command line.

        Returns:
            The memory output as a string, or empty string on failure.
        """
        result = await _run_chats("memory")
        if result:
            logger.debug("Loaded %d characters of memory data", len(result))
        return result

    async def search(self, query: str) -> str:
        """Search past conversations for context.

        Equivalent to running `chats search "<query>"`.

        Args:
            query: The search query string.

        Returns:
            Search results as a string, or empty string on failure.
        """
        if not query.strip():
            logger.warning("Empty search query, skipping")
            return ""

        result = await _run_chats("search", query)
        if result:
            logger.debug("Search for %r returned %d characters", query, len(result))
        return result

    async def add_memory(self, content: str, memory_type: str = "general") -> str:
        """Save a new memory via the chats CLI.

        Equivalent to running `chats memory add "<content>" --type <type>`.

        Args:
            content: The memory content to save.
            memory_type: One of: user, feedback, project, reference, general.

        Returns:
            CLI output confirming the save, or empty string on failure.
        """
        if not content.strip():
            logger.warning("Empty memory content, skipping")
            return ""

        if memory_type not in VALID_MEMORY_TYPES:
            logger.warning(
                "Invalid memory type %r, falling back to 'general'. Valid types: %s",
                memory_type,
                ", ".join(sorted(VALID_MEMORY_TYPES)),
            )
            memory_type = "general"

        result = await _run_chats("memory", "add", content, "--type", memory_type)
        if result:
            logger.info("Memory saved (type=%s): %s", memory_type, content[:80])
        return result

    async def load_knowledge(self) -> str:
        """Load the knowledge base index.

        Equivalent to running `chats knowledge` on the command line.

        Returns:
            Knowledge base output as a string, or empty string on failure.
        """
        result = await _run_chats("knowledge")
        if result:
            logger.debug("Loaded %d characters of knowledge data", len(result))
        return result
