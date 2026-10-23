"""The server config file (TOML): which edition runs, where it listens and keeps its files, and, for the `hosted`
edition, its public host name, Cloudflare Access values, admin and time zone.

The owner's PC runs without one (the full edition, exactly as before). The file never holds a secret: the Zendesk
token goes in the separate `secrets_file`, written only by `work-assistant set-secret`.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from work_assistant import credentials, settings

EDITIONS = ("full", "hosted")
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
BRIDGE_PORT = 8000   # the Zendesk Bridge's, on the same server: never this app's
KEYS = ("edition", "public_host", "bind_host", "port", "db_path", "log_dir", "secrets_file", "admin_email",
        "access_team_domain", "access_aud", "timezone")
ALWAYS_REQUIRED = ("edition", "db_path", "log_dir")
HOSTED_REQUIRED = ("public_host", "admin_email", "access_team_domain", "access_aud", "timezone")
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


class ConfigError(Exception):
    """The config file is missing or unreadable, or a key is missing or has a bad value. The message says which,
    in plain words."""


@dataclass(frozen=True, kw_only=True)
class ServerConfig:
    edition: Literal["full", "hosted"]
    public_host: str | None = None        # the host name people open (behind Cloudflare Access), no scheme or path
    bind_host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT
    db_path: Path
    log_dir: Path
    secrets_file: Path | None = None      # None: the keyring, as on the PC
    admin_email: str | None = None
    access_team_domain: str | None = None
    access_aud: str | None = None
    timezone: str | None = None           # pins the zone every day is judged in (settings.pin_timezone)


def _text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be text, in quotes.")
    return value.strip()


def _host(raw: dict[str, Any], key: str, example: str) -> str | None:
    value = _text(raw, key)
    if value is not None and not _HOST_RE.fullmatch(value.lower()):
        raise ConfigError(f"{key} must be a host name such as {example}, without https:// or a path.")
    return value.lower() if value is not None else None


def _path(raw: dict[str, Any], key: str, folder: Path) -> Path | None:
    """A path as written; a relative one is taken from the config file's own folder."""
    value = _text(raw, key)
    return folder / Path(value) if value is not None else None


def _port(raw: dict[str, Any]) -> int:
    value = raw.get("port", DEFAULT_PORT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("port must be a whole number from 1 to 65535, without quotes.")
    if value == BRIDGE_PORT:   # as install.sh refuses it
        raise ConfigError(f"port {BRIDGE_PORT} is used by another service on this server (the Zendesk Bridge). "
                          f"Set port = {DEFAULT_PORT}.")
    return value


def _zone(raw: dict[str, Any]) -> str | None:
    value = _text(raw, "timezone")
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):   # OSError: a folder of zones, such as "America"
            raise ConfigError(f"timezone {value!r} is not a known time zone. Use a name such as "
                              "America/Chicago.") from None
    return value


def _email(raw: dict[str, Any]) -> str | None:
    value = _text(raw, "admin_email")
    if value is None:
        return None
    try:
        return settings.normalize_email(value)
    except ValueError:
        raise ConfigError(f"admin_email {value!r} is not an email address.") from None


def load(path: Path) -> ServerConfig:
    """Read and check the config file at `path`. Raises ConfigError naming the first problem found."""
    path = Path(path)
    try:
        with open(path, "rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"The config file {path} doesn't exist.") from None
    except OSError as exc:
        raise ConfigError(f"The config file {path} couldn't be read ({type(exc).__name__}).") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"The config file {path} isn't valid TOML: {exc}") from None
    unknown = sorted(key for key in raw if key not in KEYS)
    if unknown:
        raise ConfigError(f"The config file {path} has unknown keys: {', '.join(unknown)}. "
                          f"The keys are: {', '.join(KEYS)}.")
    edition = raw.get("edition")
    required = ALWAYS_REQUIRED + (HOSTED_REQUIRED if edition == "hosted" else ())
    missing = [key for key in required if raw.get(key) is None]
    if missing:
        raise ConfigError(f"The config file {path} is missing {', '.join(missing)}.")
    folder = path.parent
    try:
        if edition not in EDITIONS:
            raise ConfigError(f"edition must be one of: {', '.join(EDITIONS)}.")
        # Only the hosted edition checks Cloudflare's sign-in, so only it may answer on a public host: the full
        # edition there would give anyone Call QA and its paid runs.
        if edition != "hosted" and raw.get("public_host") is not None:
            raise ConfigError('public_host is only for edition = "hosted", which checks who is signed in. '
                              f'The {edition} edition has no sign-in, so it answers only on this computer: remove '
                              'public_host, or set edition = "hosted".')
        return ServerConfig(
            edition=edition,
            public_host=_host(raw, "public_host", "activity.example.com"),
            bind_host=_text(raw, "bind_host") or DEFAULT_BIND_HOST,
            port=_port(raw),
            db_path=_path(raw, "db_path", folder),
            log_dir=_path(raw, "log_dir", folder),
            secrets_file=_path(raw, "secrets_file", folder),
            admin_email=_email(raw),
            access_team_domain=_host(raw, "access_team_domain", "yourteam.cloudflareaccess.com"),
            access_aud=_text(raw, "access_aud"),
            timezone=_zone(raw),
        )
    except ConfigError as exc:
        raise ConfigError(f"In the config file {path}: {exc}") from None


def apply(config: ServerConfig) -> None:
    """Make this process follow the config: its secrets file (or the keyring when it names none) and its time
    zone (or the setting when it names none)."""
    credentials.use_file(config.secrets_file)
    settings.pin_timezone(config.timezone)
