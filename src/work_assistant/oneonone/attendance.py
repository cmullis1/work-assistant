"""Each tech's PTO and sick days in a 1:1 period, taken from Team activity (spec §4).

The days are the states Team activity gives them (`activity.report.tech_day_states`, the states of the facts the Team
activity grid shows), so the owner's marks there are what this counts: a PTO day is a workday under PTO, and a sick
day is a "missed" day (a workday marked sick, or one with no sign-in and no actions). Rest days, days off the team
and days still to come count for nothing. The days are the tech's own, on their region's clock (`regions`), as the
grid judges them. Nothing here changes the database.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from work_assistant import regions, timeutil
from work_assistant.activity import store
from work_assistant.activity.report import tech_day_states
from work_assistant.oneonone.cycle import PrepPeriod


@dataclass(frozen=True)
class Attendance:
    pto: tuple[date, ...]    # oldest first
    sick: tuple[date, ...]   # oldest first
    complete: bool           # every day of the period was synced when this was counted

    @property
    def total(self) -> int:
        return len(self.pto) + len(self.sick)


def attendance(conn: sqlite3.Connection, email: str, period: PrepPeriod, *, today: date,
               now: datetime | None = None) -> Attendance:
    """The tech's PTO and sick days from `period.start` through `period.end`, on their clock, with their today at
    `now` (`tech_day_states`; None: `today`). Not `complete` when a day of the period is unsynced, or when no complete
    sync is current to the midnight ending `period.end` on their clock."""
    return attendances(conn, email, [period], today=today, now=now)[period.day]


def attendances(conn: sqlite3.Connection, email: str, periods: list[PrepPeriod], *, today: date,
                now: datetime | None = None) -> dict[date, Attendance]:
    """`attendance` for each of `periods`, by its 1:1 day, from one read of the tech's days over the span they all
    lie in."""
    if not periods:
        return {}
    first, last = min(p.start for p in periods), max(p.end for p in periods)
    states = tech_day_states(conn, email, first, last, today=today, now=now)
    zone_on = regions.day_zones(conn, email)
    through = store.synced_through(conn)
    found = {}
    for period in periods:
        days = states[(period.start - first).days:(period.end - first).days + 1]
        _, period_end = timeutil.day_window(period.end, zone_on(period.end))
        found[period.day] = Attendance(
            pto=tuple(day for day, state in days if state == "pto"),
            sick=tuple(day for day, state in days if state == "missed"),
            complete=through is not None and through >= period_end and all(state != "unsynced" for _, state in days),
        )
    return found
