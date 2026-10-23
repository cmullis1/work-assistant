"""1:1 preps counted from the collected data (Collected calls design, "How the numbers are counted").

A prep is one tech's numbers for one of their own 1:1 days (`settings.schedules`), counted each time it is asked for
from what the hourly sync stored (`activity.collected`): the Talk call legs and calls, the satisfaction ratings, the
solved tickets and each tech's daily aging list, with attendance from Team activity. Nothing here reads Zendesk or
stores a prep, so a prep is as current as the latest sync.

A tech's period is 14 days on their own clock (`regions`, Regions design): each day midnight to midnight on the zone
of the region in force that day. Their calls, solved tickets, ratings and attendance are counted over those days
(`activity.report._spans`, as `period_days` places them), and so is the department-wide queue their "% of calls" is
over: techs who share a 1:1 day and a clock share one count. A tech counted on another clock than the app's keeps
their own queue (`TechPrep.queue`); the period's queue is the count on the app's clock, as before.

A column whose collected kinds don't cover the tech's whole window is a dash with `NOT_COLLECTED`'s reason, and legs
stored before their call record leave the calls uncounted until a later sync stores the call. The calls' reason fails
`calls` and `queue` on the period and `calls` on each tech counted with it; every other failure is the tech's own.

However many periods are counted at once, each collected kind is read once, over the span all their windows lie in,
and each day is cut from that read (`collected.cut`): a 1:1 trends view counts hundreds of periods, most of whose days
other periods share. Each day's calls are tallied once on each clock that day is on (`_day_tally`) and each tech's
days out are judged once (`attendances`), however many periods hold them; the lists behind the numbers are made only
for the preps that show them (`items_for`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from work_assistant import regions, settings
from work_assistant.activity import collected
from work_assistant.activity import report as activity_report
from work_assistant.activity import store as activity_store
from work_assistant.activity.report import ZoneOn
from work_assistant.activity.store import iso, parse_iso
from work_assistant.oneonone.attendance import Attendance, attendances
from work_assistant.oneonone.count import ANSWERED, Call, CallCounts, CallItem, CallTally, Leg, tally
from work_assistant.oneonone.cycle import CYCLE_DAYS, PrepPeriod, period_for
from work_assistant.oneonone.model import PrepItem, TechPrep

NOT_COLLECTED = {"calls": "Calls aren't collected for this whole period.",
                 "solved": "Solved tickets aren't collected for this whole period.",
                 "satisfaction": "Ratings aren't collected for this whole period."}
NO_AGING = "Not recorded then: aging tickets are recorded each day from {day}."
NOT_FOUND = "Not found in Zendesk."
UNSYNCED = "Team activity isn't synced for this whole period."
BEFORE_ACTIVITY = "Team activity starts {day}"
NO_REASON = "No reason provided"   # what Zendesk gives a rating whose customer chose no reason

TECH_KEYS = ("calls", "solved", "satisfaction", "aging", "attendance")
COLUMN_KINDS = {"calls": ("legs", "calls"), "solved": ("tickets",), "satisfaction": ("ratings",)}   # what each needs

Clock = tuple[str, ...]   # the zone of each day of a period, and of its 1:1 day last (`_clock`)
Span = tuple[datetime, datetime]   # the stretch of time one day holds on a clock (`activity.report._spans`)


@dataclass(frozen=True)
class Counted:
    """One tech's prep for one 1:1 day, with the queue and failures of its period on the tech's clock: no queue when
    the calls weren't counted, or were counted on another clock than the app's, which keeps the tech's own on
    `tech.queue`."""
    period: PrepPeriod
    tech: TechPrep
    queue: int | None
    failures: dict[str, str]


def _short(day: date) -> str:
    """"Oct 22"."""
    return f"{day:%b} {day.day}"


def _days(period: PrepPeriod) -> list[date]:
    """The days `period` counts, oldest first."""
    return [period.start + timedelta(days=n) for n in range((period.end - period.start).days + 1)]


def _clock(zone_on: ZoneOn, period: PrepPeriod) -> Clock:
    """The zone of each day of `period` on a tech's clock (`regions.day_zones`), and of the 1:1 day after it, whose
    window can take an edge of the last day's: techs whose periods are on one clock share one count."""
    return tuple(zone_on(period.start + timedelta(days=n)) for n in range(CYCLE_DAYS + 1))


def _zone_on(clock: Clock, period: PrepPeriod) -> ZoneOn:
    """`clock` as the zone of each of its days."""
    return lambda day: clock[(day - period.start).days]


@dataclass(frozen=True)
class _Moment:
    at: datetime   # what `activity.report._on_days` places
    index: int


def period_days(moments: list[datetime], period: PrepPeriod, zone_on: ZoneOn) -> list[date | None]:
    """The day of `period` each of `moments` falls on, on a tech's clock (`zone_on`, `regions.day_zones`), or None for
    one outside it. Each day runs midnight to midnight on its own zone, and a moment a region change puts in two days'
    windows counts on the later day (the 1:1 day's included, so outside the period), as Team activity counts it
    (`activity.report._on_days`; Regions design, the day of a swap)."""
    days = _days(period)
    start, end = activity_report._window(days, zone_on)
    near = [_Moment(at, n) for n, at in enumerate(moments) if start <= at < end]
    found: list[date | None] = [None] * len(moments)
    for day, held in activity_report._on_days(near, days, zone_on).items():
        for moment in held:
            found[moment.index] = day
    return found


def _held(rows: list, spans: list[Span], when=lambda row: row[0]) -> list:
    """The rows of one read (oldest first by `when`, as `collected.cut` takes them) that the days of `spans` hold,
    oldest first: what `period_days` places on a day of the period."""
    return [row for start, end in spans for row in collected.cut(rows, start, end, when)]


def _added(tallies: list[CallTally], agent_ids: list[int]) -> CallTally:
    """One tally for `agent_ids` of the legs `tallies` counted apart (one per day of a period)."""
    techs = {}
    for agent_id in agent_ids:
        parts = [t.techs[agent_id] for t in tallies]
        techs[agent_id] = CallCounts(
            calls=sum(p.calls for p in parts), declined_missed=sum(p.declined_missed for p in parts),
            unreachable=sum(p.unreachable for p in parts), talk_seconds=sum(p.talk_seconds for p in parts),
            items=tuple(sorted((i for p in parts for i in p.items), key=lambda i: (i.at, i.leg_id))))
    return CallTally(queue=sum(t.queue for t in tallies), unmatched=sum(t.unmatched for t in tallies), techs=techs)


@dataclass(frozen=True)
class _Read:
    """Each collected kind, read once over the span every wanted window lies in, for the days to be cut from, and each
    day's call tally once counted."""
    legs: list[Leg]                                  # every agent's agent legs, oldest first
    calls: dict[int, Call]                           # the calls those legs name, by id
    ratings: list[tuple[datetime, dict]]             # every agent's, with when each was made, oldest first
    solves: dict[int, list[tuple[datetime, int]]]    # each wanted tech's, by agent id, oldest first
    clocks: dict[int, ZoneOn]                        # each wanted tech's clock (`regions.day_zones`), by agent id
    tallies: dict[tuple[date, str, Span], CallTally] = field(default_factory=dict)   # `_day_tally`'s, as counted


def _day_tally(read: _Read, day: date, zone: str, span: Span) -> CallTally:
    """The calls of one day on a clock (its span on that clock, `zone` its zone), for every wanted tech whose day is
    on that zone: counted once, however many periods hold the day."""
    key = (day, zone, span)
    if key not in read.tallies:
        legs = collected.cut(read.legs, *span, lambda leg: leg.created)
        agent_ids = [agent_id for agent_id, zone_on in read.clocks.items() if zone_on(day) == zone]
        read.tallies[key] = tally(legs, read.calls, agent_ids, PrepPeriod(day=day + timedelta(days=1), start=day,
                                                                           end=day), zone)
    return read.tallies[key]


def _count_calls(read: _Read, period: PrepPeriod, spans: list[Span], agent_ids: list[int],
                 zone_on: ZoneOn) -> tuple[CallTally | None, str | None]:
    """The period's call tally on a tech's clock from the stored legs and calls, or None and the reason when legs have
    no call record: each day's legs are tallied in that day's zone (`_day_tally`) and the days added up."""
    counted = _added([_day_tally(read, day, zone_on(day), span) for day, span in zip(_days(period), spans)], agent_ids)
    if counted.unmatched:
        noun = "call leg" if counted.unmatched == 1 else "call legs"
        return None, f"{counted.unmatched} {noun} had no matching call record."
    return counted, None


def _rating_item(rating: dict) -> PrepItem:
    reason = rating.get("reason")
    return PrepItem("rating", rating.get("ticket_id"), iso(parse_iso(rating["created_at"])), None, None,
                    score=rating["score"], reason=None if reason in (None, "", NO_REASON) else reason,
                    comment=(rating.get("comment") or "").strip() or None)


def _call_item(item: CallItem, tickets: dict[int, dict]) -> PrepItem:
    """A call's item; an answered one carries its ticket's priority and tags (none when its ticket isn't stored)."""
    if item.outcome != ANSWERED:
        return PrepItem(item.outcome, item.ticket_id, iso(item.at), item.talk, None)
    ticket = tickets.get(item.ticket_id, {})
    return PrepItem(item.outcome, item.ticket_id, iso(item.at), item.talk, None, priority=ticket.get("priority"),
                    tags=tuple(ticket.get("tags") or ()))


def _not_found(email: str) -> TechPrep:
    return TechPrep(email=email, calls=None, declined_missed=None, unreachable=None, talk_seconds=None, solved=None,
                    ratings_good=None, ratings_bad=None, aging=None, pto=None, sick=None,
                    failures={key: NOT_FOUND for key in TECH_KEYS}, items=())


@dataclass(frozen=True)
class _Group:
    """What the techs who share a 1:1 day and a clock are counted from, over their one window."""
    period: PrepPeriod
    zone_on: ZoneOn
    window: tuple[datetime, datetime]   # midnight starting the period to midnight ending it, on the clock
    spans: list[Span]                   # each day's of the period, on the clock (`activity.report._spans`)
    calls: CallTally | None             # None when the calls couldn't be counted
    calls_reason: str | None
    ratings: list[tuple[datetime, dict]] | None   # every agent's on the period's days; None when not collected
    solved: bool                        # whether solved tickets are collected for the whole window


def _group(conn: sqlite3.Connection, read: _Read, period: PrepPeriod, zone_on: ZoneOn,
           window: tuple[datetime, datetime], agent_ids: list[int]) -> _Group:
    """The group's days on the clock (`activity.report._spans`), each cut from `read`, and its calls counted."""
    start, end = window

    def covered(column: str) -> bool:
        return not collected.uncovered(conn, COLUMN_KINDS[column], start, end)

    spans = activity_report._spans(_days(period), zone_on)
    calls, reason = None, NOT_COLLECTED["calls"]
    if covered("calls"):
        calls, reason = _count_calls(read, period, spans, agent_ids, zone_on)
    ratings = _held(read.ratings, spans) if covered("satisfaction") else None
    return _Group(period=period, zone_on=zone_on, window=window, spans=spans, calls=calls, calls_reason=reason,
                  ratings=ratings, solved=covered("solved"))


def _tech_prep(conn: sqlite3.Connection, read: _Read, group: _Group, email: str, agent_id: int,
               tickets: dict[int, dict] | None, *, today: date, now: datetime, activity_from: datetime | None,
               attended: Attendance) -> TechPrep:
    """One tech's numbers for the group's period, on their clock, and the items behind them unless `tickets` is None
    (the priority and tags of each answered call's ticket, for its item)."""
    period, zone_on = group.period, group.zone_on
    listed = tickets is not None
    failures: dict[str, str] = {}
    items: list[PrepItem] = []
    numbers: dict[str, int | None] = {}

    counts = None if group.calls is None else group.calls.techs[agent_id]
    if counts is None:
        numbers.update(calls=None, declined_missed=None, unreachable=None, talk_seconds=None)
        failures["calls"] = group.calls_reason
    else:
        numbers.update(calls=counts.calls, declined_missed=counts.declined_missed, unreachable=counts.unreachable,
                       talk_seconds=counts.talk_seconds)
        if listed:
            items += [_call_item(i, tickets) for i in counts.items]

    if group.solved:
        numbers["solved"] = len(_held(read.solves[agent_id], group.spans))
    else:
        numbers["solved"] = None
        failures["solved"] = NOT_COLLECTED["solved"]

    if group.ratings is None:
        numbers.update(ratings_good=None, ratings_bad=None)
        failures["satisfaction"] = NOT_COLLECTED["satisfaction"]
    else:
        mine = [r for _, r in group.ratings if r.get("assignee_id") == agent_id]   # oldest first, as stored
        numbers.update(ratings_good=sum(r["score"] == "good" for r in mine),
                       ratings_bad=sum(r["score"] == "bad" for r in mine))
        if listed:
            items += [_rating_item(r) for r in mine]

    # The list recorded on the 1:1 day, or the latest before it. Before the tech's first, it says when recording began
    # (or, for a tech with none yet, that it begins today on their clock).
    size = collected.aging_size(conn, email, period.day)
    if size is None:
        began = collected.first_aging_day(conn, email) or regions.today_for(conn, email, now, today)
        numbers["aging"] = None
        failures["aging"] = NO_AGING.format(day=_short(began))
    else:
        numbers["aging"] = size[1]
        if listed:
            items += collected.aging_on(conn, email, period.day)[1]

    first = None if activity_from is None else activity_from.astimezone(ZoneInfo(zone_on(period.start))).date()
    out, reason = None, None
    if first is not None and first > period.start:
        reason = BEFORE_ACTIVITY.format(day=_short(first))
    else:
        out = attended
        if not out.complete:
            out, reason = None, UNSYNCED
    if out is None:
        numbers.update(pto=None, sick=None)
        failures["attendance"] = reason
    else:
        numbers.update(pto=len(out.pto), sick=len(out.sick))
        if listed:
            items += [PrepItem("pto", None, d.isoformat(), None, None) for d in out.pto]
            items += [PrepItem("sick", None, d.isoformat(), None, None) for d in out.sick]

    return TechPrep(email=email, **numbers, failures=failures, items=tuple(items))


def count_preps(conn: sqlite3.Connection, wanted: list[tuple[str, date]], *, now: datetime,
                items_for: set[tuple[str, date]] | None = None) -> list[Counted]:
    """Each (tech email, 1:1 day) in `wanted` counted from the collected data at `now`, oldest day first and each
    day's techs as wanted. A tech with no stored Zendesk id (`collected.agents`) is a dash in every column. Each prep
    has the items behind its numbers when it is in `items_for` (None: every prep), else none.

    Each tech's period is on their clock (`regions.day_zones`), and the techs of a 1:1 day are counted by clock
    (`_clock`): those on one share its window, its call tally and its ratings. The legs and calls, the ratings and the
    solved tickets are each read once, over the span every window lies in (`_Read`), each day's calls are tallied once
    (`_day_tally`), and each tech's days out are judged once (`attendances`)."""
    if not wanted:
        return []
    tz = settings.get_timezone(conn)
    today = now.astimezone(ZoneInfo(tz)).date()
    agents = collected.agents(conn)
    zones = {email: regions.day_zones(conn, email) for email in dict.fromkeys(email for email, _ in wanted)}
    order: dict[date, list[str]] = {}                           # each day's techs, as wanted
    by_clock: dict[date, dict[Clock, list[str]]] = {}           # each day's techs, by clock
    for email, day in wanted:
        order.setdefault(day, []).append(email)
        by_clock.setdefault(day, {}).setdefault(_clock(zones[email], period_for(day)), []).append(email)

    plans: dict[tuple[date, Clock], tuple[PrepPeriod, ZoneOn, tuple[datetime, datetime]]] = {}
    for day, clocks in by_clock.items():
        period = period_for(day)
        for clock in clocks:
            zone_on = _zone_on(clock, period)
            plans[day, clock] = period, zone_on, activity_report._window(_days(period), zone_on)
    start = min(window[0] for _, _, window in plans.values())
    end = max(window[1] for _, _, window in plans.values())
    legs, calls = collected.talk_between(conn, start, end)
    read = _Read(legs=legs, calls=calls, ratings=collected.ratings_between(conn, start, end),
                 solves=collected.solved_by(conn, start, end, sorted({agents[e] for e in zones if e in agents})),
                 clocks={agents[e]: zone_on for e, zone_on in zones.items() if e in agents})

    groups: dict[tuple[date, Clock], _Group] = {}
    for (day, clock), (period, zone_on, window) in plans.items():
        groups[day, clock] = _group(conn, read, period, zone_on, window,
                                    [agents[e] for e in by_clock[day][clock] if e in agents])

    def listed(email: str, day: date) -> bool:
        return items_for is None or (email, day) in items_for

    tickets = collected.ticket_facts(conn, [
        item.ticket_id for (day, clock), group in groups.items() if group.calls is not None
        for email in by_clock[day][clock] if email in agents and listed(email, day)
        for item in group.calls.techs[agents[email]].items
        if item.outcome == ANSWERED and item.ticket_id is not None])

    periods: dict[str, list[PrepPeriod]] = {}   # each tech's wanted periods
    for email, day in wanted:
        periods.setdefault(email, []).append(period_for(day))
    days_out = {email: attendances(conn, email, periods[email], today=today, now=now) for email in zones
                if email in agents}
    activity_from = activity_store.synced_from(conn)
    app_clock = (tz,) * (CYCLE_DAYS + 1)
    counted: dict[tuple[date, str], Counted] = {}
    for (day, clock), group in groups.items():
        # A tech counted on another clock than the app's keeps their own queue and leaves the period's empty; one on
        # the app's clock has the period's.
        own = group.calls is not None and clock != app_clock
        queue = None if group.calls is None or own else group.calls.queue
        reason = group.calls_reason
        for email in by_clock[day][clock]:
            tech = (_not_found(email) if email not in agents else
                    _tech_prep(conn, read, group, email, agents[email], tickets if listed(email, day) else None,
                               today=today, now=now, activity_from=activity_from, attended=days_out[email][day]))
            if own:
                tech = replace(tech, queue=group.calls.queue)
            failures = {} if reason is None else {"calls": reason, "queue": reason}
            counted[day, email] = Counted(period=group.period, tech=tech, queue=queue, failures=failures)
    return [counted[day, email] for day in sorted(order) for email in order[day]]
