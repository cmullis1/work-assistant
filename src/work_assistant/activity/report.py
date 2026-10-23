"""What the Team activity screens show: the range grid, one tech's day, and the shift list, as JSON-ready dicts.

Times are "HH:MM" and days are ISO dates, each on the tech's own clock (`regions`: their region's zone on that day, or
the owner's time zone for a tech with no region). Every day is worked out in local time from the stored actions by
`rules.day_facts`; nothing here changes the database.
"""

import sqlite3
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from work_assistant import regions, settings, timeutil
from work_assistant.activity import collected, store
from work_assistant.activity.model import Action, DayFacts, ShiftVersion, SignIn, Thresholds, TimeOff
from work_assistant.activity.rules import day_facts, day_state
from work_assistant.activity.sync import stale_after
from work_assistant.names import stored as typed_names
from work_assistant.periods import Period
from work_assistant.procutil import pid_alive, reap

RECENT_DAYS = 7
SyncSpan = tuple[datetime | None, datetime | None]
ZoneOn = Callable[[date], str]   # the zone a tech's day is judged in, for any day (`regions.day_zones`)


def clock(minutes: int) -> str:
    """Minutes after local midnight as "HH:MM"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _local_clock(moment: datetime | None, zone: ZoneInfo) -> str | None:
    return moment.astimezone(zone).strftime("%H:%M") if moment is not None else None


def cell(facts: DayFacts, tz: str) -> dict:
    """One tech's day as the grid and the day view show it. `shift` is the hours the tech was due to work that
    day, or None on a day they were not: a rest day (worked or not), time off, or a day with no active shift
    (before the tech joined, or after they left). `time_off` is the kind of time off covering the day ("pto" or
    "sick"), or None: a Sick day with `time_off` "sick" was marked by the owner, one with None is automatic."""
    zone = ZoneInfo(tz)
    shift = facts.shift
    return {
        "day": facts.day.isoformat(),
        "state": facts.state,
        "tickets": facts.tickets,
        "replies": facts.replies,
        "notes": facts.notes,
        "calls": facts.calls,
        "solved": facts.solved,
        "signin": _local_clock(facts.signin, zone),
        "first": _local_clock(facts.first, zone),
        "last": _local_clock(facts.last, zone),
        "gap_min": facts.gap_min,
        "gap_start": _local_clock(facts.gap_start, zone),
        "flags": [{"kind": f.kind, "minutes": f.minutes} for f in facts.flags],
        "shift": {"start": clock(shift.start_min), "end": clock(shift.end_min)}
        if shift is not None and shift.active else None,
        "time_off": facts.off,
    }


# -- who is listed ------------------------------------------------------------------------------------------


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    """Each tech's name: the one typed when they were added, or brought over with the PC's Team activity
    (`names.stored`: the hosted copy has no reviewed calls), else the name on their newest reviewed call."""
    found = typed_names(conn)
    for row in conn.execute(
        "SELECT lower(trim(agent_email)) AS email, agent_name FROM contacts ORDER BY call_started_at DESC"
    ):
        if row["agent_name"]:
            found.setdefault(row["email"], row["agent_name"])
    return found


def techs(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Everyone the section can show: the team, and anyone with a shift on record (a tech who left keeps their
    history). `{email, name, region}` by name; a tech with no name (`_names`) shows their email, and `region` is
    their current one, in force on `today`, the app's (`regions.regions_of`; None: by the real clock)."""
    emails = set(settings.team_emails(conn))
    emails.update(r["email"] for r in conn.execute("SELECT DISTINCT email FROM shift_versions"))
    names = _names(conn)
    region = regions.regions_of(conn, list(emails), today)
    listed = [{"email": e, "name": names.get(e) or e, "region": region[e]} for e in emails]
    listed.sort(key=lambda t: (t["name"].lower(), t["email"]))
    return listed


# -- the sync's state ---------------------------------------------------------------------------------------


def sync_running(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether an activity sync is running in a live process. A `running` row whose process is gone, or
    that started longer ago than a sync can take, is a crash and does not count. A sync this process started
    (Refresh) that has finished is collected first (`reap`), so one that died before it could update its row
    counts as gone at once, rather than blocking every sync until its row is stale."""
    now = now or datetime.now(timezone.utc)
    for row in conn.execute("SELECT pid, started_at, fill_techs FROM activity_sync WHERE status = 'running'"):
        if row["pid"] is not None and store.parse_iso(row["started_at"]) >= now - stale_after(row):
            reap(row["pid"])
            if pid_alive(row["pid"]):
                return True
    return False


def synced(conn: sqlite3.Connection) -> dict:
    """`at` is the instant the newest complete sync was current to (UTC, like 2026-10-19T11:00:00Z)."""
    latest = store.latest_sync(conn)
    through = store.synced_through(conn)
    return {
        "at": store.iso(through) if through is not None else None,
        "status": latest["status"] if latest else None,
        "running": sync_running(conn),
    }


# -- days -----------------------------------------------------------------------------------------------------


def _version_on(versions: list[ShiftVersion], day: date) -> ShiftVersion | None:
    """The newest version in force on `day` (`versions` are oldest first), like `store.shift_on`."""
    current = None
    for version in versions:
        if version.effective_from > day:
            break
        current = version
    return current


def _active(version: ShiftVersion | None) -> bool:
    return version is not None and version.active


def _facts(day: date, actions: list[Action], signins: list[SignIn], version: ShiftVersion | None,
           off: str | None, th: Thresholds, tz: str, today: date, span: SyncSpan,
           solved_at: list[datetime]) -> DayFacts:
    """`rules.day_facts` as far as the data is synced (`span`), except that a day after `today` with no active
    shift (before the tech joined, after they left) is "untracked" rather than "future": only days they will be
    on the team are still to come."""
    facts = day_facts(day, actions, signins, version, off, th, tz, today,
                      synced_from=span[0], synced_through=span[1], solved_at=solved_at)
    return replace(facts, state="untracked") if _untracked(facts.state, version) else facts


def _untracked(state: str, version: ShiftVersion | None) -> bool:
    """Whether a day `rules` judged as `state` is "untracked" here: one after today with no active shift."""
    return state == "future" and not _active(version)


def _sync_span(conn: sqlite3.Connection) -> SyncSpan:
    """(the earliest instant the stored activity covers, the instant the newest complete sync was current to)."""
    return store.synced_from(conn), store.synced_through(conn)


def _off_on(offs: list[TimeOff], day: date) -> str | None:
    """The kind of time off covering `day` ("pto" or "sick"), or None. A day under both is sick."""
    kinds = {o.kind for o in offs if o.first <= day <= o.last}
    return "sick" if "sick" in kinds else "pto" if kinds else None


def _window(days: list[date], zone_on: ZoneOn) -> tuple[datetime, datetime]:
    """Midnight starting the first of `days` (consecutive) to midnight ending the last, each on its own day's clock.
    Every day's window lies inside it: a day's midnight always comes after the one before's, the zones being less
    than a day apart."""
    return timeutil.day_window(days[0], zone_on(days[0]))[0], timeutil.day_window(days[-1], zone_on(days[-1]))[1]


def _spans(days: list[date], zone_on: ZoneOn) -> list[tuple[datetime, datetime]]:
    """The stretch of time each of `days` (consecutive) holds, as `_on_days` places what happened: from midnight
    starting the day on its clock (`zone_on`) to the earlier of midnight ending it on its clock and midnight starting
    the next day on the next day's clock. A region change leaves an instant in two days' windows, which counts on the
    later day, or in no window, which counts on none (Regions design, the day of a swap); the day after `days` takes
    its part of the last day's window as the grid would. Each span starts where the one before it ends, or later."""
    every = [*days, days[-1] + timedelta(days=1)]
    zones = [zone_on(day) for day in every]
    starts = [timeutil.midnight(day, zone) for day, zone in zip(every, zones)]
    spans = []
    for n, day in enumerate(days):
        end = starts[n + 1]   # midnight starting the next day on its clock: the same as on this day's, but on a swap
        if zones[n + 1] != zones[n]:
            end = min(end, timeutil.midnight(every[n + 1], zones[n]))
        spans.append((starts[n], end))
    return spans


def _on_days(items: list, days: list[date], zone_on: ZoneOn) -> dict[date, list]:
    """`items` (actions, sign-ins or solves, by their `at`) under the day of `days` (consecutive) whose span holds
    them (`_spans`: midnight to midnight on that day's clock, the later day winning an instant in two days' windows),
    each day's in the order given; an instant in no span counts on none."""
    spans = _spans(days, zone_on)
    starts = [start for start, _ in spans]
    held: dict[date, list] = {}
    for item in items:
        n = bisect_right(starts, item.at) - 1
        if n >= 0 and item.at < spans[n][1]:
            held.setdefault(days[n], []).append(item)
    return held


def _tech_today(conn: sqlite3.Connection, email: str, now: datetime | None, today: date) -> date:
    """Today on the tech's clock (`regions.today_for`). A tech on the app's own clock, as every tech with no region
    is, keeps `today`, the app's, which is today on that clock: their days read exactly as before."""
    if regions.zone_of(conn, email, today) == settings.get_timezone(conn):
        return today
    return regions.today_for(conn, email, now, today)


@dataclass(frozen=True)
class _Solve:
    at: datetime   # when a ticket assigned to the tech was solved: what `_on_days` places


def _tech_facts(conn: sqlite3.Connection, email: str, days: list[date], zone_on: ZoneOn, th: Thresholds,
                today: date, span: SyncSpan) -> list[DayFacts]:
    """One tech's DayFacts for each of `days` (consecutive, oldest first), from a single read of their rows, each day
    on its own clock (`zone_on`, `_on_days`). `today` is the tech's (`_tech_today`). Their solved tickets are those
    stored as solved while assigned to their Zendesk id (`collected.solved_between`), each on the day it was solved;
    a tech whose id no sync has stored has none."""
    if not days:
        return []
    start, end = _window(days, zone_on)
    acts: dict[date, list[Action]] = _on_days(store.actions_between(conn, email, start, end), days, zone_on)
    ins: dict[date, list[SignIn]] = _on_days(store.signins_between(conn, email, start, end), days, zone_on)
    agent_id = collected.agents(conn).get(email)
    solves = [] if agent_id is None else [_Solve(at) for at, _ in collected.solved_between(conn, start, end, agent_id)]
    solved: dict[date, list[_Solve]] = _on_days(solves, days, zone_on)
    versions = store.shift_versions(conn, email)
    offs: list[TimeOff] = store.time_off(conn, email)
    return [
        _facts(day, acts.get(day, []), ins.get(day, []), _version_on(versions, day),
               _off_on(offs, day), th, zone_on(day), today, span, [s.at for s in solved.get(day, [])])
        for day in days
    ]


def tech_day_facts(conn: sqlite3.Connection, email: str, first: date, last: date, *, today: date,
                   now: datetime | None = None) -> list[DayFacts]:
    """The DayFacts Team activity shows for the tech `email` on each day from `first` through `last`, oldest
    first: the same reads and rules as the grid, so other sections count days exactly as it shows them. The days are
    on the tech's clock, and so is their today at `now` (`_tech_today`; None: `today`)."""
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return _tech_facts(conn, email, days, regions.day_zones(conn, email), store.thresholds(conn),
                       _tech_today(conn, email, now, today), _sync_span(conn))


def tech_day_states(conn: sqlite3.Connection, email: str, first: date, last: date, *, today: date,
                    now: datetime | None = None) -> list[tuple[date, str]]:
    """Each of the tech's days from `first` through `last`, oldest first, with the state `tech_day_facts` gives it,
    judged (`rules.day_state`) from whether the tech acted and signed in on the day rather than from what each action
    was: what counts days by their state alone (1:1 prep's PTO and sick days, over a year of 1:1s) reads this much
    less. The days are on the tech's clock, and so is their today at `now` (`_tech_today`; None: `today`)."""
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    zone_on = regions.day_zones(conn, email)
    spans = _spans(days, zone_on)
    signed = _on_days(store.signins_between(conn, email, spans[0][0], spans[-1][1]), days, zone_on)
    versions = store.shift_versions(conn, email)
    offs = store.time_off(conn, email)
    tech_today = _tech_today(conn, email, now, today)
    synced_from, synced_through = _sync_span(conn)
    states = []
    for day, (start, end) in zip(days, spans):
        acted = conn.execute("SELECT EXISTS (SELECT 1 FROM activity_actions WHERE email = ? AND at >= ? AND at < ?)",
                             (email, store.iso(start), store.iso(end))).fetchone()[0]
        version = _version_on(versions, day)
        state = day_state(day, bool(acted), day in signed, version, _off_on(offs, day), zone_on(day), tech_today,
                          synced_from=synced_from, synced_through=synced_through)
        states.append((day, "untracked" if _untracked(state, version) else state))
    return states


def _on_team_during(conn: sqlite3.Connection, email: str, days: list[date]) -> bool:
    """Whether the tech has an active shift version on at least one of `days`."""
    versions = store.shift_versions(conn, email)
    return any(_active(_version_on(versions, day)) for day in days)


def _among(listed: list[dict], emails: list[str] | None) -> list[dict]:
    """The techs of `listed` whose email is in `emails`; all of them when `emails` is None."""
    return listed if emails is None else [t for t in listed if t["email"] in emails]


def _listed(conn: sqlite3.Connection, roster: list[dict], days: list[date]) -> list[dict]:
    """The techs of `roster` (`techs` entries) a view of `days` lists: everyone on the team now plus anyone with an
    active shift on one of `days`, so a tech who left drops out of periods wholly after their time."""
    team = set(settings.team_emails(conn))
    return [t for t in roster if t["email"] in team or _on_team_during(conn, t["email"], days)]


def picker(conn: sqlite3.Connection, days: list[date], emails: list[str] | None = None,
           today: date | None = None) -> list[dict]:
    """The Tech picker's lines for a view of `days` (Regions design): `{email, name, region}` for each tech it lists
    among `emails` (the viewer's; None is everyone), whatever region or tech the view itself is narrowed to, each
    under their region in force on `today`, the app's (`techs`)."""
    return [{"email": t["email"], "name": t["name"], "region": t["region"]}
            for t in _listed(conn, _among(techs(conn, today), emails), days)]


def range_view(conn: sqlite3.Connection, period: Period, email: str | None, *, today: date,
               emails: list[str] | None = None, now: datetime | None = None) -> dict:
    """The grid for `period` (every day of it, including days after `today`), for the whole team or the tech
    `email`. A tech with no day of their own in the period (before they joined, after they left) is left out
    unless they were asked for. `techs` is everyone on the team now plus anyone with an active shift on a day of the
    period (`_listed`), so a tech who left drops out of periods wholly after their time. `emails` limits both to
    those techs (a team lead's own, or one region's); None is everyone. Each row is on its tech's clock, their today
    too (`_tech_today` at `now`); the day columns, and `today`, are the period's and the app's."""
    th = store.thresholds(conn)
    roster = _among(techs(conn, today), emails)
    days = [] if period.empty else [period.start + timedelta(days=n) for n in range(period.days)]
    span = _sync_span(conn)
    rows = []
    for tech in roster:
        if email is not None and tech["email"] != email:
            continue
        zone_on = regions.day_zones(conn, tech["email"])
        tech_today = _tech_today(conn, tech["email"], now, today)
        cells = [cell(f, zone_on(f.day)) for f in _tech_facts(conn, tech["email"], days, zone_on, th, tech_today, span)]
        if email is not None or any(c["state"] != "untracked" for c in cells):
            rows.append({"email": tech["email"], "name": tech["name"], "cells": cells})
    return {
        "period": period.as_dict(),
        "today": today.isoformat(),
        "synced": synced(conn),
        "techs": [{**t, "has_shift": bool(store.shift_versions(conn, t["email"]))}
                  for t in _listed(conn, roster, days)],
        "days": [d.isoformat() for d in days],
        "rows": rows,
    }


def _shown(kind: str, value: str | None, emails: list[str] | None) -> str | None:
    """An action's old or new value as a view limited to `emails` (a team lead's techs) may give it. An assignee is
    a tech's email or a Zendesk id (`classify._person`), so a limited view keeps it only when it is one of `emails`;
    any other tech, or anyone known only by id, is None (the page still reads "changed the assignee"). Other kinds'
    values name no one, and None (everyone) keeps every value."""
    if emails is None or kind != "assignee" or value is None:
        return value
    return value if value.strip().lower() in emails else None


def day_view(conn: sqlite3.Connection, email: str, day: date, *, today: date,
             emails: list[str] | None = None, now: datetime | None = None) -> dict:
    """One tech's day: its cell, each action in order (the actions of one save stay separate rows and share
    an `event_id`), the seven days ending on `day`, the tech's open tickets as of the last sync, and `time_off`,
    the entry (id, first, last, note, kind) that covers the day, or None. `emails` (a team lead's techs) hides an
    assignee change's old and new values that name anyone else (`_shown`); None is everyone. Everything is on the
    tech's clock, their today too (`_tech_today` at `now`), and the actions are those the day's cell counts."""
    zone_on = regions.day_zones(conn, email)
    th = store.thresholds(conn)
    recent_days = [day - timedelta(days=n) for n in range(RECENT_DAYS - 1, -1, -1)]
    recent = [cell(f, zone_on(f.day)) for f in _tech_facts(conn, email, recent_days, zone_on, th,
                                                           _tech_today(conn, email, now, today), _sync_span(conn))]

    zone = ZoneInfo(zone_on(day))
    start, end = timeutil.day_window(day, zone_on(day))
    actions = _on_days(store.actions_between(conn, email, start, end), [day], zone_on).get(day, [])
    subject = store.subjects(conn, [a.ticket_id for a in actions])
    covered = [o for o in store.time_off(conn, email) if o.first <= day <= o.last]
    covering = next((o for o in covered if o.kind == "sick"), covered[0] if covered else None)
    return {
        "cell": recent[-1],
        "time_off": None if covering is None else {"id": covering.id, "first": covering.first.isoformat(),
                                                    "last": covering.last.isoformat(), "note": covering.note,
                                                    "kind": covering.kind},
        "actions": [
            {"at": _local_clock(a.at, zone), "kind": a.kind, "old": _shown(a.kind, a.old, emails),
             "new": _shown(a.kind, a.new, emails),
             "ticket_id": a.ticket_id, "subject": subject[a.ticket_id], "event_id": a.event_id}
            for a in actions
        ],
        "recent": recent,
        "open": _open_tickets(conn, email, ZoneInfo(regions.zone_of(conn, email, today))),
    }


def _open_tickets(conn: sqlite3.Connection, email: str, zone: ZoneInfo) -> dict:
    """The tickets assigned to the tech and not yet solved when the last sync looked. `last_acted` is the
    tech's latest action on the ticket as local "YYYY-MM-DDTHH:MM" (`zone`: their clock now), or None when they have
    none on record. `age_days` is whole days since the ticket was created, at the sync that saw it open, and `aging`
    whether that is past `collect.AGING_AFTER`, the rule 1:1 Prep's aging tickets follow (the owner, 2026-10-22: the
    panel picks them out); both None when the creation time isn't on record."""
    from work_assistant.activity.collect import AGING_AFTER

    def age(r) -> tuple[int | None, bool | None]:
        if not r["created"]:
            return None, None
        old = store.parse_iso(r["captured_at"]) - store.parse_iso(r["created"])
        return old.days, old > AGING_AFTER

    rows = conn.execute(
        "SELECT o.ticket_id, o.status, o.captured_at, o.created, t.subject,"
        " (SELECT MAX(a.at) FROM activity_actions a WHERE a.email = o.email AND a.ticket_id = o.ticket_id) AS last_at"
        " FROM activity_open o LEFT JOIN activity_tickets t ON t.ticket_id = o.ticket_id"
        " WHERE o.email = ? ORDER BY o.ticket_id",
        (email,),
    ).fetchall()
    if rows:
        as_of = max(r["captured_at"] for r in rows)
    else:
        through = store.synced_through(conn)
        as_of = store.iso(through) if through is not None else None
    return {
        "as_of": as_of,
        "tickets": [
            {"ticket_id": r["ticket_id"], "subject": r["subject"], "status": r["status"],
             "last_acted": store.parse_iso(r["last_at"]).astimezone(zone).strftime("%Y-%m-%dT%H:%M")
             if r["last_at"] else None,
             "age_days": age(r)[0], "aging": age(r)[1]}
            for r in rows
        ],
    }


# -- shifts and time off ------------------------------------------------------------------------------------


def version_json(v: ShiftVersion) -> dict:
    return {"effective_from": v.effective_from.isoformat(), "work_days": sorted(v.work_days),
            "start": clock(v.start_min), "end": clock(v.end_min), "active": v.active}


def shifts_view(conn: sqlite3.Connection, emails: list[str] | None = None, today: date | None = None) -> dict:
    """Each listed tech with every shift version, oldest first; only the techs in `emails` unless it is None. Each
    tech's region is the one in force on `today`, the app's (`techs`)."""
    return {"techs": [{**tech, "versions": [version_json(v) for v in store.shift_versions(conn, tech["email"])]}
                      for tech in _among(techs(conn, today), emails)]}


def time_off_json(item: TimeOff) -> dict:
    return {"id": item.id, "email": item.email, "first": item.first.isoformat(), "last": item.last.isoformat(),
            "note": item.note, "kind": item.kind}
