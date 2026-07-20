"""Rebuild and test Serena from a committed Git snapshot in an isolated directory."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_ROOT = Path("~/.config/serena/acceptance").expanduser()
DEFAULT_WORK_ROOT = Path("~/.cache/serena/reconstruction").expanduser()


def _run(command: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "elapsed": round(time.monotonic() - started, 3),
        "output_tail": result.stdout[-6000:],
    }


def _extract_git_snapshot(repository: Path, destination: Path, revision: str) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        for member in handle.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe Git archive path: {member.name}")
            if member.issym() or member.islnk():
                link_path = Path(member.linkname)
                if link_path.is_absolute() or ".." in link_path.parts:
                    raise ValueError(f"unsafe Git archive link: {member.name}")
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"unsafe Git archive path: {member.name}")
        handle.extractall(destination)
    return commit


def verify(
    *,
    repository: Path = REPO_ROOT,
    revision: str = "HEAD",
    output: Path | None = None,
    keep: bool = False,
    full_python_tests: bool = True,
) -> dict[str, Any]:
    repository = repository.resolve()
    if output:
        workspace = output.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        temporary = False
    else:
        DEFAULT_WORK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace = Path(tempfile.mkdtemp(prefix="serena-reconstruct-", dir=DEFAULT_WORK_ROOT))
        temporary = True

    steps: list[dict[str, Any]] = []
    try:
        commit = _extract_git_snapshot(repository, workspace, revision)
        steps.append(
            _run([sys.executable, "-m", "venv", "--system-site-packages", ".venv"], workspace, 120)
        )
        python = workspace / ".venv" / "bin" / "python"
        steps.append(
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-e",
                    ".[dev]",
                ],
                workspace,
                600,
            )
        )
        steps.append(
            _run(
                [
                    str(python),
                    "-m",
                    "scripts.bootstrap",
                    "doctor",
                    "--source-only",
                    "--repo",
                    str(workspace),
                    "--json",
                ],
                workspace,
                120,
            )
        )
        if (workspace / "mobile" / "package-lock.json").is_file():
            steps.append(_run(["npm", "ci", "--ignore-scripts"], workspace / "mobile", 600))
            steps.append(_run(["npm", "test", "--", "--run"], workspace / "mobile", 300))
            steps.append(_run(["npm", "run", "build"], workspace / "mobile", 600))
        tests = [str(python), "-m", "pytest", "-q"]
        if not full_python_tests:
            tests.extend(["tests/test_brain_state.py", "voice/call/tests/test_package_import.py"])
        steps.append(_run(tests, workspace, 900))
        if (workspace / "voice" / "desktop" / "package-lock.json").is_file():
            steps.append(_run(["npm", "ci"], workspace / "voice" / "desktop", 600))
            smoke = workspace / "voice" / "desktop" / "tests" / "dot-field-smoke.cjs"
            if smoke.is_file():
                steps.append(
                    _run(["npm", "run", "test:dot-field"], workspace / "voice" / "desktop", 120)
                )

        ok = all(step["returncode"] == 0 for step in steps)
        result: dict[str, Any] = {
            "ok": ok,
            "revision": revision,
            "commit": commit,
            "workspace": str(workspace),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "steps": steps,
        }
        report_root = DEFAULT_REPORT_ROOT
        report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_path = report_root / "reconstruction.json"
        result["report"] = str(report_path)
        report_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report_path.chmod(0o600)
        return result
    finally:
        if temporary and not keep:
            shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=REPO_ROOT)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--smoke", action="store_true", help="run a small Python subset instead of the full suite"
    )
    args = parser.parse_args(argv)
    try:
        result = verify(
            repository=args.repository,
            revision=args.revision,
            output=args.output,
            keep=args.keep,
            full_python_tests=not args.smoke,
        )
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
