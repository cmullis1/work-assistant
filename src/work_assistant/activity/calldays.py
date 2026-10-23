"""Each tech's calls, ratings and aging tickets day by day on their own clock, from what the hourly sync collected
(`activity.collected`): what Trends' Calls, Customers and Backlog measures add up (Collected calls design, "How the
numbers are counted"). Nothing here reads Zendesk or writes anything.

A day runs midnight to midnight on the zone of the tech's region in force that day (`regions.day_zones`), and a leg or
rating falls on the day that holds it as Team activity places an action (`activity.report._on_days`). The legs are
counted by 1:1 Prep's own rules (`oneonone.count.tally`): the tech's answered, declined and missed agent legs on inbound
calls, and the queue, every agent's answered legs on the tech's day, that their share of calls is out of. The
department's legs, calls and ratings are read once for every tech asked about, and each tech's days are cut from that
read (`collected.cut`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from work_assistant import timeutil
from work_assistant.activity import collected, report
from work_assistant.activity.report import ZoneOn
from work_assistant.oneonone.count import Call, Leg, tally
from work_assistant.oneonone.cycle import PrepPeriod
from work_assistant.oneonone.live import COLUMN_KINDS


@dataclass(frozen=True)
class CallDay:
    """One tech's day: their answered and declined/missed inbound legs and the seconds talked on the answered ones,
    every agent's answered inbound legs that day (`queue`), the good and bad ratings made that day on tickets assigned
    to them (none on a day the ratings aren't collected for), and how many tickets were on the aging list recorded for
    that day (None: none was)."""
    answered: int
    declined_missed: int
    talk: int
    queue: int
    good: int
    bad: int
    aging: int | None


@dataclass(frozen=True)
class _Held:
    at: datetime   # what `report._on_days` places
    item: object


def _by_day(pairs: list[tuple[datetime, object]], days: list[date], zone_on: ZoneOn) -> dict[date, list]:
    """Each item of `pairs` ((when, item)) under the day of `days` that holds it on the tech's clock."""
    placed = report._on_days([_Held(at, item) for at, item in pairs], days, zone_on)
    return {day: [held.item for held in items] for day, items in placed.items()}


def call_days(conn: sqlite3.Connection, days: list[date], zones: dict[str, ZoneOn]) -> dict[str, list[CallDay | None]]:
    """Each tech's CallDay for each of `days` (consecutive, oldest first), by email: `zones` names the techs, each with
    their clock (`regions.day_zones`). The department's collected legs, calls and ratings are read once, over the span
    every tech's days lie in (`report._window` on each clock); a tech whose Zendesk id no sync stored adds nothing to it
    and is None every day."""
    agents = collected.agents(conn)
    found: dict[str, list[CallDay | None]] = {email: [None] * len(days) for email in zones}
    counted = {email: zone_on for email, zone_on in zones.items() if email in agents}
    if not days or not counted:
        return found
    windows = {email: report._window(days, zone_on) for email, zone_on in counted.items()}
    start, end = min(w[0] for w in windows.values()), max(w[1] for w in windows.values())
    legs, calls = collected.talk_between(conn, start, end)
    ratings = collected.ratings_between(conn, start, end)
    for email, zone_on in counted.items():
        tech_start, tech_end = windows[email]
        found[email] = _tech_days(conn, email, agents[email], days, zone_on,
                                  collected.cut(legs, tech_start, tech_end, lambda leg: leg.created), calls,
                                  collected.cut(ratings, tech_start, tech_end))
    return found


def _tech_days(conn: sqlite3.Connection, email: str, agent_id: int, days: list[date], zone_on: ZoneOn,
               legs: list[Leg], calls: dict[int, Call], ratings: list[tuple[datetime, dict]]) -> list[CallDay | None]:
    """The tech's CallDay for each of `days`, on their clock (`zone_on`), from the legs and ratings stored over their
    days' window (`report._window`) and the calls those legs name. A day is None when the legs and calls aren't
    collected for the whole of it (`collected.uncovered`), or when a leg on it has no call record yet (it is left
    uncounted until a later sync stores the call, as 1:1 Prep leaves it)."""
    legs_on = _by_day([(leg.created, leg) for leg in legs], days, zone_on)
    rated_on = _by_day([(at, r["score"]) for at, r in ratings if r["assignee_id"] == agent_id], days, zone_on)
    aging = collected.aging_counts(conn, email, days[0], days[-1])

    found: list[CallDay | None] = []
    for day in days:
        window = timeutil.day_window(day, zone_on(day))
        counted = None
        if not collected.uncovered(conn, COLUMN_KINDS["calls"], *window):
            one_day = PrepPeriod(day=day + timedelta(days=1), start=day, end=day)
            counted = tally(legs_on.get(day, []), calls, [agent_id], one_day, zone_on(day))
        if counted is None or counted.unmatched:
            found.append(None)
            continue
        mine = counted.techs[agent_id]
        scores = [] if collected.uncovered(conn, COLUMN_KINDS["satisfaction"], *window) else rated_on.get(day, [])
        found.append(CallDay(answered=mine.calls, declined_missed=mine.declined_missed, talk=mine.talk_seconds,
                             queue=counted.queue, good=scores.count("good"), bad=scores.count("bad"),
                             aging=aging.get(day)))
    return found
