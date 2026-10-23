"""The daily review as a Windows Task Scheduler entry, managed with `schtasks`.

The task runs `work_assistant.cli run --scheduled` every day at 06:00 local time, only while the owner is
logged on (so no password is ever stored), and as soon as possible after a missed start.
Every function takes a `runner` with `subprocess.run`'s signature, so tests never call real schtasks.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

TASK_NAME = "Work Assistant Daily Review"
PROJECT_DIR = Path(__file__).resolve().parents[2]  # the working directory: data/ and logs/ live here
RUN_ARGUMENTS = "-m work_assistant.cli run --scheduled"
SCHTASKS_TIMEOUT_SECONDS = 5
NOT_YET_RUN = 267011   # SCHED_S_TASK_HAS_NOT_RUN (0x41303)
RUNNING = 267009       # SCHED_S_TASK_RUNNING (0x41301)
NOT_FOUND_TEXT = "cannot find the file specified"  # how schtasks reports a missing task (English Windows)
UNAVAILABLE = ("Windows Task Scheduler (schtasks) is not available on this computer, "
               "so the daily schedule can't be managed here.")

Runner = Callable[..., Any]
_system_runner: Runner = subprocess.run  # tests replace this so real schtasks is never reached


class SchedulerUnavailable(Exception):
    """schtasks can't be run on this computer (not Windows, or it is missing)."""


class SchedulerError(Exception):
    """schtasks ran and reported an error; the message is its own output."""


def _next_0600(now: datetime) -> datetime:
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return start if start > now else start + timedelta(days=1)


def task_xml(python_exe: Path, project_dir: Path, now: datetime | None = None) -> str:
    """The task definition. Its first trigger is the next 06:00 after `now` (local time), never a past
    one, so "start when available" can't fire a run the moment the task is installed."""
    start = _next_0600(now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Work Assistant: review the previous day's calls, and any days missed.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT26H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(f'"{python_exe}"')}</Command>
      <Arguments>{RUN_ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(project_dir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(runner: Runner | None, *args: str) -> Any:
    try:
        return (runner or _system_runner)(
            ["schtasks", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SCHTASKS_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise SchedulerUnavailable(UNAVAILABLE) from None
    except subprocess.TimeoutExpired:
        raise SchedulerError(f"schtasks did not answer within {SCHTASKS_TIMEOUT_SECONDS} seconds.") from None


def _message(result: Any) -> str:
    return ((result.stderr or "").strip() or (result.stdout or "").strip()
            or f"schtasks exited with code {result.returncode}")


def task_python(executable: Path) -> Path:
    """The interpreter the task runs: pythonw.exe beside `executable` (no console window), else `executable`."""
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.is_file() else executable


def _not_found(result: Any) -> bool:
    """A failed query that means "no such task", not some other failure (access denied and the like)."""
    return result.returncode != 0 and NOT_FOUND_TEXT in f"{result.stderr or ''} {result.stdout or ''}".lower()


def _installed(runner: Runner | None, name: str) -> bool:
    """Whether the task `name` exists. Any failure other than "not found" raises `SchedulerError`."""
    result = _schtasks(runner, "/Query", "/TN", name)
    if result.returncode == 0:
        return True
    if _not_found(result):
        return False
    raise SchedulerError(_message(result))


def _delete(runner: Runner | None, name: str) -> None:
    result = _schtasks(runner, "/Delete", "/TN", name, "/F")
    if result.returncode != 0:
        raise SchedulerError(_message(result))


def install(runner: Runner | None = None) -> None:
    """Create the task, or replace it, for this Python (the project's virtual environment)."""
    xml = task_xml(task_python(Path(sys.executable).absolute()), PROJECT_DIR)
    handle, name = tempfile.mkstemp(prefix="work-assistant-task-", suffix=".xml")
    try:
        with os.fdopen(handle, "w", encoding="utf-16") as out:  # the utf-16 codec writes the BOM schtasks needs
            out.write(xml)
        result = _schtasks(runner, "/Create", "/TN", TASK_NAME, "/XML", name, "/F")
    finally:
        os.unlink(name)
    if result.returncode != 0:
        raise SchedulerError(_message(result))


def remove(runner: Runner | None = None) -> bool:
    """Delete the task. Returns whether there was one to delete."""
    if not _installed(runner, TASK_NAME):
        return False
    _delete(runner, TASK_NAME)
    return True


def _result_text(raw: str) -> str:
    try:
        code = int(raw, 0)
    except ValueError:
        return f"Error code {raw}"
    if code == 0:
        return "Succeeded"
    if code == NOT_YET_RUN:
        return "Has not run yet"
    if code == RUNNING:
        return "Running now"
    return f"Error code {code}"


def status(runner: Runner | None = None) -> dict:
    """`{installed, next_run, last_run, last_result}` from `schtasks /Query /V /FO LIST`.

    The field names are English Windows'; a field that is missing or "N/A" gives None. A failed
    query other than "not found" raises `SchedulerError`.
    """
    result = _schtasks(runner, "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
    if result.returncode != 0:
        if not _not_found(result):
            raise SchedulerError(_message(result))
        return {"installed": False, "next_run": None, "last_run": None, "last_result": None}
    fields: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, colon, value = line.partition(":")
        value = value.strip()
        if colon and value and value != "N/A":
            fields.setdefault(key.strip(), value)
    raw_result = fields.get("Last Result")
    last_result = _result_text(raw_result) if raw_result is not None else None
    never_run = last_result == "Has not run yet"
    return {
        "installed": True,
        "next_run": fields.get("Next Run Time"),
        "last_run": None if never_run else fields.get("Last Run Time"),
        "last_result": last_result,
    }
