"""The Trends measures: how each tech's shifts went, week by week, from the day facts Team activity already
works out (`rules.day_facts`), and the 1:1 stats (calls, satisfaction and aging tickets) from what the hourly sync
collected (`calldays`). Nothing here is stored and nothing is written.

A shift counts only when it is over, judged in full and was scheduled: days that were not scheduled, time off,
untracked and unsynced days, today, and shifts the last sync had not finished are left out of every shift figure,
never counted as zeros. A day's calls, ratings and aging count on any day before today the collection covers, whatever
its state (a call answered on a rest day is still the tech's share of the queue); their per-shift figures count only
on worked shifts that count.
"""

import calendar
import math
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

from work_assistant import periods, regions, timeutil
from work_assistant.activity import calldays, report, store
from work_assistant.activity.calldays import CallDay
from work_assistant.activity.model import DayFacts
from work_assistant.activity.rules import _local_instant, _minutes
from work_assistant.activity.sync import first_covered_day
from work_assistant.periods import Period

COUNTED_STATES = ("worked", "zero", "missed")


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    group: str
    unit: str          # number, percent, count, minutes or duration (seconds)
    digits: int        # decimal places shown
    better: str        # higher or lower

    @property
    def team_digits(self) -> int:
        """Decimal places of the team's figures: the team's figure of a measure in `_AVERAGE_COUNT` is a mean per
        tech, so it keeps one decimal (two of six techs missing a shift is 0.3, not 0); every other measure shows
        `digits`."""
        return 1 if self.key in _AVERAGE_COUNT else self.digits


MEASURES = (
    Measure("tickets_per_shift", "Tickets per shift", "Output", "number", 1, "higher"),
    Measure("replies_per_shift", "Replies per shift", "Output", "number", 1, "higher"),
    Measure("solved_per_shift", "Solved per shift", "Output", "number", 1, "higher"),
    Measure("shifts_worked", "Shifts worked", "Reliability", "percent", 0, "higher"),
    Measure("missed_shifts", "Sick days", "Reliability", "count", 0, "lower"),
    Measure("signed_in_no_activity", "Signed in, no activity", "Reliability", "count", 0, "lower"),
    Measure("late_start_min", "First action after shift start", "Time on shift", "minutes", 0, "lower"),
    Measure("early_finish_min", "Last action before shift end", "Time on shift", "minutes", 0, "lower"),
    Measure("longest_gap_min", "Longest gap", "Time on shift", "minutes", 0, "lower"),
    Measure("flags_per_shift", "Flags per shift", "Flags", "number", 2, "lower"),
    Measure("calls_per_shift", "Calls per shift", "Calls", "number", 1, "higher"),
    Measure("calls_share", "% of calls", "Calls", "percent", 1, "higher"),
    Measure("declined_missed_per_shift", "Declined / missed per shift", "Calls", "number", 2, "lower"),
    Measure("declined_missed_pct", "% declined / missed", "Calls", "percent", 1, "lower"),
    Measure("call_time_per_shift", "Call time per shift", "Calls", "minutes", 0, "higher"),
    Measure("avg_call_time", "Avg. call time", "Calls", "duration", 0, "lower"),
    Measure("satisfaction", "Satisfaction", "Customers", "percent", 0, "higher"),
    Measure("aging_tickets", "Aging tickets", "Backlog", "count", 1, "lower"),
)


# -- tallies ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tally:
    """What a run of counted days adds up to. `scheduled` is every counted day; the sums over worked shifts are
    `tickets`, `replies`, `solved`, `early_sum`, `late_sum` (over the `late_n` worked shifts with an in-shift
    action: minutes from the shift start to the first of them, as the Late start flag measures it) and `gap_sum`
    (over the `gap_n` worked shifts with at least two in-shift actions, the only ones with a gap to measure);
    `flags` is over every scheduled shift.

    The rest come from the collected data (`calldays.CallDay`). `shift_answered`, `shift_dm` and `shift_talk` are the
    answered calls, declined and missed calls, and talk seconds of the `call_shifts` worked shifts whose calls are
    collected; `answered`, `offered` (answered, declined and missed), `talk`, `queue`, `good` and `bad` are over every
    day counted for its calls, whatever its state, and `aging_sum` over the `aging_n` of them with an aging list."""

    scheduled: int = 0
    worked: int = 0
    tickets: int = 0
    replies: int = 0
    solved: int = 0
    missed: int = 0
    zero: int = 0
    late_sum: int = 0
    late_n: int = 0
    early_sum: int = 0
    gap_sum: int = 0
    gap_n: int = 0
    flags: int = 0
    call_shifts: int = 0
    shift_answered: int = 0
    shift_dm: int = 0
    shift_talk: int = 0
    answered: int = 0
    offered: int = 0
    talk: int = 0
    queue: int = 0
    good: int = 0
    bad: int = 0
    aging_sum: int = 0
    aging_n: int = 0

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))


def _day_tally(facts: DayFacts, call: CallDay | None, zone: ZoneInfo, through: datetime | None,
               today: date) -> Tally | None:
    """The day as one scheduled shift, with its calls, ratings and aging from `call` (None: not collected), or None
    when it counts for nothing. The calls, ratings and aging count on any day before `today` they are collected for,
    whatever its state; the calls per shift only on a worked shift that counts."""
    shift = _shift_tally(facts, zone, through, today)
    if call is None or facts.day >= today:
        return shift
    on_shift = shift is not None and shift.worked == 1
    calls = Tally(
        call_shifts=int(on_shift),
        shift_answered=call.answered if on_shift else 0,
        shift_dm=call.declined_missed if on_shift else 0,
        shift_talk=call.talk if on_shift else 0,
        answered=call.answered,
        offered=call.answered + call.declined_missed,
        talk=call.talk,
        queue=call.queue,
        good=call.good,
        bad=call.bad,
        aging_sum=call.aging or 0,
        aging_n=int(call.aging is not None),
    )
    return calls if shift is None else shift + calls


def _shift_tally(facts: DayFacts, zone: ZoneInfo, through: datetime | None, today: date) -> Tally | None:
    """The day as one scheduled shift, or None when it doesn't count as one."""
    if through is None or facts.day >= today or facts.state not in COUNTED_STATES or facts.shift is None:
        return None
    end = _local_instant(facts.day, facts.shift.end_min, zone)
    if end > through:
        return None
    worked = facts.state == "worked"
    late = worked and facts.first_in_shift is not None   # a shift with no in-shift action has no start to time
    start = _local_instant(facts.day, facts.shift.start_min, zone)
    return Tally(
        scheduled=1,
        worked=int(worked),
        tickets=facts.tickets if worked else 0,
        replies=facts.replies if worked else 0,
        solved=facts.solved if worked else 0,
        missed=int(facts.state == "missed"),
        zero=int(facts.state == "zero"),
        late_sum=_minutes(facts.first_in_shift - start) if late else 0,
        late_n=int(late),
        early_sum=max(0, _minutes(end - facts.last)) if worked else 0,
        gap_sum=facts.gap_min if worked and facts.in_shift >= 2 else 0,
        gap_n=int(worked and facts.in_shift >= 2),
        flags=len(facts.flags),
    )


def _ratio(numerator: int, denominator: int) -> Fraction | None:
    return Fraction(numerator, denominator) if denominator > 0 else None


_VALUE = {
    "tickets_per_shift": lambda t: _ratio(t.tickets, t.worked),
    "replies_per_shift": lambda t: _ratio(t.replies, t.worked),
    "solved_per_shift": lambda t: _ratio(t.solved, t.worked),
    "shifts_worked": lambda t: _ratio(100 * t.worked, t.scheduled),
    "missed_shifts": lambda t: _ratio(t.missed, 1) if t.scheduled else None,
    "signed_in_no_activity": lambda t: _ratio(t.zero, 1) if t.scheduled else None,
    "late_start_min": lambda t: _ratio(t.late_sum, t.late_n),
    "early_finish_min": lambda t: _ratio(t.early_sum, t.worked),
    "longest_gap_min": lambda t: _ratio(t.gap_sum, t.gap_n),
    "flags_per_shift": lambda t: _ratio(t.flags, t.scheduled),
    "calls_per_shift": lambda t: _ratio(t.shift_answered, t.call_shifts),
    "calls_share": lambda t: _ratio(100 * t.answered, t.queue),
    "declined_missed_per_shift": lambda t: _ratio(t.shift_dm, t.call_shifts),
    "declined_missed_pct": lambda t: _ratio(100 * (t.offered - t.answered), t.offered),
    "call_time_per_shift": lambda t: _ratio(t.shift_talk, 60 * t.call_shifts),
    "avg_call_time": lambda t: _ratio(t.talk, t.answered),
    "satisfaction": lambda t: _ratio(100 * t.good, t.good + t.bad),
    "aging_tickets": lambda t: _ratio(t.aging_sum, t.aging_n),
}
_AVERAGE_COUNT = ("missed_shifts", "signed_in_no_activity", "calls_share", "aging_tickets")

# A graph's line pools a trailing window ending at each step, for every measure alike (the owner, 2026-10-22, from
# docs/research/trends-for-sparse-support-metrics/report.md). On a weekly graph Satisfaction, which has the fewest
# figures behind it, pools 8 weeks and every other measure 4; on a monthly graph every measure pools its month and
# the one before.
ROLLING_WEEKS = {"satisfaction": 8}
ROLLING_WEEKS_DEFAULT = 4
ROLLING_MONTHS = 2
# The measures that are a total over their days, not a ratio: pooling would add their weeks up, so their line is
# the average of each week's (or month's) own total over the window, on the same scale as each step's figure.
_TOTALS = ("missed_shifts", "signed_in_no_activity")
# What each figure is out of, and its name, which the graph's tooltip counts.
_BASE = {
    "tickets_per_shift": (lambda t: t.worked, "shifts"),
    "replies_per_shift": (lambda t: t.worked, "shifts"),
    "solved_per_shift": (lambda t: t.worked, "shifts"),
    "shifts_worked": (lambda t: t.scheduled, "shifts"),
    "missed_shifts": (lambda t: t.scheduled, "shifts"),
    "signed_in_no_activity": (lambda t: t.scheduled, "shifts"),
    "late_start_min": (lambda t: t.late_n, "shifts"),
    "early_finish_min": (lambda t: t.worked, "shifts"),
    "longest_gap_min": (lambda t: t.gap_n, "shifts"),
    "flags_per_shift": (lambda t: t.scheduled, "shifts"),
    "calls_per_shift": (lambda t: t.call_shifts, "shifts"),
    "calls_share": (lambda t: t.queue, "queue calls"),
    "declined_missed_per_shift": (lambda t: t.call_shifts, "shifts"),
    "declined_missed_pct": (lambda t: t.offered, "calls"),
    "call_time_per_shift": (lambda t: t.call_shifts, "shifts"),
    "avg_call_time": (lambda t: t.answered, "calls"),
    "satisfaction": (lambda t: t.good + t.bad, "ratings"),
    "aging_tickets": (lambda t: t.aging_n, "days"),
}

def rolling_span(key: str, unit: str) -> tuple[int, str]:
    """How far back a measure's line pools on a graph stepping by `unit`, as (count, "week" or "month")."""
    if unit == "week":
        return ROLLING_WEEKS.get(key, ROLLING_WEEKS_DEFAULT), "week"
    return ROLLING_MONTHS, "month"


def _span_start(end: date, span: tuple[int, str]) -> date:
    """The first day of the trailing window `span` ending on `end`: whole weeks back from it, or the first of the
    month `count - 1` months before its own."""
    count, unit = span
    if unit == "week":
        return end - timedelta(days=7 * count - 1)
    return _unit_window(end, unit, count - 1)[0]


def _unit_window(end: date, unit: str, back: int) -> tuple[date, date]:
    """The week or month `back` units before the one ending on `end`: the 7 days ending `back` weeks before it, or
    the calendar month `back` months before its own (up to `end` when it is that month)."""
    if unit == "week":
        stop = end - timedelta(days=7 * back)
        return stop - timedelta(days=6), stop
    year, month = divmod(end.year * 12 + end.month - 1 - back, 12)
    start = date(year, month + 1, 1)
    return start, min(end, start.replace(day=calendar.monthrange(start.year, start.month)[1]))


def _team_value(key: str, tallies: list[Tally]) -> Fraction | None:
    """Ratios and averages pool the techs' tallies; the two counts, the share of calls and the aging tickets are the
    mean of the techs' values, a tech with none (no shift, no queue, no aging list) left out."""
    if key in _AVERAGE_COUNT:
        values = [v for v in map(_VALUE[key], tallies) if v is not None]
        return sum(values, Fraction(0)) / len(values) if values else None
    return _VALUE[key](sum(tallies, Tally()))


def _round(value: Fraction | None, digits: int) -> int | float | None:
    """Half away from zero at `digits` places, exactly; whole numbers come back as ints."""
    if value is None:
        return None
    scaled = math.floor(abs(value) * 10 ** digits + Fraction(1, 2))
    scaled = -scaled if value < 0 else scaled
    return scaled if digits == 0 else scaled / 10 ** digits


# -- steps --------------------------------------------------------------------------------------------------


def _step_bounds(first: date, last: date, unit: str) -> list[tuple[date, date]]:
    """The weeks (Sunday to Saturday) or calendar months overlapping `first`..`last`, whole."""
    bounds = []
    if unit == "week":
        start = timeutil.week_start(first)
        while start <= last:
            bounds.append((start, start + timedelta(days=6)))
            start += timedelta(days=7)
    else:
        start = first.replace(day=1)
        while start <= last:
            end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
            bounds.append((start, end))
            start = end + timedelta(days=1)
    return bounds


def _label(start: date, unit: str) -> str:
    return f"{start:%b}" if unit == "month" else f"{start:%b} {start.day}"


# -- the payload --------------------------------------------------------------------------------------------


def days_of(period: Period, comparison: Period | None) -> list[date]:
    """The days a Trends answer reads: every day from the first of `period` and `comparison` to the last of them
    (none when both are empty)."""
    ranges = [r for r in (period, comparison) if r is not None and not r.empty]
    if not ranges:
        return []
    first, last = min(r.start for r in ranges), max(r.end for r in ranges)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def trends_view(conn, period: Period, comparison: Period | None, *, today: date, tech: str | None = None,
                emails: list[str] | None = None, now: datetime | None = None) -> dict:
    """Every measure for the team and each tech: the period's value, the comparison's, the change, and one value
    per step of the graph. `period` and `comparison` are the resolved `periods.Period`s. A value is None where
    nothing counted (no worked shift to average, no scheduled shift to rate, no call, rating or aging list
    collected). `tech` is the tech the screen picked (or None), echoed back: every tech of `emails` is still
    returned, because the graph draws them all. The department's calls and ratings are read once over all the days,
    for every tech (`calldays.call_days`). `emails` limits the techs to those (a team lead's own, one region's, or the
    team one tech is compared with), so the team's figures, and the ranking drawn from the techs', cover only them;
    None is everyone. Each tech's days are on their own clock (`regions.day_zones`), and so is their today at `now`
    (`report._tech_today`; None: `today`, the app's)."""
    th = store.thresholds(conn)
    span = report._sync_span(conn)
    synced_from, through = span

    unit = "month" if period.days > periods.WEEK_STEPS_MAX or period.preset in periods.MONTH_PRESETS else "week"
    days = days_of(period, comparison)
    first, last = (days[0], days[-1]) if days else (None, None)
    spans = sorted({rolling_span(m.key, unit) for m in MEASURES})
    total_spans = sorted({rolling_span(key, unit) for key in _TOTALS})

    # Each week or month is clipped to the window and split at the period's edges, so no step mixes days inside
    # and outside the period: the period's steps add up to its Now value. `label` names the week or month a step
    # belongs to (the tooltip's); `tick`, the graph's axis, names the step's own first day, or its month when it is
    # a whole one.
    cuts = [] if period.empty else [period.start, period.end + timedelta(days=1)]
    steps = []
    for bound_start, bound_end in _step_bounds(first, last, unit) if days else []:
        start, end = max(bound_start, first), min(bound_end, last)
        edges = [start, *sorted(c for c in cuts if start < c <= end), end + timedelta(days=1)]
        for piece_start, next_start in zip(edges, edges[1:]):
            piece_end = next_start - timedelta(days=1)
            part = (piece_start, piece_end) != (bound_start, bound_end)
            steps.append({
                "start": piece_start.isoformat(), "end": piece_end.isoformat(), "label": _label(bound_start, unit),
                "tick": _label(piece_start, "week" if part else unit),
                "in_period": not period.empty and period.start <= piece_start and piece_end <= period.end,
                "part": part,
            })

    # The windows tallied for every tech: the period, the comparison, each step, then each step's trailing window of
    # each span in `spans`, then for the totals each week (or month) of their span's windows, newest first. The first
    # steps' windows reach back before the view, so those days are read too.
    step_windows = [(date.fromisoformat(s["start"]), date.fromisoformat(s["end"])) for s in steps]
    trailing = [(_span_start(end, span), end) for span in spans for _, end in step_windows]
    units = [_unit_window(end, span[1], back) for span in total_spans for _, end in step_windows
             for back in range(span[0])]
    windows: list[tuple[date, date] | None] = [
        None if period.empty else (period.start, period.end),
        None if comparison is None or comparison.empty else (comparison.start, comparison.end),
        *step_windows,
        *trailing,
        *units,
    ]
    n_steps = len(steps)
    units_at = {}
    at = 2 + n_steps * (1 + len(spans))
    for total_span in total_spans:
        units_at[total_span] = at
        at += n_steps * total_span[0]
    read_first = min([first, *(start for start, _ in trailing)]) if days else first
    read_days = [read_first + timedelta(days=n) for n in range((last - read_first).days + 1)] if days else []

    def rolled(values: list, span: tuple[int, str] | None) -> list:
        """The steps' trailing windows of `span` from a list over `windows` (the steps themselves without one)."""
        if span is None:
            return values[2:2 + n_steps]
        at = 2 + n_steps * (1 + spans.index(span))
        return values[at:at + n_steps]

    def averaged(values: list, span: tuple[int, str]) -> list:
        """For a total: each step's average of its span's weeks (or months) that have a figure, from a list over
        `windows`; None where none has."""
        count, at = span[0], units_at[span]
        found = []
        for i in range(n_steps):
            present = [v for v in values[at + i * count:at + (i + 1) * count] if v is not None]
            found.append(sum(present, Fraction(0)) / len(present) if present else None)
        return found

    listed = report._listed(conn, report._among(report.techs(conn, today), emails), days)

    zones = {listed_tech["email"]: regions.day_zones(conn, listed_tech["email"]) for listed_tech in listed}
    calls = calldays.call_days(conn, read_days, zones)
    tallies: list[list[Tally]] = []
    for listed_tech in listed:
        email = listed_tech["email"]
        zone_on = zones[email]
        tech_today = report._tech_today(conn, email, now, today)
        facts = report._tech_facts(conn, email, read_days, zone_on, th, tech_today, span)
        per_day = [_day_tally(f, c, ZoneInfo(zone_on(f.day)), through, tech_today)
                   for f, c in zip(facts, calls[email])]
        row = []
        for window in windows:
            total = Tally()
            if window is not None:
                lo, hi = max(0, (window[0] - read_first).days), (window[1] - read_first).days
                total = sum((t for t in per_day[lo:hi + 1] if t is not None), Tally())
            row.append(total)
        tallies.append(row)

    def series(raw: list[Fraction | None], bases: list[int], digits: int, span: tuple[int, str] | None,
               total: bool, line_digits: int) -> dict:
        now, before = raw[0], raw[1]
        change = None
        if comparison is not None and now is not None and before is not None:
            change = _round(now - before, digits)
        return {"now": _round(now, digits), "before": _round(before, digits), "change": change,
                "steps": [_round(v, digits) for v in rolled(raw, None)],
                "steps_n": rolled(bases, None),
                "rolling": [_round(v, line_digits)
                            for v in (averaged(raw, span) if total and span else rolled(raw, span))],
                "rolling_n": rolled(bases, span)}

    measures = []
    for m in MEASURES:
        value = _VALUE[m.key]
        base, base_name = _BASE[m.key]
        window_span = rolling_span(m.key, unit) if days else None
        # A total's line is an average of its weeks' totals, so a tech's keeps the team's decimal: 0.3 a week.
        line_digits = m.team_digits if m.key in _TOTALS else m.digits
        measures.append({
            "key": m.key, "label": m.label, "group": m.group, "unit": m.unit, "digits": m.digits,
            "team_digits": m.team_digits, "better": m.better,
            "window": None if window_span is None else {"count": window_span[0], "unit": window_span[1]},
            "base": base_name, "line_digits": line_digits,
            "team": series([_team_value(m.key, [row[w] for row in tallies]) for w in range(len(windows))],
                           [sum(base(row[w]) for row in tallies) for w in range(len(windows))], m.team_digits,
                           window_span, m.key in _TOTALS, m.team_digits),
            "techs": [{"email": who["email"], **series([value(t) for t in row], [base(t) for t in row], m.digits,
                                                       window_span, m.key in _TOTALS, line_digits)}
                      for who, row in zip(listed, tallies)],
        })

    # The first synced day is the first whose local midnight is at or after `synced_from`, as `rules.day_facts`
    # judges it; a sync that read from mid-day leaves that day out. Each tech's is on their clock, and the stored
    # history starts on the earliest of them (the app's clock's when no tech is listed).
    coverage_from = None
    if synced_from is not None and days:
        firsts = [first_covered_day(conn, synced_from, t["email"]) for t in listed]
        first_synced = min(firsts) if firsts else first_covered_day(conn, synced_from)
        if first_synced > first:
            coverage_from = first_synced.isoformat()

    return {
        "period": period.as_dict(),
        "comparison": comparison.as_dict() if comparison is not None else None,
        "tech": tech,
        "today": today.isoformat(),
        "synced": report.synced(conn),
        "coverage_from": coverage_from,
        "unit": unit,
        "steps": steps,
        "techs": [{"email": t["email"], "name": t["name"], "region": t["region"]} for t in listed],
        "measures": measures,
    }
