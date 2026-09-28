"""Cross-process file locks that work on macOS, Linux and Windows.

Why: the scheduler (cron / Task Scheduler) and a dashboard click can start a run at the same
moment. Two runs at once could email the same lead twice, so every run takes one lock first.

POSIX uses fcntl.flock and Windows uses msvcrt.locking. Both are imported lazily inside the
backend functions — fcntl doesn't exist on Windows (and msvcrt doesn't exist elsewhere), so a
top-level import would crash the whole app on the other platform. Both kinds of lock belong to
the open file handle and are released by the OS if the process dies, so a crashed run never
leaves a stale lock behind (the lock *file* stays; it is just an empty marker).
"""

import errno
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO

POLL_SECONDS = 0.5

# errno values that mean "someone else holds the lock" rather than a real I/O failure.
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


class AlreadyLocked(Exception):
    """The lock is held by another process (or another handle in this one)."""

    def __init__(self, path: str, message: str | None = None):
        self.path = path
        super().__init__(message or f"{path} is locked by another run.")


def _try_lock_posix(handle: IO) -> bool:
    import fcntl  # POSIX only — imported here so Windows can import this module

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in _BUSY_ERRNOS:
            return False
        raise


def _unlock_posix(handle: IO) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _try_lock_windows(handle: IO) -> bool:
    import msvcrt  # Windows only

    # msvcrt locks a byte range starting at the current position: always lock byte 0.
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError as e:
        if e.errno in _BUSY_ERRNOS:
            return False
        raise


def _unlock_windows(handle: IO) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _platform_functions() -> tuple[Callable[[IO], bool], Callable[[IO], None]]:
    """(try_lock, unlock) for this OS. A function, so tests can exercise the Windows path anywhere."""
    if os.name == "nt":
        return _try_lock_windows, _unlock_windows
    return _try_lock_posix, _unlock_posix


@contextmanager
def file_lock(path: str, wait_seconds: float = 0, poll_seconds: float = POLL_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the duration of the `with` block.

    wait_seconds=0 fails immediately when the lock is busy (e.g. a quick reply check that can
    simply try again next time); a positive value keeps retrying until it is free or the time is
    up (e.g. a scheduled send that must not be skipped). Raises AlreadyLocked on failure.
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    try_lock, unlock = _platform_functions()
    deadline = time.monotonic() + max(0.0, wait_seconds)
    # "a+" creates the file without truncating it, and works for both lock backends.
    with open(path, "a+", encoding="utf-8") as handle:
        while not try_lock(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AlreadyLocked(path)
            time.sleep(min(poll_seconds, remaining))
        try:
            yield
        finally:
            unlock(handle)
