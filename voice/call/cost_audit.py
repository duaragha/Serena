"""Fail-closed audit for the call pipeline's zero-metered-cost objective."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

from core.billing import present_metered_auth_env, subscription_auth_evidence
from core.work_jobs import DEFAULT_JOBS_PATH

from .brain import DEFAULT_DISCOVERY
from .integrity import latest_call_id
from .telemetry import DEFAULT_METRICS_PATH

LOCAL_BRAIN_BACKENDS = frozenset({"brain.sock", "brain.tcp", "brain.json-http-nonstream"})
LOCAL_STT_BACKENDS = frozenset({"faster-whisper"})
LOCAL_TTS_BACKENDS = frozenset({"kokoro-onnx", "pocket-tts"})
LOCAL_TTS_PROVIDERS = frozenset(
    {"CPUExecutionProvider", "CUDAExecutionProvider", "DmlExecutionProvider"}
)
LOCAL_MODEL_SOURCES = frozenset({"local_path", "offline_cache"})


def capture_metrics_cursor(path: Path = DEFAULT_METRICS_PATH) -> dict[str, Any]:
    """Bind a pre-call cursor to one append-only metrics file and its prefix."""

    target = Path(path).expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("metrics path is not a regular file")
            if os.name != "nt" and metadata.st_uid != os.getuid():
                raise ValueError("metrics file is not owned by the current user")
            with suppress(OSError):
                os.fchmod(handle.fileno(), 0o600)
            digest = hashlib.sha256()
            remaining = metadata.st_size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("metrics file changed during baseline capture")
                digest.update(chunk)
                remaining -= len(chunk)
    except (OSError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "path": str(target),
            "failures": [f"metrics baseline cursor could not be captured: {exc}"],
        }
    return {
        "ok": True,
        "schema_version": 1,
        "path": str(target),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "offset": int(metadata.st_size),
        "prefix_sha256": digest.hexdigest(),
        "failures": [],
    }


def load_metrics_append(
    path: Path,
    cursor: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read only bytes appended after a verified pre-call metrics cursor."""

    target = Path(path).expanduser().resolve()
    failures: list[str] = []
    if not isinstance(cursor, Mapping) or cursor.get("ok") is not True:
        return [], {"ok": False, "failures": ["metrics baseline cursor is missing"]}
    try:
        expected_path = Path(str(cursor["path"])).expanduser().resolve()
        expected_device = int(cursor["device"])
        expected_inode = int(cursor["inode"])
        offset = int(cursor["offset"])
        prefix_sha256 = str(cursor["prefix_sha256"])
        if expected_path != target or offset < 0 or len(prefix_sha256) != 64:
            raise ValueError("metrics cursor identity is invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("metrics path is not a regular file")
            if (metadata.st_dev, metadata.st_ino) != (expected_device, expected_inode):
                raise ValueError("metrics file rotated after the baseline")
            if metadata.st_size < offset:
                raise ValueError("metrics file was truncated after the baseline")
            digest = hashlib.sha256()
            remaining = offset
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("metrics prefix could not be re-read")
                digest.update(chunk)
                remaining -= len(chunk)
            if not hmac.compare_digest(digest.hexdigest(), prefix_sha256):
                raise ValueError("metrics prefix changed after the baseline")
            appended = handle.read(metadata.st_size - offset)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return [], {
            "ok": False,
            "path": str(target),
            "failures": [f"metrics append window could not be verified: {exc}"],
        }
    if appended and not appended.endswith(b"\n"):
        failures.append("metrics append window ends with an incomplete row")
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(appended.splitlines(), 1):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            failures.append(f"metrics append row {line_number} is invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            failures.append(f"metrics append row {line_number} is not an object")
            continue
        rows.append(payload)
    return rows, {
        "ok": not failures,
        "schema_version": 1,
        "path": str(target),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "start_offset": offset,
        "end_offset": int(metadata.st_size),
        "appended_bytes": len(appended),
        "appended_sha256": hashlib.sha256(appended).hexdigest(),
        "rows": len(rows),
        "failures": failures,
    }


def _is_brain_command(command: Sequence[object]) -> bool:
    values = [str(item) for item in command]
    for index, value in enumerate(values):
        if value == "core.brain_daemon" and index > 0 and values[index - 1] == "-m":
            return True
        if Path(value).name == "brain_daemon.py" and any(
            Path(prefix).name.lower().startswith("python") for prefix in values[:index]
        ):
            return True
    return False


def _instance_lock_held(path: Path) -> bool:
    """Return true only when another process currently owns the brain lock."""

    lock_path = Path(path).expanduser()
    metadata = lock_path.stat()
    if not lock_path.is_absolute() or not stat.S_ISREG(metadata.st_mode):
        return False
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return False
    handle = lock_path.open("r+b", buffering=0)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
        return False
    finally:
        handle.close()


def _windows_process_sid(pid: int) -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return None
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if size.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            return None
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            return None
        try:
            return str(sid_text.value or "") or None
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def _current_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def brain_daemon_evidence(
    discovery_path: Path = DEFAULT_DISCOVERY,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
    lock_probe: Callable[[Path], bool] = _instance_lock_held,
    boot_time: Callable[[], float] = psutil.boot_time,
    process_sid: Callable[[int], str | None] = _windows_process_sid,
    uid_provider: Callable[[], int | None] = _current_uid,
) -> dict[str, Any]:
    """Verify that discovery points to a local daemon with no API-key override."""

    failures: list[str] = []
    try:
        payload = json.loads(Path(discovery_path).expanduser().read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        started = float(payload["started"])
        process_created = float(payload["process_created"])
        discovered_boot_time = float(payload["boot_time"])
        if not math.isfinite(started) or started <= 0:
            raise ValueError("daemon start time is invalid")
        if not math.isfinite(process_created) or process_created <= 0:
            raise ValueError("daemon process creation time is invalid")
        if not math.isfinite(discovered_boot_time) or discovered_boot_time <= 0:
            raise ValueError("daemon boot identity is invalid")
        lock_path = Path(str(payload["lock"])).expanduser()
        stream = payload["stream"]
        if not isinstance(stream, dict):
            raise ValueError("stream discovery is not an object")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "failures": [f"brain discovery could not be verified: {exc}"],
        }

    transport = str(stream.get("transport") or "")
    stream_local = False
    if transport == "unix":
        path = Path(str(stream.get("path") or "")).expanduser()
        try:
            stream_local = path.is_absolute() and stat.S_ISSOCK(path.stat().st_mode)
        except OSError:
            stream_local = False
    elif transport == "tcp":
        host = str(stream.get("host") or "")
        token = str(stream.get("token") or "")
        try:
            port = int(stream.get("port"))
        except (TypeError, ValueError):
            port = 0
        stream_local = host == "127.0.0.1" and 1 <= port <= 65_535 and bool(token)
    if not stream_local:
        failures.append("brain stream discovery is not a live local transport")

    lock_held = False
    lock_pid_matches = False
    try:
        lock_held = lock_probe(lock_path)
        lock_pid_matches = int(lock_path.read_text(encoding="ascii").strip()) == pid
    except (OSError, ValueError, UnicodeError):
        pass
    if not lock_held or not lock_pid_matches:
        failures.append("brain process-held instance lock was not proven")
    try:
        current_boot_time = float(boot_time())
    except (OSError, TypeError, ValueError, psutil.Error):
        current_boot_time = 0.0
    boot_matches = (
        math.isfinite(current_boot_time) and abs(current_boot_time - discovered_boot_time) <= 0.001
    )
    if not boot_matches:
        failures.append("brain discovery belongs to a different system boot")

    api_key_present: bool | None = None
    metered_auth_env_present: list[str] | None = None
    process_matches = False
    process_running = False
    process_created_matches = False
    try:
        process = process_factory(pid)
        process_running = bool(process.is_running())
        process_matches = _is_brain_command(process.cmdline())
        inspected_created = float(process.create_time())
        process_created_matches = (
            math.isfinite(inspected_created) and abs(inspected_created - process_created) <= 0.001
        )
        process_environ = process.environ()
        metered_auth_env_present = present_metered_auth_env(process_environ)
        api_key_present = "ANTHROPIC_API_KEY" in metered_auth_env_present
    except (OSError, psutil.Error, PermissionError) as exc:
        failures.append(f"brain daemon process could not be inspected: {exc}")
    if not process_running:
        failures.append("brain daemon PID is not running")
    if not process_matches:
        failures.append("brain discovery PID is not the brain daemon")
    if not process_created_matches:
        failures.append("brain discovery PID creation time does not match")
    if api_key_present is None:
        failures.append("brain daemon environment was not inspected")
    elif api_key_present:
        failures.append("brain daemon contains ANTHROPIC_API_KEY")
    if metered_auth_env_present is None:
        failures.append("brain daemon billing environment was not inspected")
    elif metered_auth_env_present:
        failures.append("brain daemon contains a metered-provider auth override")

    daemon_pids: set[int] = set()
    unreadable_process_pids: set[int] = set()
    current_uid = uid_provider()
    current_sid = process_sid(os.getpid()) if current_uid is None else None
    if current_uid is None and current_sid is None:
        failures.append("current Windows process owner SID could not be inspected")
    try:
        for candidate in process_iter(attrs=("pid", "cmdline", "name", "status", "uids")):
            info = getattr(candidate, "info", {})
            candidate_pid = info.get("pid") if isinstance(info, dict) else None
            command = info.get("cmdline") if isinstance(info, dict) else None
            status = info.get("status") if isinstance(info, dict) else None
            uids = info.get("uids") if isinstance(info, dict) else None
            candidate_uid = getattr(uids, "real", None)
            if current_uid is None:
                candidate_sid = (
                    process_sid(candidate_pid)
                    if isinstance(candidate_pid, int) and not isinstance(candidate_pid, bool)
                    else None
                )
                same_user = bool(current_sid and candidate_sid == current_sid)
            else:
                same_user = candidate_uid is None or candidate_uid == current_uid
            if not same_user:
                continue
            # A zombie has no executable image left and therefore cannot be a
            # competing daemon. Linux exposes its cmdline as unreadable/empty
            # until the parent reaps it, which must not poison the census.
            if status == psutil.STATUS_ZOMBIE:
                continue
            if (
                isinstance(candidate_pid, int)
                and not isinstance(candidate_pid, bool)
                and (command is None or not isinstance(command, (list, tuple)))
            ):
                unreadable_process_pids.add(candidate_pid)
            if (
                isinstance(candidate_pid, int)
                and not isinstance(candidate_pid, bool)
                and isinstance(command, (list, tuple))
                and _is_brain_command(command)
            ):
                daemon_pids.add(candidate_pid)
    except (OSError, psutil.Error, PermissionError, TypeError) as exc:
        failures.append(f"brain daemon process census failed: {exc}")
    if daemon_pids != {pid}:
        failures.append("exactly one discovered brain daemon process was not proven")
    if unreadable_process_pids:
        failures.append("one or more same-user process command lines were unreadable")
    return {
        "ok": not failures,
        "pid": pid,
        "started": started,
        "process_created": process_created,
        "boot_time": discovered_boot_time,
        "boot_matches": boot_matches,
        "lock": str(lock_path),
        "lock_held": lock_held,
        "lock_pid_matches": lock_pid_matches,
        "transport": transport,
        "stream_local": stream_local,
        "process_running": process_running,
        "process_matches": process_matches,
        "process_created_matches": process_created_matches,
        "daemon_pids": sorted(daemon_pids),
        "unreadable_process_pids": sorted(unreadable_process_pids),
        "unreadable_python_pids": sorted(unreadable_process_pids),
        "api_key_present": api_key_present,
        "metered_auth_env_present": metered_auth_env_present,
        "failures": failures,
    }


def brain_health_evidence(
    discovery_path: Path = DEFAULT_DISCOVERY,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Read non-secret live billing counters from the loopback brain health route."""

    try:
        discovery = json.loads(Path(discovery_path).expanduser().read_text(encoding="utf-8"))
        port = int(discovery["port"])
        expected_pid = int(discovery["pid"])
        expected_started = float(discovery["started"])
        expected_process_created = float(discovery["process_created"])
        expected_boot_time = float(discovery["boot_time"])
        token = str(discovery.get("token") or "")
        if (
            not token
            or not 1 <= port <= 65_535
            or not math.isfinite(expected_started)
            or expected_started <= 0
            or not math.isfinite(expected_process_created)
            or expected_process_created <= 0
            or not math.isfinite(expected_boot_time)
            or expected_boot_time <= 0
        ):
            raise ValueError("invalid loopback discovery")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with opener(request, timeout=3) as response:
            payload = json.loads(response.read(256 * 1024))
        if not isinstance(payload, dict):
            raise ValueError("brain health response is not an object")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        return {"ok": False, "failures": [f"brain health could not be verified: {exc}"]}
    billing = payload.get("billing")
    failures: list[str] = []
    health_started = payload.get("started")
    valid_started = (
        isinstance(health_started, (int, float))
        and not isinstance(health_started, bool)
        and math.isfinite(float(health_started))
        and abs(float(health_started) - expected_started) <= 0.001
    )
    health_process_created = payload.get("process_created")
    valid_process_created = (
        isinstance(health_process_created, (int, float))
        and not isinstance(health_process_created, bool)
        and math.isfinite(float(health_process_created))
        and abs(float(health_process_created) - expected_process_created) <= 0.001
    )
    health_boot_time = payload.get("boot_time")
    valid_boot_time = (
        isinstance(health_boot_time, (int, float))
        and not isinstance(health_boot_time, bool)
        and math.isfinite(float(health_boot_time))
        and abs(float(health_boot_time) - expected_boot_time) <= 0.001
    )
    if (
        payload.get("ok") is not True
        or payload.get("pid") != expected_pid
        or not valid_started
        or not valid_process_created
        or not valid_boot_time
    ):
        failures.append("brain health does not match live discovery")
    turns = payload.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
        failures.append("brain health has no valid turn counter")
    notional = billing.get("notional_cost_usd") if isinstance(billing, dict) else None
    valid_notional = (
        isinstance(notional, (int, float))
        and not isinstance(notional, bool)
        and math.isfinite(float(notional))
        and float(notional) >= 0
    )
    if (
        not isinstance(billing, dict)
        or billing.get("auth_mode") != "subscription_oauth_guarded"
        or billing.get("api_key_present") is not False
        or billing.get("metered_auth_env_present") != []
        or "metered_cost_usd" not in billing
        or billing.get("metered_cost_usd") is not None
        or billing.get("notional_source") != "sdk_result_model_price_estimate"
        or billing.get("metered_source") != "billing_dashboard_only"
        or not valid_notional
    ):
        failures.append("brain health billing provenance is incomplete or unsafe")
    return {
        "ok": not failures,
        "pid": expected_pid,
        "started": float(health_started) if valid_started else None,
        "process_created": (float(health_process_created) if valid_process_created else None),
        "boot_time": float(health_boot_time) if valid_boot_time else None,
        "turns": turns,
        "notional_cost_usd": float(notional) if valid_notional else None,
        "auth_mode": billing.get("auth_mode") if isinstance(billing, dict) else None,
        "api_key_present": (billing.get("api_key_present") if isinstance(billing, dict) else None),
        "metered_auth_env_present": (
            billing.get("metered_auth_env_present") if isinstance(billing, dict) else None
        ),
        "metered_cost_usd": (
            billing.get("metered_cost_usd") if isinstance(billing, dict) else None
        ),
        "failures": failures,
    }


def capture_baseline(
    *,
    auth: Mapping[str, Any],
    daemon: Mapping[str, Any],
    health: Mapping[str, Any],
    metrics_cursor: Mapping[str, Any],
) -> dict[str, Any]:
    failures = [
        *[str(item) for item in auth.get("failures", [])],
        *[str(item) for item in daemon.get("failures", [])],
        *[str(item) for item in health.get("failures", [])],
        *[str(item) for item in metrics_cursor.get("failures", [])],
    ]
    identity_keys = ("pid", "started", "process_created", "boot_time")
    if any(daemon.get(key) != health.get(key) for key in identity_keys):
        failures.append("daemon and health baseline identities do not match")
    return {
        "schema_version": 1,
        "captured_at": time.time(),
        "ok": bool(
            auth.get("ok")
            and daemon.get("ok")
            and health.get("ok")
            and metrics_cursor.get("ok")
            and not failures
        ),
        "auth": dict(auth),
        "daemon": dict(daemon),
        "health": dict(health),
        "metrics": dict(metrics_cursor),
        "failures": failures,
    }


def baseline_delta_evidence(
    baseline: Mapping[str, Any] | None,
    *,
    daemon: Mapping[str, Any],
    health: Mapping[str, Any] | None,
) -> dict[str, Any]:
    failures: list[str] = []
    if not isinstance(baseline, Mapping) or baseline.get("ok") is not True:
        return {"ok": False, "failures": ["a valid pre-call baseline is missing"]}
    before_daemon = baseline.get("daemon")
    before_health = baseline.get("health")
    if not isinstance(before_daemon, Mapping) or not isinstance(before_health, Mapping):
        return {"ok": False, "failures": ["pre-call baseline is malformed"]}
    captured_at = baseline.get("captured_at")
    if (
        not isinstance(captured_at, (int, float))
        or isinstance(captured_at, bool)
        or not math.isfinite(float(captured_at))
        or float(captured_at) <= 0
    ):
        failures.append("pre-call baseline capture time is invalid")
    if not isinstance(health, Mapping) or health.get("ok") is not True:
        return {"ok": False, "failures": ["post-call brain health is missing"]}
    identity_keys = ("pid", "started", "process_created", "boot_time")
    if any(before_daemon.get(key) != daemon.get(key) for key in identity_keys):
        failures.append("brain daemon changed after the pre-call baseline")
    if any(health.get(key) != daemon.get(key) for key in identity_keys):
        failures.append("post-call health does not match the audited daemon boot")
    before_turns = before_health.get("turns")
    after_turns = health.get("turns")
    if (
        not isinstance(before_turns, int)
        or isinstance(before_turns, bool)
        or not isinstance(after_turns, int)
        or isinstance(after_turns, bool)
        or after_turns <= before_turns
    ):
        failures.append("brain turn counter did not increase after the baseline")
    before_notional = before_health.get("notional_cost_usd")
    after_notional = health.get("notional_cost_usd")
    if (
        not isinstance(before_notional, (int, float))
        or isinstance(before_notional, bool)
        or not isinstance(after_notional, (int, float))
        or isinstance(after_notional, bool)
        or float(after_notional) < float(before_notional)
    ):
        failures.append("notional SDK counter moved backwards or is missing")
        delta = None
    else:
        delta = round(float(after_notional) - float(before_notional), 6)
    return {
        "ok": not failures,
        "turn_delta": (
            after_turns - before_turns
            if isinstance(before_turns, int) and isinstance(after_turns, int)
            else None
        ),
        "notional_cost_delta_usd": delta,
        "notional_cost_is_metered_spend": False,
        "failures": failures,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _monotonic(row: Mapping[str, Any]) -> int | None:
    value = row.get("monotonic_us")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _valid_lifecycle_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("lifecycle_id")
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _audited_lifecycle(
    rows: Iterable[dict[str, Any]], call_id: str
) -> tuple[list[dict[str, Any]], list[str], int | None, int | None, list[str]]:
    """Bind one call id to an ordered chain of server-minted lifecycles.

    A network roam or call-host restart legitimately creates a new websocket
    lifecycle. Every non-final lifecycle must end abruptly, never cleanly, and
    only the final lifecycle may contain the call's clean hangup. This keeps a
    resumed call auditable without allowing a completed call id to be reused.
    """

    selected = [row for row in rows if row.get("call_id") == call_id]
    failures: list[str] = []
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for row in selected:
        lifecycle_id = _valid_lifecycle_id(row)
        if lifecycle_id is None:
            failures.append("one or more call rows lack a valid server lifecycle id")
            continue
        if groups and groups[-1][0] == lifecycle_id:
            groups[-1][1].append(row)
            continue
        if lifecycle_id in seen:
            failures.append("server lifecycle rows are not one contiguous chain")
        seen.add(lifecycle_id)
        groups.append((lifecycle_id, [row]))

    lifecycle_ids = [lifecycle_id for lifecycle_id, _ in groups]
    for index, (_, group) in enumerate(groups):
        starts = [offset for offset, row in enumerate(group) if row.get("event") == "call.start"]
        ends = [offset for offset, row in enumerate(group) if row.get("event") == "call.end"]
        if len(starts) != 1:
            failures.append("server lifecycle does not contain exactly one call.start")
        elif starts[0] != 0:
            failures.append("server lifecycle has telemetry before call.start")
        if index < len(groups) - 1:
            if ends:
                failures.append("call id was reused after a completed server lifecycle")
        elif len(ends) != 1 or group[ends[0]].get("clean_hangup") is not True:
            failures.append("final server lifecycle does not contain one clean call.end")
        elif ends[0] != len(group) - 1:
            failures.append("server lifecycle has telemetry after call.end")

    monotonic_values = [_monotonic(row) for row in selected]
    if any(row.get("clock_domain") != "server_monotonic" for row in selected):
        failures.append("server lifecycle contains telemetry from an untrusted clock domain")
    if any(value is None for value in monotonic_values):
        failures.append("server lifecycle contains malformed monotonic timestamps")
    elif any(
        current < previous
        for previous, current in zip(monotonic_values, monotonic_values[1:], strict=False)
    ):
        failures.append("server lifecycle monotonic timestamps moved backwards")
    first_group = groups[0][1] if groups else []
    final_group = groups[-1][1] if groups else []
    first_starts = [row for row in first_group if row.get("event") == "call.start"]
    final_ends = [row for row in final_group if row.get("event") == "call.end"]
    start_us = _monotonic(first_starts[0]) if len(first_starts) == 1 else None
    end_us = _monotonic(final_ends[0]) if len(final_ends) == 1 else None
    if len(first_starts) == 1 and start_us is None:
        failures.append("server lifecycle call.start has no monotonic identity")
    if len(final_ends) == 1 and end_us is None:
        failures.append("server lifecycle call.end has no monotonic identity")
    return selected, lifecycle_ids, start_us, end_us, failures


def _valid_worker_billing_evidence(raw: object, worker_session_id: object) -> bool:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return False
    captured_at = payload.get("captured_at") if isinstance(payload, dict) else None
    worker_token = payload.get("worker_token") if isinstance(payload, dict) else None
    command_hash = payload.get("command_sha256") if isinstance(payload, dict) else None
    environment_hash = payload.get("environment_sha256") if isinstance(payload, dict) else None
    exec_token = payload.get("exec_token") if isinstance(payload, dict) else None
    valid_hashes = all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in (command_hash, environment_hash)
    )
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 3
        and payload.get("ok") is True
        and payload.get("stage") == "exec_ready"
        and payload.get("auth_mode") == "subscription_oauth_guarded"
        and payload.get("logged_in") is True
        and payload.get("auth_method") == "claude.ai"
        and payload.get("api_provider") == "firstParty"
        and payload.get("subscription_type") == "max"
        and payload.get("api_key_present") is False
        and payload.get("metered_auth_env_present") == []
        and payload.get("setting_sources") == []
        and isinstance(payload.get("gate_pid"), int)
        and not isinstance(payload.get("gate_pid"), bool)
        and int(payload["gate_pid"]) > 0
        and isinstance(worker_token, str)
        and bool(worker_token)
        and isinstance(worker_session_id, str)
        and payload.get("worker_session_id") == worker_session_id
        and valid_hashes
        and isinstance(payload.get("exec_pid"), int)
        and not isinstance(payload.get("exec_pid"), bool)
        and int(payload["exec_pid"]) > 0
        and isinstance(exec_token, str)
        and bool(exec_token)
        and payload.get("containment") in {"linux_pdeathsig", "windows_kill_on_close_job"}
        and isinstance(captured_at, (int, float))
        and not isinstance(captured_at, bool)
        and math.isfinite(float(captured_at))
        and float(captured_at) > 0
    )


def call_cost_evidence(
    rows: Iterable[dict[str, Any]],
    *,
    call_id: str,
    expected_daemon_pid: int | None = None,
    expected_daemon_started: float | None = None,
    jobs_path: Path = DEFAULT_JOBS_PATH,
) -> dict[str, Any]:
    """Verify one completed call used only the pinned local speech and brain path."""

    materialized = list(rows)
    selected, lifecycle_ids, lifecycle_start, final_end, failures = _audited_lifecycle(
        materialized, call_id
    )
    if any(row.get("call_id") != call_id for row in materialized):
        failures.append("metrics append window contains telemetry from another call")
    starts = [row for row in selected if row.get("event") == "call.start"]
    ready = [row for row in selected if row.get("event") == "call.ready"]
    stt_done = [row for row in selected if row.get("event") == "stt.done"]
    brain_done = [row for row in selected if row.get("event") == "brain.done"]
    content_audio = [row for row in selected if row.get("event") == "audio.first_content_send"]
    if not starts:
        failures.append("final call lifecycle has no call.start")
    if final_end is None:
        failures.append("final call lifecycle has no clean hangup")
    if not ready:
        failures.append("final call lifecycle has no call.ready evidence")
    if not stt_done:
        failures.append("final call lifecycle has no completed local STT turn")
    if not brain_done:
        failures.append("final call lifecycle has no completed resident brain turn")
    if not content_audio:
        failures.append("final call lifecycle has no synthesized content audio")
    for row in [*starts, *ready]:
        if row.get("anthropic_api_key_present") is not False:
            failures.append("call host API-key absence was not proven")
        if row.get("speech_execution") != "local_only":
            failures.append("call host did not declare the local-only speech path")
        if row.get("metered_auth_env_present") != []:
            failures.append("call host metered-provider auth absence was not proven")
        pid = row.get("call_host_pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            failures.append("call host PID provenance is missing")

    for row in ready:
        if row.get("ready") is not True:
            failures.append("call.ready reported an unready runtime")
            continue
        models = row.get("models")
        if not isinstance(models, dict) or any(
            models.get(name) != "ready" for name in ("stt", "tts", "vad")
        ):
            failures.append("call.ready did not prove every local model ready")
        details = row.get("model_details")
        stt = details.get("stt") if isinstance(details, dict) else None
        tts = details.get("tts") if isinstance(details, dict) else None
        vad = details.get("vad") if isinstance(details, dict) else None
        if not isinstance(stt, dict):
            failures.append("call.ready has no STT provenance")
        elif (
            stt.get("backend") not in LOCAL_STT_BACKENDS
            or stt.get("execution") != "local"
            or stt.get("model_source") not in LOCAL_MODEL_SOURCES
        ):
            failures.append("call.ready STT provenance is not local faster-whisper")
        if not isinstance(tts, dict):
            failures.append("call.ready has no TTS provenance")
        elif (
            tts.get("backend") not in LOCAL_TTS_BACKENDS
            or tts.get("execution") != "local"
            or tts.get("model_source") not in LOCAL_MODEL_SOURCES
            or tts.get("provider") not in LOCAL_TTS_PROVIDERS
        ):
            failures.append("call.ready TTS provenance is not an allowed local engine")
        if not isinstance(vad, dict):
            failures.append("call.ready has no VAD provenance")
        elif (
            vad.get("backend") != "silero-vad"
            or vad.get("execution") != "local"
            or vad.get("model_source") not in LOCAL_MODEL_SOURCES
        ):
            failures.append("call.ready VAD provenance is not local Silero")

    backends = sorted(
        {str(row.get("backend") or "") for row in brain_done if str(row.get("backend") or "")}
    )
    if any(backend not in LOCAL_BRAIN_BACKENDS for backend in backends):
        failures.append("a brain turn bypassed the local resident daemon transport")
    missing_backend = sum(not str(row.get("backend") or "") for row in brain_done)
    if missing_backend:
        failures.append("one or more brain completions have no transport provenance")
    for row in brain_done:
        meta = row.get("meta")
        if not isinstance(meta, dict) or meta.get("billing_mode") != "subscription_oauth_guarded":
            failures.append("brain completion has no subscription billing guard proof")
            continue
        if expected_daemon_pid is not None and meta.get("daemon_pid") != expected_daemon_pid:
            failures.append("brain completion came from a different daemon PID")
        started = meta.get("daemon_started")
        if expected_daemon_started is not None and (
            not isinstance(started, (int, float))
            or isinstance(started, bool)
            or abs(float(started) - expected_daemon_started) > 0.001
        ):
            failures.append("brain completion came from a different daemon boot")

    def generations(items: list[dict[str, Any]]) -> set[int]:
        return {
            value
            for row in items
            if isinstance((value := row.get("generation")), int) and not isinstance(value, bool)
        }

    stt_generations = generations(stt_done)
    brain_generations = generations(brain_done)
    content_generations = generations(content_audio)
    if stt_done and (
        not stt_generations
        or not stt_generations.issubset(brain_generations)
        or not stt_generations.issubset(content_generations)
    ):
        failures.append("STT, brain, and synthesized content do not cover the same turns")
    accepted_job_ids = {
        str(row.get("job_id"))
        for row in selected
        if row.get("event") == "task.accepted" and str(row.get("job_id") or "")
    }
    job_failures: list[str] = []
    terminal_jobs = 0
    if accepted_job_ids:
        try:
            uri = f"file:{Path(jobs_path).expanduser().resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                placeholders = ",".join("?" for _ in accepted_job_ids)
                rows_for_jobs = connection.execute(
                    "SELECT job_id, state, worker_pid, worker_token, "
                    "worker_session_id, billing_evidence_json FROM work_jobs "
                    f"WHERE job_id IN ({placeholders})",
                    tuple(sorted(accepted_job_ids)),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            rows_for_jobs = []
            job_failures.append(f"task worker store could not be audited: {exc}")
        indexed = {str(row["job_id"]): row for row in rows_for_jobs}
        for job_id in sorted(accepted_job_ids):
            row = indexed.get(job_id)
            if row is None:
                job_failures.append(f"task job {job_id} is missing from the durable store")
            elif (
                row["state"] not in {"artifact_ready", "failed", "cancelled"}
                or row["worker_pid"] is not None
                or row["worker_token"] is not None
            ):
                job_failures.append(f"task job {job_id} still owns a worker")
            elif not _valid_worker_billing_evidence(
                row["billing_evidence_json"], row["worker_session_id"]
            ):
                job_failures.append(
                    f"task job {job_id} has no verified subscription worker evidence"
                )
            else:
                terminal_jobs += 1
    failures.extend(job_failures)
    return {
        "ok": not failures,
        "call_id": call_id,
        "lifecycle_id": lifecycle_ids[-1] if lifecycle_ids else None,
        "lifecycle_ids": lifecycle_ids,
        "reconnects": max(0, len(lifecycle_ids) - 1),
        "lifecycle_started_at_us": lifecycle_start,
        "clean_hangup_us": final_end,
        "starts": len(starts),
        "ready_events": len(ready),
        "stt_turns": len(stt_done),
        "brain_turns": len(brain_done),
        "content_audio_turns": len(content_audio),
        "brain_backends": backends,
        "completed_generations": sorted(stt_generations & brain_generations & content_generations),
        "task_jobs": len(accepted_job_ids),
        "terminal_task_jobs": terminal_jobs,
        "failures": failures,
    }


def analyze_cost_objective(
    rows: Iterable[dict[str, Any]],
    *,
    call_id: str,
    auth: Mapping[str, Any],
    daemon: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
    health: Mapping[str, Any] | None = None,
    metrics_window: Mapping[str, Any] | None = None,
    jobs_path: Path = DEFAULT_JOBS_PATH,
    billing_dashboard_clear: bool = False,
) -> dict[str, Any]:
    expected_pid = daemon.get("pid")
    expected_started = daemon.get("started")
    call = call_cost_evidence(
        rows,
        call_id=call_id,
        expected_daemon_pid=(
            int(expected_pid)
            if isinstance(expected_pid, int) and not isinstance(expected_pid, bool)
            else None
        ),
        expected_daemon_started=(
            float(expected_started)
            if isinstance(expected_started, (int, float)) and not isinstance(expected_started, bool)
            else None
        ),
        jobs_path=jobs_path,
    )
    delta = baseline_delta_evidence(baseline, daemon=daemon, health=health)
    metrics_failures = (
        [str(item) for item in metrics_window.get("failures", [])]
        if isinstance(metrics_window, Mapping)
        else ["metrics append window was not verified"]
    )
    metrics_ok = bool(
        isinstance(metrics_window, Mapping)
        and metrics_window.get("ok") is True
        and not metrics_failures
    )
    failures = [
        *[str(item) for item in auth.get("failures", [])],
        *[str(item) for item in daemon.get("failures", [])],
        *call["failures"],
        *delta["failures"],
        *metrics_failures,
    ]
    automated_pass = bool(
        auth.get("ok") and daemon.get("ok") and call["ok"] and delta["ok"] and metrics_ok
    )
    if not billing_dashboard_clear:
        failures.append("post-call billing dashboard attestation is missing")
    acceptance = automated_pass and billing_dashboard_clear
    return {
        "ok": acceptance,
        "acceptance_claim": acceptance,
        "automated_pass": automated_pass,
        "definition": (
            "no metered provider charge per call; existing subscription, electricity, "
            "network, and hardware costs are excluded"
        ),
        "notional_sdk_cost_is_billing_evidence": False,
        "billing_dashboard_clear": billing_dashboard_clear,
        "auth": dict(auth),
        "daemon": dict(daemon),
        "baseline_delta": delta,
        "metrics_window": dict(metrics_window) if isinstance(metrics_window, Mapping) else None,
        "call": call,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--call-id")
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS_PATH)
    parser.add_argument("--capture-baseline", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--billing-dashboard-clear",
        action="store_true",
        help="attest that the Anthropic billing dashboard stayed at zero after this call",
    )
    args = parser.parse_args(argv)
    auth = subscription_auth_evidence()
    daemon = brain_daemon_evidence(args.discovery)
    health = brain_health_evidence(args.discovery)
    if args.capture_baseline is not None:
        metrics_cursor = capture_metrics_cursor(args.metrics)
        baseline = capture_baseline(
            auth=auth,
            daemon=daemon,
            health=health,
            metrics_cursor=metrics_cursor,
        )
        _write_json_atomic(args.capture_baseline, baseline)
        print(json.dumps(baseline, indent=2, sort_keys=True))
        return 0 if baseline["ok"] else 1
    try:
        baseline = (
            json.loads(args.baseline.expanduser().read_text(encoding="utf-8"))
            if args.baseline is not None
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"baseline could not be read: {exc}"}))
        return 2
    cursor = baseline.get("metrics") if isinstance(baseline, Mapping) else None
    rows, metrics_window = load_metrics_append(args.metrics, cursor)
    call_id = args.call_id or latest_call_id(rows)
    if not call_id:
        print(json.dumps({"ok": False, "error": "no appended call.start telemetry found"}))
        return 2
    report = analyze_cost_objective(
        rows,
        call_id=call_id,
        auth=auth,
        daemon=daemon,
        baseline=baseline,
        health=health,
        metrics_window=metrics_window,
        jobs_path=args.jobs,
        billing_dashboard_clear=args.billing_dashboard_clear,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
