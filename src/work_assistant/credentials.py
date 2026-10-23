"""Secrets live only in Windows Credential Manager, via keyring, or, on the hosted copy, in the secrets file its
server config names (`use_file`): a JSON object readable only by the service user. Never in the repo, the
database or a log, and no message here ever quotes a secret or the file's content."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal, get_args

import keyring

SERVICE = "work-assistant"
SecretName = Literal["zendesk_token", "anthropic_api_key"]
SECRET_NAMES: tuple[str, ...] = get_args(SecretName)
FILE_MODE = 0o600   # the secrets file: read and write for its owner only (POSIX)

_secrets_file: Path | None = None


def use_file(path: Path | None) -> None:
    """Keep this process's secrets in the JSON file at `path` instead of the keyring, which is then never read or
    written; None goes back to the keyring."""
    global _secrets_file
    _secrets_file = Path(path) if path is not None else None


def _check_name(name: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret: {name!r}")


def _read_file(path: Path) -> dict:
    """The file's secrets: none when it is missing or empty (the install script creates it empty)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ValueError(f"The secrets file {path} is not a JSON object. Empty it, then save the token again "
                         "with work-assistant set-secret.")
    return data


def _write_file(path: Path, data: dict) -> None:
    """Write in place (the hosted service may write only this file, not its folder), created owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    if sys.platform != "win32":
        os.fchmod(handle, FILE_MODE)   # a file someone made by hand keeps no wider mode
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(data, out)


def get_secret(name: SecretName) -> str | None:
    _check_name(name)
    if _secrets_file is not None:
        value = _read_file(_secrets_file).get(name)
        return value if isinstance(value, str) and value else None
    return keyring.get_password(SERVICE, name)


def set_secret(name: SecretName, value: str) -> None:
    _check_name(name)
    value = value.strip()
    if not value:
        raise ValueError("Secret value must not be empty or whitespace only")
    if _secrets_file is not None:
        _write_file(_secrets_file, {**_read_file(_secrets_file), name: value})
        return
    keyring.set_password(SERVICE, name, value)


def secret_status() -> dict[str, bool]:
    """Whether each secret is stored. Never returns the values."""
    return {name: get_secret(name) is not None for name in SECRET_NAMES}
