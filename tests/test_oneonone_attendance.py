"""A tech's PTO and sick days in a 1:1 period, from the same day rules Team activity shows."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from itertools import count
from zoneinfo import ZoneInfo

from work_assistant.activity import report, store
from work_assistant.activity.model import Action, ShiftVersion, SignIn
from work_assistant.db import connect, init_db
from work_assistant.oneonone.attendance import Attendance, attendance, attendances
from work_assistant.oneonone.cycle import period_for
from work_assistant.periods import Period
from work_assistant.settings import set_setting

TZ = "America/Chicago"
CHI = ZoneInfo(TZ)
A = "a@example.com"
MON_FRI = frozenset({1, 2, 3, 4, 5})               # Sunday = 0
PERIOD = period_for(date(2026, 10, 13))            # Tue 9/29 – Mon 10/12
TODAY = date(2026, 10, 17)                         # a Saturday
SYNCED = "2026-10-17T11:00:00Z"                    # 06:00 that Saturday in Chicago
SYNC_START = "2026-09-01T05:00:00Z"                # Sep 1, 00:00 in Chicago
_ids = count(1)


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CHI).astimezone(timezone.utc)


def make_db(tmp_path, through=SYNCED, window_start=SYNC_START, name="t.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", [A])
    if through is not None:
        sync(conn, through, window_start)
    return conn


def sync(conn, through, window_start=SYNC_START):
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", (through, through, through, window_start))
    conn.commit()


def shift(conn, first=date(2026, 9, 1), days=MON_FRI):
    store.add_shift_version(conn, ShiftVersion(A, first, frozenset(days), 480, 1020, True))


def busy(conn, day, hh=9):
    store.insert_actions(conn, [Action(A, next(_ids), at(day, hh), "reply", None, None, next(_ids), 0)])


def period_days():
    return [PERIOD.start + timedelta(days=n) for n in range((PERIOD.end - PERIOD.start).days + 1)]


def work_every_workday(conn, *, skip=(), first=PERIOD.start):
    """One reply on every Monday–Friday of the period from `first`, except the days in `skip`."""
    for day in period_days():
        if day >= first and day.weekday() < 5 and day not in skip:
            busy(conn, day)


def states(conn):
    return {f.day: f.state for f in report.tech_day_facts(conn, A, PERIOD.start, PERIOD.end, today=TODAY)}


def test_pto_and_sick_days_are_counted_separately(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    d1, d2, d3 = date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 7)
    store.add_time_off(conn, A, d1, d1, "", "pto")
    store.add_time_off(conn, A, d2, d2, "", "sick")
    work_every_workday(conn, skip=(d1, d2, d3))     # d3: a workday with no sign-in and no actions
    result = attendance(conn, A, PERIOD, today=TODAY)
    assert result == Attendance(pto=(d1,), sick=(d2, d3), complete=True)
    assert result.total == 3


def test_a_signed_in_day_with_no_activity_is_not_sick(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    quiet = date(2026, 10, 8)
    work_every_workday(conn, skip=(quiet,))
    store.insert_signins(conn, [SignIn(A, at(quiet, 8, 5))])
    assert states(conn)[quiet] == "zero"
    assert attendance(conn, A, PERIOD, today=TODAY) == Attendance(pto=(), sick=(), complete=True)


def test_rest_days_and_untracked_days_do_not_count(tmp_path):
    conn = make_db(tmp_path)
    joined = date(2026, 10, 5)                      # a Monday inside the period
    shift(conn, first=joined)
    work_every_workday(conn, first=joined)
    seen = states(conn)
    assert {seen[d] for d in period_days() if d < joined} == {"untracked"}
    assert seen[date(2026, 10, 10)] == seen[date(2026, 10, 11)] == "rest"
    result = attendance(conn, A, PERIOD, today=TODAY)
    assert result == Attendance(pto=(), sick=(), complete=True)
    assert result.total == 0


def test_pto_on_a_rest_day_does_not_count(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    fri, mon = date(2026, 10, 2), date(2026, 10, 5)
    store.add_time_off(conn, A, fri, mon, "long weekend", "pto")
    work_every_workday(conn, skip=(fri, mon))
    seen = states(conn)
    assert seen[date(2026, 10, 3)] == seen[date(2026, 10, 4)] == "rest"
    result = attendance(conn, A, PERIOD, today=TODAY)
    assert result == Attendance(pto=(fri, mon), sick=(), complete=True)
    assert result.total == 2


def test_incomplete_when_sync_does_not_cover_the_period(tmp_path):
    conn = make_db(tmp_path, through=store.iso(at(PERIOD.end, 12)))
    shift(conn)
    work_every_workday(conn)                        # the last day has a reply at 09:00, so no day is unsynced
    assert "unsynced" not in states(conn).values()
    assert attendance(conn, A, PERIOD, today=TODAY).complete is False

    sync(conn, store.iso(at(PERIOD.end + timedelta(days=1), 0)))   # current to local midnight after the period
    assert attendance(conn, A, PERIOD, today=TODAY).complete is True


def test_incomplete_with_no_sync_or_a_day_before_the_stored_activity(tmp_path):
    never = make_db(tmp_path, through=None, name="never.db")
    shift(never)
    assert attendance(never, A, PERIOD, today=TODAY) == Attendance(pto=(), sick=(), complete=False)

    late = make_db(tmp_path, window_start=store.iso(at(PERIOD.start + timedelta(days=1), 0)), name="late.db")
    shift(late)
    work_every_workday(late)
    assert states(late)[PERIOD.start] == "unsynced"
    assert attendance(late, A, PERIOD, today=TODAY).complete is False


def test_many_periods_from_one_read_are_each_period_counted_alone(tmp_path):
    """`attendances` judges the tech's days once over every period asked for, and gives each period what counting it
    alone gives, overlapping ones and one past the last sync too (T-168)."""
    conn = make_db(tmp_path)
    shift(conn)
    store.add_time_off(conn, A, date(2026, 9, 16), date(2026, 9, 16), "", "pto")
    store.add_time_off(conn, A, date(2026, 10, 7), date(2026, 10, 7), "", "sick")
    store.add_time_off(conn, A, date(2026, 10, 2), date(2026, 10, 5), "", "pto")
    for n in range(50):
        day = date(2026, 9, 1) + timedelta(days=n)
        if day.weekday() < 5 and n % 9:
            busy(conn, day)
    periods = [period_for(day) for day in (date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13),
                                           date(2026, 10, 20), date(2026, 10, 27))]
    together = attendances(conn, A, periods, today=TODAY)
    assert together == {p.day: attendance(conn, A, p, today=TODAY) for p in periods}
    assert together[date(2026, 10, 13)] == attendance(conn, A, PERIOD, today=TODAY)
    assert together[date(2026, 10, 27)].complete is False and together[date(2026, 10, 13)].complete is True
    assert sum(a.total for a in together.values()) > 0


def test_matches_team_activity_range_view(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, first=date(2026, 10, 1))            # 9/29 and 9/30 are untracked
    store.add_time_off(conn, A, date(2026, 10, 2), date(2026, 10, 5), "", "pto")    # spans a weekend
    store.add_time_off(conn, A, date(2026, 10, 7), date(2026, 10, 7), "", "sick")
    store.add_time_off(conn, A, date(2026, 10, 10), date(2026, 10, 10), "", "sick")  # a Saturday: rest
    busy(conn, date(2026, 10, 1))
    store.insert_signins(conn, [SignIn(A, at(date(2026, 10, 6), 8, 5))])            # signed in, no activity
    busy(conn, date(2026, 10, 9))
    busy(conn, date(2026, 10, 11))                  # a worked Sunday
    # 10/8 and 10/12: workdays with nothing at all

    grid = report.range_view(conn, Period(PERIOD.start, PERIOD.end, "custom", PERIOD.long, False), A, today=TODAY)
    cells = grid["rows"][0]["cells"]
    facts = report.tech_day_facts(conn, A, PERIOD.start, PERIOD.end, today=TODAY)
    assert [(f.day.isoformat(), f.state) for f in facts] == [(c["day"], c["state"]) for c in cells]

    result = attendance(conn, A, PERIOD, today=TODAY)
    assert [d.isoformat() for d in result.pto] == [c["day"] for c in cells if c["state"] == "pto"]
    assert [d.isoformat() for d in result.sick] == [c["day"] for c in cells if c["state"] == "missed"]
    assert result.pto == (date(2026, 10, 2), date(2026, 10, 5))
    assert result.sick == (date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12))
