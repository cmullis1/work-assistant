"""Process liveness on the hosted copy's Linux server (final review F8): a process that has finished but whose parent
hasn't collected it (a zombie) still answers `os.kill(pid, 0)`, so `pid_alive` counts it as gone by its state in
/proc, and `reap` collects this process's own finished children. The tests run on Windows, so the POSIX calls are
stand-ins: nothing here signals, waits for or reads a real process."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from work_assistant import procutil

PID = 4242
NO_CHILD = ChildProcessError(10, "No child processes")


class FakeOs:
    """`os` as procutil uses it on Linux: `kill(pid, 0)` finds `running` (zombies included, as the real one does) and
    `waitpid` collects the finished children in `children`."""
    WNOHANG = 1

    def __init__(self, running=(), children=()):
        self.running, self.children, self.calls = set(running), dict(children), []

    def kill(self, pid, signal):
        self.calls.append(("kill", pid, signal))
        if pid not in self.running:
            raise ProcessLookupError(3, "No such process")

    def waitpid(self, pid, options):
        self.calls.append(("waitpid", pid, options))
        if pid not in self.children:
            raise NO_CHILD
        if self.children[pid] == "running":
            return 0, 0
        del self.children[pid]
        self.running.discard(pid)   # collected: gone from the process table
        return pid, 9 << 8


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """procutil as on Linux, with /proc under tmp_path. Returns a function that gives a process its state there."""
    monkeypatch.setattr(procutil, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(procutil, "_PROC", tmp_path / "proc")

    def state(pid: int, letter: str, command: str = "python3 -m work_assistant.cli (activity) sync") -> None:
        folder = tmp_path / "proc" / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "stat").write_text(f"{pid} ({command}) {letter} 1 {pid} {pid} 0 -1 4194560\n", encoding="ascii")

    return state


def test_a_zombie_is_not_alive(linux, monkeypatch):
    fake = FakeOs(running={PID})
    monkeypatch.setattr(procutil, "os", fake)
    linux(PID, "Z")
    assert procutil.pid_alive(PID) is False, "os.kill finds it, but it runs nothing"
    linux(PID, "S")
    assert procutil.pid_alive(PID) is True
    linux(PID, "R", command="odd ) name (Z")
    assert procutil.pid_alive(PID) is True, "the state is the field after the command's last parenthesis"
    assert all(call[0] == "kill" and call[2] == 0 for call in fake.calls), "never a real signal, never a wait"


def test_without_proc_a_process_os_kill_finds_is_alive(linux, monkeypatch):
    monkeypatch.setattr(procutil, "os", FakeOs(running={PID}))
    assert procutil.pid_alive(PID) is True
    assert procutil.pid_alive(PID + 1) is False


def test_another_users_zombie_is_not_alive(linux, monkeypatch):
    class Theirs(FakeOs):
        def kill(self, pid, signal):
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(procutil, "os", Theirs())
    linux(PID, "Z")
    assert procutil.pid_alive(PID) is False
    linux(PID, "S")
    assert procutil.pid_alive(PID) is True


def test_reap_collects_a_finished_child_and_leaves_anything_else(linux, monkeypatch):
    fake = FakeOs(running={PID, PID + 1, PID + 2}, children={PID: "finished", PID + 1: "running"})
    monkeypatch.setattr(procutil, "os", fake)
    for pid in (PID, PID + 1, PID + 2, 0):
        procutil.reap(pid)   # a running child, and a process that isn't this one's child, raise nothing
    assert fake.calls == [("waitpid", PID, 1), ("waitpid", PID + 1, 1), ("waitpid", PID + 2, 1)], \
        "never a blocking wait"
    assert fake.running == {PID + 1, PID + 2}, "only the finished child is collected"
    procutil.reap(PID)
    assert fake.calls[-1] == ("waitpid", PID, 1), "collected already: no error"


def test_windows_is_unchanged(monkeypatch):
    fake, asked = FakeOs(running={PID}, children={PID: "finished"}), []
    monkeypatch.setattr(procutil, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(procutil, "os", fake)
    monkeypatch.setattr(procutil, "_windows_pid_alive", lambda pid: asked.append(pid) or True)
    assert procutil.pid_alive(PID) is True
    procutil.reap(PID)
    assert asked == [PID] and fake.calls == [], "the exit-code query only: no os.kill, no waitpid, no /proc"
