"""Create Fleet workers atomically inside an owned Windows 10+ Job Object.

Popen exposes HANDLE_LIST but not JOB_LIST. This small transport implements only
the pipe/process methods Fleet needs, using documented Win32 APIs. No suspended
unowned process, post-launch assignment, global monkeypatch or broker is used.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from contextlib import suppress
import math
import os
import subprocess

from fleet import windows_job


class _Security(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
        ("x", w.DWORD), ("y", w.DWORD), ("width", w.DWORD), ("height", w.DWORD),
        ("chars_x", w.DWORD), ("chars_y", w.DWORD), ("fill", w.DWORD),
        ("flags", w.DWORD), ("show", w.WORD), ("reserved_size", w.WORD),
        ("reserved_bytes", ctypes.c_void_p), ("stdin", w.HANDLE),
        ("stdout", w.HANDLE), ("stderr", w.HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def _api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    for name, args, result in (
        ("CreatePipe", [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.POINTER(_Security), w.DWORD], w.BOOL),
        ("SetHandleInformation", [w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        ("InitializeProcThreadAttributeList", [pointer, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
        ("UpdateProcThreadAttribute", [pointer, w.DWORD, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer], w.BOOL),
        ("DeleteProcThreadAttributeList", [pointer], None),
        ("CreateProcessW", [w.LPCWSTR, w.LPWSTR, pointer, pointer, w.BOOL, w.DWORD, pointer, w.LPCWSTR, pointer, ctypes.POINTER(_ProcessInfo)], w.BOOL),
        ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
        ("GetExitCodeProcess", [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        ("TerminateProcess", [w.HANDLE, w.UINT], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        ("CloseHandle", [w.HANDLE], w.BOOL),
    ):
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def _checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class WindowsProcess:
    """A parent-owned job and exact process HANDLE, with text stdio pipes."""

    def __init__(self, command: list[str], *, cwd: str, env: dict[str, str]):
        if not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
            raise ValueError("invalid worker command")
        if any(not isinstance(k, str) or not isinstance(v, str) or not k
               or "\0" in k or "\0" in v or "=" in k[1:] for k, v in env.items()):
            raise ValueError("invalid worker environment")
        self.args = command
        self.returncode = None
        self.pid = None
        self._handle = None
        self._job = None
        self.stdin = self.stdout = self.stderr = None
        self._api = _api()
        handles = set()
        attribute_list = None
        initialized = False
        try:
            self._job = windows_job.HelperJob(None)  # unnamed and never inheritable
            security = _Security(ctypes.sizeof(_Security), None, True)
            pairs = []
            for _ in range(3):
                read, write = w.HANDLE(), w.HANDLE()
                _checked(self._api.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0))
                handles.update((read.value, write.value))
                pairs.append((read.value, write.value))
            child_handles = (pairs[0][0], pairs[1][1], pairs[2][1])
            parent_handles = (pairs[0][1], pairs[1][0], pairs[2][0])
            for handle in parent_handles:
                _checked(self._api.SetHandleInformation(handle, 1, 0))  # clear HANDLE_FLAG_INHERIT
            size = ctypes.c_size_t()
            self._api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            attribute_list = ctypes.create_string_buffer(size.value)
            _checked(self._api.InitializeProcThreadAttributeList(attribute_list, 2, 0, ctypes.byref(size)))
            initialized = True
            inherited = (w.HANDLE * 3)(*child_handles)
            jobs = (w.HANDLE * 1)(self._job._handle)
            # Values remain alive until CreateProcess returns. Job assignment is
            # part of creation, before any worker/venv/frozen launcher code runs.
            for attribute, value in ((0x20002, inherited), (0x2000D, jobs)):
                _checked(self._api.UpdateProcThreadAttribute(attribute_list, 0, attribute,
                                                             value, ctypes.sizeof(value), None, None))
            startup = _StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = child_handles
            startup.attributes = ctypes.cast(attribute_list, ctypes.c_void_p)
            info = _ProcessInfo()
            line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            environment = ctypes.create_unicode_buffer("\0".join(
                f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())
            ) + "\0\0")
            # CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
            _checked(self._api.CreateProcessW(None, line, None, None, True, 0x400 | 0x80000 | 0x8000000,
                                              environment, cwd, ctypes.byref(startup), ctypes.byref(info)))
            self._handle, self.pid = info.process, info.pid
            _checked(self._api.CloseHandle(info.thread))
            # Transfer each parent HANDLE to a CRT fd, then its text stream.
            import msvcrt
            for name, handle, mode, flags in zip(("stdin", "stdout", "stderr"), parent_handles,
                                                  ("w", "r", "r"), (os.O_WRONLY, os.O_RDONLY, os.O_RDONLY)):
                fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
                handles.remove(handle)
                try:
                    stream = os.fdopen(fd, mode, buffering=1)
                except BaseException:
                    os.close(fd)
                    raise
                setattr(self, name, stream)
        except BaseException:
            self.close()
            raise
        finally:
            if initialized:
                self._api.DeleteProcThreadAttributeList(attribute_list)
            for handle in handles:
                self._api.CloseHandle(handle)

    def poll(self):
        if self.returncode is None and self._handle is not None:
            status = self._api.WaitForSingleObject(self._handle, 0)
            if status == 0:
                code = w.DWORD()
                _checked(self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)))
                self.returncode = code.value
            elif status != 258:  # WAIT_TIMEOUT
                raise ctypes.WinError(ctypes.get_last_error())
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
        status = self._api.WaitForSingleObject(self._handle, milliseconds)
        if status == 258:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self.poll() is None:
            _checked(self._api.TerminateProcess(self._handle, 1))

    kill = terminate

    def terminate_tree(self):
        if self._job is not None and self._job._handle:
            _checked(self._api.TerminateJobObject(self._job._handle, 1))

    def close_job(self):
        if self._job is not None:
            self._job.close()

    def close(self):
        try:
            self.close_job()
            if self._handle is not None:
                self.wait(timeout=5)
        finally:
            for pipe in (self.stdin, self.stdout, self.stderr):
                if pipe is not None:
                    with suppress(OSError):
                        pipe.close()
            if self._handle is not None:
                self._api.CloseHandle(self._handle)
                self._handle = None
