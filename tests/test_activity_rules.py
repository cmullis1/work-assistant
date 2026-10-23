from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from work_assistant.activity.model import Action, Flag, ShiftVersion, SignIn, Thresholds
from work_assistant.activity.rules import day_facts

TZ = "America/Chicago"
CHI = ZoneInfo(TZ)
TH = Thresholds()
EMAIL = "a@x.com"
# Sun-Thu 08:00-17:00
SHIFT = ShiftVersion(EMAIL, date(2026, 1, 1), frozenset({0, 1, 2, 3, 4}), 480, 1020, True)
TODAY = date(2026, 11, 30)


def local(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CHI).astimezone(timezone.utc)


def act(day, hh, mm, kind="reply", ticket=1, new=None, n=[0]):
    n[0] += 1
    return Action(EMAIL, ticket, local(day, hh, mm), kind, None, new, n[0], 0)


# Synced from long before the days these tests judge through the end of TODAY: every day is judged in full.
SYNCED_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
SYNCED_THROUGH = local(date(2026, 12, 1), 0, 0)


def facts(day, actions=(), signins=(), shift=SHIFT, off=None, today=TODAY, synced_from=SYNCED_FROM,
          synced_through=SYNCED_THROUGH, solved_at=()):
    return day_facts(day, list(actions), list(signins), shift, off, TH, TZ, today,
                     synced_from=synced_from, synced_through=synced_through, solved_at=solved_at)


def test_thursday_oct_15_late_start_and_long_gap():
    d = date(2026, 10, 15)
    times = [(9, 56), (10, 1), (10, 3), (10, 8), (10, 9), (10, 51), (11, 26), (15, 31), (16, 6), (16, 35)]
    f = facts(d, [act(d, h, m, ticket=i % 3) for i, (h, m) in enumerate(times)])
    assert f.state == "worked"
    assert f.gap_min == 245
    assert f.gap_start == local(d, 11, 26)
    assert f.flags == (Flag("late_start", 116), Flag("long_gap", 245))
    assert f.tickets == 3
    assert f.replies == 10


def test_counts_by_kind_and_solved():
    """Solved is the tickets solved while assigned to the tech (`solved_at`) inside the day's window on their clock; a
    status change to solved counts for nothing."""
    d = date(2026, 10, 15)
    actions = [
        act(d, 8, 5, "reply", ticket=1),
        act(d, 8, 10, "note", ticket=2),
        act(d, 8, 15, "call", ticket=3),
        act(d, 8, 20, "status", ticket=1, new="solved"),
        act(d, 8, 25, "status", ticket=2, new="pending"),
    ]
    f = facts(d, actions)
    assert (f.tickets, f.replies, f.notes, f.calls, f.solved) == (3, 1, 1, 1, 0)
    assert facts(d, actions, solved_at=[local(d, 10, 0)]).solved == 1
    assert facts(d, actions, solved_at=[local(date(2026, 10, 16), 0, 0)]).solved == 0, "the next local day's"


def test_signed_in_no_activity_flag():
    d = date(2026, 10, 15)
    f = facts(d, signins=[SignIn(EMAIL, local(d, 8, 4))])
    assert f.state == "zero"
    assert f.flags == (Flag("no_activity", None),)
    assert f.signin == local(d, 8, 4)


def test_missed_shift():
    f = facts(date(2026, 10, 15))
    assert f.state == "missed"
    assert f.flags == (Flag("missed", None),)


def test_pto_day_not_missed():
    f = facts(date(2026, 10, 15), off="pto")
    assert f.state == "pto"
    assert f.flags == ()


def test_rest_day_with_no_actions_is_rest():
    f = facts(date(2026, 10, 16))
    assert f.state == "rest"
    assert f.flags == ()


def test_rest_day_with_actions_is_off_not_flagged():
    d = date(2026, 10, 16)  # Friday
    f = facts(d, [act(d, 12, 0)])
    assert f.state == "off"
    assert f.flags == ()
    assert f.replies == 1


def test_before_first_version_is_untracked():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 12, 0)], shift=None)
    assert f.state == "untracked"
    assert f.flags == ()
    assert f.shift is None
    assert f.replies == 1


def test_inactive_version_is_untracked():
    d = date(2026, 10, 15)
    gone = ShiftVersion(EMAIL, date(2026, 10, 1), frozenset(), 0, 0, False)
    f = facts(d, shift=gone)
    assert f.state == "untracked"
    assert f.flags == ()


def test_thresholds_inclusive():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 9, 30), act(d, 15, 30)])
    assert Flag("late_start", 90) in f.flags
    assert Flag("early_finish", 90) in f.flags
    assert f.gap_min == 360
    assert Flag("long_gap", 360) in f.flags


def test_below_thresholds_no_flags():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 9, 29), act(d, 11, 28), act(d, 13, 27), act(d, 15, 26), act(d, 16, 30)])
    assert f.gap_min == 119
    assert f.flags == ()


def test_gap_outside_shift_ignored():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 7, 0), act(d, 12, 0)])
    assert f.first == local(d, 7, 0)
    assert f.last == local(d, 12, 0)
    assert f.gap_min == 0
    assert f.gap_start is None
    assert Flag("late_start", 240) in f.flags


def test_dst_day_nov_1_2026():
    d = date(2026, 11, 1)  # Sunday; clocks fell back at 02:00, so 08:30 is CST (UTC-6)
    f = facts(d, [act(d, 8, 30), act(d, 16, 30)])
    assert f.first == datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)
    assert f.last == datetime(2026, 11, 1, 22, 30, tzinfo=timezone.utc)
    assert f.state == "worked"
    assert f.gap_min == 480
    assert not any(fl.kind == "early_finish" for fl in f.flags)


def test_future_day():
    f = facts(date(2026, 12, 1), today=date(2026, 11, 30))
    assert f.state == "future"
    assert f.flags == ()


# -- judged only as far as the data is synced (final review F1) ------------------------------------------------

MONDAY = date(2026, 10, 19)


def test_after_the_six_am_sync_today_is_not_missed():
    """The daily sync at 06:00, before an 08:00 shift: today's shift has not happened yet as of the data."""
    f = facts(MONDAY, today=MONDAY, synced_through=local(MONDAY, 6, 0))
    assert f.state == "future" and f.flags == ()
    signed_in = facts(MONDAY, signins=[SignIn(EMAIL, local(MONDAY, 5, 55))], today=MONDAY,
                      synced_through=local(MONDAY, 6, 0))
    assert signed_in.state == "future" and signed_in.flags == ()


def test_a_mid_shift_sync_gives_no_early_finish_or_missed_shift():
    sync = local(MONDAY, 11, 0)
    worked = facts(MONDAY, [act(MONDAY, 8, 10), act(MONDAY, 8, 40)], today=MONDAY, synced_through=sync)
    assert worked.state == "worked" and worked.flags == (), "no early finish while the shift is still going"
    idle = facts(MONDAY, today=MONDAY, synced_through=local(MONDAY, 9, 0))
    assert idle.state == "unsynced" and idle.flags == (), "no missed shift before the shift is over"
    signed_in = facts(MONDAY, signins=[SignIn(EMAIL, local(MONDAY, 8, 2))], today=MONDAY, synced_through=sync)
    assert signed_in.state == "unsynced" and signed_in.flags == (), "no signed-in-no-activity flag yet"


def test_a_mid_shift_sync_keeps_gaps_between_actions_and_a_late_start_that_has_happened():
    sync = local(MONDAY, 14, 0)
    f = facts(MONDAY, [act(MONDAY, 9, 45), act(MONDAY, 12, 0)], today=MONDAY, synced_through=sync)
    assert f.flags == (Flag("late_start", 105), Flag("long_gap", 135))
    # Nothing in the shift yet, only before it: late once the limit has passed by the sync, not before.
    early = [act(MONDAY, 7, 30)]
    assert facts(MONDAY, early, today=MONDAY, synced_through=local(MONDAY, 9, 29)).flags == ()
    late = facts(MONDAY, early, today=MONDAY, synced_through=local(MONDAY, 9, 30))
    assert late.state == "worked" and late.flags == (Flag("late_start", None),)


def test_a_sync_after_the_shift_ended_judges_today_in_full():
    f = facts(MONDAY, [act(MONDAY, 8, 10)], today=MONDAY, synced_through=local(MONDAY, 18, 0))
    assert f.flags == (Flag("early_finish", 530),)
    assert facts(MONDAY, today=MONDAY, synced_through=local(MONDAY, 18, 0)).state == "missed"


def test_days_before_the_first_sync_or_after_the_last_or_with_none_are_unsynced():
    first = local(date(2026, 10, 1), 0, 0)
    last = local(date(2026, 10, 15), 6, 0)
    before = facts(date(2026, 9, 30), synced_from=first, synced_through=last)
    assert before.state == "unsynced" and before.flags == ()
    after = facts(date(2026, 10, 18), synced_from=first, synced_through=last)
    assert after.state == "unsynced" and after.flags == ()
    never = facts(date(2026, 10, 15), synced_from=None, synced_through=None)
    assert never.state == "unsynced" and never.flags == ()
    partial = facts(date(2026, 10, 1), synced_from=local(date(2026, 10, 1), 6, 0), synced_through=last)
    assert partial.state == "unsynced", "a day the first sync reached partway is not judged"


def test_a_fully_synced_past_day_is_still_judged():
    first = local(date(2026, 10, 1), 0, 0)
    last = local(date(2026, 10, 19), 6, 0)
    missed = facts(date(2026, 10, 15), today=MONDAY, synced_from=first, synced_through=last)
    assert missed.state == "missed" and missed.flags == (Flag("missed", None),)
    first_day = facts(date(2026, 10, 1), [act(date(2026, 10, 1), 8, 0), act(date(2026, 10, 1), 15, 0)],
                      today=MONDAY, synced_from=first, synced_through=last)
    assert first_day.state == "worked" and first_day.flags == (Flag("early_finish", 120), Flag("long_gap", 420))


def test_untracked_and_future_are_not_unsynced():
    assert facts(date(2026, 10, 15), shift=None, synced_from=None, synced_through=None).state == "untracked"
    assert facts(date(2026, 12, 1), synced_from=None, synced_through=None).state == "future"


# -- the shift is kept only on days the tech was due to work (final review F2) -------------------------------


def test_shift_only_on_scheduled_days():
    d = date(2026, 10, 15)
    assert facts(d, [act(d, 9, 0)]).shift == SHIFT
    assert facts(d).shift == SHIFT, "a missed shift keeps its hours"
    assert facts(date(2026, 10, 16), [act(date(2026, 10, 16), 12, 0)]).shift is None, "a worked rest day has none"
    assert facts(date(2026, 10, 16)).shift is None, "nor a rest day"
    assert facts(d, off="pto").shift is None, "nor time off"
    assert facts(date(2026, 12, 3)).shift == SHIFT, "a scheduled day still to come keeps its hours"


# -- actions inside the shift (Trends) -------------------------------------------------------------------------


def test_in_shift_counts_actions_inside_the_shift():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, h, m) for h, m in [(7, 50), (8, 0), (12, 0), (17, 0), (17, 30)]])
    assert f.in_shift == 3


def test_in_shift_is_zero_on_rest_days_and_time_off():
    rest = date(2026, 10, 16)  # Friday
    assert facts(rest, [act(rest, 9, 0), act(rest, 10, 0)]).in_shift == 0
    d = date(2026, 10, 15)
    assert facts(d, [act(d, 9, 0)], off="pto").in_shift == 0
    assert facts(d, [act(d, 9, 0)], shift=None).in_shift == 0


def test_first_in_shift_is_the_first_action_inside_the_shift():
    """Ruling 41: the first action at or after the shift start (and by its end), the one the Late start flag
    measures; None on a day with no action inside the shift or no shift due."""
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 7, 40), act(d, 12, 0)])
    assert f.first == local(d, 7, 40) and f.first_in_shift == local(d, 12, 0)
    assert f.flags[0] == Flag("late_start", 240)
    assert facts(d, [act(d, 8, 0), act(d, 9, 0)]).first_in_shift == local(d, 8, 0)
    assert facts(d, [act(d, 7, 0), act(d, 17, 30)]).first_in_shift is None
    assert facts(d, [act(d, 9, 0)], off="pto").first_in_shift is None
    rest = date(2026, 10, 16)  # Friday
    assert facts(rest, [act(rest, 9, 0)]).first_in_shift is None
    assert facts(d).first_in_shift is None


# -- a marked sick day (off="sick") ----------------------------------------------------------------------------


def test_a_marked_sick_day_is_a_missed_day_whatever_was_recorded():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 9, 0), act(d, 9, 5, ticket=2)], signins=[SignIn(EMAIL, local(d, 8, 4))], off="sick")
    assert f.state == "missed"
    assert f.flags == (Flag("missed", None),)
    assert f.shift == SHIFT, "a Sick day keeps its hours, as an automatic one does"
    assert f.replies == 2, "the recorded work is still on the day"
    assert (f.in_shift, f.first_in_shift) == (0, None), "but none of it counts as time on shift"
    assert facts(d, off="sick").flags == (Flag("missed", None),)


def test_a_marked_sick_day_has_no_late_start_early_finish_or_long_gap():
    d = date(2026, 10, 15)
    f = facts(d, [act(d, 11, 0), act(d, 15, 0)], off="sick")   # would be a late start, an early finish, a long gap
    assert f.state == "missed"
    assert [flag.kind for flag in f.flags] == ["missed"]
    assert f.gap_min == 0 and f.gap_start is None


def test_a_sick_mark_on_a_rest_day_counts_nothing():
    d = date(2026, 10, 16)   # Friday
    assert facts(d, off="sick").state == "rest"
    f = facts(d, [act(d, 12, 0)], off="sick")
    assert f.state == "off" and f.flags == () and f.shift is None


def test_a_sick_mark_is_judged_after_the_untracked_and_unsynced_checks():
    d = date(2026, 10, 15)
    assert facts(d, off="sick", shift=None).state == "untracked"
    assert facts(d, off="sick", synced_from=None, synced_through=None).state == "unsynced"
    assert facts(date(2026, 12, 3), off="sick").state == "future"
