from datetime import date, datetime, timedelta, timezone

import pytest

from work_assistant.timeutil import day_window, missed_days, previous_day, previous_week, week_start_local, week_window

CHI = "America/Chicago"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_week_window_dst():
    # DST ends Nov 1 02:00 inside this week, so the week is 7 days + 1 hour long.
    assert week_window(date(2026, 11, 4), CHI) == (utc(2026, 11, 1, 5), utc(2026, 11, 8, 6))
    assert week_window(date(2026, 10, 28), CHI) == (utc(2026, 10, 25, 5), utc(2026, 11, 1, 5))


def test_week_window_dst_spring_forward():
    # DST starts Mar 8 2026 02:00; the week starting Sunday Mar 8 begins in CST.
    assert week_window(date(2026, 3, 10), CHI) == (utc(2026, 3, 8, 6), utc(2026, 3, 15, 5))


def test_week_starts_sunday():
    assert week_window(date(2026, 10, 11), CHI)[0] == utc(2026, 10, 11, 5)


def test_saturday_is_last_day_of_week():
    start, end = week_window(date(2026, 10, 17), CHI)  # Saturday
    assert start == utc(2026, 10, 11, 5)
    assert end == utc(2026, 10, 18, 5)


def test_week_window_returns_aware_utc():
    start, end = week_window(date(2026, 10, 14), CHI)
    assert start.utcoffset().total_seconds() == 0
    assert end.utcoffset().total_seconds() == 0


def test_saturday_late_call_belongs_to_week():
    # Saturday Oct 10 23:30 CDT is in the week starting Sunday Oct 4.
    assert week_start_local("2026-10-11T04:30:00Z", CHI) == date(2026, 10, 4)


def test_sunday_midnight_call_starts_new_week():
    assert week_start_local("2026-10-11T05:00:00Z", CHI) == date(2026, 10, 11)


def test_previous_week_from_monday_run():
    # Monday Oct 19 06:00 CDT = 11:00Z; previous week is Sun Oct 11 - Sat Oct 17.
    assert previous_week(utc(2026, 10, 19, 11), CHI) == (utc(2026, 10, 11, 5), utc(2026, 10, 18, 5))


def test_previous_week_uses_local_date_not_utc_date():
    # Sunday Oct 18 21:00 CDT is already Monday Oct 19 02:00Z, but locally still Sunday Oct 18.
    assert previous_week(utc(2026, 10, 19, 2), CHI) == (utc(2026, 10, 11, 5), utc(2026, 10, 18, 5))


def test_previous_week_rejects_naive_datetime():
    with pytest.raises(ValueError):
        previous_week(datetime(2026, 10, 19, 6), CHI)


# -- days: the daily run reviews each complete local day ----------------------------------------------

MONDAY_RUN = utc(2026, 10, 19, 11)  # Monday Oct 19 06:00 CDT
YESTERDAY = (utc(2026, 10, 18, 5), utc(2026, 10, 19, 5))  # Sunday Oct 18


def chicago_day(month, day):
    """A whole CDT (UTC-5) day in 2026, as UTC bounds."""
    start = utc(2026, month, day, 5)
    return start, start + timedelta(days=1)


def test_day_window_is_one_local_day():
    assert day_window(date(2026, 10, 18), CHI) == YESTERDAY
    start, end = day_window(date(2026, 10, 14), CHI)
    assert start.utcoffset().total_seconds() == 0 and end.utcoffset().total_seconds() == 0


def test_day_window_dst_days_are_whole_local_days():
    # Spring forward (Mar 8 2026 02:00) makes a 23-hour day; fall back (Nov 1 2026 02:00) a 25-hour one.
    assert day_window(date(2026, 3, 8), CHI) == (utc(2026, 3, 8, 6), utc(2026, 3, 9, 5))
    assert day_window(date(2026, 11, 1), CHI) == (utc(2026, 11, 1, 5), utc(2026, 11, 2, 6))


def test_previous_day_uses_the_local_date():
    assert previous_day(MONDAY_RUN, CHI) == YESTERDAY
    # Oct 19 02:00 UTC is still Sunday Oct 18 in Chicago, so yesterday is Saturday Oct 17.
    assert previous_day(utc(2026, 10, 19, 2), CHI) == chicago_day(10, 17)


def test_previous_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        previous_day(datetime(2026, 10, 19, 6), CHI)


def test_missed_days_cases():
    # No prior scheduled run: just yesterday.
    assert missed_days(None, MONDAY_RUN, CHI, 90) == ([YESTERDAY], 0)
    # Covered up to today's local midnight: nothing to do.
    assert missed_days(utc(2026, 10, 19, 5), MONDAY_RUN, CHI, 90) == ([], 0)
    # Covered up to the start of Friday: Friday, Saturday and Sunday, oldest first.
    assert missed_days(utc(2026, 10, 16, 5), MONDAY_RUN, CHI, 90) == (
        [chicago_day(10, 16), chicago_day(10, 17), YESTERDAY], 0)
    # The retention cap: 30 days before the run is Sep 19 11:00Z, so the days from Sep 20 on are kept and
    # Sep 1 to Sep 19 (19 days) are skipped.
    days, skipped = missed_days(utc(2026, 9, 1, 5), MONDAY_RUN, CHI, 30)
    assert days[0] == chicago_day(9, 20) and days[-1] == YESTERDAY and len(days) == 29
    assert skipped == 19
    # Yesterday is always kept, however short the retention period.
    assert missed_days(None, MONDAY_RUN, CHI, 0) == ([YESTERDAY], 0)
    assert missed_days(utc(2026, 10, 16, 5), MONDAY_RUN, CHI, 0) == ([YESTERDAY], 2)


def test_missed_days_across_dst_changes_are_whole_local_days():
    days, skipped = missed_days(utc(2026, 10, 31, 5), utc(2026, 11, 3, 12), CHI, 90)
    assert days == [(utc(2026, 10, 31, 5), utc(2026, 11, 1, 5)),   # CDT
                    (utc(2026, 11, 1, 5), utc(2026, 11, 2, 6)),    # the 25-hour fall-back day
                    (utc(2026, 11, 2, 6), utc(2026, 11, 3, 6))]    # CST
    assert skipped == 0
    days, _ = missed_days(utc(2026, 3, 7, 6), utc(2026, 3, 10, 12), CHI, 90)
    assert days == [(utc(2026, 3, 7, 6), utc(2026, 3, 8, 6)),       # CST
                    (utc(2026, 3, 8, 6), utc(2026, 3, 9, 5)),       # the 23-hour spring-forward day
                    (utc(2026, 3, 9, 5), utc(2026, 3, 10, 5))]      # CDT


def test_missed_days_after_a_weekly_window():
    # The last weekly run covered Sunday Oct 4 to Saturday Oct 10; the first daily run is Wednesday Oct 14.
    days, _ = missed_days(utc(2026, 10, 11, 5), utc(2026, 10, 14, 11), CHI, 90)
    assert days == [chicago_day(10, 11), chicago_day(10, 12), chicago_day(10, 13)]


def test_missed_days_partly_covered_day_is_reviewed_again():
    # A covered period ending mid-day (say, after a time zone change) leaves that day in the list;
    # tickets already reviewed in it are skipped by the run itself.
    assert missed_days(utc(2026, 10, 18, 17), MONDAY_RUN, CHI, 90) == ([YESTERDAY], 0)


def test_missed_days_rejects_naive_now():
    with pytest.raises(ValueError):
        missed_days(None, datetime(2026, 10, 19, 6), CHI, 90)


def test_date_range_window_includes_last_day():
    from work_assistant.timeutil import date_range_window

    assert date_range_window(date(2026, 10, 4), date(2026, 10, 10), CHI) == (utc(2026, 10, 4, 5), utc(2026, 10, 11, 5))
    assert date_range_window(date(2026, 10, 31), date(2026, 11, 1), CHI) == (utc(2026, 10, 31, 5), utc(2026, 11, 2, 6))
    with pytest.raises(ValueError):
        date_range_window(date(2026, 10, 5), date(2026, 10, 4), CHI)
