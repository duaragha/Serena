"""Bounded read-only Git changes for an already-attached session project."""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def read_project_diff(root, *, max_bytes=2 * 1024 * 1024, max_files=100):
    root = Path(root).resolve(strict=True)
    git = shutil.which("git")
    if not git or not root.is_dir():
        raise RuntimeError("Git project is unavailable")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    deadline = time.monotonic() + 15
    remaining = max_bytes

    def run(args, allowed=(0,)):
        nonlocal remaining
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [git, "--no-pager", "-c", "core.fsmonitor=false", "-c", "diff.external=", *args],
                cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, timeout=max(.01, deadline - time.monotonic()),
            )
            if result.returncode not in allowed:
                raise RuntimeError("Git could not read this project's changes")
            if output.tell() > remaining:
                raise RuntimeError("Project diff exceeds the display limit; narrow the changes before viewing")
            remaining -= output.tell()
            output.seek(0)
            return output.read()

    flags = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--submodule=short"]
    try:
        staged = run([*flags, "--cached", "--", "."])
        unstaged = run([*flags, "--", "."])
        names = run(["ls-files", "--others", "--exclude-standard", "-z", "--", "."]).split(b"\0")
        names = [name for name in names if name]
        if len(names) > max_files:
            raise RuntimeError("Too many untracked files to display")
        untracked, omitted = [], []
        for name in names:
            relative = Path(os.fsdecode(name))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Git returned a path outside the project")
            path = root / relative
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                omitted.append(os.fsdecode(name))
                continue
            untracked.append(run([*flags, "--no-index", "--", os.devnull, str(relative)], allowed=(0, 1)))
        return {"staged": staged.decode("utf-8", "replace"), "unstaged": unstaged.decode("utf-8", "replace"),
                "untracked": b"\n".join(untracked).decode("utf-8", "replace"), "omitted": omitted}
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Git diff timed out") from error
