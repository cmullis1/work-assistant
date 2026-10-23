"""Settings stored as JSON values in the settings table, with defaults."""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal

from work_assistant.oneonone.cycle import MAX_WEEKS, Schedule
from work_assistant.reviewer.catalog import DEFAULT_MODEL, MODELS

DEFAULTS: dict[str, Any] = {
    "zendesk_subdomain": None,
    "zendesk_email": None,
    "team": [],
    "timezone": None,  # None means the PC's zone (tzlocal)
    "budget_limit_usd": 40.0,
    "problem_threshold": 1,
    "effort": "medium",
    "model": DEFAULT_MODEL,   # the reviewing model; a run keeps the model it started with
    "retention_days": 90,
    "activity_late_minutes": 90,
    "activity_early_minutes": 90,
    "activity_gap_minutes": 120,
    "show_call_qa": True,   # off hides the Call QA section; its daily review keeps running
    # The PC copy's cut-over to the hosted copy (the hosted edition ignores all three: its own timer syncs):
    "activity_sync_enabled": True,   # off: the daily task skips the Team activity sync and Refresh is hidden
    "show_team_activity": True,      # off hides Activity and Trends; their data stays
    "show_oneonone": True,           # off hides 1:1 Prep; its data stays
    "email_domain": None,   # techs and TLs added by name get firstname.lastname@ this (names.py); see email_domain
    # each tech's 1:1s: {email: {"date": one of their 1:1 days (ISO), "weeks": the repeat, 1-8}}
    "oneonone_schedule": {},
}


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    """Return the stored value for `key`, or its default when nothing is stored."""
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return copy.deepcopy(DEFAULTS[key])
    return json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def email_domain(conn: sqlite3.Connection) -> str | None:
    """The domain techs and TLs added by name get their email at: the saved `email_domain`, else the domain most of
    the team's and the people's emails have (team order first on a tie), else the Zendesk login's, else None. The
    app has no built-in domain, so a copy that never saved one keeps the one its own people already use."""
    saved = get_setting(conn, "email_domain")
    if saved:
        return saved
    emails = team_emails(conn) + [row[0].strip().lower() for row in conn.execute("SELECT email FROM people")]
    domains = Counter(email.rpartition("@")[2] for email in emails if "@" in email)
    if domains:
        return domains.most_common(1)[0][0]
    login = (get_setting(conn, "zendesk_email") or "").strip().lower()
    return login.rpartition("@")[2] or None if "@" in login else None   # "a@" has no domain either


def team_emails(conn: sqlite3.Connection) -> list[str]:
    """The configured team's emails, stripped and lowercased."""
    return [email.strip().lower() for email in get_setting(conn, "team")]


_pinned_timezone: str | None = None


def pin_timezone(name: str | None) -> None:
    """Judge every day in this process in `name` (the server config's zone), whatever the `timezone` setting and
    the machine's zone say; None goes back to them."""
    global _pinned_timezone
    _pinned_timezone = name


def schedules(conn: sqlite3.Connection) -> dict[str, Schedule]:
    """Each team member's 1:1 schedule, in team order. Entries for emails not on the team are left out."""
    stored = get_setting(conn, "oneonone_schedule")
    return {
        email: Schedule(date.fromisoformat(stored[email]["date"]), int(stored[email]["weeks"]))
        for email in team_emails(conn)
        if email in stored
    }


def get_timezone(conn: sqlite3.Connection) -> str:
    """The IANA zone name days are judged in: the server config's when it pins one, else the configured setting,
    else the PC's zone."""
    return _pinned_timezone or get_setting(conn, "timezone") or tzlocal.get_localzone_name()


# -- validation of values the owner enters (the app's Settings page) ---------------------------------

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_SUBDOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
EMAIL_DOMAIN_PROBLEM = "Enter the company's email domain, such as example.com."
_DOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def normalize_subdomain(raw: str) -> str:
    """Reduce `acme`, `acme.zendesk.com` or `https://acme.zendesk.com/` to `acme`; ValueError if invalid."""
    value = raw.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.removesuffix("/")
    value = value.removesuffix(".zendesk.com")
    if not _SUBDOMAIN_RE.fullmatch(value):
        raise ValueError(
            f"Zendesk subdomain {raw.strip()!r} is not valid. Enter just the part before .zendesk.com "
            "(letters, digits and hyphens), for example: acme"
        )
    return value


def normalize_email(raw: str) -> str:
    value = raw.strip().lower()
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError(f"{raw.strip()!r} is not an email address")
    return value


def normalize_team(raw: Any) -> list[str]:
    """Stripped, lowercased emails with duplicates removed, in the order given."""
    if not isinstance(raw, list) or not all(isinstance(e, str) for e in raw):
        raise ValueError("The team must be a list of email addresses")
    team: list[str] = []
    for email in raw:
        value = normalize_email(email)
        if value not in team:
            team.append(value)
    return team


def _number(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError("Enter a number")
    return float(raw)


def _whole(raw: Any, minimum: int) -> int:
    value = _number(raw)
    if value != int(value) or value < minimum:
        raise ValueError(f"Enter a whole number of at least {minimum}")
    return int(value)


def normalize_schedule(raw: Any) -> dict[str, dict[str, Any]]:
    """Lowercased emails, each with an ISO 1:1 date and a whole repeat of 1 to MAX_WEEKS weeks."""
    if not isinstance(raw, dict):
        raise ValueError("The 1:1 schedule must give each tech's email a 1:1 date and a repeat")
    schedule: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"{key!r} is not an email address")
        email = normalize_email(key)
        if not isinstance(entry, dict):
            raise ValueError(f"Enter a 1:1 date and a repeat for {email}")
        try:
            day = date.fromisoformat(entry.get("date").strip()).isoformat()
        except (AttributeError, ValueError):
            raise ValueError(f"Enter the 1:1 date for {email} as a date, like 2026-10-20") from None
        weeks = entry.get("weeks")
        if (isinstance(weeks, bool) or not isinstance(weeks, (int, float)) or not math.isfinite(weeks)
                or weeks != int(weeks) or not 1 <= weeks <= MAX_WEEKS):
            raise ValueError(
                f"The repeat for {email} must be a whole number of weeks from 1 to {MAX_WEEKS}")
        schedule[email] = {"date": day, "weeks": int(weeks)}
    return schedule


def _clean(key: str, raw: Any) -> Any:
    if key == "zendesk_subdomain":
        if not isinstance(raw, str):
            raise ValueError("Enter the Zendesk subdomain")
        return normalize_subdomain(raw)
    if key == "zendesk_email":
        if not isinstance(raw, str):
            raise ValueError("Enter the Zendesk login email")
        return normalize_email(raw)
    if key == "team":
        return normalize_team(raw)
    if key == "timezone":
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None  # the PC's zone
        if not isinstance(raw, str):
            raise ValueError("Enter a time zone name such as America/Chicago")
        try:
            ZoneInfo(raw.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{raw.strip()!r} is not a known time zone") from None
        return raw.strip()
    if key == "budget_limit_usd":
        value = _number(raw)
        if value <= 0:
            raise ValueError("The budget limit must be more than $0")
        return round(value, 2)
    if key == "problem_threshold":
        return _whole(raw, 1)
    if key == "effort":
        if raw not in EFFORTS:
            raise ValueError(f"Effort must be one of: {', '.join(EFFORTS)}")
        return raw
    if key == "model":
        if not isinstance(raw, str) or raw not in MODELS:
            raise ValueError(f"Model must be one of: {', '.join(m.label for m in MODELS.values())}.")
        return raw
    if key in ("retention_days", "activity_late_minutes", "activity_early_minutes",
               "activity_gap_minutes"):
        return _whole(raw, 1)
    if key == "show_call_qa":
        if not isinstance(raw, bool):
            raise ValueError("Show Call QA must be on or off")
        return raw
    if key == "show_team_activity":
        if not isinstance(raw, bool):
            raise ValueError("Show Team activity must be on or off")
        return raw
    if key == "activity_sync_enabled":
        if not isinstance(raw, bool):
            raise ValueError("Sync Team activity must be on or off")
        return raw
    if key == "show_oneonone":
        if not isinstance(raw, bool):
            raise ValueError("Show 1:1 Prep must be on or off")
        return raw
    if key == "email_domain":
        if raw is None or isinstance(raw, str) and not raw.strip():
            return None   # nothing saved: email_domain works it out from the team's own emails
        value = raw.strip().lower().removeprefix("@") if isinstance(raw, str) else ""
        if not _DOMAIN_RE.fullmatch(value):
            raise ValueError(EMAIL_DOMAIN_PROBLEM)
        return value
    if key == "oneonone_schedule":
        return normalize_schedule(raw)
    raise KeyError(f"Unknown setting: {key}")


def validate_changes(changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Clean every value in `changes` (keys must be known). Returns (cleaned, problems by key)."""
    cleaned: dict[str, Any] = {}
    problems: dict[str, str] = {}
    for key, raw in changes.items():
        try:
            cleaned[key] = _clean(key, raw)
        except ValueError as exc:
            problems[key] = str(exc)
    return cleaned, problems


def set_settings(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    """Store several settings in one transaction."""
    for key in values:
        if key not in DEFAULTS:
            raise KeyError(f"Unknown setting: {key}")
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
