"""Process liveness and the single-resumer lock.

`pid_alive` never signals the process: on Windows `os.kill(pid, 0)` would terminate it, so the
check opens the process for a query and reads its exit code instead. Elsewhere (the hosted copy's Linux server) a
process that has finished but whose parent hasn't collected it yet (a zombie) still answers `os.kill(pid, 0)`, so it
is counted as gone by its state in /proc, and `reap` collects this process's own finished children.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_PROC = Path("/proc")   # Linux's process table


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # The process exists but belongs to someone this user may not query.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _zombie(pid: int) -> bool:
    """Whether `pid` has finished but its parent hasn't collected it yet (state Z in /proc/<pid>/stat, on Linux). False
    where /proc can't tell."""
    try:
        stat = (_PROC / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    # "<pid> (<command>) <state> ...": the command may hold spaces and parentheses, so the state follows the last ")".
    return stat[stat.rfind(")") + 1:].split()[:1] == ["Z"]


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running now (a zombie, finished but not yet collected, is not). Never signals
    or touches the process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return not _zombie(pid)
    except OverflowError:
        return False
    return not _zombie(pid)


def reap(pid: int) -> None:
    """Collect `pid`'s exit status if it is a child of this process that has finished, so it stops being a zombie
    (not on Windows, which has none). The web server starts each Refresh sync as its child and never waits for it, so
    one that died before it could update its row would otherwise stay in the process table. Anything else, a child
    still running or a process that isn't this one's child, is left as it is."""
    if sys.platform == "win32" or pid <= 0:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OverflowError):
        pass   # not this process's child, or already collected


def resume_lock_path(db_path: Path) -> Path:
    """The lock for collecting one database's interrupted runs, next to it (data/<name>.resume.lock)."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.stem + ".resume.lock")


def _holder(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


@contextmanager
def resume_lock(path: Path, alive: Callable[[int], bool] = pid_alive) -> Iterator[bool]:
    """Hold the single-resumer lock file at `path` for the body; yields whether it was acquired.

    The file holds the owner's process id. A lock whose process is no longer running (or whose
    content is unreadable) is stale and taken over. The lock is removed on exit only if this
    process still owns it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    me = str(os.getpid())
    acquired = False
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _holder(path)
            if holder is not None and alive(holder):
                break
            try:
                path.unlink()  # stale: its process is gone
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(me)
        acquired = True
        break
    try:
        yield acquired
    finally:
        if acquired and _holder(path) == os.getpid():
            try:
                path.unlink()
            except FileNotFoundError:
                pass
