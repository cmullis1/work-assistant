"""Periods for the Team overview and Agents views: presets, comparisons, trend steps and UTC windows.

Pure: no database and no I/O. Every day is a local calendar day in the owner's time zone, start and
end both included; weeks run Sunday to Saturday.

A period that reaches today, or runs past the last day reviewed (Yesterday before the morning run), is
"so far": it ends at the last day reviewed (or today, if that is earlier). When nothing in it has been
reviewed yet it is empty, ending the day before it starts. Its comparison covers the same number of
days, so the comparison of an empty period is empty too, never None: `comparison` returns None only
when the owner chose to compare with nothing.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from work_assistant import timeutil

PRESETS = ("yesterday", "this_week", "last_week", "this_month", "last_month", "last_8_weeks", "last_6_months",
           "last_year", "custom")
# The whole-month presets and their months: the months before this one, compared with as many before them, and
# trended by month whatever their length in days.
MONTH_PRESETS = {"last_6_months": 6, "last_year": 12}
MONTH_PRESET_NAMES = {"last_6_months": "Last 6 months", "last_year": "Last year"}
COMPARES = ("previous", "same_days_last_week", "custom", "none")
TREND_UNITS = ("day", "week", "month")

LAST_WEEKS = 8                 # the weeks in Last 8 weeks
SAME_DAYS_MAX = 7              # Same days last week is only for periods this long or shorter
DAY_STEPS_MAX = 31             # periods up to this many days trend by day
WEEK_STEPS_MAX = 26 * 7        # up to this many days by week; longer by month
MIN_DAY_STEPS = 14
MIN_WEEK_STEPS = 8

NOTHING_YET = "nothing reviewed yet"
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2099, 12, 31)   # the days a custom or legacy date may name
OUT_OF_RANGE = "Choose dates between 2000 and 2099."


class PeriodError(ValueError):
    """A period or comparison that can't be built, with a plain message for the owner."""


@dataclass(frozen=True)
class Period:
    start: date
    end: date            # the day before `start` when the period is empty
    preset: str          # a PRESETS value; on a comparison, the COMPARES mode that made it
    label: str
    so_far: bool
    weeks: int | None = None   # the whole weeks a week-shaped period spans when complete, so a so-far one
                               # still compares weekday for weekday; None for any other period

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def empty(self) -> bool:
        return self.end < self.start

    def as_dict(self) -> dict:
        """The API shape: `{start, end, label, so_far, preset}`, days as ISO dates."""
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label,
                "so_far": self.so_far, "preset": self.preset}


@dataclass(frozen=True)
class Step:
    start: date
    end: date
    in_period: bool      # the step overlaps the period
    so_far: bool         # the last step, running past the last day reviewed


def _in_range(*days: date | None) -> None:
    """Every given day lies between FIRST_DAY and LAST_DAY, so no shift or widening can leave the calendar."""
    if any(day is not None and not FIRST_DAY <= day <= LAST_DAY for day in days):
        raise PeriodError(OUT_OF_RANGE)


# -- labels ---------------------------------------------------------------------------------------------


def _day(day: date) -> str:
    return f"{day:%b} {day.day}"                                   # Oct 5


def _weekday_day(day: date) -> str:
    return f"{day:%a}, {_day(day)}"                                # Tue, Oct 20


def _span(start: date, end: date) -> str:
    return f"{_day(start)} – {_day(end)}"                          # Oct 11 – Oct 17


def _dated(start: date, end: date) -> str:
    """A custom range's label: "Oct 5 – Oct 14, 2026", or "Mon, Oct 19, 2026" for one day."""
    if start == end:
        return f"{_weekday_day(start)}, {start.year}"
    if start.year != end.year:
        return f"{_day(start)}, {start.year} – {_day(end)}, {end.year}"
    return f"{_span(start, end)}, {end.year}"


def _label(preset: str, start: date, end: date, so_far: bool, asked_end: date) -> str:
    empty = end < start
    if preset == "yesterday":
        return f"Yesterday ({NOTHING_YET if empty else _weekday_day(start)})"
    if preset == "last_week" and not so_far:
        return f"Last week ({_span(start, end)})"
    if preset == "last_month" and not so_far:
        return f"Last month ({start:%B %Y})"
    if preset in ("this_week", "last_week"):
        days = NOTHING_YET if empty else f"{start:%a}" if start == end else f"{start:%a}–{end:%a}"
        return f"{'This' if preset == 'this_week' else 'Last'} week so far ({days})"
    if preset in ("this_month", "last_month"):
        days = NOTHING_YET if empty else _day(start) if start == end else _span(start, end)
        return f"{'This' if preset == 'this_month' else 'Last'} month so far ({days})"
    if preset == "last_8_weeks":
        days = NOTHING_YET if empty else _span(start, end)
        return f"Last {LAST_WEEKS} weeks{' so far' if so_far else ''} ({days})"
    if preset in MONTH_PRESETS:
        name = MONTH_PRESET_NAMES[preset]
        if empty:
            return f"{name} so far ({NOTHING_YET})"
        if so_far:
            return f"{name} so far ({_span(start, end)})"
        months = f"{start:%b}" if start.year == end.year else f"{start:%b %Y}"
        return f"{name} ({months} – {end:%b %Y})"
    if empty:
        return f"{_dated(start, asked_end)} ({NOTHING_YET})"
    return f"{_dated(start, end)}{' (so far)' if so_far else ''}"


# -- periods --------------------------------------------------------------------------------------------


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _previous_month_start(day: date) -> date:
    return _month_start(_month_start(day) - timedelta(days=1))


def months_back(day: date, months: int) -> date:
    """The same day `months` calendar months earlier, clamped to that month's last day."""
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _make(preset: str, start: date, end: date, *, today: date, last_reviewed: date | None,
          weeks: int | None = None) -> Period:
    """The period from `start` to `end`. One that reaches today, or runs past the last day reviewed, is
    so far: it ends at today or the last day reviewed, whichever is earlier, and is empty when that is
    before its start. With nothing reviewed at all, only one reaching today is so far (and empty)."""
    asked_end = end
    so_far = end >= today or (last_reviewed is not None and end > last_reviewed)
    if so_far:
        reached = min(end, today, last_reviewed) if last_reviewed is not None else None
        end = reached if reached is not None and reached >= start else start - timedelta(days=1)
    return Period(start, end, preset, _label(preset, start, end, so_far, asked_end), so_far, weeks)


def weeks_ending(count: int, week: date | None, *, today: date, last_reviewed: date | None,
                 latest_week_with_data: date | None = None) -> Period:
    """`count` whole weeks ending with the week containing `week`; without one, with the latest week with
    data, else last week. A custom period (the Agents view's old `weeks` and `week` parameters)."""
    _in_range(week)
    if week is None:
        week = latest_week_with_data or timeutil.week_start(today) - timedelta(weeks=1)
    last = timeutil.week_start(week)
    return _make("custom", last - timedelta(weeks=count - 1), last + timedelta(days=6),
                 today=today, last_reviewed=last_reviewed, weeks=count)


def week_of(day: date, *, today: date, last_reviewed: date | None) -> Period:
    """The week containing `day` as a custom period labelled "Week of Oct 4" (the old `week` parameter)."""
    period = weeks_ending(1, day, today=today, last_reviewed=last_reviewed)
    label = f"Week of {_day(period.start)}{' (so far)' if period.so_far else ''}"
    return Period(period.start, period.end, period.preset, label, period.so_far, period.weeks)


def resolve(preset: str, *, today: date, last_reviewed: date | None, latest_week_with_data: date | None,
            start: date | None = None, end: date | None = None) -> Period:
    """The period a preset names, on `today`. `last_reviewed` is the last day reviewed (a counted call's
    day or a day a finished run covered, never after today); `latest_week_with_data` is the Sunday of the
    latest week with a counted review. `start` and `end` are for custom, within 2000-2099."""
    facts = {"today": today, "last_reviewed": last_reviewed}
    this_week = timeutil.week_start(today)
    if preset == "yesterday":
        day = today - timedelta(days=1)
        return _make(preset, day, day, **facts)
    if preset == "this_week":
        return _make(preset, this_week, this_week + timedelta(days=6), weeks=1, **facts)
    if preset == "last_week":
        return _make(preset, this_week - timedelta(weeks=1), this_week - timedelta(days=1), weeks=1, **facts)
    if preset == "this_month":
        return _make(preset, _month_start(today), _month_end(today), **facts)
    if preset == "last_month":
        first = _previous_month_start(today)
        return _make(preset, first, _month_end(first), **facts)
    if preset == "last_8_weeks":
        weeks = weeks_ending(LAST_WEEKS, None, latest_week_with_data=latest_week_with_data, **facts)
        return _make(preset, weeks.start, weeks.start + timedelta(weeks=LAST_WEEKS, days=-1), weeks=LAST_WEEKS,
                     **facts)
    if preset in MONTH_PRESETS:
        first = months_back(_month_start(today), MONTH_PRESETS[preset])
        return _make(preset, first, _month_start(today) - timedelta(days=1), **facts)
    if preset == "custom":
        if start is None or end is None:
            raise PeriodError("Choose the start and end day of the custom range.")
        if end < start:
            raise PeriodError("The start day must be on or before the end day.")
        _in_range(start, end)
        return _make(preset, start, end, **facts)
    raise PeriodError(f"The period must be one of: {', '.join(PRESETS)}.")


# -- comparisons ----------------------------------------------------------------------------------------


def _compared(mode: str, start: date, end: date) -> Period:
    label = "Nothing to compare yet" if end < start else _dated(start, end)
    return Period(start, end, mode, label, False)


def _shifted(period: Period, mode: str, days: int) -> Period:
    return _compared(mode, period.start - timedelta(days=days), period.end - timedelta(days=days))


def comparison(period: Period, mode: str, *, start: date | None = None, end: date | None = None) -> Period | None:
    """What `period` is compared with, by a COMPARES mode; None for "none".

    "previous" is the same length immediately before, except that week-shaped periods (`weeks` set)
    keep their weekdays, going back their whole weeks even when so far (last week against the week
    before, this week so far against the same weekdays last week, the 8 weeks against the 8 before), and
    months keep their days (last month against the month before; this month so far, or last month so far
    before the morning run reaches its last day, against the month before's 1st through the same day,
    clamped to that month's last day). "same_days_last_week" is 7 days back,
    for periods of 7 days or less. "custom" is `start` to `end`.
    """
    if mode == "none":
        return None
    if mode == "custom":
        if start is None or end is None:
            raise PeriodError("Choose the start and end day of the comparison.")
        if end < start:
            raise PeriodError("The comparison's start day must be on or before its end day.")
        _in_range(start, end)
        return _compared(mode, start, end)
    if mode == "same_days_last_week":
        if period.days > SAME_DAYS_MAX:
            raise PeriodError("Same days last week is only for periods of 7 days or less.")
        return _shifted(period, mode, 7)
    if mode != "previous":
        raise PeriodError(f"The comparison must be one of: {', '.join(COMPARES)}.")
    if period.weeks:
        return _shifted(period, mode, 7 * period.weeks)
    if period.preset == "last_month" and not period.so_far:
        first = _previous_month_start(period.start)
        return _compared(mode, first, _month_end(first))
    if period.preset in ("this_month", "last_month"):
        first = _previous_month_start(period.start)
        if period.empty:
            return _compared(mode, first, first - timedelta(days=1))
        return _compared(mode, first, first.replace(day=min(period.end.day, _month_end(first).day)))
    if period.preset in MONTH_PRESETS:
        months = MONTH_PRESETS[period.preset]
        first = months_back(period.start, months)
        if period.empty:
            return _compared(mode, first, first - timedelta(days=1))
        end = period.start - timedelta(days=1) if not period.so_far else months_back(period.end, months)
        return _compared(mode, first, end)
    return _shifted(period, mode, period.days)


# -- trends ---------------------------------------------------------------------------------------------


def trend_unit(period: Period) -> str:
    """The trend's step: "day" up to 31 days, "week" up to 26 weeks, "month" beyond, and always for the
    whole-month presets."""
    if period.preset in MONTH_PRESETS:
        return "month"
    if period.days <= DAY_STEPS_MAX:
        return "day"
    return "week" if period.days <= WEEK_STEPS_MAX else "month"


def trend_steps(period: Period, unit: str, *, last_reviewed: date | None) -> list[Step]:
    """The trend's steps, oldest first, ending with the step that holds the period's end.

    Day steps cover at least the 14 days ending with it, week steps (Sunday to Saturday) at least 8
    weeks, month steps the period's months; all cover the whole period, with context steps before it.
    The last step is `so_far` when it ends after `last_reviewed` (the last day reviewed, as for
    `resolve`), so a step the morning run hasn't reached yet never reads as complete. With nothing
    reviewed at all (`last_reviewed` None) no step is marked: there is no partly reviewed step to tell
    apart.
    """
    if unit == "day":
        first = min(period.start, period.end - timedelta(days=MIN_DAY_STEPS - 1))
        bounds = [(first + timedelta(days=i), first + timedelta(days=i))
                  for i in range((period.end - first).days + 1)]
    elif unit == "week":
        last = timeutil.week_start(period.end)
        first = min(timeutil.week_start(period.start), last - timedelta(weeks=MIN_WEEK_STEPS - 1))
        bounds = [(first + timedelta(weeks=i), first + timedelta(weeks=i, days=6))
                  for i in range((last - first).days // 7 + 1)]
    elif unit == "month":
        bounds, month = [], _month_start(min(period.start, period.end))
        while month <= period.end:
            bounds.append((month, _month_end(month)))
            month = _month_end(month) + timedelta(days=1)
    else:
        raise PeriodError(f"The trend unit must be one of: {', '.join(TREND_UNITS)}.")
    steps = [Step(s, e, s <= period.end and e >= period.start, False) for s, e in bounds]
    if steps and last_reviewed is not None and steps[-1].end > last_reviewed:
        steps[-1] = Step(steps[-1].start, steps[-1].end, steps[-1].in_period, True)
    return steps


# -- windows --------------------------------------------------------------------------------------------


def to_window(period: Period, tz: str) -> tuple[datetime, datetime]:
    """The period as aware UTC datetimes, local midnight starting it to local midnight after it (end
    exclusive), DST-safe. An empty period is the empty window at local midnight starting it."""
    if period.empty:
        start, _ = timeutil.day_window(period.start, tz)
        return start, start
    return timeutil.date_range_window(period.start, period.end, tz)
