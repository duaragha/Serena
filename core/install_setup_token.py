"""Install a Claude setup token for Serena's resident brain service."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENV_PATH = Path.home() / ".config" / "serena" / "brain.env"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8377/health"
TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"


def _validate_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise ValueError("setup token is empty")
    if len(value) > 4096:
        raise ValueError("setup token is unexpectedly long")
    if "\n" in value or "\r" in value or "'" in value or "\x00" in value:
        raise ValueError("setup token contains characters unsafe for a systemd environment file")
    if not value.startswith("sk-ant-oat01-"):
        raise ValueError("expected a Claude setup token beginning with sk-ant-oat01-")
    return value


def _read_existing(path: Path) -> list[str]:
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        raise ValueError(f"refusing to replace a symlinked environment file: {path}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError("brain environment file must be a regular file owned by this user")
    if metadata.st_size > 64 * 1024:
        raise ValueError("brain environment file is unexpectedly large")
    return path.read_text(encoding="utf-8").splitlines()


def install_token(token: str, path: Path = DEFAULT_ENV_PATH) -> Path:
    """Atomically replace only the setup-token assignment in ``brain.env``."""

    value = _validate_token(token)
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink():
        raise ValueError(f"environment directory cannot be a symlink: {target.parent}")
    target.parent.chmod(0o700)
    lines = [
        line
        for line in _read_existing(target)
        if not line.lstrip().startswith(f"{TOKEN_NAME}=")
    ]
    lines.append(f"{TOKEN_NAME}='{value}'")
    payload = "\n".join(lines) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _wait_for_health(url: str, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error = "brain service did not answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                value = json.loads(response.read())
            if isinstance(value, dict) and value.get("ok") is True:
                return value
            last_error = "brain health response was not ready"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(last_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--health-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        token = getpass.getpass("paste the token from `claude setup-token`: ")
        target = install_token(token, args.env)
        result: dict[str, object] = {
            "ok": True,
            "environment_file": str(target),
            "mode": oct(stat.S_IMODE(target.stat().st_mode)),
            "token": "stored, not printed",
        }
        if not args.no_restart:
            subprocess.run(
                ["systemctl", "--user", "restart", "serena-brain.service"],
                check=True,
            )
            health = _wait_for_health(args.health_url, args.health_timeout)
            billing = health.get("billing")
            result["brain"] = {
                "pid": health.get("pid"),
                "auth_mode": billing.get("auth_mode")
                if isinstance(billing, dict)
                else None,
            }
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
