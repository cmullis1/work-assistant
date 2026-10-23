"""Synthetic demo data for `work-assistant serve --demo`, so the app can be shown and built without real calls.

Everything here is invented: agent names, emails (@example.com), tickets and call text. The data goes
through the real code: contacts and rubric versions are stored by the pipeline, timed rules are
decided by `timing`, and reviews are stored and costed by `pipeline.start_run` (the model test's by
`pipeline.start_test`). Only the reviewer is replaced, by an offline stand-in that answers each request
from its own schema. Nothing here contacts Zendesk or Anthropic.
"""
from __future__ import annotations

import dataclasses
import io
import json
import random
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
import tzlocal

from work_assistant import db, names, people, pipeline, regions, rubric, settings, timeutil
from work_assistant.activity import collect, collected, store
from work_assistant.activity import report as activity_report
from work_assistant.activity.model import Action, DayFacts, ShiftVersion, SignIn
from work_assistant.models import Comment, FirstContact
from work_assistant.oneonone import cycle
from work_assistant.oneonone.model import PrepItem
from work_assistant.zendesk.client import ZendeskError

WEEKS = 8
SEED = 20261016
FIRST_TICKET = 50001

# (name, email, how often this invented agent misses a check: 0 = never)
AGENTS = (
    ("Avery Stone", "avery.stone@example.com", 0.03),
    ("Jordan Pike", "jordan.pike@example.com", 0.09),
    ("Riley Chen", "riley.chen@example.com", 0.05),
    ("Sam Okafor", "sam.okafor@example.com", 0.06),
    ("Taylor Brooks", "taylor.brooks@example.com", 0.04),
    ("Morgan Reyes", "morgan.reyes@example.com", 0.07),
)
# Checks that are missed more often than the rest, so the reports have something to show.
OFTEN_MISSED = {"sc06": 3.0, "sc15": 3.5, "sc07": 2.0, "pr-triage-macro": 2.5, "pr-scope": 1.2}
USUALLY_NA = {"sc11", "sc12", "pr-dropped-callback", "pr-merge-correct"}

SUBJECTS = (
    "Payment terminal offline at front desk", "Receipt printer not printing", "Self-checkout stuck on loading screen",
    "Gift card balance not updating", "Booking screen shows wrong time slots", "POS terminal frozen after update",
    "Barcode scanner not reading labels", "Online orders not reaching the kitchen screen",
    "Report export fails at month end", "Customer lookup returns no results",
)
PRIORITIES = ("normal", "normal", "normal", "high", "high", "low", "urgent")
EVIDENCE = {
    "met": "Demo evidence: the agent did this clearly early in the call.",
    "missed": "Demo evidence: no sign of this anywhere in the call or the notes.",
    "na": "Demo evidence: did not apply to this call.",
    "unsure": "Demo evidence: the transcript is unclear at this point.",
}
NOT_COUNTED_REASON = "Demo: wrong number; the caller hung up before describing a problem."
TEST_MODEL = "claude-sonnet-5-5"   # the demo's model test: Sonnet at high effort on last week's newest calls
TEST_EFFORT = "high"
TEST_CALLS = 8
BUILD_RETENTION_DAYS = 3650   # retention while the demo's runs are made, so they erase none of its calls


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_contact(ticket_id: int, agent: tuple[str, str, float], call_start: datetime, rng: random.Random,
                 *, user_id: int) -> FirstContact:
    """One invented inbound call with a transcript, triage macro, call summary, and usually an email."""
    name, email, _ = agent
    duration = int(rng.triangular(150, 1500, 420))
    call_end = call_start + timedelta(seconds=duration)
    roll = rng.random()
    if roll < 0.06:
        email_at = None
    elif roll < 0.12:
        email_at = _iso(call_end + timedelta(hours=rng.uniform(25, 40)))
    elif roll < 0.25:
        email_at = _iso(call_end + timedelta(minutes=rng.uniform(35, 300)))  # past the 30-minute deadline
    else:
        email_at = _iso(call_end + timedelta(minutes=rng.uniform(3, 28)))
    subject = rng.choice(SUBJECTS)
    first = name.split()[0]

    def comment(n: int, at: datetime, role: str, kind: str, text: str, public: bool = False) -> Comment:
        author = (user_id, name) if role == "agent" else (0, "System")
        return Comment(comment_id=ticket_id * 10 + n, created_at=_iso(at), author_id=author[0],
                       author_name=author[1], author_role=role, public=public, kind=kind, text=text,
                       image_urls=())

    comments = [
        comment(1, call_end, "system", "transcript",
                f"[00:03] Agent: Thank you for calling technical support, this is {first}.\n"
                f"[00:20] Caller: Hi, we have a problem: {subject.lower()}.\n"
                "[02:10] Agent: When did it start, and how many devices are affected?\n"
                "[09:40] Agent: Is there anything else I can help with today?"),
        comment(2, call_start + timedelta(minutes=3), "agent", "triage_macro",
                f"Location: Demo Store {rng.randint(1, 40)}\nIssue: {subject}\nDevices: {rng.randint(1, 6)}\n"
                "Error message: none shown"),
        comment(3, call_end, "system", "call_summary", f"Inbound call about: {subject}."),
    ]
    if email_at is not None:
        comments.append(comment(4, datetime.fromisoformat(email_at.replace("Z", "+00:00")), "agent",
                                "summary_email", f"Hello, thanks for calling today about {subject.lower()}. "
                                "Here is a summary of what we did and what happens next.", public=True))
    return FirstContact(
        ticket_id=ticket_id, channel="voice", subject=subject, priority=rng.choice(PRIORITIES), tags=("demo",),
        fields={"Location ID": f"DEMO-{rng.randint(100, 999)}"}, call_started_at=_iso(call_start),
        call_duration_seconds=duration, agent_user_id=user_id, agent_email=email, agent_name=name,
        attribution_ok=rng.random() > 0.03, summary_email_at=email_at, comments=tuple(comments),
    )


class OfflineReviewer:
    """Stands in for `anthropic.Anthropic` with the batch calls the pipeline makes. Each request is
    answered from its own output schema, with outcomes drawn from the invented agent's miss rate."""

    def __init__(self, rng: random.Random, miss_rate_by_ticket: dict[int, float]):
        self._rng = rng
        self._miss_rate = miss_rate_by_ticket
        self._batches: dict[str, list[Any]] = {}
        self.messages = NS(batches=self)

    def with_options(self, **_options: Any) -> OfflineReviewer:
        return self

    def create(self, requests: list[Any]) -> NS:
        batch_id = f"demo_batch_{len(self._batches) + 1}"
        self._batches[batch_id] = list(requests)
        return NS(id=batch_id)

    def retrieve(self, batch_id: str) -> NS:
        return NS(processing_status="ended")

    def results(self, batch_id: str):
        for request in self._batches[batch_id]:
            yield NS(custom_id=request["custom_id"], result=NS(type="succeeded", message=self._answer(request)))

    def _answer(self, request: Any) -> NS:
        schema = request["params"]["output_config"]["format"]["schema"]
        check_ids = schema["properties"]["checks"]["items"]["properties"]["check_id"]["enum"]
        ticket_id = int(request["custom_id"][1:])
        miss_rate = self._miss_rate[ticket_id]
        checks = []
        for check_id in check_ids:
            if check_id in USUALLY_NA and self._rng.random() < 0.85:
                outcome = "na"
            elif self._rng.random() < miss_rate * OFTEN_MISSED.get(check_id, 1.0):
                outcome = "missed"
            elif self._rng.random() < 0.02:
                outcome = "unsure"
            else:
                outcome = "met"
            checks.append({"check_id": check_id, "outcome": outcome, "evidence": EVIDENCE[outcome]})
        not_counted = ticket_id % 41 == 0
        text = json.dumps({
            "checks": checks,
            "resolved_on_call": "yes" if self._rng.random() < 0.8 else "no",
            "call_dropped": "no",
            "counted": "no" if not_counted else "yes",
            "not_counted_reason": NOT_COUNTED_REASON if not_counted else "",
        })
        usage = NS(input_tokens=self._rng.randint(6000, 12000), output_tokens=self._rng.randint(2000, 4500),
                   cache_read_input_tokens=7000, cache_creation_input_tokens=0)
        return NS(stop_reason="end_turn", stop_details=None, content=[NS(type="text", text=text)], usage=usage)


def _remove_old(path: Path, log_dir: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    if log_dir.is_dir():
        for old in log_dir.glob("run-*.log"):
            old.unlink()


def _week_calls(window: tuple[datetime, datetime], rng: random.Random, next_id: int,
                miss_rate: dict[int, float]) -> list[FirstContact]:
    contacts = []
    for user_id, agent in enumerate(AGENTS, start=1):
        for _ in range(rng.randint(4, 7)):
            # Monday to Friday, business hours (local midnight Sunday + days + hours).
            start = window[0] + timedelta(days=rng.randint(1, 5), hours=rng.uniform(8.5, 16.5))
            contacts.append(make_contact(next_id, agent, start, rng, user_id=user_id))
            miss_rate[next_id] = agent[2]
            next_id += 1
    return contacts


def _date_run(conn, run_id: int, started: datetime, minutes: int) -> None:
    conn.execute("UPDATE runs SET created_at = ?, finished_at = ? WHERE run_id = ?",
                 (_iso(started), _iso(started + timedelta(minutes=minutes)), run_id))
    conn.commit()


def _add_overrides(conn, rng: random.Random, now: datetime) -> None:
    """The owner's Disagree corrections on a few calls, written the way the app writes them."""
    missed = conn.execute(
        "SELECT cr.review_id, cr.check_id FROM check_results cr JOIN reviews r ON r.review_id = cr.review_id "
        "WHERE r.is_current = 1 AND cr.outcome = 'missed' AND cr.decided_by = 'model' ORDER BY cr.review_id"
    ).fetchall()
    met = conn.execute(
        "SELECT cr.review_id, cr.check_id FROM check_results cr JOIN reviews r ON r.review_id = cr.review_id "
        "WHERE r.is_current = 1 AND cr.outcome = 'met' AND cr.check_id = 'sc06' ORDER BY cr.review_id"
    ).fetchall()
    when = _iso(now - timedelta(days=2))
    changes = [(row, "met", "Demo: the agent did cover this; the reviewer missed it.")
               for row in rng.sample(missed, min(5, len(missed)))]
    changes += [(row, "missed", "Demo: the summary back to the caller was incomplete.")
                for row in rng.sample(met, min(2, len(met)))]
    conn.executemany(
        "UPDATE check_results SET override_outcome = ?, override_note = ?, override_at = ? "
        "WHERE review_id = ? AND check_id = ?",
        [(outcome, note, when, row["review_id"], row["check_id"]) for row, outcome, note in changes],
    )
    conn.commit()


def as_of(day: date) -> datetime:
    """Noon on `day` in this PC's time zone (the demo's own, since it sets none): the moment a demo built "as of"
    that day (`work-assistant serve --demo --demo-date`) stands at."""
    return datetime.combine(day, time(12), ZoneInfo(tzlocal.get_localzone_name())).astimezone(timezone.utc)


def build_demo_db(path: Path, *, log_dir: Path | None = None, now: datetime | None = None) -> None:
    """Replace `path` with a database of invented data: the last 8 weeks of calls by 6 invented
    agents, reviewed under two rubric versions, with a few Not counted calls, a few overrides, one
    failed manual run in the history, and a finished model test of last week's newest calls; then their Team
    activity (`seed_activity`), what the hourly sync collects for 1:1 Prep and Trends (`seed_collected`) and their
    1:1 schedules (`seed_schedules`): 1:1 Prep counts the techs' preps from what is collected, as it counts real
    ones.

    Everything is dated from `now` (default: the real time), so a demo built with a fixed `now` is the same
    whatever day it is built on."""
    path = Path(path)
    log_dir = Path(log_dir) if log_dir is not None else path.parent / "demo-logs"
    now = now or datetime.now(timezone.utc)
    _remove_old(path, log_dir)
    rng = random.Random(SEED)
    conn = db.connect(path)
    try:
        db.init_db(conn)
        version = rubric.ensure_seeded(conn)
        settings.set_settings(conn, {
            "team": [email for _, email, _ in AGENTS],
            "zendesk_subdomain": "example",
            "zendesk_email": "team.lead@example.com",
        })
        tz = settings.get_timezone(conn)
        # The runs erase content past retention by the real clock; a demo dated earlier keeps all of its own.
        settings.set_setting(conn, "retention_days", BUILD_RETENTION_DAYS)
        miss_rate: dict[int, float] = {}
        reviewer = OfflineReviewer(rng, miss_rate)
        next_id = FIRST_TICKET
        last_week = timeutil.previous_week(now, tz)
        for index in range(WEEKS):
            weeks_back = WEEKS - 1 - index
            window = (last_week[0] - timedelta(weeks=weeks_back), last_week[1] - timedelta(weeks=weeks_back))
            if index == WEEKS // 2:
                version = _second_version(conn, version)
            contacts = _week_calls(window, rng, next_id, miss_rate)
            next_id += len(contacts)
            run_id = pipeline.start_run(
                conn, "scheduled", window, fetcher=lambda w, log, c=contacts: list(c),
                anthropic_client=reviewer, images_loader=lambda contact, log: [], confirm=True,
                log_dir=log_dir, sleep=lambda seconds: None)
            _date_run(conn, run_id, window[1] + timedelta(hours=30), 42)  # Monday about 06:00
            if index == 2:
                _failed_manual_run(conn, window, log_dir)
        _add_overrides(conn, rng, now)
        _model_test(conn, reviewer, last_week, log_dir, now)
        settings.set_setting(conn, "retention_days", settings.DEFAULTS["retention_days"])
        _date_versions(conn, last_week)
        seed_activity(conn, now=now, weeks=ACTIVITY_WEEKS)
        seed_collected(conn, now)
        seed_history(conn, now)
        seed_schedules(conn)
    finally:
        conn.close()


# -- Team activity ------------------------------------------------------------------------------------------

ACTIVITY_WEEKS = 17   # Last 8 weeks and the 8 before it, with a few days to spare
FIRST_ACTIVITY_TICKET = 60001
TICKETS_PER_TECH = 30
SHIFT_START, SHIFT_END = 8 * 60, 17 * 60
SUN_THU = frozenset({0, 1, 2, 3, 4})
TUE_SAT = frozenset({2, 3, 4, 5, 6})
# The first two invented techs work Sunday to Thursday; the rest Tuesday to Saturday. Each of the other four
# gets one unusual day within the last 14 days, so every kind of day shows in "Last week" and "Last month".
SPECIAL_DAYS = {   # AGENTS index -> the kind of unusual day
    0: "long_gap", 2: "zero", 3: "missed", 4: "pto", 5: "worked_rest_day",
}
# Trends needs someone slipping and someone improving. The second tech (Jordan Pike) slips steadily across the last
# 8 whole weeks: actions spread out to 1.6 times as far apart (1 + SLIP_SPACING), the first action comes up to SLIP_LATE_MIN
# minutes late, and one shift is missed outright in the 4th week back. The fifth (Taylor Brooks) did that
# before, in the 8 weeks before, and eased back to normal.
SLIPPING, IMPROVING = 1, 4
SLIP_SPACING, SLIP_LATE_MIN = 0.6, 45         # extra spacing (a fraction) and minutes at the worst week
IMPROVE_SPACING, IMPROVE_LATE_MIN = 0.5, 40   # the same, at the start of the 8 weeks before
SLIP_MISSED_WEEKS_BACK = 4
TREND_WEEKS = 8
ACTIVITY_SUBJECTS = (
    "Checkout – payment terminal offline", "Back office – dashboard screen frozen", "Front desk – receipt printer jams",
    "Self-checkout – stuck on loading screen", "Bookings – calendar shows wrong slot",
    "Café – POS terminal restarting", "Gift cards – balance not updating",
    "Labels – scanner not reading", "Reports – month end export fails",
    "Loyalty – lookup returns nothing", "Inventory – stock count off by one",
    "Online orders – missing from kitchen screen",
)
_ACTION_KINDS = (("reply", 40), ("note", 25), ("call", 10), ("status", 12), ("assignee", 5), ("priority", 4),
                 ("group", 2), ("fields", 2))


def _local_utc(day: date, minutes: float, zone: ZoneInfo) -> datetime:
    """The UTC instant `minutes` after local midnight on `day` (wall-clock, so daylight saving is safe)."""
    wall = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
    return wall.replace(tzinfo=zone).astimezone(timezone.utc)


def _action_values(kind: str, rng: random.Random) -> tuple[str | None, str | None]:
    if kind == "status":
        return rng.choice((("open", "pending"), ("pending", "open"), ("open", "solved"), ("open", "solved")))
    if kind == "priority":
        return rng.choice((("normal", "high"), ("high", "urgent"), ("low", "normal")))
    if kind == "fields":
        return "Location ID: DEMO-100", "Location ID: DEMO-200"
    if kind == "assignee":
        return "Unassigned", "Assigned"
    if kind == "group":
        return "Support", "Technical Support"
    return None, None


def _drift(index: int, day: date, today: date) -> tuple[float, float]:
    """How far the slipping and the improving tech are from their normal on `day`: the factor on the spacing
    between their actions and the minutes added to their first action. Weeks are counted back from the last whole
    week (1); the current week counts as the last. The slip builds steadily over the last 8 weeks; the old trouble
    eases over the 8 before them. Everyone else, and every other week, is normal."""
    back = max(1, (timeutil.week_start(today) - timeutil.week_start(day)).days // 7)
    if index == SLIPPING and back <= TREND_WEEKS:
        worse = (TREND_WEEKS - back) / (TREND_WEEKS - 1)
        return 1 + SLIP_SPACING * worse, SLIP_LATE_MIN * worse
    if index == IMPROVING and back > TREND_WEEKS:
        worse = min(1.0, (back - TREND_WEEKS) / TREND_WEEKS)
        return 1 + IMPROVE_SPACING * worse, IMPROVE_LATE_MIN * worse
    return 1.0, 0.0


def seed_activity(conn, *, now: datetime, weeks: int = ACTIVITY_WEEKS) -> None:
    """Invent Team activity for the demo techs as of `now`: shift versions, tickets, actions, sign-ins, one day
    of PTO, each unusual kind of day within the last 14 days, a few open tickets, and one complete sync that read
    from the first day and was current to `now`. One tech slips and one improves across the weeks (see SLIPPING and
    IMPROVING) for Trends. Deterministic for a given `now` (seeded with SEED)."""
    tz = settings.get_timezone(conn)
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    rng = random.Random(SEED + 1)
    first_day = today - timedelta(days=weeks * 7 - 1)
    events = iter(range(FIRST_ACTIVITY_TICKET * 100, FIRST_ACTIVITY_TICKET * 100 + 10 ** 6))
    subjects: dict[int, str] = {}
    actions: list[Action] = []
    signins: list[SignIn] = []

    def pool(index: int) -> list[int]:
        start = FIRST_ACTIVITY_TICKET + index * TICKETS_PER_TECH
        return list(range(start, start + TICKETS_PER_TECH))

    for index, (_, email, _) in enumerate(AGENTS):
        work_days = SUN_THU if index < 2 else TUE_SAT
        store.add_shift_version(conn, ShiftVersion(email, first_day, work_days, SHIFT_START, SHIFT_END, True))
        for ticket in pool(index):
            subjects[ticket] = rng.choice(ACTIVITY_SUBJECTS)

    def is_work_day(index: int, day: date) -> bool:
        return (day.weekday() + 1) % 7 in (SUN_THU if index < 2 else TUE_SAT)

    def recent_day(index: int, work: bool, skip: int) -> date:
        """The `skip`-th most recent complete day (not today) that is (or is not) one of the tech's work days."""
        found = [d for d in (today - timedelta(days=n) for n in range(1, 14)) if is_work_day(index, d) == work]
        return found[skip]

    special: dict[tuple[int, date], str] = {}
    for index, kind in SPECIAL_DAYS.items():
        special[(index, recent_day(index, kind != "worked_rest_day", 0 if kind != "long_gap" else 1))] = kind
    # The slipping tech misses one whole shift (a Tuesday) in the 4th week back.
    special[(SLIPPING, timeutil.week_start(today) - timedelta(weeks=SLIP_MISSED_WEEKS_BACK, days=-2))] = "missed"
    for (index, day), kind in special.items():
        if kind == "pto":
            store.add_time_off(conn, AGENTS[index][1], day, day, "Demo: vacation day")

    def add(index: int, day: date, minute: float, kind: str | None = None) -> None:
        email = AGENTS[index][1]
        kind = kind or rng.choices([k for k, _ in _ACTION_KINDS], [w for _, w in _ACTION_KINDS])[0]
        old, new = _action_values(kind, rng)
        actions.append(Action(email, rng.choice(pool(index)), _local_utc(day, minute, zone), kind, old, new,
                              next(events), 0))

    for index, (_, email, _) in enumerate(AGENTS):
        for offset in range((today - first_day).days + 1):
            day = first_day + timedelta(days=offset)
            kind = special.get((index, day))
            if kind in ("pto", "missed"):
                continue
            if kind == "zero":
                signins.append(SignIn(email, _local_utc(day, SHIFT_START + rng.uniform(-10, 10), zone)))
                continue
            if kind == "worked_rest_day":
                for n in range(5):
                    add(index, day, 10 * 60 + n * 25 + rng.uniform(0, 10))
                continue
            if not is_work_day(index, day):
                continue
            signins.append(SignIn(email, _local_utc(day, SHIFT_START + rng.uniform(-12, 8), zone)))
            if kind == "long_gap":   # busy morning, nothing from 10:00 to 12:45, then busy again
                times = [8 * 60 + 10 + n * 20 for n in range(6)] + [12 * 60 + 45 + n * 20 for n in range(6)]
            else:
                limit = 12 * 60 if day == today else SHIFT_END - 20   # today is still in its morning
                spacing, late = _drift(index, day, today)
                times, minute = [], SHIFT_START + rng.uniform(5, 30) + late
                while minute < limit:
                    times.append(minute)
                    minute += rng.uniform(15, 45) * spacing
            for minute in times:
                add(index, day, minute)

    store.insert_actions(conn, actions)
    store.insert_signins(conn, signins)
    conn.executemany("INSERT OR REPLACE INTO activity_tickets (ticket_id, subject, purged) VALUES (?, ?, 0)",
                     list(subjects.items()))
    opened = random.Random(SEED + 12)   # when each open ticket was created: its own draws, so the rest is unchanged
    conn.executemany(
        "INSERT OR REPLACE INTO activity_open (email, captured_at, ticket_id, status, created) VALUES (?, ?, ?, ?, ?)",
        [(email, store.iso(now), ticket, status, store.iso(now - timedelta(days=opened.uniform(1, 40))))
         for email, ticket, status in [
             (email, ticket, rng.choice(("open", "open", "pending")))
             for index, (_, email, _) in enumerate(AGENTS) for ticket in pool(index)[:rng.randint(2, 4)]]])
    conn.execute(
        "INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, pages, window_start) "
        "VALUES ('daily', 'complete', ?, ?, ?, 1, ?)",
        (store.iso(now), store.iso(now), store.iso(now), store.iso(_local_utc(first_day, 0, zone))))
    conn.commit()


# -- the hosted copy's people -------------------------------------------------------------------------------

# The demo of the hosted edition (`serve --demo --edition hosted`) has a team lead, Jamie Rivera, added by name as
# the admin would, who sees three of the techs: the one slipping (Jordan Pike), one with a sick day in the last two
# weeks (Sam Okafor) and the one improving, with a PTO day (Taylor Brooks). Their screens show each kind of day. The
# admin, a team lead too (hosted Team design §3), has the other three: Avery Stone, Riley Chen and Morgan Reyes, and
# the four techs outside NASA (REGION_TECHS), so the admin's techs span three regions. Two more team leads and a
# manager come with Managers (DEMO_REGION_LEADS, DEMO_MANAGER).
DEMO_TL = ("Jamie Rivera", "jamie.rivera@example.com")
DEMO_TL_TECHS = tuple(AGENTS[index][1] for index in (SLIPPING, 3, IMPROVING))

# The hosted demo's techs outside NASA (Regions design: "The hosted demo gains a few EMEA and APAC techs with activity
# on their own clocks"), so the admin's Tech picker groups by region and an APAC tech's calendar can be checked with
# Eyes (hosted-regions.yaml). Each works REGION_SHIFT on their region's clock: an APAC tech's 09:00 is about 8 PM the
# day before in Dallas. The last served APAC and moved to NASA (MOVED_TO) on the Monday MOVED_WEEKS_BACK weeks before
# the demo's week, a dated swap as a real tech's can be: their days before it are on Manila's clock, from it on
# Dallas's. They are invented, added by the hosted demo alone: the PC's demo has no regions.
MON_FRI = frozenset({1, 2, 3, 4, 5})
REGION_TECHS = (   # (name, email, region, work days)
    ("Lena Fischer", "lena.fischer@example.com", "EMEA", MON_FRI),
    ("Omar Haddad", "omar.haddad@example.com", "EMEA", MON_FRI),
    ("Mara Santos", "mara.santos@example.com", "APAC", TUE_SAT),
    ("Paolo Dizon", "paolo.dizon@example.com", "APAC", MON_FRI),
)
MOVED, MOVED_TO, MOVED_WEEKS_BACK = 3, "NASA", 3   # REGION_TECHS index of the tech who moved, where, and when
REGION_SHIFT = (9 * 60, 18 * 60)
FIRST_REGION_TICKET = FIRST_ACTIVITY_TICKET + len(AGENTS) * TICKETS_PER_TECH
FIRST_REGION_EVENT = FIRST_ACTIVITY_TICKET * 100 + 10 ** 6   # after seed_activity's events
DEMO_ADMIN_TECHS = tuple(AGENTS[index][1] for index in (0, 2, 5)) + tuple(t[1] for t in REGION_TECHS)

# The hosted demo's manager and the team leads a manager's screens set side by side (Managers design, "The demo"). The
# admin has a name, so those screens name every team lead. Each region outside NASA has its own team lead, who leads
# that region's techs in REGION_TECHS (Paolo Ramos keeps Paolo Dizon after his move to NASA) and shares them with the
# admin's team: a tech may be on more than one team, so the admin's demo stays as Regions made it. Parker Lane manages
# NASA and APAC (`serve --demo --edition hosted --demo-user parker.lane@example.com`; Eyes: hosted-manager.yaml and
# hosted-manager-activity.yaml): every tech but the two in EMEA, so Leila Haddad, whose techs are all EMEA, isn't one
# of Parker's team leads.
DEMO_ADMIN_NAME = "Alex Kim"
DEMO_REGION_LEADS = {"EMEA": ("Leila Haddad", "leila.haddad@example.com"),
                     "APAC": ("Paolo Ramos", "paolo.ramos@example.com")}
DEMO_MANAGER = ("Parker Lane", "parker.lane@example.com", ("NASA", "APAC"))


def seed_team_lead(path: Path, admin_email: str, *, now: datetime | None = None) -> None:
    """Add the hosted demo's people to the demo database at `path`: the admin (`admin_email`, the demo's server
    config's), named DEMO_ADMIN_NAME, with their own team, DEMO_ADMIN_TECHS, the techs outside NASA among them
    (`seed_regions`, as of `now`, the moment the demo was built as of; default: the real time); the demo's team lead
    (DEMO_TL), assigned DEMO_TL_TECHS; a team lead for each region's techs outside NASA (DEMO_REGION_LEADS); and the
    manager (DEMO_MANAGER) with the regions they picked. The demo of the Team activity edition calls it; the PC's demo
    has no people and no regions."""
    conn = db.connect(Path(path))
    try:
        seed_regions(conn, now=now or datetime.now(timezone.utc))
        people.add_person(conn, admin_email, "admin", DEMO_ADMIN_NAME)   # as the server's start makes them, named
        people.set_assignments(conn, admin_email, list(DEMO_ADMIN_TECHS))
        name, email = DEMO_TL
        people.add_person(conn, email, "tl", name)
        people.set_assignments(conn, email, list(DEMO_TL_TECHS))
        for region, (name, email) in DEMO_REGION_LEADS.items():
            people.add_person(conn, email, "tl", name)
            people.set_assignments(conn, email, [tech[1] for tech in REGION_TECHS if tech[2] == region])
        name, email, picked = DEMO_MANAGER
        people.add_person(conn, email, "manager", name)
        people.set_manager_regions(conn, email, list(picked))
    finally:
        conn.close()


def seed_regions(conn, *, now: datetime) -> None:
    """Add REGION_TECHS to the roster by name, with their regions (the moved tech's dated), shifts, sign-ins, actions
    and a few open tickets as of `now`, each day on the clock of the region in force that day (`regions.day_zones`),
    and what the hourly sync collects for them (`seed_collected`; Zendesk ids after AGENTS', usual two weeks
    REGION_PROFILES). Their first day is the day after Team activity's first: that one starts at midnight on the app's
    clock, already its afternoon or evening in Dubai and Manila. Deterministic for a given `now` (seeded with SEED + 3,
    and SEED + 5 for what is collected)."""
    app_zone = ZoneInfo(settings.get_timezone(conn))
    today = now.astimezone(app_zone).date()
    started = today - timedelta(days=ACTIVITY_WEEKS * 7 - 2)
    moved_on = timeutil.week_start(today) - timedelta(weeks=MOVED_WEEKS_BACK, days=-1)   # a Monday
    rng = random.Random(SEED + 3)
    events = iter(range(FIRST_REGION_EVENT, FIRST_REGION_EVENT + 10 ** 6))
    start_min, end_min = REGION_SHIFT
    actions: list[Action] = []
    signins: list[SignIn] = []
    subjects: dict[int, str] = {}
    open_tickets: list[tuple[str, int]] = []
    for index, (name, email, region, work_days) in enumerate(REGION_TECHS):
        names.store(conn, email, name)
        regions.set_regions(conn, email, [(started, region)] + ([(moved_on, MOVED_TO)] if index == MOVED else []))
        store.add_shift_version(conn, ShiftVersion(email, started, work_days, start_min, end_min, True))
        first_ticket = FIRST_REGION_TICKET + index * TICKETS_PER_TECH
        pool = list(range(first_ticket, first_ticket + TICKETS_PER_TECH))
        subjects.update((ticket, rng.choice(ACTIVITY_SUBJECTS)) for ticket in pool)
        open_tickets += [(email, ticket) for ticket in pool[:rng.randint(2, 4)]]
        zone_on = regions.day_zones(conn, email)
        their_today = regions.today_for(conn, email, now, today)
        for offset in range((their_today - started).days + 1):
            day = started + timedelta(days=offset)
            if (day.weekday() + 1) % 7 not in work_days:
                continue
            zone = ZoneInfo(zone_on(day))
            signin = _local_utc(day, start_min + rng.uniform(-12, 8), zone)
            if signin <= now:
                signins.append(SignIn(email, signin))
            minute = start_min + rng.uniform(5, 30)
            while minute < end_min - 20:
                at = _local_utc(day, minute, zone)
                if at > now:
                    break
                kind = rng.choices([k for k, _ in _ACTION_KINDS], [w for _, w in _ACTION_KINDS])[0]
                old, new = _action_values(kind, rng)
                actions.append(Action(email, rng.choice(pool), at, kind, old, new, next(events), 0))
                minute += rng.uniform(15, 45)
    store.insert_actions(conn, actions)
    store.insert_signins(conn, signins)
    conn.executemany("INSERT OR REPLACE INTO activity_tickets (ticket_id, subject, purged) VALUES (?, ?, 0)",
                     list(subjects.items()))
    conn.executemany(
        "INSERT OR REPLACE INTO activity_open (email, captured_at, ticket_id, status) VALUES (?, ?, ?, ?)",
        [(email, store.iso(now), ticket, rng.choice(("open", "open", "pending"))) for email, ticket in open_tickets])
    settings.set_settings(conn, {"team": [*settings.team_emails(conn), *(t[1] for t in REGION_TECHS)]})
    conn.commit()
    _collect_for(conn, [(email, FIRST_AGENT_ID + len(AGENTS) + index, REGION_PROFILES[index])
                        for index, (_, email, _, _) in enumerate(REGION_TECHS)],
                 first=started, now=now, rng=random.Random(SEED + 5), block=REGION_IDS)


# -- the hosted demo's Teams Shifts export ------------------------------------------------------------------

# Import from Teams Shifts (the hosted admin's Settings) reads the workbooks Teams Shifts' "Export schedule" writes. The
# hosted demo serves an invented one (`shifts_export`), built in memory, to try the panel on and for the hosted-admin
# Eyes scenario: four weeks of shifts, Sunday Oct 4 to Saturday Oct 31, 2026, with a bit of each thing the preview
# shows. Avery Stone takes a day of leave written only in the notes, as the real exports often do; Jordan Pike moves to
# Monday to Friday, 9 to 6, has a sick day and a half day (a work day); Riley Chen works in EMEA (Dubai 9 to 6, which is
# midnight to 9 on Chicago's clock) and has two days of Training, a label read as a working shift; Sam Okafor moves
# from APAC (Manila 8 to 5) to NASA; Taylor Brooks has a shift that crosses midnight and a day off entered with times (a
# full day); Morgan Reyes joins on Tuesday Oct 13. Jamie Rivera, the demo's team lead, isn't on the roster, so is
# skipped; an open shift (no one's) is left out.
SHIFTS_EXPORT_NAME = "demo-shifts-export.xlsx"
SHIFTS_EXPORT_ZONE = "America/Chicago"   # the exporter's clock: the hosted demo's zone (cli.DEMO_TIMEZONE)
_EXPORT_ZONES = {"NASA Team": "America/Chicago", "EMEA Team": "Asia/Dubai", "APAC Team": "Asia/Manila"}
_SHIFTS_HEADER = ("Member", "Work Email", "Group", "Start Date", "Start Time", "End Date", "End Time", "Theme Color",
                  "Custom Label", "Unpaid Break (minutes)", "Notes", "Shared")
_TIME_OFF_HEADER = ("Member", "Work Email", "Start Date", "Start Time", "End Date", "End Time", "Time Off Reason",
                    "Theme Color", "Notes", "Shared")
_AVERY, _JORDAN, _RILEY, _SAM, _TAYLOR, _MORGAN = (agent[:2] for agent in AGENTS)
# Each run of a person's schedule: who, their Group, its first and last day, the days worked (Sunday = 0) and the
# hours, on the clock of the Group's region. Every other day of the run is a Rest Day.
_EXPORT_RUNS = (
    (_AVERY, "NASA Team", date(2026, 10, 4), date(2026, 10, 31), SUN_THU, "08:00", "17:00"),
    (_JORDAN, "NASA Team", date(2026, 10, 4), date(2026, 10, 17), SUN_THU, "08:00", "17:00"),
    (_JORDAN, "NASA Team", date(2026, 10, 18), date(2026, 10, 31), MON_FRI, "09:00", "18:00"),
    (_RILEY, "EMEA Team", date(2026, 10, 4), date(2026, 10, 31), TUE_SAT, "09:00", "18:00"),
    (_SAM, "APAC Team", date(2026, 10, 4), date(2026, 10, 17), MON_FRI, "08:00", "17:00"),
    (_SAM, "NASA Team", date(2026, 10, 18), date(2026, 10, 31), TUE_SAT, "08:00", "17:00"),
    (_TAYLOR, "NASA Team", date(2026, 10, 4), date(2026, 10, 31), TUE_SAT, "08:00", "17:00"),
    (_MORGAN, "NASA Team", date(2026, 10, 11), date(2026, 10, 31), TUE_SAT, "10:00", "19:00"),
    (DEMO_TL, "NASA Team", date(2026, 10, 4), date(2026, 10, 31), MON_FRI, "08:00", "17:00"),
)
# Days unlike the rest of their run: (email, day) -> (start, end, Custom Label, Notes).
_EXPORT_DAYS = {
    (_AVERY[1], date(2026, 10, 14)): ("08:00", "17:00", "", "PTO"),
    (_JORDAN[1], date(2026, 10, 27)): ("09:00", "13:00", "Half Day SL", ""),
    (_RILEY[1], date(2026, 10, 20)): ("09:00", "18:00", "Training", ""),
    (_RILEY[1], date(2026, 10, 21)): ("09:00", "18:00", "Training", ""),
    (_TAYLOR[1], date(2026, 10, 23)): ("20:00", "04:00", "", ""),
}
# The Time Off sheet: who, the reason, and its start and end on Chicago's clock (an all-day entry runs midnight to
# midnight; Taylor's day off is entered with times, 8 to 5, which is a full day).
_EXPORT_TIME_OFF = (
    (_JORDAN, "Sick Leave", datetime(2026, 10, 20, 0, 0), datetime(2026, 10, 21, 0, 0)),
    (_TAYLOR, "Annual Leave", datetime(2026, 10, 29, 8, 0), datetime(2026, 10, 29, 17, 0)),
)


def _written(moment: datetime) -> tuple[str, str]:
    """A moment as the export writes it: its date M/D/YYYY and its time HH:MM."""
    return f"{moment.month}/{moment.day}/{moment.year}", f"{moment:%H:%M}"


def _export_shift(person: tuple[str, str], group: str, day: date, start: str, end: str, label: str,
                  note: str) -> list:
    """One Shifts row: `day` from `start` to `end` on the Group's region clock (an end at or before the start is the
    next day), written on the exporter's clock as the export does."""
    zone, chicago = ZoneInfo(_EXPORT_ZONES[group]), ZoneInfo(SHIFTS_EXPORT_ZONE)
    begins = datetime.combine(day, time.fromisoformat(start), tzinfo=zone)
    ends = datetime.combine(day, time.fromisoformat(end), tzinfo=zone)
    if ends <= begins:
        ends += timedelta(days=1)
    return [*person, group, *_written(begins.astimezone(chicago)), *_written(ends.astimezone(chicago)), "1. White",
            label, 60, note, "1. Shared"]


def shifts_export() -> bytes:
    """The hosted demo's invented Teams Shifts export, as an Excel workbook's bytes in the export's layout: a Shifts
    sheet (one row per person per day, with an open shift) and a Time Off sheet. Built in memory; never written."""
    book = openpyxl.Workbook()
    shifts = book.active
    shifts.title = "Shifts"
    shifts.append(_SHIFTS_HEADER)
    for person, group, first, last, days, start, end in _EXPORT_RUNS:
        for n in range((last - first).days + 1):
            day = first + timedelta(days=n)
            usual = (start, end, "" if (day.weekday() + 1) % 7 in days else "Rest Day", "")
            shifts.append(_export_shift(person, group, day, *_EXPORT_DAYS.get((person[1], day), usual)))
    shifts.append(_export_shift(("", ""), "NASA Team", date(2026, 10, 24), "08:00", "17:00", "", ""))
    off = book.create_sheet("Time Off")
    off.append(_TIME_OFF_HEADER)
    for person, reason, start, end in _EXPORT_TIME_OFF:
        off.append([*person, *_written(start), *_written(end), reason, "1. White", "", "1. Shared"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# -- 1:1 prep -----------------------------------------------------------------------------------------------

# Each invented tech's usual two weeks, in AGENTS order: answered calls, declined/missed calls, solved tickets.
PREP_PROFILES = ((30, 1, 52), (9, 3, 38), (16, 1, 46), (22, 2, 41), (14, 1, 35), (19, 2, 44))
# Each invented tech's 1:1 schedule, in AGENTS order (None: no 1:1 date set). As of the demo's usual day, Tue Oct 20,
# the first tech's 1:1 is today, the next two's tomorrow and in two days, the fourth's on Wednesdays in the other week
# from the second's, the fifth's on Fridays, and the last tech has none.
PREP_SCHEDULES = (
    cycle.Schedule(date(2026, 10, 20), 2),
    cycle.Schedule(date(2026, 10, 21), 2),
    cycle.Schedule(date(2026, 10, 22), 2),
    cycle.Schedule(date(2026, 10, 28), 2),
    cycle.Schedule(date(2026, 10, 16), 2),
    None,
)
PREP_STATUSES = ("open", "open", "pending", "hold", "new")
PREP_PRIORITIES = ("normal", "normal", "normal", "normal", "high", "low", "urgent")
PREP_NEVER_REPLIED = 0.12          # how often an aging ticket has no public reply from its tech
# Invented ratings: Zendesk's reasons for a bad rating, and what customers wrote (None: no comment).
RATING_REASONS = ("The issue took too long to resolve", "The issue was not resolved", "Some other reason")
GOOD_COMMENTS = ("Fixed the payment terminal on the first call, thank you!", "Very patient and walked me through it.",
                 "Quick and friendly.", "Knew exactly what was wrong with the self-checkout.", None, None)
BAD_COMMENTS = ("Took three calls before anyone could help.", "The receipt printer still jams.", None)


def seed_schedules(conn) -> None:
    """Set the invented techs' 1:1 schedules (PREP_SCHEDULES). Their preps are counted from what `seed_collected`
    invents whenever 1:1 Prep asks, as real ones are: nothing is made or stored."""
    settings.set_settings(conn, {"oneonone_schedule": {
        AGENTS[index][1]: {"date": s.date.isoformat(), "weeks": s.weeks}
        for index, s in enumerate(PREP_SCHEDULES) if s is not None}})


# -- what the hourly sync collects --------------------------------------------------------------------------

# The demo invents what the hourly sync stores (`activity.collected`), so 1:1 Prep and Trends count the demo's calls,
# ratings, solved tickets and aging tickets as they count real ones. Each tech's calls are spread over the days Team
# activity has them working, on their own clock (`activity.report.tech_day_facts`), between their first and last
# action of the day (never in a long gap), at about their usual two weeks (PREP_PROFILES): its numbers are over the
# PERIOD_WORK_DAYS of two five-day weeks. The rest of the department answers calls too, so the queue a tech's "% of
# calls" is over is department-wide, as the real one is.
FIRST_AGENT_ID = 900001             # the invented techs' Zendesk ids: AGENTS in order, then REGION_TECHS
FIRST_COLLECTED_TICKET = 80001      # the tickets behind the collected calls, solves and aging lists
FIRST_TALK_ID = 800_000_001         # Talk call and leg ids, from one run so no two share a number
FIRST_RATING_ID = 700_000_001
# Each kind of id comes in blocks of ID_BLOCK from its FIRST_ id (`_ids`): one block for AGENTS, one for REGION_TECHS
# and one for the rest of the department.
ID_BLOCK = 10 ** 5
TEAM_IDS, REGION_IDS, DEPARTMENT_IDS = 0, 1, 2
PERIOD_WORK_DAYS = 10               # the work days in two five-day weeks
# REGION_TECHS' usual two weeks, in that order, as PREP_PROFILES: answered calls, declined/missed calls, solved tickets.
REGION_PROFILES = ((20, 2, 40), (14, 1, 34), (24, 2, 47), (17, 1, 38))
DEPARTMENT_AGENTS = tuple(range(990001, 990025))   # the rest of the department, never on the team
DEPARTMENT_CALLS = (33, 15)                         # their answered calls on a weekday, and on a weekend day
DEPARTMENT_HOURS = (7 * 60, 21 * 60)                # when they take them, on the app's clock
TALK_SECONDS = (3 * 60, 12 * 60, 6 * 60)            # an answered call's talk time: shortest, longest, most usual
SOLVED_ON_CALL = 0.7                # how often an answered call's ticket is solved after the call
RATED, RATED_BAD = 0.05, 0.15       # how often a solved ticket is rated, and how often a rating is bad
UNREACHABLE_DAY = 0.02              # how often a worked day has a few unreachable rings (a phone or connection fault)
AGING_DAYS = 14                     # aging lists recorded: each tech's today and the 13 days before it
AGING_RECORDED = 5                  # the first hourly sync of a tech's day records its list (local minutes)
AGING_POOL = (6, 14)                # how many tickets each tech has open long enough to age, over the demo's weeks
AGING_OPEN = (9, 45)                # how many days an aging ticket stays open before it is solved, when it is


def seed_collected(conn, now: datetime) -> None:
    """Invent what the hourly sync would have collected by `now` for the invented techs (AGENTS, Zendesk ids from
    FIRST_AGENT_ID): Talk legs and calls at about their usual two weeks (PREP_PROFILES), spread over the days Team
    activity has them working, with talk times of 3 to 12 minutes and the rest of the department's answered calls;
    their tickets' priorities and solves (PREP_PROFILES' solved tickets); ratings, with reasons and comments
    (RATING_REASONS, GOOD_COMMENTS, BAD_COMMENTS); an aging list for each of their last AGING_DAYS days; and every
    kind collected from Team activity's first day to `now`. Run after `seed_activity`. Deterministic for a given
    `now` (seeded with SEED + 4)."""
    zone = ZoneInfo(settings.get_timezone(conn))
    first = store.synced_from(conn)
    first_day, today = first.astimezone(zone).date(), now.astimezone(zone).date()
    rng = random.Random(SEED + 4)
    talk, tickets = _ids(FIRST_TALK_ID, DEPARTMENT_IDS), _ids(FIRST_COLLECTED_TICKET, DEPARTMENT_IDS)
    legs: list[dict] = []
    calls: list[dict] = []
    for offset in range((today - first_day).days + 1):
        day = first_day + timedelta(days=offset)
        weekend = (day.weekday() + 1) % 7 in (0, 6)
        for _ in range(round(DEPARTMENT_CALLS[weekend] * rng.uniform(0.85, 1.15))):
            at = _local_utc(day, rng.uniform(*DEPARTMENT_HOURS), zone)
            if at < now:
                call = next(talk)
                calls.append({"id": call, "direction": "inbound", "ticket_id": next(tickets)})
                legs.append(_leg(next(talk), call, rng.choice(DEPARTMENT_AGENTS), "completed", at, _talk(rng)))
    collected.upsert_legs(conn, legs)
    collected.upsert_calls(conn, calls)
    _collect_for(conn, [(email, FIRST_AGENT_ID + index, PREP_PROFILES[index])
                        for index, (_, email, _) in enumerate(AGENTS)],
                 first=first_day, now=now, rng=rng, block=TEAM_IDS)
    for kind in collected.KINDS:
        collected.save_state(conn, kind, covered_from=first, through=now)


# A year of the demo's past before Team activity's ACTIVITY_WEEKS (the owner, 2026-10-22: so 1:1 Prep's Compare of 6
# months and a year, and Trends' Last 6 months and Last year, show something): HISTORY_WEEKS in all. It is invented
# by its own random draws and ids (HISTORY_TEAM_IDS, HISTORY_DEPARTMENT_IDS, HISTORY_EVENTS), so the last
# ACTIVITY_WEEKS stay exactly as they were: ordinary weeks, every tech on their usual shifts, no unusual days.
HISTORY_WEEKS = 53
HISTORY_TEAM_IDS, HISTORY_DEPARTMENT_IDS = 3, 4
HISTORY_EVENTS = FIRST_ACTIVITY_TICKET * 100 + 10 ** 6


def seed_history(conn, now: datetime) -> None:
    """Invent the invented techs' Team activity and what the hourly sync collected (calls, ratings, solves) over the
    HISTORY_WEEKS before Team activity's first day, and move the sync's and the collection's start back to it. Run
    after `seed_activity` and `seed_collected`. Deterministic for a given `now` (seeded with SEED + 11)."""
    tz = settings.get_timezone(conn)
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    activity_first = today - timedelta(days=ACTIVITY_WEEKS * 7 - 1)
    start = today - timedelta(days=HISTORY_WEEKS * 7 - 1)
    rng = random.Random(SEED + 11)
    events = iter(range(HISTORY_EVENTS, HISTORY_EVENTS + 10 ** 6))
    actions: list[Action] = []
    signins: list[SignIn] = []
    for index, (_, email, _) in enumerate(AGENTS):
        work_days = SUN_THU if index < 2 else TUE_SAT
        store.add_shift_version(conn, ShiftVersion(email, start, work_days, SHIFT_START, SHIFT_END, True))
        tickets = list(range(FIRST_ACTIVITY_TICKET + index * TICKETS_PER_TECH,
                             FIRST_ACTIVITY_TICKET + (index + 1) * TICKETS_PER_TECH))
        for offset in range((activity_first - start).days):
            day = start + timedelta(days=offset)
            if (day.weekday() + 1) % 7 not in work_days:
                continue
            signins.append(SignIn(email, _local_utc(day, SHIFT_START + rng.uniform(-12, 8), zone)))
            spacing, late = _drift(index, day, today)
            minute = SHIFT_START + rng.uniform(5, 30) + late
            while minute < SHIFT_END - 20:
                kind = rng.choices([k for k, _ in _ACTION_KINDS], [w for _, w in _ACTION_KINDS])[0]
                old, new = _action_values(kind, rng)
                actions.append(Action(email, rng.choice(tickets), _local_utc(day, minute, zone), kind, old, new,
                                      next(events), 0))
                minute += rng.uniform(15, 45) * spacing
    store.insert_actions(conn, actions)
    store.insert_signins(conn, signins)
    began = _local_utc(start, 0, zone)
    conn.execute("UPDATE activity_sync SET window_start = ?", (store.iso(began),))
    conn.commit()

    talk, tickets_ids = _ids(FIRST_TALK_ID, HISTORY_DEPARTMENT_IDS), _ids(FIRST_COLLECTED_TICKET, HISTORY_DEPARTMENT_IDS)
    legs: list[dict] = []
    calls: list[dict] = []
    for offset in range((activity_first - start).days):
        day = start + timedelta(days=offset)
        weekend = (day.weekday() + 1) % 7 in (0, 6)
        for _ in range(round(DEPARTMENT_CALLS[weekend] * rng.uniform(0.85, 1.15))):
            call = next(talk)
            calls.append({"id": call, "direction": "inbound", "ticket_id": next(tickets_ids)})
            legs.append(_leg(next(talk), call, rng.choice(DEPARTMENT_AGENTS), "completed",
                             _local_utc(day, rng.uniform(*DEPARTMENT_HOURS), zone), _talk(rng)))
    collected.upsert_legs(conn, legs)
    collected.upsert_calls(conn, calls)
    _collect_for(conn, [(email, FIRST_AGENT_ID + index, PREP_PROFILES[index])
                        for index, (_, email, _) in enumerate(AGENTS)],
                 first=start, now=now, rng=rng, block=HISTORY_TEAM_IDS, until=activity_first - timedelta(days=1))
    for kind in collected.KINDS:
        collected.save_state(conn, kind, covered_from=began, through=now)


def _ids(first: int, block: int) -> Iterator[int]:
    """The ids of one kind that `block` (TEAM_IDS, REGION_IDS or DEPARTMENT_IDS) has, from that kind's `first`."""
    return iter(range(first + block * ID_BLOCK, first + (block + 1) * ID_BLOCK))


def _talk(rng: random.Random) -> int:
    return int(rng.triangular(*TALK_SECONDS))


def _leg(leg_id: int, call_id: int, agent_id: int, status: str, at: datetime, talk: int) -> dict:
    """One agent leg as the Talk legs export yields it."""
    return {"id": leg_id, "call_id": call_id, "agent_id": agent_id, "type": "agent", "completion_status": status,
            "created_at": store.iso(at), "talk_time": talk}


def _about(mean: float, rng: random.Random) -> int:
    """A whole count within a fifth of `mean`, rounded up or down at random so the counts average `mean`."""
    near = mean * rng.uniform(0.8, 1.2)
    return int(near) + (rng.random() < near - int(near))


def _collect_for(conn, techs: list[tuple[str, int, tuple[int, int, int]]], *, first: date, now: datetime,
                 rng: random.Random, block: int, until: date | None = None) -> None:
    """Store each of `techs` ((email, Zendesk id, usual two weeks as PREP_PROFILES)) with their Zendesk id, and invent
    their collected calls, tickets, ratings and aging lists from their days from `first` up to `now` (see
    `seed_collected`), each day on their own clock (`regions.day_zones`). `block` (TEAM_IDS or REGION_IDS) picks their
    ids (`_ids`). With `until`, only the days through it, and no aging lists (those are each tech's last
    AGING_DAYS)."""
    today = now.astimezone(ZoneInfo(settings.get_timezone(conn))).date()
    talk, tickets = _ids(FIRST_TALK_ID, block), _ids(FIRST_COLLECTED_TICKET, block)
    rating_ids = _ids(FIRST_RATING_ID, block)
    legs: list[dict] = []
    calls: list[dict] = []
    states: list[dict] = []
    solves: list[dict] = []
    ratings: list[dict] = []
    collected.set_agents(conn, {email: agent_id for email, agent_id, _ in techs})

    def solved(ticket: int, agent_id: int, at: datetime) -> None:
        solves.append({"ticket_id": ticket, "solved_at": store.iso(at)})
        if rng.random() < RATED:
            made = at + timedelta(hours=rng.uniform(1, 30))
            if made < now:
                bad = rng.random() < RATED_BAD
                ratings.append({"id": next(rating_ids), "ticket_id": ticket, "assignee_id": agent_id,
                                "score": "bad" if bad else "good", "created_at": store.iso(made),
                                "reason": rng.choice(RATING_REASONS) if bad else None,
                                "comment": rng.choice(BAD_COMMENTS if bad else GOOD_COMMENTS)})

    for email, agent_id, (usual_calls, usual_missed, usual_solved) in techs:
        facts = activity_report.tech_day_facts(conn, email, first, until or today + timedelta(days=1), today=today,
                                               now=now)
        for f in facts:
            if f.state not in ("worked", "off") or f.first is None:
                continue
            moment = _moment_on(f, now, rng)
            if moment is None:
                continue
            on_call = 0
            for _ in range(_about(usual_calls / PERIOD_WORK_DAYS, rng)):
                at, call, ticket, seconds = moment(), next(talk), next(tickets), _talk(rng)
                calls.append({"id": call, "direction": "inbound", "ticket_id": ticket})
                legs.append(_leg(next(talk), call, agent_id, "completed", at, seconds))
                done = at + timedelta(seconds=seconds, minutes=rng.uniform(1, 15))
                is_solved = rng.random() < SOLVED_ON_CALL and done < now
                states.append({"id": ticket, "assignee_id": agent_id, "priority": rng.choice(PREP_PRIORITIES),
                               "status": "solved" if is_solved else rng.choice(("open", "pending")), "tags": []})
                if is_solved:
                    solved(ticket, agent_id, done)
                    on_call += 1
            not_taken = [rng.choice(("agent_declined", "agent_missed"))
                         for _ in range(_about(usual_missed / PERIOD_WORK_DAYS, rng))]
            if rng.random() < UNREACHABLE_DAY:
                not_taken += ["agent_unreachable"] * rng.randint(2, 5)
            for status in not_taken:   # each rang the tech first, then someone else in the department answered it
                at, call = moment(), next(talk)
                calls.append({"id": call, "direction": "inbound", "ticket_id": next(tickets)})
                legs.append(_leg(next(talk), call, agent_id, status, at, 0))
                legs.append(_leg(next(talk), call, rng.choice(DEPARTMENT_AGENTS), "completed",
                                 at + timedelta(seconds=rng.uniform(20, 40)), _talk(rng)))
            for _ in range(max(0, _about(usual_solved / PERIOD_WORK_DAYS, rng) - on_call)):   # emails and web forms
                ticket = next(tickets)
                states.append({"id": ticket, "assignee_id": agent_id, "priority": rng.choice(PREP_PRIORITIES),
                               "status": "solved", "tags": []})
                solved(ticket, agent_id, moment())
        if until is None:
            _record_aging(conn, email, now=now, today=today, rng=rng, tickets=tickets)
    collected.upsert_legs(conn, legs)
    collected.upsert_calls(conn, calls)
    collected.upsert_ticket_states(conn, states, solves)
    collected.upsert_ratings(conn, ratings)


def _moment_on(f: DayFacts, now: datetime, rng: random.Random) -> Callable[[], datetime] | None:
    """A picker of random moments on the tech's day `f` between their first and last action, and before
    `now`, never in the day's long gap; None when the day has no such stretch."""
    start, end = f.first, min(f.last, now)
    if end <= start:
        return None
    gap_start, gap = end, timedelta(0)
    if any(flag.kind == "long_gap" for flag in f.flags) and f.gap_start is not None and f.gap_start < end:
        gap_start, gap = f.gap_start, min(f.gap_start + timedelta(minutes=f.gap_min), end) - f.gap_start
    span = end - start - gap

    def moment() -> datetime:
        at = start + span * rng.random()
        return at + gap if at >= gap_start else at

    return moment


def _record_aging(conn, email: str, *, now: datetime, today: date, rng: random.Random,
                  tickets: Iterator[int]) -> None:
    """Record the tech's aging list for each of their last AGING_DAYS days, on their clock, as the first hourly sync of
    each day would: their tickets open longer than `collect.AGING_AFTER` then, oldest first, each with its status and
    their last public reply before the list was recorded (some never had one). The tickets come from one pool, so a
    ticket stays on the lists from the day it ages until it is solved."""
    zone_on = regions.day_zones(conn, email)
    their_today = regions.today_for(conn, email, now, today)
    pool = []
    for _ in range(rng.randint(*AGING_POOL)):
        opened_on = their_today - timedelta(days=rng.randint(9, 60))
        opened = _local_utc(opened_on, rng.uniform(SHIFT_START, SHIFT_END), ZoneInfo(zone_on(opened_on)))
        solved_on = None if rng.random() < 0.35 else opened_on + timedelta(days=rng.randint(*AGING_OPEN))
        replies = [] if rng.random() < PREP_NEVER_REPLIED else sorted(
            opened + timedelta(days=rng.uniform(1, (now - opened).days or 1)) for _ in range(rng.randint(1, 4)))
        pool.append((opened, next(tickets), rng.choice(PREP_STATUSES), solved_on, replies))
    pool.sort()
    for back in range(AGING_DAYS - 1, -1, -1):
        day = their_today - timedelta(days=back)
        recorded = min(_local_utc(day, AGING_RECORDED, ZoneInfo(zone_on(day))), now)
        items = []
        for opened, ticket, status, solved_on, replies in pool:
            if recorded - opened > collect.AGING_AFTER and (solved_on is None or solved_on > day):
                before = [r for r in replies if r < recorded]
                items.append(PrepItem("aging", ticket, store.iso(opened), None, status,
                                      last_reply=store.iso(before[-1]) if before else None))
        collected.record_aging(conn, email, day, items, now=recorded)


def _date_versions(conn, last_week: tuple[datetime, datetime]) -> None:
    """Date the rubric versions within the demo's weeks (they are saved at the real time): the first the day
    before the first week, the second at the start of the week it was saved before."""
    first = last_week[0] - timedelta(weeks=WEEKS - 1, days=1)
    second = last_week[0] - timedelta(weeks=WEEKS - 1 - WEEKS // 2)
    for version_id, when in ((1, first), (2, second)):
        conn.execute("UPDATE rubric_versions SET created_at = ? WHERE version_id = ?",
                     (when.astimezone(timezone.utc).isoformat(timespec="seconds"), version_id))
    conn.commit()


def _second_version(conn, version):
    """A rubric edit partway through, so the reports show results under two versions."""
    checks = tuple(
        dataclasses.replace(c, definition=c.definition + " Mentioning the summary email counts.")
        if c.check_id == "sc15" else c
        for c in version.checks
    )
    return rubric.save_version(conn, checks, version.reference, version.params,
                               "Demo: clarified #15 (what comes next)")


def _failed_manual_run(conn, window, log_dir: Path) -> None:
    def unreachable(w, log):
        raise ZendeskError("Demo: Zendesk could not be reached")

    run_id = pipeline.start_run(conn, "manual", window, fetcher=unreachable, anthropic_client=None,
                                images_loader=lambda contact, log: [], log_dir=log_dir, sleep=lambda s: None)
    _date_run(conn, run_id, window[1] + timedelta(days=2, hours=15), 1)


def _model_test(conn, reviewer: OfflineReviewer, window, log_dir: Path, now: datetime) -> None:
    """A finished model test through the real test path (`pipeline.start_test`), and one owner override on
    a shown review it was compared with, on a check where the test differs when there is one, so the
    comparison and its override line have something to show."""
    run_id = pipeline.start_test(
        conn, window, model_id=TEST_MODEL, effort=TEST_EFFORT, limit=TEST_CALLS, anthropic_client=reviewer,
        images_loader=lambda contact, log: [], confirm=True, log_dir=log_dir, sleep=lambda seconds: None)
    _date_run(conn, run_id, window[1] + timedelta(hours=33), 25)   # Monday about 09:00, after the weekly run
    row = conn.execute(
        "SELECT s.review_id, s.check_id, s.outcome, t.outcome AS test_outcome FROM reviews tr "
        "JOIN check_results t ON t.review_id = tr.review_id "
        "JOIN check_results s ON s.review_id = tr.compared_review_id AND s.check_id = t.check_id "
        "WHERE tr.run_id = ? AND s.decided_by = 'model' AND s.override_outcome IS NULL "
        "ORDER BY s.outcome = t.outcome, s.review_id, s.check_id LIMIT 1", (run_id,)).fetchone()
    outcome = row["test_outcome"] if row["test_outcome"] != row["outcome"] else (
        "missed" if row["outcome"] == "met" else "met")
    conn.execute(
        "UPDATE check_results SET override_outcome = ?, override_note = ?, override_at = ? "
        "WHERE review_id = ? AND check_id = ?",
        (outcome, "Demo: the reviewer got this one wrong.", _iso(now - timedelta(days=1)),
         row["review_id"], row["check_id"]))
    conn.commit()
