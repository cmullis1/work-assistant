"""The 1:1 prep page's data: each tech's latest 1:1 period, a tech's past periods and what's behind the selected one,
their trends, and the Settings schedule rows (spec §3, §5–§8).

Every prep is counted from the collected data each time the page asks for it (`live.count_preps`; Collected calls
design): nothing is made, stored or made again, so a prep is as current as the latest sync and a 1:1 schedule change
takes effect at the next look. A tech's periods are their 1:1 days (`settings.schedules`): the latest on or before
today, and the ones before it back to where the collected data begins (`_tech_days`).

Each number becomes a cell: its sheet `text` (None for a dash), its `value` (what Trends draws) and, for a dash or
a note worth showing, a plain `reason`. A blank or dashed cell is an empty field in the Copy row. A tech's days are
their local days, on their region's clock (`regions`); display dates are "Tue Oct 20" labels and "M/D" days.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from work_assistant import regions, rubric, settings, timeutil
from work_assistant.activity import collected
from work_assistant.activity import report as activity_report
from work_assistant.activity import store as activity_store
from work_assistant.activity.store import iso, parse_iso
from work_assistant.oneonone import live
from work_assistant.oneonone.cycle import (CYCLE_DAYS, PrepPeriod, Schedule, history_for, latest_day, next_day,
                                           period_for, tech_days)
from work_assistant.oneonone.live import Counted, period_days
from work_assistant.oneonone.model import PrepItem, TechPrep
from work_assistant.timing import is_exempt

NO_ROW = "No prep for that tech in that period."
TREND_SIZES = (4, 8, 13, 26)
BAD_TREND_SIZE = "Choose 4, 8, 13 or 26 1:1s."
UNREACHABLE = "{n} unreachable {rings} left out (a phone or connection fault)"
SOON = {1: "Tomorrow", 2: "In 2 days"}

# The sheet's ten numbers in its order, and which way is better (None: neither).
COLUMNS: dict[str, str | None] = {
    "calls": "higher",
    "share": "higher",
    "declined_missed": "lower",
    "dm_share": "lower",
    "talk": None,
    "avg": None,
    "solved": "higher",
    "satisfaction": "higher",
    "aging": "lower",
    "attendance": None,
}

# Trends' eight measures in spec §8's order: (key, label, unit). Which way is better comes from COLUMNS.
MEASURES = (
    ("calls", "Total calls", "count"),
    ("share", "% of calls", "percent"),
    ("declined_missed", "Declined / missed", "count"),
    ("avg", "Avg. call time", "seconds"),
    ("solved", "Solved tickets", "count"),
    ("satisfaction", "Satisfaction", "percent"),
    ("aging", "Aging tickets", "count"),
    ("attendance", "Attendance", "count"),
)

# The failure key each column's reason comes from: on the tech's row, then on the period.
_TECH_FAILURE = {"calls": "calls", "share": "calls", "declined_missed": "calls", "dm_share": "calls",
                 "talk": "calls", "avg": "calls", "solved": "solved", "satisfaction": "satisfaction",
                 "aging": "aging", "attendance": "attendance"}
_PERIOD_FAILURE = {**_TECH_FAILURE, "share": "queue"}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# -- formats ----------------------------------------------------------------------------------------------------

def pct(n: int, d: int) -> str | None:
    """`n` out of `d` as a percent with 2 decimals ("3.41%"); None when `d` is 0."""
    if d == 0:
        return None
    return f"{100 * n / d:.2f}%"


def hms(seconds: int) -> str:
    """h:mm:ss ("2:47:49")."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def avg_time(seconds: int) -> str:
    """mm:ss ("09:52"), or h:mm:ss from an hour up."""
    if seconds >= 3600:
        return hms(seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def _talk(seconds: int) -> str:
    """m:ss ("5:12", "62:03"), a call's talk time."""
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _whole_percent(n: int, d: int) -> int:
    """`n` out of `d` as a whole percent, halves rounded up as the sheet rounds them."""
    return (200 * n + d) // (2 * d)


def _label(d: date) -> str:
    """"Tue Oct 20"."""
    return f"{_WEEKDAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day}"


def _md(d: date) -> str:
    """"10/20"."""
    return f"{d.month}/{d.day}"


def _span(start: date, end: date) -> str:
    """"10/6 – 10/19", both days included."""
    return f"{_md(start)} – {_md(end)}"


# -- cells ------------------------------------------------------------------------------------------------------

def _values(tech: TechPrep, queue: int | None) -> dict[str, tuple[str | None, float | int | None]]:
    """Each column's (text, value) for one tech's prep; (None, None) for a dash. The "% of calls" is over the tech's
    own queue when they have one (counted on another clock than the app's), else over `queue`, their period's."""
    calls, dm, talk = tech.calls, tech.declined_missed, tech.talk_seconds
    queue = tech.queue if tech.queue is not None else queue
    out: dict[str, tuple[str | None, float | int | None]] = {k: (None, None) for k in COLUMNS}
    if calls is not None:
        out["calls"] = (str(calls), calls)
        if queue:
            out["share"] = (pct(calls, queue), round(100 * calls / queue, 2))
    if dm is not None:
        out["declined_missed"] = (str(dm), dm)
        if calls:
            out["dm_share"] = (pct(dm, calls), round(100 * dm / calls, 2))
    if talk is not None:
        out["talk"] = (hms(talk), talk)
        if calls:
            avg = round(talk / calls)
            out["avg"] = (avg_time(avg), avg)
    if tech.solved is not None:
        out["solved"] = (str(tech.solved), tech.solved)
    if tech.ratings_good is not None and tech.ratings_bad is not None and tech.ratings_good + tech.ratings_bad:
        percent = _whole_percent(tech.ratings_good, tech.ratings_good + tech.ratings_bad)
        out["satisfaction"] = (f"{percent}%", percent)
    if tech.aging is not None:
        out["aging"] = (str(tech.aging), tech.aging)
    if tech.pto is not None and tech.sick is not None:
        out["attendance"] = (str(tech.pto + tech.sick), tech.pto + tech.sick)
    return out


def _cells(tech: TechPrep, queue: int | None, period_failures: dict[str, str]) -> dict[str, dict]:
    """Each column's {text, value, reason}. A dash's reason is its failure; a Declined / missed count's is the
    unreachable rings left out of it, when there are any."""
    cells = {}
    for key, (text, value) in _values(tech, queue).items():
        reason = None
        if text is None:
            reason = tech.failures.get(_TECH_FAILURE[key]) or period_failures.get(_PERIOD_FAILURE[key])
        elif key == "declined_missed" and tech.unreachable:
            reason = UNREACHABLE.format(n=tech.unreachable, rings="ring" if tech.unreachable == 1 else "rings")
        cells[key] = {"text": text, "value": value, "reason": reason}
    return cells


def copy_row(period: PrepPeriod, cells: dict) -> str:
    """The sheet's 12 columns, tab-separated: the 1:1 day, the time frame, then the ten numbers (a dash is empty)."""
    texts = [cells[key]["text"] for key in COLUMNS]
    return "\t".join([period.sheet_day, period.frame, *("" if t is None else t for t in texts)])


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    return {t["email"]: t["name"] for t in activity_report.techs(conn)}


# -- which 1:1 days -----------------------------------------------------------------------------------------------

def _collected_from(conn: sqlite3.Connection) -> datetime | None:
    """Where the collected data begins: the earliest `covered_from` of the collected kinds; None before any is
    collected."""
    starts = [collected.state(conn, kind).covered_from for kind in collected.KINDS]
    return min((start for start in starts if start is not None), default=None)


def _tech_days(conn: sqlite3.Connection, email: str, s: Schedule, today: date) -> list[date]:
    """The tech's 1:1 days the page shows, newest first: their latest on or before `today`, then the ones before it
    (`history_for`) whose period starts, at midnight on the tech's clock, no earlier than where the collected data
    begins (`_collected_from`). Before anything is collected, the latest only."""
    since = _collected_from(conn)
    if since is None:
        return [latest_day(s, today)]
    zone_on = regions.day_zones(conn, email)

    def starts(day: date) -> datetime:
        first = period_for(day).start
        return timeutil.day_window(first, zone_on(first))[0]

    return [latest_day(s, today), *reversed([d for d in history_for(s, today) if starts(d) >= since])]


# -- latest per tech --------------------------------------------------------------------------------------------

def latest_view(conn: sqlite3.Connection, *, today: date, now: datetime, emails: list[str] | None = None) -> dict:
    """The page: when the data was last synced (`synced`, the latest sync's `synced_through` as ISO UTC; None before
    any), and one row per tech on the team (only those in `emails`, when given: the viewer's techs), in team order:
    their latest 1:1 day on or before `today`, counted at `now` ("counted"), or "no_schedule" for a tech with no 1:1
    date."""
    names = _names(conn)
    schedules = settings.schedules(conn)
    team = [e for e in settings.team_emails(conn) if emails is None or e in emails]
    wanted = [(email, latest_day(schedules[email], today)) for email in team if email in schedules]
    counted = {c.tech.email: c for c in live.count_preps(conn, wanted, now=now, items_for=set())}
    rows = []
    for email in team:
        row = {"email": email, "name": names.get(email) or email, "state": "no_schedule", "day": None, "label": None,
               "frame": None, "today": False, "next_day": None, "next_label": None, "soon": None, "cells": None,
               "copy": None, "unreachable": None}
        rows.append(row)
        prep = counted.get(email)
        if prep is None:
            continue
        period, following = prep.period, next_day(schedules[email], today)
        cells = _cells(prep.tech, prep.queue, prep.failures)
        row.update(state="counted", day=period.day.isoformat(), label=_label(period.day),
                   frame=_span(period.start, period.end), today=period.day == today, next_day=following.isoformat(),
                   next_label=_label(following), soon=SOON.get((following - today).days), cells=cells,
                   copy=copy_row(period, cells), unreachable=prep.tech.unreachable)
    through = activity_store.synced_through(conn)
    return {"synced": None if through is None else iso(through), "rows": rows}


# -- tech detail ------------------------------------------------------------------------------------------------

def _behind(conn: sqlite3.Connection, prep: Counted) -> dict:
    """The lists behind one tech's prep. A call is over the limit when its talk time is longer than the current
    rubric's limit and its ticket's priority and tags aren't exempt, judged now, from the rubric as it is.

    Days are on the tech's clock (`regions.day_zones`): a call or rating shows the day of the period it falls on
    (`live.period_days`). The aging list is the one recorded on the 1:1 day, or the latest before it: a ticket's age
    counts from the moment that list was recorded, and "{n}d ago" from the day it was recorded for."""
    params = rubric.current_version(conn).params
    period, tech = prep.period, prep.tech
    items = tech.items
    zone_on = regions.day_zones(conn, tech.email)
    tz = zone_on(period.day)

    def by_time(kind: str) -> list[PrepItem]:
        return sorted((i for i in items if i.kind == kind), key=lambda i: i.at)

    def days_of(listed: list[PrepItem]) -> list[str]:
        moments = [parse_iso(i.at) for i in listed]
        return [_md(found if found is not None else moment.astimezone(ZoneInfo(tz)).date())
                for moment, found in zip(moments, period_days(moments, period, zone_on))]

    calls, rated = by_time("answered"), by_time("rating")
    answered = []
    for i, shown in zip(calls, days_of(calls)):
        talk = i.talk_seconds or 0
        answered.append({"ticket_id": i.ticket_id, "day": shown, "talk": _talk(talk), "priority": i.priority,
                         "over": talk > params.call_limit_minutes * 60 and not is_exempt(i.priority, i.tags, params)})
    ratings = [{"ticket_id": i.ticket_id, "day": shown, "score": i.score, "reason": i.reason, "comment": i.comment}
               for i, shown in zip(rated, days_of(rated))]

    aging, aging_reason = None, None
    if tech.aging is None:
        aging_reason = tech.failures.get("aging")
    else:
        snapshot, recorded = collected.aging_recorded(conn, tech.email, period.day)
        aging = []
        for i in (i for i in items if i.kind == "aging"):
            replied = None if i.last_reply is None else timeutil.local_date(i.last_reply, zone_on(snapshot))
            aging.append({"ticket_id": i.ticket_id, "age_days": (recorded - parse_iso(i.at)).days,
                          "status": i.status, "last_reply": None if replied is None else _md(replied),
                          "reply_days_ago": None if replied is None else max(0, (snapshot - replied).days)})

    days_out = sorted(((date.fromisoformat(i.at), i.kind) for i in items if i.kind in ("pto", "sick")))
    return {
        "label": f"1:1 {_md(period.day)} ({_span(period.start, period.end)})",
        "answered": answered,
        "over_count": sum(c["over"] for c in answered),
        "limit": params.call_limit_minutes,
        "ratings": ratings,
        "aging": aging,
        "aging_reason": aging_reason,
        "days_out": [{"day": _md(d), "kind": kind} for d, kind in days_out],
    }


def tech_view(conn: sqlite3.Connection, email: str, day: date | None, *, today: date, now: datetime) -> dict:
    """One tech's detail, counted at `now`: each of their 1:1 periods (`_tech_days`, newest first) and what's behind
    the selected one (`day`, else their latest), the only one whose lists are made. Raises ValueError(NO_ROW) for a
    tech with no 1:1 date, or a day that isn't one of those periods."""
    s = settings.schedules(conn).get(email)
    days = [] if s is None else _tech_days(conn, email, s, today)
    if day is None and days:
        day = days[0]
    if day not in days:
        raise ValueError(NO_ROW)
    counted = live.count_preps(conn, [(email, d) for d in days], now=now,
                               items_for={(email, day)})[::-1]   # counted oldest first
    past = [{"day": c.period.day.isoformat(), "sheet_day": c.period.sheet_day, "frame": c.period.frame,
             "cells": _cells(c.tech, c.queue, c.failures), "unreachable": c.tech.unreachable} for c in counted]
    following = next_day(s, today)
    return {
        "email": email,
        "name": _names(conn).get(email) or email,
        "next_day": following.isoformat(),
        "next_label": _label(following),
        "selected": day.isoformat(),
        "past": past,
        "behind": _behind(conn, next(c for c in counted if c.period.day == day)),
    }


# -- trends -----------------------------------------------------------------------------------------------------

def _mean(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounded(value: float | int | None) -> float | int | None:
    return None if value is None else round(value, 2)


# Each trend's line pools the 1:1 periods up to each 1:1 (the owner, 2026-10-22, as Team Trends does): back-to-back
# blocks of CYCLE_DAYS ending at it, 4 (8 weeks) for Satisfaction and 2 (4 weeks) for every other measure.
TREND_BLOCKS = {"satisfaction": 4}
TREND_BLOCKS_DEFAULT = 2
# The measures that are a total over a period: their line is the average of its blocks' own totals, on the scale of
# each 1:1's figure. The others pool their parts: % of calls over the queue, the talk time over the calls, the good
# ratings over the ratings.
_PERIOD_TOTALS = ("calls", "declined_missed", "solved", "aging", "attendance")


def _pooled(key: str, preps: list[tuple[TechPrep, int | None]]) -> tuple[float | int | None, int]:
    """One measure over `preps` (each block's TechPrep and its period's queue) together, and how many ratings, calls
    or periods are behind it."""
    if key in _PERIOD_TOTALS:
        values = [v for v in (_values(t, q)[key][1] for t, q in preps) if v is not None]
        return (_rounded(sum(values) / len(values)) if values else None), len(values)
    if key == "satisfaction":
        good = sum(t.ratings_good or 0 for t, _ in preps)
        bad = sum(t.ratings_bad or 0 for t, _ in preps)
        return (_whole_percent(good, good + bad) if good + bad else None), good + bad
    if key == "avg":
        both = [(t.talk_seconds, t.calls) for t, _ in preps if t.talk_seconds is not None and t.calls]
        calls = sum(c for _, c in both)
        return (round(sum(talk for talk, _ in both) / calls) if calls else None), calls
    # % of calls: over the tech's own queue when they have one, else their period's
    parts = [(t.calls, t.queue if t.queue is not None else q) for t, q in preps if t.calls is not None]
    parts = [(c, q) for c, q in parts if q]
    queue = sum(q for _, q in parts)
    return (round(100 * sum(c for c, _ in parts) / queue, 2) if queue else None), queue


def trends_view(conn: sqlite3.Connection, email: str, n: int, *, today: date, now: datetime,
                emails: list[str] | None = None) -> dict:
    """The tech's last `n` 1:1 days (`_tech_days`, oldest first), and for each of spec §8's eight measures their
    values, the team average at each day, the latest value and its change against the mean of the earlier ones in
    the range.

    The team average at a day: for each tech on the team with a 1:1 date (only those in `emails`, when given: the
    viewer's techs, so nobody else's numbers are in it), the mean of their non-blank values over their 1:1 days that
    fall in the CYCLE_DAYS days ending on it; then the mean of those across the techs that have one. Every period
    this needs, the tech's and the team's, is counted at `now` in one `live.count_preps`."""
    if isinstance(n, bool) or n not in TREND_SIZES:
        raise ValueError(BAD_TREND_SIZE)
    schedules = settings.schedules(conn)
    s = schedules.get(email)
    days = [] if s is None else _tech_days(conn, email, s, today)[:n][::-1]
    team: dict[str, list[date]] = {}
    if days:
        first = days[0] - timedelta(days=CYCLE_DAYS - 1)
        team = {other: tech_days(theirs, first, days[-1]) for other, theirs in schedules.items()
                if emails is None or other in emails}
    # Every block a line pools: the CYCLE_DAYS periods ending at each 1:1 and the ones before them, for the tech
    # and for each tech on the team, so the team's line pools the same days.
    most = max(TREND_BLOCKS_DEFAULT, *TREND_BLOCKS.values())
    blocks = [d - timedelta(days=CYCLE_DAYS * k) for d in days for k in range(most)]
    wanted = list(dict.fromkeys([(email, d) for d in days] + [(t, d) for t, held in team.items() for d in held]
                                + [(t, b) for t in [email, *team] for b in blocks]))
    counts = {(c.tech.email, c.period.day): (c.tech, c.queue)
              for c in live.count_preps(conn, wanted, now=now, items_for=set())}
    values = {key: _values(*prep) for key, prep in counts.items()}

    def line(tech: str, key: str) -> tuple[list, list]:
        """The tech's pooled figure at each 1:1 day, and what is behind each."""
        size = TREND_BLOCKS.get(key, TREND_BLOCKS_DEFAULT)
        pooled = [_pooled(key, [counts[tech, d - timedelta(days=CYCLE_DAYS * k)] for k in range(size)]) for d in days]
        return [v for v, _ in pooled], [n for _, n in pooled]

    measures = []
    for key, label, unit in MEASURES:
        mine = [values[email, d][key][1] for d in days]
        averages = []
        for d in days:
            start = d - timedelta(days=CYCLE_DAYS - 1)
            per_tech = [_mean([v for v in (values[t, x][key][1] for x in held if start <= x <= d) if v is not None])
                        for t, held in team.items()]
            averages.append(_rounded(_mean([m for m in per_tech if m is not None])))
        latest = mine[-1] if mine else None
        earlier = _mean([v for v in mine[:-1] if v is not None])
        rolling, rolling_n = line(email, key)
        others = [line(t, key)[0] for t in team]
        team_rolling = [_rounded(_mean([o[i] for o in others if o[i] is not None])) for i in range(len(days))]
        measures.append({"key": key, "label": label, "better": COLUMNS[key], "unit": unit, "values": mine,
                         "team": averages, "latest": latest,
                         "change": None if latest is None or earlier is None else _rounded(latest - earlier),
                         "window_weeks": TREND_BLOCKS.get(key, TREND_BLOCKS_DEFAULT) * CYCLE_DAYS // 7,
                         "rolling": rolling, "rolling_n": rolling_n, "team_rolling": team_rolling})
    return {"days": [d.isoformat() for d in days], "measures": measures}


# -- Settings schedule rows -------------------------------------------------------------------------------------

def schedule_rows(conn: sqlite3.Connection, *, today: date, emails: list[str] | None = None) -> list[dict]:
    """One row per tech on the team (only those in `emails`, when given: the viewer's team), in team order: their schedule, their latest and next 1:1 days, and the days
    the next prep covers. The schedule fields are None for a tech with no 1:1 date set."""
    names = _names(conn)
    schedules = settings.schedules(conn)
    rows = []
    for email in settings.team_emails(conn):
        if emails is not None and email not in emails:
            continue
        row = {"email": email, "name": names.get(email) or email, "date": None, "weeks": None,
               "latest_label": None, "next_label": None, "covers": None}
        s = schedules.get(email)
        if s is not None:
            following = period_for(next_day(s, today))
            row.update(date=s.date.isoformat(), weeks=s.weeks, latest_label=_label(latest_day(s, today)),
                       next_label=_label(following.day), covers=_span(following.start, following.end))
        rows.append(row)
    return rows
