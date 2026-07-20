"""Hold and supervise a call-job child under a durable, bounded PID."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.billing import (  # noqa: E402
    command_fingerprint,
    environment_fingerprint,
    subscription_auth_evidence,
)
from core.work_jobs import process_start_token  # noqa: E402

_DEFAULT_STDOUT_LIMIT = 512 * 1024
_DEFAULT_STDERR_LIMIT = 64 * 1024
_WORKER_EXEC = Path(__file__).with_name("worker_exec.py")


def _worker_session_id(command: list[str]) -> str:
    try:
        return command[command.index("--session-id") + 1]
    except (ValueError, IndexError):
        return ""


def _write_attestation(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _attest(command: list[str]) -> dict[str, object]:
    environment = dict(os.environ)
    auth_command = [command[0], "--setting-sources", "", "auth", "status"]
    evidence = subscription_auth_evidence(environ=environment, command=auth_command)
    return {
        **evidence,
        "schema_version": 3,
        "captured_at": time.time(),
        "auth_mode": "subscription_oauth_guarded",
        "setting_sources": [],
        "gate_pid": os.getpid(),
        "worker_token": process_start_token(os.getpid()),
        "worker_session_id": _worker_session_id(command),
        "command_sha256": command_fingerprint(command),
        "environment_sha256": environment_fingerprint(environment),
        "stage": "authenticated",
    }


def _windows_kill_on_close_job(child: subprocess.Popen[bytes]) -> int:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(child._handle)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "worker Job Object setup failed")
    return int(job)


def _close_windows_handle(handle: int | None) -> None:
    if not handle:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _resume_windows_process(child: subprocess.Popen[bytes]) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(wintypes.HANDLE(child._handle))
    if status != 0:
        raise OSError(int(status), "NtResumeProcess failed")


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    with suppress(ProcessLookupError):
        child.terminate()
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            child.kill()
        child.wait()


def _limit(name: str, default: int) -> int:
    try:
        return max(1024, min(int(os.environ.get(name, default)), 8 * 1024 * 1024))
    except (TypeError, ValueError):
        return default


def _pump(
    source,
    target_fd: int,
    limit: int,
    overflow: threading.Event,
    child: subprocess.Popen[bytes],
) -> None:
    written = 0
    while True:
        chunk = source.read(64 * 1024)
        if not chunk:
            return
        remaining = max(0, limit - written)
        if remaining:
            view = memoryview(chunk[:remaining])
            while view:
                count = os.write(target_fd, view)
                if count < 1:
                    return
                view = view[count:]
            written += min(len(chunk), remaining)
        if len(chunk) > remaining:
            overflow.set()
            with suppress(ProcessLookupError):
                child.terminate()


def main() -> int:
    if len(sys.argv) < 2:
        return 64
    command = sys.argv[1:]
    if sys.stdin.buffer.read(1) != b"A":
        return 75
    attestation_name = os.environ.get("SERENA_WORKER_ATTESTATION_PATH", "")
    attestation_path = Path(attestation_name).expanduser()
    if not attestation_path.is_absolute():
        return 78
    try:
        attestation = _attest(command)
        _write_attestation(attestation_path, attestation)
    except (OSError, ValueError, TypeError):
        return 78
    if attestation.get("ok") is not True:
        return 78
    if sys.stdin.buffer.read(1) != b"G":
        return 75
    child_environment = dict(os.environ)
    child_environment["SERENA_WORKER_GATE_PID"] = str(os.getpid())
    job_handle: int | None = None
    try:
        if os.name == "nt":
            child = subprocess.Popen(
                command,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=0x00000004 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            job_handle = _windows_kill_on_close_job(child)
            containment = "windows_kill_on_close_job"
        else:
            child = subprocess.Popen(
                [sys.executable, str(_WORKER_EXEC), *command],
                env=child_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            containment = "linux_pdeathsig"
        exec_token = process_start_token(child.pid)
        if not exec_token:
            raise RuntimeError("worker exec identity is unavailable")
        _write_attestation(
            attestation_path,
            {
                **attestation,
                "captured_at": time.time(),
                "stage": "exec_ready",
                "exec_pid": child.pid,
                "exec_token": exec_token,
                "containment": containment,
                "environment_sha256": environment_fingerprint(child_environment),
            },
        )
        if sys.stdin.buffer.read(1) != b"E":
            raise RuntimeError("worker exec was not released")
        if os.name == "nt":
            _resume_windows_process(child)
        else:
            assert child.stdin is not None
            child.stdin.write(b"E")
            child.stdin.flush()
            child.stdin.close()
            child.stdin = None
    except (OSError, RuntimeError, ValueError, TypeError):
        if "child" in locals():
            _stop_child(child)
        _close_windows_handle(job_handle)
        return 78
    assert child.stdout is not None and child.stderr is not None
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    pumps = [
        threading.Thread(
            target=_pump,
            args=(
                child.stdout,
                sys.stdout.fileno(),
                _limit("SERENA_WORKER_STDOUT_LIMIT", _DEFAULT_STDOUT_LIMIT),
                stdout_overflow,
                child,
            ),
        ),
        threading.Thread(
            target=_pump,
            args=(
                child.stderr,
                sys.stderr.fileno(),
                _limit("SERENA_WORKER_STDERR_LIMIT", _DEFAULT_STDERR_LIMIT),
                stderr_overflow,
                child,
            ),
        ),
    ]
    for pump in pumps:
        pump.start()
    return_code: int
    try:
        while True:
            completed = child.poll()
            if completed is not None:
                return_code = completed
                break
            if stdout_overflow.is_set() or stderr_overflow.is_set():
                _stop_child(child)
                return_code = child.returncode or 74
                break
            time.sleep(0.01)
        for pump in pumps:
            pump.join()
    finally:
        _close_windows_handle(job_handle)
    if stdout_overflow.is_set() or stderr_overflow.is_set():
        stream = "stdout" if stdout_overflow.is_set() else "stderr"
        os.write(sys.stderr.fileno(), f"\nworker {stream} exceeded byte limit\n".encode())
        return 74
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
