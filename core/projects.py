"""Canonical project identity for the chat sidebar.

Chats were grouped by the slug Claude Code writes for a working directory
(``-home-raghav-Documents-Projects-personal_projects-locket``). That slug is not
stable: older releases folded ``_`` into ``-``, so one real folder produced two
slugs and the sidebar listed it twice with the history split between them.
On this machine that silently divided locket, atrium, full_tracker and vantage
across pairs of rows.

The working directory recorded inside the transcript does not drift, so it is
the identity used here. Slugs are only a fallback for the few sessions that
never recorded one.

A project that gets renamed on disk is the second source of duplicates, since
old chats keep pointing at the old path forever. Aliases map a retired path onto
its successor so that history follows the project instead of the folder name.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ALIAS_CONFIG_PATH = Path.home() / ".config" / "serena" / "project-aliases.json"

# full_tracker became locket: the locket repo's first commit is literally
# "full working full tracker …", and Claude already filed some of those chats
# under a locket slug while recording a full_tracker cwd. Keeping them apart
# splits one project's history across two rows.
DEFAULT_ALIASES: dict[str, str] = {
    "~/Documents/Projects/personal_projects/full_tracker": (
        "~/Documents/Projects/personal_projects/locket"
    ),
}

_alias_cache: dict[str, object] = {"mtime": None, "aliases": None}


def _expand(path: str) -> str:
    return os.path.expanduser(str(path or "").strip())


def _normalise(path: str) -> str:
    """Trailing separators and duplicate slashes must not fork a project."""

    raw = _expand(path).replace("\\", "/")
    if not raw:
        return ""
    while "//" in raw:
        raw = raw.replace("//", "/")
    if len(raw) > 1:
        raw = raw.rstrip("/")
    return raw


def load_aliases() -> dict[str, str]:
    """Retired path -> current path, from config, falling back to the defaults."""

    aliases = {_normalise(k): _normalise(v) for k, v in DEFAULT_ALIASES.items()}
    try:
        mtime = ALIAS_CONFIG_PATH.stat().st_mtime
    except OSError:
        _alias_cache["mtime"] = None
        _alias_cache["aliases"] = aliases
        return dict(aliases)
    if _alias_cache["mtime"] == mtime and isinstance(_alias_cache["aliases"], dict):
        return dict(_alias_cache["aliases"])  # type: ignore[arg-type]
    try:
        data = json.loads(ALIAS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for key, value in data.items():
            source, target = _normalise(str(key)), _normalise(str(value))
            if source and target and source != target:
                aliases[source] = target
    _alias_cache["mtime"] = mtime
    _alias_cache["aliases"] = aliases
    return dict(aliases)


def canonical_cwd(cwd: str | None) -> str:
    """Resolve one working directory to the project it now belongs to.

    Aliases apply to the directory and everything under it, so a subfolder of a
    renamed project follows it too. Chains are followed, with a bound so a
    mistaken config cannot spin.
    """

    path = _normalise(cwd or "")
    if not path:
        return ""
    aliases = load_aliases()
    for _hop in range(8):
        moved = False
        for source, target in aliases.items():
            if path == source:
                path, moved = target, True
                break
            if path.startswith(source + "/"):
                path, moved = target + path[len(source) :], True
                break
        if not moved:
            return path
    return path


def project_key(project_dir: str | None, cwd: str | None) -> str:
    """The stable identity a chat should be grouped under.

    The recorded cwd wins because it survives slug-format changes. Sessions with
    no cwd keep their slug so they stay visible rather than collapsing into one
    anonymous bucket.
    """

    resolved = canonical_cwd(cwd)
    if resolved:
        return resolved
    return _normalise(project_dir or "") or ""


# --------------------------------------------------------------------------
# Project roots
#
# Grouping by the exact working directory is too literal: /home/raghav alone
# fragments into Downloads, .claude and a stray bluebubbles folder, and every
# ephemeral Fleet worktree becomes its own "project". A chat belongs to the
# repository it was run in, so each cwd is folded up to the nearest real
# project root and Serena's own scratch directories are classified as
# machinery rather than work.
# --------------------------------------------------------------------------

WORKSPACE_ROOTS = (
    "~/Documents/Projects",
    "~/Projects",
)

# Serena's own runtime state. These hold real transcripts, but they are the
# machinery that ran them, not projects to browse alongside your repos.
MACHINERY_PREFIXES = (
    "~/.local/state/serena",
    "~/.config/serena",
    "~/.cache/serena-headless-codex",
    "~/.cache/serena-headless-brain",
    "~/.claude",
    "~/.codex",
    "/tmp",
    "/var/tmp",
)

_root_cache: dict[str, str] = {}


def is_machinery(cwd: str | None) -> bool:
    """True for Serena's own scratch, state and temp directories."""

    path = _normalise(cwd or "")
    if not path:
        return False
    if "/fleet-worktrees/" in path:
        return True
    for prefix in MACHINERY_PREFIXES:
        root = _normalise(prefix)
        if root and (path == root or path.startswith(root + "/")):
            return True
    return False


def _is_repo(path: str) -> bool:
    """A checkout, whether a full clone or a linked worktree.

    Git writes .git as a directory for a clone but as a FILE for a worktree,
    and the per-ticket checkouts here are worktrees, so testing only for a
    directory missed every one of them.
    """

    return bool(path) and os.path.exists(os.path.join(path, ".git"))


# A per-ticket clone is named for the work, not for a different project:
# liquid-fw43, frameworth-google-ads-fw34, ...-fw43-followup, ...-fw32-closeout.
# Folding on any shared prefix would also merge genuinely separate repos such as
# amazon and amazon-appeals, so only these ticket-shaped tails count.
_TICKET_TAIL = re.compile(
    r"^(?:fw|pr|issue|task|v)?\d+[a-z]?"
    r"(?:[-_](?:followup|follow-up|closeout|fleet|part|[a-z]|\d+))*$",
    re.IGNORECASE,
)
_TICKET_WORDS = frozenset({"followup", "follow-up", "closeout", "wip", "tmp", "fleet"})


def _ticket_stem(name: str) -> str:
    """Strip a trailing ticket marker, e.g. liquid-fw43 -> liquid."""

    parts = name.split("-")
    while len(parts) > 1:
        tail = parts[-1].lower()
        if _TICKET_TAIL.match(tail) or tail in _TICKET_WORDS:
            parts = parts[:-1]
            continue
        break
    stem = "-".join(parts)
    return stem if stem and stem != name else ""


def _sibling_repo_prefix(parent: str, name: str) -> str:
    """Fold a per-ticket clone onto the repository it was cut from.

    The base repo is folded onto when it exists (liquid-fw43 -> liquid). When a
    project is only ever cloned per ticket there is no base directory at all, so
    a cluster of siblings sharing the stem stands in for it: the fifteen
    frameworth-google-ads-* checkouts become one project instead of fifteen rows
    that all truncate to the same thing.
    """

    stem = _ticket_stem(name)
    if not stem:
        return ""
    if _is_repo(os.path.join(parent, stem)):
        return stem
    try:
        siblings = os.listdir(parent)
    except OSError:
        return ""
    cluster = sum(
        1
        for sibling in siblings
        if sibling != name
        and (sibling == stem or _ticket_stem(sibling) == stem)
        and _is_repo(os.path.join(parent, sibling))
    )
    return stem if cluster >= 2 else ""


def project_root(cwd: str | None) -> str:
    """Fold one working directory up to the project it belongs to."""

    path = canonical_cwd(cwd)
    if not path or is_machinery(path):
        return path
    cached = _root_cache.get(path)
    if cached is not None:
        return cached

    result = path
    for workspace in WORKSPACE_ROOTS:
        root = _normalise(workspace)
        if not root or not path.startswith(root + "/"):
            continue
        parts = [p for p in path[len(root) + 1 :].split("/") if p]
        if not parts:
            break
        # A git worktree lives under <repo>/.worktrees/<branch>; the branch is
        # not a separate project.
        if ".worktrees" in parts:
            parts = parts[: parts.index(".worktrees")]
        # Walk down while each level is a plain container (no .git of its own),
        # so personal_projects/locket resolves to locket while a bare repo
        # directly under Projects resolves to itself.
        chosen: list[str] = []
        current = root
        for part in parts:
            candidate = os.path.join(current, part)
            chosen.append(part)
            current = candidate
            if _is_repo(candidate) or not os.path.isdir(candidate):
                break
        if chosen:
            parent = os.path.join(root, *chosen[:-1]) if len(chosen) > 1 else root
            folded = _sibling_repo_prefix(parent, chosen[-1])
            if folded:
                chosen[-1] = folded
            result = _normalise(os.path.join(root, *chosen))
        break
    else:
        # A cwd under home but outside every workspace root is a one-off: a
        # Downloads folder, a stray Documents path, a chat started from home
        # itself. Those showed up as single-chat rows beside real projects.
        # Anything that is genuinely a checkout keeps its own row; the rest
        # fold into home.
        home = _normalise("~")
        if home and (path == home or path.startswith(home + "/")) and not _is_repo(path):
            result = home

    _root_cache[path] = result
    return result
