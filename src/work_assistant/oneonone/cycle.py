"""1:1 days and the 14 days each one covers.

Pure date arithmetic, no I/O. Each tech has their own schedule (the `oneonone_schedule` setting): one
of their 1:1 days and a repeat in weeks, so their 1:1 days are `date + k * weeks * 7` days for any
whole number k. A 1:1 day's period is always the CYCLE_DAYS local days before it, whatever the repeat.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

CYCLE_DAYS = 14
HISTORY_PERIODS = 26   # back periods per tech, so Trends can show a year
MAX_WEEKS = 8          # the longest repeat a schedule may have

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _numeric(d: date) -> str:
    return f"{d.month}/{d.day}"


@dataclass(frozen=True)
class PrepPeriod:
    day: date     # the 1:1 day
    start: date   # first counted day (day - 14)
    end: date     # last counted day (day - 1)

    @property
    def label(self) -> str:
        return _short(self.day)

    @property
    def long(self) -> str:
        return f"{_short(self.start)} – {_short(self.end)}"

    @property
    def sheet_day(self) -> str:
        return f"{_numeric(self.day)}/{self.day.year}"

    @property
    def frame(self) -> str:
        return f"{_numeric(self.start)} - {_numeric(self.end)}"

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "long": self.long,
            "sheet_day": self.sheet_day,
            "frame": self.frame,
        }


def period_for(day: date) -> PrepPeriod:
    """The period a 1:1 on `day` covers: the CYCLE_DAYS days before it."""
    return PrepPeriod(day=day, start=day - timedelta(days=CYCLE_DAYS), end=day - timedelta(days=1))


@dataclass(frozen=True)
class Schedule:
    date: date   # any one of the tech's 1:1 days
    weeks: int   # the repeat, 1 to MAX_WEEKS


def _step(s: Schedule) -> int:
    return s.weeks * 7


def tech_days(s: Schedule, first: date, last: date) -> list[date]:
    """The tech's 1:1 days from `first` through `last`, oldest first."""
    step = _step(s)
    steps = -((s.date - first).days // step)  # rounded up, so this is the first 1:1 day on or after `first`
    day = s.date + timedelta(days=steps * step)
    days: list[date] = []
    while day <= last:
        days.append(day)
        day += timedelta(days=step)
    return days


def latest_day(s: Schedule, today: date) -> date:
    """The tech's latest 1:1 day on or before `today`."""
    step = _step(s)
    return s.date + timedelta(days=(today - s.date).days // step * step)


def next_day(s: Schedule, today: date) -> date:
    """The tech's first 1:1 day after `today`."""
    return latest_day(s, today) + timedelta(days=_step(s))


def is_tech_day(s: Schedule, day: date) -> bool:
    return (day - s.date).days % _step(s) == 0


def history_for(s: Schedule, today: date, n: int = HISTORY_PERIODS) -> list[date]:
    """The `n` 1:1 days before the tech's latest one (`latest_day(s, today)`), oldest first."""
    latest = latest_day(s, today)
    return [latest - timedelta(days=_step(s) * k) for k in range(n, 0, -1)]
