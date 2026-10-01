"""Exercise the scheduled destructive command boundary without touching Docker."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/docker-cache-maintenance.sh"


def run_script(tmp_path, mode, *, host="docker-vm", history='{"status":"Completed"}'):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hostname = bin_dir / "hostname"
    hostname.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_HOST"\n')
    docker = bin_dir / "docker"
    docker.write_text('''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_CALLS"
case "$*" in
  *"info --format"*) printf '%s\n' "$TEST_HOST" ;;
  *"history ls"*) printf '%s\n' "$TEST_HISTORY" ;;
  *" du") printf 'Total: 123GB\n' ;;
esac
''')
    hostname.chmod(0o755)
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               TEST_HOST=host, TEST_HISTORY=history, TEST_CALLS=str(calls),
               RUNTIME_DIRECTORY=str(tmp_path))
    result = subprocess.run(["bash", str(SCRIPT), mode], env=env,
                            capture_output=True, text=True)
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_wrong_machine_refuses_before_docker(tmp_path):
    result, calls = run_script(tmp_path, "--apply", host="laptop")
    assert result.returncode == 1
    assert calls == []


def test_dry_run_does_not_prune(tmp_path):
    result, calls = run_script(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not any("prune" in call for call in calls)


def test_active_build_defers(tmp_path):
    result, calls = run_script(tmp_path, "--apply", history='{"status":"Running"}')
    assert result.returncode == 0, result.stderr
    assert "deferred" in result.stdout
    assert not any("prune" in call for call in calls)


def test_unreadable_history_fails_closed(tmp_path):
    result, calls = run_script(tmp_path, "--apply", history="not-json")
    assert result.returncode != 0
    assert not any("prune" in call for call in calls)


def test_apply_only_prunes_build_cache_with_age_and_budget(tmp_path):
    result, calls = run_script(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if "prune" in call] == [
        "--context default buildx --builder default prune --all --force --filter until=168h",
        "--context default buildx --builder default prune --all --force "
        "--max-used-space 40000000000 --reserved-space 15000000000",
    ]
