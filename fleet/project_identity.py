"""Local, non-network repository identity shared by Fleet advice surfaces."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def canonical_remote(value: str) -> str | None:
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        return None
    if "://" not in value:
        match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([^?#]+)", value)
        if not match:
            return None
        host, path = match.groups()
    else:
        try:
            url = urlsplit(value)
            if url.scheme not in {"https", "ssh"} or url.password or url.query or url.fragment:
                return None
            if url.username and not (url.scheme == "ssh" and url.username == "git"):
                return None
            if url.port not in {None, 22 if url.scheme == "ssh" else 443}:
                return None
            host, path = url.hostname, url.path
        except ValueError:
            return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path or any(p in {".", "..", ""} for p in path.split("/")):
        return None
    # GitHub repository names are case-insensitive; other hosts need not be.
    return "git:" + host.lower() + "/" + (path.lower() if host.lower() == "github.com" else path)


def repository_identity(cwd: str, *, fallback: str | None = None) -> str:
    root = Path(cwd).resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                stdin=subprocess.DEVNULL, text=True, timeout=5, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result

    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0 and common.stdout.strip():
            configured = git("config", "--get", "remote.origin.url")
            if configured.returncode not in {0, 1}:
                return fallback or repository_local_identity(root)
            remote = canonical_remote(configured.stdout.strip())
            if remote:
                return remote
            root = Path(common.stdout.strip()).resolve()
        elif fallback:
            return fallback
    except (OSError, subprocess.TimeoutExpired):
        if fallback:
            return fallback
    return repository_local_identity(root)


def repository_local_identity(root: Path) -> str:
    return "local:" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()


def project_identity(run: dict) -> str:
    checkout = run.get("checkout") or {}
    source = checkout.get("source_cwd") or run["cwd"]
    if not Path(source).exists() and run.get("repository_identity"):
        return run["repository_identity"]
    return repository_identity(source, fallback=run.get("repository_identity"))


def run_identity(db, run_id: str) -> str | None:
    """Resolve trusted checkout metadata without loading a whole run projection."""
    row = db.execute(
        "SELECT COALESCE(c.source_cwd,r.cwd) AS source FROM fleet_runs r "
        "LEFT JOIN fleet_run_checkouts c ON c.run_id=r.run_id WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    if not row:
        return None
    return project_identity({"cwd": row["source"], "repository_identity": stored_identity(db, run_id)})


def remember(db, run_id, cwd):
    """Freeze local Git evidence before dispatcher cleanup, independently of capture."""
    if Path(cwd).is_dir():
        db.execute("INSERT OR IGNORE INTO fleet_run_projects VALUES (?, ?)",
                   (run_id, repository_identity(cwd)))


def stored_identity(db, run_id, *, at_creation=False):
    if not at_creation:
        latest = db.execute(
            "SELECT repository_identity FROM fleet_events WHERE run_id=? "
            "AND repository_identity IS NOT NULL ORDER BY event_seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        if latest:
            return latest[0]
    row = db.execute("SELECT project FROM fleet_run_projects WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None


def report_identity(db, cwd):
    """Resolve deleted historical paths only through locally persisted run metadata."""
    if not Path(cwd).exists():
        rows = db.execute(
            "SELECT DISTINCT p.project FROM fleet_run_projects p JOIN fleet_runs r ON r.run_id=p.run_id "
            "LEFT JOIN fleet_run_checkouts c ON c.run_id=r.run_id "
            "WHERE r.cwd=? OR c.source_cwd=? OR c.path=? LIMIT 2", (cwd, cwd, cwd)
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]
    return repository_identity(cwd)


def terms(text: str) -> set[str]:
    return {s for s in re.findall(r"[a-z0-9_]{3,}", text.lower())
            if s not in {"the", "and", "for", "with", "this", "that", "from", "run", "task"}}
