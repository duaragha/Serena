"""Mechanical contract for coding jobs accepted by Serena's resident brain.

The inbox is the durable queue. This module owns the parts that must be true
before an item can enter that queue: one validated Git root, a frozen model
policy, a structured brief, and a reproducible snapshot of the initial tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.coding_model_preferences import AUTO_MODEL, normalise_coding_model

SCHEMA_VERSION = 1
CODEX_MODEL = "gpt-5.6-sol"
CLAUDE_REVIEW_MODEL = "claude-opus-5"
CLAUDE_REVIEW_EFFORT = "xhigh"

# Implementation effort is a per-job decision, not a constant. Every job used
# to implement at the maximum tier, which means the model deliberated at full
# depth over routine orientation: on 2026-08-05 job a8bfddf8 spent 7.6 minutes
# and ninety Bash calls at xhigh before its first edit. The brain judges the
# job when it accepts it, ordinary work implements at high, and xhigh is kept
# for work it actually believes is hard. Review is not tiered; a cheaper
# reviewer is a different risk and is not what was asked for.
ROUTINE_COMPLEXITY = "routine"
NORMAL_COMPLEXITY = "normal"
ORDINARY_COMPLEXITY = "ordinary"
HARD_COMPLEXITY = "hard"
DEFAULT_COMPLEXITY = ORDINARY_COMPLEXITY
IMPLEMENT_EFFORT_BY_COMPLEXITY = {
    ROUTINE_COMPLEXITY: "high",
    NORMAL_COMPLEXITY: "high",
    ORDINARY_COMPLEXITY: "high",
    HARD_COMPLEXITY: "xhigh",
}
COMPLEXITY_TIERS = frozenset(IMPLEMENT_EFFORT_BY_COMPLEXITY)
DEFAULT_IMPLEMENT_EFFORT = IMPLEMENT_EFFORT_BY_COMPLEXITY[DEFAULT_COMPLEXITY]
# The ceiling, kept under its old name because the model policy still freezes
# against it. It is no longer what an ordinary job runs at.
CODEX_EFFORT = IMPLEMENT_EFFORT_BY_COMPLEXITY[HARD_COMPLEXITY]

DEFAULT_PROJECTS_ROOT = Path.home() / "Documents" / "Projects"
DEFAULT_SERENA_ROOT = DEFAULT_PROJECTS_ROOT / "serena"

_PATH = re.compile(r"(?:^|[\s'\"(])(?P<path>(?:~|/)[^\s'\"),;]+)")
_NORMALISE = re.compile(r"[^a-z0-9]+")
_GENERIC_PROJECT_ALIASES = frozenset({"app"})
_TEST_COMMAND = re.compile(
    r"(?:^|[\s'\"])(?:pytest|python\s+-m\s+pytest|npm\s+(?:run\s+)?test|"
    r"pnpm\s+(?:run\s+)?test|yarn\s+test|bun\s+test|cargo\s+test|"
    r"go\s+test|swift\s+test|gradle\w*\s+.*test|node\s+--test|"
    r"ruff\s+check|mypy|pyright|tsc\b|eslint\b)",
    re.IGNORECASE,
)
_LIVE_PROOF_COMMAND = re.compile(
    r"(?:^|[\s'\"])(?:curl\b|wget\b|systemctl\b|journalctl\b|smoke\b|"
    r"playwright\b|selenium\b|xdotool\b|busctl\b|dbus-send\b|"
    r"docker\s+(?:compose\s+)?(?:run|exec)|npm\s+run\s+(?:e2e|smoke))",
    re.IGNORECASE,
)
# Runtime probes are not limited to a handful of familiar binaries. A Python
# entrypoint can be the most direct safe proof, but treating every Python call
# as live would let static inspection or fixture generation satisfy the gate.
# Workers therefore label deliberate runtime evidence in the command itself.
# The marker only classifies a non-test command; exit status is still checked
# below, and tests cannot buy two kinds of evidence by adding the marker.
_LIVE_PROOF_MARKER = re.compile(r"\bSERENA_EVIDENCE_KIND=live\b", re.IGNORECASE)
_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})
# A shipped surface is not documentation because it is markup. An overlay HTML
# or stylesheet change is what Raghav actually looks at, so it never skips
# review on the strength of its file extension.
_RUNTIME_PREFIXES = (
    "core/",
    "voice/",
    "ui/",
    "desktop/",
    "systemd/",
    "config/",
    "integrations/",
    "tests/",
    "scripts/",
)
_SKIP_DIRECTORIES = frozenset(
    {
        "node_modules",
        "venv",
        ".venv",
        "target",
        "dist",
        "build",
        "vendor",
        "__pycache__",
        "site-packages",
    }
)
MAX_DISCOVERY_DEPTH = 3


class RepositoryResolutionError(ValueError):
    """The request did not resolve to exactly one valid Git repository."""


class GitSnapshotError(RuntimeError):
    """Git could not freeze or compare a job tree."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _normalise(value: object) -> str:
    return _NORMALISE.sub(" ", str(value or "").casefold()).strip()


def _json_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            value = decoded
        else:
            return [line.strip(" -*\t") for line in text.splitlines() if line.strip(" -*\t")]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_clean(item) for item in value if _clean(item)]
    clean = _clean(value)
    return [clean] if clean else []


def normalise_complexity(value: object) -> str:
    """Return the tier the brain asked for, or the ordinary default.

    Unreadable input never escalates. A job whose complexity nobody stated is
    an ordinary job, because assuming every unlabelled request is hard is how
    the old constant behaved.
    """

    tier = _clean(value).casefold()
    return tier if tier in COMPLEXITY_TIERS else DEFAULT_COMPLEXITY


def implement_effort_for(complexity: object) -> str:
    """Map a complexity tier to the effort the implement pass really runs at."""

    return IMPLEMENT_EFFORT_BY_COMPLEXITY[normalise_complexity(complexity)]


def frozen_implement_effort(brief: Mapping[str, Any] | None) -> str:
    """The effort one accepted brief really runs at.

    A brief that names its tier is decided by that tier, and the caller still
    checks the frozen effort agrees with it. A brief accepted before effort was
    tiered names no tier and froze the old ceiling, so it is decided by what it
    froze. Work already sitting in the queue when this shipped has to keep
    running; failing it would turn a speed change into an outage.
    """

    data = brief or {}
    policy = data.get("model_policy")
    if isinstance(policy, Mapping):
        implement = policy.get("implement")
        frozen = (
            _clean(implement.get("effort")).casefold()
            if isinstance(implement, Mapping)
            else ""
        )
        if frozen in set(IMPLEMENT_EFFORT_BY_COMPLEXITY.values()):
            return frozen
    if "complexity" in data:
        return implement_effort_for(data.get("complexity"))
    frozen = _clean(data.get("codex_effort")).casefold()
    if frozen in set(IMPLEMENT_EFFORT_BY_COMPLEXITY.values()):
        return frozen
    return DEFAULT_IMPLEMENT_EFFORT


def _git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    text: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[Any]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=text,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise GitSnapshotError(
            f"git {' '.join(args)} failed with {result.returncode}: {_clean(stderr)[:500]}"
        )
    return result


def validate_repository_root(candidate: str | Path) -> Path:
    """Return the canonical Git top level or reject the candidate."""

    path = Path(candidate).expanduser()
    if path.is_file():
        path = path.parent
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise RepositoryResolutionError(f"project path does not exist: {path}") from exc
    if not path.is_dir():
        raise RepositoryResolutionError(f"project path is not a directory: {path}")
    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RepositoryResolutionError(f"project is not a Git repository: {path}")
    try:
        root = Path(result.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise RepositoryResolutionError("Git returned an invalid repository root") from exc
    if not root.is_dir():
        raise RepositoryResolutionError("Git returned an invalid repository root")
    return root


def discover_repository_roots(projects_root: Path = DEFAULT_PROJECTS_ROOT) -> list[Path]:
    """Discover local project roots without treating an ordinary directory as one.

    Bounded on purpose. This runs synchronously inside the brain's tool call
    while he is waiting on a live call, so a recursive descent through every
    node_modules, .venv and build tree under Projects is not acceptable: stop at
    three levels, skip the known heavy directories, and stop descending as soon
    as a directory turns out to be a repository root.
    """

    if not projects_root.is_dir():
        return []
    roots: set[Path] = set()
    frontier: list[tuple[Path, int]] = [(projects_root, 0)]
    while frontier:
        directory, depth = frontier.pop()
        if (directory / ".git").exists():
            try:
                roots.add(validate_repository_root(directory))
                continue
            except RepositoryResolutionError:
                pass
        if depth >= MAX_DISCOVERY_DEPTH:
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP_DIRECTORIES:
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            frontier.append((entry, depth + 1))
    return sorted(roots, key=lambda path: str(path).casefold())


def resolve_repository_root(
    request: str,
    *,
    project_hint: str = "",
    project_context: Iterable[str] = (),
    roots: Iterable[Path] | None = None,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    serena_root: Path = DEFAULT_SERENA_ROOT,
) -> Path:
    """Resolve exactly one repository from explicit paths, context, and aliases.

    There is deliberately no current-directory or HOME fallback. An unresolved
    or ambiguous request must go back to the resident brain for one question.
    """

    search_text = "\n".join(
        part for part in [str(request or ""), str(project_hint or ""), *map(str, project_context)] if part
    )
    explicit: set[Path] = set()
    explicit_errors: list[str] = []
    for match in _PATH.finditer(search_text):
        raw = match.group("path")
        try:
            explicit.add(validate_repository_root(raw))
        except RepositoryResolutionError as error:
            explicit_errors.append(str(error))
    if len(explicit) == 1:
        return next(iter(explicit))
    if len(explicit) > 1:
        names = ", ".join(sorted(path.name for path in explicit))
        raise RepositoryResolutionError(f"more than one Git project matches: {names}")
    # A path-shaped token that is not a repository is not a refusal on its own.
    # "fix the /api/health route in serena" names one real project and one route,
    # so alias scoring still gets to run; the failed path only colours the final
    # message if nothing else resolves.

    available = list(roots) if roots is not None else discover_repository_roots(projects_root)
    canonical: dict[Path, set[str]] = {}
    for candidate in available:
        try:
            root = validate_repository_root(candidate)
        except RepositoryResolutionError:
            continue
        aliases = {_normalise(root.name)}
        try:
            aliases.add(_normalise(root.relative_to(projects_root)))
        except ValueError:
            aliases.add(_normalise(root))
        canonical[root] = {
            alias for alias in aliases if alias and alias not in _GENERIC_PROJECT_ALIASES
        }

    normalised = f" {_normalise(search_text)} "
    scored: list[tuple[int, Path]] = []
    for root, aliases in canonical.items():
        score = max((len(alias) for alias in aliases if f" {alias} " in normalised), default=0)
        if score:
            scored.append((score, root))
    if not scored and any(
        phrase in normalised
        for phrase in (
            " serena ",
            " voice ",
            " wake word ",
            " dot display ",
            " brain daemon ",
            " coding pane ",
            " coding panel ",
            " code pane ",
            " code panel ",
            " coding app ",
            " chats app ",
            " voice work ",
            " fleet tab ",
            " dot overlay ",
        )
    ):
        try:
            return validate_repository_root(serena_root)
        except RepositoryResolutionError:
            pass
    if not scored:
        if explicit_errors:
            raise RepositoryResolutionError(explicit_errors[0])
        raise RepositoryResolutionError(
            "i need the project name or Git repository path before coding can start"
        )
    winners = sorted({root for _score, root in scored}, key=str)
    if len(winners) != 1:
        names = ", ".join(path.name for path in winners)
        raise RepositoryResolutionError(f"which Git project: {names}?")
    return winners[0]


def _path_digest(path: Path) -> str:
    try:
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        else:
            payload = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(payload).hexdigest()


def _nul_paths(payload: bytes) -> list[str]:
    return [part.decode("utf-8", errors="surrogateescape") for part in payload.split(b"\0") if part]


def _worktree_tree(root: Path) -> str:
    descriptor, index_name = tempfile.mkstemp(prefix="serena-job-index-")
    os.close(descriptor)
    with contextlib_suppress(FileNotFoundError):
        os.unlink(index_name)
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = index_name
    try:
        _git(root, "read-tree", "HEAD", env=environment)
        _git(root, "add", "-A", "--", ".", env=environment)
        return _git(root, "write-tree", env=environment).stdout.strip()
    finally:
        with contextlib_suppress(FileNotFoundError):
            os.unlink(index_name)


class contextlib_suppress:
    """Tiny local suppressor to keep this module dependency-light."""

    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        return bool(exc_type and issubclass(exc_type, self.exceptions))


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    root: str
    tree: str
    head: str
    branch: str
    status: str
    tracked_patch: str
    dirty_paths: list[str]
    untracked_hashes: dict[str, str]
    captured_at: float
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def prune_job_refs(root: Path, item_id: str, *, keep: Iterable[str] = ()) -> list[str]:
    """Drop the superseded snapshot refs this job wrote into his live repository.

    Every capture writes refs/serena/jobs/<item>/<label>, and nothing used to
    remove them. Keep the frozen baseline, because a resume still diffs against
    it, and the ref just written, because the evidence for the current state is
    derived from it. Everything in between is scaffolding.
    """

    safe_item = re.sub(r"[^A-Za-z0-9._-]+", "-", str(item_id)).strip("-") or "unknown"
    prefix = f"refs/serena/jobs/{safe_item}/"
    protected = {str(value) for value in keep}
    listing = _git(root, "for-each-ref", "--format=%(refname)", prefix + "*", check=False)
    if listing.returncode != 0:
        return []
    removed: list[str] = []
    for line in listing.stdout.splitlines():
        ref = line.strip()
        if not ref.startswith(prefix) or ref in protected:
            continue
        if _git(root, "update-ref", "-d", ref, check=False).returncode == 0:
            removed.append(ref)
    return removed


def capture_git_snapshot(root: Path, *, item_id: str, label: str) -> GitSnapshot:
    """Freeze the current tracked and untracked worktree in a durable Git tree."""

    root = validate_repository_root(root)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    tracked_patch = _git(root, "diff", "--binary", "HEAD", "--").stdout
    dirty = _nul_paths(
        _git(
            root,
            "ls-files",
            "-z",
            "--modified",
            "--deleted",
            "--others",
            "--exclude-standard",
            text=False,
        ).stdout
    )
    untracked = _nul_paths(
        _git(root, "ls-files", "-z", "--others", "--exclude-standard", text=False).stdout
    )
    untracked_hashes = {path: _path_digest(root / path) for path in untracked}
    tree = _worktree_tree(root)
    safe_item = re.sub(r"[^A-Za-z0-9._-]+", "-", str(item_id)).strip("-") or "unknown"
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label)).strip("-") or "snapshot"
    ref = f"refs/serena/jobs/{safe_item}/{safe_label}"
    _git(root, "update-ref", ref, tree)
    prune_job_refs(root, item_id, keep={ref, f"refs/serena/jobs/{safe_item}/baseline"})
    return GitSnapshot(
        root=str(root),
        tree=tree,
        head=head,
        branch=branch,
        status=status,
        tracked_patch=tracked_patch,
        dirty_paths=sorted(set(dirty)),
        untracked_hashes=untracked_hashes,
        captured_at=time.time(),
        ref=ref,
    )


@dataclass(frozen=True, slots=True)
class CodingJobBrief:
    item_id: str
    exact_request: str
    triggering_request: str
    relevant_conversation: list[str]
    project_root: str
    project_context: list[str]
    memory_guidance: list[str]
    ledger_guidance: list[str]
    handoff_guidance: list[str]
    requested_outcome: str
    acceptance_criteria: list[str]
    authority_boundaries: list[str]
    commit_authorized: bool
    initial_git: dict[str, Any]
    likely_files: list[str] = field(default_factory=list)
    complexity: str = DEFAULT_COMPLEXITY
    risk: str = "normal"
    coding_lane: str = "normal"
    work_route: dict[str, Any] = field(default_factory=dict)
    coding_model: str = AUTO_MODEL
    implement_provider: str = "codex"
    implement_model: str = CODEX_MODEL
    implement_effort: str = DEFAULT_IMPLEMENT_EFFORT
    review_provider: str = "claude"
    codex_model: str = CODEX_MODEL
    codex_effort: str = DEFAULT_IMPLEMENT_EFFORT
    review_model: str = CLAUDE_REVIEW_MODEL
    review_effort: str = CLAUDE_REVIEW_EFFORT
    model_policy: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    accepted_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        *,
        item_id: str,
        exact_request: str,
        triggering_request: str,
        project_root: Path,
        initial_git: GitSnapshot,
        context: Mapping[str, object] | None = None,
        work_route: Mapping[str, object] | None = None,
    ) -> CodingJobBrief:
        supplied = dict(context or {})
        acceptance = _json_list(supplied.get("acceptance_criteria")) or [
            "the requested outcome is implemented in the resolved repository",
            "the scoped baseline-to-final diff and changed files are recorded mechanically",
            "proportionate verification commands complete successfully with exit codes",
            "pre-existing unrelated dirty work remains unchanged",
        ]
        boundaries = _json_list(supplied.get("authority_boundaries")) or [
            "work only inside the resolved repository",
            "preserve unrelated tracked and untracked user changes",
            "do not commit, push, deploy, or contact anyone unless the triggering request says so",
            "stop truthfully when required authority or evidence is missing",
        ]
        complexity = normalise_complexity(supplied.get("complexity"))
        selected_model = normalise_coding_model(
            supplied.get("coding_model"), strict=True
        )
        model_policy = dict(supplied.get("model_policy") or {})
        if not model_policy:
            from core.serena_policy import classify_risk, resolve_policy

            risk, _risk_reason = classify_risk(
                exact_request,
                paths=_json_list(supplied.get("likely_files")),
                explicit=supplied.get("risk"),
            )
            implement = resolve_policy(
                "coding",
                activity="implement",
                complexity=complexity,
                risk=risk,
                role="implement",
                manual_override=selected_model,
            )
            review = resolve_policy(
                "coding",
                activity="review",
                complexity=complexity,
                risk=risk,
                role="review",
            )
            model_policy = {
                "usable": True,
                "reason": f"{implement.reason}; {review.reason}",
                "implement": {
                    "provider": implement.provider,
                    "model": implement.model,
                    "effort": implement.effort,
                },
                "review": {
                    "provider": review.provider,
                    "model": review.model,
                    "effort": review.effort,
                },
                "lane": implement.lane,
                "risk": implement.risk,
                "selected_model": selected_model,
                "fallback_reason": "",
                "policy_version": implement.policy_version,
                "implement_decision": implement.as_dict(),
                "review_decision": review.as_dict(),
                "capacity": {},
            }
        implement_policy = dict(model_policy.get("implement") or {})
        review_policy = dict(model_policy.get("review") or {})
        return cls(
            item_id=item_id,
            exact_request=str(exact_request),
            triggering_request=str(triggering_request),
            relevant_conversation=_json_list(supplied.get("relevant_conversation")),
            project_root=str(project_root),
            project_context=_json_list(supplied.get("project_context")),
            memory_guidance=_json_list(supplied.get("memory_guidance")),
            ledger_guidance=_json_list(supplied.get("ledger_guidance")),
            handoff_guidance=_json_list(supplied.get("handoff_guidance")),
            requested_outcome=_clean(supplied.get("requested_outcome")) or _clean(exact_request),
            acceptance_criteria=acceptance,
            authority_boundaries=boundaries,
            commit_authorized=supplied.get("commit_authorized") is True,
            initial_git=initial_git.to_dict(),
            likely_files=_json_list(supplied.get("likely_files")),
            complexity=complexity,
            risk=str(model_policy.get("risk") or "normal"),
            coding_lane=str(model_policy.get("lane") or "normal"),
            coding_model=selected_model,
            implement_provider=str(implement_policy.get("provider") or "codex"),
            implement_model=str(implement_policy.get("model") or CODEX_MODEL),
            implement_effort=str(
                implement_policy.get("effort") or implement_effort_for(complexity)
            ),
            review_provider=str(review_policy.get("provider") or "claude"),
            codex_effort=str(
                implement_policy.get("effort") or implement_effort_for(complexity)
            ),
            review_model=str(review_policy.get("model") or CLAUDE_REVIEW_MODEL),
            review_effort=str(review_policy.get("effort") or CLAUDE_REVIEW_EFFORT),
            model_policy=model_policy,
            work_route=dict(work_route or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# What a worker prompt needs from the frozen snapshot: the tree ids it must not
# lose and the paths it must not touch. Not the payloads.
_PROMPT_GIT_KEYS = frozenset({"root", "tree", "head", "branch", "ref", "captured_at"})
MAX_PROMPT_DIRTY_PATHS = 200
MAX_PROMPT_LIKELY_FILES = 25
# Said to the worker in the brief itself, not just in a tool description the
# worker never sees. A guess presented as fact is worse than no guess: it sends
# the worker down a false path it then defends. Presented as a guess, a wrong
# entry costs one Read.
LIKELY_FILES_CAVEAT = (
    "UNVERIFIED STARTING POINT, NOT FACT. Serena assembled this from memory, "
    "past conversations, the ledger and git before you started. Open these "
    "first instead of searching the repository blind. Any entry may be wrong, "
    "stale, or renamed: confirm each one by reading it, drop the ones that do "
    "not match, and search normally for whatever is missing. Never cite this "
    "list as evidence and never assume a path here exists."
)


def prompt_brief(brief: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project an accepted brief down to what belongs in a worker prompt.

    The durable snapshot keeps the whole baseline patch because the evidence
    math needs to reproduce it. Handing that to a worker is a different thing:
    on this repository the baseline patch alone is roughly 370KB of unrelated
    dirty work, so every job would open with a third of a megabyte of noise
    before the worker read a word of the actual request. Keep the tree ids and
    the paths to preserve, drop the payloads.

    The file map is rewritten here rather than passed through, so the caveat
    travels attached to the paths and cannot be read apart from them.
    """

    projected = dict(brief or {})
    paths = [str(path) for path in projected.get("likely_files") or []]
    if paths:
        projected["likely_files"] = {
            "status": LIKELY_FILES_CAVEAT,
            "paths": paths[:MAX_PROMPT_LIKELY_FILES],
        }
    else:
        projected.pop("likely_files", None)
    initial = projected.get("initial_git")
    if not isinstance(initial, Mapping):
        return projected
    trimmed = {key: value for key, value in initial.items() if key in _PROMPT_GIT_KEYS}
    dirty = [str(path) for path in initial.get("dirty_paths") or []]
    untracked = initial.get("untracked_hashes") or {}
    trimmed["dirty_paths"] = dirty[:MAX_PROMPT_DIRTY_PATHS]
    trimmed["dirty_path_count"] = len(dirty)
    trimmed["untracked_count"] = len(untracked) if isinstance(untracked, Mapping) else 0
    trimmed["patch_omitted"] = True
    projected["initial_git"] = trimmed
    return projected


def _documentation_only(paths: Sequence[str]) -> bool:
    """True only when nothing shipped changed.

    Path first, extension second. `voice/desktop/renderer/index.html` and
    `ui/static/app.css` are the overlay he actually looks at, not prose, so a
    markup extension alone must never buy a skipped review.
    """

    if not paths:
        return False
    for raw in paths:
        path = str(raw)
        lowered = path.casefold()
        if lowered.startswith("docs/"):
            continue
        if lowered.startswith(_RUNTIME_PREFIXES):
            return False
        if Path(path).suffix.casefold() not in _DOC_SUFFIXES:
            return False
    return True


def _unresolved_commands(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Commands whose last recorded run did not exit clean.

    Failing a test, fixing the defect, and rerunning the same test green is the
    normal shape of real work, so a later clean run of that command resolves
    it. A different command exiting zero afterwards does not.
    """

    last: dict[str, Any] = {}
    for entry in entries:
        last[str(entry.get("command") or "")] = entry.get("exit_code")
    return [command for command, code in last.items() if code != 0]


def _latest_commands(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last observed result for each exact verification command."""

    last: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        last[str(entry.get("command") or "")] = (index, entry)
    return [entry for _index, entry in sorted(last.values(), key=lambda pair: pair[0])]


def _is_live_proof_command(command: str) -> bool:
    if _LIVE_PROOF_COMMAND.search(command):
        return True
    return bool(
        _LIVE_PROOF_MARKER.search(command) and not _TEST_COMMAND.search(command)
    )


def scoped_git_evidence(
    brief: Mapping[str, Any],
    *,
    commands: Sequence[Mapping[str, Any]],
    final_snapshot: GitSnapshot,
) -> dict[str, Any]:
    """Derive completion evidence from frozen trees and observed commands."""

    root = validate_repository_root(str(brief.get("project_root") or ""))
    initial = brief.get("initial_git") or {}
    baseline_tree = str(initial.get("tree") or "")
    if not baseline_tree:
        raise GitSnapshotError("accepted brief has no baseline tree")
    diff = _git(root, "diff", "--binary", "--find-renames", baseline_tree, final_snapshot.tree, "--").stdout
    changed = _nul_paths(
        _git(root, "diff", "--name-only", "-z", baseline_tree, final_snapshot.tree, "--", text=False).stdout
    )
    changed_files = sorted(set(changed))

    normalized_commands: list[dict[str, Any]] = []
    for entry in commands:
        command = str(entry.get("command") or "").strip()
        if not command:
            continue
        code = entry.get("exit_code")
        try:
            exit_code = int(code) if code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        normalized_commands.append(
            {
                "command": command,
                "exit_code": exit_code,
                "output": str(entry.get("output") or "")[-8_000:],
            }
        )
    latest_commands = _latest_commands(normalized_commands)
    tests = [entry for entry in latest_commands if _TEST_COMMAND.search(entry["command"])]
    live = [entry for entry in latest_commands if _is_live_proof_command(entry["command"])]

    baseline_dirty = sorted(set(map(str, initial.get("dirty_paths") or [])))
    scoped = set(changed_files)
    unrelated_dirty = [
        {"path": path, "preserved": True}
        for path in baseline_dirty
        if path not in scoped
    ]
    overlapping_dirty = sorted(path for path in baseline_dirty if path in scoped)
    commit_state = {
        "baseline_head": str(initial.get("head") or ""),
        "final_head": final_snapshot.head,
        "baseline_branch": str(initial.get("branch") or ""),
        "final_branch": final_snapshot.branch,
        "head_changed": final_snapshot.head != str(initial.get("head") or ""),
    }

    doc_only = _documentation_only(changed_files)
    acceptance_text = " ".join(map(str, brief.get("acceptance_criteria") or [])).casefold()
    live_required = any(
        path.startswith(("voice/", "desktop/", "ui/", "systemd/", "config/runtime"))
        or path.endswith((".service", ".socket", ".timer"))
        for path in changed_files
    ) or any(term in acceptance_text for term in ("live proof", "end-to-end", "end to end", "e2e"))
    unresolved_tests = _unresolved_commands(tests)
    unresolved_live = _unresolved_commands(live)
    errors: list[str] = []
    if unresolved_tests:
        errors.append(
            "a recorded test command never exited clean: " + unresolved_tests[0][:200]
        )
    if changed_files and not doc_only and not tests:
        errors.append("code changed without a recorded test command and exit code")
    if not changed_files and not any(entry["exit_code"] == 0 for entry in [*tests, *live]):
        errors.append("no scoped change and no successful test or proof command was recorded")
    if live_required and not live:
        errors.append("runtime-sensitive changes lack a recorded live proof command and exit code")
    if unresolved_live:
        errors.append(
            "a recorded live proof command never exited clean: " + unresolved_live[0][:200]
        )
    if commit_state["head_changed"] and brief.get("commit_authorized") is not True:
        errors.append("repository HEAD changed without explicit commit authority")

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_tree": baseline_tree,
        "final_tree": final_snapshot.tree,
        "baseline_status": str(initial.get("status") or ""),
        "final_status": final_snapshot.status,
        "scoped_diff": diff,
        "changed_files": changed_files,
        "commands": normalized_commands,
        "tests": tests,
        "tests_not_required": doc_only,
        "live_proof": live,
        "live_proof_required": live_required,
        "live_proof_not_required": not live_required,
        "commit_state": commit_state,
        "unrelated_dirty_changes": unrelated_dirty,
        "overlapping_initial_dirty": overlapping_dirty,
        # Both the frozen trees and the dirty listing honour .gitignore, so an
        # edit to .env or a local-only config produces no scoped diff here. Say
        # that in the payload rather than implying the coverage is total.
        "ignored_paths_covered": False,
        "complete": not errors,
        "errors": errors,
        "captured_at": time.time(),
    }


def review_required(evidence: Mapping[str, Any]) -> tuple[bool, str]:
    """Decide review from the scoped diff and evidence, never worker prose."""

    changed = [str(path) for path in evidence.get("changed_files") or []]
    if evidence.get("errors"):
        return True, "mechanical evidence contains anomalies"
    if evidence.get("overlapping_initial_dirty"):
        return True, "the scoped diff overlaps pre-existing dirty files"
    if not changed:
        return False, "no files changed"
    if _documentation_only(changed):
        return False, "documentation-only scoped diff"
    return True, "production, runtime, configuration, or executable test files changed"
