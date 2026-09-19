#!/usr/bin/env python3
"""Git hook shim: his commits and pushes become ambient `vcs` events.

The interrupt policy treats a commit or push as a breakpoint (a natural
boundary where held notices may go out), but no code recorded that stream.
This script is the writer: install it as a git hook and every commit or push
lands one metadata row (repo name + action, never diffs or messages).

Install (operator step, per repo he works in):
    ./scripts/ambient-git-hook.py --install --repo ~/Documents/Projects/serena

That links post-commit, post-merge and pre-push to this file. A hook must
never break his git: every failure mode exits 0 and records nothing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HOOK_ACTIONS = {
    "post-commit": "commit",
    "post-merge": "merge",
    "pre-push": "push",
    "post-checkout": "checkout",
}
INSTALL_HOOKS = ("post-commit", "post-merge", "pre-push")


def _repo_name(cwd: Path) -> str:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if probe.returncode != 0:
        return ""
    return Path(probe.stdout.strip()).name


def record_vcs(action: str, repo: str, *, now: float | None = None) -> bool:
    """One ambient row for a commit/merge/push. False means skipped quietly."""

    from core.ambient_store import AmbientStore

    clean_repo = " ".join(str(repo or "").split())[:128]
    clean_action = " ".join(str(action or "").split())[:32]
    if not clean_repo or not clean_action:
        return False
    try:
        recorded = AmbientStore().record(
            "vcs", app=clean_repo, title=clean_action,
            now=time.time() if now is None else float(now),
        )
    except Exception:
        return False
    return recorded is not None


def install(repo: str) -> list[str]:
    """Link this script into a repo's .git/hooks. Returns installed names."""

    top = Path(repo).expanduser().resolve()
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=top, capture_output=True, text=True, timeout=5,
    )
    if probe.returncode != 0:
        raise SystemExit(f"not a git repo: {repo}")
    git_dir = Path(probe.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = top / git_dir
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in INSTALL_HOOKS:
        target = hooks / name
        if target.exists() and not target.is_symlink():
            # His repo, his hooks: never clobber a real hook file.
            continue
        if target.is_symlink():
            target.unlink()
        target.symlink_to(Path(__file__).resolve())
        installed.append(name)
    return installed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--repo", default=".")
    parser.add_argument("action", nargs="?",
                       help="commit|merge|push (default: from hook name)")
    # parse_known_args: git appends its own hook parameters (pre-push
    # passes "<name> <url>", post-merge a squash flag). A strict parse
    # would exit 2 there — and a failing pre-push aborts his push.
    args, _extra = parser.parse_known_args(argv)
    if args.install:
        try:
            installed = install(args.repo)
        except SystemExit as error:
            print(str(error), file=sys.stderr)
            return 1
        print("installed: " + (", ".join(installed) or "nothing (hooks exist)"))
        return 0
    invoked = Path(sys.argv[0]).name
    # Under a hook name the mapping wins: git's parameters are not actions.
    # The explicit positional exists only for direct manual runs.
    action = HOOK_ACTIONS.get(invoked) or args.action or ""
    if not action:
        print(f"unknown hook {invoked!r}; pass an action explicitly",
              file=sys.stderr)
        return 0
    record_vcs(action, _repo_name(Path.cwd()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
