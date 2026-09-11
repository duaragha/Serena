"""Bounded, names-only project lookup for clients without native file search."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target"}


def search_project_files(root: Path, query: str) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or "\0" in query:
        raise ValueError("A file search of 1 to 200 characters is required")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Session project is unavailable")
    paths = None
    git = shutil.which("git")
    if git:
        # Disk-backed capture bounds RAM even for an unexpectedly large index.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [git, "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, timeout=5,
            )
            if result.returncode == 0:
                if output.tell() > 8 * 1024 * 1024:
                    raise RuntimeError("Project file index exceeds the search limit")
                output.seek(0)
                paths = [os.fsdecode(path) for path in output.read().split(b"\0") if path]
            elif any((parent / ".git").exists() for parent in (root, *root.parents)):
                raise RuntimeError("Git could not read this project's file index")
    if paths is None:
        paths = []
        deadline = time.monotonic() + 2
        visited = 0
        for directory, folders, files in os.walk(root, followlinks=False):
            folders[:] = [name for name in folders if name not in _IGNORE and not name.startswith(".")]
            visited += len(folders) + len(files)
            if visited > 10000 or time.monotonic() > deadline:
                raise RuntimeError("Project file scan exceeds the search limit")
            paths.extend(str((Path(directory) / name).relative_to(root)) for name in files if not name.startswith("."))
    matches = []
    for name in sorted(set(paths)):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or query.casefold() not in name.casefold():
            continue
        target = root / relative
        if target.is_file() and target.resolve().is_relative_to(root):
            matches.append(relative.as_posix())
            if len(matches) == 100:
                break
    return {"paths": matches}
