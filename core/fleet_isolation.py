"""Isolated git worktrees, enforced path claims, and gated integration.

Fleet's only protection for concurrent writers used to be sequencing in one
checkout. This module gives every writer its own worktree instead. The
supervisor claims declared ownership before execution and takes a repository-
wide claim when ownership is unknown. Workers never mutate this registry.
Every overlap or base drift is refused without partially applying a patch.

The safety rule that shapes everything here: creating a worktree from a commit
cannot hurt a dirty base, but merging back can. So isolation is permitted
whenever the repository is structurally healthy, and integration is permitted
only when the incoming paths provably do not intersect the base's dirty work.
When that cannot be proven this fails closed, leaves the branch intact, and
reports the conflict instead of resolving it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.coding_job_contract import (
    GitSnapshotError,
    RepositoryResolutionError,
    _git,
    capture_git_snapshot,
    validate_repository_root,
)

DEFAULT_ISOLATION_DB_PATH = (
    Path.home() / ".local" / "state" / "serena" / "fleet-isolation.sqlite3"
)
DEFAULT_WORKSPACE_ROOT = Path.home() / ".local" / "state" / "serena" / "fleet-worktrees"
DEFAULT_INTEGRATION_LOCK_ROOT = (
    Path.home() / ".local" / "state" / "serena" / "fleet-integration-locks"
)
SCHEMA_VERSION = 1

CLAIM_MODES = frozenset({"write", "read"})
CLAIM_STATES = frozenset({"active", "released", "transferred"})
ISOLATION_MODES = frozenset({"worktree", "shared_fallback"})
WORKSPACE_STATES = frozenset({"active", "integrated", "abandoned", "blocked"})

MAX_REASON_CHARS = 2_000
MAX_CLAIM_PATHS = 500
MAX_PATH_CHARS = 512
MAX_PATCH_CHARS = 4_000_000
ROOT_CLAIM = "*"

# A worker may never claim these. Git's own metadata, credentials, and local
# runtime state are not implementation surface, and a merge that rewrote them
# would be unreviewable.
PROTECTED_PATH_PREFIXES = (
    ".git/",
    ".ssh/",
    "config/secrets/",
)
PROTECTED_PATH_NAMES = frozenset(
    {
        ".git",
        ".env",
        "telegram.env",
        "brain.env",
        "id_rsa",
        "credentials.json",
    }
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_BRANCH_TOKEN = re.compile(r"[^A-Za-z0-9._-]+")


class IsolationError(RuntimeError):
    """Isolation could not be established or proven safe."""


@dataclass(frozen=True, slots=True)
class IsolationAssessment:
    """Whether per-writer worktrees are provably safe for this checkout."""

    safe: bool
    mode: str
    reason: str
    repo_root: str
    head: str
    base_dirty_paths: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Workspace:
    run_id: str
    worker_key: str
    path: str
    branch: str
    base_head: str
    state: str
    created_at: float
    updated_at: float
    integrated_at: float | None = None
    rollback_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    """The result of asking for exclusive write ownership of some paths."""

    granted: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    ok: bool
    run_id: str
    worker_key: str
    reason: str
    changed_paths: list[str] = field(default_factory=list)
    unclaimed_paths: list[str] = field(default_factory=list)
    dirty_conflicts: list[str] = field(default_factory=list)
    rollback_ref: str = ""
    patch_path: str = ""
    test_gate: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalise_claim_path(value: object) -> str:
    """Reduce a claim to a repo-relative posix path, or reject it."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("claim paths must be short repo-relative paths")
    if raw.startswith("/") or ".." in Path(raw).parts:
        raise ValueError("claim paths must stay inside the repository")
    cleaned = "/".join(part for part in raw.split("/") if part and part != ".")
    if not cleaned:
        raise ValueError("claim paths must name a real path")
    return cleaned


def is_protected_path(path: str) -> bool:
    cleaned = normalise_claim_path(path)
    if cleaned.startswith(PROTECTED_PATH_PREFIXES):
        return True
    parts = cleaned.split("/")
    if any(part in PROTECTED_PATH_NAMES for part in parts):
        return True
    return cleaned.endswith(_SECRET_SUFFIXES)


def paths_overlap(left: str, right: str) -> bool:
    """True when two claims cover any of the same files.

    Directory claims are prefixes, so ``core`` and ``core/fleet_store.py``
    collide. This is deliberately conservative: a false collision costs one
    serialized wave, a missed collision costs a lost edit.
    """

    a = normalise_claim_path(left)
    b = normalise_claim_path(right)
    if ROOT_CLAIM in {a, b}:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def assess_isolation(cwd: str | Path) -> IsolationAssessment:
    """Decide whether per-writer worktrees are safe for this repository.

    A dirty base is explicitly fine here. Worktrees are created from the HEAD
    commit and live in their own directory, so uncommitted work in the base
    checkout is never the worker's working copy. What blocks isolation is a
    repository that is structurally unable to carry another worktree.
    """

    checks: dict[str, Any] = {}
    try:
        root = validate_repository_root(cwd)
    except RepositoryResolutionError as error:
        return IsolationAssessment(
            safe=False,
            mode="shared_fallback",
            reason=f"not an isolable git repository: {error}",
            repo_root=str(Path(cwd).expanduser()),
            head="",
            checks={"repository": False},
        )
    checks["repository"] = True

    bare = _git(root, "rev-parse", "--is-bare-repository", check=False).stdout.strip()
    checks["bare_repository"] = bare == "true"
    if checks["bare_repository"]:
        return _unsafe(root, "", "a bare repository cannot host a worker worktree", checks)

    head = _git(root, "rev-parse", "HEAD", check=False)
    if head.returncode != 0 or not head.stdout.strip():
        checks["head_commit"] = False
        return _unsafe(root, "", "HEAD has no commit to branch a worktree from", checks)
    head_sha = head.stdout.strip()
    checks["head_commit"] = True

    git_dir = _git(root, "rev-parse", "--git-dir", check=False).stdout.strip()
    git_path = (root / git_dir) if git_dir and not Path(git_dir).is_absolute() else Path(git_dir or "")
    in_progress = [
        name
        for name in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
        if git_path and (git_path / name).exists()
    ]
    for directory in ("rebase-merge", "rebase-apply"):
        if git_path and (git_path / directory).exists():
            in_progress.append(directory)
    checks["in_progress_operations"] = in_progress
    if in_progress:
        return _unsafe(
            root,
            head_sha,
            f"a git operation is already in progress: {', '.join(sorted(in_progress))}",
            checks,
        )

    worktree_support = _git(root, "worktree", "list", check=False)
    checks["worktree_support"] = worktree_support.returncode == 0
    if not checks["worktree_support"]:
        return _unsafe(root, head_sha, "this git installation cannot list worktrees", checks)

    dirty = _dirty_paths(root)
    checks["base_dirty_path_count"] = len(dirty)
    return IsolationAssessment(
        safe=True,
        mode="worktree",
        reason=(
            "per-writer worktrees branch from HEAD, so the dirty base checkout is "
            "never a worker's working copy"
        ),
        repo_root=str(root),
        head=head_sha,
        base_dirty_paths=dirty,
        checks=checks,
    )


def _unsafe(
    root: Path | str, head: str, reason: str, checks: dict[str, Any]
) -> IsolationAssessment:
    return IsolationAssessment(
        safe=False,
        mode="shared_fallback",
        reason=reason,
        repo_root=str(root),
        head=head,
        checks=checks,
    )


def _dirty_paths(root: Path) -> list[str]:
    """Every tracked-modified, deleted, or untracked path in the base checkout."""

    result = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
        check=False,
        text=False,
    )
    if result.returncode != 0:
        return []
    entries: list[str] = []
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    tokens = [token for token in raw.split("\0") if token]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if len(token) < 4:
            index += 1
            continue
        status, path = token[:2], token[3:]
        entries.append(path)
        # Renames carry their source path in the following NUL-separated field.
        if "R" in status or "C" in status:
            index += 1
            if index < len(tokens):
                entries.append(tokens[index])
        index += 1
    return sorted({normalise_claim_path(entry) for entry in entries if entry.strip()})


def branch_name(run_id: str, worker_key: str) -> str:
    run_token = _BRANCH_TOKEN.sub("-", str(run_id)).strip("-") or "run"
    worker_token = _BRANCH_TOKEN.sub("-", str(worker_key)).strip("-") or "worker"
    return f"serena/fleet/{run_token}/{worker_token}"


class FleetIsolationStore:
    """Durable claims and worktree registry for one Fleet installation."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_FLEET_ISOLATION_DB_PATH", "").strip()
        fleet_path = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
        colocated = (
            Path(fleet_path).expanduser().with_name("fleet-isolation.sqlite3")
            if fleet_path
            else None
        )
        self.path = Path(path or configured or colocated or DEFAULT_ISOLATION_DB_PATH).expanduser()
        root = os.environ.get("SERENA_FLEET_WORKSPACE_ROOT", "").strip()
        self.workspace_root = Path(
            workspace_root or root or DEFAULT_WORKSPACE_ROOT
        ).expanduser()
        self._initialize()

    # ---- claims -----------------------------------------------------------

    def claim_paths(
        self,
        *,
        run_id: str,
        worker_key: str,
        paths: list[str],
        mode: str = "write",
    ) -> ClaimDecision:
        """Take exclusive ownership of paths, or report exactly what blocked it.

        Claims are all-or-nothing per call. A partially granted claim would let
        a worker start editing a surface it does not fully own, which is the
        failure this registry exists to prevent.
        """

        clean_mode = str(mode or "").strip().lower()
        if clean_mode not in CLAIM_MODES:
            raise ValueError("claim mode must be write or read")
        run = _identifier(run_id, "run_id")
        worker = _identifier(worker_key, "worker_key")
        if len(paths) > MAX_CLAIM_PATHS:
            raise ValueError("too many paths in one claim")

        requested: list[str] = []
        rejected: list[dict[str, str]] = []
        for candidate in paths:
            try:
                cleaned = normalise_claim_path(candidate)
            except ValueError as error:
                rejected.append({"path": str(candidate)[:MAX_PATH_CHARS], "reason": str(error)})
                continue
            if is_protected_path(cleaned):
                rejected.append(
                    {"path": cleaned, "reason": "protected path cannot be claimed by a worker"}
                )
                continue
            requested.append(cleaned)
        if rejected:
            return ClaimDecision(rejected=rejected)

        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT worker_key, path, mode FROM fleet_path_claims "
                "WHERE run_id = ? AND state = 'active'",
                (run,),
            ).fetchall()
            conflicts: list[dict[str, str]] = []
            for candidate in requested:
                for row in existing:
                    holder = str(row["worker_key"])
                    if holder == worker:
                        continue
                    # Two readers never collide; a writer collides with anyone.
                    if clean_mode == "read" and str(row["mode"]) == "read":
                        continue
                    if paths_overlap(candidate, str(row["path"])):
                        conflicts.append(
                            {
                                "path": candidate,
                                "held_by": holder,
                                "held_path": str(row["path"]),
                                "held_mode": str(row["mode"]),
                            }
                        )
            if conflicts:
                return ClaimDecision(conflicts=conflicts)
            for candidate in requested:
                connection.execute(
                    """
                    INSERT INTO fleet_path_claims(
                        run_id, worker_key, path, mode, state, claimed_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(run_id, worker_key, path) DO UPDATE SET
                        state = 'active', mode = excluded.mode, updated_at = excluded.updated_at
                    """,
                    (run, worker, candidate, clean_mode, now, now),
                )
        return ClaimDecision(granted=sorted(requested))

    def active_claims(
        self, run_id: str, *, worker_key: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?", "state = 'active'"]
        params: list[object] = [_identifier(run_id, "run_id")]
        if worker_key is not None:
            clauses.append("worker_key = ?")
            params.append(_identifier(worker_key, "worker_key"))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, worker_key, path, mode, state, claimed_at, updated_at "
                "FROM fleet_path_claims WHERE " + " AND ".join(clauses) + " ORDER BY path",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def release_claims(self, run_id: str, worker_key: str) -> int:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE fleet_path_claims SET state = 'released', updated_at = ? "
                "WHERE run_id = ? AND worker_key = ? AND state = 'active'",
                (now, _identifier(run_id, "run_id"), _identifier(worker_key, "worker_key")),
            )
            return int(cursor.rowcount or 0)

    def transfer_claims(
        self, *, run_id: str, from_worker: str, to_worker: str, reason: str = ""
    ) -> list[str]:
        """Move ownership on a provider handoff or reassignment.

        The same logical worker slot keeps its surface across a handoff, so the
        replacement inherits the claims instead of colliding with the ghost of
        the attempt it replaced.
        """

        run = _identifier(run_id, "run_id")
        source = _identifier(from_worker, "worker_key")
        target = _identifier(to_worker, "worker_key")
        if source == target:
            return []
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT path, mode FROM fleet_path_claims "
                "WHERE run_id = ? AND worker_key = ? AND state = 'active'",
                (run, source),
            ).fetchall()
            moved: list[str] = []
            for row in rows:
                path = str(row["path"])
                connection.execute(
                    """
                    INSERT INTO fleet_path_claims(
                        run_id, worker_key, path, mode, state, claimed_at, updated_at,
                        transferred_from
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                    ON CONFLICT(run_id, worker_key, path) DO UPDATE SET
                        state = 'active', updated_at = excluded.updated_at,
                        transferred_from = excluded.transferred_from
                    """,
                    (run, target, path, str(row["mode"]), now, now, source),
                )
                moved.append(path)
            connection.execute(
                "UPDATE fleet_path_claims SET state = 'transferred', updated_at = ?, "
                "transfer_reason = ? WHERE run_id = ? AND worker_key = ? AND state = 'active'",
                (now, _text(reason, MAX_REASON_CHARS), run, source),
            )
        return sorted(moved)

    def unclaimed_paths(
        self, *, run_id: str, worker_key: str, paths: list[str]
    ) -> list[str]:
        """Which of these changed paths this worker never claimed."""

        held = [
            str(row["path"])
            for row in self.active_claims(run_id, worker_key=worker_key)
            if str(row["mode"]) == "write"
        ]
        missing: list[str] = []
        for candidate in paths:
            cleaned = normalise_claim_path(candidate)
            if not any(paths_overlap(cleaned, owned) for owned in held):
                missing.append(cleaned)
        return sorted(set(missing))

    # ---- workspaces -------------------------------------------------------

    def record_workspace(
        self,
        *,
        run_id: str,
        worker_key: str,
        path: str,
        branch: str,
        base_head: str,
    ) -> Workspace:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO fleet_workspaces(
                    run_id, worker_key, path, branch, base_head, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(run_id, worker_key) DO UPDATE SET
                    path = excluded.path, branch = excluded.branch,
                    base_head = excluded.base_head, state = 'active',
                    reason = NULL, rollback_ref = NULL, integrated_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    _identifier(run_id, "run_id"),
                    _identifier(worker_key, "worker_key"),
                    str(path),
                    str(branch),
                    str(base_head),
                    now,
                    now,
                ),
            )
            return self._require_workspace(connection, run_id, worker_key)

    def get_workspace(self, run_id: str, worker_key: str) -> Workspace | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_workspaces WHERE run_id = ? AND worker_key = ?",
                (_identifier(run_id, "run_id"), _identifier(worker_key, "worker_key")),
            ).fetchone()
        return _workspace_from_row(row) if row is not None else None

    def workspaces(self, run_id: str) -> list[Workspace]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fleet_workspaces WHERE run_id = ? ORDER BY worker_key",
                (_identifier(run_id, "run_id"),),
            ).fetchall()
        return [_workspace_from_row(row) for row in rows]

    def mark_workspace(
        self,
        *,
        run_id: str,
        worker_key: str,
        state: str,
        rollback_ref: str | None = None,
        reason: str = "",
    ) -> Workspace:
        clean_state = str(state or "").strip().lower()
        if clean_state not in WORKSPACE_STATES:
            raise ValueError("invalid workspace state")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_workspace(connection, run_id, worker_key)
            connection.execute(
                "UPDATE fleet_workspaces SET state = ?, updated_at = ?, "
                "integrated_at = CASE WHEN ? = 'integrated' THEN ? ELSE integrated_at END, "
                "rollback_ref = COALESCE(?, rollback_ref), reason = ? "
                "WHERE run_id = ? AND worker_key = ?",
                (
                    clean_state,
                    now,
                    clean_state,
                    now,
                    rollback_ref,
                    _text(reason, MAX_REASON_CHARS),
                    _identifier(run_id, "run_id"),
                    _identifier(worker_key, "worker_key"),
                ),
            )
            return self._require_workspace(connection, run_id, worker_key)

    def record_integration(self, result: IntegrationResult) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO fleet_integrations(
                    run_id, worker_key, ok, reason, changed_paths_json,
                    unclaimed_paths_json, dirty_conflicts_json, rollback_ref,
                    patch_path, test_gate_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.worker_key,
                    1 if result.ok else 0,
                    _text(result.reason, MAX_REASON_CHARS),
                    json.dumps(result.changed_paths, separators=(",", ":")),
                    json.dumps(result.unclaimed_paths, separators=(",", ":")),
                    json.dumps(result.dirty_conflicts, separators=(",", ":")),
                    result.rollback_ref,
                    result.patch_path,
                    json.dumps(result.test_gate, separators=(",", ":"), default=str),
                    time.time(),
                ),
            )

    def integrations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fleet_integrations WHERE run_id = ? ORDER BY created_at, rowid",
                (_identifier(run_id, "run_id"),),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            for key in (
                "changed_paths_json",
                "unclaimed_paths_json",
                "dirty_conflicts_json",
                "test_gate_json",
            ):
                with suppress(json.JSONDecodeError, TypeError):
                    entry[key.removesuffix("_json")] = json.loads(str(entry.pop(key) or "[]"))
            entry["ok"] = bool(entry.get("ok"))
            entries.append(entry)
        return entries

    def delete_run_records(self, run_id: str) -> None:
        """Remove isolation history after a terminal Fleet is explicitly deleted."""

        clean_id = _identifier(run_id, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM fleet_path_claims WHERE run_id = ?", (clean_id,))
            connection.execute("DELETE FROM fleet_integrations WHERE run_id = ?", (clean_id,))
            connection.execute("DELETE FROM fleet_workspaces WHERE run_id = ?", (clean_id,))

    @staticmethod
    def _require_workspace(
        connection: sqlite3.Connection, run_id: str, worker_key: str
    ) -> Workspace:
        row = connection.execute(
            "SELECT * FROM fleet_workspaces WHERE run_id = ? AND worker_key = ?",
            (run_id, worker_key),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Fleet workspace {run_id}/{worker_key}")
        return _workspace_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_path_claims (
                    run_id TEXT NOT NULL,
                    worker_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    claimed_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    transferred_from TEXT,
                    transfer_reason TEXT,
                    PRIMARY KEY (run_id, worker_key, path)
                );
                CREATE INDEX IF NOT EXISTS fleet_path_claims_run_idx
                    ON fleet_path_claims(run_id, state, path);

                CREATE TABLE IF NOT EXISTS fleet_workspaces (
                    run_id TEXT NOT NULL,
                    worker_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    base_head TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    rollback_ref TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    integrated_at REAL,
                    PRIMARY KEY (run_id, worker_key)
                );
                CREATE INDEX IF NOT EXISTS fleet_workspaces_run_idx
                    ON fleet_workspaces(run_id, state);

                CREATE TABLE IF NOT EXISTS fleet_integrations (
                    integration_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    worker_key TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    reason TEXT,
                    changed_paths_json TEXT NOT NULL,
                    unclaimed_paths_json TEXT NOT NULL,
                    dirty_conflicts_json TEXT NOT NULL,
                    rollback_ref TEXT,
                    patch_path TEXT,
                    test_gate_json TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fleet_integrations_run_idx
                    ON fleet_integrations(run_id, created_at);
                """
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def ensure_workspace(
    store: FleetIsolationStore,
    *,
    run_id: str,
    worker_key: str,
    cwd: str | Path,
    assessment: IsolationAssessment | None = None,
) -> Workspace:
    """Create or reuse this logical worker's isolated worktree.

    Reuse matters: one worker keeps the same worktree across phases, so its
    Code-phase edits are still there when it returns in Fix. A fresh worktree
    per attempt would silently discard the previous phase's work.
    """

    check = assessment or assess_isolation(cwd)
    if not check.safe:
        raise IsolationError(check.reason)
    root = Path(check.repo_root)
    existing = store.get_workspace(run_id, worker_key)
    existing_usable = existing is not None and _workspace_is_usable(existing)
    if existing is not None and existing.state == "active" and existing_usable:
        _share_base_venv(root, Path(existing.path))
        return existing
    if existing is not None and existing.state == "blocked" and existing_usable:
        changed = workspace_changed_paths(existing)
        recovered = any(
            entry.get("worker_key") == worker_key
            and set(entry.get("changed_paths") or []) == set(changed)
            and bool(entry.get("patch_path"))
            and Path(str(entry.get("patch_path"))).is_file()
            for entry in reversed(store.integrations(run_id))
        )
        if changed and not recovered:
            raise IsolationError(
                f"blocked workspace for {worker_key} has unrecovered changes and cannot be reforked"
            )

    branch = branch_name(run_id, worker_key)
    target = store.workspace_root / _BRANCH_TOKEN.sub("-", str(run_id)).strip("-") / (
        _BRANCH_TOKEN.sub("-", str(worker_key)).strip("-") or "worker"
    )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if existing is not None and Path(existing.path).is_dir():
        if Path(existing.path).resolve() != target.resolve():
            raise IsolationError(f"recorded workspace path escaped its Fleet slot for {worker_key}")
        if existing_usable:
            removed = _git(root, "worktree", "remove", "--force", existing.path, check=False)
            if removed.returncode != 0:
                raise IsolationError(
                    f"could not refresh the prior worktree for {worker_key}: "
                    f"{removed.stderr.strip()[:400]}"
                )
            _git(root, "worktree", "prune", check=False)
        else:
            _quarantine_invalid_workspace(store, target)
            _git(root, "worktree", "prune", check=False)
    if target.exists():
        if any(target.iterdir()):
            _quarantine_invalid_workspace(store, target)
        else:
            with suppress(OSError):
                target.rmdir()

    baseline = _baseline_commit(root, check, run_id=run_id, worker_key=worker_key)
    existing_branch = _git(root, "rev-parse", "--verify", "--quiet", branch, check=False)
    args = ["worktree", "add"]
    if existing_branch.returncode == 0:
        moved = _git(root, "branch", "-f", branch, baseline, check=False)
        if moved.returncode != 0:
            raise IsolationError(
                f"could not refresh the isolated branch for {worker_key}: "
                f"{moved.stderr.strip()[:400]}"
            )
        args += [str(target), branch]
    else:
        args += ["-b", branch, str(target), baseline]
    created = _git(root, *args, check=False)
    if created.returncode != 0:
        raise IsolationError(
            f"could not create an isolated worktree for {worker_key}: "
            f"{created.stderr.strip()[:400]}"
        )
    _share_base_venv(root, target)
    workspace = store.record_workspace(
        run_id=run_id,
        worker_key=worker_key,
        path=str(target),
        branch=branch,
        base_head=baseline,
    )
    if not _workspace_is_usable(workspace):
        raise IsolationError(f"created worktree for {worker_key} failed repository validation")
    return workspace


def refresh_workspace_for_retry(
    store: FleetIsolationStore,
    *,
    run_id: str,
    worker_key: str,
    cwd: str | Path,
    assessment: IsolationAssessment | None = None,
) -> tuple[Workspace, dict[str, Any]]:
    """Resume a retry on the latest combined base without losing its patch.

    A rejected provider turn may leave useful edits in its worktree while a
    peer integrates into the authoritative checkout. Reusing that old tree
    hides the peer work and can only discover the collision after another
    expensive turn. Fleet therefore snapshots the worker patch, reforks from
    the current combined tree, and reapplies only when plain ``git apply`` can
    prove the patch remains conflict-free. A conflicting patch stays durable
    for the same worker to reconcile explicitly.
    """

    check = assessment or assess_isolation(cwd)
    if not check.safe:
        raise IsolationError(check.reason)
    root = Path(check.repo_root)
    existing = store.get_workspace(run_id, worker_key)
    if existing is None:
        workspace = ensure_workspace(
            store,
            run_id=run_id,
            worker_key=worker_key,
            cwd=root,
            assessment=check,
        )
        return workspace, {"action": "created", "changed_paths": []}

    if existing.state == "integrated" or not _workspace_is_usable(existing):
        workspace = ensure_workspace(
            store,
            run_id=run_id,
            worker_key=worker_key,
            cwd=root,
            assessment=check,
        )
        return workspace, {"action": "refreshed", "changed_paths": []}

    changed = workspace_changed_paths(existing)
    if not changed:
        return existing, {"action": "reused", "changed_paths": []}
    protected = [path for path in changed if is_protected_path(path)]
    if protected:
        raise IsolationError(
            "retry cannot refork a workspace containing protected paths: "
            + ", ".join(protected[:20])
        )

    current_base = _baseline_commit(
        root,
        check,
        run_id=run_id,
        worker_key=worker_key,
    )
    previous_tree = _git(
        root, "rev-parse", f"{existing.base_head}^{{tree}}", check=False
    )
    current_tree = _git(root, "rev-parse", f"{current_base}^{{tree}}", check=False)
    if (
        previous_tree.returncode == 0
        and current_tree.returncode == 0
        and previous_tree.stdout.strip() == current_tree.stdout.strip()
        and existing.state == "active"
    ):
        return existing, {"action": "reused", "changed_paths": changed}

    patch = _workspace_patch(existing, changed)
    if patch is None:
        raise IsolationError("could not preserve the worker patch before retry refork")
    patch_path = _persist_patch(store, run_id, worker_key, patch)
    if not patch_path:
        raise IsolationError("could not persist the worker patch before retry refork")
    drift = base_drift_paths(root, existing, changed)
    reason = (
        "retry reforked from the latest combined checkout because the base changed"
    )
    store.record_integration(
        IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=reason,
            changed_paths=changed,
            dirty_conflicts=drift,
            patch_path=patch_path,
        )
    )
    store.mark_workspace(
        run_id=run_id,
        worker_key=worker_key,
        state="blocked",
        reason=reason,
    )
    workspace = ensure_workspace(
        store,
        run_id=run_id,
        worker_key=worker_key,
        cwd=root,
        assessment=check,
    )

    checked = subprocess.run(
        ["git", "-C", workspace.path, "apply", "--check", patch_path],
        capture_output=True,
        text=True,
        check=False,
    )
    applied = None
    if checked.returncode == 0:
        applied = subprocess.run(
            ["git", "-C", workspace.path, "apply", patch_path],
            capture_output=True,
            text=True,
            check=False,
        )
    if applied is not None and applied.returncode == 0:
        return workspace, {
            "action": "reforked_reapplied",
            "changed_paths": changed,
            "drift_paths": drift,
            "patch_path": patch_path,
        }
    detail = (
        (applied.stderr if applied is not None else checked.stderr).strip()[:400]
        or "patch overlaps the latest combined checkout"
    )
    return workspace, {
        "action": "reforked_conflict",
        "changed_paths": changed,
        "drift_paths": drift,
        "patch_path": patch_path,
        "reason": detail,
    }


def _share_base_venv(root: Path, target: Path) -> None:
    """Symlink the base checkout's virtualenv into a worker's worktree.

    A bare worktree has no .venv, which left workers improvising with the
    system python ("No module named pytest") and recording verification the
    completion gate could not re-run. Applied on creation AND reuse so
    workspaces from before this change pick it up on their next phase.
    """

    base_venv = root / ".venv"
    venv_link = target / ".venv"
    if base_venv.is_dir() and not venv_link.exists():
        with suppress(OSError):
            venv_link.symlink_to(base_venv, target_is_directory=True)


def _workspace_is_usable(workspace: Workspace) -> bool:
    """A reusable workspace must still be a real Git worktree with a valid HEAD."""

    root = Path(workspace.path)
    git_link = root / ".git"
    if not root.is_dir() or not git_link.is_file():
        return False
    try:
        if git_link.stat().st_size <= 0:
            return False
    except OSError:
        return False
    top = _git(root, "rev-parse", "--show-toplevel", check=False)
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    return (
        top.returncode == 0
        and Path(top.stdout.strip()).resolve() == root.resolve()
        and head.returncode == 0
        and bool(head.stdout.strip())
    )


def _quarantine_invalid_workspace(store: FleetIsolationStore, target: Path) -> Path:
    """Move a corrupt disposable workspace aside so evidence remains recoverable."""

    workspace_root = store.workspace_root.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as error:
        raise IsolationError("refusing to quarantine a path outside Fleet workspace storage") from error
    quarantine = target.with_name(f".{target.name}.invalid-{time.time_ns()}")
    target.rename(quarantine)
    return quarantine


def _baseline_commit(
    root: Path,
    assessment: IsolationAssessment,
    *,
    run_id: str,
    worker_key: str,
) -> str:
    """Freeze the exact dirty base into a synthetic commit for one worker.

    Worktrees created directly from HEAD omit every uncommitted and untracked
    file in the base checkout. Freezing the base tree lets a worker see the
    combined implementation without staging or changing the real index. The
    generated commit is only an internal worktree baseline.
    """

    if not assessment.base_dirty_paths:
        return assessment.head
    item_id = f"fleet-baseline-{run_id}-{worker_key}"
    snapshot = capture_git_snapshot(root, item_id=item_id, label="baseline")
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Serena Fleet",
            "GIT_AUTHOR_EMAIL": "fleet@serena.local",
            "GIT_COMMITTER_NAME": "Serena Fleet",
            "GIT_COMMITTER_EMAIL": "fleet@serena.local",
        }
    )
    created = subprocess.run(
        ["git", "-C", str(root), "commit-tree", snapshot.tree, "-p", snapshot.head],
        input=f"Serena Fleet baseline for {run_id}/{worker_key}\n",
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    commit = created.stdout.strip()
    if created.returncode != 0 or not commit:
        raise IsolationError(
            "could not freeze the dirty base for an isolated worker: "
            + created.stderr.strip()[:400]
        )
    verified = _git(root, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    if verified.returncode != 0:
        raise IsolationError("the synthetic Fleet baseline commit failed object validation")
    ref = (
        "refs/serena/fleet-baselines/"
        + _BRANCH_TOKEN.sub("-", str(run_id)).strip("-")
        + "/"
        + (_BRANCH_TOKEN.sub("-", str(worker_key)).strip("-") or "worker")
    )
    updated = _git(root, "update-ref", ref, commit, check=False)
    if updated.returncode != 0:
        raise IsolationError(
            f"could not retain the isolated baseline for {worker_key}: "
            f"{updated.stderr.strip()[:400]}"
        )
    return commit


def workspace_changed_paths(workspace: Workspace) -> list[str]:
    """Every path this worker actually changed, committed or not."""

    root = Path(workspace.path)
    if not root.is_dir():
        return []
    changed: set[str] = set()
    committed = _git(
        root, "diff", "--name-only", "-z", f"{workspace.base_head}..HEAD", check=False, text=False
    )
    if committed.returncode == 0:
        changed.update(
            part.decode("utf-8", errors="surrogateescape")
            for part in committed.stdout.split(b"\0")
            if part
        )
    changed.update(_dirty_paths(root))
    # Fleet provisions workspace/.venv as a symlink to the base checkout's
    # virtualenv. The repo ignores `.venv/` (a directory-only pattern), which
    # does not match a symlink, so git would report Fleet's own infrastructure
    # as the worker's untracked change.
    changed.discard(".venv")
    return sorted(normalise_claim_path(entry) for entry in changed if str(entry).strip())


def unrecovered_workspaces(
    store: FleetIsolationStore, run_id: str
) -> list[dict[str, Any]]:
    """Return worker trees whose edits have not reached the authoritative checkout."""

    blockers: list[dict[str, Any]] = []
    for workspace in store.workspaces(run_id):
        if workspace.state == "integrated":
            continue
        changed = workspace_changed_paths(workspace)
        if changed:
            blockers.append(
                {
                    "worker_key": workspace.worker_key,
                    "state": workspace.state,
                    "path": workspace.path,
                    "changed_paths": changed,
                }
            )
            continue
        if workspace.state in {"active", "blocked"} and not Path(workspace.path).is_dir():
            blockers.append(
                {
                    "worker_key": workspace.worker_key,
                    "state": workspace.state,
                    "path": workspace.path,
                    "changed_paths": [],
                    "reason": "workspace is unavailable, so recovery cannot be proven",
                }
            )
    return blockers


def plan_integration_order(workspaces: list[Workspace]) -> list[Workspace]:
    """Deterministic merge order, so a rerun integrates identically.

    Worker key is stable across phases and providers, which makes it the only
    ordering key that survives a handoff.
    """

    return sorted(workspaces, key=lambda item: (str(item.worker_key), str(item.run_id)))


def base_drift_paths(root: Path, workspace: Workspace, changed: list[str]) -> list[str]:
    """Paths the base checkout moved on since this worker forked.

    Found live: a worker forked at HEAD, Raghav then committed his own edit to
    the same file, and a three-way apply happily wrote conflict markers into
    his checkout. Divergence is a conflict to report, not a merge to attempt.

    The workspace baseline may be a synthetic commit containing dirty and
    untracked files. ``git diff <baseline>`` cannot compare those files to the
    live checkout because Git ignores their current untracked contents and
    reports them as deleted. Compare the baseline blob to the filesystem
    instead, byte for byte, so unchanged untracked work is accepted while a
    real edit after the fork still fails closed.
    """

    if not changed:
        return []
    drift: list[str] = []
    for raw_path in changed:
        path = normalise_claim_path(raw_path)
        try:
            baseline = _baseline_path_entry(root, workspace.base_head, path)
            current = _filesystem_path_entry(root, path)
        except (OSError, IsolationError, UnicodeError):
            # Uncertainty at the overwrite boundary is itself a conflict.
            drift.append(path)
            continue
        if baseline != current:
            drift.append(path)
    return sorted(set(drift))


def _baseline_path_entry(
    root: Path,
    base_head: str,
    path: str,
) -> tuple[str, bytes] | None:
    """Return the exact Git mode and blob bytes for one baseline path."""

    result = _git(
        root,
        "ls-tree",
        "-z",
        base_head,
        "--",
        path,
        check=False,
        text=False,
    )
    if result.returncode != 0:
        raise IsolationError(f"could not inspect baseline path {path}")
    record = bytes(result.stdout or b"").rstrip(b"\0")
    if not record:
        return None
    header, separator, _name = record.partition(b"\t")
    fields = header.split()
    if not separator or len(fields) != 3:
        raise IsolationError(f"baseline path metadata was malformed for {path}")
    mode = fields[0].decode("ascii", errors="strict")
    object_type = fields[1].decode("ascii", errors="strict")
    object_id = fields[2].decode("ascii", errors="strict")
    if object_type != "blob":
        raise IsolationError(f"baseline path is not a file or symlink: {path}")
    blob = _git(root, "cat-file", "blob", object_id, check=False, text=False)
    if blob.returncode != 0:
        raise IsolationError(f"could not read baseline content for {path}")
    return mode, bytes(blob.stdout)


def _filesystem_path_entry(root: Path, path: str) -> tuple[str, bytes] | None:
    """Return a Git-compatible mode and byte payload from the live checkout."""

    target = root / path
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if target.is_symlink():
        link = os.readlink(target).encode("utf-8", errors="surrogateescape")
        return "120000", link
    if not target.is_file():
        raise IsolationError(f"live checkout path is not a regular file: {path}")
    mode = "100755" if metadata.st_mode & 0o111 else "100644"
    return mode, target.read_bytes()


def _capture_paths(root: Path, paths: list[str]) -> dict[str, bytes | None]:
    """Byte-exact contents of these paths, so a failed apply can be undone."""

    captured: dict[str, bytes | None] = {}
    for path in paths:
        target = root / path
        try:
            captured[path] = target.read_bytes() if target.is_file() else None
        except OSError:
            captured[path] = None
    return captured


def _restore_paths(root: Path, captured: dict[str, bytes | None]) -> None:
    """Put every captured path back exactly as it was."""

    for path, payload in captured.items():
        target = root / path
        try:
            if payload is None:
                if target.is_file():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        except OSError:
            continue


def run_test_gate(
    root: Path | str, command: list[str] | None, *, timeout: int = 900
) -> dict[str, Any]:
    """Run the integration test gate and record what was actually observed."""

    if not command:
        return {"ran": False, "ok": True, "reason": "no test gate configured"}
    try:
        result = subprocess.run(
            list(command),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ran": False, "ok": False, "reason": f"test gate could not run: {error}"}
    tail = (result.stdout or "")[-2_000:] + (result.stderr or "")[-2_000:]
    return {
        "ran": True,
        "ok": result.returncode == 0,
        "command": list(command),
        "exit_code": result.returncode,
        "output_tail": tail,
    }


def run_test_gates(
    root: Path | str, commands: list[list[str]] | None, *, timeout: int = 900
) -> dict[str, Any]:
    """Run every integration gate command in order and stop at the first failure.

    A worker proves its slice in its own worktree, where its peers' changes do
    not exist yet. Re-running those same checks against the combined checkout is
    what catches the patch that was correct alone and wrong together, which is
    exactly the breakage that used to survive integration and get discovered a
    phase later by a reviewer reading code.
    """

    if not commands:
        return {"ran": False, "ok": True, "reason": "no test gate configured"}
    results: list[dict[str, Any]] = []
    for command in commands:
        result = run_test_gate(root, command, timeout=timeout)
        results.append(result)
        if not result.get("ok", False):
            return {
                "ran": True,
                "ok": False,
                "command": list(command),
                "exit_code": result.get("exit_code"),
                "output_tail": result.get("output_tail", ""),
                "reason": result.get("reason", ""),
                "commands": [list(item) for item in commands],
                "results": results,
            }
    return {
        "ran": True,
        "ok": True,
        "commands": [list(item) for item in commands],
        "results": results,
    }


@contextmanager
def repository_integration_lock(cwd: str | Path):
    """Serialize integration for one canonical repository across processes."""

    root = validate_repository_root(cwd).resolve()
    configured = os.environ.get("SERENA_FLEET_INTEGRATION_LOCK_ROOT", "").strip()
    state_root = os.environ.get("SERENA_FLEET_STATE_DIR", "").strip()
    lock_root = Path(
        configured
        or (Path(state_root) / "integration-locks" if state_root else DEFAULT_INTEGRATION_LOCK_ROOT)
    ).expanduser()
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def integrate_workspace(
    store: FleetIsolationStore,
    *,
    run_id: str,
    worker_key: str,
    cwd: str | Path,
    test_gate: list[str] | None = None,
    declared_tests: list[list[str]] | None = None,
    apply_changes: bool = True,
) -> IntegrationResult:
    """Run the entire integration transaction under the repository mutex."""

    try:
        with repository_integration_lock(cwd):
            return _integrate_workspace_locked(
                store,
                run_id=run_id,
                worker_key=worker_key,
                cwd=cwd,
                test_gate=test_gate,
                declared_tests=declared_tests,
                apply_changes=apply_changes,
            )
    except RepositoryResolutionError as error:
        return IntegrationResult(
            ok=False, run_id=run_id, worker_key=worker_key, reason=str(error)
        )


def _integrate_workspace_locked(
    store: FleetIsolationStore,
    *,
    run_id: str,
    worker_key: str,
    cwd: str | Path,
    test_gate: list[str] | None = None,
    declared_tests: list[list[str]] | None = None,
    apply_changes: bool = True,
) -> IntegrationResult:
    """Merge one worker's isolated work back, or refuse and say exactly why.

    Three gates, all fail-closed. The worker may only deliver paths it claimed.
    The base copies of those paths must still match the exact dirty snapshot
    the worker started from. And the configured test gate must pass. Anything
    else leaves the branch untouched so the operator still has the work.
    """

    workspace = store.get_workspace(run_id, worker_key)
    if workspace is None:
        return IntegrationResult(
            ok=False, run_id=run_id, worker_key=worker_key, reason="no workspace to integrate"
        )
    try:
        root = validate_repository_root(cwd)
    except RepositoryResolutionError as error:
        return IntegrationResult(
            ok=False, run_id=run_id, worker_key=worker_key, reason=str(error)
        )

    changed = workspace_changed_paths(workspace)
    if not changed:
        result = IntegrationResult(
            ok=True,
            run_id=run_id,
            worker_key=worker_key,
            reason="worker made no changes to integrate",
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="integrated", reason=result.reason
        )
        return result

    protected = [path for path in changed if is_protected_path(path)]
    if protected:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason="refusing to integrate protected paths: " + ", ".join(protected[:20]),
            changed_paths=changed,
            unclaimed_paths=protected,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    # Claims belong to the supervisor and must exist before the provider is
    # launched. Integration never invents ownership after seeing what a worker
    # changed; doing so made safety depend on provider sandbox permissions.
    if not store.active_claims(run_id, worker_key=worker_key):
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason="refusing integration because the supervisor ownership claim is missing",
            changed_paths=changed,
            unclaimed_paths=changed,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    patch = _workspace_patch(workspace, changed)
    if patch is None:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason="could not produce a reviewable patch for this workspace",
            changed_paths=changed,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result
    patch_path = _persist_patch(store, run_id, worker_key, patch)
    if not patch_path:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason="could not persist the recovery patch before integration",
            changed_paths=changed,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    unclaimed = store.unclaimed_paths(run_id=run_id, worker_key=worker_key, paths=changed)
    if unclaimed:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=(
                "refusing to integrate paths this worker never claimed: "
                + ", ".join(unclaimed[:20])
            ),
            changed_paths=changed,
            unclaimed_paths=unclaimed,
            patch_path=patch_path,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    drift = base_drift_paths(root, workspace, changed)
    if drift:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=(
                "the base checkout moved on since this worker forked; "
                "refusing to merge diverged paths: " + ", ".join(drift[:20])
            ),
            changed_paths=changed,
            dirty_conflicts=drift,
            patch_path=patch_path,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    # Plain apply, never --3way. A three-way apply can leave conflict markers
    # in the working tree, which is a corrupted checkout rather than a refused
    # merge. Plain apply is all-or-nothing and fails without touching a file.
    check = subprocess.run(
        ["git", "-C", str(root), "apply", "--check", "-"],
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=f"merge acceptance failed: {check.stderr.strip()[:400]}",
            changed_paths=changed,
            patch_path=patch_path,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    if not apply_changes:
        result = IntegrationResult(
            ok=True,
            run_id=run_id,
            worker_key=worker_key,
            reason="merge acceptance passed in preview mode; nothing was applied",
            changed_paths=changed,
            patch_path=patch_path,
        )
        store.record_integration(result)
        return result

    # Rollback evidence is captured before the tree moves, never after.
    rollback_ref = ""
    with suppress(GitSnapshotError, RepositoryResolutionError, OSError):
        snapshot = capture_git_snapshot(
            root,
            item_id=f"fleet-{run_id}-{worker_key}",
            label="pre-integration",
        )
        rollback_ref = snapshot.ref
    # Byte-exact safety net. Even an atomic apply gets verified, because a
    # half-written checkout is the one outcome this module may never produce.
    captured = _capture_paths(root, changed)
    applied = subprocess.run(
        ["git", "-C", str(root), "apply", "-"],
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        _restore_paths(root, captured)
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=f"integration failed while applying: {applied.stderr.strip()[:400]}",
            changed_paths=changed,
            rollback_ref=rollback_ref,
            patch_path=patch_path,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    # An explicitly configured repository gate outranks everything. Otherwise
    # the worker's own declared verification runs against the merged tree, and
    # only a worker that declared nothing runnable falls back to the whitespace
    # check that used to be the entire gate.
    if test_gate:
        gate = run_test_gate(root, test_gate)
    elif declared_tests:
        gate = run_test_gates(root, declared_tests)
    else:
        gate = run_test_gate(root, ["git", "diff", "--check", "--", *changed])
    if not gate.get("ok", False):
        reverted = subprocess.run(
            ["git", "-C", str(root), "apply", "-R", "-"],
            input=patch,
            capture_output=True,
            text=True,
            check=False,
        )
        if reverted.returncode != 0:
            _restore_paths(root, captured)
        result = IntegrationResult(
            ok=False,
            run_id=run_id,
            worker_key=worker_key,
            reason=(
                "test gate failed after integration; changes were rolled back"
                if reverted.returncode == 0
                else "test gate failed; changes were restored from the pre-integration capture"
            ),
            changed_paths=changed,
            rollback_ref=rollback_ref,
            patch_path=patch_path,
            test_gate=gate,
        )
        store.record_integration(result)
        store.mark_workspace(
            run_id=run_id, worker_key=worker_key, state="blocked", reason=result.reason
        )
        return result

    result = IntegrationResult(
        ok=True,
        run_id=run_id,
        worker_key=worker_key,
        reason="integrated with claim, dirty-base, and test gates satisfied",
        changed_paths=changed,
        rollback_ref=rollback_ref,
        patch_path=patch_path,
        test_gate=gate,
    )
    store.record_integration(result)
    store.mark_workspace(
        run_id=run_id,
        worker_key=worker_key,
        state="integrated",
        rollback_ref=rollback_ref or None,
        reason=result.reason,
    )
    return result


def rollback_integration(
    store: FleetIsolationStore, *, run_id: str, worker_key: str, cwd: str | Path
) -> dict[str, Any]:
    """Reverse a completed integration using its own recorded patch."""

    entries = [
        entry
        for entry in store.integrations(run_id)
        if entry.get("worker_key") == worker_key and entry.get("ok") and entry.get("patch_path")
    ]
    if not entries:
        return {"ok": False, "reason": "no applied integration to roll back"}
    patch_path = Path(str(entries[-1]["patch_path"]))
    if not patch_path.is_file():
        return {"ok": False, "reason": f"integration patch is missing: {patch_path}"}
    try:
        root = validate_repository_root(cwd)
    except RepositoryResolutionError as error:
        return {"ok": False, "reason": str(error)}
    patch = patch_path.read_text(encoding="utf-8", errors="surrogateescape")
    reverted = subprocess.run(
        ["git", "-C", str(root), "apply", "-R", "-"],
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if reverted.returncode != 0:
        return {
            "ok": False,
            "reason": (
                "rollback did not apply cleanly and the checkout was left "
                f"untouched: {reverted.stderr.strip()[:300]}"
            ),
        }
    store.mark_workspace(
        run_id=run_id, worker_key=worker_key, state="active", reason="integration rolled back"
    )
    return {"ok": True, "reason": "integration reversed", "patch_path": str(patch_path)}


def cleanup_workspace(
    store: FleetIsolationStore, *, run_id: str, worker_key: str, cwd: str | Path
) -> bool:
    """Remove a finished worktree without ever touching the base checkout."""

    workspace = store.get_workspace(run_id, worker_key)
    if workspace is None:
        return False
    try:
        root = validate_repository_root(cwd)
    except RepositoryResolutionError:
        return False
    removed = _git(root, "worktree", "remove", "--force", workspace.path, check=False)
    _git(root, "worktree", "prune", check=False)
    store.mark_workspace(
        run_id=run_id,
        worker_key=worker_key,
        state=workspace.state if workspace.state == "integrated" else "abandoned",
        reason="worktree removed",
    )
    return removed.returncode == 0


def _workspace_patch(workspace: Workspace, changed: list[str]) -> str | None:
    """One binary-safe patch carrying every change this worker made."""

    root = Path(workspace.path)
    if not root.is_dir():
        return None
    # Stage everything in the worktree so untracked new files are included, then
    # diff the base commit against that index without disturbing the checkout.
    index_file = root / ".git-serena-integration-index"
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index_file)
    try:
        if _git(root, "read-tree", workspace.base_head, env=environment, check=False).returncode != 0:
            return None
        if _git(root, "add", "-A", "--", ".", env=environment, check=False).returncode != 0:
            return None
        tree = _git(root, "write-tree", env=environment, check=False)
        if tree.returncode != 0:
            return None
        diff = _git(
            root,
            "diff",
            "--binary",
            workspace.base_head,
            tree.stdout.strip(),
            "--",
            *changed,
            env=environment,
            check=False,
        )
        if diff.returncode != 0:
            return None
        patch = diff.stdout
        return patch if patch and len(patch) <= MAX_PATCH_CHARS else (patch or None)
    finally:
        with suppress(OSError):
            index_file.unlink()


def _persist_patch(
    store: FleetIsolationStore, run_id: str, worker_key: str, patch: str
) -> str:
    directory = store.workspace_root / "patches" / (
        _BRANCH_TOKEN.sub("-", str(run_id)).strip("-") or "run"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{_BRANCH_TOKEN.sub('-', str(worker_key)).strip('-')}-{int(time.time())}.patch"
    try:
        target.write_text(patch, encoding="utf-8", errors="surrogateescape")
        if os.name != "nt":
            with suppress(OSError):
                target.chmod(0o600)
    except OSError:
        return ""
    return str(target)


def _workspace_from_row(row: sqlite3.Row) -> Workspace:
    return Workspace(
        run_id=str(row["run_id"]),
        worker_key=str(row["worker_key"]),
        path=str(row["path"]),
        branch=str(row["branch"]),
        base_head=str(row["base_head"]),
        state=str(row["state"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        integrated_at=float(row["integrated_at"]) if row["integrated_at"] else None,
        rollback_ref=row["rollback_ref"],
    )


def _identifier(value: object, name: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 128:
        raise ValueError(f"invalid Fleet isolation {name}")
    return clean


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]
