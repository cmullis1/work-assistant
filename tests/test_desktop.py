"""The "Work Assistant" Start menu shortcut. Every test uses a fake runner; real PowerShell is never called."""
import base64
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from work_assistant import cli, desktop, scheduler

PYTHONW = Path(r"C:\Tools & Co\venv\Scripts\pythonw.exe")
PROJECT = Path(r"D:\Projects\Work Assistant")
START_MENU_LINK = r"C:\Users\owner\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Work Assistant.lnk"
DESKTOP_LINK = r"C:\Users\owner\OneDrive\Desktop\Work Assistant.lnk"


class FakeRunner:
    """Records each PowerShell call and answers with one (returncode, stdout, stderr)."""

    def __init__(self, code=0, out=START_MENU_LINK + "\r\n" + DESKTOP_LINK + "\r\n", err=""):
        self.answer = (code, out, err)
        self.calls = []
        self.kwargs = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        code, out, err = self.answer
        return NS(returncode=code, stdout=out, stderr=err)


def decoded(args):
    """The script inside a `powershell ... -EncodedCommand <base64 UTF-16LE>` command line."""
    assert args[-2] == "-EncodedCommand"
    return base64.b64decode(args[-1]).decode("utf-16-le")


def assignment(script, prop):
    """The PowerShell literal assigned to `$link.<prop>`."""
    lines = [line for line in script.splitlines() if line.strip().startswith(f"$link.{prop} = ")]
    assert len(lines) == 1, f"one assignment to $link.{prop}"
    return lines[0].split(" = ", 1)[1].strip()


# -- the generated script --------------------------------------------------------------------------------


def test_script_targets_pythonw_serve_in_the_project_folder():
    script = desktop.shortcut_script(PYTHONW, PROJECT)
    assert assignment(script, "TargetPath") == r"'C:\Tools & Co\venv\Scripts\pythonw.exe'"
    assert assignment(script, "Arguments") == "'-m work_assistant.cli serve'"
    assert assignment(script, "WorkingDirectory") == r"'D:\Projects\Work Assistant'"
    assert assignment(script, "Description") == "'Open the Work Assistant app in your browser'"
    assert "$link.Save()" in script


def test_script_writes_the_start_menu_and_desktop_shortcuts():
    """Both folders come from Windows (GetFolderPath follows a OneDrive desktop). The desktop copy is the one
    Windows search finds straight away, even with desktop icons hidden; the Start menu copy can be pinned."""
    script = desktop.shortcut_script(PYTHONW, PROJECT)
    assert desktop.SHORTCUT_FOLDERS == ("Programs", "Desktop")
    assert "foreach ($folder in @('Programs', 'Desktop')) {" in script
    assert "Join-Path ([Environment]::GetFolderPath($folder)) 'Work Assistant.lnk'" in script
    assert "New-Object -ComObject WScript.Shell" in script
    assert "Remove-Item" not in script
    assert "USERPROFILE" not in script


def test_script_stops_on_the_first_error_and_reports_it():
    script = desktop.shortcut_script(PYTHONW, PROJECT)
    assert script.startswith("$ErrorActionPreference = 'Stop'")
    assert "[Console]::Error.WriteLine" in script and "exit 1" in script


def test_script_quotes_apostrophes_in_paths():
    script = desktop.shortcut_script(Path(r"C:\Users\o'brien\venv\Scripts\pythonw.exe"), Path(r"C:\Users\o'brien\QA"))
    assert assignment(script, "TargetPath") == r"'C:\Users\o''brien\venv\Scripts\pythonw.exe'"
    assert assignment(script, "WorkingDirectory") == r"'C:\Users\o''brien\QA'"


def test_powershell_args_encode_the_script():
    args = desktop.powershell_args("Write-Output 'é'")
    assert args[:3] == ["powershell", "-NoProfile", "-NonInteractive"]
    assert decoded(args) == "Write-Output 'é'"


# -- create -----------------------------------------------------------------------------------------------


def test_create_runs_the_script_for_this_venv(monkeypatch):
    runner = FakeRunner()
    paths = desktop.create(runner)
    assert paths == [START_MENU_LINK, DESKTOP_LINK]
    [args] = runner.calls
    script = decoded(args)
    python = scheduler.task_python(Path(sys.executable).absolute())
    assert assignment(script, "TargetPath") == f"'{python}'"
    assert assignment(script, "WorkingDirectory") == f"'{scheduler.PROJECT_DIR}'"
    assert runner.kwargs[0]["timeout"] == desktop.POWERSHELL_TIMEOUT_SECONDS


def test_create_reports_a_powershell_error():
    runner = FakeRunner(code=1, out="", err="Access to the path is denied.\n")
    with pytest.raises(desktop.ShortcutError, match="Access to the path is denied."):
        desktop.create(runner)


def test_create_without_powershell():
    def missing(args, **kwargs):
        raise FileNotFoundError(2, "not found", args[0])

    with pytest.raises(desktop.ShortcutError, match="PowerShell is not available"):
        desktop.create(missing)


def test_create_times_out():
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(desktop.ShortcutError, match="did not finish"):
        desktop.create(slow)


def test_real_powershell_is_blocked_in_tests():
    """The conftest guard: without a fake runner, no test can reach the real PowerShell or desktop."""
    with pytest.raises(desktop.ShortcutError, match="PowerShell is not available"):
        desktop.create()


# -- the CLI command --------------------------------------------------------------------------------------


def test_shortcut_command(monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(desktop, "_system_runner", runner)
    assert cli.main(["shortcut"]) == 0
    out = capsys.readouterr().out
    assert "Made the Work Assistant shortcuts (Start menu and desktop):" in out
    assert "  " + START_MENU_LINK in out and "  " + DESKTOP_LINK in out
    assert 'type "Work Assistant"' in out and "Pin to taskbar" in out
    assert len(runner.calls) == 1


def test_shortcut_command_failure(monkeypatch, capsys):
    monkeypatch.setattr(desktop, "_system_runner", FakeRunner(code=1, out="", err="Boom.\n"))
    assert cli.main(["shortcut"]) == 1
    assert "Could not make the Work Assistant shortcuts: Boom." in capsys.readouterr().out
