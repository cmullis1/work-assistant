"""The 1:1 prep page's data, counted from the collected data whenever it is asked for (`oneonone.live.count_preps`):
the sheet's formats, the Copy row, each tech's latest 1:1 period, a tech's detail (their periods back to where the
collected data begins, and what's behind the selected one), their trends, and the Settings schedule rows. Nothing is
made or stored.

The stored rows are test_oneonone_live's (Pat's and Sam's calls, ratings, solved tickets and aging lists, and Pat's
attendance), collected from Aug 1 to NOW. DAY is Tue Oct 20, Pat's and Sam's 1:1 day every other week; its period is
Oct 6 to Oct 19 in Chicago. Kim is on the team with no 1:1 date."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_oneonone_live import (DAY, EVERY_OTHER_TUESDAY, IDLE_DAY, KIM, LEGS, NOW, PAT, PAT_ID, PTO_DAY, RATINGS, SAM,
                                SAM_ID, SICK_DAY, TZ, WINDOW_END, WINDOW_START, at, call, collect, cover, leg, rating,
                                sync_activity, work, z)

from work_assistant import rubric
from work_assistant.activity import collected
from work_assistant.oneonone import live, report
from work_assistant.oneonone.model import PrepItem
from work_assistant.settings import set_setting

DESMOND, SAAHIL, RUBEN, ZANE = "desmond@example.com", "saahil@example.com", "ruben@example.com", "zane@example.com"
NO_AGING_YET = "Not recorded then: aging tickets are recorded each day from Oct 20."
ONE_RING = "1 unreachable ring left out (a phone or connection fault)"


def row_for(view, email):
    return next(r for r in view["rows"] if r["email"] == email)


def latest(conn) -> dict:
    return report.latest_view(conn, today=DAY, now=NOW)


def detail(conn, email=PAT, day=None) -> dict:
    return report.tech_view(conn, email, day, today=DAY, now=NOW)


def schedule(conn, entries: dict[str, str]) -> None:
    set_setting(conn, "oneonone_schedule", {email: {"date": day, "weeks": 2} for email, day in entries.items()})


def set_rule(conn, **params) -> None:
    """A new rubric version with these timed params changed."""
    current = rubric.current_version(conn)
    rubric.save_version(conn, current.checks, current.reference, replace(current.params, **params), "test")


@pytest.fixture
def db(conn):
    """Pat, Sam and Kim on the team; Pat and Sam meet every other Tuesday from Oct 20, Kim has no 1:1 date. Every kind
    is collected from Aug 1 to NOW, Team activity is synced to NOW, and Pat works Monday to Friday with a PTO day, a
    sick day and a day without activity in the period."""
    rubric.ensure_seeded(conn)
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [PAT, SAM, KIM])
    set_setting(conn, "oneonone_schedule", {PAT: EVERY_OTHER_TUESDAY, SAM: EVERY_OTHER_TUESDAY})
    sync_activity(conn)
    work(conn, PAT, pto=(PTO_DAY,), sick=(SICK_DAY,), idle=(IDLE_DAY,))
    collected.set_agents(conn, {PAT: PAT_ID, SAM: SAM_ID})
    cover(conn)
    return conn


@pytest.fixture
def four(conn):
    """The owner's four techs, in team order Saahil, Desmond, Zane, Ruben: Desmond's 1:1 is today, Saahil's tomorrow
    (latest 10/7), Ruben's in 2 days (latest 10/8), and Zane has no 1:1 date."""
    rubric.ensure_seeded(conn)
    set_setting(conn, "timezone", "America/Chicago")
    set_setting(conn, "team", [SAAHIL, DESMOND, ZANE, RUBEN])
    schedule(conn, {DESMOND: "2026-10-20", SAAHIL: "2026-10-21", RUBEN: "2026-10-22"})
    return conn


# -- formats ----------------------------------------------------------------------------------------------------

def test_formatters():
    assert report.pct(17, 498) == "3.41%"
    assert report.pct(0, 0) is None
    assert report.hms(10069) == "2:47:49"
    assert report.avg_time(592) == "09:52"
    assert report.avg_time(3723) == "1:02:03"


def test_columns_order_and_better():
    assert list(report.COLUMNS) == ["calls", "share", "declined_missed", "dm_share", "talk", "avg", "solved",
                                    "satisfaction", "aging", "attendance"]
    assert list(report.COLUMNS.values()) == ["higher", "higher", "lower", "lower", None, None, "higher", "higher",
                                             "lower", None]


# -- latest per tech ---------------------------------------------------------------------------------------------

def test_latest_view_counts_without_making(db):
    """Each tech with a 1:1 date has their latest 1:1 period counted from the stored rows, with nothing made first;
    the view says when the data was last synced, and has no next prep and no job."""
    collect(db)
    view = latest(db)
    assert set(view) == {"synced", "rows"}
    assert view["synced"] == "2026-10-20T11:30:00Z"
    assert [r["email"] for r in view["rows"]] == [PAT, SAM, KIM]   # team order

    pat = row_for(view, PAT)
    assert {k: pat[k] for k in ("state", "day", "label", "frame", "today", "next_day", "next_label", "soon",
                                "unreachable", "name")} == {
        "state": "counted", "day": "2026-10-20", "label": "Tue Oct 20", "frame": "10/6 – 10/19", "today": True,
        "next_day": "2026-11-03", "next_label": "Tue Nov 3", "soon": None, "unreachable": 1, "name": PAT}
    assert {k: c["text"] for k, c in pat["cells"].items()} == {
        "calls": "1", "share": "33.33%", "declined_missed": "1", "dm_share": "100.00%", "talk": "0:05:00",
        "avg": "05:00", "solved": "34", "satisfaction": "67%", "aging": "1", "attendance": "3"}
    assert {k: c["value"] for k, c in pat["cells"].items()} == {
        "calls": 1, "share": 33.33, "declined_missed": 1, "dm_share": 100.0, "talk": 300, "avg": 300, "solved": 34,
        "satisfaction": 67, "aging": 1, "attendance": 3}
    assert pat["copy"] == "10/20/2026\t10/6 - 10/19\t1\t33.33%\t1\t100.00%\t0:05:00\t05:00\t34\t67%\t1\t3"

    sam = row_for(view, SAM)
    assert (sam["state"], sam["day"], sam["cells"]["calls"]["text"], sam["cells"]["solved"]["text"]) == (
        "counted", "2026-10-20", "1", "12")
    assert row_for(view, KIM) == {
        "email": KIM, "name": KIM, "state": "no_schedule", "day": None, "label": None, "frame": None, "today": False,
        "next_day": None, "next_label": None, "soon": None, "cells": None, "copy": None, "unreachable": None}


def test_schedule_change_takes_effect_at_once(db):
    """A new 1:1 date counts the new 1:1 day at the next look: Pat's period moves a day earlier and takes in the call
    at 23:00 on Oct 5."""
    collect(db)
    assert row_for(latest(db), PAT)["day"] == "2026-10-20"
    set_setting(db, "oneonone_schedule", {PAT: {"date": "2026-10-19", "weeks": 2}, SAM: EVERY_OTHER_TUESDAY})
    pat = row_for(latest(db), PAT)
    assert (pat["state"], pat["day"], pat["frame"], pat["next_day"]) == (
        "counted", "2026-10-19", "10/5 – 10/18", "2026-11-02")
    assert (pat["cells"]["calls"]["text"], pat["cells"]["talk"]["text"]) == ("2", "0:06:30")
    assert pat["copy"].startswith("10/19/2026\t10/5 - 10/18\t2\t")


def test_soon_and_today_follow_each_techs_own_days(four):
    """Desmond's 1:1 is today; Saahil's latest was Oct 7 and his next is tomorrow, Ruben's in 2 days."""
    rows = latest(four)["rows"]
    assert [(r["email"], r["state"], r["day"], r["today"], r["soon"]) for r in rows] == [
        (SAAHIL, "counted", "2026-10-07", False, "Tomorrow"), (DESMOND, "counted", "2026-10-20", True, None),
        (ZANE, "no_schedule", None, False, None), (RUBEN, "counted", "2026-10-08", False, "In 2 days")]


def test_without_schedules_every_row_says_so(conn):
    set_setting(conn, "team", [PAT])
    view = latest(conn)
    assert view["synced"] is None and [r["state"] for r in view["rows"]] == ["no_schedule"]


def test_zero_calls_gives_dashes_not_errors(db):
    collect(db, legs=[row for row in LEGS if row["id"] != 3])   # Sam's answered call goes; his missed one stays
    sam = row_for(latest(db), SAM)
    cells = sam["cells"]
    assert (cells["calls"]["text"], cells["share"]["text"], cells["declined_missed"]["text"]) == ("0", "0.00%", "1")
    for key in ("dm_share", "avg"):
        assert cells[key] == {"text": None, "value": None, "reason": None}, key
    assert sam["copy"].startswith("10/20/2026\t10/6 - 10/19\t0\t0.00%\t1\t\t0:00:00\t\t12\t")


def test_failed_column_carries_reason(db):
    """Calls collected only to 23:00 on the period's last day: every call column of both techs is a dash with the
    reason, the % of calls included; a tech with no ratings has a dash without one; unsynced attendance says so."""
    db.execute("DELETE FROM activity_sync")
    db.commit()
    collect(db, ratings=[r for r in RATINGS if r["assignee_id"] != SAM_ID])
    collected.save_state(db, "legs", through=WINDOW_END - timedelta(hours=1))
    view = latest(db)
    pat = row_for(view, PAT)
    for key in ("calls", "share", "declined_missed", "dm_share", "talk", "avg"):
        assert pat["cells"][key] == {"text": None, "value": None, "reason": live.NOT_COLLECTED["calls"]}, key
    assert pat["unreachable"] is None and pat["cells"]["solved"]["text"] == "34"
    sam = row_for(view, SAM)["cells"]
    assert sam["satisfaction"] == {"text": None, "value": None, "reason": None}
    assert sam["attendance"] == {"text": None, "value": None, "reason": live.UNSYNCED}


def test_unreachable_title_on_declined_missed(db):
    collect(db)
    view = latest(db)
    assert row_for(view, PAT)["cells"]["declined_missed"] == {"text": "1", "value": 1, "reason": ONE_RING}
    assert row_for(view, SAM)["cells"]["declined_missed"]["reason"] is None
    collect(db, legs=LEGS + [leg(30, 107, PAT_ID, status="agent_unreachable", talk=0, at=z(10, 13, 15))],
            calls=[call(107, 5008)])
    pat = row_for(latest(db), PAT)
    assert pat["unreachable"] == 2
    assert pat["cells"]["declined_missed"]["reason"] == "2 unreachable rings left out (a phone or connection fault)"
    assert detail(db)["past"][0]["unreachable"] == 2


def test_tech_names_come_from_team_activity(db, monkeypatch):
    monkeypatch.setattr(report.activity_report, "techs", lambda conn: [{"email": PAT, "name": "Pat Lee"}])
    collect(db)
    assert [r["name"] for r in latest(db)["rows"]] == ["Pat Lee", SAM, KIM]
    assert detail(db)["name"] == "Pat Lee"
    assert report.schedule_rows(db, today=DAY)[0]["name"] == "Pat Lee"


# -- tech detail -------------------------------------------------------------------------------------------------

def test_tech_view_lists_their_periods_back_to_the_collected_data(db):
    """Pat's latest 1:1 and the ones before it, newest first, back to where the collected data begins (Aug 1: Aug
    11's period starts Jul 28, so it is left out), each counted with its cells."""
    collect(db)
    view = detail(db)
    assert (view["email"], view["name"], view["selected"]) == (PAT, PAT, "2026-10-20")
    assert (view["next_day"], view["next_label"]) == ("2026-11-03", "Tue Nov 3")
    assert [(p["day"], p["sheet_day"], p["frame"]) for p in view["past"]] == [
        ("2026-10-20", "10/20/2026", "10/6 - 10/19"), ("2026-10-06", "10/6/2026", "9/22 - 10/5"),
        ("2026-09-22", "9/22/2026", "9/8 - 9/21"), ("2026-09-08", "9/8/2026", "8/25 - 9/7"),
        ("2026-08-25", "8/25/2026", "8/11 - 8/24")]
    assert view["past"][0]["cells"] == row_for(latest(db), PAT)["cells"]
    assert view["past"][1]["cells"]["calls"]["text"] == "1", "the call at 23:00 on Oct 5"
    assert view["past"][1]["cells"]["aging"] == {"text": None, "value": None, "reason": NO_AGING_YET}


def test_tech_view_goes_back_to_the_earliest_collected_kind(db):
    """Calls collected from Sep 8 and the rest from Oct 6: the periods go back to Sep 8, and those before Oct 6 say
    their ratings and solved tickets aren't collected."""
    collect(db)
    cover(db, start=WINDOW_START)
    collected.save_state(db, "legs", covered_from=at(date(2026, 9, 8), 0))
    collected.save_state(db, "calls", covered_from=at(date(2026, 9, 8), 0))
    past = detail(db)["past"]
    assert [p["day"] for p in past] == ["2026-10-20", "2026-10-06", "2026-09-22"]
    assert past[1]["cells"]["calls"]["text"] == "1"
    assert past[1]["cells"]["satisfaction"]["reason"] == live.NOT_COLLECTED["satisfaction"]
    assert past[1]["cells"]["solved"]["reason"] == live.NOT_COLLECTED["solved"]


def test_tech_view_before_anything_is_collected_has_the_latest_only(db):
    db.execute("DELETE FROM collect_state")
    db.commit()
    past = detail(db)["past"]
    assert [p["day"] for p in past] == ["2026-10-20"]
    assert past[0]["cells"]["calls"]["reason"] == live.NOT_COLLECTED["calls"]


def test_tech_view_shows_whats_behind_the_selected_period(db):
    collect(db)
    assert detail(db)["behind"] == {
        "label": "1:1 10/20 (10/6 – 10/19)",
        "answered": [{"ticket_id": 5001, "day": "10/7", "talk": "5:00", "priority": "normal", "over": False}],
        "over_count": 0, "limit": 10,
        "ratings": [{"ticket_id": 7001, "day": "10/12", "score": "good", "reason": None, "comment": None},
                    {"ticket_id": 7002, "day": "10/13", "score": "good", "reason": None, "comment": None},
                    {"ticket_id": 7003, "day": "10/14", "score": "bad", "reason": None, "comment": None}],
        "aging": [{"ticket_id": 6001, "age_days": 8, "status": "open", "last_reply": "10/12", "reply_days_ago": 8}],
        "aging_reason": None,
        "days_out": [{"day": "10/13", "kind": "pto"}, {"day": "10/14", "kind": "sick"},
                     {"day": "10/15", "kind": "sick"}],
    }
    older = detail(db, day=date(2026, 10, 6))
    assert older["selected"] == "2026-10-06" and len(older["past"]) == 5
    behind = older["behind"]
    assert behind["label"] == "1:1 10/6 (9/22 – 10/5)"
    assert behind["answered"] == [{"ticket_id": 5007, "day": "10/5", "talk": "1:30", "priority": None, "over": False}]
    assert (behind["ratings"], behind["aging"], behind["aging_reason"]) == ([], None, NO_AGING_YET)


def test_aging_ages_count_from_when_its_list_was_recorded(db):
    """No list was recorded on the 1:1 day: the latest before it (Oct 18, 06:05 in Chicago) is shown, its tickets' ages
    and "{n}d ago" counted from when it was recorded."""
    collect(db, aging=False)
    item = PrepItem("aging", 6001, "2026-10-01T15:00:00Z", None, "open", last_reply="2026-10-12T16:20:00Z")
    collected.record_aging(db, PAT, date(2026, 10, 18), [item], now=z(10, 18, 11, 5))
    assert detail(db)["behind"]["aging"] == [
        {"ticket_id": 6001, "age_days": 16, "status": "open", "last_reply": "10/12", "reply_days_ago": 6}]


def test_highlight_follows_current_rubric(db):
    """Stored priorities and tags are judged against the rubric as it is now."""
    collect(db)
    set_rule(db, call_limit_minutes=4)
    behind = detail(db)["behind"]
    assert [c["over"] for c in behind["answered"]] == [True] and (behind["over_count"], behind["limit"]) == (1, 4)
    set_rule(db, call_limit_minutes=4, exempt_tags=("pos",))   # the call's ticket is tagged pos
    behind = detail(db)["behind"]
    assert [c["over"] for c in behind["answered"]] == [False] and behind["over_count"] == 0


def test_tech_view_without_that_period(db):
    collect(db)
    for email, day in ((PAT, date(2026, 10, 13)),          # not one of Pat's 1:1 days
                       (PAT, date(2026, 8, 11)),           # before the collected data
                       (PAT, date(2026, 11, 3)),           # hasn't come yet
                       (KIM, None),                        # no 1:1 date
                       ("nobody@example.com", None)):      # not on the team
        with pytest.raises(ValueError, match="No prep for that tech in that period."):
            detail(db, email, day)


# -- trends ------------------------------------------------------------------------------------------------------

def answered_on(agent_id: int, day: date, n: int, first: int) -> tuple[list[dict], list[dict]]:
    """`n` answered calls of `agent_id` at 10:00 in Chicago on `day`, each on its own inbound call (ids from
    `first`)."""
    return ([leg(first + k, first + k, agent_id, talk=60, at=at(day, 10)) for k in range(n)],
            [call(first + k, 9000 + first + k) for k in range(n)])


@pytest.fixture
def trends(db):
    """Pat meets on Tuesdays (Oct 20 every 2 weeks) and Sam on the Wednesdays after the other Tuesdays (Oct 7). Each
    period's calls are on its third day; ratings on its fourth."""
    set_setting(db, "oneonone_schedule", {PAT: EVERY_OTHER_TUESDAY, SAM: {"date": "2026-10-07", "weeks": 2}})
    plan = [(PAT_ID, date(2026, 8, 25), 2), (PAT_ID, date(2026, 9, 8), 5), (PAT_ID, date(2026, 9, 22), 7),
            (PAT_ID, date(2026, 10, 6), 9), (PAT_ID, date(2026, 10, 20), 11),
            (SAM_ID, date(2026, 8, 26), 3), (SAM_ID, date(2026, 9, 9), 4), (SAM_ID, date(2026, 9, 23), 6),
            (SAM_ID, date(2026, 10, 7), 10)]
    legs, calls = [], []
    for n, (agent_id, day, count) in enumerate(plan):
        more_legs, more_calls = answered_on(agent_id, day - timedelta(days=12), count, 1000 * (n + 1))
        legs += more_legs
        calls += more_calls
    scores = [(PAT_ID, date(2026, 9, 8), ("good", "bad")), (PAT_ID, date(2026, 10, 6), ("good", "good", "good", "bad")),
              (PAT_ID, date(2026, 10, 20), ("good",)), (SAM_ID, date(2026, 9, 23), ("good",)),
              (SAM_ID, date(2026, 10, 7), ("good", "bad"))]
    ratings = [rating(100 * (n + 1) + k, agent_id, score, at=at(day - timedelta(days=11), 12),
                      ticket_id=7000 + 100 * (n + 1) + k)
               for n, (agent_id, day, picked) in enumerate(scores) for k, score in enumerate(picked)]
    collect(db, legs=legs, calls=calls, tickets=[], ratings=ratings, solves=[], aging=False)
    return db


def by_key(view) -> dict:
    return {m["key"]: m for m in view["measures"]}


def test_trends_view_values_team_change_and_gaps(trends):
    view = report.trends_view(trends, PAT, 4, today=DAY, now=NOW)
    assert view["days"] == ["2026-09-08", "2026-09-22", "2026-10-06", "2026-10-20"]
    assert [(m["key"], m["label"], m["better"], m["unit"]) for m in view["measures"]] == [
        ("calls", "Total calls", "higher", "count"),
        ("share", "% of calls", "higher", "percent"),
        ("declined_missed", "Declined / missed", "lower", "count"),
        ("avg", "Avg. call time", None, "seconds"),
        ("solved", "Solved tickets", "higher", "count"),
        ("satisfaction", "Satisfaction", "higher", "percent"),
        ("aging", "Aging tickets", "lower", "count"),
        ("attendance", "Attendance", None, "count"),
    ]
    calls = by_key(view)["calls"]
    assert calls["values"] == [5, 7, 9, 11]
    assert calls["team"] == [4, 5.5, 7.5, 10.5]          # with Sam's period in the 14 days ending on each of Pat's days
    assert (calls["latest"], calls["change"]) == (11, 4)  # 11 against the mean of 5, 7 and 9
    satisfaction = by_key(view)["satisfaction"]
    assert satisfaction["values"] == [50, None, 75, 100]  # no ratings is a gap, not 0
    assert satisfaction["team"] == [50, None, 87.5, 75]   # blanks are left out
    assert (satisfaction["latest"], satisfaction["change"]) == (100, 37.5)
    assert by_key(report.trends_view(trends, PAT, 4, today=DAY, now=NOW, emails=[PAT]))["calls"]["team"] == \
        [5, 7, 9, 11], "the viewer's team only"

    assert report.trends_view(trends, PAT, 26, today=DAY, now=NOW)["days"][0] == "2026-08-25"
    for n in (5, 0, 27, True):
        with pytest.raises(ValueError, match="Choose 4, 8, 13 or 26 1:1s."):
            report.trends_view(trends, PAT, n, today=DAY, now=NOW)


def test_trends_lines_pool_the_periods_up_to_each_1_1(trends):
    """The owner, 2026-10-22, as Team Trends: each 1:1's line point pools the 14-day blocks up to it, 4 (8 weeks) for
    Satisfaction and 2 (4 weeks) for the rest. Ratings pool good over all, call time talk over calls; a total (Total
    calls) is the average of its blocks' totals. Each 1:1's own figure stays in `values`."""
    view = report.trends_view(trends, PAT, 4, today=DAY, now=NOW)
    calls, avg, sat = by_key(view)["calls"], by_key(view)["avg"], by_key(view)["satisfaction"]
    assert (calls["window_weeks"], sat["window_weeks"]) == (4, 8)
    assert calls["values"] == [5, 7, 9, 11], "each 1:1 alone, as before"
    assert (calls["rolling"], calls["rolling_n"]) == ([3.5, 6, 8, 10], [2, 2, 2, 2]), "Aug 25's 2 with Sep 8's 5 ..."
    assert avg["rolling"] == [60, 60, 60, 60] and avg["rolling_n"] == [7, 12, 16, 20], "talk over calls"
    assert (sat["rolling"], sat["rolling_n"]) == ([50, 50, 67, 71], [2, 2, 6, 7]), "Oct 20: 5 good of 7, 8 weeks"
    alone = by_key(report.trends_view(trends, PAT, 4, today=DAY, now=NOW, emails=[PAT]))
    assert alone["satisfaction"]["team_rolling"] == [50, 50, 67, 71], "the team's line: the mean of its techs' lines"


def test_pooled_parts():
    """`_pooled` over two blocks: % of calls over the queues, call time over the calls, a total the mean of the
    blocks that have one, Satisfaction None with no rating."""
    def prep(calls, talk, queue, good, bad, solved):
        return report.TechPrep(PAT, calls, 0, 0, talk, solved, good, bad, 3, 0, 0, {}, ())

    blocks = [(prep(10, 600, None, 0, 0, 4), 200), (prep(30, 3000, None, 0, 0, None), 400)]
    assert report._pooled("share", blocks) == (6.67, 600)
    assert report._pooled("avg", blocks) == (90, 40)
    assert report._pooled("solved", blocks) == (4, 1)
    assert report._pooled("satisfaction", blocks) == (None, 0)


def test_trends_count_every_period_they_need_at_once(trends, monkeypatch):
    """Pat's four periods, Sam's in the team average's 14 days before each, and every 14-day block the lines pool
    (each 1:1's and the three before it, for Pat and for Sam) are counted in one call, each once."""
    asked = []
    count = live.count_preps

    def spy(conn, wanted, *, now, **rest):
        asked.append(list(wanted))
        return count(conn, wanted, now=now, **rest)

    monkeypatch.setattr(live, "count_preps", spy)
    report.trends_view(trends, PAT, 4, today=DAY, now=NOW)
    assert len(asked) == 1
    pats = [date(2026, 9, 8), date(2026, 9, 22), date(2026, 10, 6), DAY]
    sams = [date(2026, 8, 26), date(2026, 9, 9), date(2026, 9, 23), date(2026, 10, 7)]
    blocks = {d - timedelta(days=14 * k) for d in pats for k in range(report.TREND_BLOCKS["satisfaction"])}
    assert len(asked[0]) == len(set(asked[0]))
    assert set(asked[0]) == {(PAT, d) for d in pats} | {(SAM, d) for d in sams} | {(t, b) for t in (PAT, SAM)
                                                                                 for b in blocks}


def test_trends_view_change_needs_a_latest_and_an_earlier_value(db):
    collect(db, aging=False)
    cover(db, start=WINDOW_START)   # only the latest period is collected
    view = report.trends_view(db, PAT, 8, today=DAY, now=NOW)
    assert view["days"] == ["2026-10-20"]
    calls, aging = by_key(view)["calls"], by_key(view)["aging"]
    assert (calls["values"], calls["team"], calls["latest"], calls["change"]) == ([1], [1], 1, None)
    assert (aging["values"], aging["team"], aging["latest"], aging["change"]) == ([None], [None], None, None)
    empty = report.trends_view(db, KIM, 8, today=DAY, now=NOW)
    assert empty["days"] == [] and all(m["values"] == [] and m["latest"] is None for m in empty["measures"])


# -- Settings schedule rows --------------------------------------------------------------------------------------

def test_schedule_rows(four):
    rows = report.schedule_rows(four, today=DAY)
    assert rows == [
        {"email": SAAHIL, "name": SAAHIL, "date": "2026-10-21", "weeks": 2, "latest_label": "Wed Oct 7",
         "next_label": "Wed Oct 21", "covers": "10/7 – 10/20"},
        {"email": DESMOND, "name": DESMOND, "date": "2026-10-20", "weeks": 2, "latest_label": "Tue Oct 20",
         "next_label": "Tue Nov 3", "covers": "10/20 – 11/2"},
        {"email": ZANE, "name": ZANE, "date": None, "weeks": None, "latest_label": None, "next_label": None,
         "covers": None},
        {"email": RUBEN, "name": RUBEN, "date": "2026-10-22", "weeks": 2, "latest_label": "Thu Oct 8",
         "next_label": "Thu Oct 22", "covers": "10/8 – 10/21"},
    ]
