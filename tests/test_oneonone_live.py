"""1:1 preps counted from the collected data (`oneonone.live.count_preps`, Collected calls design, "How the numbers
are counted").

The parity check: test_oneonone_make's fixtures (its legs, calls, ratings, tickets and aging ticket), stored here with
the collection's upserts instead of served by a fake Zendesk, give the same numbers and items its preps were made with.
Its solved counts (34 and 12, canned search answers there) are tickets stored as solved in the tech's window here.

DAY is Tue Oct 20 2026; the period is Oct 6 to Oct 19. Chicago is on CDT (UTC-5): its period runs 05:00Z Oct 6 to
05:00Z Oct 20. Manila is UTC+8: its period runs 16:00Z Oct 5 to 16:00Z Oct 19."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import count
from zoneinfo import ZoneInfo

import pytest

from work_assistant import regions
from work_assistant.activity import collected
from work_assistant.activity import store as activity_store
from work_assistant.activity.model import Action, ShiftVersion
from work_assistant.oneonone import live
from work_assistant.oneonone.attendance import attendances
from work_assistant.oneonone.cycle import period_for
from work_assistant.oneonone.model import PrepItem, TechPrep
from work_assistant.settings import set_setting

UTC = timezone.utc
TZ = "America/Chicago"
CHI = ZoneInfo(TZ)
DAY = date(2026, 10, 20)                              # Pat's and Sam's 1:1 day
PERIOD = period_for(DAY)                              # Tue 10/6 - Mon 10/19
NOW = datetime(2026, 10, 20, 11, 30, tzinfo=UTC)      # 06:30 in Chicago on the 1:1 day
WINDOW_START = datetime(2026, 10, 6, 5, tzinfo=UTC)   # local midnight starting 10/6 (CDT)
WINDOW_END = datetime(2026, 10, 20, 5, tzinfo=UTC)    # local midnight after 10/19
COVERED_FROM = datetime(2026, 8, 1, tzinfo=UTC)       # where every collected kind's stretch begins here
PAT, SAM, KIM, GONE = "pat@example.com", "sam@example.com", "kim@example.com", "gone@example.com"
PAT_ID, SAM_ID, KIM_ID, OTHER_ID = 77, 88, 66, 99
EVERY_OTHER_TUESDAY = {"date": "2026-10-20", "weeks": 2}
MON_FRI = frozenset({1, 2, 3, 4, 5})                  # Sunday = 0
CALL_KINDS = ("answered", "declined", "missed", "unreachable")
KINDS = ("legs", "calls", "ratings", "tickets")
_ids = count(1)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(day: date, hh: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, tzinfo=CHI).astimezone(UTC)


def z(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC moment in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def leg(id, call_id, agent_id, status="completed", talk=120, at=datetime(2026, 10, 7, 15, tzinfo=UTC), type="agent"):
    return {"id": id, "call_id": call_id, "agent_id": agent_id, "type": type, "completion_status": status,
            "created_at": stamp(at), "talk_time": talk, "updated_at": stamp(at + timedelta(minutes=10))}


def call(id, ticket_id, direction="inbound"):
    return {"id": id, "direction": direction, "ticket_id": ticket_id, "updated_at": "2026-10-10T00:00:00Z"}


def rating(id, assignee_id, score, *, at, ticket_id, reason=None, comment=""):
    return {"id": id, "assignee_id": assignee_id, "score": score, "ticket_id": ticket_id, "created_at": stamp(at),
            "reason": reason, "comment": comment}


def solve(id, assignee_id, when):
    """A ticket solved at `when` while assigned to `assignee_id`: the ticket and its metric set, as the incremental
    ticket export pages carry them."""
    return ({"id": id, "assignee_id": assignee_id, "status": "solved", "priority": "normal", "tags": []},
            {"ticket_id": id, "solved_at": stamp(when)})


# test_oneonone_make's fixtures, as its fake Zendesk served them.
LEGS = [
    leg(1, 100, PAT_ID, talk=300, at=datetime(2026, 10, 7, 15, tzinfo=UTC)),
    leg(2, 101, PAT_ID, status="agent_declined", talk=0, at=datetime(2026, 10, 8, 15, tzinfo=UTC)),
    leg(3, 101, SAM_ID, talk=200, at=datetime(2026, 10, 8, 15, 1, tzinfo=UTC)),
    leg(4, 102, OTHER_ID, talk=100, at=datetime(2026, 10, 9, 14, tzinfo=UTC)),         # the queue only
    leg(5, 103, SAM_ID, status="agent_missed", talk=0, at=datetime(2026, 10, 9, 15, tzinfo=UTC)),
    leg(6, 104, PAT_ID, status="agent_unreachable", talk=0, at=datetime(2026, 10, 9, 16, tzinfo=UTC)),
    leg(7, 105, PAT_ID, talk=50, at=datetime(2026, 10, 9, 17, tzinfo=UTC)),            # outbound: not counted
    leg(8, 100, None, talk=300, at=datetime(2026, 10, 7, 15, tzinfo=UTC), type="customer"),
    leg(9, 106, PAT_ID, talk=90, at=datetime(2026, 10, 6, 4, tzinfo=UTC)),             # 10/5 23:00 local: before
]
CALLS = [call(100, 5001), call(101, 5002), call(102, 5003), call(103, 5004), call(104, 5005),
         call(105, 5006, "outbound"), call(106, 5007)]
TICKETS = [{"id": 5001, "priority": "normal", "tags": ["pos"]}, {"id": 5002, "priority": "urgent", "tags": []}]
RATINGS = [rating(1, PAT_ID, "good", at=datetime(2026, 10, 12, 15, tzinfo=UTC), ticket_id=7001),
           rating(2, PAT_ID, "good", at=datetime(2026, 10, 13, 15, tzinfo=UTC), ticket_id=7002),
           rating(3, PAT_ID, "bad", at=datetime(2026, 10, 14, 15, tzinfo=UTC), ticket_id=7003),
           rating(4, SAM_ID, "good", at=datetime(2026, 10, 12, 16, tzinfo=UTC), ticket_id=7004),
           rating(5, OTHER_ID, "bad", at=datetime(2026, 10, 12, 17, tzinfo=UTC), ticket_id=7005)]
# The canned solved counts (34 for Pat, 12 for Sam), as tickets solved in their window, and three that don't count.
SOLVES = ([solve(20000 + n, PAT_ID, at(PERIOD.start + timedelta(days=n % 14), 10)) for n in range(34)]
          + [solve(30000 + n, SAM_ID, at(PERIOD.start + timedelta(days=n % 14), 11)) for n in range(12)]
          + [solve(40001, PAT_ID, WINDOW_START - timedelta(minutes=1)),     # 23:59 10/5 local: before
             solve(40002, PAT_ID, WINDOW_END),                              # midnight starting the 1:1 day: after
             solve(40003, OTHER_ID, at(date(2026, 10, 8), 10))])            # another agent's
PAT_AGING = PrepItem("aging", 6001, stamp(NOW - timedelta(days=8)), None, "open", last_reply="2026-10-12T16:20:00Z")

PAT_CALL_ITEMS = (
    PrepItem("answered", 5001, "2026-10-07T15:00:00Z", 300, None, priority="normal", tags=("pos",)),
    PrepItem("declined", 5002, "2026-10-08T15:00:00Z", 0, None),
    PrepItem("unreachable", 5005, "2026-10-09T16:00:00Z", 0, None),
)
PAT_RATING_ITEMS = (
    PrepItem("rating", 7001, "2026-10-12T15:00:00Z", None, None, score="good"),
    PrepItem("rating", 7002, "2026-10-13T15:00:00Z", None, None, score="good"),
    PrepItem("rating", 7003, "2026-10-14T15:00:00Z", None, None, score="bad"),
)
PAT_AGING_ITEMS = (PAT_AGING,)
PAT_DAYS_OUT = (PrepItem("pto", None, "2026-10-13", None, None), PrepItem("sick", None, "2026-10-14", None, None),
                PrepItem("sick", None, "2026-10-15", None, None))
SAM_ITEMS = (
    PrepItem("answered", 5002, "2026-10-08T15:01:00Z", 200, None, priority="urgent"),
    PrepItem("missed", 5004, "2026-10-09T15:00:00Z", 0, None),
    PrepItem("rating", 7004, "2026-10-12T16:00:00Z", None, None, score="good"),
)


def sync_activity(conn, through=NOW, window_start="2026-09-01T05:00:00Z"):
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)",
                 (stamp(through), stamp(through), stamp(through), window_start))
    conn.commit()


def work(conn, email, *, pto=(), sick=(), idle=()):
    """A Monday-Friday shift from 9/1, with a reply on every workday of the period except the days named."""
    activity_store.add_shift_version(conn, ShiftVersion(email, date(2026, 9, 1), MON_FRI, 480, 1020, True))
    for d in pto:
        activity_store.add_time_off(conn, email, d, d, "", "pto")
    for d in sick:
        activity_store.add_time_off(conn, email, d, d, "", "sick")
    day = PERIOD.start
    while day <= PERIOD.end:
        if day.weekday() < 5 and day not in (*pto, *sick, *idle):
            activity_store.insert_actions(conn, [Action(email, next(_ids), at(day, 9), "reply", None, None,
                                                        next(_ids), 0)])
        day += timedelta(days=1)


def cover(conn, kinds=KINDS, *, start=COVERED_FROM, through=NOW):
    """Every kind of `kinds` collected from `start` through `through`."""
    for kind in kinds:
        collected.save_state(conn, kind, covered_from=start, through=through)


def collect(conn, *, legs=LEGS, calls=CALLS, tickets=TICKETS, ratings=RATINGS, solves=SOLVES, aging=True):
    """Store the rows the fake Zendesk served, as the hourly sync stores them, and Pat's and Sam's aging lists recorded
    on the 1:1 day (the old prep's snapshot that morning)."""
    collected.upsert_legs(conn, legs)
    collected.upsert_calls(conn, calls)
    collected.upsert_ticket_states(conn, list(tickets), [])
    collected.upsert_ticket_states(conn, [t for t, _ in solves], [m for _, m in solves])
    collected.upsert_ratings(conn, ratings)
    if aging:
        collected.record_aging(conn, PAT, DAY, [PAT_AGING], now=NOW)
        collected.record_aging(conn, SAM, DAY, [], now=NOW)


PTO_DAY, SICK_DAY, IDLE_DAY = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)


@pytest.fixture
def db(conn):
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [PAT, SAM])
    set_setting(conn, "oneonone_schedule", {PAT: EVERY_OTHER_TUESDAY, SAM: EVERY_OTHER_TUESDAY})
    sync_activity(conn)
    work(conn, PAT, pto=(PTO_DAY,), sick=(SICK_DAY,), idle=(IDLE_DAY,))
    collected.set_agents(conn, {PAT: PAT_ID, SAM: SAM_ID})
    cover(conn)
    return conn


BOTH = ((PAT, DAY), (SAM, DAY))


def count_preps(conn, wanted=BOTH, now=NOW) -> list[live.Counted]:
    return live.count_preps(conn, list(wanted), now=now)


def techs(conn, wanted=BOTH, now=NOW) -> list[TechPrep]:
    return [c.tech for c in count_preps(conn, wanted, now)]


# -- the parity check: test_oneonone_make's numbers and items ---------------------------------------------------


@pytest.mark.parametrize("cleared", [False, True], ids=["no region", "region set and cleared"])
def test_counts_every_column(db, cleared):
    if cleared:
        regions.set_regions(db, PAT, [(date(2026, 1, 1), "APAC")])
        regions.set_regions(db, PAT, [])
    collect(db)
    pat_prep, sam_prep = count_preps(db)
    assert (pat_prep.period, sam_prep.period) == (PERIOD, PERIOD)
    assert (pat_prep.queue, sam_prep.queue) == (3, 3), "every answered inbound agent leg in the period, from any agent"
    assert (pat_prep.failures, sam_prep.failures) == ({}, {})
    assert pat_prep.tech == TechPrep(PAT, calls=1, declined_missed=1, unreachable=1, talk_seconds=300, solved=34,
                                     ratings_good=2, ratings_bad=1, aging=1, pto=1, sick=2, failures={},
                                     items=PAT_CALL_ITEMS + PAT_RATING_ITEMS + PAT_AGING_ITEMS + PAT_DAYS_OUT)
    assert sam_prep.tech == TechPrep(SAM, calls=1, declined_missed=1, unreachable=0, talk_seconds=200, solved=12,
                                     ratings_good=1, ratings_bad=0, aging=0, pto=0, sick=0, failures={},
                                     items=SAM_ITEMS)


def test_fixed_copy():
    assert live.NOT_COLLECTED == {"calls": "Calls aren't collected for this whole period.",
                                  "solved": "Solved tickets aren't collected for this whole period.",
                                  "satisfaction": "Ratings aren't collected for this whole period."}
    assert live.NO_AGING == "Not recorded then: aging tickets are recorded each day from {day}."


def test_nothing_wanted_counts_nothing(db):
    assert count_preps(db, wanted=[]) == []


def test_not_found_without_agent(db):
    """A tech the sync never found in Zendesk has no stored agent id: every column is a dash with its reason, and the
    techs counted with them are counted as before."""
    set_setting(db, "team", [PAT, GONE])
    collect(db)
    pat, gone = count_preps(db, wanted=[(PAT, DAY), (GONE, DAY)])
    assert pat.tech.calls == 1 and pat.tech.failures == {}
    reason = "Not found in Zendesk."
    assert gone.tech == TechPrep(GONE, calls=None, declined_missed=None, unreachable=None, talk_seconds=None,
                                 solved=None, ratings_good=None, ratings_bad=None, aging=None, pto=None, sick=None,
                                 failures={k: reason for k in ("calls", "solved", "satisfaction", "aging",
                                                               "attendance")},
                                 items=())
    assert (gone.queue, gone.failures) == (3, {})


def test_calls_not_collected_for_the_whole_period(db):
    """The calls read failed after 23:00 on the period's last day (review focus 5): its legs are current only to
    then, so the calls and the queue say so, as a failed Talk read did, and every other column is counted."""
    collect(db)
    collected.save_state(db, "legs", through=WINDOW_END - timedelta(hours=1), error="Zendesk could not be reached.")
    reason = "Calls aren't collected for this whole period."
    pat_prep, sam_prep = count_preps(db)
    assert pat_prep.queue is None and pat_prep.failures == {"calls": reason, "queue": reason}
    pat, sam = pat_prep.tech, sam_prep.tech
    assert (pat.calls, pat.declined_missed, pat.unreachable, pat.talk_seconds) == (None, None, None, None)
    assert pat.failures == {"calls": reason} and sam.failures == {"calls": reason}
    assert (pat.solved, pat.ratings_good, pat.ratings_bad, pat.aging, pat.pto, pat.sick) == (34, 2, 1, 1, 1, 2)
    assert pat.items == PAT_RATING_ITEMS + PAT_AGING_ITEMS + PAT_DAYS_OUT


def test_uncovered_columns(db):
    """Calls are collected over the whole period and ratings only from two days into it (the history read-back hasn't
    reached back yet): satisfaction says so, and the calls are counted."""
    collect(db)
    collected.save_state(db, "ratings", covered_from=WINDOW_START + timedelta(days=2))
    pat_prep, sam_prep = count_preps(db)
    reason = "Ratings aren't collected for this whole period."
    assert (pat_prep.queue, pat_prep.failures) == (3, {})
    pat, sam = pat_prep.tech, sam_prep.tech
    assert (pat.ratings_good, pat.ratings_bad) == (None, None) and pat.failures == {"satisfaction": reason}
    assert sam.failures == {"satisfaction": reason}
    assert (pat.calls, pat.talk_seconds, pat.solved, pat.aging) == (1, 300, 34, 1)
    assert pat.items == PAT_CALL_ITEMS + PAT_AGING_ITEMS + PAT_DAYS_OUT


def test_solved_not_collected_for_the_whole_period(db):
    collect(db)
    collected.save_state(db, "tickets", through=WINDOW_END - timedelta(hours=2))
    reason = "Solved tickets aren't collected for this whole period."
    pat_prep, sam_prep = count_preps(db)
    assert (pat_prep.queue, pat_prep.failures) == (3, {})
    for tech in (pat_prep.tech, sam_prep.tech):
        assert tech.solved is None and tech.failures == {"solved": reason}
    assert (pat_prep.tech.calls, pat_prep.tech.ratings_good) == (1, 2)


def test_unmatched_legs_fail_call_columns(db):
    orphans = [leg(20, 900, PAT_ID, at=datetime(2026, 10, 12, 15, tzinfo=UTC)),
               leg(21, 901, OTHER_ID, at=datetime(2026, 10, 12, 16, tzinfo=UTC))]
    collect(db, legs=LEGS + orphans)
    pat_prep, sam_prep = count_preps(db)
    reason = "2 call legs had no matching call record."
    assert pat_prep.queue is None and pat_prep.failures == {"calls": reason, "queue": reason}
    pat, sam = pat_prep.tech, sam_prep.tech
    assert pat.calls is None and pat.talk_seconds is None and pat.failures == {"calls": reason}
    assert sam.calls is None and sam.failures == {"calls": reason}
    assert pat.solved == 34 and pat.aging == 1
    assert all(item.kind not in CALL_KINDS for item in pat.items)


def test_one_unmatched_leg_is_named_singular(db):
    collect(db, legs=LEGS + [leg(20, 900, PAT_ID, at=datetime(2026, 10, 12, 15, tzinfo=UTC))])
    reason = "1 call leg had no matching call record."
    assert count_preps(db)[0].failures == {"calls": reason, "queue": reason}


def test_unmatched_leg_clears_when_call_arrives(db):
    """A leg stored before its call record leaves the calls uncounted; once a later sync stores the call, the next
    count has it, with nothing to clear by hand."""
    collect(db, legs=LEGS + [leg(20, 900, PAT_ID, talk=60, at=datetime(2026, 10, 12, 15, tzinfo=UTC))])
    assert count_preps(db)[0].tech.calls is None

    collected.upsert_calls(db, [call(900, 5900)])
    pat_prep = count_preps(db)[0]
    assert (pat_prep.queue, pat_prep.failures) == (4, {})
    assert (pat_prep.tech.calls, pat_prep.tech.talk_seconds, pat_prep.tech.failures) == (2, 360, {})
    assert [i.ticket_id for i in pat_prep.tech.items if i.kind == "answered"] == [5001, 5900]


def test_repeated_rows_are_counted_once(db):
    """The incremental exports can repeat a row across page edges: it is stored once, by id, and counted once."""
    repeat = dict(LEGS[0], updated_at="2026-10-08T00:00:00Z")
    collect(db, legs=LEGS + [repeat], calls=CALLS + [CALLS[0]])
    pat_prep = count_preps(db)[0]
    assert pat_prep.queue == 3 and pat_prep.failures == {}
    assert (pat_prep.tech.calls, pat_prep.tech.talk_seconds) == (1, 300)
    assert [i.kind for i in pat_prep.tech.items].count("answered") == 1


def test_rating_items_with_reason_and_comment(db):
    ratings = [
        rating(11, PAT_ID, "bad", at=datetime(2026, 10, 8, 15, tzinfo=UTC), ticket_id=7101,
               reason="Issue not resolved", comment="  Waited an hour.  "),
        rating(12, PAT_ID, "good", at=datetime(2026, 10, 9, 15, tzinfo=UTC), ticket_id=7102,
               reason="No reason provided", comment="   "),
        {"id": 13, "assignee_id": PAT_ID, "score": "good", "ticket_id": 7103,     # no reason or comment at all
         "created_at": "2026-10-10T15:00:00Z"},
        rating(14, PAT_ID, "good", at=datetime(2026, 10, 6, 4, tzinfo=UTC), ticket_id=7104),  # 10/5 local: before
        rating(15, PAT_ID, "offered", at=datetime(2026, 10, 11, 15, tzinfo=UTC), ticket_id=7105),
        rating(16, SAM_ID, "bad", at=datetime(2026, 10, 9, 16, tzinfo=UTC), ticket_id=7106, reason="",
               comment="Rude."),
    ]
    collect(db, ratings=ratings)
    pat, sam = techs(db)
    assert (pat.ratings_good, pat.ratings_bad) == (2, 1)
    assert (sam.ratings_good, sam.ratings_bad) == (0, 1)
    assert [i for i in pat.items if i.kind == "rating"] == [
        PrepItem("rating", 7101, "2026-10-08T15:00:00Z", None, None, score="bad", reason="Issue not resolved",
                 comment="Waited an hour."),
        PrepItem("rating", 7102, "2026-10-09T15:00:00Z", None, None, score="good"),
        PrepItem("rating", 7103, "2026-10-10T15:00:00Z", None, None, score="good"),
    ]
    assert [i for i in sam.items if i.kind == "rating"] == [
        PrepItem("rating", 7106, "2026-10-09T16:00:00Z", None, None, score="bad", comment="Rude.")]


def test_answered_calls_carry_priority_and_tags(db):
    collect(db, tickets=[{"id": 5001, "priority": "urgent", "tags": ["pos", "vip"]},
                         {"id": 5002, "priority": None, "tags": []}])
    pat, sam = techs(db)
    assert [i for i in pat.items if i.kind in CALL_KINDS] == [
        PrepItem("answered", 5001, "2026-10-07T15:00:00Z", 300, None, priority="urgent", tags=("pos", "vip")),
        PrepItem("declined", 5002, "2026-10-08T15:00:00Z", 0, None),
        PrepItem("unreachable", 5005, "2026-10-09T16:00:00Z", 0, None),
    ]
    assert [i for i in sam.items if i.kind in CALL_KINDS] == [
        PrepItem("answered", 5002, "2026-10-08T15:01:00Z", 200, None),
        PrepItem("missed", 5004, "2026-10-09T15:00:00Z", 0, None),
    ]


def test_a_ticket_never_collected_has_no_priority(db):
    collect(db, tickets=[TICKETS[1]])
    pat = techs(db)[0]
    assert [i for i in pat.items if i.kind == "answered"] == [PrepItem("answered", 5001, "2026-10-07T15:00:00Z", 300,
                                                                       None)]


def test_unsynced_activity_blanks_attendance_with_reason(db):
    db.execute("DELETE FROM activity_sync")
    db.commit()
    collect(db)
    pat, sam = techs(db)
    reason = "Team activity isn't synced for this whole period."
    for tech in (pat, sam):
        assert tech.pto is None and tech.sick is None and tech.failures == {"attendance": reason}
    assert pat.calls == 1 and pat.solved == 34 and pat.aging == 1
    assert all(i.kind not in ("pto", "sick") for i in pat.items)


def test_attendance_before_team_activity_start_is_a_dash(db):
    """A period that starts before Team activity's first day gets no partial count (review focus 3)."""
    db.execute("DELETE FROM activity_sync")
    sync_activity(db, window_start="2026-07-21T05:00:00Z")     # local midnight starting 7/21
    early, first_full = date(2026, 7, 28), date(2026, 8, 4)     # 7/14 - 7/27, and 7/21 - 8/3
    tech, counted = techs(db, wanted=[(PAT, early), (PAT, first_full)])
    assert tech.pto is None and tech.sick is None
    assert tech.failures["attendance"] == "Team activity starts Jul 21"
    assert all(i.kind not in ("pto", "sick") for i in tech.items)
    assert (counted.pto, counted.sick) == (0, 0) and "attendance" not in counted.failures


def test_many_periods_each_counted_from_the_stored_rows(db):
    """3 techs x 4 days, Kim's periods overlapping the others' by a week: each period counts its own calls and queue,
    and the preps come oldest day first, each day's techs as wanted."""
    set_setting(db, "team", [PAT, SAM, KIM])
    shared_days = [date(2026, 9, 8), date(2026, 9, 22), date(2026, 10, 6), DAY]
    kim_days = [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 13)]
    agents = {PAT: PAT_ID, SAM: SAM_ID, KIM: KIM_ID}
    collected.set_agents(db, agents)
    plan = [(PAT, d) for d in shared_days] + [(SAM, d) for d in shared_days] + [(KIM, d) for d in kim_days]
    legs, calls = [], []
    for n, (email, day) in enumerate(plan, 1):     # one answered call each, on the second day of the period
        legs.append(leg(n, 1000 + n, agents[email], talk=10 * n, at=at(period_for(day).start + timedelta(days=1), 10)))
        calls.append(call(1000 + n, 8000 + n))
    collect(db, legs=legs, calls=calls, tickets=[], ratings=[], solves=[], aging=False)

    counted = count_preps(db, wanted=plan)
    days = sorted(shared_days + kim_days)
    assert [(c.tech.email, c.period.day) for c in counted] == [
        (email, day) for day in days for email in ((PAT, SAM) if day in shared_days else (KIM,))]
    found = {(c.tech.email, c.period.day): c for c in counted}
    for n, (email, day) in enumerate(plan, 1):
        tech = found[email, day].tech
        assert (tech.calls, tech.talk_seconds, tech.queue) == (1, 10 * n, None), (email, day)
    assert {c.period.day: c.queue for c in counted} == {**{day: 3 for day in days}, DAY: 2}


def spy_reads(monkeypatch, *names) -> list[tuple]:
    """Each read of `names` (`collected`'s) made from here on, as (name, start, end)."""
    reads = []

    def spying(name):
        real = getattr(collected, name)

        def spy(conn, start, end, *rest):
            reads.append((name, start, end))
            return real(conn, start, end, *rest)
        monkeypatch.setattr(collected, name, spy)

    for name in names:
        spying(name)
    return reads


def test_each_kind_is_read_once_for_every_period(db, monkeypatch):
    """3 techs x 4 days, Kim's on Manila's clock: every period is cut from one read of each collected kind over the span
    all their windows lie in, from Manila's midnight starting Kim's first period (Aug 18) to Chicago's ending the
    latest (Oct 19), and counts what reading its own window alone counts. (Attendance reads each tech's Team activity
    once, as the grid judges its days: `attendances`.)"""
    set_setting(db, "team", [PAT, SAM, KIM])
    apac(db, KIM)
    agents = {PAT: PAT_ID, SAM: SAM_ID, KIM: KIM_ID}
    collected.set_agents(db, agents)
    shared_days = [date(2026, 9, 8), date(2026, 9, 22), date(2026, 10, 6), DAY]
    kim_days = [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 13)]
    plan = [(PAT, d) for d in shared_days] + [(SAM, d) for d in shared_days] + [(KIM, d) for d in kim_days]
    legs, calls, ratings, solves = [], [], [], []
    for n, (email, day) in enumerate(plan, 1):     # a call, a rating and a solve each, inside the period
        start = period_for(day).start
        legs.append(leg(n, 1000 + n, agents[email], talk=10 * n, at=at(start + timedelta(days=1), 10)))
        calls.append(call(1000 + n, 8000 + n))
        ratings.append(rating(n, agents[email], "good" if n % 2 else "bad", at=at(start + timedelta(days=2), 10),
                              ticket_id=7000 + n))
        solves.append(solve(20000 + n, agents[email], at(start + timedelta(days=3), 10)))
    collect(db, legs=legs, calls=calls, tickets=[], ratings=ratings, solves=solves, aging=False)
    alone = [count_preps(db, wanted=[pair])[0] for pair in plan]

    reads = spy_reads(monkeypatch, "talk_between", "ratings_between", "solved_by")
    counted = count_preps(db, wanted=plan)
    span = (z(8, 17, 16), WINDOW_END)
    assert sorted(reads) == [("ratings_between", *span), ("solved_by", *span), ("talk_between", *span)]
    found = {(c.tech.email, c.period.day): c for c in counted}
    assert [found[pair] for pair in plan] == alone
    assert [(c.tech.calls, c.tech.solved, c.tech.ratings_good + c.tech.ratings_bad) for c in alone] == [(1, 1, 1)] * 12


def test_items_only_for_the_preps_that_show_them(db):
    """`items_for` names the preps whose lists are made (tech_view's picked period): every other prep has the same
    numbers and no items, and the one named has the items counting every prep's gives it (T-168)."""
    collect(db)
    every = count_preps(db)
    bare = [replace(c, tech=replace(c.tech, items=())) for c in every]
    assert every[0].tech.items and every[1].tech.items, "both have lists to make"
    assert live.count_preps(db, list(BOTH), now=NOW, items_for=set()) == bare
    assert live.count_preps(db, list(BOTH), now=NOW, items_for={(PAT, DAY)}) == [every[0], bare[1]]


def test_counting_again_gives_the_same_answer_and_new_data_at_once(db):
    """Nothing counted is kept from one count to the next: counting again gives the same preps, and a call, a rating
    and a solved ticket stored since show in the next count, the call in the queue of everyone counted with it
    (T-168)."""
    collect(db)
    first = count_preps(db)
    assert count_preps(db) == first
    collected.upsert_legs(db, [leg(50, 150, PAT_ID, talk=60, at=datetime(2026, 10, 16, 15, tzinfo=UTC))])
    collected.upsert_calls(db, [call(150, 5050)])
    collected.upsert_ratings(db, [rating(50, PAT_ID, "good", at=datetime(2026, 10, 16, 16, tzinfo=UTC),
                                         ticket_id=7050)])
    ticket, metrics = solve(50050, PAT_ID, at(date(2026, 10, 16), 10))
    collected.upsert_ticket_states(db, [ticket], [metrics])
    pat, sam = count_preps(db)
    before = first[0].tech
    assert (pat.tech.calls, pat.tech.talk_seconds, pat.tech.ratings_good, pat.tech.solved) == (
        before.calls + 1, before.talk_seconds + 60, before.ratings_good + 1, before.solved + 1)
    assert pat.queue == sam.queue == first[0].queue + 1
    assert sam.tech == first[1].tech


# -- aging --------------------------------------------------------------------------------------------------------


def test_aging_from_record_and_before_recording(db):
    """A prep's aging is the tech's list recorded on its 1:1 day, or the latest before it; a 1:1 day before the
    tech's recording began says when it began, and a tech with nothing recorded yet says it begins today."""
    collect(db, aging=False)
    older = PrepItem("aging", 6001, "2026-10-01T15:00:00Z", None, "open", last_reply="2026-10-12T16:20:00Z")
    newer = PrepItem("aging", 6003, "2026-10-05T15:00:00Z", None, "pending")
    collected.record_aging(db, PAT, date(2026, 10, 15), [older], now=z(10, 15, 11))
    collected.record_aging(db, PAT, date(2026, 10, 21), [older, newer], now=z(10, 21, 11))

    before, latest, sam = techs(db, wanted=[(PAT, date(2026, 10, 6)), (PAT, DAY), (SAM, DAY)])
    assert latest.aging == 1 and latest.failures == {}
    assert [i for i in latest.items if i.kind == "aging"] == [older], "the list of Oct 15, the latest on or before"
    assert before.aging is None
    assert before.failures["aging"] == "Not recorded then: aging tickets are recorded each day from Oct 15."
    assert all(i.kind != "aging" for i in before.items)
    assert sam.aging is None
    assert sam.failures["aging"] == "Not recorded then: aging tickets are recorded each day from Oct 20."


# -- each tech's clock -------------------------------------------------------------------------------------------


SINCE = date(2026, 1, 1)                  # the regions below are in force from before every day counted
MANILA_START = z(10, 5, 16)               # Manila midnight starting Oct 6
MANILA_END = z(10, 19, 16)                # Manila midnight ending Oct 19


def answered(id, agent_id, at, talk=100):
    """An answered leg on its own inbound call (call id = leg id + 100, ticket = leg id + 5000)."""
    return leg(id, id + 100, agent_id, talk=talk, at=at)


def apac(conn, email=PAT):
    regions.set_regions(conn, email, [(SINCE, "APAC")])


def test_an_apac_techs_prep_counts_their_manila_days(db):
    """Pat (APAC) is counted over Manila's Oct 6 to Oct 19, with Manila's queue as their own; Sam, with no region, on
    Chicago's days, with the period's queue. A call at 01:00 Oct 6 Manila is in and one at 02:00 Oct 20 Manila (13:00
    Oct 19 in Chicago) is out, ratings and solved tickets the same."""
    apac(db)
    legs = [
        answered(1, PAT_ID, z(10, 5, 17), talk=300),     # 01:00 Oct 6 Manila; 12:00 Oct 5 Chicago
        answered(2, PAT_ID, z(10, 19, 18), talk=60),     # 02:00 Oct 20 Manila (the 1:1 day); 13:00 Oct 19 Chicago
        answered(3, PAT_ID, z(10, 7, 20), talk=200),     # 04:00 Oct 8 Manila; 15:00 Oct 7 Chicago
        leg(4, 104, PAT_ID, status="agent_declined", talk=0, at=z(10, 19, 15, 59)),   # 23:59 Oct 19 Manila
        answered(5, OTHER_ID, z(10, 5, 18)),              # Manila's period only: the APAC queue
        answered(7, SAM_ID, z(10, 8, 15)),                # both periods
    ]
    ratings = [rating(1, PAT_ID, "good", at=z(10, 5, 17, 30), ticket_id=7001),    # 01:30 Oct 6 Manila
               rating(2, PAT_ID, "bad", at=z(10, 19, 17), ticket_id=7002),        # 01:00 Oct 20 Manila
               rating(3, PAT_ID, "good", at=z(10, 12, 18), ticket_id=7003)]       # 02:00 Oct 13 Manila
    solves = [solve(1, PAT_ID, MANILA_START), solve(2, PAT_ID, MANILA_START - timedelta(minutes=1)),
              solve(3, PAT_ID, MANILA_END - timedelta(minutes=1)), solve(4, PAT_ID, MANILA_END),
              solve(5, SAM_ID, WINDOW_END - timedelta(minutes=1)), solve(6, SAM_ID, z(10, 5, 20))]
    collect(db, legs=legs, calls=[call(i + 100, i + 5000) for i in (1, 2, 3, 4, 5, 7)], tickets=[], ratings=ratings,
            solves=solves)

    pat_prep, sam_prep = count_preps(db)
    pat, sam = pat_prep.tech, sam_prep.tech
    assert (pat.calls, pat.declined_missed, pat.talk_seconds, pat.queue) == (2, 1, 500, 4)
    assert pat_prep.queue is None, "counted on another clock than the app's: the queue is the row's own"
    assert (pat.ratings_good, pat.ratings_bad, pat.solved) == (2, 0, 2)
    assert [(i.kind, i.ticket_id) for i in pat.items if i.kind in CALL_KINDS] == [
        ("answered", 5001), ("answered", 5003), ("declined", 5004)]
    assert [i.ticket_id for i in pat.items if i.kind == "rating"] == [7001, 7003]
    assert (sam.calls, sam.talk_seconds, sam.queue, sam.solved) == (1, 100, None, 1)
    assert sam_prep.queue == 3, "Chicago's period: legs 2, 3 and 7"


def test_region_change_inside_period(db):
    """Pat serves NASA until Oct 12 and APAC from Oct 13: Oct 6-12 are Chicago days and Oct 13-19 Manila days. A leg,
    a rating and a solve on each side of the change fall on the days `period_days` gives: one in both Oct 12's Chicago
    window and Oct 13's Manila window counts on the later day, and one outside the period on neither."""
    regions.set_regions(db, PAT, [(SINCE, "NASA"), (date(2026, 10, 13), "APAC")])
    zone_on = regions.day_zones(db, PAT)
    leg_times = [z(10, 5, 20),    # 15:00 Oct 5 Chicago: before
                 z(10, 8, 15),    # 10:00 Oct 8 Chicago
                 z(10, 12, 20),   # 15:00 Oct 12 Chicago and 04:00 Oct 13 Manila: the later
                 z(10, 15, 2),    # 10:00 Oct 15 Manila
                 z(10, 19, 20)]   # 04:00 Oct 20 Manila: the 1:1 day
    rating_times = [z(10, 9, 15), z(10, 14, 2), z(10, 19, 17)]                 # Oct 9 Chicago; Oct 14, Oct 20 Manila
    solve_times = [z(10, 6, 4), z(10, 7, 15), z(10, 12, 18), z(10, 16, 3)]     # Oct 5, Oct 7 Chicago; Oct 13, 16 Manila
    assert live.period_days(leg_times, PERIOD, zone_on) == [None, date(2026, 10, 8), date(2026, 10, 13),
                                                            date(2026, 10, 15), None]
    assert live.period_days(rating_times, PERIOD, zone_on) == [date(2026, 10, 9), date(2026, 10, 14), None]
    assert live.period_days(solve_times, PERIOD, zone_on) == [None, date(2026, 10, 7), date(2026, 10, 13),
                                                              date(2026, 10, 16)]

    legs = [answered(n, PAT_ID, moment, talk=100 * n) for n, moment in enumerate(leg_times, 1)]
    ratings = [rating(n, PAT_ID, "good", at=moment, ticket_id=7000 + n) for n, moment in enumerate(rating_times, 1)]
    solves = [solve(n, PAT_ID, moment) for n, moment in enumerate(solve_times, 1)]
    collect(db, legs=legs, calls=[call(n + 100, n + 5000) for n in range(1, 6)], tickets=[], ratings=ratings,
            solves=solves, aging=False)

    pat_prep = count_preps(db, wanted=[(PAT, DAY)])[0]
    pat = pat_prep.tech
    assert (pat.calls, pat.talk_seconds, pat.queue, pat_prep.queue) == (3, 900, 3, None)
    assert [(i.ticket_id, i.at) for i in pat.items if i.kind == "answered"] == [
        (5002, "2026-10-08T15:00:00Z"), (5003, "2026-10-12T20:00:00Z"), (5004, "2026-10-15T02:00:00Z")]
    assert [i.ticket_id for i in pat.items if i.kind == "rating"] == [7001, 7002]
    assert pat.solved == 3


def test_a_moment_between_two_clocks_counts_on_no_day(db):
    """Pat serves APAC until Oct 12 and NASA from Oct 13: Manila's Oct 12 ends at 16:00Z Oct 12 and Chicago's Oct 13
    begins at 05:00Z Oct 13. A call, a rating and a solve at 20:00Z Oct 12, inside the period's window, fall in neither
    day and count on none, as Team activity counts them."""
    regions.set_regions(db, PAT, [(SINCE, "APAC"), (date(2026, 10, 13), "NASA")])
    gap, kept = z(10, 12, 20), z(10, 8, 3)                      # 04:00 Oct 13 Manila / 15:00 Oct 12 Chicago; Oct 8
    assert live.period_days([gap, kept], PERIOD, regions.day_zones(db, PAT)) == [None, date(2026, 10, 8)]
    collect(db, legs=[answered(1, PAT_ID, gap), answered(2, PAT_ID, kept)], calls=[call(101, 5001), call(102, 5002)],
            tickets=[], ratings=[rating(1, PAT_ID, "bad", at=gap, ticket_id=7001),
                                 rating(2, PAT_ID, "good", at=kept, ticket_id=7002)],
            solves=[solve(1, PAT_ID, gap), solve(2, PAT_ID, kept)], aging=False)
    pat = techs(db, wanted=[(PAT, DAY)])[0]
    assert (pat.calls, pat.queue, pat.ratings_good, pat.ratings_bad, pat.solved) == (1, 1, 1, 0, 1)


def test_counting_passes_now_to_attendance(db, monkeypatch):
    seen = []

    def spy(conn, email, periods, *, today, now=None):
        seen.append((email, today, now))
        return attendances(conn, email, periods, today=today, now=now)

    monkeypatch.setattr(live, "attendances", spy)
    collect(db)
    count_preps(db)
    assert seen == [(PAT, DAY, NOW), (SAM, DAY, NOW)]
