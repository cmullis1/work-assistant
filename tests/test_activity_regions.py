"""Regions (design 2026-10-21, plan Tasks 2 and 3): Team activity and its Trends judge each tech's days on their
region's clock. An APAC tech's day runs midnight to midnight in Manila, their shift hours, flags and times are Manila's,
and so is their today; a tech with no region is judged on the app's own zone, exactly as before. At a region change each
day keeps its own clock's window: an instant in two days' windows counts on the later day, one in neither counts on
none (Ruling 4). Trends compares one tech with their team lead's team (T-144): on the PC, with every tech.

PAT and SAM work Monday to Friday, 09:00 to 18:00 on their own clock, from Sep 1. The app's zone is Chicago (CDT,
UTC-5, until Sun Nov 1); Manila is UTC+8 all year."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_activity_sync import PAT, PAT_ID, SAM, SAM_ID, FakeClient, page, run, stamp
from test_web_scoping import (  # noqa: F401 (fixtures: pytest finds them by name here)
    AVERY, BLAKE, CASEY, WEEK, app, client_hosted_tl, db_path, started)

from work_assistant import regions
from work_assistant.activity import collected, report, store, trends
from work_assistant.activity import sync as activity_sync
from work_assistant.activity.model import Action, ShiftVersion, SignIn
from work_assistant.db import connect
from work_assistant.periods import Period
from work_assistant.settings import set_setting
from work_assistant.web.app import create_app

UTC = timezone.utc
MON_FRI = frozenset({1, 2, 3, 4, 5})
SINCE = date(2026, 1, 1)                 # the regions below are in force from before every day judged
TODAY = date(2026, 11, 6)                # after every day judged, unless a test says otherwise
REGION_ERROR = "The region must be NASA, EMEA or APAC."
_ids = count(1)


def z(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC moment in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def synced(conn, window_start="2026-08-01T00:00:00Z", through="2026-11-06T00:00:00Z"):
    """A complete sync that read from `window_start` up to `through`."""
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", (through, through, through, window_start))
    conn.commit()


def did(conn, email, *moments):
    """One reply at each moment, each on its own ticket."""
    store.insert_actions(conn, [Action(email, next(_ids), at, "reply", None, None, next(_ids), 1) for at in moments])


def apac(conn, email=PAT):
    regions.set_regions(conn, email, [(SINCE, "APAC")])


def solve(conn, ticket_id, at, assignee_id=PAT_ID):
    """The ticket stored as solved at `at` while assigned to `assignee_id`, as the sync's ticket read stores it."""
    collected.upsert_ticket_states(conn, [{"id": ticket_id, "assignee_id": assignee_id, "status": "solved"}],
                                   [{"ticket_id": ticket_id, "solved_at": stamp(at)}])


def custom(first: str, last: str) -> Period:
    return Period(date.fromisoformat(first), date.fromisoformat(last), "custom", f"{first} - {last}", False)


def cells_for(conn, email, first, last, *, today=TODAY, now=None) -> list[dict]:
    """The tech's row of the grid from `first` to `last`."""
    return report.range_view(conn, custom(first, last), email, today=today, now=now)["rows"][0]["cells"]


def day_times(conn, email, day, *, today=TODAY) -> list[str]:
    """The times the day view lists the tech's actions at on `day`."""
    return [a["at"] for a in report.day_view(conn, email, day, today=today)["actions"]]


def figures(body: dict, key: str) -> dict:
    """The period's figure for the measure `key` in a Trends answer: the team's, then each listed tech's, by email."""
    measure = next(m for m in body["measures"] if m["key"] == key)
    return {"team": measure["team"]["now"], **{t["email"]: t["now"] for t in measure["techs"]}}


@pytest.fixture
def db(conn):
    set_setting(conn, "timezone", "America/Chicago")
    set_setting(conn, "team", [PAT, SAM])
    for email in (PAT, SAM):
        store.add_shift_version(conn, ShiftVersion(email, date(2026, 9, 1), MON_FRI, 540, 1080, True))
    synced(conn)
    return conn


# -- an APAC tech's day -------------------------------------------------------------------------------------


def test_an_apac_day_is_a_manila_day(db):
    """Actions at 09:30 and 17:30 Monday in Manila (20:30 Sunday and 04:30 Monday in Chicago) are both Monday's; one at
    23:00 Sunday in Manila (10:00 Sunday in Chicago) is Sunday's. With no region they split the Chicago way."""
    did(db, PAT, z(10, 12, 1, 30), z(10, 12, 9, 30), z(10, 11, 15))
    assert [c["tickets"] for c in cells_for(db, PAT, "2026-10-11", "2026-10-12")] == [2, 1], "on Chicago's clock"
    apac(db)
    sunday, monday = cells_for(db, PAT, "2026-10-11", "2026-10-12")
    assert [sunday["tickets"], monday["tickets"]] == [1, 2]
    assert (monday["first"], monday["last"], monday["shift"]) == ("09:30", "17:30", {"start": "09:00", "end": "18:00"})
    assert day_times(db, PAT, date(2026, 10, 12)) == ["09:30", "17:30"]


def test_late_start_is_judged_on_the_techs_clock(db):
    """A first action at 10:45 in Manila on a 09:00 shift is 105 minutes late; on Chicago's clock the same moment is
    21:45 the evening before."""
    apac(db)
    did(db, PAT, z(10, 12, 2, 45), z(10, 12, 9, 50))      # 10:45 and 17:50 Monday in Manila
    monday = cells_for(db, PAT, "2026-10-12", "2026-10-12")[0]
    assert (monday["state"], monday["first"]) == ("worked", "10:45")
    assert {"kind": "late_start", "minutes": 105} in monday["flags"]
    assert not any(f["kind"] == "early_finish" for f in monday["flags"]), "17:50 is 10 minutes before the end"


def test_solved_on_the_techs_clock(db):
    """Solved is the tickets solved while assigned to the tech, on the day solved on their clock (T-158): a solve at
    17:30 Oct 20 UTC is 01:30 Oct 21 in Manila and 12:30 Oct 20 in Chicago. A ticket solved while assigned to someone
    else, or a status change to solved, counts for nothing; a tech with no stored Zendesk id has none."""
    did(db, PAT, z(10, 20, 15), z(10, 21, 15))
    store.insert_actions(db, [Action(PAT, 902, z(10, 20, 16), "status", "open", "solved", next(_ids), 1)])
    solve(db, 900, z(10, 20, 17, 30))
    solve(db, 901, z(10, 20, 18), assignee_id=SAM_ID)
    assert [c["solved"] for c in cells_for(db, PAT, "2026-10-20", "2026-10-21")] == [0, 0], "no stored id yet"
    collected.set_agents(db, {PAT: PAT_ID, SAM: SAM_ID})
    assert [c["solved"] for c in cells_for(db, PAT, "2026-10-20", "2026-10-21")] == [1, 0], "on Chicago's clock"
    apac(db)
    assert [c["solved"] for c in cells_for(db, PAT, "2026-10-20", "2026-10-21")] == [0, 1]
    assert report.day_view(db, PAT, date(2026, 10, 21), today=TODAY)["cell"]["solved"] == 1
    assert [f.solved for f in report.tech_day_facts(db, PAT, date(2026, 10, 20), date(2026, 10, 21),
                                                    today=TODAY)] == [0, 1]


def test_today_is_the_techs_own(db):
    """At 22:00 Monday in Chicago it is 11:00 Tuesday in Manila: the APAC tech's Tuesday is under way, judged on what
    had happened by the sync, while the app's today, and the grid's, is still Monday. Without `now` there is only the
    app's today."""
    apac(db)
    now = z(10, 13, 3)
    synced(db, through=stamp(now))
    did(db, PAT, z(10, 13, 1, 30))                         # 09:30 Tuesday in Manila
    monday = date(2026, 10, 12)
    assert [c["state"] for c in cells_for(db, PAT, "2026-10-12", "2026-10-13", today=monday)] == ["missed", "future"]
    assert [c["state"] for c in cells_for(db, PAT, "2026-10-12", "2026-10-13", today=monday, now=now)] == [
        "missed", "worked"]
    assert report.range_view(db, custom("2026-10-12", "2026-10-13"), PAT, today=monday, now=now)["today"] \
        == "2026-10-12"
    assert report.day_view(db, PAT, date(2026, 10, 13), today=monday, now=now)["cell"]["state"] == "worked"
    assert [f.state for f in report.tech_day_facts(db, PAT, date(2026, 10, 13), date(2026, 10, 13), today=monday,
                                                   now=now)] == ["worked"]


def test_a_manila_day_across_the_chicago_dst_change_is_24_hours(db):
    """Chicago's Sunday Nov 1 has 25 hours (its clocks go back at 02:00); a Manila Sunday is still midnight to midnight
    in Manila, 16:00 Oct 31 to 16:00 Nov 1 UTC, its first instant in and its last out."""
    apac(db)
    did(db, PAT, z(10, 31, 15, 59), z(10, 31, 16), z(11, 1, 15, 59), z(11, 1, 16))
    saturday, sunday, monday = cells_for(db, PAT, "2026-10-31", "2026-11-02")
    assert [saturday["tickets"], sunday["tickets"], monday["tickets"]] == [1, 2, 1]
    assert (sunday["first"], sunday["last"]) == ("00:00", "23:59")
    assert day_times(db, PAT, date(2026, 11, 1)) == ["00:00", "23:59"]


# -- a region change ----------------------------------------------------------------------------------------


def test_a_swap_from_nasa_to_apac_counts_the_hours_in_both_days_on_the_later(db):
    """Monday Oct 12 is a Chicago day (05:00 Oct 12 to 05:00 Oct 13 UTC) and Tuesday a Manila one (16:00 Oct 12 to
    16:00 Oct 13 UTC): an action in both counts on Tuesday, whether or not Tuesday is shown."""
    regions.set_regions(db, PAT, [(SINCE, "NASA"), (date(2026, 10, 13), "APAC")])
    did(db, PAT, z(10, 12, 15), z(10, 12, 20), z(10, 13, 10))  # 10:00 Mon Chicago; 04:00 Tue Manila; 18:00 Tue Manila
    monday, tuesday = cells_for(db, PAT, "2026-10-12", "2026-10-13")
    assert [monday["tickets"], tuesday["tickets"]] == [1, 2]
    assert (monday["first"], tuesday["first"], tuesday["last"]) == ("10:00", "04:00", "18:00")
    assert cells_for(db, PAT, "2026-10-12", "2026-10-12")[0]["tickets"] == 1
    assert day_times(db, PAT, date(2026, 10, 12)) == ["10:00"]
    assert day_times(db, PAT, date(2026, 10, 13)) == ["04:00", "18:00"]


def test_a_solve_in_both_days_of_a_swap_counts_once_on_the_later(db):
    """A ticket solved at 20:00 Oct 12 UTC, in Chicago's Monday and Manila's Tuesday, counts on Tuesday only, as an
    action there does (`report._on_days`)."""
    regions.set_regions(db, PAT, [(SINCE, "NASA"), (date(2026, 10, 13), "APAC")])
    collected.set_agents(db, {PAT: PAT_ID})
    solve(db, 910, z(10, 12, 20))
    assert [c["solved"] for c in cells_for(db, PAT, "2026-10-12", "2026-10-13")] == [0, 1]
    assert cells_for(db, PAT, "2026-10-12", "2026-10-12")[0]["solved"] == 0


def test_a_swap_from_apac_to_nasa_counts_the_hours_between_on_neither_day(db):
    """Monday Oct 12 is a Manila day (16:00 Oct 11 to 16:00 Oct 12 UTC) and Tuesday a Chicago one (05:00 Oct 13 to
    05:00 Oct 14 UTC): an action between the two is on neither."""
    regions.set_regions(db, PAT, [(SINCE, "APAC"), (date(2026, 10, 13), "NASA")])
    did(db, PAT, z(10, 12, 2), z(10, 12, 20), z(10, 13, 15))   # 10:00 Mon Manila; between; 10:00 Tue Chicago
    monday, tuesday = cells_for(db, PAT, "2026-10-12", "2026-10-13")
    assert [monday["tickets"], tuesday["tickets"]] == [1, 1]
    assert (monday["first"], tuesday["first"]) == ("10:00", "10:00")
    assert day_times(db, PAT, date(2026, 10, 12)) == ["10:00"]
    assert day_times(db, PAT, date(2026, 10, 13)) == ["10:00"]


def latest_window_holding(at: datetime, days: list[date], zone_on) -> date | None:
    """Where an instant counts, worked out the long way, as `report._on_days` placed it before its spans (T-168): the
    latest of `days` and the day after them whose window, midnight to midnight on that day's clock, holds it; None
    for the day after, or for no window."""
    following = days[-1] + timedelta(days=1)
    holding = [day for day in [*days, following] if at.astimezone(ZoneInfo(zone_on(day))).date() == day]
    latest = max(holding, default=None)
    return None if latest == following else latest


@pytest.mark.parametrize("history", [
    [(SINCE, "NASA")],                                             # Chicago's clock, its fall-back night (Nov 1) too
    [(SINCE, "NASA"), (date(2026, 10, 13), "APAC")],               # a swap east: two windows hold some hours
    [(SINCE, "APAC"), (date(2026, 10, 13), "NASA")],               # a swap west: no window holds some hours
    [(SINCE, "EMEA"), (date(2026, 10, 20), "APAC"), (date(2026, 10, 27), "EMEA"), (date(2026, 11, 1), "NASA")],
])
def test_each_instant_counts_on_the_latest_day_whose_window_holds_it(db, history):
    """`report._on_days` places what happened by each day's span (`report._spans`): every instant of the weeks around
    a region change, or a clock change, counts where the latest window holding it puts it, and every day's span
    starts where the day before's ends, or later (T-168)."""
    regions.set_regions(db, PAT, history)
    zone_on = regions.day_zones(db, PAT)
    days = [date(2026, 10, 5) + timedelta(days=n) for n in range(35)]   # Mon Oct 5 to Sun Nov 8
    moments = [SimpleNamespace(at=z(10, 4, 0) + timedelta(minutes=17 * n)) for n in range(36 * 24 * 60 // 17)]
    placed = {id(m): day for day, held in report._on_days(moments, days, zone_on).items() for m in held}
    assert [placed.get(id(m)) for m in moments] == [latest_window_holding(m.at, days, zone_on) for m in moments]
    spans = report._spans(days, zone_on)
    assert all(start < end for start, end in spans)
    assert all(before[1] <= after[0] for before, after in zip(spans, spans[1:]))


def test_day_states_are_the_states_of_the_grid_s_days(db):
    """`report.tech_day_states` judges each day from whether the tech acted and signed in on it, and gives every day
    the state `tech_day_facts` gives it, across a swap east and west, on the day of the last sync and after it
    (T-168)."""
    regions.set_regions(db, PAT, [(SINCE, "NASA"), (date(2026, 10, 13), "APAC"), (date(2026, 10, 20), "NASA")])
    synced(db, through="2026-10-23T15:00:00Z")                              # 10:00 Fri Oct 23 in Chicago
    store.add_time_off(db, PAT, date(2026, 10, 7), date(2026, 10, 7), "", "pto")
    store.add_time_off(db, PAT, date(2026, 10, 8), date(2026, 10, 8), "", "sick")
    did(db, PAT, z(10, 5, 15), z(10, 6, 15), z(10, 10, 15),                 # Mon, Tue, a worked Saturday
        z(10, 12, 20), z(10, 13, 10), z(10, 19, 20), z(10, 23, 14, 30))      # around both swaps; Friday's shift
    store.insert_signins(db, [SignIn(PAT, z(10, 9, 13, 55))])               # Fri Oct 9: signed in, no activity
    friday = date(2026, 10, 23)
    for today, now in ((friday, z(10, 23, 15)), (friday, None), (date(2026, 10, 25), None)):
        facts = report.tech_day_facts(db, PAT, date(2026, 8, 30), date(2026, 10, 31), today=today, now=now)
        states = report.tech_day_states(db, PAT, date(2026, 8, 30), date(2026, 10, 31), today=today, now=now)
        assert states == [(f.day, f.state) for f in facts], (today, now)
    assert {state for _, state in states} >= {"unsynced", "worked", "off", "rest", "pto", "missed", "zero", "future"}


# -- nothing changes until a region is set -------------------------------------------------------------------


def unset_db(conn) -> None:
    """A database with no regions on a UTC app zone, as many tests' are: PAT and SAM work Monday to Friday, 08:00 to
    17:00, from Sep 1, and the stored history begins at 06:00 Sep 25. Its actions number themselves from 1, so the
    answers, which name tickets, don't depend on the tests run before. Each ticket a tech set to solved is also stored
    as solved then while assigned to them, as the sync's ticket read stores it: Solved counts those (T-158)."""
    set_setting(conn, "timezone", "UTC")
    set_setting(conn, "team", [PAT, SAM])
    for email in (PAT, SAM):
        store.add_shift_version(conn, ShiftVersion(email, date(2026, 9, 1), MON_FRI, 480, 1020, True))
    synced(conn, window_start="2026-09-25T06:00:00Z", through="2026-10-17T06:00:00Z")
    done = [(PAT, z(9, 29, 10), "reply", None, None), (SAM, z(9, 30, 8, 15), "reply", None, None),
            (PAT, z(10, 5, 8, 30), "reply", None, None), (PAT, z(10, 5, 23, 30), "reply", None, None),
            (PAT, z(10, 6, 2), "note", None, None), (PAT, z(10, 6, 9), "reply", None, None),
            (PAT, z(10, 6, 16, 30), "status", "open", "solved"), (PAT, z(10, 8, 12), "reply", None, None),
            (SAM, z(10, 5, 9), "reply", None, None), (SAM, z(10, 7, 22), "reply", None, None),
            (SAM, z(10, 13, 7), "reply", None, None), (SAM, z(10, 13, 7, 30), "status", "open", "solved")]
    store.insert_actions(conn, [Action(email, n, at, kind, old, new, n, 1)
                                for n, (email, at, kind, old, new) in enumerate(done, start=1)])
    collected.set_agents(conn, {PAT: PAT_ID, SAM: SAM_ID})
    solve(conn, 7, z(10, 6, 16, 30), PAT_ID)       # the two tickets set to solved above
    solve(conn, 12, z(10, 13, 7, 30), SAM_ID)
    store.insert_signins(conn, [SignIn(PAT, z(10, 7, 7, 55)), SignIn(SAM, z(10, 6, 23))])
    store.add_time_off(conn, SAM, date(2026, 10, 9), date(2026, 10, 9), "dentist")


def without_regions(answer: dict) -> dict:
    """`answer` with each listed tech's `region` taken out, once it is checked to be NASA: the one thing the answers
    hold that they didn't before regions."""
    for tech in answer["techs"]:
        assert tech.pop("region") == "NASA", tech
    return answer


def without_collected(answer: dict) -> dict:
    """A Trends answer with the measures counted from the collected calls, ratings and aging (T-162) taken out, once
    each is checked to have no value anywhere: this database collected none. The line's fields (the owner,
    2026-10-22: each step's trailing window and the counts behind each figure) are taken out too, which came later."""
    first_ten = [m.key for m in trends.MEASURES[:10]]
    for measure in answer["measures"]:
        for key in ("window", "base", "line_digits"):
            measure.pop(key)
        for row in (measure["team"], *measure["techs"]):
            for key in ("steps_n", "rolling", "rolling_n"):
                row.pop(key)
        if measure["key"] not in first_ten:
            for row in (measure["team"], *measure["techs"]):
                assert row["now"] is None and row["before"] is None and all(v is None for v in row["steps"]), row
    answer["measures"] = [m for m in answer["measures"] if m["key"] in first_ten]
    return answer


def test_no_regions_answers_as_before(conn):
    """Ruling 8: with no region set, on a database whose app zone is UTC, the Activity grid, a day and Trends answer
    exactly as the code before regions did: BEFORE_GRID, BEFORE_DAY and BEFORE_TRENDS (at the end of this file) are
    these very calls, run once through commit 75d24c4. The only change is each listed tech's region, NASA, and the
    measures counted from the collected data, which have no value here. A tech with no region keeps the app's today,
    so a `now` that disagrees with it changes nothing."""
    unset_db(conn)
    today = date(2026, 10, 17)
    weeks, before = custom("2026-10-04", "2026-10-17"), custom("2026-09-20", "2026-10-03")
    for now in (None, z(12, 25, 12)):
        grid = report.range_view(conn, custom("2026-10-05", "2026-10-09"), None, today=today, now=now)
        assert without_regions(grid) == BEFORE_GRID
        assert report.day_view(conn, PAT, date(2026, 10, 6), today=today, now=now) == BEFORE_DAY
        answer = trends.trends_view(conn, weeks, before, today=today, now=now)
        assert without_collected(without_regions(answer)) == BEFORE_TRENDS


def test_setting_a_region_back_gives_the_same_answer(db):
    """Review Focus 4: a region change rewrites nothing stored, so changing it back gives the first answers, Trends'
    too. NASA is Dallas time, the app's own zone here, so a tech set to NASA reads as one never set."""
    did(db, PAT, z(10, 12, 1, 30), z(10, 12, 9, 30), z(10, 12, 14), z(10, 13, 15), z(10, 14, 23))
    store.insert_signins(db, [SignIn(PAT, z(10, 12, 0, 55)), SignIn(PAT, z(10, 15, 13))])
    weeks = custom("2026-10-11", "2026-10-17")

    def answers():
        return (report.range_view(db, weeks, None, today=TODAY),
                report.day_view(db, PAT, date(2026, 10, 12), today=TODAY),
                trends.trends_view(db, weeks, None, today=TODAY))

    unset = answers()
    regions.set_regions(db, PAT, [(SINCE, "NASA")])
    first = answers()
    assert first[0]["rows"] == unset[0]["rows"] and first[1:] == unset[1:]
    regions.set_regions(db, PAT, [(SINCE, "APAC")])
    moved = answers()
    assert moved[0]["rows"] != first[0]["rows"] and moved[2]["measures"] != first[2]["measures"]
    regions.set_regions(db, PAT, [(SINCE, "NASA")])
    assert answers() == first


# -- Trends -------------------------------------------------------------------------------------------------


def test_trends_counts_each_techs_days_on_their_clock(db):
    """Moments at 09:30 and 17:30 Monday in Manila are one Monday shift with two tickets and a first action 30
    minutes after its start, and 10:00 Tuesday in Manila is a Tuesday shift 60 minutes after. On Chicago's clock the
    same moments are 20:30 Sunday, a day outside the week, then 04:30 and 21:00 Monday: one shift, both outside its
    hours. The listed techs carry their current region."""
    did(db, PAT, z(10, 12, 1, 30), z(10, 12, 9, 30), z(10, 13, 2))
    week = custom("2026-10-12", "2026-10-16")
    chicago = trends.trends_view(db, week, None, today=TODAY)
    assert figures(chicago, "tickets_per_shift") == {"team": 2, PAT: 2, SAM: None}
    assert figures(chicago, "shifts_worked") == {"team": 10, PAT: 20, SAM: 0}
    assert figures(chicago, "late_start_min") == {"team": None, PAT: None, SAM: None}
    apac(db)
    manila = trends.trends_view(db, week, None, today=TODAY)
    assert figures(manila, "tickets_per_shift") == {"team": 1.5, PAT: 1.5, SAM: None}
    assert figures(manila, "shifts_worked") == {"team": 20, PAT: 40, SAM: 0}
    assert figures(manila, "late_start_min") == {"team": 45, PAT: 45, SAM: None}
    assert manila["techs"] == [{"email": PAT, "name": PAT, "region": "APAC"},
                               {"email": SAM, "name": SAM, "region": "NASA"}]


def test_trends_counts_a_day_once_it_is_over_on_the_techs_clock(db):
    """At 22:00 Monday in Chicago it is 11:00 Tuesday in Manila: the APAC tech's Monday is over and counts, while the
    NASA tech's Monday is still today and doesn't. Without `now` there is only the app's today, Monday, for both."""
    apac(db)
    now = z(10, 13, 3)
    synced(db, through=stamp(now))
    did(db, PAT, z(10, 12, 1, 30), z(10, 12, 9, 30))       # 09:30 and 17:30 Monday in Manila
    did(db, SAM, z(10, 12, 15))                            # 10:00 Monday in Chicago
    monday, day = date(2026, 10, 12), custom("2026-10-12", "2026-10-12")
    body = trends.trends_view(db, day, None, today=monday, now=now)
    assert figures(body, "tickets_per_shift") == {"team": 2, PAT: 2, SAM: None}
    assert body["today"] == "2026-10-12", "the answer's today is the app's"
    assert figures(trends.trends_view(db, day, None, today=monday), "tickets_per_shift") == {
        "team": None, PAT: None, SAM: None}


def test_the_trends_route_compares_one_tech_with_every_tech_on_the_pc(db, tmp_path):
    """The app's pinned moment reaches Trends. One tech is compared with their team lead's team (T-144), and the PC has
    no teams, so PAT (APAC) and SAM (NASA) are each drawn and averaged with every tech, as All techs is, and the answer
    names no team. region=APAC (in any case) is the APAC techs and NASA the NASA techs, with a tech or without. At
    22:00 Monday in Chicago only PAT's Monday is over."""
    apac(db)
    now = z(10, 13, 3)
    synced(db, through=stamp(now))
    did(db, PAT, z(10, 12, 1, 30), z(10, 12, 9, 30))
    did(db, SAM, z(10, 12, 15))
    client = TestClient(create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 1, now=now,
                                   log_dir=tmp_path / "logs", startup_work=False), base_url="http://127.0.0.1:8765")

    def read(**params) -> tuple:
        body = client.get("/api/activity/trends", params={"period": "custom", "start": "2026-10-12",
                                                          "end": "2026-10-12", **params}).json()
        assert body["team"] is None, params
        return body["tech"], [t["email"] for t in body["techs"]], figures(body, "tickets_per_shift")

    assert read() == (None, [PAT, SAM], {"team": 2, PAT: 2, SAM: None})
    assert read(tech=PAT) == (PAT, [PAT, SAM], {"team": 2, PAT: 2, SAM: None})
    assert read(tech=SAM) == (SAM, [PAT, SAM], {"team": 2, PAT: 2, SAM: None})
    assert read(region="APAC") == read(region=" apac ") == (None, [PAT], {"team": 2, PAT: 2})
    assert read(tech=PAT, region="APAC") == (PAT, [PAT], {"team": 2, PAT: 2})
    assert read(region="NASA") == (None, [SAM], {"team": None, SAM: None})
    assert read(tech=PAT, region="NASA") == (PAT, [SAM], {"team": None, SAM: None})
    assert read(region="EMEA") == (None, [], {"team": None})
    refused = client.get("/api/activity/trends", params={"region": "LATAM"})
    assert (refused.status_code, refused.json()["detail"]) == (400, REGION_ERROR)


# -- the techs, the routes and the history fill ---------------------------------------------------------------


def test_each_listed_tech_carries_their_current_region(db):
    regions.set_regions(db, PAT, [(SINCE, "APAC"), (date(2026, 10, 13), "EMEA")])
    assert [(t["email"], t["region"]) for t in report.techs(db)] == [(PAT, "EMEA"), (SAM, "NASA")]


def test_the_activity_routes_judge_each_techs_today_on_their_clock(db, tmp_path):
    """The app's pinned moment (`now_of`) reaches the grid and the day: at 22:00 Monday in Chicago, the APAC tech's
    Tuesday is under way while the NASA tech's is still to come."""
    apac(db)
    now = z(10, 13, 3)
    synced(db, through=stamp(now))
    did(db, PAT, z(10, 13, 1, 30))
    did(db, SAM, z(10, 12, 15))
    client = TestClient(create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 1, now=now,
                                   log_dir=tmp_path / "logs", startup_work=False), base_url="http://127.0.0.1:8765")
    body = client.get("/api/activity", params={"period": "custom", "start": "2026-10-12", "end": "2026-10-13"}).json()
    assert body["today"] == "2026-10-12"
    assert {r["email"]: [c["state"] for c in r["cells"]] for r in body["rows"]} == {
        PAT: ["missed", "worked"], SAM: ["worked", "future"]}
    day = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-13"}).json()
    assert day["cell"]["state"] == "worked" and [a["at"] for a in day["actions"]] == ["09:30"]


def test_activity_region_filter_keeps_a_lead_to_their_techs(client_hosted_tl, db_path):
    """Lee leads Avery and Blake. Casey, set to APAC, is not Lee's: Lee's APAC is empty, never Casey. Then Blake moves
    to APAC: Lee's APAC is Blake, Lee's NASA is Avery. An unknown region is refused."""
    conn = connect(db_path)
    regions.set_regions(conn, CASEY, [(SINCE, "APAC")])
    conn.close()
    nobody = client_hosted_tl.get("/api/activity", params={**WEEK, "region": "APAC"}).json()
    assert (nobody["techs"], nobody["rows"]) == ([], [])
    nasa = client_hosted_tl.get("/api/activity", params={**WEEK, "region": "NASA"}).json()
    assert sorted(t["email"] for t in nasa["techs"]) == sorted(r["email"] for r in nasa["rows"]) == [AVERY, BLAKE]
    assert {t["region"] for t in nasa["techs"]} == {"NASA"}

    conn = connect(db_path)
    regions.set_regions(conn, BLAKE, [(SINCE, "APAC")])
    conn.close()
    blake = client_hosted_tl.get("/api/activity", params={**WEEK, "region": "APAC"}).json()
    assert [t["email"] for t in blake["techs"]] == [r["email"] for r in blake["rows"]] == [BLAKE]
    assert client_hosted_tl.get("/api/activity", params={**WEEK, "region": " apac "}).json() == blake, "any case"
    avery = client_hosted_tl.get("/api/activity", params={**WEEK, "region": "NASA"}).json()
    assert [t["email"] for t in avery["techs"]] == [r["email"] for r in avery["rows"]] == [AVERY]

    refused = client_hosted_tl.get("/api/activity", params={**WEEK, "region": "LATAM"})
    assert (refused.status_code, refused.json()["detail"]) == (400, REGION_ERROR)


def test_a_tech_fill_starts_their_shift_on_their_clock(conn, tmp_path):
    """Coverage per tech: a history that began at 20:00 UTC on Jul 21 covers Jul 22 from its midnight in UTC, the app's
    zone here, but in Manila it began at 04:00 Jul 22, so an APAC tech's first covered day is Jul 23. A tech whose shift
    starts Jul 23 needs a fill on UTC's clock and not on Manila's."""
    set_setting(conn, "timezone", "UTC")
    set_setting(conn, "retention_days", 90)
    set_setting(conn, "team", [PAT, SAM])
    covered = z(7, 21, 20)
    synced(conn, window_start=stamp(covered), through="2026-10-19T00:00:00Z")
    apac(conn, SAM)
    assert activity_sync.first_covered_day(conn, covered) == date(2026, 7, 22)
    assert activity_sync.first_covered_day(conn, covered, SAM) == date(2026, 7, 23)
    assert activity_sync.first_covered_day(conn, covered, PAT) == date(2026, 7, 22), "no region: the app's clock"
    for email in (PAT, SAM):
        store.add_shift_version(conn, ShiftVersion(email, date(2026, 7, 23), MON_FRI, 540, 1080, True))
    assert activity_sync.techs_to_fill(conn) == [(PAT, date(2026, 7, 23))]

    later = z(10, 20, 12)
    result = run(conn, FakeClient([page([], later, end_of_stream=True)], users={PAT: PAT_ID, SAM: SAM_ID}), tmp_path,
                 now=later, kind="backfill", fill=[PAT, SAM])
    assert result["status"] == "complete"
    assert [store.shift_versions(conn, email)[0].effective_from for email in (PAT, SAM)] == [
        date(2026, 7, 22), date(2026, 7, 23)]


# -- the answers before regions ------------------------------------------------------------------------------
#
# test_no_regions_answers_as_before's calls, answered once by commit 75d24c4 (the code before Tasks 2 and 3) on
# unset_db's database, and written here as they came.

BEFORE_GRID = {"period": {"start": "2026-10-05", "end": "2026-10-09", "label": "2026-10-05 - 2026-10-09",
                          "so_far": False, "preset": "custom"},
               "today": "2026-10-17",
               "synced": {"at": "2026-10-17T06:00:00Z", "status": "complete", "running": False},
               "techs": [{"email": "pat@example.com", "name": "pat@example.com", "has_shift": True},
                         {"email": "sam@example.com", "name": "sam@example.com", "has_shift": True}],
               "days": ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"],
               "rows": [{"email": "pat@example.com", "name": "pat@example.com",
                         "cells": [{"day": "2026-10-05", "state": "worked", "tickets": 2, "replies": 2, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": "08:30", "last": "23:30",
                                    "gap_min": 0, "gap_start": None, "flags": [],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-06", "state": "worked", "tickets": 3, "replies": 1, "notes": 1,
                                    "calls": 0, "solved": 1, "signin": None, "first": "02:00", "last": "16:30",
                                    "gap_min": 450, "gap_start": "09:00",
                                    "flags": [{"kind": "long_gap", "minutes": 450}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-07", "state": "zero", "tickets": 0, "replies": 0, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": "07:55", "first": None, "last": None,
                                    "gap_min": 0, "gap_start": None,
                                    "flags": [{"kind": "no_activity", "minutes": None}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-08", "state": "worked", "tickets": 1, "replies": 1, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": "12:00", "last": "12:00",
                                    "gap_min": 0, "gap_start": None,
                                    "flags": [{"kind": "late_start", "minutes": 240},
                                              {"kind": "early_finish", "minutes": 300}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-09", "state": "missed", "tickets": 0, "replies": 0, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": None, "last": None,
                                    "gap_min": 0, "gap_start": None, "flags": [{"kind": "missed", "minutes": None}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None}]},
                        {"email": "sam@example.com", "name": "sam@example.com",
                         "cells": [{"day": "2026-10-05", "state": "worked", "tickets": 1, "replies": 1, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": "09:00", "last": "09:00",
                                    "gap_min": 0, "gap_start": None,
                                    "flags": [{"kind": "early_finish", "minutes": 480}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-06", "state": "zero", "tickets": 0, "replies": 0, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": "23:00", "first": None, "last": None,
                                    "gap_min": 0, "gap_start": None,
                                    "flags": [{"kind": "no_activity", "minutes": None}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-07", "state": "worked", "tickets": 1, "replies": 1, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": "22:00", "last": "22:00",
                                    "gap_min": 0, "gap_start": None,
                                    "flags": [{"kind": "late_start", "minutes": 840}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-08", "state": "missed", "tickets": 0, "replies": 0, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": None, "last": None,
                                    "gap_min": 0, "gap_start": None, "flags": [{"kind": "missed", "minutes": None}],
                                    "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
                                   {"day": "2026-10-09", "state": "pto", "tickets": 0, "replies": 0, "notes": 0,
                                    "calls": 0, "solved": 0, "signin": None, "first": None, "last": None,
                                    "gap_min": 0, "gap_start": None, "flags": [], "shift": None, "time_off": "pto"}]}]}

BEFORE_DAY = {"cell": {"day": "2026-10-06", "state": "worked", "tickets": 3, "replies": 1, "notes": 1, "calls": 0,
                       "solved": 1, "signin": None, "first": "02:00", "last": "16:30", "gap_min": 450,
                       "gap_start": "09:00", "flags": [{"kind": "long_gap", "minutes": 450}],
                       "shift": {"start": "08:00", "end": "17:00"}, "time_off": None},
              "time_off": None,
              "actions": [{"at": "02:00", "kind": "note", "old": None, "new": None, "ticket_id": 5, "subject": None,
                           "event_id": 5},
                          {"at": "09:00", "kind": "reply", "old": None, "new": None, "ticket_id": 6, "subject": None,
                           "event_id": 6},
                          {"at": "16:30", "kind": "status", "old": "open", "new": "solved", "ticket_id": 7,
                           "subject": None, "event_id": 7}],
              "recent": [{"day": "2026-09-30", "state": "missed", "tickets": 0, "replies": 0, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None,
                          "flags": [{"kind": "missed", "minutes": None}], "shift": {"start": "08:00", "end": "17:00"},
                          "time_off": None},
                         {"day": "2026-10-01", "state": "missed", "tickets": 0, "replies": 0, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None,
                          "flags": [{"kind": "missed", "minutes": None}], "shift": {"start": "08:00", "end": "17:00"},
                          "time_off": None},
                         {"day": "2026-10-02", "state": "missed", "tickets": 0, "replies": 0, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None,
                          "flags": [{"kind": "missed", "minutes": None}], "shift": {"start": "08:00", "end": "17:00"},
                          "time_off": None},
                         {"day": "2026-10-03", "state": "rest", "tickets": 0, "replies": 0, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None,
                          "flags": [], "shift": None, "time_off": None},
                         {"day": "2026-10-04", "state": "rest", "tickets": 0, "replies": 0, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None,
                          "flags": [], "shift": None, "time_off": None},
                         {"day": "2026-10-05", "state": "worked", "tickets": 2, "replies": 2, "notes": 0, "calls": 0,
                          "solved": 0, "signin": None, "first": "08:30", "last": "23:30", "gap_min": 0,
                          "gap_start": None, "flags": [], "shift": {"start": "08:00", "end": "17:00"},
                          "time_off": None},
                         {"day": "2026-10-06", "state": "worked", "tickets": 3, "replies": 1, "notes": 1, "calls": 0,
                          "solved": 1, "signin": None, "first": "02:00", "last": "16:30", "gap_min": 450,
                          "gap_start": "09:00", "flags": [{"kind": "long_gap", "minutes": 450}],
                          "shift": {"start": "08:00", "end": "17:00"}, "time_off": None}],
              "open": {"as_of": "2026-10-17T06:00:00Z", "tickets": []}}

BEFORE_TRENDS = {"period": {"start": "2026-10-04", "end": "2026-10-17", "label": "2026-10-04 - 2026-10-17",
                            "so_far": False, "preset": "custom"},
                 "comparison": {"start": "2026-09-20", "end": "2026-10-03", "label": "2026-09-20 - 2026-10-03",
                                "so_far": False, "preset": "custom"},
                 "tech": None, "today": "2026-10-17",
                 "synced": {"at": "2026-10-17T06:00:00Z", "status": "complete", "running": False},
                 "coverage_from": "2026-09-26", "unit": "week",
                 "steps": [{"start": "2026-09-20", "end": "2026-09-26", "label": "Sep 20", "tick": "Sep 20",
                            "in_period": False, "part": False},
                           {"start": "2026-09-27", "end": "2026-10-03", "label": "Sep 27", "tick": "Sep 27",
                            "in_period": False, "part": False},
                           {"start": "2026-10-04", "end": "2026-10-10", "label": "Oct 4", "tick": "Oct 4",
                            "in_period": True, "part": False},
                           {"start": "2026-10-11", "end": "2026-10-17", "label": "Oct 11", "tick": "Oct 11",
                            "in_period": True, "part": False}],
                 "techs": [{"email": "pat@example.com", "name": "pat@example.com"},
                           {"email": "sam@example.com", "name": "sam@example.com"}],
                 "measures": [{"key": "tickets_per_shift", "label": "Tickets per shift", "group": "Output",
                               "unit": "number", "digits": 1, "team_digits": 1, "better": "higher",
                               "team": {"now": 1.7, "before": 1.0, "change": 0.7, "steps": [None, 1.0, 1.6, 2.0]},
                               "techs": [{"email": "pat@example.com", "now": 2.0, "before": 1.0, "change": 1.0,
                                          "steps": [None, 1.0, 2.0, None]},
                                         {"email": "sam@example.com", "now": 1.3, "before": 1.0, "change": 0.3,
                                          "steps": [None, 1.0, 1.0, 2.0]}]},
                              {"key": "replies_per_shift", "label": "Replies per shift", "group": "Output",
                               "unit": "number", "digits": 1, "team_digits": 1, "better": "higher",
                               "team": {"now": 1.2, "before": 1.0, "change": 0.2, "steps": [None, 1.0, 1.2, 1.0]},
                               "techs": [{"email": "pat@example.com", "now": 1.3, "before": 1.0, "change": 0.3,
                                          "steps": [None, 1.0, 1.3, None]},
                                         {"email": "sam@example.com", "now": 1.0, "before": 1.0, "change": 0.0,
                                          "steps": [None, 1.0, 1.0, 1.0]}]},
                              {"key": "solved_per_shift", "label": "Solved per shift", "group": "Output",
                               "unit": "number", "digits": 1, "team_digits": 1, "better": "higher",
                               "team": {"now": 0.3, "before": 0.0, "change": 0.3, "steps": [None, 0.0, 0.2, 1.0]},
                               "techs": [{"email": "pat@example.com", "now": 0.3, "before": 0.0, "change": 0.3,
                                          "steps": [None, 0.0, 0.3, None]},
                                         {"email": "sam@example.com", "now": 0.3, "before": 0.0, "change": 0.3,
                                          "steps": [None, 0.0, 0.0, 1.0]}]},
                              {"key": "shifts_worked", "label": "Shifts worked", "group": "Reliability",
                               "unit": "percent", "digits": 0, "team_digits": 0, "better": "higher",
                               "team": {"now": 32, "before": 20, "change": 12, "steps": [None, 20, 56, 10]},
                               "techs": [{"email": "pat@example.com", "now": 30, "before": 20, "change": 10,
                                          "steps": [None, 20, 60, 0]},
                                         {"email": "sam@example.com", "now": 33, "before": 20, "change": 13,
                                          "steps": [None, 20, 50, 20]}]},
                              {"key": "missed_shifts", "label": "Sick days", "group": "Reliability", "unit": "count",
                               "digits": 0, "team_digits": 1, "better": "lower",
                               "team": {"now": 5.5, "before": 4.0, "change": 1.5, "steps": [None, 4.0, 1.0, 4.5]},
                               "techs": [{"email": "pat@example.com", "now": 6, "before": 4, "change": 2,
                                          "steps": [None, 4, 1, 5]},
                                         {"email": "sam@example.com", "now": 5, "before": 4, "change": 1,
                                          "steps": [None, 4, 1, 4]}]},
                              {"key": "signed_in_no_activity", "label": "Signed in, no activity",
                               "group": "Reliability", "unit": "count", "digits": 0, "team_digits": 1,
                               "better": "lower",
                               "team": {"now": 1.0, "before": 0.0, "change": 1.0, "steps": [None, 0.0, 1.0, 0.0]},
                               "techs": [{"email": "pat@example.com", "now": 1, "before": 0, "change": 1,
                                          "steps": [None, 0, 1, 0]},
                                         {"email": "sam@example.com", "now": 1, "before": 0, "change": 1,
                                          "steps": [None, 0, 1, 0]}]},
                              {"key": "late_start_min", "label": "First action after shift start",
                               "group": "Time on shift", "unit": "minutes", "digits": 0, "team_digits": 0,
                               "better": "lower",
                               "team": {"now": 98, "before": 68, "change": 30, "steps": [None, 68, 98, None]},
                               "techs": [{"email": "pat@example.com", "now": 110, "before": 120, "change": -10,
                                          "steps": [None, 120, 110, None]},
                                         {"email": "sam@example.com", "now": 60, "before": 15, "change": 45,
                                          "steps": [None, 15, 60, None]}]},
                              {"key": "early_finish_min", "label": "Last action before shift end",
                               "group": "Time on shift", "unit": "minutes", "digits": 0, "team_digits": 0,
                               "better": "lower",
                               "team": {"now": 230, "before": 473, "change": -243, "steps": [None, 473, 162, 570]},
                               "techs": [{"email": "pat@example.com", "now": 110, "before": 420, "change": -310,
                                          "steps": [None, 420, 110, None]},
                                         {"email": "sam@example.com", "now": 350, "before": 525, "change": -175,
                                          "steps": [None, 525, 240, 570]}]},
                              {"key": "longest_gap_min", "label": "Longest gap", "group": "Time on shift",
                               "unit": "minutes", "digits": 0, "team_digits": 0, "better": "lower",
                               "team": {"now": 450, "before": None, "change": None, "steps": [None, None, 450, None]},
                               "techs": [{"email": "pat@example.com", "now": 450, "before": None, "change": None,
                                          "steps": [None, None, 450, None]},
                                         {"email": "sam@example.com", "now": None, "before": None, "change": None,
                                          "steps": [None, None, None, None]}]},
                              {"key": "flags_per_shift", "label": "Flags per shift", "group": "Flags",
                               "unit": "number", "digits": 2, "team_digits": 2, "better": "lower",
                               "team": {"now": 1.0, "before": 1.1, "change": -0.1, "steps": [None, 1.1, 1.0, 1.0]},
                               "techs": [{"email": "pat@example.com", "now": 1.0, "before": 1.2, "change": -0.2,
                                          "steps": [None, 1.2, 1.0, 1.0]},
                                         {"email": "sam@example.com", "now": 1.0, "before": 1.0, "change": 0.0,
                                          "steps": [None, 1.0, 1.0, 1.0]}]}]}
