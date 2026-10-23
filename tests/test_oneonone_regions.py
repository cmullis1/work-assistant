"""Regions (design 2026-10-21, plan Task 5): 1:1 Prep counts each tech's 14 days on their region's clock. An APAC
tech's period is 14 Manila days, each midnight to midnight in Manila: their calls, ratings and attendance are counted
over it, and so is the department-wide call count their "% of calls" is over, which is their own. A NASA and an APAC
tech who share a 1:1 day each get their own count, and the lists behind a prep show the tech's own days. Every prep is
counted from the collected data when asked (`oneonone.live`, whose tests count the numbers on each clock); these
check what the 1:1 Prep page shows from them.

DAY is Tue Oct 20 2026; the period is Oct 6 to Oct 19. Chicago is on CDT (UTC-5): its period runs 05:00Z Oct 6 to
05:00Z Oct 20. Manila is UTC+8: its period runs 16:00Z Oct 5 to 16:00Z Oct 19."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from test_oneonone_live import (DAY, EVERY_OTHER_TUESDAY, NOW, OTHER_ID, PAT, PAT_ID, PERIOD, PTO_DAY, SAM, SAM_ID,
                                SICK_DAY, TZ, answered, apac, call, collect, cover, leg, rating, sync_activity, z)

from work_assistant import regions, rubric
from work_assistant.activity import collected
from work_assistant.activity import store as activity_store
from work_assistant.activity.model import Action, ShiftVersion
from work_assistant.oneonone import report
from work_assistant.oneonone.attendance import attendance
from work_assistant.oneonone.cycle import period_for
from work_assistant.settings import set_setting

UTC = timezone.utc
SINCE = date(2026, 1, 1)                  # the regions below are in force from before every day counted
MON_FRI = frozenset({1, 2, 3, 4, 5})      # Sunday = 0

LEGS = [
    answered(1, PAT_ID, z(10, 5, 17), talk=300),     # 01:00 Oct 6 Manila; 12:00 Oct 5 Chicago
    answered(2, PAT_ID, z(10, 19, 18), talk=60),     # 02:00 Oct 20 Manila (the 1:1 day); 13:00 Oct 19 Chicago
    answered(3, PAT_ID, z(10, 7, 20), talk=200),     # 04:00 Oct 8 Manila; 15:00 Oct 7 Chicago
    leg(4, 104, PAT_ID, status="agent_declined", talk=0, at=z(10, 19, 15, 59)),   # 23:59 Oct 19 Manila
    answered(5, OTHER_ID, z(10, 5, 18)),              # Manila's period only: the APAC queue
    answered(7, SAM_ID, z(10, 8, 15)),                # both periods
]
CALLS = [call(i + 100, i + 5000) for i in (1, 2, 3, 4, 5, 7)]
RATINGS = [rating(1, PAT_ID, "good", at=z(10, 5, 17, 30), ticket_id=7001),    # 01:30 Oct 6 Manila
           rating(2, PAT_ID, "bad", at=z(10, 19, 17), ticket_id=7002),        # 01:00 Oct 20 Manila
           rating(3, PAT_ID, "good", at=z(10, 12, 18), ticket_id=7003)]       # 02:00 Oct 13 Manila


@pytest.fixture
def db(conn):
    """Pat and Sam meet every other Tuesday from Oct 20; every kind is collected from Aug 1 to NOW."""
    rubric.ensure_seeded(conn)
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [PAT, SAM])
    set_setting(conn, "oneonone_schedule", {PAT: EVERY_OTHER_TUESDAY, SAM: EVERY_OTHER_TUESDAY})
    sync_activity(conn)
    collected.set_agents(conn, {PAT: PAT_ID, SAM: SAM_ID})
    cover(conn)
    return conn


def shares(conn) -> dict[str, str]:
    """Each tech's "% of calls" on the 1:1 prep page."""
    rows = report.latest_view(conn, today=DAY, now=NOW)["rows"]
    return {row["email"]: row["cells"]["share"]["text"] for row in rows}


def behind(conn, email=PAT) -> dict:
    return report.tech_view(conn, email, DAY, today=DAY, now=NOW)["behind"]


# -- an APAC tech's prep -------------------------------------------------------------------------------------


def test_an_apac_techs_prep_shows_their_manila_days(db):
    """Pat (APAC) is counted over Manila's Oct 6 to Oct 19: a call at 01:00 Oct 6 Manila is in and one at 02:00 Oct 20
    Manila (13:00 Oct 19 in Chicago) is out, ratings the same, and the lists behind show Manila days. Sam, with no
    region, is counted on Chicago's days as before."""
    apac(db)
    collect(db, legs=LEGS, calls=CALLS, tickets=[], ratings=RATINGS, solves=[])
    assert shares(db) == {PAT: "50.00%", SAM: "33.33%"}       # 2 of 4 in Manila; 1 of 3 in Chicago
    shown = behind(db)
    assert [(c["ticket_id"], c["day"]) for c in shown["answered"]] == [(5001, "10/6"), (5003, "10/8")]
    assert [(r["ticket_id"], r["day"]) for r in shown["ratings"]] == [(7001, "10/6"), (7003, "10/13")]
    assert [(a["ticket_id"], a["last_reply"], a["reply_days_ago"]) for a in shown["aging"]] == [(6001, "10/13", 7)]
    assert [c["day"] for c in behind(db, SAM)["answered"]] == ["10/8"]


def test_an_apac_prep_whose_calls_arent_collected_still_shows_manila_days(db):
    """With the calls not collected for the whole period, the ratings still show on the tech's clock."""
    apac(db)
    collect(db, legs=LEGS, calls=CALLS, tickets=[], ratings=RATINGS, solves=[])
    collected.save_state(db, "legs", through=datetime(2026, 10, 19, 12, tzinfo=UTC))
    shown = behind(db)
    assert shown["answered"] == [] and [r["day"] for r in shown["ratings"]] == ["10/6", "10/13"]


def test_a_nasa_and_an_apac_tech_on_one_day_each_get_their_own_queue(db):
    """Pat (APAC) and Sam share Oct 20: Pat's % of calls is over Manila's count, Sam's over Chicago's, and a call that
    only one clock's period holds changes only that tech's (review focus 5)."""
    apac(db)
    collect(db, legs=LEGS, calls=CALLS, tickets=[], ratings=RATINGS, solves=[])
    assert shares(db) == {PAT: "50.00%", SAM: "33.33%"}

    manila_only = answered(8, OTHER_ID, z(10, 5, 19))          # 03:00 Oct 6 Manila; 14:00 Oct 5 Chicago
    collect(db, legs=[manila_only], calls=[call(108, 5008)], tickets=[], ratings=[], solves=[])
    assert shares(db) == {PAT: "40.00%", SAM: "33.33%"}
    chicago_only = answered(9, OTHER_ID, z(10, 20, 3))         # 22:00 Oct 19 Chicago; 11:00 Oct 20 Manila
    collect(db, legs=[chicago_only], calls=[call(109, 5009)], tickets=[], ratings=[], solves=[])
    assert shares(db) == {PAT: "40.00%", SAM: "25.00%"}

    past = report.tech_view(db, PAT, DAY, today=DAY, now=NOW)["past"]
    assert past[0]["cells"]["share"]["text"] == "40.00%"
    trend = next(m for m in report.trends_view(db, PAT, 4, today=DAY, now=NOW)["measures"] if m["key"] == "share")
    assert (trend["values"][-1], trend["team"][-1]) == (40.0, 32.5)


def test_a_region_change_inside_a_period_shows_each_day_on_its_own_clock(db):
    """Pat serves NASA until Oct 12 and APAC from Oct 13: Oct 6-12 are Chicago days and Oct 13-19 Manila days. A call
    at 15:00 Oct 5 Chicago (04:00 Oct 6 Manila, a Chicago day) is out, and one at 04:00 Oct 20 Manila (15:00 Oct 19
    Chicago, a Manila day) is out; one at 15:00 Oct 12 Chicago, in both days' windows, shows on the later, Oct 13."""
    regions.set_regions(db, PAT, [(SINCE, "NASA"), (date(2026, 10, 13), "APAC")])
    legs = [answered(1, PAT_ID, z(10, 5, 20)), answered(2, PAT_ID, z(10, 19, 20)),
            answered(3, PAT_ID, z(10, 12, 20), talk=300), answered(4, PAT_ID, z(10, 8, 15), talk=200)]
    collect(db, legs=legs, calls=[call(n + 100, n + 5000) for n in range(1, 5)], tickets=[], ratings=[], solves=[],
            aging=False)
    cells = report.latest_view(db, today=DAY, now=NOW)["rows"][0]["cells"]
    assert (cells["calls"]["text"], cells["talk"]["text"]) == ("2", "0:08:20")
    assert [(c["ticket_id"], c["day"]) for c in behind(db)["answered"]] == [(5004, "10/8"), (5003, "10/13")]


# -- attendance ----------------------------------------------------------------------------------------------


def manila_9_30(day: date) -> datetime:
    """09:30 in Manila on `day` (20:30 the evening before in Chicago)."""
    return datetime(day.year, day.month, day.day, 1, 30, tzinfo=UTC)


def test_attendance_reads_manila_days(conn):
    """Pat (APAC) works Monday to Friday, 09:00 to 18:00 Manila, with a reply at 09:30 Manila every workday but Oct 13
    (PTO) and Oct 14 (nothing). On Manila's days that is one PTO day and one sick day; on Chicago's the replies fall
    on the evenings before. Activity synced to 04:00 Oct 20 Manila covers Manila's whole period, not Chicago's."""
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [PAT])
    sync_activity(conn, through=z(10, 19, 20), window_start="2026-09-01T00:00:00Z")
    activity_store.add_shift_version(conn, ShiftVersion(PAT, date(2026, 9, 1), MON_FRI, 540, 1080, True))
    activity_store.add_time_off(conn, PAT, PTO_DAY, PTO_DAY, "", "pto")
    day = PERIOD.start
    while day <= PERIOD.end:
        if day.weekday() < 5 and day not in (PTO_DAY, SICK_DAY):
            activity_store.insert_actions(conn, [Action(PAT, day.toordinal(), manila_9_30(day), "reply", None, None,
                                                        day.toordinal(), 0)])
        day += timedelta(days=1)
    chicago = attendance(conn, PAT, PERIOD, today=DAY, now=NOW)
    assert chicago.complete is False and chicago.pto == (PTO_DAY,)
    assert chicago.sick == (date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 16)), "Fridays and Oct 12: no reply"

    apac(conn)
    manila = attendance(conn, PAT, PERIOD, today=DAY, now=NOW)
    assert (manila.pto, manila.sick, manila.complete) == ((PTO_DAY,), (SICK_DAY,), True)


def test_attendance_takes_the_techs_today_from_now(conn):
    """At 09:00 Tue Oct 20 in Manila it is still Monday in Chicago: Pat's PTO on Tuesday is a day under way on Pat's
    clock, not one still to come, when `now` is given (review F3)."""
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [PAT])
    now = z(10, 20, 1)
    sync_activity(conn, through=now, window_start="2026-09-01T00:00:00Z")
    activity_store.add_shift_version(conn, ShiftVersion(PAT, date(2026, 9, 1), MON_FRI, 540, 1080, True))
    tuesday = date(2026, 10, 20)
    activity_store.add_time_off(conn, PAT, tuesday, tuesday, "", "pto")
    apac(conn)
    period = period_for(date(2026, 10, 21))
    monday = date(2026, 10, 19)
    assert attendance(conn, PAT, period, today=monday).pto == ()
    assert attendance(conn, PAT, period, today=monday, now=now).pto == (tuesday,)
