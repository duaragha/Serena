"""Shared fail-closed policy for Serena tool, MCP, and risky execution boundaries.

This module decides and describes. It never grants approval, installs a
dependency, starts a container, or opens a network connection on its own.
Callers keep their native authority and execute only after these checks pass.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

BASE_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)
SUBSCRIPTION_ENV_ALLOWLIST = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CODEX_HOME",
    }
)
_CREDENTIAL_MARKERS = ("AUTH", "COOKIE", "CREDENTIAL", "KEY", "PASSWORD", "SECRET", "TOKEN")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{12,}|(?:sk|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,})"
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SSH_TARGET = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
_UNSAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "RUBYOPT",
    }
)
_UNSAFE_ENVIRONMENT_PREFIXES = ("DYLD_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

PROTECTED_PATH_NAMES = frozenset(
    {
        ".git",
        ".ssh",
        ".env",
        "auth.json",
        "brain.env",
        "credentials.json",
        "id_rsa",
        "mcp-secrets.json",
        "persona.md",
        "telegram.env",
    }
)
PROTECTED_REPOSITORY_PATHS = frozenset(
    {
        "config/serena-policy.json",
        "core/control_plane.py",
        "core/memory_authority.py",
        "core/notification_authority.py",
        "core/security_policy.py",
        "core/serena_plugins.py",
        "core/session_identity.py",
        "core/work_authority.py",
        "persona.md",
    }
)


class SecurityPolicyError(RuntimeError):
    pass


def _credential_name(name: object) -> bool:
    upper = str(name or "").upper()
    parts = set(upper.split("_"))
    return bool(parts.intersection(_CREDENTIAL_MARKERS)) or upper.endswith(
        ("APIKEY", "ACCESSKEY", "PRIVATEKEY", "SECRETKEY")
    )


def _unsafe_environment_name(name: object) -> bool:
    upper = str(name or "").upper()
    return upper in _UNSAFE_ENVIRONMENT_NAMES or upper.startswith(
        _UNSAFE_ENVIRONMENT_PREFIXES
    )


def validate_plain_environment(values: Mapping[str, object]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name)
        value = str(raw_value)
        if not _ENV_NAME.fullmatch(name):
            raise SecurityPolicyError(f"invalid environment variable name: {name!r}")
        if _unsafe_environment_name(name):
            raise SecurityPolicyError(f"environment variable {name} can alter child execution")
        if _credential_name(name) or _CREDENTIAL_VALUE.search(value):
            raise SecurityPolicyError(
                f"credential-like environment variable {name} must be a named secret reference"
            )
        clean[name] = value
    return clean


def build_child_environment(
    inherited: Mapping[str, str],
    *,
    plain: Mapping[str, object] | None = None,
    secrets: Mapping[str, str] | None = None,
    inherit: Sequence[str] = (),
    preserve_subscription: bool = False,
) -> dict[str, str]:
    """Build a minimal child environment from explicit names only."""

    allowed = set(BASE_ENV_ALLOWLIST)
    for raw in inherit:
        name = str(raw)
        if not _ENV_NAME.fullmatch(name):
            raise SecurityPolicyError(f"invalid inherited environment name: {name!r}")
        if _unsafe_environment_name(name):
            raise SecurityPolicyError(f"environment variable {name} can alter child execution")
        if _credential_name(name):
            raise SecurityPolicyError(f"credential environment {name} must be a secret reference")
        allowed.add(name)
    if preserve_subscription:
        allowed.update(SUBSCRIPTION_ENV_ALLOWLIST)
    environment = {name: str(inherited[name]) for name in allowed if inherited.get(name)}
    # Commands resolved by Serena may still launch helpers. Give them a fixed
    # system search path rather than inheriting a user-controlled PATH.
    if inherited.get("PATH") and os.defpath:
        environment["PATH"] = os.defpath
    environment.update(validate_plain_environment(plain or {}))
    for raw_name, raw_value in (secrets or {}).items():
        name = str(raw_name)
        if not _ENV_NAME.fullmatch(name):
            raise SecurityPolicyError(f"invalid secret environment name: {name!r}")
        if _unsafe_environment_name(name):
            raise SecurityPolicyError(f"environment variable {name} can alter child execution")
        if raw_value:
            environment[name] = str(raw_value)
    return environment


def redact_credentials(text: object, *, secret_values: Sequence[str] = ()) -> str:
    clean = str(text or "")
    for secret in sorted({str(value) for value in secret_values if value}, key=len, reverse=True):
        clean = clean.replace(secret, "[REDACTED]")
    clean = _CREDENTIAL_VALUE.sub("[REDACTED]", clean)
    clean = re.sub(
        r"(?im)^([A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|COOKIE)[A-Z0-9_]*\s*=\s*).+$",
        r"\1[REDACTED]",
        clean,
    )
    return clean


def validate_protected_path(
    value: str | Path,
    *,
    allowed_roots: Sequence[str | Path],
    allow_missing: bool = True,
) -> Path:
    raw = Path(value).expanduser()
    candidate = raw.resolve(strict=not allow_missing)
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not roots or not any(candidate == root or root in candidate.parents for root in roots):
        raise SecurityPolicyError(f"path is outside the allowed roots: {candidate}")
    lowered_parts = {part.lower() for part in candidate.parts}
    if lowered_parts & PROTECTED_PATH_NAMES:
        raise SecurityPolicyError(f"path is protected: {candidate}")
    for root in roots:
        try:
            relative = candidate.relative_to(root).as_posix().lower()
        except ValueError:
            continue
        if relative in PROTECTED_REPOSITORY_PATHS:
            raise SecurityPolicyError(f"path is protected: {relative}")
    return candidate


Resolver = Callable[..., Sequence[tuple[Any, ...]]]


@dataclass(frozen=True, slots=True)
class URLDecision:
    url: str
    hostname: str
    addresses: tuple[str, ...]
    private_opt_in: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class URLPolicy:
    allowed_domains: tuple[str, ...] = ()
    allow_private_network: bool = False
    allow_http: bool = False

    def validate(self, value: object, *, resolver: Resolver = socket.getaddrinfo) -> URLDecision:
        raw = str(value or "").strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"}:
            raise SecurityPolicyError("URL scheme must be http or https")
        if parsed.scheme == "http" and not self.allow_http:
            raise SecurityPolicyError("plain HTTP requires explicit policy opt-in")
        if parsed.username is not None or parsed.password is not None:
            raise SecurityPolicyError("URL credentials are forbidden")
        if parsed.fragment:
            raise SecurityPolicyError("URL fragments are not accepted at tool boundaries")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname or not _DOMAIN.fullmatch(hostname):
            try:
                ipaddress.ip_address(hostname)
            except ValueError as exc:
                raise SecurityPolicyError("URL hostname is invalid") from exc
        allowed = tuple(domain.rstrip(".").lower() for domain in self.allowed_domains)
        if not allowed:
            raise SecurityPolicyError("URL hostname allowlist is required")
        if not any(
            hostname == domain or hostname.endswith("." + domain) for domain in allowed
        ):
            raise SecurityPolicyError(f"URL hostname is not allowlisted: {hostname}")
        try:
            infos = resolver(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except (OSError, socket.gaierror) as exc:
            raise SecurityPolicyError(f"URL hostname could not be resolved: {hostname}") from exc
        addresses = sorted({str(item[4][0]).split("%")[0] for item in infos if len(item) > 4})
        if not addresses:
            raise SecurityPolicyError(f"URL hostname resolved to no addresses: {hostname}")
        for raw_address in addresses:
            address = ipaddress.ip_address(raw_address)
            blocked = not address.is_global
            if blocked and not self.allow_private_network:
                raise SecurityPolicyError(f"URL resolves to a non-public address: {raw_address}")
        return URLDecision(raw, hostname, tuple(addresses), self.allow_private_network)

    def validate_redirect(
        self,
        previous_url: object,
        location: object,
        *,
        resolver: Resolver = socket.getaddrinfo,
    ) -> URLDecision:
        target = urljoin(str(previous_url or ""), str(location or ""))
        return self.validate(target, resolver=resolver)


@dataclass(frozen=True, slots=True)
class ApprovalSuggestion:
    action: str
    reason: str
    requested_scopes: tuple[str, ...]
    requires_explicit_approval: bool = True
    auto_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def suggest_approval(
    action: object, reason: object, scopes: Sequence[object] = ()
) -> ApprovalSuggestion:
    clean_action = " ".join(str(action or "").split())[:200]
    clean_reason = " ".join(str(reason or "").split())[:1_000]
    if not clean_action or not clean_reason:
        raise SecurityPolicyError("approval suggestions require an action and reason")
    return ApprovalSuggestion(
        action=clean_action,
        reason=clean_reason,
        requested_scopes=tuple(sorted({str(scope)[:128] for scope in scopes if str(scope)})),
    )


class DenialCircuitBreaker:
    def __init__(self, path: Path, *, threshold: int = 3) -> None:
        if threshold < 2:
            raise ValueError("denial threshold must be at least two")
        self.path = Path(path).expanduser()
        self.threshold = int(threshold)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS security_denials("
                "subject TEXT PRIMARY KEY, consecutive INTEGER NOT NULL, tripped_at REAL, updated_at REAL NOT NULL)"
            )

    def record_denial(self, subject: object) -> dict[str, Any]:
        clean = _subject(subject)
        now = time.time()
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO security_denials(subject, consecutive, updated_at) VALUES (?, 1, ?) "
                "ON CONFLICT(subject) DO UPDATE SET consecutive=consecutive+1, updated_at=excluded.updated_at",
                (clean, now),
            )
            row = connection.execute(
                "SELECT consecutive, tripped_at FROM security_denials WHERE subject=?", (clean,)
            ).fetchone()
            assert row is not None
            consecutive = int(row[0])
            if consecutive >= self.threshold and row[1] is None:
                connection.execute(
                    "UPDATE security_denials SET tripped_at=? WHERE subject=?", (now, clean)
                )
        return {
            "subject": clean,
            "consecutive": consecutive,
            "tripped": consecutive >= self.threshold,
        }

    def record_explicit_approval(self, subject: object) -> bool:
        clean = _subject(subject)
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute("DELETE FROM security_denials WHERE subject=?", (clean,))
        return bool(cursor.rowcount)

    def record_success(self, subject: object) -> bool:
        """Break a non-tripped denial sequence without clearing a tripped circuit."""

        clean = _subject(subject)
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                "DELETE FROM security_denials WHERE subject=? AND tripped_at IS NULL", (clean,)
            )
        return bool(cursor.rowcount)

    def is_tripped(self, subject: object) -> bool:
        clean = _subject(subject)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT consecutive FROM security_denials WHERE subject=?", (clean,)
            ).fetchone()
        return bool(row and int(row[0]) >= self.threshold)


def _subject(value: object) -> str:
    clean = " ".join(str(value or "").split())[:256]
    if not clean:
        raise SecurityPolicyError("circuit-breaker subject is required")
    return clean


def dependency_advisory_plan(root: Path) -> list[dict[str, Any]]:
    project = Path(root).resolve()
    checks: list[dict[str, Any]] = []
    if (project / "pyproject.toml").exists() or (project / "requirements.txt").exists():
        checks.append(
            {
                "ecosystem": "python",
                "command": [sys.executable, "-m", "pip_audit", "--format", "json"],
                "available": _module_available("pip_audit"),
            }
        )
    if (project / "package-lock.json").exists():
        checks.append(
            {
                "ecosystem": "npm",
                "command": ["npm", "audit", "--omit=dev", "--json"],
                "available": shutil.which("npm") is not None,
            }
        )
    return checks


def run_dependency_advisories(
    root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    project = Path(root).resolve()
    results: list[dict[str, Any]] = []
    for check in dependency_advisory_plan(project):
        if not check["available"]:
            results.append({**check, "ran": False, "ok": False, "reason": "checker unavailable"})
            continue
        try:
            completed = runner(
                check["command"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=build_child_environment(os.environ),
            )
            output = bounded_output((completed.stdout or "") + (completed.stderr or ""))
            results.append(
                {
                    **check,
                    "ran": True,
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "output": output,
                }
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({**check, "ran": True, "ok": False, "reason": type(exc).__name__})
    return results


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def build_execution_command(
    command: Sequence[str],
    *,
    mode: str,
    project_root: Path,
    explicit_opt_in: bool = False,
    container_image: str = "",
    ssh_target: str = "",
) -> list[str]:
    argv = [str(part) for part in command if str(part)]
    if not argv:
        raise SecurityPolicyError("execution command is empty")
    selected = str(mode or "native").lower()
    binary = Path(argv[0]).name.lower()
    if binary in {"claude", "codex", "claude.exe", "codex.exe"} and selected != "native":
        raise SecurityPolicyError("native Claude/Codex subscription CLIs cannot be wrapped")
    if selected == "native":
        return argv
    if not explicit_opt_in:
        raise SecurityPolicyError(f"{selected} execution requires explicit opt-in")
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise SecurityPolicyError("risky execution requires an existing project root")
    home = Path.home().resolve()
    if root == Path(root.anchor) or root == home:
        raise SecurityPolicyError("risky execution refuses a broad filesystem root")
    validate_protected_path(root, allowed_roots=(root.parent,), allow_missing=False)
    if selected == "container":
        if not _IMAGE.fullmatch(container_image) or not re.search(
            r"@sha256:[a-fA-F0-9]{64}$", container_image
        ):
            raise SecurityPolicyError("container execution requires a sha256-pinned image")
        engine = shutil.which("podman") or shutil.which("docker") or "podman"
        return [
            engine,
            "run",
            "--rm",
            "--network=none",
            "-v",
            f"{root}:/workspace:rw",
            "-w",
            "/workspace",
            container_image,
            *argv,
        ]
    if selected == "ssh":
        if not _SSH_TARGET.fullmatch(ssh_target):
            raise SecurityPolicyError("SSH execution requires a simple explicit target")
        return [shutil.which("ssh") or "ssh", "--", ssh_target, *argv]
    if selected == "sandbox":
        sandbox = shutil.which("bwrap") or "bwrap"
        return [
            sandbox,
            "--unshare-net",
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(root),
            "--",
            *argv,
        ]
    raise SecurityPolicyError(f"unsupported execution mode: {selected}")


def bounded_output(text: object, *, limit: int = 48_000) -> dict[str, Any]:
    clean = redact_credentials(text)
    maximum = max(1_000, int(limit))
    encoded = clean.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(clean) <= maximum:
        return {"text": clean, "truncated": False, "characters": len(clean), "sha256": digest}
    marker = "\n...[output truncated by Serena; narrow the query and inspect the receipt]...\n"
    available = max(0, maximum - len(marker))
    head = available // 2
    return {
        "text": clean[:head] + marker + clean[-(available - head) :],
        "truncated": True,
        "characters": len(clean),
        "omitted_characters": len(clean) - available,
        "sha256": digest,
        "recovery": "use this output hash to correlate logs; narrow a read, but never repeat a write without inspecting state",
    }


def recovery_advice(error: object, *, idempotent: bool, truncated: bool = False) -> dict[str, Any]:
    text = str(error or "")
    lower = text.lower()
    if truncated:
        failure = "truncated_output"
        action = (
            "rerun a narrower read and correlate it with the output receipt"
            if idempotent
            else "inspect external state using the output receipt; do not repeat the write"
        )
    elif "timed out" in lower or "timeout" in lower:
        failure = "timeout"
        action = (
            "retry once with a bounded timeout" if idempotent else "inspect state before any retry"
        )
    elif "broken pipe" in lower or "connection reset" in lower:
        failure = "transport_interrupted"
        action = "resume the same native session and inspect state before retrying"
    elif "permission denied" in lower or "not allowed" in lower:
        failure = "policy_denial"
        action = "show the denied scope and an explicit approval suggestion"
    elif "unauthorized" in lower or "forbidden" in lower or "credential" in lower:
        failure = "authentication"
        action = "repair the named secret reference; never echo the credential"
    elif "resolve" in lower or "dns" in lower:
        failure = "name_resolution"
        action = "verify the allowlisted hostname and resolved addresses"
    else:
        failure = "unknown"
        action = "inspect the receipt and preserve current state"
    return {
        "failure_class": failure,
        "action": action,
        "automatic_retry": bool(idempotent and failure == "timeout"),
    }
