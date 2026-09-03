"""App and URL launcher tool."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from typing import Any

logger = logging.getLogger(__name__)

# Commands that must never be launched
_BLOCKED_COMMANDS = frozenset({
    "rm", "rmdir", "sudo", "su", "dd", "mkfs", "fdisk", "parted",
    "shutdown", "reboot", "poweroff", "halt", "init",
    "chmod", "chown", "chgrp",
    "kill", "killall", "pkill",
    "mv",  # prevent accidental moves
    "shred", "wipefs",
})

# Pattern for things that look like URLs
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Pattern for things that look like file paths
_PATH_RE = re.compile(r"^[/~.]")


class LauncherTool:
    """Open URLs, files, and applications via xdg-open / subprocess.

    Safety: blocks dangerous shell commands (rm, sudo, dd, etc.)
    before anything is spawned.
    """

    @property
    def name(self) -> str:
        return "launch"

    @property
    def description(self) -> str:
        return (
            "Open a URL in the browser, open a file with its default application, "
            "or launch a desktop application by name. Examples: a website URL, "
            "a file path, or an app name like 'firefox' or 'nautilus'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "What to open — a URL (https://...), file path, "
                        "or application name."
                    ),
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, **_: Any) -> str:
        return await self.launch(target)

    async def launch(self, target: str) -> str:
        """Open the target. Returns a confirmation or error string."""
        target = target.strip()
        if not target:
            return "Nothing to launch — empty target."

        # Safety check: extract the base command word and reject dangerous ones
        base_cmd = target.split()[0].split("/")[-1].lower()
        if base_cmd in _BLOCKED_COMMANDS:
            return f"Blocked: '{base_cmd}' is not allowed for safety reasons."

        # Decide how to launch
        if _URL_RE.match(target) or _PATH_RE.match(target):
            return await self._xdg_open(target)

        # Looks like an app name — try xdg-open first (handles .desktop entries),
        # fall back to running it directly if it's on PATH
        if shutil.which(target):
            return await self._spawn_app(target)

        # Last resort: try xdg-open, which handles registered app names
        return await self._xdg_open(target)

    async def _xdg_open(self, target: str) -> str:
        """Use xdg-open to open a URL, file, or registered handler."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdg-open", target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                kind = "URL" if _URL_RE.match(target) else "target"
                logger.info("Opened %s: %s", kind, target)
                return f"Opened {target}"

            err = stderr.decode(errors="replace").strip() if stderr else "unknown error"
            logger.warning("xdg-open failed for %s: %s", target, err)
            return f"Failed to open {target}: {err}"

        except asyncio.TimeoutError:
            logger.warning("xdg-open timed out for %s", target)
            return f"Opened {target} (xdg-open returned no response, likely opened in background)"
        except FileNotFoundError:
            return "xdg-open not found — is this a Linux desktop?"

    async def _spawn_app(self, app: str) -> str:
        """Launch an application by name as a detached process."""
        try:
            proc = await asyncio.create_subprocess_exec(
                app,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                # Don't tie the app's lifetime to ours
                start_new_session=True,
            )
            logger.info("Launched app '%s' (pid %d)", app, proc.pid)
            return f"Launched {app}"

        except FileNotFoundError:
            return f"Application '{app}' not found on PATH."
        except PermissionError:
            return f"Permission denied launching '{app}'."
