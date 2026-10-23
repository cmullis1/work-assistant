"""Plain data types for the Team activity section."""

from dataclasses import dataclass
from datetime import date, datetime

TIME_OFF_KINDS = ("pto", "sick")   # PTO excuses the day; a marked sick day counts as a Sick day


@dataclass(frozen=True)
class ShiftVersion:
    """One tech's schedule from `effective_from` until the next version.

    `work_days` uses Sunday = 0 .. Saturday = 6. Minutes count from local midnight.
    `active` is False once the tech has left the team.
    """

    email: str
    effective_from: date
    work_days: frozenset[int]
    start_min: int
    end_min: int
    active: bool


@dataclass(frozen=True)
class TimeOff:
    id: int
    email: str
    first: date
    last: date
    note: str
    kind: str = "pto"


@dataclass(frozen=True)
class Thresholds:
    late_min: int = 90
    early_min: int = 90
    gap_min: int = 120


@dataclass(frozen=True)
class Action:
    """One thing a tech did on a ticket. `at` is UTC.

    `kind` is one of reply, note, call, status, assignee, group, priority,
    fields, created, merge.
    """

    email: str
    ticket_id: int
    at: datetime
    kind: str
    old: str | None
    new: str | None
    event_id: int
    child_id: int


@dataclass(frozen=True)
class SignIn:
    email: str
    at: datetime


@dataclass(frozen=True)
class Flag:
    """`kind` is one of missed, no_activity, late_start, early_finish, long_gap."""

    kind: str
    minutes: int | None


@dataclass(frozen=True)
class DayFacts:
    """One tech's day.

    `state` is one of worked, zero, missed, rest, pto, off, future, untracked, unsynced.
    "off" means the tech worked a rest day. "untracked" means the day falls
    before their first shift version, or under an inactive version. "unsynced"
    means no sync has reached the day (or the part of its shift that matters), so
    it is not judged. `shift` is the shift the tech was due to work that day, or
    None on a rest day, PTO, or a day off the team; a marked sick day keeps its shift hours. `in_shift` is the
    number of actions between the shift's start and end (both included) on a scheduled day, else 0, and
    `first_in_shift` the first of them (None when there is none); nothing recorded on a marked sick day counts as
    in-shift. `off` is the kind of time off covering the day ("pto" or "sick"), or None, whatever the state.
    """

    day: date
    state: str
    tickets: int
    replies: int
    notes: int
    calls: int
    solved: int
    signin: datetime | None
    first: datetime | None
    last: datetime | None
    gap_min: int
    gap_start: datetime | None
    flags: tuple[Flag, ...]
    shift: ShiftVersion | None
    in_shift: int
    first_in_shift: datetime | None
    off: str | None = None
