"""The period model: presets, comparisons, trend steps and UTC windows. Pure: no database."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from work_assistant import periods
from work_assistant.periods import Period, Step

TODAY = date(2026, 10, 21)          # a Wednesday
LAST_REVIEWED = date(2026, 10, 20)  # yesterday, as after the morning run
LATEST_WEEK = date(2026, 10, 18)    # this week has data (the daily runs)
TZ = "America/Chicago"


def resolve(preset, **kwargs):
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("last_reviewed", LAST_REVIEWED)
    kwargs.setdefault("latest_week_with_data", date(2026, 10, 11))
    return periods.resolve(preset, **kwargs)


def days(period):
    return (period.start, period.end)


# -- the brief's tests ----------------------------------------------------------------------------------


def test_presets_resolve():
    assert resolve("yesterday") == Period(date(2026, 10, 20), date(2026, 10, 20), "yesterday",
                                          "Yesterday (Tue, Oct 20)", False)
    assert resolve("this_week") == Period(date(2026, 10, 18), date(2026, 10, 20), "this_week",
                                          "This week so far (Sun–Tue)", True, 1)
    assert resolve("last_week") == Period(date(2026, 10, 11), date(2026, 10, 17), "last_week",
                                          "Last week (Oct 11 – Oct 17)", False, 1)
    assert resolve("this_month") == Period(date(2026, 10, 1), date(2026, 10, 20), "this_month",
                                           "This month so far (Oct 1 – Oct 20)", True)
    assert resolve("last_month") == Period(date(2026, 9, 1), date(2026, 9, 30), "last_month",
                                           "Last month (September 2026)", False)
    assert resolve("last_8_weeks") == Period(date(2026, 8, 23), date(2026, 10, 17), "last_8_weeks",
                                             "Last 8 weeks (Aug 23 – Oct 17)", False, 8)
    assert resolve("custom", start=date(2026, 10, 5), end=date(2026, 10, 14)) == Period(
        date(2026, 10, 5), date(2026, 10, 14), "custom", "Oct 5 – Oct 14, 2026", False)
    assert resolve("custom", start=date(2026, 10, 19), end=date(2026, 10, 19)).label == "Mon, Oct 19, 2026"
    assert resolve("custom", start=date(2025, 12, 28), end=date(2026, 1, 3)).label == "Dec 28, 2025 – Jan 3, 2026"
    assert periods.resolve("last_week", today=date(2026, 1, 7), last_reviewed=None,
                           latest_week_with_data=None).label == "Last week (Dec 28 – Jan 3)"


def test_comparisons():
    previous = periods.comparison
    # The period before: the same length immediately before.
    assert days(previous(resolve("yesterday"), "previous")) == (date(2026, 10, 19), date(2026, 10, 19))
    ten = resolve("custom", start=date(2026, 10, 5), end=date(2026, 10, 14))
    assert days(previous(ten, "previous")) == (date(2026, 9, 25), date(2026, 10, 4))
    assert days(previous(resolve("last_8_weeks"), "previous")) == (date(2026, 6, 28), date(2026, 8, 22))
    # Last week against the week before; last month against the calendar month before.
    assert days(previous(resolve("last_week"), "previous")) == (date(2026, 10, 4), date(2026, 10, 10))
    assert days(previous(resolve("last_month"), "previous")) == (date(2026, 8, 1), date(2026, 8, 31))
    # So far: the same weekdays last week, and last month's 1st through the same day.
    assert days(previous(resolve("this_week"), "previous")) == (date(2026, 10, 11), date(2026, 10, 13))
    assert days(previous(resolve("this_month"), "previous")) == (date(2026, 9, 1), date(2026, 9, 20))
    # Same days last week: back 7 days, for 7 days or less only.
    assert days(previous(resolve("yesterday"), "same_days_last_week")) == (date(2026, 10, 13), date(2026, 10, 13))
    assert days(previous(resolve("last_week"), "same_days_last_week")) == (date(2026, 10, 4), date(2026, 10, 10))
    with pytest.raises(periods.PeriodError, match="^Same days last week is only for periods of 7 days or less.$"):
        previous(ten, "same_days_last_week")
    # A custom comparison, and none.
    custom = previous(ten, "custom", start=date(2026, 9, 1), end=date(2026, 9, 14))
    assert custom == Period(date(2026, 9, 1), date(2026, 9, 14), "custom", "Sep 1 – Sep 14, 2026", False)
    assert previous(ten, "none") is None
    assert previous(ten, "previous").preset == "previous" and previous(ten, "previous").label == "Sep 25 – Oct 4, 2026"


def test_period_edges_dst_and_month_lengths():
    """Review Focus 1."""
    march = periods.resolve("this_month", today=date(2026, 3, 31), last_reviewed=date(2026, 3, 31),
                            latest_week_with_data=None)
    assert days(march) == (date(2026, 3, 1), date(2026, 3, 31))
    assert days(periods.comparison(march, "previous")) == (date(2026, 2, 1), date(2026, 2, 28))
    leap = periods.resolve("this_month", today=date(2028, 3, 31), last_reviewed=date(2028, 3, 30),
                           latest_week_with_data=None)
    assert days(periods.comparison(leap, "previous")) == (date(2028, 2, 1), date(2028, 2, 29))
    may = periods.resolve("this_month", today=date(2026, 5, 31), last_reviewed=date(2026, 5, 31),
                          latest_week_with_data=None)
    assert days(periods.comparison(may, "previous")) == (date(2026, 4, 1), date(2026, 4, 30))

    # Clocks go back at 2:00 on Sunday Nov 1, 2026 in Chicago: that day is 25 hours long.
    crossing = resolve("custom", start=date(2026, 10, 31), end=date(2026, 11, 2), today=date(2026, 12, 1),
                       last_reviewed=date(2026, 11, 30))
    start, end = periods.to_window(crossing, TZ)
    assert start == datetime(2026, 10, 31, 5, tzinfo=timezone.utc)       # midnight CDT
    assert end == datetime(2026, 11, 3, 6, tzinfo=timezone.utc)          # midnight CST
    assert end - start == timedelta(hours=3 * 24 + 1)
    spring = resolve("custom", start=date(2026, 3, 8), end=date(2026, 3, 8))
    start, end = periods.to_window(spring, TZ)
    assert (start, end - start) == (datetime(2026, 3, 8, 6, tzinfo=timezone.utc), timedelta(hours=23))


def test_trend_steps():
    yesterday = resolve("yesterday")
    assert periods.trend_unit(yesterday) == "day"
    steps = periods.trend_steps(yesterday, "day", last_reviewed=LAST_REVIEWED)
    assert len(steps) == 14 and steps[0].start == date(2026, 10, 7)
    assert steps[-1] == Step(date(2026, 10, 20), date(2026, 10, 20), True, False)
    assert not any(s.in_period for s in steps[:-1])

    eight = resolve("last_8_weeks")
    assert periods.trend_unit(eight) == "week"
    steps = periods.trend_steps(eight, "week", last_reviewed=LAST_REVIEWED)
    assert len(steps) == 8 and all(s.in_period for s in steps)
    assert (steps[0].start, steps[-1].end) == (date(2026, 8, 23), date(2026, 10, 17))
    assert all(s.start.weekday() == 6 and s.end == s.start + timedelta(days=6) for s in steps)

    forty = resolve("custom", start=date(2026, 6, 3), end=date(2026, 7, 12))   # Wed Jun 3 to Sun Jul 12
    assert periods.trend_unit(forty) == "week"
    steps = periods.trend_steps(forty, "week", last_reviewed=LAST_REVIEWED)
    assert len(steps) >= 8 and steps[0].start <= forty.start and steps[-1].end >= forty.end
    assert steps[-1].start == date(2026, 7, 12) and all(s.in_period for s in steps if s.end >= forty.start)
    assert not any(s.in_period for s in steps if s.end < forty.start)

    long = resolve("custom", start=date(2026, 1, 15), end=date(2026, 8, 2))   # 200 days
    assert periods.trend_unit(long) == "month"
    steps = periods.trend_steps(long, "month", last_reviewed=LAST_REVIEWED)
    assert [s.start for s in steps] == [date(2026, m, 1) for m in range(1, 9)]
    assert steps[1].end == date(2026, 2, 28) and steps[-1].end == date(2026, 8, 31)
    assert all(s.in_period for s in steps) and not any(s.so_far for s in steps)


# -- more -----------------------------------------------------------------------------------------------


def test_so_far_period_with_nothing_reviewed_is_empty():
    """On Sunday morning before any run, This week so far ends the day before it starts; its comparison is
    empty too, never None (None means the owner chose Nothing)."""
    sunday = periods.resolve("this_week", today=date(2026, 10, 18), last_reviewed=date(2026, 10, 17),
                             latest_week_with_data=date(2026, 10, 11))
    assert days(sunday) == (date(2026, 10, 18), date(2026, 10, 17)) and sunday.empty and sunday.so_far
    assert sunday.label == "This week so far (nothing reviewed yet)"
    before = periods.comparison(sunday, "previous")
    assert before is not None and before.empty and before.start == date(2026, 10, 11)
    assert periods.comparison(sunday, "same_days_last_week").empty
    start, end = periods.to_window(sunday, TZ)
    assert start == end == datetime(2026, 10, 18, 5, tzinfo=timezone.utc)
    never = periods.resolve("this_month", today=TODAY, last_reviewed=None, latest_week_with_data=None)
    assert never.empty and never.label == "This month so far (nothing reviewed yet)"


def test_so_far_ends_at_today_or_last_reviewed():
    ran_today = resolve("this_week", last_reviewed=TODAY)
    assert days(ran_today) == (date(2026, 10, 18), TODAY) and ran_today.label == "This week so far (Sun–Wed)"
    one_day = resolve("this_week", last_reviewed=date(2026, 10, 18))
    assert one_day.label == "This week so far (Sun)"
    behind = resolve("this_month", last_reviewed=date(2026, 9, 29))
    assert behind.empty


def test_last_8_weeks_follows_latest_week_with_data():
    assert days(resolve("last_8_weeks", latest_week_with_data=date(2026, 9, 27))) == (
        date(2026, 8, 9), date(2026, 10, 3))
    assert days(resolve("last_8_weeks", latest_week_with_data=None)) == (date(2026, 8, 23), date(2026, 10, 17))
    # The latest week with data is this week: it ends at the last day reviewed, so far.
    current = resolve("last_8_weeks", latest_week_with_data=LATEST_WEEK)
    assert days(current) == (date(2026, 8, 30), LAST_REVIEWED) and current.so_far
    assert current.label == "Last 8 weeks so far (Aug 30 – Oct 20)"
    assert days(periods.comparison(current, "previous")) == (date(2026, 7, 5), date(2026, 8, 25))
    steps = periods.trend_steps(current, periods.trend_unit(current), last_reviewed=LAST_REVIEWED)
    assert len(steps) == 8 and steps[-1] == Step(date(2026, 10, 18), date(2026, 10, 24), True, True)
    assert not any(s.so_far for s in steps[:-1])


def test_custom_range_reaching_today_is_so_far():
    reaching = resolve("custom", start=date(2026, 10, 12), end=date(2026, 10, 31))
    assert days(reaching) == (date(2026, 10, 12), LAST_REVIEWED) and reaching.so_far
    assert reaching.label == "Oct 12 – Oct 20, 2026 (so far)"
    future = resolve("custom", start=date(2026, 10, 25), end=date(2026, 10, 31))
    assert future.empty and future.label == "Oct 25 – Oct 31, 2026 (nothing reviewed yet)"
    with pytest.raises(periods.PeriodError, match="^The start day must be on or before the end day.$"):
        resolve("custom", start=date(2026, 10, 14), end=date(2026, 10, 5))
    with pytest.raises(periods.PeriodError, match="^Choose the start and end day of the custom range.$"):
        resolve("custom", start=date(2026, 10, 14))
    with pytest.raises(periods.PeriodError, match="^The period must be one of: yesterday, this_week, last_week, "
                                                  "this_month, last_month, last_8_weeks, last_6_months, last_year, custom.$"):
        resolve("fortnight")


def test_bad_comparisons():
    week = resolve("last_week")
    with pytest.raises(periods.PeriodError, match="^Choose the start and end day of the comparison.$"):
        periods.comparison(week, "custom", start=date(2026, 9, 1))
    with pytest.raises(periods.PeriodError, match="^The comparison's start day must be on or before its end day.$"):
        periods.comparison(week, "custom", start=date(2026, 9, 14), end=date(2026, 9, 1))
    with pytest.raises(periods.PeriodError, match="^The comparison must be one of: previous, same_days_last_week, "
                                                  "custom, none.$"):
        periods.comparison(week, "yearly")


def test_week_helpers_for_legacy_addresses():
    week = periods.week_of(date(2026, 10, 7), today=TODAY, last_reviewed=LAST_REVIEWED)
    assert week == Period(date(2026, 10, 4), date(2026, 10, 10), "custom", "Week of Oct 4", False, 1)
    current = periods.week_of(date(2026, 10, 19), today=TODAY, last_reviewed=LAST_REVIEWED)
    assert days(current) == (date(2026, 10, 18), LAST_REVIEWED) and current.so_far
    assert current.label == "Week of Oct 18 (so far)"
    four = periods.weeks_ending(4, date(2026, 10, 7), today=TODAY, last_reviewed=LAST_REVIEWED)
    assert four == Period(date(2026, 9, 13), date(2026, 10, 10), "custom", "Sep 13 – Oct 10, 2026", False, 4)
    # Without a week: the latest week with data, else last week (as Last 8 weeks).
    assert days(periods.weeks_ending(2, None, today=TODAY, last_reviewed=LAST_REVIEWED)) == (
        date(2026, 10, 4), date(2026, 10, 17))
    assert days(periods.weeks_ending(2, None, today=TODAY, last_reviewed=LAST_REVIEWED,
                                     latest_week_with_data=date(2026, 9, 27))) == (date(2026, 9, 20), date(2026, 10, 3))


def test_day_steps_cover_a_long_day_period_and_week_steps_need_eight():
    month = resolve("last_month")                                  # 30 days: day steps, the whole month
    steps = periods.trend_steps(month, periods.trend_unit(month), last_reviewed=LAST_REVIEWED)
    assert len(steps) == 30 and all(s.in_period for s in steps)
    assert periods.trend_unit(resolve("custom", start=date(2026, 1, 1), end=date(2026, 1, 31))) == "day"
    assert periods.trend_unit(resolve("custom", start=date(2026, 1, 1), end=date(2026, 2, 1))) == "week"
    assert periods.trend_unit(resolve("custom", start=date(2026, 1, 4), end=date(2026, 7, 4))) == "week"  # 26 weeks
    assert periods.trend_unit(resolve("custom", start=date(2026, 1, 4), end=date(2026, 7, 5))) == "month"
    empty = periods.resolve("this_week", today=date(2026, 10, 18), last_reviewed=date(2026, 10, 17),
                            latest_week_with_data=None)
    steps = periods.trend_steps(empty, periods.trend_unit(empty), last_reviewed=date(2026, 10, 17))
    assert len(steps) == 14 and steps[-1].end == date(2026, 10, 17) and not any(s.in_period for s in steps)


def test_period_as_dict():
    assert resolve("last_week").as_dict() == {"start": "2026-10-11", "end": "2026-10-17",
                                              "label": "Last week (Oct 11 – Oct 17)", "so_far": False,
                                              "preset": "last_week"}
    assert resolve("yesterday").days == 1 and resolve("last_week").days == 7


# -- fix round 1 ----------------------------------------------------------------------------------------


def test_last_step_is_so_far_until_the_run_reaches_it():
    """Ruling 3: the last step is so far when it ends after the last day reviewed. Before the morning run
    a period past the last day reviewed is so far itself (final review fix 1), so its day steps stop at
    the last day reviewed and only a week step can run past it."""
    before_run = resolve("yesterday", last_reviewed=date(2026, 10, 19))
    steps = periods.trend_steps(before_run, "day", last_reviewed=date(2026, 10, 19))
    assert steps[-1].end == date(2026, 10, 19) and not any(s.so_far or s.in_period for s in steps)

    sunday = dict(today=date(2026, 10, 18), latest_week_with_data=date(2026, 10, 11))
    last_week = periods.resolve("last_week", last_reviewed=date(2026, 10, 16), **sunday)   # Fri reviewed
    steps = periods.trend_steps(last_week, periods.trend_unit(last_week), last_reviewed=date(2026, 10, 16))
    assert steps[-1] == Step(date(2026, 10, 16), date(2026, 10, 16), True, False)
    weekly = periods.trend_steps(last_week, "week", last_reviewed=date(2026, 10, 16))
    assert weekly[-1] == Step(date(2026, 10, 11), date(2026, 10, 17), True, True)
    whole = periods.resolve("last_week", last_reviewed=date(2026, 10, 17), **sunday)
    assert not any(s.so_far for s in periods.trend_steps(whole, "day", last_reviewed=date(2026, 10, 17)))
    assert not any(s.so_far for s in periods.trend_steps(whole, "day", last_reviewed=None))


def test_legacy_weeks_in_progress_compare_by_whole_weeks():
    current = periods.week_of(date(2026, 10, 19), today=TODAY, last_reviewed=LAST_REVIEWED)    # Sun-Tue
    assert days(periods.comparison(current, "previous")) == (date(2026, 10, 11), date(2026, 10, 13))
    four = periods.weeks_ending(4, date(2026, 10, 19), today=TODAY, last_reviewed=LAST_REVIEWED)
    assert days(four) == (date(2026, 9, 27), LAST_REVIEWED) and four.so_far
    assert days(periods.comparison(four, "previous")) == (date(2026, 8, 30), date(2026, 9, 22))
    # A custom range that only happens to start on a Sunday still compares with the days just before it.
    sunday_start = resolve("custom", start=date(2026, 10, 18), end=date(2026, 10, 24))
    assert days(periods.comparison(sunday_start, "previous")) == (date(2026, 10, 15), date(2026, 10, 17))


def test_custom_dates_between_2000_and_2099():
    message = "^Choose dates between 2000 and 2099.$"
    edges = resolve("custom", start=date(2000, 1, 1), end=date(2000, 1, 3))
    assert days(periods.comparison(edges, "previous")) == (date(1999, 12, 29), date(1999, 12, 31))
    assert periods.trend_steps(edges, "day", last_reviewed=LAST_REVIEWED)[0].start == date(1999, 12, 21)
    far = periods.resolve("custom", start=date(2099, 12, 25), end=date(2099, 12, 31), today=date(2100, 1, 1),
                          last_reviewed=None, latest_week_with_data=None)
    assert periods.to_window(far, TZ)[1] == datetime(2100, 1, 1, 6, tzinfo=timezone.utc)
    for bad in ({"start": date(1999, 12, 31), "end": date(2000, 1, 3)},
                {"start": date(2099, 12, 25), "end": date(2100, 1, 1)},
                {"start": date(1, 1, 1), "end": date(1, 1, 3)}):
        with pytest.raises(periods.PeriodError, match=message):
            resolve("custom", **bad)
    with pytest.raises(periods.PeriodError, match=message):
        periods.comparison(edges, "custom", start=date(1999, 12, 1), end=date(1999, 12, 31))
    with pytest.raises(periods.PeriodError, match=message):
        periods.week_of(date(1999, 12, 31), today=TODAY, last_reviewed=None)
    with pytest.raises(periods.PeriodError, match=message):
        periods.weeks_ending(4, date(2100, 1, 1), today=TODAY, last_reviewed=None)


# -- final review -----------------------------------------------------------------------------------------


def test_yesterday_waits_for_the_morning_run():
    """Before the morning run reviews yesterday, Yesterday is so far and empty: nothing reviewed yet, not
    "no calls". Its comparison is empty too, never None."""
    before_run = resolve("yesterday", last_reviewed=date(2026, 10, 19))
    assert days(before_run) == (date(2026, 10, 20), date(2026, 10, 19)) and before_run.empty and before_run.so_far
    assert before_run.label == "Yesterday (nothing reviewed yet)"
    before = periods.comparison(before_run, "previous")
    assert before is not None and before.empty
    assert periods.comparison(before_run, "same_days_last_week").empty
    start, end = periods.to_window(before_run, TZ)
    assert start == end == datetime(2026, 10, 20, 5, tzinfo=timezone.utc)
    # After the run it is whole again.
    assert resolve("yesterday") == Period(date(2026, 10, 20), date(2026, 10, 20), "yesterday",
                                          "Yesterday (Tue, Oct 20)", False)


def test_last_week_on_sunday_before_the_run_is_so_far():
    sunday = dict(today=date(2026, 10, 18), latest_week_with_data=date(2026, 10, 11))
    week = periods.resolve("last_week", last_reviewed=date(2026, 10, 16), **sunday)          # Fri reviewed
    assert week == Period(date(2026, 10, 11), date(2026, 10, 16), "last_week", "Last week so far (Sun–Fri)",
                          True, 1)
    # Compared weekday for weekday, as This week so far.
    assert days(periods.comparison(week, "previous")) == (date(2026, 10, 4), date(2026, 10, 9))
    assert days(periods.comparison(week, "same_days_last_week")) == (date(2026, 10, 4), date(2026, 10, 9))
    behind = periods.resolve("last_week", last_reviewed=date(2026, 10, 10), **sunday)
    assert behind.empty and behind.label == "Last week so far (nothing reviewed yet)"
    assert periods.resolve("last_week", last_reviewed=date(2026, 10, 17), **sunday).label == \
        "Last week (Oct 11 – Oct 17)"


def test_last_month_on_the_first_before_the_run_is_so_far():
    first = dict(today=date(2026, 12, 1), latest_week_with_data=date(2026, 11, 22))
    month = periods.resolve("last_month", last_reviewed=date(2026, 11, 29), **first)
    assert month == Period(date(2026, 11, 1), date(2026, 11, 29), "last_month",
                           "Last month so far (Nov 1 – Nov 29)", True)
    # Compared with the month before's 1st through the same day, as This month so far.
    assert days(periods.comparison(month, "previous")) == (date(2026, 10, 1), date(2026, 10, 29))
    behind = periods.resolve("last_month", last_reviewed=date(2026, 10, 31), **first)
    assert behind.empty and behind.label == "Last month so far (nothing reviewed yet)"
    assert periods.comparison(behind, "previous").empty
    # Clamped to a shorter month's last day.
    march = periods.resolve("last_month", today=date(2026, 4, 1), last_reviewed=date(2026, 3, 30),
                            latest_week_with_data=None)
    assert days(periods.comparison(march, "previous")) == (date(2026, 2, 1), date(2026, 2, 28))
    assert periods.resolve("last_month", last_reviewed=date(2026, 11, 30), **first).label == \
        "Last month (November 2026)"


def test_custom_range_past_the_last_day_reviewed_is_so_far():
    behind = resolve("custom", start=date(2026, 10, 12), end=date(2026, 10, 18), last_reviewed=date(2026, 10, 16))
    assert days(behind) == (date(2026, 10, 12), date(2026, 10, 16)) and behind.label == "Oct 12 – Oct 16, 2026 (so far)"
    # With nothing reviewed at all, a past period is whole: there is no last day reviewed to stop at.
    never = resolve("custom", start=date(2026, 10, 12), end=date(2026, 10, 18), last_reviewed=None)
    assert days(never) == (date(2026, 10, 12), date(2026, 10, 18)) and not never.so_far


# -- whole-month periods (T-132) ----------------------------------------------------------------------------


def test_last_6_months_and_last_year_are_whole_months_before_this_one():
    assert resolve("last_6_months") == Period(date(2026, 4, 1), date(2026, 9, 30), "last_6_months",
                                              "Last 6 months (Apr – Sep 2026)", False)
    assert resolve("last_year") == Period(date(2025, 10, 1), date(2026, 9, 30), "last_year",
                                          "Last year (Oct 2025 – Sep 2026)", False)
    january = periods.resolve("last_6_months", today=date(2027, 1, 10), last_reviewed=date(2027, 1, 9),
                              latest_week_with_data=None)
    assert days(january) == (date(2026, 7, 1), date(2026, 12, 31))
    assert january.label == "Last 6 months (Jul – Dec 2026)"


def test_whole_month_periods_compare_with_the_same_months_before():
    assert days(periods.comparison(resolve("last_6_months"), "previous")) == (date(2025, 10, 1), date(2026, 3, 31))
    assert days(periods.comparison(resolve("last_year"), "previous")) == (date(2024, 10, 1), date(2025, 9, 30))


def test_whole_month_periods_before_the_morning_run_are_so_far():
    early = periods.resolve("last_6_months", today=date(2026, 10, 1), last_reviewed=date(2026, 9, 29),
                            latest_week_with_data=None)
    assert days(early) == (date(2026, 4, 1), date(2026, 9, 29)) and early.so_far
    assert early.label == "Last 6 months so far (Apr 1 – Sep 29)"
    assert days(periods.comparison(early, "previous")) == (date(2025, 10, 1), date(2026, 3, 29))


def test_whole_month_periods_trend_by_month():
    for preset in ("last_6_months", "last_year"):
        assert periods.trend_unit(resolve(preset)) == "month"
    short = periods.resolve("last_6_months", today=date(2027, 3, 5), last_reviewed=date(2027, 3, 4),
                            latest_week_with_data=None)
    assert short.days == 181 and periods.trend_unit(short) == "month", "Sep to Feb is 181 days, still months"
