"""One command that checks the things that actually broke.

Every check here exists because a real outage needed it and nothing surfaced
it. They are deliberately cheap and read-only: no Fleet run is started, no
schedule is changed, nothing is dispatched. `chats doctor` is safe to run at
any time, including while work is in flight.

The failures this was written from, on 2026-09-17/18:

- Five transient ticks disabled `serena.fleet.start` and
  `serena.fleet.reconcile`. Nothing dispatched for half an hour and the queue
  read as empty, because a disabled action simply stops appearing in the run
  log -- its absence was the only symptom.
- Two task files claimed the same id, so `enqueue_task` refused every new
  brief and the phone line could queue nothing.
- `config/fleet.example.json` still named a retired model after the policy
  moved, so `load_config` refused and no run could start at all.
- `fleet/isolation.py` imported a module that was never committed, which
  broke the test gate for every integration.
- A dispatched worker was asked to audit the task list and produced unrelated
  work, because `memory/task` is not in git and its private checkout had no
  task list to read.
- `fleet serve` ran for two days on stale code while fix after fix was
  deployed to disk and never picked up.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A schedule this far past due is not merely late; something is wedged.
OVERDUE_INTERVALS = 3
# A service older than its deployed code is running fixes nobody gets.
STALE_PROCESS_HOURS = 12


@dataclass
class Finding:
    """One check, and what to do when it fails."""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    severity: str = "fail"  # "fail" or "warn"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "severity": self.severity,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.severity == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "findings": [f.as_dict() for f in self.findings]}


def _repo_root() -> Path:
    """The checkout this code is installed in, not whatever directory he ran from."""

    return Path(__file__).resolve().parent.parent


def _state_dir() -> Path:
    configured = os.environ.get("SERENA_STATE_DIR", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "serena")


def check_schedules(now: float | None = None) -> list[Finding]:
    """Disabled or wedged schedules, the outage that looks like an empty queue."""

    from core.serena_scheduler import DEFAULT_DB_PATH, MAX_CONSECUTIVE_FAILURES

    moment = time.time() if now is None else now
    path = Path(os.environ.get("SERENA_SCHEDULER_DB_PATH", "").strip() or DEFAULT_DB_PATH)
    if not path.is_file():
        return [Finding("schedules", True, "no scheduler on this machine", severity="warn")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                "SELECT schedule_id, action, state, interval_seconds, next_run_at, "
                "consecutive_failures FROM schedules")]
    except sqlite3.Error as error:
        return [Finding("schedules", False, f"scheduler database unreadable: {error}")]

    findings: list[Finding] = []
    disabled = [r for r in rows if r["state"] == "disabled"]
    if disabled:
        names = ", ".join(sorted(str(r["action"]) for r in disabled))
        ids = " ".join(str(r["schedule_id"]) for r in disabled)
        findings.append(Finding(
            "schedules.disabled", False,
            f"{len(disabled)} schedule(s) switched off after "
            f"{MAX_CONSECUTIVE_FAILURES} failures: {names}. Nothing runs them again "
            f"until they are resumed, so the queue looks idle rather than broken.",
            fix=f"chats schedule resume {ids} --actor raghav",
        ))

    failing = [r for r in rows if r["state"] == "active" and int(r["consecutive_failures"] or 0) > 0]
    if failing:
        worst = max(failing, key=lambda r: int(r["consecutive_failures"] or 0))
        findings.append(Finding(
            "schedules.failing", False,
            f"{len(failing)} active schedule(s) failing; {worst['action']} is at "
            f"{worst['consecutive_failures']}/{MAX_CONSECUTIVE_FAILURES} and will "
            f"switch itself off at the cap.",
            fix="chats schedule list --json, then read schedule_runs for the detail",
            severity="warn",
        ))

    overdue = [
        r for r in rows
        if r["state"] == "active"
        and float(r["next_run_at"] or 0) > 0
        and moment - float(r["next_run_at"]) > OVERDUE_INTERVALS * int(r["interval_seconds"] or 60)
    ]
    if overdue:
        names = ", ".join(sorted(str(r["action"]) for r in overdue))
        findings.append(Finding(
            "schedules.overdue", False,
            f"{len(overdue)} schedule(s) more than {OVERDUE_INTERVALS} intervals past "
            f"due: {names}. The automation loop is stopped, wedged, or not running here.",
            fix="check the automation service is alive and draining",
        ))

    if not findings:
        findings.append(Finding(
            "schedules", True, f"{len(rows)} schedule(s) active and on time"))
    return findings


def check_task_store() -> list[Finding]:
    """A duplicate id refuses every write, so the phone line can queue nothing."""

    import re
    from collections import defaultdict

    from memory import store

    task_dir = Path(store.MEMORY_DIR) / "task"
    if not task_dir.is_dir():
        return [Finding("tasks", True, "no task queue on this machine", severity="warn")]
    by_id: dict[int, list[str]] = defaultdict(list)
    for path in sorted(task_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        found = re.search(r"^id:\s*(\d+)", head, re.M)
        if found:
            by_id[int(found.group(1))].append(path.name)
    duplicates = {tid: names for tid, names in by_id.items() if len(names) > 1}
    if duplicates:
        shown = "; ".join(f"#{tid}: {', '.join(names)}" for tid, names in sorted(duplicates.items()))
        return [Finding(
            "tasks.duplicate_ids", False,
            f"{len(duplicates)} duplicate task id(s). enqueue_task refuses every write "
            f"while this holds, so nothing he texts can be queued. {shown}",
            fix="renumber the newer file's `id:` and filename to a free id",
        )]
    return [Finding("tasks", True, f"{len(by_id)} task(s), ids unique")]


def check_fleet_config() -> list[Finding]:
    """The shipped config drifting from the model policy stops every run."""

    try:
        from fleet.policy import config_path, load_config

        load_config()
    except Exception as error:  # the policy raises plain ValueError
        where = ""
        try:
            from fleet.policy import config_path as _cp

            where = f" ({_cp()})"
        except Exception:
            pass
        return [Finding(
            "fleet.config", False,
            f"Fleet refuses its own config{where}: {error}. start_run raises before a "
            f"worker is chosen, so no run can start at all.",
            fix="align the config's phase models with PHASE_MODEL_POLICY in fleet/policy.py",
        )]
    return [Finding("fleet.config", True, "config satisfies the model policy")]


def check_first_party_imports(roots: tuple[str, ...] = ("core", "fleet", "memory")) -> list[Finding]:
    """An import of a module nobody committed breaks whatever touches it."""

    import ast

    packages = set(roots) | {"ui", "knowledge", "voice", "integrations"}
    missing: dict[str, set[str]] = {}
    base = _repo_root()
    for root in roots:
        for path in sorted((base / root).glob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    if name.split(".")[0] not in packages:
                        continue
                    target = base / Path(name.replace(".", "/"))
                    if target.with_suffix(".py").exists() or (target / "__init__.py").exists():
                        continue
                    missing.setdefault(name, set()).add(f"{root}/{path.name}")
    if missing:
        shown = "; ".join(f"{name} <- {', '.join(sorted(users))}" for name, users in sorted(missing.items()))
        return [Finding(
            "imports", False,
            f"{len(missing)} first-party import(s) name a module that is not committed. "
            f"Anything importing them raises on a fresh checkout. {shown}",
            fix="commit the module or remove the import",
        )]
    return [Finding("imports", True, "every first-party import resolves")]


def check_toolchain() -> list[Finding]:
    """A package manager that will not launch reads as the worker's tests failing."""

    resolved = {name: shutil.which(name) for name in ("npm", "pnpm", "yarn", "git", "gh")}
    for required in ("git",):
        if not resolved[required]:
            return [Finding("toolchain", False, f"{required} is not on PATH")]
    if not resolved["npm"]:
        return [Finding(
            "toolchain.npm", False,
            "npm does not resolve. Fleet's dependency sync reports \"test gate could "
            "not run\", which integration scores as a failing test and rolls the "
            "worker's change back.",
            fix="install Node, or ensure npm is on the service's PATH",
            severity="warn",
        )]
    return [Finding(
        "toolchain", True,
        "git and npm resolve" + (f" (npm: {resolved['npm']})" if os.name == "nt" else ""))]


def check_dispatch_visibility() -> list[Finding]:
    """A worker only sees what git tracks, so a brief about untracked state cannot land."""

    import subprocess

    from memory import store

    task_dir = Path(store.MEMORY_DIR) / "task"
    # The queue's own checkout, not this one: a worktree running the doctor is
    # still asking about the repository the tasks live in.
    repo = task_dir
    while repo != repo.parent and not (repo / ".git").exists():
        repo = repo.parent
    if not (repo / ".git").exists():
        return [Finding("dispatch.visibility", True, "not a repository", severity="warn")]
    try:
        relative = task_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return [Finding(
            "dispatch.visibility", True,
            "the task queue lives outside this repository", severity="warn")]
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(relative)],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout.strip()
    if tracked:
        return [Finding("dispatch.visibility", True, "a worker's checkout can read the task queue")]
    # Untracked is the normal, intended state: the queue is local, not source.
    # What matters is whether the dispatcher compensates, so check the
    # mitigation rather than nagging forever about the fact.
    try:
        from core.scheduler_actions import _attached_task_list, _unseen_state_rules

        handoff = _unseen_state_rules("audit the open task list")
        warns = "do not substitute different work" in handoff
        # Capability, not output: an empty queue correctly attaches nothing, so
        # asking whether tasks appeared would fail on a quiet day.
        attaches = callable(_attached_task_list)
    except Exception as error:
        return [Finding(
            "dispatch.visibility", False,
            f"{relative} is untracked and the handoff could not be checked: {error}",
            fix="verify core.scheduler_actions._unseen_state_rules still exists",
        )]
    if attaches and warns:
        return [Finding(
            "dispatch.visibility", True,
            f"{relative} is untracked, as intended, and the handoff both says so and "
            f"attaches the queue for briefs about it")]
    missing = "does not attach the task list" if not attaches else "does not warn against substituting"
    return [Finding(
        "dispatch.visibility", False,
        f"{relative} is not tracked by git, so a dispatched worker's private checkout "
        f"has no task list, and the handoff {missing}. A brief asking it to audit or "
        f"work through his tasks cannot be satisfied, and the worker invents unrelated "
        f"work instead -- which is what produced a green run and a pointless PR.",
        fix="restore the unseen-state note in core.scheduler_actions",
    )]


INTEGRATION_BRANCH = "origin/master"


def check_repo_freshness() -> list[Finding]:
    """Code on disk that is behind the branch everything else is deployed from.

    `fleet serve` ran for two days on stale code while fix after fix was
    merged and deployed, and every one of them looked live because the files
    on disk were current somewhere else. A checkout that has fallen behind is
    the quiet half of that: the services started from it are running whatever
    it holds, not what was shipped.
    """

    import subprocess

    repo = _repo_root()
    if not (repo / ".git").exists():
        return [Finding("repo", True, "not a checkout", severity="warn")]

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, check=False).stdout.strip()

    # Always the integration branch, never merely this branch's own upstream.
    # Measuring against @{upstream} is how this check missed the thing it was
    # written for: the laptop sat on laptop-master, which tracked
    # origin/laptop-master, which matched it exactly -- so the doctor said "up
    # to date" while the checkout was forty-six commits behind origin/master
    # and twenty-five ahead of it, and both machines ran that tree for a day.
    target = INTEGRATION_BRANCH
    if not git("rev-parse", "--verify", "--quiet", target):
        return [Finding("repo", True, f"no {target} to compare against", severity="warn")]
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    counts = git("rev-list", "--left-right", "--count", f"{target}...HEAD").split()
    if len(counts) != 2 or not all(value.isdigit() for value in counts):
        return [Finding("repo", True, "could not compare against the remote", severity="warn")]
    behind, ahead = (int(value) for value in counts)

    findings: list[Finding] = []
    if behind:
        newest = git("log", "-1", "--format=%h %s", target)[:90]
        findings.append(Finding(
            "repo.behind", False,
            f"this checkout ({branch}) is {behind} commit(s) behind {target} as of the "
            f"last fetch, so services started from it are not running what was shipped. "
            f"Newest there: {newest}",
            fix=f"git fetch origin && git merge --ff-only {target} (check for local work first)",
            severity="warn",
        ))
    if ahead:
        # Commits that exist here and nowhere that is deployed or reviewed. One
        # is a branch mid-review; twenty-five is a program nobody landed.
        oldest = git("log", "--reverse", "--format=%h %s", f"{target}..HEAD")
        first = oldest.splitlines()[0][:90] if oldest else ""
        findings.append(Finding(
            "repo.unlanded", False,
            f"{ahead} commit(s) on {branch} are not on {target}, so they are running here "
            f"and nowhere else, and nothing is reviewing them. Oldest: {first}",
            fix=f"open a pull request for {branch}, or drop it and return to {target}",
            severity="warn",
        ))
    if findings:
        return findings
    return [Finding("repo", True, f"{branch} is level with {target}")]


def check_notification_backlog(now: float | None = None) -> list[Finding]:
    """Notices queued but never delivered mean he is not being told things."""

    moment = time.time() if now is None else now
    path = _state_dir() / "notifications.sqlite3"
    if not path.is_file():
        return [Finding("notifications", True, "no notification store here", severity="warn")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                "SELECT summary, decision, deliver_after FROM notifications "
                "WHERE decision = 'deferred' AND delivered_at IS NULL")]
    except sqlite3.Error as error:
        return [Finding("notifications", False, f"notification store unreadable: {error}")]
    overdue = [r for r in rows if float(r["deliver_after"] or 0) and float(r["deliver_after"]) < moment]
    if overdue:
        return [Finding(
            "notifications.stuck", False,
            f"{len(overdue)} notice(s) past their deliver-after time and still "
            f"undelivered, e.g. {str(overdue[0]['summary'])[:80]!r}",
            fix="check serena.notifications.flush is scheduled and running",
        )]
    return [Finding("notifications", True, f"{len(rows)} deferred, none overdue")]


CHECKS = (
    check_schedules,
    check_task_store,
    check_fleet_config,
    check_first_party_imports,
    check_toolchain,
    check_dispatch_visibility,
    check_repo_freshness,
    check_notification_backlog,
)


def run(checks=CHECKS) -> Report:
    """Every check, with a failure in one never hiding the rest."""

    report = Report()
    for check in checks:
        try:
            report.findings.extend(check())
        except Exception as error:  # a broken check is itself a finding
            report.findings.append(Finding(
                getattr(check, "__name__", "check"), False,
                f"check raised {type(error).__name__}: {str(error)[:200]}",
                severity="warn",
            ))
    return report


def render(report: Report) -> str:
    """Plain text, worst first, with the fix on the line under each failure."""

    lines: list[str] = []
    for finding in report.failures + report.warnings:
        mark = "FAIL" if finding.severity == "fail" else "WARN"
        lines.append(f"{mark}  {finding.name}: {finding.detail}")
        if finding.fix:
            lines.append(f"      fix: {finding.fix}")
    healthy = [f for f in report.findings if f.ok]
    for finding in healthy:
        lines.append(f"ok    {finding.name}: {finding.detail}")
    if report.ok:
        lines.append("")
        lines.append(f"nothing broken ({len(report.warnings)} warning(s))")
    else:
        lines.append("")
        lines.append(f"{len(report.failures)} failure(s), {len(report.warnings)} warning(s)")
    return "\n".join(lines)


def as_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, default=str)
