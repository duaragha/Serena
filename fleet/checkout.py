"""Pin explicit task baselines independently of a user's mutable checkout."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Fleet baseline git {args[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def requested_baseline(task: str, root: Path) -> str | None:
    """Recognize an explicit directive, not arbitrary commit citations in prose."""
    references = re.findall(r"(?im)^\s*Fleet baseline:\s*`?([^`\s]+)`?\s*$", task)
    mandatory = re.findall(
        r"(?im)^\s*[-*]?\s*MANDATORY start point:\s*branch\s+`[^`]+`\s+at commit\s+([0-9a-f]{40})\b",
        task,
    )
    references += mandatory
    if not references:
        return None
    resolved = {
        _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        for ref in references
    }
    if len(resolved) != 1:
        raise ValueError("conflicting Fleet baseline directives; no worker was launched")
    return resolved.pop()


def ensure_run_checkout(store, run_id: str) -> None:
    """Provision once under the run lock; retries preserve integrated dirty files."""
    with store._connect() as connection:
        row = connection.execute(
            "SELECT * FROM fleet_run_checkouts WHERE run_id = ?", (run_id,),
        ).fetchone()
    if row is None:
        return
    source = Path(row["source_cwd"])
    target = Path(row["path"])
    baseline = row["baseline"]
    if target.exists():
        # Never remove/refork an existing run checkout: it may contain accepted work.
        if target.is_symlink() or not (target / ".git").is_file():
            raise RuntimeError("Fleet baseline checkout is not a verified worktree; preserved for repair")
        if Path(_git(target, "rev-parse", "--show-toplevel")).resolve() != target.resolve():
            raise RuntimeError("Fleet baseline checkout root changed; preserved for repair")
        source_common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        target_common = Path(_git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if source_common.resolve() != target_common.resolve():
            raise RuntimeError("Fleet baseline checkout belongs to another repository")
        _git(target, "merge-base", "--is-ancestor", baseline, "HEAD")
    else:
        if row["state"] == "ready":
            raise RuntimeError("Fleet integration checkout disappeared; cannot discard accepted work")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _git(source, "worktree", "add", "--detach", str(target), baseline)
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE fleet_run_checkouts SET state = 'ready' WHERE run_id = ? AND state = 'pending'",
            (run_id,),
        ).rowcount
        if changed:
            store._insert_event(
                connection, run_id=run_id, event_type="run.baseline_provisioned",
                payload={"baseline": baseline, "path": str(target), "source_cwd": str(source)},
            )


def check_checkout_deletable(run: dict) -> None:
    checkout = run.get("checkout")
    if not checkout:
        return
    target = Path(checkout["path"])
    if not target.exists():
        return
    if target.is_symlink() or not (target / ".git").is_file():
        raise RuntimeError("Fleet baseline checkout ownership is unverifiable; deletion refused")
    dirty = _git(target, "status", "--porcelain", "--untracked-files=all")
    changed = _git(target, "diff", "--name-only", checkout["baseline"], "HEAD", "--")
    if dirty or changed:
        raise RuntimeError(
            f"Fleet baseline checkout retains delivered or uncommitted work at {target}; "
            "archive that work before deleting the run"
        )


def cleanup_run_checkout(run: dict) -> None:
    check_checkout_deletable(run)
    checkout = run.get("checkout")
    if checkout and Path(checkout["path"]).exists():
        # No --force: Git independently refuses newly dirty/locked worktrees.
        _git(Path(checkout["source_cwd"]), "worktree", "remove", checkout["path"])
