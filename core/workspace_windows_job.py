"""Windows process-tree ownership. Assign only gated, not yet running workers.

This primitive does not launch processes or replace the session lease. The
launcher must prevent provider execution until assignment succeeds.
"""

import ctypes
import os
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _Limits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
    ]


class WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows job objects require Windows")
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._api, name)
            function.argtypes, function.restype = args, result
        # Unnamed, non-inheritable: no child may keep the ownership handle alive.
        self.handle = self._api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        try:
            self._check(self._api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _open(self):
        if not self.handle:
            raise OSError("Windows job is closed")
        return self.handle

    def assign(self, pid):
        handle = self._open()
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or pid == os.getpid():
            raise ValueError("An owned child PID is required")
        process = self._api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._check(self._api.AssignProcessToJobObject(handle, process))
        finally:
            self._check(self._api.CloseHandle(process))

    def active_processes(self):
        info = _Accounting()
        self._check(self._api.QueryInformationJobObject(
            self._open(), 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info.active

    def terminate(self):
        self._check(self._api.TerminateJobObject(self._open(), 1))

    def close(self):
        if self.handle:
            self._check(self._api.CloseHandle(self.handle))
            self.handle = None
