"""The Trends measures: tallies of the counted days, the eighteen measures, the team's figures, the steps."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from itertools import count
from zoneinfo import ZoneInfo

from work_assistant.activity import collected, store, trends
from work_assistant.activity.model import Action, ShiftVersion, SignIn
from work_assistant.db import connect, init_db
from work_assistant.oneonone.model import PrepItem
from work_assistant.periods import Period
from work_assistant.settings import set_setting

TZ = "America/Chicago"
CHI = ZoneInfo(TZ)
A, B, C = "a@example.com", "b@example.com", "c@example.com"
ALL_DAYS = frozenset(range(7))
SUN_WED = frozenset({0, 1, 2, 3})
TODAY = date(2026, 10, 17)                         # a Saturday
SYNCED = "2026-10-17T11:00:00Z"                    # 06:00 that Saturday in Chicago
SYNC_START = "2026-09-01T05:00:00Z"                # Sep 1, 00:00 in Chicago
_ids = count(1)


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CHI).astimezone(timezone.utc)


def period(start: date, end: date, preset: str = "custom", so_far: bool = False) -> Period:
    return Period(start, end, preset, f"{start} - {end}", so_far)


def make_db(tmp_path, team=(A,), through=SYNCED, window_start=SYNC_START, name="t.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    set_setting(conn, "timezone", TZ)
    set_setting(conn, "team", list(team))
    if through is not None:
        conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                     " VALUES ('daily', 'complete', ?, ?, ?, ?)", (through, through, through, window_start))
        conn.commit()
    return conn


def shift(conn, email=A, first=date(2026, 9, 1), days=ALL_DAYS, start=480, end=1020, active=True):
    store.add_shift_version(conn, ShiftVersion(email, first, frozenset(days), start, end, active))


def work(conn, email, day, *times, tickets=None, kind="reply", new=None):
    """One action at each (hour, minute) on `day`; each on its own ticket unless `tickets` names them."""
    rows = [Action(email, tickets[i] if tickets else next(_ids), at(day, h, m), kind, None, new, next(_ids), 0)
            for i, (h, m) in enumerate(times)]
    store.insert_actions(conn, rows)


def busy(conn, email, day, n, start_hour=9):
    """`n` replies on `n` tickets, five minutes apart from `start_hour`."""
    work(conn, email, day, *[(start_hour, 5 * i) for i in range(n)])


def signin(conn, email, day, hh=8, mm=5):
    store.insert_signins(conn, [SignIn(email, at(day, hh, mm))])


def measure(body, key):
    return next(m for m in body["measures"] if m["key"] == key)


def tech(body, key, email=A):
    return next(t for t in measure(body, key)["techs"] if t["email"] == email)


# -- the table of measures -----------------------------------------------------------------------------------


def test_measures_keys_labels_and_order():
    assert [(m.key, m.label, m.group, m.unit, m.digits, m.better) for m in trends.MEASURES] == [
        ("tickets_per_shift", "Tickets per shift", "Output", "number", 1, "higher"),
        ("replies_per_shift", "Replies per shift", "Output", "number", 1, "higher"),
        ("solved_per_shift", "Solved per shift", "Output", "number", 1, "higher"),
        ("shifts_worked", "Shifts worked", "Reliability", "percent", 0, "higher"),
        ("missed_shifts", "Sick days", "Reliability", "count", 0, "lower"),
        ("signed_in_no_activity", "Signed in, no activity", "Reliability", "count", 0, "lower"),
        ("late_start_min", "First action after shift start", "Time on shift", "minutes", 0, "lower"),
        ("early_finish_min", "Last action before shift end", "Time on shift", "minutes", 0, "lower"),
        ("longest_gap_min", "Longest gap", "Time on shift", "minutes", 0, "lower"),
        ("flags_per_shift", "Flags per shift", "Flags", "number", 2, "lower"),
        ("calls_per_shift", "Calls per shift", "Calls", "number", 1, "higher"),
        ("calls_share", "% of calls", "Calls", "percent", 1, "higher"),
        ("declined_missed_per_shift", "Declined / missed per shift", "Calls", "number", 2, "lower"),
        ("declined_missed_pct", "% declined / missed", "Calls", "percent", 1, "lower"),
        ("call_time_per_shift", "Call time per shift", "Calls", "minutes", 0, "higher"),
        ("avg_call_time", "Avg. call time", "Calls", "duration", 0, "lower"),
        ("satisfaction", "Satisfaction", "Customers", "percent", 0, "higher"),
        ("aging_tickets", "Aging tickets", "Backlog", "count", 1, "lower"),
    ]


def test_the_payload_has_the_pinned_fields(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    busy(conn, A, date(2026, 10, 12), 2)
    body = trends.trends_view(conn, period(date(2026, 10, 11), date(2026, 10, 16)),
                              period(date(2026, 10, 4), date(2026, 10, 9), "previous"), today=TODAY)
    assert list(body) == ["period", "comparison", "tech", "today", "synced", "coverage_from", "unit", "steps",
                          "techs", "measures"]
    assert body["tech"] is None
    one = trends.trends_view(conn, period(date(2026, 10, 11), date(2026, 10, 16)), None, today=TODAY, tech=A)
    assert one["tech"] == A and one["techs"] == body["techs"]
    assert body["today"] == "2026-10-17" and body["unit"] == "week" and body["coverage_from"] is None
    assert body["synced"]["at"] == SYNCED
    assert body["period"]["start"] == "2026-10-11" and body["comparison"]["start"] == "2026-10-04"
    assert body["techs"] == [{"email": A, "name": A, "region": "NASA"}]
    assert [m["key"] for m in body["measures"]] == [m.key for m in trends.MEASURES]
    first = body["measures"][0]
    assert list(first) == ["key", "label", "group", "unit", "digits", "team_digits", "better", "window", "base",
                           "line_digits", "team", "techs"]
    assert first["line_digits"] == first["digits"]
    assert (first["window"], first["base"]) == ({"count": 4, "unit": "week"}, "shifts")
    sat = measure(body, "satisfaction")
    assert (sat["window"], sat["base"]) == ({"count": 8, "unit": "week"}, "ratings")
    series = ["now", "before", "change", "steps", "steps_n", "rolling", "rolling_n"]
    assert list(first["team"]) == series
    assert list(first["techs"][0]) == ["email", *series]
    assert [s["label"] for s in body["steps"]] == ["Oct 4", "Oct 11"]
    assert len(first["team"]["steps"]) == len(first["techs"][0]["steps"]) == 2


# -- what counts ----------------------------------------------------------------------------------------------


def test_scheduled_and_worked_shifts(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, days=SUN_WED)
    busy(conn, A, date(2026, 10, 4), 3)            # Sunday: worked
    # Monday: nothing at all, missed
    signin(conn, A, date(2026, 10, 6))             # Tuesday: signed in, no activity
    store.add_time_off(conn, A, date(2026, 10, 7), date(2026, 10, 7), "")   # Wednesday: time off
    busy(conn, A, date(2026, 10, 9), 4)            # Friday is a rest day: a worked rest day counts for nothing
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)), None, today=TODAY)
    for who in (tech(body, "tickets_per_shift"), measure(body, "tickets_per_shift")["team"]):
        assert who["now"] == 3.0
    assert tech(body, "shifts_worked")["now"] == 33
    assert tech(body, "missed_shifts")["now"] == 1
    assert tech(body, "signed_in_no_activity")["now"] == 1


def _only_counts(body):
    return {m["key"]: m["team"]["now"] for m in body["measures"]}


def test_untracked_unsynced_today_and_unfinished_shifts_count_nothing(tmp_path):
    base_day, plain = date(2026, 10, 12), period(date(2026, 10, 11), date(2026, 10, 17))

    def world(name, **kw):
        conn = make_db(tmp_path, name=name, **kw)
        shift(conn)
        busy(conn, A, base_day, 2)
        return conn

    reference = _only_counts(trends.trends_view(world("ref.db"), plain, None, today=TODAY))
    assert reference["tickets_per_shift"] == 2.0

    # Today (judged in full by a late sync) is left out.
    late = world("today.db", through="2026-10-18T05:00:00Z")
    busy(late, A, TODAY, 9)
    assert _only_counts(trends.trends_view(late, plain, None, today=TODAY))["tickets_per_shift"] == 2.0

    # A day before the first synced day is left out (the sync read from Oct 12).
    short = world("unsynced.db", window_start="2026-10-12T05:00:00Z")
    busy(short, A, date(2026, 10, 11), 9)
    body = trends.trends_view(short, plain, None, today=TODAY)
    assert _only_counts(body)["tickets_per_shift"] == 2.0 and body["coverage_from"] == "2026-10-12"

    # Yesterday, when the sync stopped at 15:00 and the shift ends at 17:00, is left out: not a late start,
    # an early finish or low output.
    lagging = world("lag.db", through="2026-10-16T20:00:00Z")
    busy(lagging, A, date(2026, 10, 16), 9)
    both = trends.trends_view(lagging, plain, None, today=TODAY)
    counts = _only_counts(both)
    for key in ("tickets_per_shift", "late_start_min", "early_finish_min", "longest_gap_min"):
        assert counts[key] == reference[key]
    assert counts["shifts_worked"] == 20 and counts["missed_shifts"] == 4     # 1 of 5 shifts; Oct 16 not counted


def test_first_and_last_action_clamped_at_zero(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    early_bird, normal = date(2026, 10, 4), date(2026, 10, 5)
    work(conn, A, early_bird, (7, 40), (8, 0), (17, 20))  # first in-shift action right at the start
    work(conn, A, normal, (8, 30), (16, 30))
    body = trends.trends_view(conn, period(early_bird, normal), None, today=TODAY)
    assert tech(body, "late_start_min")["now"] == 15      # (0 + 30) / 2
    assert tech(body, "early_finish_min")["now"] == 15


def test_first_action_after_shift_start_uses_the_first_in_shift_action(tmp_path):
    """Ruling 41 (spec §5): minutes from shift start to the first in-shift action, as Team activity's Late start
    flag measures it. An action before the start doesn't make the shift on time; a worked shift with no action
    inside it is left out of the average, never counted as 0."""
    conn = make_db(tmp_path)
    shift(conn)
    early_then_late, outside, normal = date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)
    work(conn, A, early_then_late, (7, 40), (12, 0))
    work(conn, A, outside, (7, 0), (17, 30))               # worked, but nothing inside the shift
    work(conn, A, normal, (8, 30), (16, 30))
    one = trends.trends_view(conn, period(early_then_late, early_then_late), None, today=TODAY)
    assert tech(one, "late_start_min")["now"] == 240
    alone = trends.trends_view(conn, period(outside, outside), None, today=TODAY)
    assert tech(alone, "late_start_min")["now"] is None
    assert tech(alone, "tickets_per_shift")["now"] == 2.0
    body = trends.trends_view(conn, period(early_then_late, normal), None, today=TODAY)
    assert tech(body, "late_start_min")["now"] == 135     # (240 + 30) / 2, the outside day left out


def test_minutes_on_the_daylight_saving_day(tmp_path):
    sunday = date(2026, 11, 1)                 # clocks fell back at 02:00
    conn = make_db(tmp_path, through="2026-11-03T12:00:00Z")
    shift(conn)
    work(conn, A, sunday, (8, 0), (16, 0))
    body = trends.trends_view(conn, period(sunday, sunday), None, today=date(2026, 11, 3))
    assert tech(body, "late_start_min")["now"] == 0
    assert tech(body, "early_finish_min")["now"] == 60
    assert tech(body, "longest_gap_min")["now"] == 480


def test_longest_gap_leaves_out_shifts_with_fewer_than_two_actions(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    work(conn, A, date(2026, 10, 4), (9, 0), (9, 30), (12, 0))      # 150
    work(conn, A, date(2026, 10, 5), (8, 0), (8, 20))               # 20
    work(conn, A, date(2026, 10, 6), (10, 0))                       # one action: no gap to measure
    work(conn, A, date(2026, 10, 7), (7, 0), (7, 30), (10, 0))      # one in-shift action
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 7)), None, today=TODAY)
    assert tech(body, "longest_gap_min")["now"] == 85
    lone = trends.trends_view(conn, period(date(2026, 10, 6), date(2026, 10, 7)), None, today=TODAY)
    assert tech(lone, "longest_gap_min")["now"] is None
    assert tech(lone, "tickets_per_shift")["now"] == 2.0


def test_flags_per_shift_over_scheduled_shifts(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, days=SUN_WED | {4})
    work(conn, A, date(2026, 10, 4), (9, 45), (12, 0), (16, 50))                    # late start, long gap
    work(conn, A, date(2026, 10, 5), (8, 0), (9, 30), (11, 0), (12, 30), (14, 0), (15, 30), (17, 0))   # clean
    signin(conn, A, date(2026, 10, 6))                                              # no activity
    # Wednesday: missed. Thursday is scheduled too (days 0-4), so the week has 5 scheduled shifts.
    busy(conn, A, date(2026, 10, 9), 2)                                             # Friday: rest, no flags
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)), None, today=TODAY)
    # 2 + 0 + 1 + 1 (missed) + 1 (missed Thursday) = 5 flags over 5 scheduled shifts
    assert tech(body, "flags_per_shift")["now"] == 1.0
    assert tech(body, "missed_shifts")["now"] == 2


def test_week_with_no_shift_is_null_not_zero(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, first=date(2026, 10, 11), days=SUN_WED | {4})
    busy(conn, A, date(2026, 10, 12), 4)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 16)), None, today=TODAY)
    assert [s["label"] for s in body["steps"]] == ["Oct 4", "Oct 11"]
    for key in ("tickets_per_shift", "shifts_worked", "missed_shifts", "flags_per_shift", "late_start_min"):
        assert tech(body, key)["steps"][0] is None
        assert measure(body, key)["team"]["steps"][0] is None
    assert tech(body, "tickets_per_shift")["steps"][1] == 4.0
    assert tech(body, "missed_shifts")["steps"][1] == 4          # Sun, Tue, Wed, Thu with nothing


# -- the team ------------------------------------------------------------------------------------------------


def test_team_pools_ratios_and_averages_counts_per_tech(tmp_path):
    conn = make_db(tmp_path, team=(A, B))
    shift(conn, A, first=date(2026, 10, 15))                    # two shifts in the window
    shift(conn, B)
    for n in (15, 16):
        busy(conn, A, date(2026, 10, n), 5)
    day = date(2026, 9, 7)
    for _ in range(40):
        busy(conn, B, day, 3)
        day += timedelta(days=1)
    assert day == date(2026, 10, 17)
    body = trends.trends_view(conn, period(date(2026, 9, 7), date(2026, 10, 16)), None, today=TODAY)
    tickets = measure(body, "tickets_per_shift")
    assert tech(body, "tickets_per_shift", A)["now"] == 5.0
    assert tech(body, "tickets_per_shift", B)["now"] == 3.0
    assert tickets["team"]["now"] == 3.1                        # 130 / 42, not (5 + 3) / 2
    assert measure(body, "shifts_worked")["team"]["now"] == 100


def test_team_counts_are_the_mean_over_techs_with_shifts(tmp_path):
    conn = make_db(tmp_path, team=(A, B, C))
    shift(conn, A)
    shift(conn, B)
    shift(conn, C, first=date(2026, 12, 1))                     # no shift in the window: not in the mean
    for n in (4, 5):                                            # A misses Oct 6 (1); B misses all three days (3)
        busy(conn, A, date(2026, 10, n), 1)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 6)), None, today=TODAY)
    assert tech(body, "missed_shifts", A)["now"] == 1
    assert tech(body, "missed_shifts", B)["now"] == 3
    assert tech(body, "missed_shifts", C)["now"] is None
    assert measure(body, "missed_shifts")["team"]["now"] == 2   # (1 + 3) / 2


def test_team_counts_keep_one_decimal_and_tech_counts_stay_whole(tmp_path):
    """Ruling 38: the team's count is a mean per tech, so it keeps one decimal. Rounded whole, two of six techs
    missing a shift would read "Sick days 0, no change"."""
    six = tuple(f"t{n}@example.com" for n in range(6))
    conn = make_db(tmp_path, team=six)
    for email in six:
        shift(conn, email)
        day = date(2026, 9, 27)
        while day <= date(2026, 10, 5):
            if not (email in six[:2] and day == date(2026, 10, 5)):     # two techs miss Oct 5
                busy(conn, email, day, 2)
            day += timedelta(days=1)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 5)),
                              period(date(2026, 9, 27), date(2026, 9, 28), "previous"), today=TODAY)
    missed = measure(body, "missed_shifts")
    assert (missed["digits"], missed["team_digits"]) == (0, 1)
    assert (missed["team"]["now"], missed["team"]["before"], missed["team"]["change"]) == (0.3, 0, 0.3)
    assert missed["team"]["steps"] == [0, 0.3]                  # Sep 27 - Oct 3, Oct 4 - 5
    assert (tech(body, "missed_shifts", six[0])["now"], tech(body, "missed_shifts", six[0])["change"]) == (1, 1)
    assert isinstance(tech(body, "missed_shifts", six[0])["now"], int)
    assert measure(body, "signed_in_no_activity")["team_digits"] == 1
    for m in body["measures"]:
        if m["key"] not in ("missed_shifts", "signed_in_no_activity"):
            assert m["team_digits"] == m["digits"], m["key"]


# -- change --------------------------------------------------------------------------------------------------


def test_change_rounds_at_the_measure_precision():
    assert trends._round(Fraction(404, 100) - Fraction(401, 100), 1) == 0.0
    assert trends._round(Fraction(124, 1000) - Fraction(118, 1000), 2) == 0.01
    assert trends._round(Fraction(5, 2), 0) == 3 and trends._round(Fraction(-5, 2), 0) == -3
    assert trends._round(Fraction(-1, 1000), 2) == 0 and str(trends._round(Fraction(-1, 1000), 2)) == "0.0"
    assert trends._round(None, 1) is None


def test_a_time_change_is_whole_seconds_so_it_never_reads_0_00():
    """T-169: a duration's change, the team's included, is rounded to whole seconds, so one under half a second is
    0 ("no change") and any other is at least 1 ("0:01"): the screen never shows a coloured "▲ 0:00"."""
    for m in trends.MEASURES:
        if m.unit == "duration":
            assert (m.digits, m.team_digits) == (0, 0), m.key
    assert trends._round(Fraction(2, 5), 0) == 0 and trends._round(Fraction(-2, 5), 0) == 0
    assert trends._round(Fraction(3, 5), 0) == 1


def test_change_is_the_raw_difference_rounded(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, days=SUN_WED)
    for n, tickets in ((27, 2), (28, 1), (29, 1), (30, 1)):     # comparison: 5 tickets over 4 shifts = 1.25
        busy(conn, A, date(2026, 9, n), tickets)
    for n, tickets in ((4, 2), (5, 1), (6, 1)):                 # period: 4 tickets over 3 worked shifts = 1.33
        busy(conn, A, date(2026, 10, n), tickets)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)),
                              period(date(2026, 9, 27), date(2026, 10, 3), "previous"), today=TODAY)
    row = tech(body, "tickets_per_shift")
    assert (row["now"], row["before"], row["change"]) == (1.3, 1.3, 0.1)
    assert tech(body, "shifts_worked")["change"] == -25         # 3 of 4 shifts against 4 of 4
    assert tech(body, "missed_shifts")["change"] == 1


def test_new_tech_has_null_before_and_change(tmp_path):
    conn = make_db(tmp_path, team=(A, B, C))
    shift(conn, A)
    shift(conn, B, first=date(2026, 10, 4))                     # joined in the period
    shift(conn, C)                                              # away the whole comparison
    store.add_time_off(conn, C, date(2026, 9, 27), date(2026, 10, 3), "")
    for email in (A, B, C):
        for n in (27, 28, 29):
            busy(conn, email, date(2026, 9, n), 2)
        for n in (4, 5, 6):
            busy(conn, email, date(2026, 10, n), 3)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)),
                              period(date(2026, 9, 27), date(2026, 10, 3), "previous"), today=TODAY)
    old, new, away = (tech(body, "tickets_per_shift", e) for e in (A, B, C))
    assert (old["before"], old["change"]) == (2.0, 1.0)
    assert new["now"] == 3.0 and new["before"] is None and new["change"] is None
    assert away["now"] == 3.0 and away["before"] is None and away["change"] is None
    team = measure(body, "tickets_per_shift")["team"]
    assert team["now"] == 3.0 and team["before"] == 2.0


def test_compare_none_has_null_before_and_change_everywhere(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    busy(conn, A, date(2026, 10, 4), 2)
    busy(conn, A, date(2026, 9, 27), 2)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)), None, today=TODAY)
    assert body["comparison"] is None
    for m in body["measures"]:
        for row in (m["team"], *m["techs"]):
            assert row["before"] is None and row["change"] is None
    assert tech(body, "tickets_per_shift")["now"] == 2.0
    assert [s["label"] for s in body["steps"]] == ["Oct 4"]


# -- steps ---------------------------------------------------------------------------------------------------


def test_edge_weeks_are_clipped_and_marked_part(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    body = trends.trends_view(conn, period(date(2026, 9, 2), date(2026, 9, 16)), None, today=TODAY)
    assert [(s["start"], s["end"], s["label"], s["in_period"], s["part"]) for s in body["steps"]] == [
        ("2026-09-02", "2026-09-05", "Aug 30", True, True),
        ("2026-09-06", "2026-09-12", "Sep 6", True, False),
        ("2026-09-13", "2026-09-16", "Sep 13", True, True),
    ]


def _in_period_sum(body, key):
    row = tech(body, key)
    return sum(v for v, s in zip(row["steps"], body["steps"]) if s["in_period"])


def test_period_edges_split_steps_so_no_point_mixes_days(tmp_path):
    conn = make_db(tmp_path, window_start="2026-07-01T05:00:00Z")
    shift(conn, first=date(2026, 8, 1))
    for day in (date(2026, 8, 31), date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 14)):
        busy(conn, A, day, 2)
    # Custom Wed Sep 2 - Wed Sep 16, against the 15 days before it (Tue Aug 18 - Tue Sep 1).
    body = trends.trends_view(conn, period(date(2026, 9, 2), date(2026, 9, 16)),
                              period(date(2026, 8, 18), date(2026, 9, 1), "previous"), today=TODAY)
    assert [(s["start"], s["end"], s["label"], s["in_period"], s["part"]) for s in body["steps"]] == [
        ("2026-08-18", "2026-08-22", "Aug 16", False, True),
        ("2026-08-23", "2026-08-29", "Aug 23", False, False),
        ("2026-08-30", "2026-09-01", "Aug 30", False, True),
        ("2026-09-02", "2026-09-05", "Aug 30", True, True),
        ("2026-09-06", "2026-09-12", "Sep 6", True, False),
        ("2026-09-13", "2026-09-16", "Sep 13", True, True),
    ]
    for key in ("missed_shifts", "signed_in_no_activity"):
        assert _in_period_sum(body, key) == tech(body, key)["now"]
    assert tech(body, "missed_shifts")["now"] == 12                     # 15 shifts, 3 worked
    assert tech(body, "missed_shifts")["steps"][3] == 3                 # Sep 2-5 only: Sep 3 worked


def test_last_month_with_previous_splits_the_edge_weeks(tmp_path):
    conn = make_db(tmp_path, window_start="2026-07-01T05:00:00Z")
    shift(conn, first=date(2026, 8, 1))
    for day in (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 29)):
        busy(conn, A, day, 4 if day.month == 8 else 2)
    last_month = Period(date(2026, 9, 1), date(2026, 9, 30), "last_month", "Last month", False)
    body = trends.trends_view(conn, last_month, period(date(2026, 8, 1), date(2026, 8, 31), "previous"),
                              today=TODAY)
    steps = [(s["start"], s["end"], s["in_period"], s["part"]) for s in body["steps"]]
    assert steps[0] == ("2026-08-01", "2026-08-01", False, True)
    assert ("2026-08-30", "2026-08-31", False, True) in steps
    assert [s for s in steps if s[2]] == [
        ("2026-09-01", "2026-09-05", True, True), ("2026-09-06", "2026-09-12", True, False),
        ("2026-09-13", "2026-09-19", True, False), ("2026-09-20", "2026-09-26", True, False),
        ("2026-09-27", "2026-09-30", True, True)]
    for key in ("missed_shifts", "signed_in_no_activity"):
        assert _in_period_sum(body, key) == tech(body, key)["now"]
    assert tech(body, "missed_shifts")["now"] == 28
    # The Sep 1-5 point has only September's worked day in it, not the busier Aug 30-31.
    edge = [s["start"] for s in body["steps"]].index("2026-09-01")
    assert tech(body, "tickets_per_shift")["steps"][edge] == 2.0
    assert tech(body, "tickets_per_shift")["steps"][edge - 1] == 4.0


def test_period_edges_split_month_steps(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    start = date(2026, 3, 15)
    now = period(start, start + timedelta(days=26 * 7))
    before = period(date(2025, 9, 13), date(2026, 3, 14), "previous")
    body = trends.trends_view(conn, now, before, today=TODAY)
    assert body["unit"] == "month"
    march = [(s["start"], s["end"], s["label"], s["in_period"], s["part"]) for s in body["steps"]
             if s["label"] == "Mar"]
    assert march == [("2026-03-01", "2026-03-14", "Mar", False, True),
                     ("2026-03-15", "2026-03-31", "Mar", True, True)]


def test_the_graph_covers_the_period_and_its_comparison(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 17)),
                              period(date(2026, 9, 20), date(2026, 10, 3), "previous"), today=TODAY)
    assert [(s["label"], s["in_period"], s["part"]) for s in body["steps"]] == [
        ("Sep 20", False, False), ("Sep 27", False, False), ("Oct 4", True, False), ("Oct 11", True, False)]
    # A comparison that starts mid-week is clipped to the window like the period's edges.
    clipped = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)),
                                 period(date(2026, 9, 29), date(2026, 10, 3), "custom"), today=TODAY)
    assert [(s["start"], s["part"], s["in_period"]) for s in clipped["steps"]] == [
        ("2026-09-29", True, False), ("2026-10-04", False, True)]


def test_each_step_is_ticked_with_its_own_first_day(tmp_path):
    """The axis names the day each point's days start, so a part week reads "Sep 1", not its week's "Aug 30"; the
    label (the tooltip's "Week of") stays its week's."""
    conn = make_db(tmp_path, window_start="2026-07-01T05:00:00Z")
    shift(conn, first=date(2026, 8, 1))
    body = trends.trends_view(conn, period(date(2026, 9, 1), date(2026, 9, 30)),
                              period(date(2026, 8, 1), date(2026, 8, 31), "previous"), today=TODAY)
    assert [(s["label"], s["tick"]) for s in body["steps"]] == [
        ("Jul 26", "Aug 1"), ("Aug 2", "Aug 2"), ("Aug 9", "Aug 9"), ("Aug 16", "Aug 16"), ("Aug 23", "Aug 23"),
        ("Aug 30", "Aug 30"), ("Aug 30", "Sep 1"), ("Sep 6", "Sep 6"), ("Sep 13", "Sep 13"), ("Sep 20", "Sep 20"),
        ("Sep 27", "Sep 27")]
    start = date(2026, 3, 15)
    long = trends.trends_view(conn, period(start, start + timedelta(days=26 * 7)), None, today=TODAY)
    assert [s["tick"] for s in long["steps"]][:2] == ["Mar 15", "Apr"], "a whole month by its name, a part by its day"


def test_long_periods_step_by_month(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    start = date(2026, 3, 15)
    long = trends.trends_view(conn, period(start, start + timedelta(days=26 * 7)), None, today=TODAY)
    assert long["unit"] == "month"
    assert [s["label"] for s in long["steps"]] == ["Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]
    assert long["steps"][0]["start"] == "2026-03-15" and long["steps"][0]["part"] is True
    assert long["steps"][1]["start"] == "2026-04-01" and long["steps"][1]["part"] is False
    limit = trends.trends_view(conn, period(start, start + timedelta(days=26 * 7 - 1)), None, today=TODAY)
    assert limit["unit"] == "week"


def test_coverage_from_when_window_starts_before_the_first_sync(tmp_path):
    conn = make_db(tmp_path, window_start="2026-10-01T05:00:00Z")
    shift(conn)
    busy(conn, A, date(2026, 10, 2), 2)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)),
                              period(date(2026, 9, 27), date(2026, 10, 3), "previous"), today=TODAY)
    assert body["coverage_from"] == "2026-10-01"
    # Only Oct 1-3 were synced: one worked day and two missed; Sep 27-30 count nothing, not even as misses.
    assert tech(body, "tickets_per_shift")["before"] == 2.0 and tech(body, "missed_shifts")["before"] == 2
    assert tech(body, "shifts_worked")["before"] == 33
    assert tech(body, "tickets_per_shift")["steps"] == [2.0, None]  # no worked shift in the week of Oct 4
    # Starting on the first synced day, or after it, needs no note.
    assert trends.trends_view(conn, period(date(2026, 10, 1), date(2026, 10, 10)), None,
                              today=TODAY)["coverage_from"] is None


def test_coverage_from_is_the_first_whole_synced_day(tmp_path):
    # Real syncs read from "now - 90 days", mid-day: Oct 1 10:00 in Chicago, so Oct 1 itself is not synced.
    conn = make_db(tmp_path, window_start="2026-10-01T15:00:00Z")
    shift(conn)
    body = trends.trends_view(conn, period(date(2026, 10, 1), date(2026, 10, 10)), None, today=TODAY)
    assert body["coverage_from"] == "2026-10-02"
    assert trends.trends_view(conn, period(date(2026, 10, 2), date(2026, 10, 10)), None,
                              today=TODAY)["coverage_from"] is None


def test_no_sync_at_all_leaves_every_value_null(tmp_path):
    conn = make_db(tmp_path, through=None)
    shift(conn)
    busy(conn, A, date(2026, 10, 5), 2)
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)),
                              period(date(2026, 9, 27), date(2026, 10, 3), "previous"), today=TODAY)
    assert body["coverage_from"] is None and body["synced"]["at"] is None
    assert [t["email"] for t in body["techs"]] == [A]
    for m in body["measures"]:
        for row in (m["team"], *m["techs"]):
            assert row["now"] is None and row["before"] is None and row["change"] is None
            assert all(v is None for v in row["steps"])


def test_an_empty_period_has_no_steps_and_null_values(tmp_path):
    conn = make_db(tmp_path)
    shift(conn)
    empty = Period(date(2026, 10, 18), date(2026, 10, 17), "this_week", "This week so far", True)
    body = trends.trends_view(conn, empty, period(date(2026, 10, 11), date(2026, 10, 17), "previous"), today=TODAY)
    assert body["steps"] and all(s["in_period"] is False for s in body["steps"])
    assert tech(body, "tickets_per_shift")["now"] is None
    nothing = trends.trends_view(conn, empty, None, today=TODAY)
    assert nothing["steps"] == [] and nothing["coverage_from"] is None


def test_departed_tech_listed_only_while_on_the_team(tmp_path):
    conn = make_db(tmp_path, team=(A,))
    shift(conn, A)
    shift(conn, B)
    shift(conn, B, first=date(2026, 9, 21), active=False)       # left on Sep 21, not on the team now
    busy(conn, B, date(2026, 9, 8), 2)
    during = trends.trends_view(conn, period(date(2026, 9, 6), date(2026, 9, 12)), None, today=TODAY)
    assert [t["email"] for t in during["techs"]] == [A, B]
    assert tech(during, "tickets_per_shift", B)["now"] == 2.0
    after = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)), None, today=TODAY)
    assert [t["email"] for t in after["techs"]] == [A]
    assert all(len(m["techs"]) == 1 for m in after["measures"])


def test_tickets_replies_and_solved_are_per_worked_shift(tmp_path):
    """Solved per shift counts the tickets solved while assigned to the tech (`ticket_states`, T-158), not their
    status changes to solved: here one of theirs on the worked Monday; one solved on the Tuesday shift they
    missed and one assigned to someone else count for nothing."""
    conn = make_db(tmp_path)
    shift(conn)
    d = date(2026, 10, 5)
    work(conn, A, d, (9, 0), (9, 10), (9, 20), tickets=[1, 1, 2])
    work(conn, A, d, (9, 30), (9, 40), tickets=[2, 1], kind="status", new="solved")
    collected.set_agents(conn, {A: 11, B: 12})
    collected.upsert_ticket_states(conn, [{"id": 2, "assignee_id": 11}, {"id": 4, "assignee_id": 11},
                                          {"id": 5, "assignee_id": 12}],
                                   [{"ticket_id": 2, "solved_at": store.iso(at(d, 9, 30))},
                                    {"ticket_id": 4, "solved_at": store.iso(at(d + timedelta(days=1), 10))},
                                    {"ticket_id": 5, "solved_at": store.iso(at(d, 10))}])
    body = trends.trends_view(conn, period(d, d + timedelta(days=1)), None, today=TODAY)
    assert tech(body, "tickets_per_shift")["now"] == 2.0
    assert tech(body, "replies_per_shift")["now"] == 3.0
    assert tech(body, "solved_per_shift")["now"] == 1.0


# -- a marked sick day ----------------------------------------------------------------------------------------


def test_a_marked_sick_day_with_a_sign_in_and_two_actions_counts_only_as_a_sick_day(tmp_path):
    conn = make_db(tmp_path)
    shift(conn, days=frozenset({0, 1}))                    # Sunday and Monday
    busy(conn, A, date(2026, 10, 4), 3)                    # Sunday: worked, 3 tickets
    signin(conn, A, date(2026, 10, 5))
    busy(conn, A, date(2026, 10, 5), 2)                    # Monday: signed in and two actions, marked sick
    store.add_time_off(conn, A, date(2026, 10, 5), date(2026, 10, 5), "", "sick")
    body = trends.trends_view(conn, period(date(2026, 10, 4), date(2026, 10, 10)), None, today=TODAY)
    assert tech(body, "missed_shifts")["now"] == 1
    assert tech(body, "shifts_worked")["now"] == 50        # 1 worked of 2 scheduled
    assert tech(body, "signed_in_no_activity")["now"] == 0
    for who in (tech(body, "tickets_per_shift"), measure(body, "tickets_per_shift")["team"]):
        assert who["now"] == 3.0, "none of the sick day's tickets count toward tickets per shift"
    assert tech(body, "replies_per_shift")["now"] == 3.0
    assert tech(body, "flags_per_shift")["now"] == 1.0     # Sunday's early finish and the one Sick day flag, over 2


# -- the 1:1 measures, from the collected calls, ratings and aging ----------------------------------------------

A_ID, B_ID, OTHER_ID = 11, 12, 99
CALL_KEYS = ("calls_per_shift", "calls_share", "declined_missed_per_shift", "declined_missed_pct",
             "call_time_per_shift", "avg_call_time", "satisfaction", "aging_tickets")
LATER = date(2026, 10, 21)                         # a Wednesday
LATER_SYNCED = "2026-10-21T11:00:00Z"              # 06:00 that Wednesday in Chicago


def collect_all(conn, through=LATER_SYNCED):
    """Every collected kind covered from Sep 1 to `through`."""
    for kind in collected.KINDS:
        collected.save_state(conn, kind, covered_from=store.parse_iso(SYNC_START), through=store.parse_iso(through))


def calls(conn, *legs):
    """One inbound call for each (agent id, completion status, when, talk seconds), with its one agent leg."""
    rows, records = [], []
    for agent_id, status, moment, talk in legs:
        call_id = next(_ids)
        records.append({"id": call_id, "direction": "inbound", "ticket_id": None})
        rows.append({"id": next(_ids), "call_id": call_id, "agent_id": agent_id, "type": "agent",
                     "completion_status": status, "created_at": store.iso(moment), "talk_time": talk})
    collected.upsert_legs(conn, rows)
    collected.upsert_calls(conn, records)


def ratings(conn, assignee_id, day, good, bad):
    collected.upsert_ratings(conn, [
        {"id": next(_ids), "ticket_id": next(_ids), "assignee_id": assignee_id, "score": score,
         "created_at": store.iso(at(day, 12)), "reason": None, "comment": None}
        for score in ["good"] * good + ["bad"] * bad])


def aging(conn, email, day, n):
    items = [PrepItem("aging", next(_ids), "2026-10-01T15:00:00Z", None, "open") for _ in range(n)]
    collected.record_aging(conn, email, day, items, now=at(day, 0, 5))


def mondays_calls(conn, day=date(2026, 10, 19)):
    """A's Monday: three answered calls (60, 120 and 180 seconds) and one declined; the rest of the department
    answered three more, so six in all."""
    calls(conn, (A_ID, "completed", at(day, 9), 60), (A_ID, "completed", at(day, 10), 120),
          (A_ID, "completed", at(day, 11), 180), (A_ID, "agent_declined", at(day, 12), 0),
          (OTHER_ID, "completed", at(day, 12, 1), 200), (OTHER_ID, "completed", at(day, 13), 200),
          (OTHER_ID, "completed", at(day, 14), 200))


def test_the_line_pools_a_trailing_window_reaching_back_before_the_view(tmp_path):
    """The owner, 2026-10-22: each weekly step's line point pools the trailing weeks ending with it, 8 for
    Satisfaction and 4 for every other measure, all ratings or calls together, reading back before the view where the
    window reaches; the step's own figure, the period's Now and its counts are as before."""
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    ratings(conn, A_ID, date(2026, 9, 6), good=3, bad=1)     # 6 weeks before the view: in the 8 weeks
    ratings(conn, A_ID, date(2026, 9, 27), good=0, bad=2)
    ratings(conn, A_ID, date(2026, 10, 13), good=1, bad=0)   # the view's one week
    calls(conn, (A_ID, "completed", at(date(2026, 9, 14), 10), 600),    # 5 weeks back: outside the 4
          (A_ID, "completed", at(date(2026, 9, 22), 10), 300),
          (A_ID, "completed", at(date(2026, 10, 12), 10), 100))
    week = period(date(2026, 10, 11), date(2026, 10, 17))
    body = trends.trends_view(conn, week, None, today=LATER)
    sat, avg = tech(body, "satisfaction"), tech(body, "avg_call_time")
    assert (sat["now"], sat["steps"], sat["steps_n"]) == (100, [100], [1]), "the week itself: 1 good rating"
    assert (sat["rolling"], sat["rolling_n"]) == ([57], [7]), "Aug 23 - Oct 17: 4 good of 7, pooled"
    assert (avg["now"], avg["steps"], avg["steps_n"]) == (100, [100], [1])
    assert (avg["rolling"], avg["rolling_n"]) == ([200], [2]), "Sep 20 - Oct 17: 400 seconds over 2 calls"
    assert measure(body, "satisfaction")["team"]["rolling"] == [57] and measure(body, "avg_call_time")["team"][
        "rolling_n"] == [2], "one tech: the team pools the same"
    assert [s["label"] for s in body["steps"]] == ["Oct 11"], "the steps are the view's alone"


def test_a_monthly_graph_pools_every_measure_over_two_months(tmp_path):
    """The owner, 2026-10-22: on a monthly graph (Last 6 months, Last year) every measure's point pools its month and
    the one before, as every measure pools on a weekly graph; each month's own figure is still its step."""
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    ratings(conn, A_ID, date(2026, 9, 6), good=3, bad=1)
    ratings(conn, A_ID, date(2026, 10, 13), good=0, bad=1)
    calls(conn, (A_ID, "completed", at(date(2026, 9, 14), 10), 600), (A_ID, "completed", at(date(2026, 10, 12), 10), 100))
    body = trends.trends_view(conn, period(date(2026, 5, 1), date(2026, 10, 17), "last_6_months"), None, today=LATER)
    assert body["unit"] == "month" and [s["label"] for s in body["steps"]][4:] == ["Sep", "Oct"]
    sat, avg = tech(body, "satisfaction"), tech(body, "avg_call_time")
    assert all(m["window"] == {"count": 2, "unit": "month"} for m in body["measures"])
    assert (sat["steps"][4:], sat["steps_n"][4:]) == ([75, 0], [4, 1]), "each month on its own"
    assert (sat["rolling"][4:], sat["rolling_n"][4:]) == ([75, 60], [4, 5]), "October with September: 3 good of 5"
    assert (avg["steps"][4:], avg["rolling"][4:], avg["rolling_n"][4:]) == ([600, 100], [600, 350], [1, 2])


def test_a_totals_line_averages_its_weeks_on_the_steps_scale(tmp_path):
    """Sick days and Signed in, no activity are totals: their line is the average of each week's own total over the
    window (weeks with no scheduled shift left out), so one sick day reads 0.25 a week over 4 weeks, not 1 for each
    of them; on a monthly graph, the average of the two months."""
    conn = make_db(tmp_path)
    shift(conn)
    day = date(2026, 9, 1)
    while day < TODAY:
        if day != date(2026, 9, 27):
            busy(conn, A, day, 1)
        day += timedelta(days=1)
    body = trends.trends_view(conn, period(date(2026, 9, 27), date(2026, 10, 10)), None, today=TODAY)
    sick = tech(body, "missed_shifts")
    assert sick["steps"] == [1, 0], "Sep 27 not worked"
    assert measure(body, "missed_shifts")["line_digits"] == 1
    assert sick["rolling"] == [0.3, 0.3], "1 over 4 weeks of shifts: 0.25 a week, each week"
    assert measure(body, "missed_shifts")["team"]["rolling"] == [0.3, 0.3]
    month = trends.trends_view(conn, period(date(2026, 5, 1), date(2026, 10, 16), "last_6_months"), None, today=TODAY)
    sick = tech(month, "missed_shifts")
    assert (sick["steps"][4:], sick["rolling"][4:]) == ([1, 0], [1, 0.5]), "Sep: 1 (Aug had no shifts); Oct: (1+0)/2"


def test_a_monthly_window_reaches_back_before_the_view(tmp_path):
    """The first month's Satisfaction point pools the month before the view starts, read for it."""
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    ratings(conn, A_ID, date(2026, 9, 6), good=1, bad=1)
    ratings(conn, A_ID, date(2026, 10, 13), good=2, bad=0)
    body = trends.trends_view(conn, period(date(2026, 10, 1), date(2026, 10, 17), "last_6_months"), None, today=LATER)
    sat = tech(body, "satisfaction")
    assert [s["label"] for s in body["steps"]] == ["Oct"]
    assert (sat["steps"], sat["rolling"], sat["rolling_n"]) == ([100], [75], [4]), "October with September"


def test_call_measures_values(tmp_path):
    """A worked Mon 10/19 and missed Tue 10/20. Calls, declined/missed and call time are per worked shift; the share,
    the declined/missed percent and the average call time are over every day; satisfaction is good out of good and
    bad; aging averages the days a list was recorded for."""
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn)
    monday = date(2026, 10, 19)
    busy(conn, A, monday, 2)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    mondays_calls(conn)
    ratings(conn, A_ID, monday, good=2, bad=1)
    ratings(conn, A_ID, monday + timedelta(days=1), good=1, bad=0)
    ratings(conn, OTHER_ID, monday, good=0, bad=3)                  # someone else's
    aging(conn, A, monday, 4)
    aging(conn, A, monday + timedelta(days=1), 6)
    body = trends.trends_view(conn, period(monday, monday + timedelta(days=1)), None, today=LATER)
    assert {key: tech(body, key)["now"] for key in CALL_KEYS} == {
        "calls_per_shift": 3.0, "calls_share": 50.0, "declined_missed_per_shift": 1.0, "declined_missed_pct": 25.0,
        "call_time_per_shift": 6, "avg_call_time": 120, "satisfaction": 75, "aging_tickets": 5.0}
    assert {key: measure(body, key)["team"]["now"] for key in CALL_KEYS} == {
        key: tech(body, key)["now"] for key in CALL_KEYS}
    assert tech(body, "missed_shifts")["now"] == 1 and tech(body, "tickets_per_shift")["now"] == 2.0


def test_rest_day_call_counts_in_share_not_per_shift(tmp_path):
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn, days={1, 2})                                         # Monday and Tuesday
    monday, sunday = date(2026, 10, 19), date(2026, 10, 18)
    busy(conn, A, monday, 2)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    mondays_calls(conn)
    week = period(sunday, monday + timedelta(days=1))
    before = trends.trends_view(conn, week, None, today=LATER)
    calls(conn, (A_ID, "completed", at(sunday, 10), 30))             # a call on the rest day
    after = trends.trends_view(conn, week, None, today=LATER)
    assert tech(before, "calls_per_shift")["now"] == tech(after, "calls_per_shift")["now"] == 3.0
    assert tech(before, "call_time_per_shift")["now"] == tech(after, "call_time_per_shift")["now"] == 6
    assert (tech(before, "calls_share")["now"], tech(after, "calls_share")["now"]) == (50.0, 57.1)   # 4 of 7
    assert (tech(before, "avg_call_time")["now"], tech(after, "avg_call_time")["now"]) == (120, 98)  # 390 / 4


def test_no_ratings_is_no_value(tmp_path):
    """A step with no ratings has no satisfaction, never 0, though its calls are counted; so has a period with none,
    and one with no aging list recorded has no aging figure."""
    conn = make_db(tmp_path, through=LATER_SYNCED)
    shift(conn)
    first, second = date(2026, 10, 12), date(2026, 10, 19)
    for day in (first, second):
        busy(conn, A, day, 2)
        mondays_calls(conn, day)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    ratings(conn, A_ID, second, good=1, bad=0)
    body = trends.trends_view(conn, period(date(2026, 10, 11), date(2026, 10, 20)), None, today=LATER)
    assert [s["label"] for s in body["steps"]] == ["Oct 11", "Oct 18"]
    assert tech(body, "satisfaction")["steps"] == [None, 100]
    assert measure(body, "satisfaction")["team"]["steps"] == [None, 100]
    assert tech(body, "calls_share")["steps"] == [50.0, 50.0]
    alone = trends.trends_view(conn, period(date(2026, 10, 11), date(2026, 10, 17)), None, today=LATER)
    assert tech(alone, "satisfaction")["now"] is None and measure(alone, "satisfaction")["team"]["now"] is None
    assert tech(alone, "aging_tickets")["now"] is None


def test_tech_without_agent_has_no_call_values(tmp_path):
    """B's Zendesk id was never stored, so none of B's days has collected calls: every call measure is no value for
    B, never a zero, and B's worked shift doesn't thin the team's calls per shift."""
    conn = make_db(tmp_path, team=(A, B), through=LATER_SYNCED)
    monday = date(2026, 10, 19)
    for email in (A, B):
        shift(conn, email)
        busy(conn, email, monday, 2)
    collected.set_agents(conn, {A: A_ID})
    collect_all(conn)
    mondays_calls(conn)
    ratings(conn, A_ID, monday, good=1, bad=0)
    body = trends.trends_view(conn, period(date(2026, 10, 18), date(2026, 10, 20)), None, today=LATER)
    for key in CALL_KEYS[:-1]:
        assert tech(body, key, A)["now"] is not None, key
    for key in CALL_KEYS:
        row = tech(body, key, B)
        assert row["now"] is None and row["steps"] == [None], key
    assert measure(body, "calls_per_shift")["team"]["now"] == 3.0
    assert tech(body, "tickets_per_shift", B)["now"] == 2.0


def test_team_share_and_aging_are_the_mean_of_the_techs(tmp_path):
    """The team's % of calls and aging tickets are the mean of the techs' values, as the counts are: aging (4 + 6) / 2
    for A and 2 for B is 3.5, not the 12 tickets over 3 recorded days pooled."""
    conn = make_db(tmp_path, team=(A, B), through=LATER_SYNCED)
    monday = date(2026, 10, 19)
    for email in (A, B):
        shift(conn, email)
        busy(conn, email, monday, 2)
    collected.set_agents(conn, {A: A_ID, B: B_ID})
    collect_all(conn)
    mondays_calls(conn)
    aging(conn, A, monday, 4)
    aging(conn, A, monday + timedelta(days=1), 6)
    aging(conn, B, monday, 2)
    body = trends.trends_view(conn, period(monday, monday + timedelta(days=1)), None, today=LATER)
    assert (tech(body, "aging_tickets", A)["now"], tech(body, "aging_tickets", B)["now"]) == (5.0, 2.0)
    assert measure(body, "aging_tickets")["team"]["now"] == 3.5
    assert (tech(body, "calls_share", A)["now"], tech(body, "calls_share", B)["now"]) == (50.0, 0.0)
    assert measure(body, "calls_share")["team"]["now"] == 25.0


def test_the_departments_calls_and_ratings_are_read_once_for_every_tech(tmp_path, monkeypatch):
    """One read of the department's legs, calls and ratings serves every tech's days (`calldays.call_days`), not one
    read per tech; the figures are as before."""
    conn = make_db(tmp_path, team=(A, B), through=LATER_SYNCED)
    monday = date(2026, 10, 19)
    for email in (A, B):
        shift(conn, email)
        busy(conn, email, monday, 2)
    collected.set_agents(conn, {A: A_ID, B: B_ID})
    collect_all(conn)
    mondays_calls(conn)
    ratings(conn, A_ID, monday, good=2, bad=1)
    reads = []
    for name in ("talk_between", "ratings_between"):
        def spy(conn, start, end, name=name, real=getattr(collected, name)):
            reads.append(name)
            return real(conn, start, end)
        monkeypatch.setattr(collected, name, spy)
    body = trends.trends_view(conn, period(monday, monday + timedelta(days=1)), None, today=LATER)
    assert sorted(reads) == ["ratings_between", "talk_between"]
    assert (tech(body, "calls_share", A)["now"], tech(body, "calls_share", B)["now"]) == (50.0, 0.0)
    assert (tech(body, "satisfaction", A)["now"], tech(body, "satisfaction", B)["now"]) == (67, None)
