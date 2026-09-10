"""Cross-process exclusive lock without importing POSIX modules on Windows."""

from contextlib import contextmanager
import errno
import os
import time


@contextmanager
def exclusive_lock(handle, *, timeout=None):
    if os.name == "nt":
        import msvcrt

        def acquire():
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

        def release():
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def acquire():
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        try:
            acquire()
            break
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("exclusive file lock is still owned") from error
            time.sleep(0.05)
    try:
        yield
    finally:
        release()
