"""The "Work Assistant" shortcuts, made with PowerShell's WScript.Shell COM object.

Each shortcut starts the app (`work_assistant.cli serve`) with the virtual environment's pythonw.exe, so no
console window stays open, in the project folder, where data/ and logs/ live. Two identical shortcuts
are written, in folders Windows itself names: the Start menu's Programs folder (so it can be pinned to
the taskbar) and the desktop. Windows search indexes the desktop at once, even when desktop icons are
hidden, while a new Start menu entry can take a long time to show up in search.
`create` takes a `runner` with `subprocess.run`'s signature, so tests never make a real shortcut.
"""
from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from work_assistant import scheduler

SHORTCUT_NAME = "Work Assistant"
APP_ARGUMENTS = "-m work_assistant.cli serve"
DESCRIPTION = "Open the Work Assistant app in your browser"
POWERSHELL_TIMEOUT_SECONDS = 60
SHORTCUT_FOLDERS = ("Programs", "Desktop")  # Environment.SpecialFolder names: the Start menu, the desktop
UNAVAILABLE = "PowerShell is not available on this computer, so the shortcuts can't be made here."

Runner = Callable[..., Any]
_system_runner: Runner = subprocess.run  # tests replace this so real PowerShell is never reached


class ShortcutError(Exception):
    """The shortcut could not be made; the message says why."""


def _literal(text: str) -> str:
    """A PowerShell single-quoted string: nothing inside is expanded, and ' is written ''."""
    return "'" + text.replace("'", "''") + "'"


def shortcut_script(python_exe: Path, project_dir: Path) -> str:
    """The PowerShell that writes (or replaces) both shortcuts and prints each path on its own line."""
    folders = ", ".join(_literal(folder) for folder in SHORTCUT_FOLDERS)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "try {",
        "    $shell = New-Object -ComObject WScript.Shell",
        f"    foreach ($folder in @({folders})) {{",
        f"        $path = Join-Path ([Environment]::GetFolderPath($folder)) {_literal(SHORTCUT_NAME + '.lnk')}",
        "        $link = $shell.CreateShortcut($path)",
        f"        $link.TargetPath = {_literal(str(python_exe))}",
        f"        $link.Arguments = {_literal(APP_ARGUMENTS)}",
        f"        $link.WorkingDirectory = {_literal(str(project_dir))}",
        f"        $link.Description = {_literal(DESCRIPTION)}",
        "        $link.Save()",
        "        Write-Output $path",
        "    }",
        "} catch {",
        "    [Console]::Error.WriteLine($_.Exception.Message)",
        "    exit 1",
        "}",
    ])


def powershell_args(script: str) -> list[str]:
    """The command line that runs `script`. -EncodedCommand (base64 of UTF-16LE) carries the script
    whole, so no quote or space in a path can be mangled on the way."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def create(runner: Runner | None = None) -> list[str]:
    """Make both shortcuts for this Python (the project's virtual environment); return their paths."""
    script = shortcut_script(scheduler.task_python(Path(sys.executable).absolute()), scheduler.PROJECT_DIR)
    try:
        result = (runner or _system_runner)(
            powershell_args(script), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=POWERSHELL_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise ShortcutError(UNAVAILABLE) from None
    except subprocess.TimeoutExpired:
        raise ShortcutError(f"PowerShell did not finish within {POWERSHELL_TIMEOUT_SECONDS} seconds.") from None
    if result.returncode != 0:
        raise ShortcutError((result.stderr or "").strip() or (result.stdout or "").strip()
                            or f"PowerShell exited with code {result.returncode}")
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
