"""Read-only, bounded working-tree details for the workspace inspector."""

import os
import subprocess


def repository_changes(cwd):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")

    def git(*args):
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            timeout=5,
            env=env,
        )

    root = git("rev-parse", "--show-toplevel")
    if root.returncode:
        return {"is_git": False, "changes": []}
    root_path = os.fsdecode(root.stdout).strip()
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if status.returncode:
        raise RuntimeError("Could not read Git working-tree status")
    records = status.stdout.split(b"\0")
    changes, index = [], 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2].decode("ascii", errors="replace")
        entry = {"status": code.strip(), "path": os.fsdecode(record[3:])}
        # In porcelain -z, rename destination precedes the original path.
        if "R" in code or "C" in code:
            entry["original_path"] = os.fsdecode(records[index])
            index += 1
        changes.append(entry)
    branch = git("symbolic-ref", "--short", "HEAD")
    return {
        "is_git": True,
        "root": root_path,
        "branch": os.fsdecode(branch.stdout).strip(),
        "changes": changes[:500],
        "truncated": len(changes) > 500,
    }
