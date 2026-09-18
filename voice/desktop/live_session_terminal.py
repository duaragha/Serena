"""Open one completed Serena coding session in its real interactive CLI."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from core.billing import strip_metered_auth_env
from core.metadata import (
    clear_external_runtime,
    external_runtime_active,
    set_external_runtime,
)
from voice.desktop.coding_jobs_query import (
    _database_path,
    resolve_live_terminal_target,
)

DEFAULT_LOCK_DIR = Path.home() / ".local" / "state" / "serena" / "live-terminals"
READY_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def interactive_command(
    target: dict,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Build only an exact-session interactive resume command."""

    provider = str(target.get("provider") or "").strip().lower()
    session_id = str(target.get("session_id") or "").strip().lower()
    if provider not in {"codex", "claude"} or not session_id:
        raise ValueError("interactive session metadata is incomplete")
    executable = which(provider)
    if not executable:
        raise FileNotFoundError(f"{provider} CLI is not installed")
    if provider == "codex":
        return [executable, "resume", session_id]
    # Match Serena's existing interactive resume policy in ui/web.py and
    # ui/tui.py. This opens a user-controlled coding terminal, not a worker.
    return [executable, "--dangerously-skip-permissions", "-r", session_id]


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _ready_path(lock_dir: Path, session_id: str, token: str) -> Path:
    if not READY_TOKEN_RE.fullmatch(token):
        raise ValueError("interactive terminal readiness token is invalid")
    return lock_dir / f"{session_id}.{token}.ready"


def _write_ready(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as ready_file:
        ready_file.write(encoded)
        ready_file.flush()
        os.fsync(ready_file.fileno())


def run_provider_runtime(
    *,
    provider: str,
    session_id: str,
    project_root: Path,
    ready_token: str,
    lock_dir: Path = DEFAULT_LOCK_DIR,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    isatty: Callable[[int], bool] = os.isatty,
) -> int:
    """Run the real provider inside the terminal and prove its process started."""

    if not isatty(0) or not isatty(1):
        return 2
    root = project_root.expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        return 2
    lock_dir = lock_dir.expanduser().resolve()
    ready_path = _ready_path(lock_dir, session_id, ready_token)
    try:
        command = interactive_command(
            {"provider": provider, "session_id": session_id},
            which=which,
        )
    except (FileNotFoundError, ValueError):
        return 2
    try:
        process = popen(
            command,
            cwd=str(root),
            env=strip_metered_auth_env(os.environ),
        )
    except OSError:
        return 4
    try:
        _write_ready(
            ready_path,
            {
                "token": ready_token,
                "session_id": session_id,
                "provider": provider,
                "pid": int(process.pid),
            },
        )
    except OSError:
        process.terminate()
        with suppress(Exception):
            process.wait(timeout=2)
        return 4
    try:
        return int(process.wait())
    except KeyboardInterrupt:
        return int(process.wait())


def _wait_for_provider_runtime(
    terminal_process: subprocess.Popen,
    ready_path: Path,
    *,
    token: str,
    session_id: str,
    provider: str,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Wait for proof from the provider process running inside the real TTY."""

    while terminal_process.poll() is None:
        try:
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            sleep(0.05)
            continue
        except (OSError, json.JSONDecodeError):
            return False, "interactive terminal returned invalid readiness evidence"
        try:
            ready_pid = int(payload.get("pid") or 0) if isinstance(payload, dict) else 0
        except (TypeError, ValueError):
            ready_pid = 0
        if (
            isinstance(payload, dict)
            and payload.get("token") == token
            and payload.get("session_id") == session_id
            and payload.get("provider") == provider
            and ready_pid > 0
        ):
            return True, ""
        return False, "interactive terminal returned mismatched readiness evidence"
    return False, "terminal exited before the provider session attached"


def run_terminal(
    item_id: str,
    *,
    database: Path,
    terminal_executable: str,
    lock_dir: Path = DEFAULT_LOCK_DIR,
    resolver: Callable[..., tuple[dict | None, str]] = resolve_live_terminal_target,
    runtime_active: Callable[[str], bool] = external_runtime_active,
    claim_runtime: Callable[..., dict] = set_external_runtime,
    release_runtime: Callable[..., bool] = clear_external_runtime,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    which: Callable[[str], str | None] = shutil.which,
    emit: Callable[[dict], None] = _emit,
) -> int:
    """Claim, launch, and supervise one exact persisted interactive runtime."""

    target, error = resolver(database, item_id)
    if target is None:
        emit({"ok": False, "error": error or "interactive terminal is unavailable"})
        return 2

    session_id = str(target.get("session_id") or "").strip().lower()
    lock_dir = lock_dir.expanduser().resolve()
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / f"{session_id}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit({"ok": False, "error": "interactive terminal is already open"})
            return 3

        # Re-resolve after owning the cross-process lock. The job or route may
        # have changed between the drawer snapshot and this explicit click.
        target, error = resolver(database, item_id)
        if target is None or str(target.get("session_id") or "") != session_id:
            emit({"ok": False, "error": error or "coding session changed before launch"})
            return 2
        if runtime_active(session_id):
            emit({"ok": False, "error": "persisted session already has an active runtime"})
            return 3

        try:
            interactive_command(target, which=which)
        except (FileNotFoundError, ValueError) as exc:
            emit({"ok": False, "error": str(exc)})
            return 2

        project_root = str(target["project_root"])
        provider = str(target["provider"])
        ready_token = secrets.token_hex(16)
        ready_path = _ready_path(lock_dir, session_id, ready_token)
        title = f"Serena coding {str(item_id)[:8]}"
        terminal_command = [
            terminal_executable,
            "--wait",
            "--title",
            title,
            "--working-directory",
            project_root,
            "--",
            sys.executable,
            "-m",
            "voice.desktop.live_session_terminal",
            "--provider-runtime",
            "--provider",
            provider,
            "--session-id",
            session_id,
            "--project-root",
            project_root,
            "--ready-token",
            ready_token,
            "--lock-dir",
            str(lock_dir),
        ]
        claimed = False
        process: subprocess.Popen | None = None
        try:
            claim_runtime(
                session_id,
                kind="coding-terminal",
                pid=os.getpid(),
                lease_seconds=24 * 60 * 60,
            )
            claimed = True
            process = popen(
                terminal_command,
                cwd=project_root,
                env=strip_metered_auth_env(os.environ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            ready, ready_error = _wait_for_provider_runtime(
                process,
                ready_path,
                token=ready_token,
                session_id=session_id,
                provider=provider,
            )
            if not ready:
                emit({"ok": False, "error": ready_error})
                return 4
            emit(
                {
                    "ok": True,
                    "session_id": session_id,
                    "provider": provider,
                }
            )
            return int(process.wait())
        except OSError as exc:
            emit({"ok": False, "error": f"interactive terminal could not start: {exc}"})
            return 4
        finally:
            with suppress(OSError):
                ready_path.unlink()
            if claimed:
                with suppress(Exception):
                    release_runtime(session_id, pid=os.getpid())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-id", default="")
    parser.add_argument("--database", default="")
    parser.add_argument("--terminal-executable", default="")
    parser.add_argument("--provider-runtime", action="store_true")
    parser.add_argument("--provider", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--ready-token", default="")
    parser.add_argument("--lock-dir", default=str(DEFAULT_LOCK_DIR))
    args = parser.parse_args()
    if args.provider_runtime:
        return run_provider_runtime(
            provider=args.provider,
            session_id=args.session_id,
            project_root=Path(args.project_root),
            ready_token=args.ready_token,
            lock_dir=Path(args.lock_dir),
        )
    if not args.item_id or not args.terminal_executable:
        _emit({"ok": False, "error": "interactive terminal request is incomplete"})
        return 2
    executable = Path(args.terminal_executable).expanduser()
    if not executable.is_absolute() or not os.access(executable, os.X_OK):
        _emit({"ok": False, "error": "terminal executable is unavailable"})
        return 2
    return run_terminal(
        args.item_id,
        database=_database_path(args.database),
        terminal_executable=str(executable),
    )


if __name__ == "__main__":
    raise SystemExit(main())
