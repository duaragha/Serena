"""Create a non-inheritable kill-on-close Windows Job Object.

WindowsProcess uses an empty job for atomic JOB_LIST creation. The legacy
optional process argument is safe only for stdin-gated helpers, with assignment
before releasing their request; never use it to admit arbitrary running workers.
"""

import ctypes
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperations", "WriteOperations", "OtherOperations",
        "ReadBytes", "WriteBytes", "OtherBytes",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("Basic", _BasicLimits), ("Io", _IoCounters),
        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t),
    ]


class HelperJob:
    def __init__(self, process=None):
        self._handle = None
        api = self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int,
                                         ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        # NULL security attributes: unnamed, non-inheritable handle.
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen's existing process handle binds the actual instance, not a
            # reopened PID which could already have been recycled.
            if process is not None and not api.AssignProcessToJobObject(self._handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
