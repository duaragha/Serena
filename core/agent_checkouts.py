"""Private checkouts for dispatched work, and the one path work leaves them by.

The Projects tree is live-synced between Raghav's machines, `.git` included.
An agent editing that tree on the dispatcher would push half-finished changes
straight into whatever he has open on the laptop. So dispatched work never
touches it: the synced repository is only consulted for its GitHub remote, and
every task gets its own worktree of a private clone that no sync tool watches.

Work leaves by exactly one door: a commit on `serena/task-<id>`, pushed, with a
pull request. Merging is opt-in per repository. Nothing here writes to a
default branch directly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 600
GH_TIMEOUT_SECONDS = 120
DEFAULT_AUTHOR = ("Serena", "serena@users.noreply.github.com")
JUNK_PATHS = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**", "**/node_modules/**",
              "**/.DS_Store")
_GITHUB = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class CheckoutError(RuntimeError):
    """The task cannot get a safe private checkout, or cannot be delivered."""


def repos_dir() -> Path:
    configured = os.environ.get("SERENA_AGENT_REPOS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "serena" / "agent-repos"


def dispatch_config() -> dict:
    """Operator settings: which repositories may merge without him."""

    path = Path(os.environ.get(
        "SERENA_DISPATCH_CONFIG",
        str(Path.home() / ".config" / "serena" / "dispatch.json"),
    )).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = GIT_TIMEOUT_SECONDS,
         check: bool = True) -> subprocess.CompletedProcess:
    executable = shutil.which(args[0])
    if not executable:
        raise CheckoutError(f"{args[0]} is not installed on the dispatcher")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GH_PROMPT_DISABLED", "1")
    try:
        result = subprocess.run(
            [executable, *args[1:]], cwd=str(cwd) if cwd else None, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckoutError(f"{args[0]} {args[1]} timed out") from error
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        raise CheckoutError(f"{args[0]} {args[1]} failed: {detail[0][:300]}")
    return result


def _git(repo: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT_SECONDS):
    return _run(["git", "-C", str(repo), *args], check=check, timeout=timeout)


def github_slug(url: str) -> tuple[str, str]:
    match = _GITHUB.match(url.strip())
    if not match:
        raise CheckoutError("repository has no GitHub remote to deliver through")
    return match.group("owner"), match.group("repo")


def github_remote(source: Path) -> str:
    """The canonical https remote of a synced repository, read-only."""

    # Raw config values: `remote get-url` applies insteadOf rewrites, which
    # would hide the GitHub identity behind a mirror or local path.
    urls = []
    origin = _git(source, "config", "--get", "remote.origin.url", check=False)
    if origin.returncode == 0:
        urls.append(origin.stdout.strip())
    listing = _git(source, "config", "--get-regexp", r"^remote\..*\.url$", check=False)
    urls.extend(line.split()[1] for line in listing.stdout.splitlines() if len(line.split()) >= 2)
    for url in urls:
        try:
            owner, repo = github_slug(url)
        except CheckoutError:
            continue
        return f"https://github.com/{owner}/{repo}.git"
    raise CheckoutError(f"{source.name} has no GitHub remote to deliver through")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _default_branch(base: Path) -> str:
    head = _git(base, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if head.returncode != 0:
        _git(base, "remote", "set-head", "origin", "--auto")
        head = _git(base, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return head.stdout.strip().split("/", 1)[1]


@dataclass(frozen=True)
class TaskCheckout:
    path: Path
    base: Path
    remote: str
    branch: str
    default_branch: str
    # True when the base could not be refreshed from GitHub, so this worktree
    # branches from whatever origin/<default> was last fetched.
    stale_base: bool = False


def task_branch(task_id: int) -> str:
    return f"serena/task-{int(task_id)}"


def prepare(source: Path, task_id: int, *, projects_root: Path | None = None) -> TaskCheckout:
    """A fresh worktree of the remote default branch, private to one task."""

    from core.coding_job_contract import DEFAULT_PROJECTS_ROOT

    root = repos_dir()
    synced = projects_root or DEFAULT_PROJECTS_ROOT
    if _inside(root, synced):
        raise CheckoutError("agent checkouts must live outside the synced Projects tree")
    remote = github_remote(source)
    owner, repo = github_slug(remote)
    base = root / f"{owner}__{repo}"
    if not (base / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", remote, str(base)])
        stale = False
    else:
        # Fetching keeps the base current, and it is the only step here that
        # needs credentials. Failing it used to block the task before any work
        # started, which is how expired GitHub auth turned into a queue that
        # silently never ran. An existing origin/<default> is a real commit to
        # branch from, so an unreachable remote degrades to a stale base rather
        # than a dead queue. Delivery still needs auth and still says so.
        fetched = _git(base, "fetch", "--prune", "--quiet", "origin", check=False)
        stale = fetched.returncode != 0
        _git(base, "worktree", "prune")
    default = _default_branch(base)
    branch = task_branch(task_id)
    path = root / f"{owner}__{repo}.task-{int(task_id)}"
    if path.exists():
        # A crash between preparing and dispatching leaves this behind. It was
        # never handed to Fleet, so starting it again from the remote is safe.
        _git(base, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    _git(base, "worktree", "add", "--force", "-B", branch, str(path), f"origin/{default}")
    return TaskCheckout(path=path, base=base, remote=remote, branch=branch,
                        default_branch=default, stale_base=stale)


def locate(checkout_path: str | Path) -> TaskCheckout:
    """Rebuild the checkout record from the directory Fleet ran in."""

    path = Path(checkout_path)
    if not _inside(path, repos_dir()):
        raise CheckoutError("run directory is not a private agent checkout")
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    base = Path(common).parent
    remote = github_remote(base)
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return TaskCheckout(path=path, base=base, remote=remote, branch=branch,
                        default_branch=_default_branch(base))


@dataclass(frozen=True)
class Delivery:
    status: str  # "pr", "merged", "no_changes"
    url: str = ""
    detail: str = ""


def _identity(path: Path) -> list[str]:
    name = _git(path, "config", "user.name", check=False).stdout.strip()
    email = _git(path, "config", "user.email", check=False).stdout.strip()
    return [] if name and email else [
        "-c", f"user.name={DEFAULT_AUTHOR[0]}", "-c", f"user.email={DEFAULT_AUTHOR[1]}",
    ]


def deliver(checkout: TaskCheckout, *, task_id: int, brief: str, run_id: str) -> Delivery:
    """Commit what Fleet left, push the task branch, open (and maybe merge) a PR."""

    path = checkout.path
    if checkout.branch != task_branch(task_id):
        raise CheckoutError(f"refusing to deliver from unexpected branch {checkout.branch}")
    # Build caches never ship, even from a repository that forgot to ignore
    # them: a test run inside the worktree is enough to create them.
    _git(path, "add", "-A", "--", ".", *(f":(exclude,glob){pattern}" for pattern in JUNK_PATHS))
    title_line = " ".join(brief.split())[:72] or f"task {task_id}"
    staged = _git(path, "diff", "--cached", "--quiet", check=False).returncode != 0
    if staged:
        _run(["git", "-C", str(path), *_identity(path), "commit", "--quiet",
              "-m", f"serena: {title_line}",
              "-m", f"Dispatched task #{task_id}, Fleet run {run_id}."])
    ahead = _git(path, "rev-list", "--count",
                 f"origin/{checkout.default_branch}..HEAD").stdout.strip()
    if ahead == "0":
        return Delivery("no_changes", detail="the run finished without changing the repository")
    bump_app_version(checkout)
    _git(path, "push", "--quiet", "--force-with-lease", "-u", "origin",
         f"HEAD:refs/heads/{checkout.branch}")
    owner, repo = github_slug(checkout.remote)
    slug = f"{owner}/{repo}"
    existing = _run(["gh", "pr", "view", checkout.branch, "--repo", slug,
                     "--json", "url,state", "-q", ".url + \" \" + .state"],
                    timeout=GH_TIMEOUT_SECONDS, check=False)
    url = ""
    if existing.returncode == 0 and existing.stdout.strip():
        url, _, state = existing.stdout.strip().partition(" ")
        if state == "MERGED":
            return Delivery("merged", url=url)
    if not url:
        body = (
            f"Dispatched from Serena task #{task_id} (Fleet run `{run_id}`).\n\n"
            f"**Brief**\n\n{brief.strip()[:3000]}\n"
        )
        created = _run(["gh", "pr", "create", "--repo", slug, "--head", checkout.branch,
                        "--base", checkout.default_branch, "--title", f"serena: {title_line}",
                        "--body", body], timeout=GH_TIMEOUT_SECONDS)
        url = created.stdout.strip().splitlines()[-1]
    automerge = {str(item).lower() for item in dispatch_config().get("automerge_repos", [])}
    if slug.lower() in automerge:
        merged = _run(["gh", "pr", "merge", checkout.branch, "--repo", slug, "--squash",
                       "--delete-branch=false"], timeout=GH_TIMEOUT_SECONDS, check=False)
        if merged.returncode == 0:
            return Delivery("merged", url=url)
        return Delivery("pr", url=url, detail="auto-merge was refused; review it")
    return Delivery("pr", url=url)


def _codemagic_token() -> str:
    token = os.environ.get("CODEMAGIC_API_TOKEN", "").strip()
    if token:
        return token
    path = Path.home() / ".config" / "serena" / "codemagic.env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "CODEMAGIC_API_TOKEN":
                return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def _ship_rule(checkout: TaskCheckout) -> dict:
    owner, repo = github_slug(checkout.remote)
    rules = {str(k).lower(): v for k, v in (dispatch_config().get("ship") or {}).items()}
    rule = rules.get(f"{owner}/{repo}".lower())
    return rule if isinstance(rule, dict) else {}


def _touches_app(checkout: TaskCheckout, rule: dict) -> bool:
    paths = [str(p) for p in rule.get("paths") or [] if str(p).strip()]
    if not paths:
        return True
    changed = _git(checkout.path, "diff", "--name-only",
                   f"origin/{checkout.default_branch}...HEAD", check=False).stdout.split()
    return any(name.startswith(prefix) for name in changed for prefix in paths)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(version)))


def bump_app_version(checkout: TaskCheckout) -> str:
    """Raise the app's version above the default branch's, so SideStore sees an update.

    SideStore only offers an install when the version string goes up, so a
    merged app change that forgot to bump never reaches his phone. Nobody
    should have to remember it: when the repository's ship rule names an
    ``expo_app_json`` and the change touches the app, the task branch gets a
    version one patch above whatever the default branch has *now*, and a
    build number one above it. A branch that already bumped past it is left alone.
    Returns the new version, or "" when nothing was bumped.
    """

    rule = _ship_rule(checkout)
    relative = str(rule.get("expo_app_json") or "").strip()
    if not relative or not _touches_app(checkout, rule):
        return ""
    path = checkout.path
    _git(path, "fetch", "--quiet", "origin", checkout.default_branch, check=False)
    upstream_text = _git(path, "show", f"origin/{checkout.default_branch}:{relative}",
                         check=False).stdout
    target = path / relative
    try:
        upstream = json.loads(upstream_text)["expo"]
        ours = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    expo = ours.get("expo") if isinstance(ours, dict) else None
    if not isinstance(expo, dict):
        return ""
    base_version = str(upstream.get("version") or "0.0.0")
    base_build = int(str((upstream.get("ios") or {}).get("buildNumber") or 0) or 0)
    our_build = int(str((expo.get("ios") or {}).get("buildNumber") or 0) or 0)
    if (_version_key(expo.get("version") or "") > _version_key(base_version)
            and our_build > base_build):
        return ""
    parts = list(_version_key(base_version)) or [0, 0, 0]
    parts[-1] += 1
    version, build = ".".join(map(str, parts)), base_build + 1
    expo["version"] = version
    expo.setdefault("ios", {})["buildNumber"] = str(build)
    if isinstance(expo.get("android"), dict) and "versionCode" in expo["android"]:
        expo["android"]["versionCode"] = build
    target.write_text(json.dumps(ours, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _git(path, "add", "--", relative)
    _run(["git", "-C", str(path), *_identity(path), "commit", "--quiet",
          "-m", f"chore(release): bump app to {version} (build {build}) for SideStore"])
    return version


def ship(checkout: TaskCheckout) -> str:
    """Start the repository's release build after a merge, when one is configured.

    Railway services deploy from their GitHub branch by themselves, so only
    repositories that need an explicit trigger appear in dispatch.json:
    ``{"ship": {"owner/repo": {"codemagic_app_id": "...", "codemagic_workflow": "...",
    "paths": ["apps/mobile/", "packages/"]}}}``. With ``paths``, only a change
    touching one of them builds: Locket's iOS shell loads the live site, so a
    web-only change would burn a Mac build for an identical app.
    Returns a short receipt, or "" when the repository has nothing to trigger.
    """

    import urllib.request

    rule = _ship_rule(checkout)
    if not rule.get("codemagic_app_id") or not _touches_app(checkout, rule):
        return ""
    token = _codemagic_token()
    if not token:
        raise CheckoutError("a Codemagic build is configured but no API token is present")
    request = urllib.request.Request(
        "https://api.codemagic.io/builds",
        data=json.dumps({
            "appId": rule["codemagic_app_id"],
            "workflowId": rule.get("codemagic_workflow", ""),
            "branch": checkout.default_branch,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-auth-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            build = json.loads(response.read() or b"{}").get("buildId", "")
    except (OSError, ValueError) as error:
        raise CheckoutError(f"Codemagic refused the build: {type(error).__name__}") from error
    return f"codemagic build {build}" if build else "codemagic build started"


def cleanup(checkout: TaskCheckout) -> None:
    """Drop the task worktree; the branch and PR stay on GitHub."""

    _git(checkout.base, "worktree", "remove", "--force", str(checkout.path), check=False)
    shutil.rmtree(checkout.path, ignore_errors=True)
    _git(checkout.base, "worktree", "prune", check=False)
