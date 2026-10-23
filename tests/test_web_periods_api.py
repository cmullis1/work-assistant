"""The period parameters of /api/overview and /api/agents/{email}, and the legacy ones they replace.
Synthetic data only; "today" is pinned to Saturday 2026-10-17 (tests/conftest.py)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from factories import add_run
from test_queries import LEE, SAM, add_review, all_met, missing, version  # noqa: F401  (fixture)
from work_assistant import periods
from work_assistant.web import app as webapp
from work_assistant.web import queries
from work_assistant.web.app import create_app

BASE = "http://127.0.0.1:8765"


def client(tmp_path):
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 4242,
                     zendesk_factory=lambda: None, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


def period(start, end, label, preset="custom", so_far=False):
    return {"start": start, "end": end, "label": label, "so_far": so_far, "preset": preset}


@pytest.fixture
def seeded(conn, version):
    """Calls in the weeks of Sep 20, Sep 27 and Oct 4 (America/Chicago); Oct 4 is last week."""
    add_review(conn, version, 1, all_met(version), agent=SAM, call="2026-10-05T15:00:00Z")
    add_review(conn, version, 2, missing(version, "sc06"), agent=LEE, call="2026-10-06T15:00:00Z")
    add_review(conn, version, 3, missing(version, "pr-call-length", "pr-scope"), agent=SAM,
               call="2026-09-28T15:00:00Z")
    add_review(conn, version, 4, all_met(version), agent=LEE, call="2026-09-21T15:00:00Z")
    return conn


def without_periods(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("period", "comparison")}


# The seeded facts: today is pinned to Oct 17; the last day reviewed is Oct 10, the end of the run that
# tests/test_queries.add_review records.
FACTS = {"today": date(2026, 10, 17), "last_reviewed": date(2026, 10, 10),
         "latest_week_with_data": date(2026, 10, 4)}


def agent_of(conn, email, chosen, last_reviewed=FACTS["last_reviewed"]):
    """`queries.agent_detail` for `chosen`, without its period (the endpoint's body adds the comparison)."""
    return without_periods(queries.agent_detail(conn, email, chosen, last_reviewed=last_reviewed))


def overview_of(conn, chosen, against="previous"):
    """`queries.overview` for `chosen` against its `against` comparison (a mode) or an explicit Period."""
    compare = periods.comparison(chosen, against) if isinstance(against, str) else against
    return queries.overview(conn, chosen, compare, last_reviewed=FACTS["last_reviewed"])


# -- the brief's tests ----------------------------------------------------------------------------------


def test_defaults_match_todays_views(seeded, tmp_path):
    """Ruling 6: with no parameters, the overview is Last week and Agents is Last 8 weeks, and each shows
    exactly what the views showed before periods (the latest week with data, and 8 weeks ending with it)."""
    api = client(tmp_path)
    body = api.get("/api/overview").json()
    assert body == overview_of(seeded, periods.resolve("last_week", **FACTS))
    assert body["period"] == period("2026-10-04", "2026-10-10", "Last week (Oct 4 – Oct 10)", "last_week")
    assert body["comparison"] == period("2026-09-27", "2026-10-03", "Sep 27 – Oct 3, 2026", "previous")

    body = api.get(f"/api/agents/{SAM[0]}").json()
    assert without_periods(body) == agent_of(seeded, SAM[0], periods.resolve("last_8_weeks", **FACTS))
    assert body["period"] == period("2026-08-16", "2026-10-10", "Last 8 weeks (Aug 16 – Oct 10)", "last_8_weeks")
    assert body["comparison"] == period("2026-06-21", "2026-08-15", "Jun 21 – Aug 15, 2026", "previous")


def test_agents_default_when_this_week_has_data(seeded, version, tmp_path):
    """With daily runs the latest week with data is this week: Last 8 weeks ends there, so far, as the
    Agents view did before."""
    add_review(seeded, version, 5, all_met(version), agent=SAM, call="2026-10-13T15:00:00Z")   # Tue Oct 13
    body = client(tmp_path).get(f"/api/agents/{SAM[0]}").json()
    facts = {"today": FACTS["today"], "last_reviewed": date(2026, 10, 13), "latest_week_with_data": date(2026, 10, 11)}
    assert without_periods(body) == agent_of(seeded, SAM[0], periods.resolve("last_8_weeks", **facts),
                                             last_reviewed=facts["last_reviewed"])
    assert body["period"] == period("2026-08-23", "2026-10-13", "Last 8 weeks so far (Aug 23 – Oct 13)",
                                    "last_8_weeks", so_far=True)


def test_legacy_params_map_to_periods(seeded, tmp_path):
    """Review Focus 5: old bookmarks' week, compare=<date> and weeks parameters open what they did."""
    api = client(tmp_path)
    days = {"today": FACTS["today"], "last_reviewed": FACTS["last_reviewed"]}
    body = api.get("/api/overview?week=2026-09-30&compare=2026-09-24").json()
    assert body == overview_of(seeded, periods.week_of(date(2026, 9, 30), **days),
                               periods.week_of(date(2026, 9, 24), **days))
    assert body["period"] == period("2026-09-27", "2026-10-03", "Week of Sep 27")
    assert body["comparison"] == period("2026-09-20", "2026-09-26", "Week of Sep 20")

    body = api.get("/api/overview?week=2026-09-20").json()
    assert body == overview_of(seeded, periods.week_of(date(2026, 9, 20), **days))
    assert body["comparison"] == period("2026-09-13", "2026-09-19", "Sep 13 – Sep 19, 2026", "previous")

    body = api.get(f"/api/agents/{SAM[0]}?weeks=8&week=2026-09-30").json()
    assert without_periods(body) == agent_of(seeded, SAM[0], periods.weeks_ending(8, date(2026, 9, 30), **days))
    assert body["period"] == period("2026-08-09", "2026-10-03", "Aug 9 – Oct 3, 2026")
    body = api.get(f"/api/agents/{SAM[0]}?weeks=4").json()
    assert without_periods(body) == agent_of(seeded, SAM[0], periods.weeks_ending(4, None, **FACTS))
    assert body["period"] == period("2026-09-13", "2026-10-10", "Sep 13 – Oct 10, 2026")
    body = api.get(f"/api/agents/{SAM[0]}?week=2026-09-30").json()            # 8 weeks, as before
    assert without_periods(body) == agent_of(seeded, SAM[0], periods.weeks_ending(8, date(2026, 9, 30), **days))
    assert body["period"]["start"] == "2026-08-09"


@pytest.mark.parametrize("path", ["/api/overview", f"/api/agents/{SAM[0]}"])
@pytest.mark.parametrize("query, message", [
    ("period=fortnight", "The period must be one of: yesterday, this_week, last_week, this_month, last_month, "
                         "last_8_weeks, last_6_months, last_year, custom."),
    ("period=custom", "Choose the start and end day of the custom range."),
    ("period=custom&start=2026-10-05", "Choose the start and end day of the custom range."),
    ("period=custom&start=2026-10-14&end=2026-10-05", "The start day must be on or before the end day."),
    ("period=custom&start=2026-10-4&end=2026-10-14", "The start day must be a date in the form YYYY-MM-DD."),
    ("period=custom&start=2026-10-04&end=soon", "The end day must be a date in the form YYYY-MM-DD."),
    ("period=last_8_weeks&compare=same_days_last_week", "Same days last week is only for periods of 7 days or less."),
    ("compare=sometimes", "The comparison must be one of: previous, same_days_last_week, custom, none, or a week's "
                          "date in the form YYYY-MM-DD."),
    ("compare=custom&compare_start=2026-09-01", "Choose the start and end day of the comparison."),
    ("compare=custom&compare_start=2026-09-14&compare_end=2026-09-01",
     "The comparison's start day must be on or before its end day."),
    ("compare=custom&compare_start=2026-9-01&compare_end=2026-09-14",
     "The comparison's start day must be a date in the form YYYY-MM-DD."),
    ("week=2026-13-01", "The week must be a date in the form YYYY-MM-DD."),
])
def test_bad_period_params_400(seeded, tmp_path, path, query, message):
    response = client(tmp_path).get(f"{path}?{query}")
    assert response.status_code == 400
    assert response.json() == {"detail": message}


# -- more -----------------------------------------------------------------------------------------------


def test_presets_and_comparisons_reach_the_response(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 7))     # Wednesday; Tue Oct 6 reviewed
    seeded.execute("UPDATE runs SET window_end = '2026-10-07T05:00:00Z'")  # the run covered through Tue
    seeded.commit()
    api = client(tmp_path)
    body = api.get("/api/overview?period=this_week").json()
    assert body["period"] == period("2026-10-04", "2026-10-06", "This week so far (Sun–Tue)", "this_week", True)
    assert body["comparison"] == period("2026-09-27", "2026-09-29", "Sep 27 – Sep 29, 2026", "previous")
    assert body["tiles"]["calls"] == {"count": 2, "compare_count": 1}        # Oct 5-6 against Sep 28
    body = api.get("/api/overview?period=yesterday&compare=same_days_last_week").json()
    assert body["period"] == period("2026-10-06", "2026-10-06", "Yesterday (Tue, Oct 6)", "yesterday")
    assert body["comparison"] == period("2026-09-29", "2026-09-29", "Tue, Sep 29, 2026", "same_days_last_week")
    body = api.get("/api/overview?period=last_month&compare=none").json()
    assert body["period"] == period("2026-09-01", "2026-09-30", "Last month (September 2026)", "last_month")
    assert body["comparison"] is None
    body = api.get(f"/api/agents/{LEE[0]}?period=custom&start=2026-09-21&end=2026-10-06&compare=custom"
                   "&compare_start=2026-09-01&compare_end=2026-09-14").json()
    assert body["period"] == period("2026-09-21", "2026-10-06", "Sep 21 – Oct 6, 2026")
    assert body["comparison"] == period("2026-09-01", "2026-09-14", "Sep 1 – Sep 14, 2026")
    steps = body["trend"]["steps"]       # 16 days, by day
    assert body["trend"]["unit"] == "day" and [s["start"] for s in steps][::15] == ["2026-09-21", "2026-10-06"]
    assert len(steps) == 16 and all(s["in_period"] for s in steps)
    # A period parameter wins over the legacy ones.
    body = api.get("/api/overview?period=last_week&week=2026-09-20").json()
    assert body["period"]["preset"] == "last_week" and body["period"]["start"] == "2026-09-27"
    assert body["tiles"]["calls"]["count"] == 1


def test_this_week_before_anything_is_reviewed_is_empty(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 11))    # Sunday; nothing yet this week
    body = client(tmp_path).get("/api/overview?period=this_week").json()
    assert body["period"] == period("2026-10-11", "2026-10-10", "This week so far (nothing reviewed yet)",
                                    "this_week", so_far=True)
    assert body["comparison"]["start"] == "2026-10-04" and body["comparison"]["end"] == "2026-10-03"
    assert body["has_data_in_period"] is False and body["tiles"]["calls"]["count"] is None


def test_period_facts_ignore_uncounted_and_test_reviews(conn, version, tmp_path, monkeypatch):
    """The last day reviewed and the latest week with data come from current, counted reviews only."""
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 8))
    add_review(conn, version, 1, all_met(version), call="2026-09-28T15:00:00Z")                   # Mon Sep 28
    add_review(conn, version, 2, all_met(version), counted=False, call="2026-10-05T15:00:00Z")
    add_review(conn, version, 3, all_met(version), current=False, call="2026-10-06T15:00:00Z")
    conn.execute("UPDATE runs SET window_start = '2026-09-27T05:00:00Z', window_end = '2026-09-29T05:00:00Z'")
    conn.commit()                                                      # the run covered Sep 27-28
    api = client(tmp_path)
    # The Sep 27 week, so far through Sep 28: the last day reviewed (final review fix 1).
    assert api.get(f"/api/agents/{SAM[0]}").json()["period"]["end"] == "2026-09-28"
    assert api.get("/api/overview?period=this_week").json()["period"]["end"] == "2026-10-03"   # empty


# -- fix round 1 ----------------------------------------------------------------------------------------


def test_future_dated_call_does_not_move_the_periods(seeded, version, tmp_path):
    """The last day reviewed is never after today, so a call dated Nov 5 can't push Last 8 weeks on."""
    add_review(seeded, version, 5, all_met(version), agent=SAM, call="2026-11-05T15:00:00Z")
    body = client(tmp_path).get(f"/api/agents/{SAM[0]}").json()
    assert body["period"] == period("2026-08-23", "2026-10-17", "Last 8 weeks so far (Aug 23 – Oct 17)",
                                    "last_8_weeks", so_far=True)


@pytest.mark.real_today
def test_real_today_follows_the_owners_time_zone(seeded, tmp_path, monkeypatch):
    """At 03:00 UTC on Oct 18 it is still Oct 17 in Chicago, so Yesterday is Oct 16."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 18, 3, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(webapp, "datetime", Clock)
    assert webapp._today("America/Chicago") == date(2026, 10, 17)
    assert webapp._today("UTC") == date(2026, 10, 18)
    body = client(tmp_path).get("/api/overview?period=yesterday").json()
    # Oct 16; nothing is reviewed after Oct 10 in the seeded data, so it is empty, ending the day before.
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-10-16", "2026-10-15")


@pytest.mark.parametrize("path", ["/api/overview", f"/api/agents/{SAM[0]}"])
@pytest.mark.parametrize("query", [
    "period=custom&start=1999-12-31&end=2000-01-03",
    "period=custom&start=2099-12-25&end=2100-01-01",
    "period=custom&start=0001-01-01&end=0001-01-03",
    "compare=custom&compare_start=1999-12-01&compare_end=2000-01-01",
    "week=1999-12-31",
    "compare=1999-12-31",
])
def test_dates_outside_2000_to_2099_400(seeded, tmp_path, path, query):
    response = client(tmp_path).get(f"{path}?{query}")
    assert response.status_code == 400
    assert response.json() == {"detail": "Choose dates between 2000 and 2099."}


@pytest.mark.parametrize("path", ["/api/overview", f"/api/agents/{SAM[0]}"])
def test_dates_at_the_edges_of_the_range_answer(seeded, tmp_path, path):
    api = client(tmp_path)
    body = api.get(f"{path}?period=custom&start=2000-01-01&end=2000-01-03"
                   "&compare=custom&compare_start=2000-01-01&compare_end=2099-12-31").json()
    assert body["period"] == period("2000-01-01", "2000-01-03", "Jan 1 – Jan 3, 2000")
    assert body["comparison"] == period("2000-01-01", "2099-12-31", "Jan 1, 2000 – Dec 31, 2099")
    body = api.get(f"{path}?period=custom&start=2099-12-25&end=2099-12-31").json()
    assert body["period"] == period("2099-12-25", "2099-12-24", "Dec 25 – Dec 31, 2099 (nothing reviewed yet)",
                                    so_far=True)


# -- fix round 2: finished runs count as reviewed -----------------------------------------------------------


def last_reviewed_setup(conn, version, monkeypatch):
    """Monday Oct 19; a complete daily run covered Sat Oct 17, which had a counted call."""
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 19))
    add_run(conn, kind="scheduled", window=("2026-10-17T05:00:00Z", "2026-10-18T05:00:00Z"))
    add_review(conn, version, 1, all_met(version), call="2026-10-17T15:00:00Z")


def this_week(tmp_path) -> tuple[str, str]:
    body = client(tmp_path).get("/api/overview?period=this_week").json()["period"]
    return body["start"], body["end"]


def test_complete_run_over_a_quiet_sunday_counts_as_reviewed(conn, version, tmp_path, monkeypatch):
    last_reviewed_setup(conn, version, monkeypatch)
    assert this_week(tmp_path) == ("2026-10-18", "2026-10-17")              # empty: Sunday not reviewed yet
    add_run(conn, kind="scheduled", window=("2026-10-18T05:00:00Z", "2026-10-19T05:00:00Z"))   # no calls
    assert this_week(tmp_path) == ("2026-10-18", "2026-10-18")
    body = client(tmp_path).get("/api/overview?period=this_week").json()
    assert body["period"]["label"] == "This week so far (Sun)"


@pytest.mark.parametrize("kind, status", [("manual", "complete"), ("rereview", "complete")])
def test_other_finished_review_runs_count(conn, version, tmp_path, monkeypatch, kind, status):
    last_reviewed_setup(conn, version, monkeypatch)
    add_run(conn, kind=kind, status=status, window=("2026-10-18T05:00:00Z", "2026-10-19T05:00:00Z"))
    assert this_week(tmp_path) == ("2026-10-18", "2026-10-18")


@pytest.mark.parametrize("kind, status", [("scheduled", "failed"), ("test", "complete"),
                                          ("manual", "awaiting_confirmation"), ("scheduled", "cancelled"),
                                          ("scheduled", "submitted")])
def test_unfinished_failed_and_test_runs_do_not_count(conn, version, tmp_path, monkeypatch, kind, status):
    last_reviewed_setup(conn, version, monkeypatch)
    add_run(conn, kind=kind, status=status, window=("2026-10-18T05:00:00Z", "2026-10-19T05:00:00Z"))
    assert this_week(tmp_path) == ("2026-10-18", "2026-10-17")


def test_run_covering_days_ahead_is_capped_at_today(conn, version, tmp_path, monkeypatch):
    last_reviewed_setup(conn, version, monkeypatch)
    add_run(conn, kind="manual", window=("2026-10-18T05:00:00Z", "2026-10-25T05:00:00Z"))
    assert this_week(tmp_path) == ("2026-10-18", "2026-10-19")
    # The latest week with data still comes from counted reviews only.
    assert client(tmp_path).get(f"/api/agents/{SAM[0]}").json()["period"]["start"] == "2026-08-23"
