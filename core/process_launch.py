"""Cross-platform subprocess launch helpers shared by Serena surfaces."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable


def windows_launch_argv(
    argv: list[str],
    env: dict[str, str],
    *,
    which: Callable[..., str | None] | None = None,
) -> list[str]:
    """Wrap Windows batch shims so ``subprocess`` and PTYs launch them alike.

    npm installs commands such as Claude and Codex as ``.cmd`` files. Windows
    cannot execute those shims directly with ``shell=False``; resolving the
    shim and routing it through ``cmd /c call`` preserves every argument while
    keeping shell expansion away from user-provided prompt text.
    """

    if not argv:
        raise ValueError("process argv must not be empty")
    resolve = which or shutil.which
    path = env.get("PATH", os.defpath)
    resolved = resolve(argv[0], path=path)
    if not resolved or os.path.splitext(resolved)[1].lower() not in {".bat", ".cmd"}:
        return argv
    comspec = env.get("COMSPEC") or resolve("cmd.exe", path=path) or "cmd.exe"
    return [comspec, "/d", "/s", "/c", "call", resolved, *argv[1:]]


def platform_launch_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """Return argv suitable for the current platform without invoking a shell."""

    if os.name == "nt":
        return windows_launch_argv(argv, env)
    return argv
