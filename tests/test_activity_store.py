from datetime import date, datetime, timedelta, timezone

from work_assistant import settings
from work_assistant.activity import store
from work_assistant.activity.model import Action, ShiftVersion, SignIn, Thresholds

EMAIL = "pat@example.com"


def _v(effective_from, days, start=480, end=1020, active=True):
    return ShiftVersion(EMAIL, effective_from, frozenset(days), start, end, active)


def test_shift_on_uses_latest_version_on_or_before_day(conn):
    store.add_shift_version(conn, _v(date(2026, 10, 1), {0, 1, 2, 3, 4}))
    store.add_shift_version(conn, _v(date(2026, 11, 1), {2, 3, 4, 5, 6}))
    assert store.shift_on(conn, EMAIL, date(2026, 10, 31)).work_days == frozenset({0, 1, 2, 3, 4})
    assert store.shift_on(conn, EMAIL, date(2026, 11, 1)).work_days == frozenset({2, 3, 4, 5, 6})
    assert [v.effective_from for v in store.shift_versions(conn, EMAIL)] == [
        date(2026, 10, 1), date(2026, 11, 1)]


def test_shift_on_before_first_version_is_none(conn):
    store.add_shift_version(conn, _v(date(2026, 10, 1), {1}))
    assert store.shift_on(conn, EMAIL, date(2026, 9, 30)) is None
    assert store.shift_on(conn, "nobody@example.com", date(2026, 10, 5)) is None


def test_same_effective_date_replaces_version(conn):
    store.add_shift_version(conn, _v(date(2026, 10, 1), {1}))
    store.add_shift_version(conn, _v(date(2026, 10, 1), {2, 3}, start=540, end=1080, active=False))
    versions = store.shift_versions(conn, EMAIL)
    assert versions == [_v(date(2026, 10, 1), {2, 3}, start=540, end=1080, active=False)]


def test_time_off_range_inclusive(conn):
    tid = store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 13), "trip")
    assert store.on_time_off(conn, EMAIL, date(2026, 10, 12))
    assert store.on_time_off(conn, EMAIL, date(2026, 10, 13))
    assert not store.on_time_off(conn, EMAIL, date(2026, 10, 14))
    assert not store.on_time_off(conn, EMAIL, date(2026, 10, 11))
    assert [t.id for t in store.time_off(conn, EMAIL)] == [tid]
    assert store.time_off(conn, "other@example.com") == []
    store.remove_time_off(conn, tid)
    assert store.time_off(conn) == []


def test_remove_time_off_day_single_day_entry_deleted(conn):
    store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 12), "dentist")
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 12)) is True
    assert store.time_off(conn) == []


def test_remove_time_off_day_trims_first_and_last(conn):
    store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 14), "trip")
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 12)) is True
    assert [(t.first, t.last) for t in store.time_off(conn)] == [(date(2026, 10, 13), date(2026, 10, 14))]
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 14)) is True
    assert [(t.first, t.last, t.note) for t in store.time_off(conn)] == [
        (date(2026, 10, 13), date(2026, 10, 13), "trip")]


def test_remove_time_off_day_splits_range(conn):
    first = store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 14), "trip")
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 13)) is True
    items = store.time_off(conn, EMAIL)
    assert [(t.first, t.last, t.note) for t in items] == [
        (date(2026, 10, 12), date(2026, 10, 12), "trip"), (date(2026, 10, 14), date(2026, 10, 14), "trip")]
    assert items[0].id == first
    assert not store.on_time_off(conn, EMAIL, date(2026, 10, 13))


def test_remove_time_off_day_no_entry_returns_false(conn):
    store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 14), "trip")
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 15)) is False
    assert store.remove_time_off_day(conn, "other@example.com", date(2026, 10, 13)) is False
    assert len(store.time_off(conn)) == 1


def test_thresholds_default_90_90_120(conn):
    assert store.thresholds(conn) == Thresholds(90, 90, 120)


def test_threshold_settings_reject_zero():
    for key in ("activity_late_minutes", "activity_early_minutes", "activity_gap_minutes"):
        cleaned, problems = settings.validate_changes({key: 0})
        assert key in problems
    cleaned, problems = settings.validate_changes({"activity_gap_minutes": 45})
    assert not problems and cleaned == {"activity_gap_minutes": 45}


# -- actions, sign-ins, subjects and sync bookkeeping --------------------------------------------

T0 = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def _a(event_id, at=T0, child_id=1, email=EMAIL, kind="reply", ticket_id=5, old=None, new=None):
    return Action(email, ticket_id, at, kind, old, new, event_id, child_id)


def test_insert_actions_ignores_repeats(conn):
    rows = [_a(1), _a(2, kind="status", old="open", new="solved")]
    assert store.insert_actions(conn, rows) == 2
    assert store.insert_actions(conn, rows) == 0
    assert store.insert_actions(conn, [_a(1, child_id=2)]) == 1  # another child of the same event is new
    assert conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0] == 3


def test_actions_between_is_half_open_and_per_tech(conn):
    store.insert_actions(conn, [
        _a(1, T0), _a(2, T0 + timedelta(hours=1)), _a(3, T0 + timedelta(hours=2)),
        _a(4, T0, email="other@example.com"),
    ])
    got = store.actions_between(conn, EMAIL, T0, T0 + timedelta(hours=2))
    assert [a.event_id for a in got] == [1, 2]
    assert got[0] == _a(1, T0)  # round trip keeps every field, with a UTC-aware time


def test_actions_between_orders_by_time(conn):
    store.insert_actions(conn, [_a(9, T0 + timedelta(minutes=5)), _a(3, T0)])
    assert [a.event_id for a in store.actions_between(conn, EMAIL, T0, T0 + timedelta(hours=1))] == [3, 9]


def test_signins_between(conn):
    rows = [SignIn(EMAIL, T0), SignIn(EMAIL, T0 + timedelta(hours=3)), SignIn("other@example.com", T0)]
    assert store.insert_signins(conn, rows) == 3
    assert store.insert_signins(conn, rows) == 0
    assert store.signins_between(conn, EMAIL, T0, T0 + timedelta(hours=1)) == [SignIn(EMAIL, T0)]


def test_subjects_maps_every_requested_id(conn):
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject) VALUES (1, 'Hello'), (2, NULL)")
    conn.commit()
    assert store.subjects(conn, [1, 2, 3]) == {1: "Hello", 2: None, 3: None}
    assert store.subjects(conn, []) == {}


def test_latest_sync_and_synced_through(conn):
    assert store.latest_sync(conn) is None
    assert store.synced_through(conn) is None
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, synced_through)"
                 " VALUES ('daily', 'complete', '2026-10-15T10:00:00Z', '2026-10-15T10:00:00Z')")
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, error)"
                 " VALUES ('refresh', 'failed', '2026-10-15T11:00:00Z', 'x')")
    conn.commit()
    assert store.latest_sync(conn)["status"] == "failed"
    assert store.synced_through(conn) == datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)


def test_purge_activity_counts_what_it_erased(conn):
    store.insert_actions(conn, [_a(1, T0, kind="fields", old="a", new="b")])
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject) VALUES (5, 'Secret')")
    conn.commit()
    cutoff = T0 + timedelta(days=1)
    assert store.purge_activity(conn, cutoff) == 2   # one subject, one action's values
    assert store.purge_activity(conn, cutoff) == 0


def test_purge_activity_erases_a_subject_that_no_action_or_open_ticket_holds(conn):
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject) VALUES (8, 'Orphan'), (9, 'Open one')")
    conn.execute("INSERT INTO activity_open (email, captured_at, ticket_id, status) VALUES (?, 'x', 9, 'open')",
                 (EMAIL,))
    conn.commit()
    assert store.purge_activity(conn, T0) == 1
    assert store.subjects(conn, [8, 9]) == {8: None, 9: 'Open one'}
    assert conn.execute("SELECT purged FROM activity_tickets WHERE ticket_id = 8").fetchone()[0] == 1


# -- the kind of time off -----------------------------------------------------------------------------------


def test_time_off_kind_defaults_to_pto_and_round_trips(conn):
    pto = store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 12), "")
    sick = store.add_time_off(conn, EMAIL, date(2026, 10, 13), date(2026, 10, 13), "flu", "sick")
    assert {t.id: t.kind for t in store.time_off(conn, EMAIL)} == {pto: "pto", sick: "sick"}


def test_edit_time_off(conn):
    tid = store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 14), "")
    assert store.edit_time_off(conn, tid, date(2026, 10, 13), date(2026, 10, 15), "flu", "sick") is True
    item = store.time_off(conn)[0]
    assert (item.first, item.last, item.note, item.kind) == (date(2026, 10, 13), date(2026, 10, 15), "flu", "sick")
    assert store.edit_time_off(conn, tid + 1, date(2026, 10, 13), date(2026, 10, 15), "", "pto") is False


def test_undoing_one_day_of_a_three_day_sick_entry_leaves_two_sick_days(conn):
    store.add_time_off(conn, EMAIL, date(2026, 10, 12), date(2026, 10, 14), "flu", "sick")
    assert store.remove_time_off_day(conn, EMAIL, date(2026, 10, 13)) is True
    items = store.time_off(conn, EMAIL)
    assert [(t.first, t.last, t.kind, t.note) for t in items] == [
        (date(2026, 10, 12), date(2026, 10, 12), "sick", "flu"),
        (date(2026, 10, 14), date(2026, 10, 14), "sick", "flu")]
    store.remove_time_off_day(conn, EMAIL, date(2026, 10, 12))
    assert [(t.first, t.kind) for t in store.time_off(conn, EMAIL)] == [(date(2026, 10, 14), "sick")]
    store.add_time_off(conn, EMAIL, date(2026, 10, 19), date(2026, 10, 21), "", "sick")
    store.remove_time_off_day(conn, EMAIL, date(2026, 10, 21))
    assert store.time_off(conn, EMAIL)[-1].kind == "sick"


def _sync_row(conn, status, window_start, history_from=None, history_cursor_ts=None):
    conn.execute(
        "INSERT INTO activity_sync (kind, status, started_at, window_start, history_from, history_cursor_ts)"
        " VALUES ('backfill', ?, '2026-10-20T00:00:00Z', ?, ?, ?)",
        (status, window_start, history_from, history_cursor_ts),
    )
    conn.commit()


def test_synced_from_reaches_back_to_a_completed_history_fill(conn):
    _sync_row(conn, "complete", "2026-07-21T21:37:11Z")
    assert store.synced_from(conn) == datetime(2026, 7, 21, 21, 37, 11, tzinfo=timezone.utc)
    _sync_row(conn, "failed", "2025-10-21T03:00:00Z", history_cursor_ts=1760000000)  # unfinished fill: no change
    assert store.synced_from(conn) == datetime(2026, 7, 21, 21, 37, 11, tzinfo=timezone.utc)
    _sync_row(conn, "failed", "2025-10-21T03:00:00Z", history_from="2025-10-21T03:00:00Z")  # fill done, rest failed
    assert store.synced_from(conn) == datetime(2025, 10, 21, 3, 0, tzinfo=timezone.utc)


def test_synced_from_is_none_before_any_complete_sync_even_with_a_fill(conn):
    _sync_row(conn, "failed", "2025-10-21T03:00:00Z", history_from="2025-10-21T03:00:00Z")
    assert store.synced_from(conn) is None
