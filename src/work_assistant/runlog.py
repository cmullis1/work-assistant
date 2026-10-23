"""Per-run log files with secret scrubbing.

Callers must never pass customer content: only ticket ids, counts, costs and error class names.
As a second line of defence, every secret substring is replaced with [REDACTED] before writing.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

LOG_DIR = Path("logs")
REDACTED = "[REDACTED]"


class LogSink(Protocol):
    """Anything that accepts progress lines; `RunLog` is the production implementation."""

    def info(self, msg: str) -> None: ...

    def warn(self, msg: str) -> None: ...


def log_path(log_dir: Path, run_id: int) -> Path:
    return Path(log_dir) / f"run-{run_id}.log"


class RunLog:
    """Appends timestamped lines (`<ISO UTC> INFO|WARN <message>`) to one run's log file."""

    def __init__(self, path: Path, secrets: Iterable[str | None]) -> None:
        self.path = Path(path)
        # Longest first, so a secret that contains another is redacted whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def _scrub(self, msg: str) -> str:
        for secret in self._secrets:
            msg = msg.replace(secret, REDACTED)
        # One message is one line: an embedded newline cannot forge another entry.
        return msg.replace("\r", " ").replace("\n", " ")

    def _write(self, level: str, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {level} {self._scrub(msg)}\n")

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)
