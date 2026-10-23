from datetime import date

from work_assistant.oneonone.cycle import (
    CYCLE_DAYS, HISTORY_PERIODS, MAX_WEEKS, PrepPeriod, Schedule, history_for, is_tech_day, latest_day, next_day,
    period_for, tech_days,
)

# The owner's real schedules, all every 2 weeks.
DESMOND = Schedule(date(2026, 10, 20), 2)
SAAHIL = Schedule(date(2026, 10, 21), 2)
RUBEN = Schedule(date(2026, 10, 22), 2)
ZANE = Schedule(date(2026, 10, 28), 2)
TODAY = date(2026, 10, 20)


def test_constants():
    assert CYCLE_DAYS == 14
    assert HISTORY_PERIODS == 26
    assert MAX_WEEKS == 8


def test_tech_days_every_two_weeks():
    first, last = date(2026, 10, 1), date(2026, 11, 30)
    expected = [date(2026, 10, 7), date(2026, 10, 21), date(2026, 11, 4), date(2026, 11, 18)]
    assert tech_days(SAAHIL, first, last) == expected
    assert tech_days(Schedule(date(2026, 11, 18), 2), first, last) == expected  # a future date works back
    assert tech_days(Schedule(date(2027, 3, 10), 2), first, last) == expected
    assert tech_days(SAAHIL, date(2026, 10, 21), date(2026, 10, 21)) == [date(2026, 10, 21)]  # ends count
    assert tech_days(SAAHIL, date(2026, 10, 22), date(2026, 11, 3)) == []
    assert is_tech_day(SAAHIL, date(2026, 10, 21))
    assert is_tech_day(SAAHIL, date(2026, 10, 7))  # before the entered date
    assert is_tech_day(SAAHIL, date(2026, 11, 4))
    assert not is_tech_day(SAAHIL, date(2026, 10, 20))
    assert not is_tech_day(SAAHIL, date(2026, 10, 28))  # a week off


def test_latest_and_next_day():
    assert (latest_day(SAAHIL, TODAY), next_day(SAAHIL, TODAY)) == (date(2026, 10, 7), date(2026, 10, 21))
    assert (latest_day(DESMOND, TODAY), next_day(DESMOND, TODAY)) == (date(2026, 10, 20), date(2026, 11, 3))
    assert (latest_day(ZANE, TODAY), next_day(ZANE, TODAY)) == (date(2026, 10, 14), date(2026, 10, 28))
    assert (latest_day(RUBEN, TODAY), next_day(RUBEN, TODAY)) == (date(2026, 10, 8), date(2026, 10, 22))


def test_history_for_counts_back_from_latest():
    assert history_for(DESMOND, TODAY, n=3) == [date(2026, 9, 8), date(2026, 9, 22), date(2026, 10, 6)]
    assert history_for(SAAHIL, TODAY, n=2) == [date(2026, 9, 9), date(2026, 9, 23)]  # latest is 10/7
    year = history_for(DESMOND, TODAY)
    assert len(year) == HISTORY_PERIODS
    assert year[0] == date(2025, 10, 21) and year[-1] == date(2026, 10, 6)
    assert year == sorted(year)


def test_weekly_and_four_weekly_repeats():
    weekly = Schedule(date(2026, 10, 20), 1)
    assert tech_days(weekly, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27)]
    assert latest_day(weekly, date(2026, 10, 22)) == date(2026, 10, 20)
    assert next_day(weekly, date(2026, 10, 22)) == date(2026, 10, 27)
    assert history_for(weekly, date(2026, 10, 22), n=2) == [date(2026, 10, 6), date(2026, 10, 13)]
    assert is_tech_day(weekly, date(2026, 10, 27))

    four = Schedule(date(2026, 10, 20), 4)
    assert tech_days(four, date(2026, 9, 1), date(2026, 12, 31)) == [
        date(2026, 9, 22), date(2026, 10, 20), date(2026, 11, 17), date(2026, 12, 15)]
    assert latest_day(four, date(2026, 11, 16)) == date(2026, 10, 20)
    assert next_day(four, date(2026, 11, 16)) == date(2026, 11, 17)
    assert latest_day(four, date(2026, 11, 17)) == date(2026, 11, 17)
    assert history_for(four, date(2026, 11, 17), n=2) == [date(2026, 9, 22), date(2026, 10, 20)]
    assert is_tech_day(four, date(2026, 11, 17))
    assert not is_tech_day(four, date(2026, 11, 3))  # two weeks in, not four
    # The period is still the 14 days before the 1:1 day, whatever the repeat.
    assert period_for(date(2026, 11, 17)).start == date(2026, 11, 3)


def test_period_for_covers_the_14_days_before():
    period = period_for(date(2026, 10, 20))
    assert period == PrepPeriod(day=date(2026, 10, 20), start=date(2026, 10, 6), end=date(2026, 10, 19))
    assert period.label == "Oct 20"
    assert period.long == "Oct 6 – Oct 19"
    assert period.sheet_day == "10/20/2026"
    assert period.frame == "10/6 - 10/19"


def test_period_labels_across_a_month_and_year_edge():
    period = period_for(date(2027, 1, 5))
    assert (period.start, period.end) == (date(2026, 12, 22), date(2027, 1, 4))
    assert period.label == "Jan 5"
    assert period.long == "Dec 22 – Jan 4"
    assert period.sheet_day == "1/5/2027"
    assert period.frame == "12/22 - 1/4"


def test_as_dict():
    assert period_for(date(2026, 11, 3)).as_dict() == {
        "day": "2026-11-03", "start": "2026-10-20", "end": "2026-11-02",
        "label": "Nov 3", "long": "Oct 20 – Nov 2", "sheet_day": "11/3/2026",
        "frame": "10/20 - 11/2",
    }
