"""Read-side statistics behind the app's screens (web/queries.py). Synthetic data only."""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from work_assistant import demo, periods, rubric
from work_assistant.db import connect
from work_assistant.settings import set_setting
from work_assistant.web import queries

from factories import add_run

TZ = "America/Chicago"
WEEK = date(2026, 10, 4)       # a Sunday
PREV = date(2026, 9, 27)       # the Sunday before
SAM = ("sam@example.com", "Sam Agent")
LEE = ("lee@example.com", "Lee Agent")


@pytest.fixture
def version(conn):
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [SAM[0], LEE[0]])
    return rubric.ensure_seeded(conn)


def all_met(version) -> dict[str, str]:
    return {c.check_id: "met" for c in version.checks if c.enabled}


def add_review(conn, version, ticket_id, outcomes, *, agent=SAM, call="2026-10-05T15:00:00Z", overrides=None,
               counted=True, status="ok", priority="normal", attribution_ok=True, current=True,
               recheck=(), subject="Receipt printer offline", model="claude-opus-5-5"):
    """One contact and its review, written straight into the database."""
    overrides = overrides or {}
    run_id = conn.execute("SELECT MIN(run_id) FROM runs").fetchone()[0] or add_run(conn)
    conn.execute(
        "INSERT OR IGNORE INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', ?, ?, ?, ?, ?, ?, '2026-10-12T00:00:00Z')",
        (ticket_id, call, agent[0], agent[1], priority, int(attribution_ok),
         json.dumps({"subject": subject, "comments": [{"text": f"transcript of {subject}"}]})))
    review_id = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at, counted, "
        "not_counted_reason, model) VALUES (?, ?, ?, ?, ?, '2026-10-12T00:00:00Z', ?, ?, ?)",
        (run_id, ticket_id, version.version_id, status, int(current), int(counted),
         None if counted else "Caller said it was already fixed", model)).lastrowid
    conn.executemany(
        "INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by, override_outcome, "
        "needs_recheck) VALUES (?, ?, ?, 'evidence', 'model', ?, ?)",
        [(review_id, cid, out, overrides.get(cid), int(cid in recheck)) for cid, out in outcomes.items()])
    conn.commit()
    return review_id


def missing(version, *check_ids) -> dict[str, str]:
    return {**all_met(version), **{cid: "missed" for cid in check_ids}}


TODAY = date(2026, 10, 17)        # a Saturday, as tests/conftest.py pins the web app's today
REVIEWED = date(2026, 10, 10)     # the last day reviewed: the Saturday ending the week of WEEK


def week_overview(conn, week=WEEK, compare=PREV, agent=None):
    """The week containing `week` against the week containing `compare` (the old week view's periods)."""
    facts = {"today": TODAY, "last_reviewed": REVIEWED}
    return queries.overview(conn, periods.week_of(week, **facts), periods.week_of(compare, **facts), agent,
                            last_reviewed=REVIEWED)


def eight_weeks(conn, agent=None):
    """The 8 weeks ending with the week of WEEK, against the 8 before: the trend steps are those weeks."""
    chosen = periods.weeks_ending(8, WEEK, today=TODAY, last_reviewed=REVIEWED)
    return queries.overview(conn, chosen, periods.comparison(chosen, "previous"), agent, last_reviewed=REVIEWED)


def preset_overview(conn, preset, compare="previous", agent=None, *, today=TODAY, last_reviewed=REVIEWED,
                    start=None, end=None, compare_start=None, compare_end=None):
    chosen = periods.resolve(preset, today=today, last_reviewed=last_reviewed, latest_week_with_data=WEEK,
                             start=start, end=end)
    against = periods.comparison(chosen, compare, start=compare_start, end=compare_end)
    return queries.overview(conn, chosen, against, agent, last_reviewed=last_reviewed)


def agent_page(conn, email, preset="last_8_weeks", *, today=TODAY, last_reviewed=REVIEWED, start=None, end=None):
    """`queries.agent_detail` for a preset; by default Last 8 weeks, Aug 16 to Oct 10 (the old default page)."""
    chosen = periods.resolve(preset, today=today, last_reviewed=last_reviewed, latest_week_with_data=WEEK,
                             start=start, end=end)
    return queries.agent_detail(conn, email, chosen, last_reviewed=last_reviewed)


def marked(result, key):
    """The start days of the trend steps that carry the `key` marker."""
    return [s["start"] for s in result["trend"]["steps"] if s[key]]


def figures(count, share, compare_count, compare_share, change_pts, count_change):
    return {"count": count, "share_pct": share, "compare_count": compare_count, "compare_share_pct": compare_share,
            "change_pts": change_pts, "count_change": count_change}


NO_FIGURES = figures(None, None, None, None, None, None)


# -- contact_rows -------------------------------------------------------------------------------------


def test_effective_outcome_used(conn, version):
    review_id = add_review(conn, version, 1, missing(version, "sc06"))
    [row] = queries.contact_rows(conn, start=None, end=None, agent=None, verdict=None, missed_check=None,
                                 priority=None, q=None)
    assert row["verdict"] == "problem" and row["missed"] == ["#6"] and row["percent"] == 95.0

    conn.execute("UPDATE check_results SET override_outcome = 'met' WHERE review_id = ? AND check_id = 'sc06'",
                 (review_id,))
    conn.commit()
    [row] = queries.contact_rows(conn, start=None, end=None, agent=None, verdict=None, missed_check=None,
                                 priority=None, q=None)
    assert row["verdict"] == "good"
    assert row["missed"] == []
    assert (row["earned"], row["total"], row["percent"]) == (100, 100, 100.0)


def test_contact_row_shape(conn, version):
    add_review(conn, version, 7, missing(version, "sc15", "pr-call-length"), priority="high", recheck=("sc15",))
    [row] = queries.contact_rows(conn, start=None, end=None, agent=None, verdict=None, missed_check=None,
                                 priority=None, q=None)
    assert row == {
        "ticket_id": 7, "date": "2026-10-05", "agent_email": SAM[0], "agent_name": SAM[1], "priority": "high",
        "earned": 95, "total": 100, "percent": 95.0, "verdict": "problem", "counted": True,
        "missed": ["#15", "10-minute rule"], "missed_ids": ["sc15", "pr-call-length"], "needs_recheck": True,
        "review_error": False, "error": None, "not_counted_reason": None,
    }


def test_contact_rows_filters(conn, version):
    add_review(conn, version, 1, missing(version, "sc06"), agent=SAM, priority="high", subject="Kiosk frozen")
    add_review(conn, version, 2, all_met(version), agent=LEE, call="2026-10-06T15:00:00Z")
    add_review(conn, version, 3, {**all_met(version), "sc02": "unsure"}, agent=LEE, call="2026-09-29T15:00:00Z")
    add_review(conn, version, 4, missing(version, "sc06"), counted=False, call="2026-10-07T15:00:00Z")

    def ids(**filters):
        args = dict(start=None, end=None, agent=None, verdict=None, missed_check=None, priority=None, q=None)
        return [r["ticket_id"] for r in queries.contact_rows(conn, **{**args, **filters})]

    assert ids() == [4, 2, 1, 3]  # newest call first, Not counted included and marked
    assert ids(start=WEEK, end=date(2026, 10, 10)) == [4, 2, 1]
    assert ids(end=date(2026, 10, 3)) == [3]
    assert ids(agent="LEE@example.com") == [2, 3]
    assert ids(verdict="problem") == [1]
    assert ids(verdict="good") == [2]
    assert ids(verdict="needs_review") == [3]
    assert ids(verdict="not_counted") == [4]
    assert ids(missed_check="sc06") == [1]  # the Not counted call's miss is not counted
    assert ids(priority="high") == [1]
    assert ids(q="kiosk") == [1]
    assert ids(q="lee agent") == [2, 3]
    assert ids(q="3") == [3]
    nc = [r for r in queries.contact_rows(conn, start=None, end=None, agent=None, verdict="not_counted",
                                          missed_check=None, priority=None, q=None)][0]
    assert nc["counted"] is False and nc["verdict"] == "not_counted" and nc["percent"] is None and nc["missed"] == []


def test_only_current_reviews_and_enabled_checks(conn, version):
    add_review(conn, version, 1, missing(version, "sc06"), current=False)
    checks = tuple(dataclasses.replace(c, enabled=False) if c.check_id == "sc06" else c for c in version.checks)
    v2 = rubric.save_version(conn, checks, version.reference, version.params, "sc06 off")
    add_review(conn, v2, 1, {**all_met(v2), "sc06": "missed"})
    [row] = queries.contact_rows(conn, start=None, end=None, agent=None, verdict=None, missed_check=None,
                                 priority=None, q=None)
    assert row["verdict"] == "good"  # the old review is history; sc06 is off in the review's version
    assert row["total"] == 95 and row["percent"] == 100.0


# -- overview -------------------------------------------------------------------------------------------


def test_overview_counts(conn, version):
    add_review(conn, version, 1, all_met(version))
    add_review(conn, version, 2, all_met(version), agent=LEE)
    add_review(conn, version, 3, missing(version, "sc06"), agent=LEE)
    add_review(conn, version, 4, missing(version, "sc06"), counted=False)
    add_review(conn, version, 5, missing(version, "pr-call-length", "pr-scope"), call="2026-09-28T15:00:00Z")
    result = week_overview(conn)
    assert result["period"] == {"start": "2026-10-04", "end": "2026-10-10", "label": "Week of Oct 4",
                                "so_far": False, "preset": "custom"}
    assert result["comparison"]["start"] == "2026-09-27"
    assert result["has_data_in_period"] is True and result["has_data_in_comparison"] is True
    tiles = result["tiles"]
    assert tiles["calls"] == {"count": 3, "compare_count": 1}
    assert tiles["avg_score"] == {"avg_percent": 98.3, "compare_avg_percent": 100.0, "change": -1.7}  # (100+100+95)/3
    assert tiles["problems"] == figures(1, 33.3, 1, 100.0, -66.7, 0)
    assert tiles["call_length_misses"] == figures(0, 0.0, 1, 100.0, -100.0, -1)
    assert tiles["scope_misses"] == figures(0, 0.0, 1, 100.0, -100.0, -1)
    assert result["most_missed"] == [{"check_id": "sc06", "label": "#6 Triage: Verify understanding",
                                      **figures(1, 33.3, 0, 0.0, 33.3, 1)}]
    assert [p["ticket_id"] for p in result["problems"]] == [3]
    # A week trends by day: the 14 days ending with it, Sep 27 to Oct 10. The calls are on Sep 28 and Oct 5.
    agents = {a["email"]: a for a in result["agents"]}
    assert agents[LEE[0]] == {"email": LEE[0], "name": LEE[1], "contacts": 2, "avg_percent": 97.5, "problems": 1,
                              "problem_rate": 50.0, "trend": [None] * 8 + [97.5] + [None] * 5,
                              "top_miss": "#6 Triage: Verify understanding"}
    assert agents[SAM[0]]["trend"] == [None, 100.0] + [None] * 6 + [100.0] + [None] * 5
    assert agents[SAM[0]]["top_miss"] is None
    assert result["trend"]["unit"] == "day"
    assert result["trend"]["steps"][8] == {"start": "2026-10-05", "end": "2026-10-05", "in_period": True,
                                           "so_far": False, "partial": False, "calls": 3, "avg_percent": 98.3,
                                           "problem_share_pct": 33.3, "rubric_changed": False,
                                           "model_changed": False}

    only_lee = week_overview(conn, agent=LEE[0])
    assert only_lee["tiles"]["calls"]["count"] == 2 and [a["email"] for a in only_lee["agents"]] == [LEE[0]]


def test_overview_empty_week(conn):
    result = week_overview(conn)
    assert result["has_data_in_period"] is False and result["has_data_in_comparison"] is False
    tiles = result["tiles"]
    assert tiles["calls"] == {"count": None, "compare_count": None}       # no calls is not a count of 0
    assert tiles["avg_score"] == {"avg_percent": None, "compare_avg_percent": None, "change": None}
    assert tiles["problems"] == tiles["call_length_misses"] == tiles["scope_misses"] == NO_FIGURES
    steps = result["trend"]["steps"]
    assert len(steps) == 14 and steps[0]["start"] == "2026-09-27"
    assert all(s["avg_percent"] is None and s["problem_share_pct"] is None and s["calls"] == 0
               and s["rubric_changed"] is False and s["model_changed"] is False for s in steps)
    assert result["agents"] == [] and result["most_missed"] == [] and result["problems"] == []


def test_overview_counts_timed_and_scope_misses(conn, version):
    add_review(conn, version, 1, missing(version, "pr-call-length"))
    add_review(conn, version, 2, missing(version, "pr-call-length", "pr-scope"))
    add_review(conn, version, 3, missing(version, "pr-scope"), overrides={"pr-scope": "met"})
    result = week_overview(conn)
    tiles = result["tiles"]
    assert (tiles["call_length_misses"]["count"], tiles["scope_misses"]["count"]) == (2, 1)
    # The week before has no calls, so nothing is compared.
    assert result["most_missed"][0] == {"check_id": "pr-call-length", "label": "10-minute rule",
                                        **figures(2, 66.7, None, None, None, None)}


def test_rubric_changed_flag(conn, version):
    add_review(conn, version, 1, all_met(version), call="2026-09-28T15:00:00Z")
    v2 = rubric.save_version(conn, version.checks, version.reference, version.params, "v2")
    add_review(conn, v2, 2, all_met(v2))
    steps = eight_weeks(conn)["trend"]["steps"]
    assert [s["rubric_changed"] for s in steps] == [False] * 7 + [True]
    assert steps[-2]["avg_percent"] == 100.0


def test_retired_check_label_kept(conn, version):
    add_review(conn, version, 1, missing(version, "sc06"))
    renamed = tuple(dataclasses.replace(c, summary="Triage: Confirm the issue") if c.check_id == "sc06" else c
                    for c in version.checks)
    v2 = rubric.save_version(conn, renamed, version.reference, version.params, "reworded #6")
    rubric.save_version(conn, tuple(c for c in v2.checks if c.check_id != "sc06"), v2.reference, v2.params,
                        "retired #6")
    result = week_overview(conn)
    assert [(m["check_id"], m["label"], m["count"]) for m in result["most_missed"]] == [
        ("sc06", "#6 Triage: Confirm the issue", 1)]
    detail = agent_page(conn, SAM[0])
    assert {c["check_id"]: c["label"] for c in detail["checks"]}["sc06"] == "#6 Triage: Confirm the issue"


# -- agents ---------------------------------------------------------------------------------------------


def test_removed_agent_still_listed(conn, version):
    add_review(conn, version, 1, missing(version, "sc06"), agent=LEE)
    add_review(conn, version, 2, all_met(version), agent=SAM)
    set_setting(conn, "team", [SAM[0]])
    rows = queries.contact_rows(conn, start=None, end=None, agent=LEE[0], verdict=None, missed_check=None,
                                priority=None, q=None)
    assert [r["ticket_id"] for r in rows] == [1]
    assert LEE[0] in {a["email"] for a in week_overview(conn)["agents"]}
    detail = agent_page(conn, LEE[0])
    assert detail["name"] == LEE[1] and detail["active"] is False
    assert detail["trend"]["steps"][-1] == {
        "start": "2026-10-04", "end": "2026-10-10", "in_period": True, "so_far": False, "partial": False, "calls": 1,
        "agent": 95.0, "team": 97.5, "rubric_changed": False, "model_changed": False}
    assert [p["ticket_id"] for p in detail["problems"]] == [1]
    assert agent_page(conn, SAM[0])["active"] is True


def test_check_gap_flag(conn, version):
    ticket = 0
    for agent, passes, total in ((SAM, 61, 100), (LEE, 149, 150)):
        for i in range(total):
            ticket += 1
            add_review(conn, version, ticket, {"sc01": "met", "sc06": "met" if i < passes else "missed",
                                               "sc11": "na"}, agent=agent)
    checks = {c["check_id"]: c for c in agent_page(conn, SAM[0])["checks"]}
    sc06 = checks["sc06"]
    assert (sc06["agent_rate"], sc06["team_rate"], sc06["gap"]) == (61.0, 84.0, -23.0)
    assert sc06["flagged"] is True and sc06["applied_count"] == 100
    assert checks["sc01"]["gap"] == 0 and checks["sc01"]["flagged"] is False
    assert checks["sc11"]["applied_count"] == 0 and checks["sc11"]["agent_rate"] is None
    assert checks["sc11"]["flagged"] is False


def test_agent_detail_tiles_and_notes(conn, version):
    add_review(conn, version, 1, missing(version, "sc06"), agent=SAM)
    add_review(conn, version, 2, all_met(version), agent=SAM)
    add_review(conn, version, 3, all_met(version), agent=LEE)
    add_review(conn, version, 4, all_met(version), agent=SAM, call="2026-09-28T15:00:00Z")
    conn.execute("INSERT INTO coaching_notes (agent_email, created_at, body) VALUES (?, ?, ?)",
                 (SAM[0], "2026-10-06T12:00:00Z", "Summarize the issue back"))
    conn.commit()
    detail = agent_page(conn, SAM[0])
    tiles = detail["tiles"]
    # Every tile covers the 8 weeks: Sam has 3 of the team's 4 calls, 1 of them a problem.
    assert detail["period"]["label"] == "Last 8 weeks (Aug 16 – Oct 10)" and detail["has_data_in_period"] is True
    assert tiles["contacts"] == 3 and tiles["contacts_rank"] == 1
    assert tiles["avg_percent"] == 98.3 and tiles["team_avg_percent"] == 98.8
    assert tiles["problem_rate"] == 33.3 and tiles["team_problem_rate"] == 25.0
    # Sam met #6 on 2 of 3 calls (66.7%), the team on 3 of 4 (75.0%).
    assert tiles["biggest_gap"] == {"check_id": "sc06", "label": "#6 Triage: Verify understanding", "gap": -8.3}
    assert detail["notes"] == [{"note_id": 1, "created_at": "2026-10-06T12:00:00Z",
                                "body": "Summarize the issue back"}]
    assert detail["trend"]["unit"] == "week" and len(detail["trend"]["steps"]) == 8


# -- agreement ------------------------------------------------------------------------------------------


def test_agreement_rate(conn, version):
    ten = {f"sc{n:02d}": "met" for n in range(1, 11)}
    add_review(conn, version, 1, ten, overrides={"sc03": "missed"})
    add_review(conn, version, 2, ten)
    add_review(conn, version, 3, ten, overrides={"sc04": "missed"}, counted=False)
    add_review(conn, version, 4, ten, overrides={"sc05": "missed"}, current=False)
    add_review(conn, version, 5, ten, overrides={"sc05": "missed"}, call="2026-09-28T15:00:00Z")
    result = queries.agreement(conn, WEEK, date(2026, 10, 10))
    assert result["overall"] == 0.95
    assert result["by_check"]["sc03"] == 0.5 and result["by_check"]["sc04"] == 1.0
    assert result["by_check"]["sc05"] == 1.0
    assert queries.agreement(conn, None, None)["overall"] == pytest.approx(1 - 2 / 30)
    assert queries.agreement(conn, date(2020, 1, 1), date(2020, 1, 2)) == {"overall": None, "by_check": {}}


# -- the demo database, end to end ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo_conn(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.db"
    demo.build_demo_db(path, now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    connection = connect(path)
    yield connection
    connection.close()


DEMO_FACTS = {"today": date(2026, 10, 14), "last_reviewed": date(2026, 10, 10)}   # the demo's calls end Oct 10


def demo_last_8_weeks():
    return periods.resolve("last_8_weeks", latest_week_with_data=date(2026, 10, 4), **DEMO_FACTS)


def demo_overview(conn, chosen, agent=None):
    return queries.overview(conn, chosen, periods.comparison(chosen, "previous"), agent,
                            last_reviewed=DEMO_FACTS["last_reviewed"])


def test_demo_overview_is_consistent(demo_conn):
    rows = queries.contact_rows(demo_conn, start=None, end=None, agent=None, verdict=None, missed_check=None,
                                priority=None, q=None)
    current = demo_conn.execute("SELECT COUNT(*) FROM reviews WHERE is_current = 1").fetchone()[0]
    assert len(rows) == current
    assert any(not r["counted"] for r in rows)

    latest = max(date.fromisoformat(r["date"]) for r in rows)
    result = demo_overview(demo_conn, periods.week_of(latest, **DEMO_FACTS))
    week_rows = [r for r in rows if r["counted"] and date.fromisoformat(r["date"]) >= date.fromisoformat(
        result["period"]["start"])]
    assert result["tiles"]["calls"]["count"] == len(week_rows)
    assert result["tiles"]["problems"]["count"] == sum(r["verdict"] == "problem" for r in week_rows)
    assert len(result["agents"]) == 6
    assert sum(a["contacts"] for a in result["agents"]) == result["tiles"]["calls"]["count"]
    steps = demo_overview(demo_conn, demo_last_8_weeks())["trend"]["steps"]
    assert [s["rubric_changed"] for s in steps] == [False] * 4 + [True] + [False] * 3
    assert 0 < queries.agreement(demo_conn, None, None)["overall"] < 1


# -- fix round 1 ------------------------------------------------------------------------------------------


def test_review_error_is_review_failed_but_not_a_zero_score(conn, version):
    add_review(conn, version, 1, all_met(version), agent=LEE, call="2026-09-28T15:00:00Z")
    add_review(conn, version, 2, all_met(version))
    add_review(conn, version, 3, {"pr-call-length": "met", "pr-email-sent": "met"}, status="error")
    result = week_overview(conn)
    assert result["tiles"]["avg_score"]["avg_percent"] == 100.0  # not 50.0: the errored call has no score
    assert result["tiles"]["calls"]["count"] == 2 and result["tiles"]["problems"]["count"] == 0
    oct_5 = 8   # the trend's day steps run from Sep 27
    assert result["trend"]["steps"][oct_5]["avg_percent"] == 100.0
    [sam] = [a for a in result["agents"] if a["email"] == SAM[0]]
    assert sam["contacts"] == 2 and sam["avg_percent"] == 100.0 and sam["trend"][oct_5] == 100.0
    rows = queries.contact_rows(conn, start=WEEK, end=None, agent=None, verdict="review_failed", missed_check=None,
                                priority=None, q=None)
    assert [r["ticket_id"] for r in rows] == [3]
    assert rows[0]["verdict"] == "review_failed" and rows[0]["review_error"] is True
    assert (rows[0]["earned"], rows[0]["percent"], rows[0]["total"]) == (None, None, 100)  # never a 0 score
    assert queries.contact_rows(conn, start=WEEK, end=None, agent=None, verdict="needs_review", missed_check=None,
                                priority=None, q=None) == []

    detail = agent_page(conn, SAM[0])
    assert detail["tiles"]["avg_percent"] == 100.0 and detail["tiles"]["team_avg_percent"] == 100.0
    assert detail["tiles"]["contacts"] == 2
    last = detail["trend"]["steps"][-1]
    assert (last["start"], last["calls"], last["agent"], last["team"]) == ("2026-10-04", 2, 100.0, 100.0)
    applied = {c["check_id"]: c["applied_count"] for c in detail["checks"]}
    assert applied["pr-call-length"] == 1  # pass rates come from ok reviews only


def test_rubric_changed_marks_only_new_versions(conn, version):
    v2 = rubric.save_version(conn, version.checks, version.reference, version.params, "v2")
    # Week of Sep 20: v1 counted, plus a Not counted call already on v2 (ignored).
    add_review(conn, version, 1, all_met(version), call="2026-09-21T15:00:00Z")
    add_review(conn, v2, 2, all_met(v2), call="2026-09-22T15:00:00Z", counted=False)
    # Week of Sep 27: mixed v1 and v2.  Week of Oct 4: v2 only.
    add_review(conn, version, 3, all_met(version), call="2026-09-28T15:00:00Z")
    add_review(conn, v2, 4, all_met(v2), call="2026-09-29T15:00:00Z")
    add_review(conn, v2, 5, all_met(v2))
    steps = eight_weeks(conn)["trend"]["steps"]
    assert [s["rubric_changed"] for s in steps] == [False] * 5 + [False, True, False]


def test_overview_filter_choices_are_team_wide(conn, version):
    add_review(conn, version, 1, all_met(version), call="2026-09-28T15:00:00Z")
    add_review(conn, version, 2, all_met(version), agent=LEE, call="2026-09-21T15:00:00Z")
    add_review(conn, version, 3, missing(version, "sc06"), counted=False, call="2026-10-05T15:00:00Z")
    for result in (week_overview(conn), week_overview(conn, agent=LEE[0])):
        assert "weeks" not in result and result["has_data"] is True
        assert result["agent_options"] == [{"email": LEE[0], "name": LEE[1]}, {"email": SAM[0], "name": SAM[1]}]


def test_overview_without_any_data(conn, version):
    result = week_overview(conn)
    assert result["has_data"] is False and result["agent_options"] == []
    assert result["agents"] == [] and result["most_missed"] == [] and result["problems"] == []


# -- the reviewing model (Task 4, T-034) ------------------------------------------------------------------

OPUS, SONNET, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"


def test_model_changed_marker(conn, version):
    """A week is marked when the most common model among its current reviews differs from the most recent
    earlier week that has reviews. Weeks without reviews are skipped, and the first week is never marked."""
    v2 = rubric.save_version(conn, version.checks, version.reference, version.params, "v2")
    add_review(conn, version, 1, all_met(version), call="2026-08-31T15:00:00Z")                 # Aug 30: Opus
    add_review(conn, version, 2, all_met(version), call="2026-09-14T15:00:00Z")                 # Sep 13: Opus
    add_review(conn, version, 3, all_met(version), call="2026-09-21T15:00:00Z", model=SONNET)   # Sep 20: mostly
    add_review(conn, version, 4, all_met(version), call="2026-09-22T15:00:00Z", model=SONNET)   # Sonnet
    add_review(conn, version, 5, all_met(version), call="2026-09-23T15:00:00Z", agent=LEE)
    add_review(conn, version, 6, all_met(version), call="2026-09-28T15:00:00Z", model=SONNET)   # Sep 27: Sonnet,
    add_review(conn, version, 7, all_met(version), call="2026-09-29T15:00:00Z", model=FABLE,    # history ignored
               current=False)
    add_review(conn, v2, 8, all_met(v2), agent=LEE)                                             # Oct 4: Opus, v2
    steps = eight_weeks(conn)["trend"]["steps"]
    assert [s["start"] for s in steps][2::] == ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20",
                                                 "2026-09-27", "2026-10-04"]
    assert [s["model_changed"] for s in steps] == [False, False, False, False, False, True, False, True]
    assert [s["rubric_changed"] for s in steps] == [False] * 7 + [True]   # the last week carries both markers
    # Agent trends carry the team-wide marker, as they do the rubric one; so does one agent's overview.
    trend = agent_page(conn, LEE[0])["trend"]["steps"]
    assert [t["model_changed"] for t in trend] == [False, False, False, False, False, True, False, True]
    only_lee = eight_weeks(conn, agent=LEE[0])["trend"]["steps"]
    assert [s["model_changed"] for s in only_lee] == [False, False, False, False, False, True, False, True]


# -- any period (Task 4, T-042) ----------------------------------------------------------------------------


def add_calls(conn, version, first_ticket, count, problems, call):
    """`count` counted calls by Sam at `call`, the first `problems` of them missing #6 (a problem)."""
    for i in range(count):
        add_review(conn, version, first_ticket + i, missing(version, "sc06") if i < problems else all_met(version),
                   call=call)


def test_overview_tiles_share_and_change(conn, version):
    """Count tiles compare shares of calls, so a busier period is never worse just for having more calls."""
    add_calls(conn, version, 1, 20, 4, "2026-10-05T15:00:00Z")      # the week of Oct 4: 4 problems in 20 calls
    add_calls(conn, version, 101, 10, 3, "2026-09-28T15:00:00Z")    # the week before: 3 in 10
    result = week_overview(conn)
    tiles = result["tiles"]
    assert tiles["problems"] == figures(4, 20.0, 3, 30.0, -10.0, 1)
    assert tiles["calls"] == {"count": 20, "compare_count": 10}
    assert tiles["avg_score"] == {"avg_percent": 99.0, "compare_avg_percent": 98.5, "change": 0.5}
    assert result["most_missed"] == [{"check_id": "sc06", "label": "#6 Triage: Verify understanding",
                                      **figures(4, 20.0, 3, 30.0, -10.0, 1)}]

    # Compared with nothing: every comparison figure is None, never 0.
    alone = queries.overview(conn, periods.week_of(WEEK, today=TODAY, last_reviewed=REVIEWED), None,
                             last_reviewed=REVIEWED)
    assert alone["comparison"] is None and alone["has_data_in_comparison"] is False
    assert alone["tiles"]["problems"] == figures(4, 20.0, None, None, None, None)
    assert alone["tiles"]["calls"] == {"count": 20, "compare_count": None}
    assert alone["tiles"]["avg_score"] == {"avg_percent": 99.0, "compare_avg_percent": None, "change": None}
    assert alone["most_missed"][0]["change_pts"] is None


# Captured from the pre-T-042 `queries.overview(demo_conn, date(2026, 10, 4), None, None)` (the week view), so
# the period-based overview is checked against what the app showed before, not against hand-worked numbers.
# Weekly points: [week, avg_percent, problem_rate, rubric_changed, model_changed].
PRE_T042_DEMO_WEEKLY = [
    ["2026-08-16", 93.6, 84.8, False, False], ["2026-08-23", 93.7, 91.2, False, False],
    ["2026-08-30", 94.0, 96.7, False, False], ["2026-09-06", 93.0, 93.3, False, False],
    ["2026-09-13", 93.0, 97.3, True, False], ["2026-09-20", 94.4, 80.6, False, False],
    ["2026-09-27", 91.5, 87.9, False, False], ["2026-10-04", 91.5, 93.3, False, False]]
PRE_T042_DEMO_SPARKLINES = {
    "avery.stone@example.com": [99.0, 95.8, 95.0, 95.0, 96.0, 98.8, 95.0, 93.8],
    "jordan.pike@example.com": [90.0, 87.9, 90.0, 91.0, 90.8, 95.0, 82.1, 90.8],
    "morgan.reyes@example.com": [88.8, 97.0, 93.8, 88.0, 91.4, 92.0, 93.6, 90.0],
    "riley.chen@example.com": [92.5, 96.4, 95.0, 93.0, 95.0, 92.1, 93.6, 92.5],
    "sam.okafor@example.com": [92.5, 93.0, 93.6, 94.2, 94.3, 92.5, 95.0, 91.2],
    "taylor.brooks@example.com": [97.1, 92.5, 95.7, 96.7, 91.4, 96.4, 93.3, 92.0]}
# The same with agent=sam.okafor@example.com.
PRE_T042_DEMO_SAM_WEEKLY = [
    ["2026-08-16", 92.5, 100.0, False, False], ["2026-08-23", 93.0, 80.0, False, False],
    ["2026-08-30", 93.6, 100.0, False, False], ["2026-09-06", 94.2, 83.3, False, False],
    ["2026-09-13", 94.3, 100.0, True, False], ["2026-09-20", 92.5, 100.0, False, False],
    ["2026-09-27", 95.0, 100.0, False, False], ["2026-10-04", 91.2, 100.0, False, False]]
# The week of Oct 4 against the week before: tiles, most-missed [check_id, count, delta], agent rows
# [email, contacts, avg_percent, problems, problem_rate, top_miss] and the latest problems.
PRE_T042_DEMO_TILES = {"avg_percent": 91.5, "problem_count": 28, "contact_count": 30, "call_length_misses": 12,
                       "scope_misses": 3, "prev": {"avg_percent": 91.5, "problem_count": 29, "contact_count": 33,
                                                   "call_length_misses": 12, "scope_misses": 1}}
PRE_T042_DEMO_MOST_MISSED = [
    ["pr-call-length", 12, 0], ["sc15", 6, -3], ["pr-email-sent", 5, -3], ["sc01", 4, 1], ["sc07", 4, 0],
    ["sc13", 4, 0], ["sc16", 4, 0], ["pr-triage-macro", 4, 1], ["sc04", 3, 2], ["sc06", 3, -1], ["sc08", 3, 0],
    ["sc09", 3, -2], ["pr-scope", 3, 2], ["sc02", 2, -1], ["sc11", 2, 2], ["sc14", 2, 0], ["sc03", 1, -1],
    ["sc05", 1, -1], ["sc10", 1, 1], ["pr-dropped-callback", 1, 0]]
PRE_T042_DEMO_AGENTS = [
    ["avery.stone@example.com", 4, 93.8, 3, 75.0, "10-minute rule"],
    ["jordan.pike@example.com", 6, 90.8, 6, 100.0, "#15 Closing: Remind the customer what comes next"],
    ["morgan.reyes@example.com", 7, 90.0, 7, 100.0, "#9 Resolution: Take ownership and assure the customer"],
    ["riley.chen@example.com", 4, 92.5, 3, 75.0, "#4 Triage: Request what the question, issue, or problem is"],
    ["sam.okafor@example.com", 4, 91.2, 4, 100.0, "10-minute rule"],
    ["taylor.brooks@example.com", 5, 92.0, 5, 100.0, "#13 Confirmation: Confirm the problem has been resolved"]]
PRE_T042_DEMO_PROBLEMS = [50254, 50259, 50241, 50249, 50236, 50252, 50258, 50247, 50238, 50261]


def weekly_points(result):
    return [[s["start"], s["avg_percent"], s["problem_share_pct"], s["rubric_changed"], s["model_changed"]]
            for s in result["trend"]["steps"]]


def test_overview_trend_uses_steps(conn, version, demo_conn):
    """Yesterday trends by day over the 14 days ending with it. Last 8 weeks trends by week with exactly the
    values the week view showed (a regression guard)."""
    add_review(conn, version, 1, missing(version, "sc06"), call="2026-10-16T15:00:00Z")      # Fri Oct 16
    add_review(conn, version, 2, all_met(version), call="2026-10-05T15:00:00Z")
    result = preset_overview(conn, "yesterday", last_reviewed=date(2026, 10, 16))
    trend = result["trend"]
    assert trend["unit"] == "day" and len(trend["steps"]) == 14
    assert [s["start"] for s in trend["steps"]] == [(date(2026, 10, 3) + timedelta(days=i)).isoformat()
                                                   for i in range(14)]
    assert all(s["start"] == s["end"] for s in trend["steps"])
    assert [s["in_period"] for s in trend["steps"]] == [False] * 13 + [True]
    assert not any(s["so_far"] for s in trend["steps"])
    assert trend["steps"][-1] == {
        "start": "2026-10-16", "end": "2026-10-16", "in_period": True, "so_far": False, "partial": False, "calls": 1,
        "avg_percent": 95.0, "problem_share_pct": 100.0, "rubric_changed": False, "model_changed": False}
    assert trend["steps"][2]["avg_percent"] == 100.0 and trend["steps"][2]["problem_share_pct"] == 0.0
    assert trend["steps"][3]["calls"] == 0 and trend["steps"][3]["avg_percent"] is None
    # Before the morning run reaches yesterday, Yesterday is empty: nothing in it is counted yet, and its
    # trend stops at the last day reviewed.
    before_run = preset_overview(conn, "yesterday", last_reviewed=date(2026, 10, 15))
    assert before_run["period"]["label"] == "Yesterday (nothing reviewed yet)"
    assert not before_run["has_data_in_period"] and before_run["trend"]["steps"][-1]["end"] == "2026-10-15"

    eight = demo_overview(demo_conn, demo_last_8_weeks())
    assert eight["trend"]["unit"] == "week"
    assert weekly_points(eight) == PRE_T042_DEMO_WEEKLY
    assert all(s["in_period"] and not s["so_far"] for s in eight["trend"]["steps"])
    assert {a["email"]: a["trend"] for a in eight["agents"]} == PRE_T042_DEMO_SPARKLINES
    assert weekly_points(demo_overview(demo_conn, demo_last_8_weeks(), agent="sam.okafor@example.com")) == \
        PRE_T042_DEMO_SAM_WEEKLY


def test_overview_last_week_matches_the_week_view(demo_conn):
    """Last week shows the figures the week view showed for that week (a regression guard)."""
    result = demo_overview(demo_conn, periods.resolve("last_week", latest_week_with_data=date(2026, 10, 4),
                                                      **DEMO_FACTS))
    assert result["period"]["start"] == "2026-10-04"
    old, prev, tiles = PRE_T042_DEMO_TILES, PRE_T042_DEMO_TILES["prev"], result["tiles"]
    assert tiles["calls"] == {"count": old["contact_count"], "compare_count": prev["contact_count"]}
    assert (tiles["avg_score"]["avg_percent"], tiles["avg_score"]["compare_avg_percent"]) == (
        old["avg_percent"], prev["avg_percent"])
    for key, old_key in (("problems", "problem_count"), ("call_length_misses", "call_length_misses"),
                         ("scope_misses", "scope_misses")):
        assert (tiles[key]["count"], tiles[key]["compare_count"]) == (old[old_key], prev[old_key]), key
    assert [[m["check_id"], m["count"], m["count_change"]] for m in result["most_missed"]] == \
        PRE_T042_DEMO_MOST_MISSED
    assert [[a["email"], a["contacts"], a["avg_percent"], a["problems"], a["problem_rate"], a["top_miss"]]
            for a in result["agents"]] == PRE_T042_DEMO_AGENTS
    assert [p["ticket_id"] for p in result["problems"]] == PRE_T042_DEMO_PROBLEMS


def test_overview_empty_period_and_empty_comparison(conn, version):
    """Review Focus 2: an empty period or comparison has no figures (None, never 0) and never crashes."""
    add_review(conn, version, 1, missing(version, "sc06"), call="2026-10-15T15:00:00Z")      # Thu Oct 15
    add_review(conn, version, 2, all_met(version), call="2026-10-05T15:00:00Z")

    # Yesterday on a day with no calls, against the day before, which had one.
    result = preset_overview(conn, "yesterday", last_reviewed=date(2026, 10, 16))
    assert result["period"]["start"] == "2026-10-16" and result["comparison"]["start"] == "2026-10-15"
    assert result["has_data_in_period"] is False and result["has_data_in_comparison"] is True
    tiles = result["tiles"]
    assert tiles["calls"] == {"count": None, "compare_count": 1}
    assert tiles["avg_score"] == {"avg_percent": None, "compare_avg_percent": 95.0, "change": None}
    assert tiles["problems"] == figures(None, None, 1, 100.0, None, None)
    assert tiles["scope_misses"] == figures(None, None, 0, 0.0, None, None)
    assert result["most_missed"] == [] and result["agents"] == [] and result["problems"] == []
    steps = result["trend"]["steps"]
    assert (steps[-1]["calls"], steps[-1]["avg_percent"], steps[-1]["problem_share_pct"]) == (0, None, None)
    assert steps[-2]["avg_percent"] == 95.0      # the context before the period still shows

    # This week so far on a Sunday before anything is reviewed: an empty period, and an empty comparison.
    result = preset_overview(conn, "this_week", today=date(2026, 10, 18), last_reviewed=date(2026, 10, 17))
    assert result["period"]["end"] == "2026-10-17" and result["comparison"]["label"] == "Nothing to compare yet"
    assert result["has_data_in_period"] is False and result["has_data_in_comparison"] is False
    tiles = result["tiles"]
    assert tiles["calls"] == {"count": None, "compare_count": None}
    assert tiles["avg_score"] == {"avg_percent": None, "compare_avg_percent": None, "change": None}
    assert tiles["problems"] == tiles["call_length_misses"] == tiles["scope_misses"] == NO_FIGURES
    assert result["most_missed"] == [] and result["agents"] == [] and result["problems"] == []
    steps = result["trend"]["steps"]
    assert len(steps) == 14 and steps[-1]["start"] == "2026-10-17" and not any(s["in_period"] for s in steps)

    # A period with calls against a comparison with none.
    result = preset_overview(conn, "last_week", "custom", compare_start=date(2026, 9, 1),
                             compare_end=date(2026, 9, 14))
    assert result["has_data_in_period"] is True and result["has_data_in_comparison"] is False
    assert result["tiles"]["calls"] == {"count": 1, "compare_count": None}
    assert result["tiles"]["problems"] == figures(0, 0.0, None, None, None, None)
    assert result["tiles"]["avg_score"] == {"avg_percent": 100.0, "compare_avg_percent": None, "change": None}

    # An agent with no calls in the period.
    result = preset_overview(conn, "last_week", agent=LEE[0])
    assert result["has_data_in_period"] is False and result["tiles"]["problems"] == NO_FIGURES


def test_overview_markers_by_step(conn, version):
    """A rubric change mid-week marks the day it first appears on, the week that contains it and its month;
    markers stay team-wide whatever the agent filter."""
    v2 = rubric.save_version(conn, version.checks, version.reference, version.params, "v2")
    add_review(conn, version, 1, all_met(version), call="2026-09-28T15:00:00Z")              # Mon Sep 28: v1
    add_review(conn, version, 2, all_met(version), call="2026-10-05T15:00:00Z")              # Mon Oct 5: v1
    add_review(conn, v2, 3, all_met(v2), call="2026-10-07T15:00:00Z")                        # Wed Oct 7: v2
    add_review(conn, v2, 4, all_met(v2), call="2026-10-08T15:00:00Z", model=SONNET)          # Thu Oct 8: v2

    days = week_overview(conn)
    assert days["trend"]["unit"] == "day"
    assert marked(days, "rubric_changed") == ["2026-10-07"]
    assert marked(days, "model_changed") == ["2026-10-08"]      # Sonnet after a day on Opus

    weeks = eight_weeks(conn)
    assert weeks["trend"]["unit"] == "week"
    assert marked(weeks, "rubric_changed") == ["2026-10-04"]
    assert marked(weeks, "model_changed") == []                 # the week was mostly Opus, as the one before

    months = preset_overview(conn, "custom", start=date(2026, 4, 1), end=date(2026, 10, 10))
    assert months["trend"]["unit"] == "month"
    assert [s["start"] for s in months["trend"]["steps"]][-2:] == ["2026-09-01", "2026-10-01"]
    assert marked(months, "rubric_changed") == ["2026-10-01"]   # September is the first month with data

    assert marked(week_overview(conn, agent=LEE[0]), "rubric_changed") == ["2026-10-07"]   # Lee has no calls


# -- fix round 1 (T-042) ----------------------------------------------------------------------------------


def test_custom_range_edge_steps_count_only_the_period(conn, version):
    """A 35-day range from Wed Sep 2 to Tue Oct 6 trends by week. Its first and last weeks only partly
    overlap it: they count only the period's calls (and say so), so the in-period steps add up to the
    tiles. Context steps stay whole."""
    add_review(conn, version, 1, all_met(version), call="2026-08-25T15:00:00Z")            # Tue Aug 25: context
    add_review(conn, version, 2, missing(version, "sc06"), call="2026-08-31T15:00:00Z")    # Mon Aug 31: before
    add_review(conn, version, 3, all_met(version), call="2026-09-02T15:00:00Z")            # Wed Sep 2: first day
    add_review(conn, version, 4, all_met(version), agent=LEE, call="2026-09-15T15:00:00Z")
    add_review(conn, version, 5, missing(version, "sc06"), call="2026-10-06T15:00:00Z")    # Tue Oct 6: last day
    add_review(conn, version, 6, all_met(version), call="2026-10-08T15:00:00Z")            # Thu Oct 8: after
    result = preset_overview(conn, "custom", start=date(2026, 9, 2), end=date(2026, 10, 6))
    steps = {s["start"]: s for s in result["trend"]["steps"]}
    assert result["trend"]["unit"] == "week"
    in_period = [s for s in result["trend"]["steps"] if s["in_period"]]
    assert [s["start"] for s in in_period] == ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20",
                                              "2026-09-27", "2026-10-04"]
    assert sum(s["calls"] for s in in_period) == result["tiles"]["calls"]["count"] == 3
    assert [s["partial"] for s in in_period] == [True, False, False, False, False, True]
    assert (steps["2026-08-30"]["calls"], steps["2026-08-30"]["avg_percent"]) == (1, 100.0)    # not Aug 31
    assert (steps["2026-10-04"]["calls"], steps["2026-10-04"]["problem_share_pct"]) == (1, 100.0)   # not Oct 8
    assert (steps["2026-08-23"]["calls"], steps["2026-08-23"]["partial"]) == (1, False)      # context, whole
    [sam] = [a for a in result["agents"] if a["email"] == SAM[0]]
    assert sam["trend"][2] == 100.0 and sam["trend"][-1] == 95.0    # the sparkline follows the clipped steps


def test_late_evening_call_lands_on_its_local_day(conn, version):
    """04:30 UTC on Oct 11 is 23:30 on Saturday Oct 10 in Chicago: the call is Oct 10's, so it is in Last
    week (Oct 4-10) and on the Oct 10 step."""
    add_review(conn, version, 1, all_met(version), call="2026-10-11T04:30:00Z")
    result = preset_overview(conn, "last_week")
    assert (result["period"]["start"], result["period"]["end"]) == ("2026-10-04", "2026-10-10")
    assert result["tiles"]["calls"]["count"] == 1
    assert [s["start"] for s in result["trend"]["steps"] if s["calls"]] == ["2026-10-10"]


# -- Agents over any period (Task 5, T-043) ----------------------------------------------------------------


def test_agent_period_tiles_and_pass_rates(conn, version):
    """Every tile and pass rate covers the chosen period, against the team over the same days; calls just
    outside the period count nowhere."""
    add_review(conn, version, 1, missing(version, "sc06"), call="2026-10-05T15:00:00Z")            # Sam, problem
    add_review(conn, version, 2, missing(version, "sc06"), call="2026-10-06T15:00:00Z")            # Sam, problem
    add_review(conn, version, 3, all_met(version), call="2026-10-07T15:00:00Z")                    # Sam
    add_review(conn, version, 4, all_met(version), agent=LEE, call="2026-09-28T15:00:00Z")
    add_review(conn, version, 5, all_met(version), agent=LEE, call="2026-10-06T16:00:00Z")
    add_review(conn, version, 6, all_met(version), call="2026-09-26T15:00:00Z")                    # Sat Sep 26
    add_review(conn, version, 7, missing(version, "sc06"), agent=LEE, call="2026-10-11T15:00:00Z")  # Sun Oct 11
    detail = agent_page(conn, SAM[0], "custom", start=date(2026, 9, 27), end=date(2026, 10, 10))
    assert detail["period"] == period_dict("2026-09-27", "2026-10-10", "Sep 27 – Oct 10, 2026")
    assert detail["has_data_in_period"] is True
    assert detail["tiles"] == {
        "avg_percent": 96.7, "team_avg_percent": 98.0,          # (95 + 95 + 100) / 3; the team's 5 calls
        "problem_rate": 66.7, "team_problem_rate": 40.0,        # 2 of 3; 2 of 5
        "contacts": 3, "contacts_rank": 1,
        "biggest_gap": {"check_id": "sc06", "label": "#6 Triage: Verify understanding", "gap": -26.7},
    }
    checks = {c["check_id"]: c for c in detail["checks"]}
    sc06 = checks["sc06"]       # Sam met #6 on 1 of 3 calls (33.3%), the team on 3 of 5 (60.0%)
    assert (sc06["agent_rate"], sc06["team_rate"], sc06["gap"], sc06["flagged"], sc06["applied_count"]) == (
        33.3, 60.0, -26.7, True, 3)
    assert [c["check_id"] for c in detail["checks"] if c["flagged"]] == ["sc06"]   # the only coaching target
    assert {c["gap"] for c in detail["checks"] if c["check_id"] != "sc06"} == {0.0}
    assert [p["ticket_id"] for p in detail["problems"]] == [2, 1]     # newest first
    lee = agent_page(conn, LEE[0], "custom", start=date(2026, 9, 27), end=date(2026, 10, 10))["tiles"]
    assert (lee["contacts"], lee["contacts_rank"], lee["problem_rate"]) == (2, 2, 0.0)


def period_dict(start, end, label, preset="custom", so_far=False):
    return {"start": start, "end": end, "label": label, "so_far": so_far, "preset": preset}


def test_agent_trend_steps(conn, version):
    """The agent-against-team trend steps as `periods.trend_steps` does: by day, week or month to suit the
    period, edge steps clipped to the period, the unfinished last step so far."""
    add_review(conn, version, 1, missing(version, "sc06"), call="2026-10-16T15:00:00Z")            # Fri Oct 16
    add_review(conn, version, 2, all_met(version), agent=LEE, call="2026-10-16T16:00:00Z")
    add_review(conn, version, 3, all_met(version), call="2026-10-05T15:00:00Z")                    # context
    trend = agent_page(conn, SAM[0], "yesterday", last_reviewed=date(2026, 10, 16))["trend"]
    steps = trend["steps"]
    assert trend["unit"] == "day" and len(steps) == 14 and steps[0]["start"] == "2026-10-03"
    assert [s["in_period"] for s in steps] == [False] * 13 + [True]
    assert steps[-1] == {"start": "2026-10-16", "end": "2026-10-16", "in_period": True, "so_far": False,
                         "partial": False, "calls": 1, "agent": 95.0, "team": 97.5, "rubric_changed": False,
                         "model_changed": False}
    assert (steps[2]["calls"], steps[2]["agent"], steps[2]["team"]) == (1, 100.0, 100.0)    # Oct 5
    assert (steps[3]["calls"], steps[3]["agent"], steps[3]["team"]) == (0, None, None)
    # Before the morning run reaches yesterday, Yesterday is empty and its trend stops at the last day reviewed.
    before_run = agent_page(conn, SAM[0], "yesterday", last_reviewed=date(2026, 10, 15))
    assert not before_run["has_data_in_period"] and before_run["trend"]["steps"][-1]["end"] == "2026-10-15"

    # A 35-day range, Wed Sep 2 to Tue Oct 6, trends by week; its first and last weeks count only its days.
    add_review(conn, version, 4, missing(version, "sc06"), call="2026-08-31T15:00:00Z")            # Mon Aug 31
    add_review(conn, version, 5, all_met(version), agent=LEE, call="2026-08-31T16:00:00Z")
    add_review(conn, version, 6, all_met(version), call="2026-09-02T15:00:00Z")                    # Wed Sep 2
    add_review(conn, version, 7, missing(version, "sc06"), agent=LEE, call="2026-10-08T15:00:00Z")  # Thu Oct 8
    add_review(conn, version, 8, all_met(version), call="2026-08-25T15:00:00Z")                    # context
    trend = agent_page(conn, SAM[0], "custom", start=date(2026, 9, 2), end=date(2026, 10, 6))["trend"]
    steps = {s["start"]: s for s in trend["steps"]}
    assert trend["unit"] == "week" and len(trend["steps"]) == 8
    assert [s["partial"] for s in trend["steps"] if s["in_period"]] == [True, False, False, False, False, True]
    assert (steps["2026-08-30"]["calls"], steps["2026-08-30"]["agent"], steps["2026-08-30"]["team"]) == (
        1, 100.0, 100.0)                                         # Sep 2 only, not Aug 31's calls
    assert (steps["2026-10-04"]["calls"], steps["2026-10-04"]["team"]) == (1, 100.0)    # Oct 5, not Oct 8
    assert (steps["2026-08-23"]["calls"], steps["2026-08-23"]["partial"]) == (1, False)  # context, whole

    months = agent_page(conn, SAM[0], "custom", start=date(2026, 4, 1), end=date(2026, 10, 10))["trend"]
    assert months["unit"] == "month" and [s["start"] for s in months["steps"]][-2:] == ["2026-09-01", "2026-10-01"]
    assert (months["steps"][-2]["calls"], months["steps"][-2]["agent"]) == (1, 100.0)    # Sep 2


def test_agent_empty_period(conn, version):
    """Review Focus 2: an agent without calls in the period has no figures (None, never 0) and never crashes;
    the team's figures and line still show, and so does the context before the period."""
    add_review(conn, version, 1, missing(version, "sc06"), call="2026-10-14T15:00:00Z")            # Sam, Wed
    add_review(conn, version, 2, all_met(version), agent=LEE, call="2026-10-16T15:00:00Z")          # Lee, Fri
    conn.execute("INSERT INTO coaching_notes (agent_email, created_at, body) VALUES (?, ?, ?)",
                 (SAM[0], "2026-06-01T12:00:00Z", "Older than any period"))
    conn.commit()

    # Yesterday (Fri Oct 16): Sam had no calls, the team had Lee's.
    detail = agent_page(conn, SAM[0], "yesterday", last_reviewed=date(2026, 10, 16))
    assert detail["has_data_in_period"] is False
    assert detail["tiles"] == {"avg_percent": None, "team_avg_percent": 100.0, "problem_rate": None,
                               "team_problem_rate": 0.0, "contacts": None, "contacts_rank": None,
                               "biggest_gap": None}
    assert detail["checks"] and all(
        (c["agent_rate"], c["gap"], c["flagged"], c["applied_count"], c["team_rate"]) == (None, None, False, 0, 100.0)
        for c in detail["checks"])
    steps = detail["trend"]["steps"]
    assert (steps[-1]["calls"], steps[-1]["agent"], steps[-1]["team"]) == (0, None, 100.0)
    assert (steps[-3]["start"], steps[-3]["agent"]) == ("2026-10-14", 95.0)     # the context still shows
    assert detail["problems"] == []
    assert [n["body"] for n in detail["notes"]] == ["Older than any period"]    # notes ignore the period

    # This week so far on a Sunday before anything is reviewed: the period itself is empty.
    detail = agent_page(conn, SAM[0], "this_week", today=date(2026, 10, 18), last_reviewed=date(2026, 10, 17))
    assert detail["period"]["label"] == "This week so far (nothing reviewed yet)"
    assert detail["has_data_in_period"] is False
    assert set(detail["tiles"].values()) == {None} and detail["checks"] == [] and detail["problems"] == []
    steps = {s["start"]: s for s in detail["trend"]["steps"]}
    assert len(steps) == 14 and not any(s["in_period"] for s in steps.values())
    assert (steps["2026-10-14"]["agent"], steps["2026-10-16"]["team"]) == (95.0, 100.0)   # context only
    assert [n["body"] for n in detail["notes"]] == ["Older than any period"]


# Captured by running the unchanged pre-T-043 code (commit 306c7d7) on the demo database built at
# 2026-10-14 12:00Z: `queries.agent_detail(demo_conn, "sam.okafor@example.com")`, the default Agents page
# (8 weeks ending with the week of Oct 4). These figures already covered the 8 weeks and must not move.
PRE_T043_DEMO_SAM_AVG = {"avg_percent": 93.4, "team_avg_percent": 93.1}
PRE_T043_DEMO_SAM_BIGGEST_GAP = {"check_id": "sc12", "label": "#12 Escalation: Unable to identify root cause",
                                 "gap": -22.0}
# Weekly points: [week, agent, team, rubric_changed, model_changed].
PRE_T043_DEMO_SAM_WEEKLY = [
    ["2026-08-16", 92.5, 93.6, False, False], ["2026-08-23", 93.0, 93.7, False, False],
    ["2026-08-30", 93.6, 94.0, False, False], ["2026-09-06", 94.2, 93.0, False, False],
    ["2026-09-13", 94.3, 93.0, True, False], ["2026-09-20", 92.5, 94.4, False, False],
    ["2026-09-27", 95.0, 91.5, False, False], ["2026-10-04", 91.2, 91.5, False, False]]
# Checks: [check_id, agent_rate, team_rate, gap, flagged, applied_count].
PRE_T043_DEMO_SAM_CHECKS = [
    ["sc01", 86.0, 92.2, -6.2, False, 43], ["sc02", 95.3, 93.4, 1.9, False, 43],
    ["sc03", 97.7, 94.2, 3.5, False, 43], ["sc04", 97.7, 94.2, 3.5, False, 43],
    ["sc05", 93.0, 93.4, -0.4, False, 43], ["sc06", 88.4, 86.8, 1.6, False, 43],
    ["sc07", 93.0, 89.1, 3.9, False, 43], ["sc08", 93.0, 94.6, -1.6, False, 43],
    ["sc09", 97.7, 91.5, 6.2, False, 43], ["sc10", 93.0, 96.5, -3.5, False, 43],
    ["sc11", 100.0, 95.5, 4.5, False, 4], ["sc12", 75.0, 97.0, -22.0, True, 4],
    ["sc13", 100.0, 90.7, 9.3, False, 43], ["sc14", 90.7, 95.3, -4.6, False, 43],
    ["sc15", 74.4, 81.4, -7.0, False, 43], ["sc16", 93.0, 93.4, -0.4, False, 43],
    ["pr-call-length", 58.1, 53.9, 4.2, False, 43], ["pr-dropped-callback", 100.0, 90.0, 10.0, False, 7],
    ["pr-merge-correct", 87.5, 95.7, -8.2, False, 8], ["pr-scope", 95.3, 95.0, 0.3, False, 43],
    ["pr-email-qualities", 97.7, 95.7, 2.0, False, 43], ["pr-email-sent", 60.5, 72.5, -12.0, True, 43],
    ["pr-triage-macro", 88.4, 84.9, 3.5, False, 43]]
PRE_T043_DEMO_SAM_PROBLEMS = [
    50249, 50252, 50250, 50251, 50223, 50221, 50222, 50220, 50186, 50185, 50188, 50187, 50151, 50150, 50153,
    50152, 50149, 50154, 50148, 50114, 50118, 50115, 50117, 50119, 50087, 50084, 50083, 50082, 50088, 50086,
    50085, 50055, 50057, 50056, 50059, 50023, 50021, 50017, 50019, 50022, 50018]

# Changed on purpose: before T-043 the problem rate, ticket count and rank tiles covered only the last week;
# now they cover the period. Their 8-week values were captured at the same commit from the unchanged
# `queries.contact_rows` (counted calls from Aug 16 to Oct 10, by agent), so they are not hand-worked either.
PRE_T043_DEMO_PERIOD_CALLS = {"taylor.brooks@example.com": 46, "riley.chen@example.com": 45,
                              "morgan.reyes@example.com": 44, "jordan.pike@example.com": 44,
                              "sam.okafor@example.com": 43, "avery.stone@example.com": 36}
PRE_T043_DEMO_PERIOD_PROBLEMS = {"taylor.brooks@example.com": 38, "riley.chen@example.com": 38,
                                 "morgan.reyes@example.com": 44, "jordan.pike@example.com": 41,
                                 "sam.okafor@example.com": 41, "avery.stone@example.com": 32}
INTENDED_T043_DEMO_SAM_PERIOD_TILES = {
    "problem_rate": 95.3,          # 41 of Sam's 43 calls
    "team_problem_rate": 90.7,     # 234 of the team's 258
    "contacts": 43, "contacts_rank": 5}     # behind 46, 45, 44 and 44


def test_agent_last_8_weeks_matches_today(demo_conn):
    """Last 8 weeks reproduces the default Agents page from before periods (a regression guard); the three
    tiles that now cover the period on purpose match the period's calls."""
    def detail_of(email):
        return queries.agent_detail(demo_conn, email, demo_last_8_weeks(), last_reviewed=DEMO_FACTS["last_reviewed"])

    detail = detail_of("sam.okafor@example.com")
    assert detail["period"]["label"] == "Last 8 weeks (Aug 16 – Oct 10)"
    assert (detail["name"], detail["active"], detail["notes"]) == ("Sam Okafor", True, [])
    tiles = detail["tiles"]
    assert {k: tiles[k] for k in PRE_T043_DEMO_SAM_AVG} == PRE_T043_DEMO_SAM_AVG
    assert tiles["biggest_gap"] == PRE_T043_DEMO_SAM_BIGGEST_GAP
    trend = detail["trend"]
    assert trend["unit"] == "week"
    assert all(s["in_period"] and not s["so_far"] and not s["partial"] for s in trend["steps"])
    assert [[s["start"], s["agent"], s["team"], s["rubric_changed"], s["model_changed"]]
            for s in trend["steps"]] == PRE_T043_DEMO_SAM_WEEKLY
    assert [[c["check_id"], c["agent_rate"], c["team_rate"], c["gap"], c["flagged"], c["applied_count"]]
            for c in detail["checks"]] == PRE_T043_DEMO_SAM_CHECKS
    assert [p["ticket_id"] for p in detail["problems"]] == PRE_T043_DEMO_SAM_PROBLEMS

    assert {k: tiles[k] for k in INTENDED_T043_DEMO_SAM_PERIOD_TILES} == INTENDED_T043_DEMO_SAM_PERIOD_TILES
    for email, calls in PRE_T043_DEMO_PERIOD_CALLS.items():
        other = detail_of(email)["tiles"]
        assert other["contacts"] == calls, email
        assert other["problem_rate"] == round(PRE_T043_DEMO_PERIOD_PROBLEMS[email] / calls * 100, 1), email
        assert other["team_problem_rate"] == INTENDED_T043_DEMO_SAM_PERIOD_TILES["team_problem_rate"], email
