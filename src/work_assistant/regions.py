"""Each tech's region (Regions design): NASA, EMEA or APAC. Techs serve one region and work its hours wherever they
sit, so a tech's days are judged on their region's clock: NASA on Dallas time, EMEA on Dubai's, APAC on Manila's.

A tech can move to another region (the owner, 2026-10-21), so a region is dated, as a shift is: each tech's history
is a list of (since, region) changes in `tech_regions`, and the region in force on a day is the latest change on or
before it (the earliest change's, before any). A tech with no changes is NASA on the app's own zone
(`settings.get_timezone`), so a database with no regions judges every day as before; a tech set to NASA is on Dallas
time.

Only the hosted copy's admin sets regions (Techs' Edit). The PC-to-server import never writes them and the export
never carries them (transfer.py), so an import can't reset anyone.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from work_assistant import settings

REGIONS = {"NASA": "America/Chicago", "EMEA": "Asia/Dubai", "APAC": "Asia/Manila"}   # each region's clock
ORDER = ("NASA", "EMEA", "APAC")                                                    # the order they are listed in
CITIES = {"NASA": "Dallas", "EMEA": "Dubai", "APAC": "Manila"}                      # whose time each keeps
DEFAULT = "NASA"
NOT_A_REGION = "The region must be NASA, EMEA or APAC."
REPEATED_DATE = "Each region change needs its own date."


def _email(email: str) -> str:
    return email.strip().lower()


def checked(history: list[tuple[date, str]]) -> list[tuple[date, str]]:
    """`history` as it is kept: oldest first, with a change to the region already in force left out (the earlier date
    stays). ValueError (NOT_A_REGION, REPEATED_DATE) for an unknown region or two changes on one date."""
    if not all(isinstance(region, str) and region in REGIONS for _, region in history):
        raise ValueError(NOT_A_REGION)
    if len({since for since, _ in history}) != len(history):
        raise ValueError(REPEATED_DATE)
    kept: list[tuple[date, str]] = []
    for since, region in sorted(history):
        if not kept or kept[-1][1] != region:
            kept.append((since, region))
    return kept


def set_regions(conn: sqlite3.Connection, email: str, history: list[tuple[date, str]]) -> None:
    """Make `history` the tech's whole region history, in place of any before (empty: back to none, NASA on the app's
    zone). ValueError, with nothing changed, as `checked` refuses."""
    kept = checked(history)
    address = _email(email)
    with conn:
        conn.execute("DELETE FROM tech_regions WHERE email = ?", (address,))
        conn.executemany("INSERT INTO tech_regions (email, since, region) VALUES (?, ?, ?)",
                         [(address, since.isoformat(), region) for since, region in kept])


def region_history(conn: sqlite3.Connection, email: str) -> list[tuple[date, str]]:
    """The tech's region changes, oldest first, each (since, region)."""
    return [(date.fromisoformat(row[0]), row[1]) for row in conn.execute(
        "SELECT since, region FROM tech_regions WHERE email = ? ORDER BY since", (_email(email),))]


def app_today(conn: sqlite3.Connection) -> date:
    """Today on the app's own zone by the real clock: the day a tech's current region is the one in force on, when the
    caller has no today of its own (the web app's, which a demo pins, is passed in)."""
    return datetime.now(ZoneInfo(settings.get_timezone(conn))).date()


def _in_force(history: list[tuple[date, str]], day: date) -> str | None:
    """The region `history` has in force on `day`, or None for no history."""
    if not history:
        return None
    region = history[0][1]
    for since, change in history:
        if since > day:
            break
        region = change
    return region


def region_of(conn: sqlite3.Connection, email: str, day: date | None = None) -> str:
    """The tech's region on `day` (None: their current one, in force on `app_today`); DEFAULT for a tech with none.
    A tech's current region is the one in force today, not their latest change's: an import can write a change dated
    after today, and until that day comes the tech is still in their old region."""
    return _in_force(region_history(conn, email), app_today(conn) if day is None else day) or DEFAULT


def regions_of(conn: sqlite3.Connection, emails: list[str], today: date | None = None) -> dict[str, str]:
    """Each tech's current region, by the email given: the one in force on `today` (None: `app_today`), so a change
    dated later isn't current yet; DEFAULT for a tech with none."""
    day = app_today(conn) if today is None else today
    histories: dict[str, list[tuple[date, str]]] = {}
    for email, since, region in conn.execute("SELECT email, since, region FROM tech_regions ORDER BY email, since"):
        histories.setdefault(email, []).append((date.fromisoformat(since), region))
    return {email: _in_force(histories.get(_email(email), []), day) or DEFAULT for email in emails}


def zone_of(conn: sqlite3.Connection, email: str, day: date | None = None) -> str:
    """The IANA zone the tech's `day` is judged in (None: their current region's, in force on `app_today`): their
    region's clock, or the app's own zone for a tech with no region."""
    region = _in_force(region_history(conn, email), app_today(conn) if day is None else day)
    return REGIONS[region] if region else settings.get_timezone(conn)


def day_zones(conn: sqlite3.Connection, email: str) -> Callable[[date], str]:
    """`zone_of` for any day, from one read of the tech's history: for the reports, which ask it of every day."""
    history = region_history(conn, email)
    if not history:
        zone = settings.get_timezone(conn)
        return lambda day: zone
    return lambda day: REGIONS[_in_force(history, day)]


def today_for(conn: sqlite3.Connection, email: str, now: datetime | None, today: date) -> date:
    """Today on the tech's clock: `now` in the zone of their region in force on `today` (the app's today), or
    `today` when there is no `now`."""
    return now.astimezone(ZoneInfo(zone_of(conn, email, today))).date() if now is not None else today
