"""The Team activity API: the range and day views, starting a sync, shifts, time off and team changes."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from work_assistant import credentials, regions
from work_assistant.activity import collected, store
from work_assistant.activity.model import Action, ShiftVersion, SignIn
from work_assistant.db import connect, init_db
from work_assistant.rubric import ensure_seeded
from work_assistant.settings import set_setting
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app

BASE = "http://127.0.0.1:8765"
UTC = timezone.utc
PAT, SAM, NEW = "pat@example.com", "sam@example.com", "new@example.com"
TODAY = date(2026, 10, 17)   # conftest's pinned Saturday; last week is Oct 4-10 (CDT, UTC-5)
SUN_THU = frozenset({0, 1, 2, 3, 4})
LAST_WEEK = {"period": "last_week"}   # the tests' own week; the grid's default is This week (so far) since T-143


class FakeZendesk:
    def __init__(self, known=()):
        self.known = set(known)

    def users_by_email(self, emails):
        return {e: {"id": i + 1, "email": e, "role": "agent"} for i, e in enumerate(emails) if e in self.known}

    def close(self):
        pass


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "work-assistant.db"
    conn = connect(path)
    init_db(conn)
    ensure_seeded(conn)
    set_setting(conn, "timezone", "America/Chicago")
    set_setting(conn, "team", [PAT])
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def started():
    return []


@pytest.fixture
def client(db_path, tmp_path, started):
    app = create_app(db_path, run_starter=lambda spec: started.append(spec) or 4242,
                     zendesk_factory=lambda: FakeZendesk({PAT, SAM, NEW}), log_dir=tmp_path / "logs",
                     startup_work=False)
    return TestClient(app, base_url=BASE)


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


_event = iter(range(1, 10_000))


def act(email, ticket_id, at, kind="reply", old=None, new=None, event_id=None, child_id=1):
    return Action(email, ticket_id, at, kind, old, new, event_id or next(_event), child_id)


def shift(db, email=PAT, first=date(2026, 9, 1), days=SUN_THU, start=480, end=1020, active=True):
    store.add_shift_version(db, ShiftVersion(email, first, frozenset(days), start, end, active))


def synced(db, through="2026-10-17T11:00:00Z", window_start="2026-09-01T05:00:00Z", started_at=None):
    """A complete sync that read from `window_start` (Sep 1, 00:00 in Chicago) up to `through` (06:00 on the pinned
    Saturday, Oct 17)."""
    db.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
               " VALUES ('daily', 'complete', ?, ?, ?, ?)", (started_at or through, through, through, window_start))
    db.commit()


def cells(body, email=PAT):
    row = next(r for r in body["rows"] if r["email"] == email)
    return {c["day"]: c for c in row["cells"]}


# -- the range view ---------------------------------------------------------------------------------------


def test_activity_week_cells_and_states(client, db):
    shift(db)
    unsynced = cells(client.get("/api/activity", params=LAST_WEEK).json())
    assert {c["state"] for c in unsynced.values()} == {"unsynced"}, "with no sync at all, no day is judged"
    assert all(c["flags"] == [] for c in unsynced.values())
    synced(db)
    store.insert_actions(db, [
        act(PAT, 11, utc(5, 13, 30)),                      # Mon 08:30 local
        act(PAT, 11, utc(5, 14, 30), "note"),              # Mon 09:30
        act(PAT, 12, utc(5, 21, 30), "status", "open", "solved"),   # Mon 16:30
        act(PAT, 13, utc(10, 15, 0)),                      # Sat, a rest day
    ])
    collected.set_agents(db, {PAT: 77})                    # ticket 12 solved while assigned to Pat: Solved (T-158)
    collected.upsert_ticket_states(db, [{"id": 12, "assignee_id": 77, "status": "solved"}],
                                   [{"ticket_id": 12, "solved_at": "2026-10-05T21:30:00Z"}])
    store.insert_signins(db, [SignIn(PAT, utc(6, 13, 0))])    # Tue: signed in, did nothing
    store.add_time_off(db, PAT, date(2026, 10, 8), date(2026, 10, 8), "dentist")

    body = client.get("/api/activity", params=LAST_WEEK).json()

    assert body["period"] == {"start": "2026-10-04", "end": "2026-10-10", "label": body["period"]["label"],
                              "so_far": False, "preset": "last_week"}
    assert body["today"] == "2026-10-17"
    assert body["synced"] == {"at": "2026-10-17T11:00:00Z", "status": "complete", "running": False}
    assert body["techs"] == [{"email": PAT, "name": PAT, "region": "NASA", "has_shift": True}]
    assert body["days"] == [f"2026-10-{d:02d}" for d in range(4, 11)]
    assert [r["email"] for r in body["rows"]] == [PAT]
    by_day = cells(body)
    assert [by_day[d]["state"] for d in body["days"]] == [
        "missed", "worked", "zero", "missed", "pto", "rest", "off"]

    monday = by_day["2026-10-05"]
    assert monday == {
        "day": "2026-10-05", "state": "worked", "tickets": 2, "replies": 1, "notes": 1, "calls": 0, "solved": 1,
        "signin": None, "first": "08:30", "last": "16:30", "gap_min": 420, "gap_start": "09:30",
        "flags": [{"kind": "long_gap", "minutes": 420}], "shift": {"start": "08:00", "end": "17:00"},
        "time_off": None,
    }
    assert by_day["2026-10-06"]["signin"] == "08:00"
    assert by_day["2026-10-06"]["flags"] == [{"kind": "no_activity", "minutes": None}]
    assert by_day["2026-10-07"]["flags"] == [{"kind": "missed", "minutes": None}]
    # Final review F2: the shift's hours only on days the tech was due to work.
    assert by_day["2026-10-07"]["shift"] == {"start": "08:00", "end": "17:00"}
    assert [by_day[d]["shift"] for d in ("2026-10-08", "2026-10-09", "2026-10-10")] == [None, None, None], \
        "time off, a rest day and a worked rest day have none"


def test_after_the_six_am_sync_today_is_not_missed(client, db, monkeypatch):
    """Final review F1: the daily sync runs at 06:00, before an 08:00 shift, so today reads still to come."""
    shift(db)
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 19))      # a Monday
    synced(db, through="2026-10-19T11:00:00Z")                                # 06:00 in Chicago
    today = cells(client.get("/api/activity", params={"start": "2026-10-19", "end": "2026-10-19"}).json())
    assert today["2026-10-19"]["state"] == "future" and today["2026-10-19"]["flags"] == []
    day = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-19"}).json()
    assert day["cell"]["state"] == "future" and day["cell"]["flags"] == []
    assert cells(client.get("/api/activity", params={"start": "2026-10-15", "end": "2026-10-15"}).json()
                 )["2026-10-15"]["state"] == "missed", "a past shift the sync has covered is still judged"


def test_a_mid_shift_refresh_flags_no_early_finish(client, db, monkeypatch):
    shift(db)
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 19))
    store.insert_actions(db, [act(PAT, 1, utc(19, 13, 10)), act(PAT, 2, utc(19, 14, 0))])   # 08:10 and 09:00
    synced(db, through="2026-10-19T15:30:00Z")                                              # Refresh at 10:30
    monday = cells(client.get("/api/activity", params={"start": "2026-10-19", "end": "2026-10-19"}).json())
    assert monday["2026-10-19"]["state"] == "worked" and monday["2026-10-19"]["flags"] == []


def test_days_before_the_first_sync_are_unsynced(client, db):
    shift(db)
    synced(db, window_start="2026-10-07T05:00:00Z")                       # the first sync read from Oct 7
    week = cells(client.get("/api/activity", params=LAST_WEEK).json())    # Oct 4-10
    assert [week[f"2026-10-{d:02d}"]["state"] for d in range(4, 11)] == [
        "unsynced", "unsynced", "unsynced", "missed", "missed", "rest", "rest"]
    assert all(week[f"2026-10-{d:02d}"]["flags"] == [] for d in range(4, 7))


def test_a_sync_stored_before_its_window_was_recorded_counts_back_by_retention(client, db):
    shift(db)
    set_setting(db, "retention_days", 10)
    synced(db, window_start=None, started_at="2026-10-17T11:00:00Z")   # read from Oct 7, 06:00
    week = cells(client.get("/api/activity", params=LAST_WEEK).json())
    assert [week[f"2026-10-{d:02d}"]["state"] for d in (6, 7, 8)] == ["unsynced", "unsynced", "missed"]


def test_a_region_change_dated_after_today_is_not_current_yet(client, db):
    """A region change dated after the app's today (an import can write one) isn't current until its day comes: the
    Tech picker (Activity and Trends), the region filter and the techs each answer lists keep the tech under the
    region in force on the app's today, the pinned Saturday Oct 17 (not the real clock's)."""
    set_setting(db, "team", [PAT, SAM])
    shift(db)
    shift(db, SAM)
    regions.set_regions(db, PAT, [(date(2026, 9, 1), "APAC"), (date(2026, 10, 20), "NASA")])
    body = client.get("/api/activity", params=LAST_WEEK).json()
    assert [(t["email"], t["region"]) for t in body["picker"]] == [(PAT, "APAC"), (SAM, "NASA")]
    assert [(t["email"], t["region"]) for t in body["techs"]] == [(PAT, "APAC"), (SAM, "NASA")]
    apac = client.get("/api/activity", params={**LAST_WEEK, "region": "APAC"}).json()
    assert [r["email"] for r in apac["rows"]] == [PAT]
    trends = client.get("/api/activity/trends").json()
    assert [(t["email"], t["region"]) for t in trends["picker"]] == [(PAT, "APAC"), (SAM, "NASA")]
    assert [(t["email"], t["region"]) for t in trends["techs"]] == [(PAT, "APAC"), (SAM, "NASA")]


def test_the_grid_names_techs_from_their_calls_and_filters_by_tech(client, db):
    set_setting(db, "team", [PAT, SAM])
    shift(db)
    shift(db, SAM)
    db.execute("INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, attribution_ok,"
               " snapshot, fetched_at) VALUES (1, 'voice', '2026-10-06T15:00:00Z', 'SAM@example.com ', 'Sam Able', 1,"
               " '{}', '2026-10-07T00:00:00Z')")
    db.commit()
    body = client.get("/api/activity").json()
    assert body["techs"] == [{"email": PAT, "name": PAT, "region": "NASA", "has_shift": True},
                             {"email": SAM, "name": "Sam Able", "region": "NASA", "has_shift": True}]
    assert [r["email"] for r in body["rows"]] == [PAT, SAM], "rows follow the techs' order"
    assert [r["name"] for r in body["rows"]] == [PAT, "Sam Able"]
    one = client.get("/api/activity", params={"tech": SAM}).json()
    assert [r["email"] for r in one["rows"]] == [SAM]
    assert [t["email"] for t in one["techs"]] == [PAT, SAM], "the picker lists everyone on the team, not just SAM"


def test_unknown_tech_is_a_bad_request(client):
    assert client.get("/api/activity", params={"tech": "nobody@example.com"}).status_code == 400
    assert client.get("/api/activity/day", params={"tech": "nobody@example.com", "day": "2026-10-05"}
                      ).status_code == 400


def test_this_month_includes_future_days(client, db):
    shift(db)
    body = client.get("/api/activity", params={"period": "this_month"}).json()
    assert body["days"] == [f"2026-10-{d:02d}" for d in range(1, 32)]
    assert body["period"]["so_far"] is True
    by_day = cells(body)
    assert all(by_day[f"2026-10-{d:02d}"]["state"] == "future" for d in range(18, 32))
    assert by_day["2026-10-17"]["state"] != "future"


def test_this_week_so_far_shows_the_rest_of_the_week_as_future(client, db, monkeypatch):
    shift(db)
    synced(db, through="2026-10-14T23:00:00Z")   # Wednesday 18:00, after the shift
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 14))     # a Wednesday
    body = client.get("/api/activity", params={"period": "this_week"}).json()
    assert body["days"] == [f"2026-10-{d:02d}" for d in range(11, 18)]
    assert body["period"]["start"] == "2026-10-11" and body["period"]["end"] == "2026-10-17"
    assert [cells(body)[d]["state"] for d in body["days"][-3:]] == ["future"] * 3
    assert cells(body)["2026-10-14"]["state"] == "missed"


def test_the_grid_defaults_to_this_week_so_far(client, db, monkeypatch):
    """T-143: Activity's period defaults to This week (so far), shown as the whole week."""
    shift(db)
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 14))     # a Wednesday
    body = client.get("/api/activity").json()
    assert (body["period"]["preset"], body["period"]["start"]) == ("this_week", "2026-10-11")
    assert body["days"] == [f"2026-10-{d:02d}" for d in range(11, 18)]


def test_a_custom_period_after_today_is_all_future(client, db):
    shift(db)
    body = client.get("/api/activity", params={"start": "2026-10-20", "end": "2026-10-22"}).json()
    assert body["days"] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert {c["state"] for c in cells(body, PAT).values()} == {"future"}


def test_custom_period_and_bad_period(client, db):
    shift(db)
    body = client.get("/api/activity", params={"start": "2026-10-05", "end": "2026-10-06"}).json()
    assert body["days"] == ["2026-10-05", "2026-10-06"] and body["period"]["preset"] == "custom"
    assert client.get("/api/activity", params={"period": "someday"}).status_code == 400


def test_actions_land_on_their_local_day(client, db):
    shift(db)
    # 02:30Z on Tue Oct 6 is Mon Oct 5 21:30 in Chicago.
    store.insert_actions(db, [act(PAT, 1, utc(6, 2, 30))])
    by_day = cells(client.get("/api/activity", params=LAST_WEEK).json())
    assert by_day["2026-10-05"]["replies"] == 1 and by_day["2026-10-06"]["replies"] == 0


def test_a_tech_with_no_shift_is_listed_but_has_no_row(client, db):
    assert client.get("/api/activity").json()["rows"] == []
    assert [r["email"] for r in client.get("/api/activity", params={"tech": PAT}).json()["rows"]] == [PAT]


def test_synced_reports_the_latest_sync(client, db):
    db.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through)"
               " VALUES ('daily', 'complete', '2026-10-17T10:00:00Z', '2026-10-17T10:01:00Z', '2026-10-17T10:00:00Z')")
    db.commit()
    assert client.get("/api/activity").json()["synced"] == {
        "at": "2026-10-17T10:00:00Z", "status": "complete", "running": False}
    db.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running',"
               " ?, ?)", (store.iso(datetime.now(UTC)), os.getpid()))
    db.commit()
    assert client.get("/api/activity").json()["synced"]["running"] is True
    assert client.get("/api/activity").json()["synced"]["status"] == "running"


def test_a_running_row_with_a_dead_process_is_not_running(client, db):
    db.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, NULL)",
               (store.iso(datetime.now(UTC)),))
    db.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, ?)",
               (store.iso(datetime.now(UTC) - timedelta(days=2)), os.getpid()))
    db.commit()
    assert client.get("/api/activity").json()["synced"]["running"] is False


# -- the day view -----------------------------------------------------------------------------------------


def test_day_view_recent_is_seven_days(client, db):
    shift(db)
    store.insert_actions(db, [
        act(PAT, 11, utc(14, 14, 0), "reply", event_id=900, child_id=1),
        act(PAT, 11, utc(14, 14, 0), "status", "open", "pending", event_id=900, child_id=2),
        act(PAT, 12, utc(14, 15, 5), "note"),
        act(PAT, 13, utc(13, 15, 0)),
    ])
    db.execute("INSERT INTO activity_tickets (ticket_id, subject, purged) VALUES (11, 'Printer jam', 0), (12, NULL, 1)")
    db.commit()

    body = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-14"}).json()

    assert [c["day"] for c in body["recent"]] == [f"2026-10-{d:02d}" for d in range(8, 15)]
    assert body["cell"] == body["recent"][-1] and body["cell"]["day"] == "2026-10-14"
    assert body["cell"]["tickets"] == 2
    assert body["actions"] == [
        {"at": "09:00", "kind": "reply", "old": None, "new": None, "ticket_id": 11, "subject": "Printer jam",
         "event_id": 900},
        {"at": "09:00", "kind": "status", "old": "open", "new": "pending", "ticket_id": 11,
         "subject": "Printer jam", "event_id": 900},
        {"at": "10:05", "kind": "note", "old": None, "new": None, "ticket_id": 12, "subject": None,
         "event_id": body["actions"][2]["event_id"]},
    ]
    assert next(c for c in body["recent"] if c["day"] == "2026-10-13")["replies"] == 1


def test_day_view_for_a_future_day_and_bad_input(client, db):
    shift(db)
    body = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-20"}).json()
    assert body["cell"]["state"] == "future" and body["actions"] == []
    assert client.get("/api/activity/day", params={"tech": PAT, "day": "soon"}).status_code == 400
    assert client.get("/api/activity/day", params={"tech": PAT}).status_code == 400


def test_open_tickets_as_of(client, db):
    shift(db)
    store.insert_actions(db, [act(PAT, 21, utc(14, 14, 0)), act(PAT, 21, utc(15, 20, 30), "note")])
    db.execute("INSERT INTO activity_tickets (ticket_id, subject, purged) VALUES (21, 'Login loop', 0)")
    db.executemany("INSERT INTO activity_open (email, captured_at, ticket_id, status, created) VALUES (?, ?, ?, ?, ?)", [
        (PAT, "2026-10-17T10:00:00Z", 21, "open", "2026-10-05T09:00:00Z"),
        (PAT, "2026-10-17T10:00:00Z", 22, "pending", None), (PAT, "2026-10-17T10:00:00Z", 24, "open",
                                                              "2026-10-10T10:00:00Z"),
        (SAM, "2026-10-17T10:00:00Z", 23, "open", None)])
    db.commit()

    body = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-16"}).json()

    # Aging is 1:1 Prep's rule: open longer than 7 days since created, at the sync that saw it (the owner, 2026-10-22).
    assert body["open"] == {"as_of": "2026-10-17T10:00:00Z", "tickets": [
        {"ticket_id": 21, "subject": "Login loop", "status": "open", "last_acted": "2026-10-15T15:30",
         "age_days": 12, "aging": True},
        {"ticket_id": 22, "subject": None, "status": "pending", "last_acted": None, "age_days": None, "aging": None},
        {"ticket_id": 24, "subject": None, "status": "open", "last_acted": None, "age_days": 7, "aging": False}]}


def test_open_tickets_with_no_snapshot(client, db):
    shift(db)
    body = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-16"}).json()
    assert body["open"] == {"as_of": None, "tickets": []}


# -- starting a sync --------------------------------------------------------------------------------------


def test_sync_post_starts_and_rejects_when_running(client, db, started):
    response = client.post("/api/activity/sync", json={"kind": "refresh"})
    assert response.status_code == 202 and response.json()["started"] is True
    assert started == [{"command": "activity-sync", "kind": "refresh"}]

    db.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, ?)",
               (store.iso(datetime.now(UTC)), os.getpid()))
    db.commit()
    again = client.post("/api/activity/sync", json={"kind": "refresh"})
    assert again.status_code == 409 and "already running" in again.json()["detail"]
    assert len(started) == 1


def test_a_refresh_that_died_unreaped_doesnt_block_the_next_sync(client, db, started, monkeypatch, tmp_path):
    """Final review F8: on the server (Linux) a Refresh sync is the web app's own child, which it never waits for. One
    killed before it could update its row stays a zombie, which os.kill(pid, 0) still finds, until it is collected: the
    app collects its finished sync before it checks, so the row counts as dead and the next sync starts, instead of
    every sync (the timer's too) being refused until the row is 6 hours old."""
    from test_procutil import FakeOs

    from work_assistant import procutil

    fake = FakeOs(running={4242, 5151}, children={4242: "finished", 5151: "running"})
    monkeypatch.setattr(procutil, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(procutil, "os", fake)
    monkeypatch.setattr(procutil, "_PROC", tmp_path / "no-proc")   # /proc can't tell: only the reaping helps
    db.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, 4242)",
               (store.iso(datetime.now(UTC)),))
    db.commit()
    assert client.get("/api/activity").json()["synced"]["running"] is False
    response = client.post("/api/activity/sync", json={"kind": "refresh"})
    assert response.status_code == 202 and started == [{"command": "activity-sync", "kind": "refresh"}]
    assert ("waitpid", 4242, 1) in fake.calls and 4242 not in fake.running, "collected, without waiting"

    db.execute("UPDATE activity_sync SET pid = 5151")   # a sync of its own still running stays running
    db.commit()
    again = client.post("/api/activity/sync", json={"kind": "refresh"})
    assert again.status_code == 409 and len(started) == 1


def test_sync_post_accepts_a_backfill_and_rejects_other_kinds(client, started):
    assert client.post("/api/activity/sync", json={"kind": "backfill"}).status_code == 202
    assert started == [{"command": "activity-sync", "kind": "backfill"}]
    for body in ({"kind": "daily"}, {"kind": 3}, {}, [], "refresh"):
        assert client.post("/api/activity/sync", json=body).status_code == 400
    assert len(started) == 1


def test_activity_sync_command_line(tmp_path):
    args = webapp.cli_args({"command": "activity-sync", "kind": "backfill"}, tmp_path / "x.db")
    assert args[0] == sys.executable
    assert args[1:] == ["-m", "work_assistant.cli", "--db", str(tmp_path / "x.db"), "activity", "sync", "--kind",
                        "backfill"]


def test_production_starter_needs_zendesk_but_not_the_anthropic_key(db_path, tmp_path, db, monkeypatch):
    launched = []
    monkeypatch.setattr(webapp.subprocess, "Popen",
                        lambda args, **kwargs: launched.append(args) or SimpleNamespace(pid=91))
    starter = webapp.subprocess_run_starter(db_path, tmp_path / "logs")
    spec = {"command": "activity-sync", "kind": "refresh"}
    with pytest.raises(webapp.Unavailable, match="Zendesk"):
        starter(spec)
    set_setting(db, "zendesk_subdomain", "acme")
    set_setting(db, "zendesk_email", "owner@example.com")
    credentials.set_secret("zendesk_token", "zd-test")
    assert credentials.get_secret("anthropic_api_key") is None
    assert starter(spec) == 91
    assert launched[0][-4:] == ["activity", "sync", "--kind", "refresh"]
    with pytest.raises(webapp.Unavailable, match="Anthropic"):
        starter({"command": "confirm", "run_id": 3})


# -- shifts -----------------------------------------------------------------------------------------------


def test_shifts_lists_versions(client, db):
    set_setting(db, "team", [PAT, SAM])
    shift(db, PAT, date(2026, 9, 1), {1, 2, 3}, 540, 1050)
    shift(db, PAT, date(2026, 10, 1), SUN_THU)
    body = client.get("/api/shifts").json()
    assert body["techs"] == [
        {"email": PAT, "name": PAT, "region": "NASA", "versions": [
            {"effective_from": "2026-09-01", "work_days": [1, 2, 3], "start": "09:00", "end": "17:30", "active": True},
            {"effective_from": "2026-10-01", "work_days": [0, 1, 2, 3, 4], "start": "08:00", "end": "17:00",
             "active": True}]},
        {"email": SAM, "name": SAM, "region": "NASA", "versions": []}]


def test_shift_put_adds_a_version(client, db):
    response = client.put(f"/api/shifts/{PAT}", json={
        "effective_from": "2026-10-19", "work_days": [1, 2, 3, 4, 5], "start": "07:30", "end": "16:00"})
    assert response.status_code == 200
    assert response.json()["versions"] == [
        {"effective_from": "2026-10-19", "work_days": [1, 2, 3, 4, 5], "start": "07:30", "end": "16:00",
         "active": True}]
    assert store.shift_on(db, PAT, date(2026, 10, 19)).start_min == 450
    # the same effective day replaces it
    client.put(f"/api/shifts/{PAT}", json={
        "effective_from": "2026-10-19", "work_days": [1], "start": "09:00", "end": "10:00"})
    assert len(store.shift_versions(db, PAT)) == 1


GOOD_SHIFT = {"effective_from": "2026-10-19", "work_days": [1, 2], "start": "08:00", "end": "17:00"}


@pytest.mark.parametrize("change", [
    {"end": "08:00"}, {"end": "07:59"}, {"work_days": []}, {"work_days": [7]}, {"work_days": [-1]},
    {"work_days": "1,2"}, {"work_days": [True]}, {"start": "8am"}, {"start": "24:00"},
    {"end": "17:60"}, {"start": None}, {"effective_from": "next week"}, {"effective_from": "2026-10-1"},
    {"effective_from": None},
])
def test_shift_put_validation(client, db, change):
    response = client.put(f"/api/shifts/{PAT}", json={**GOOD_SHIFT, **change})
    assert response.status_code == 400, response.text
    assert store.shift_versions(db, PAT) == []


def test_shift_put_rejects_a_missing_key_a_non_object_and_someone_off_the_team(client, db):
    assert client.put(f"/api/shifts/{PAT}", json={"start": "08:00"}).status_code == 400
    assert client.put(f"/api/shifts/{PAT}", json=[GOOD_SHIFT]).status_code == 400
    response = client.put("/api/shifts/stranger@example.com", json=GOOD_SHIFT)
    assert response.status_code == 400 and "team" in response.json()["detail"]
    assert store.shift_versions(db, "stranger@example.com") == []


# -- a tech's start ---------------------------------------------------------------------------------------

TUE_SAT = frozenset({2, 3, 4, 5, 6})


def versions_of(db, email=PAT):
    return [(v.effective_from.isoformat(), sorted(v.work_days), v.start_min, v.end_min, v.active)
            for v in store.shift_versions(db, email)]


def test_a_later_start_stops_tracking_the_days_before(client, db):
    shift(db, PAT, date(2025, 10, 22), SUN_THU)
    shift(db, PAT, date(2026, 10, 1), SUN_THU)
    response = client.put(f"/api/shifts/{PAT}/start", json={"started": "2026-03-02"})
    assert response.status_code == 200, response.text
    assert versions_of(db) == [("2026-03-02", [0, 1, 2, 3, 4], 480, 1020, True)], \
        "the earlier entry is gone and the repeat of it merged"
    assert store.shift_on(db, PAT, date(2026, 3, 1)) is None, "not tracked before the start"
    assert response.json()["versions"][0]["effective_from"] == "2026-03-02"


def test_a_start_takes_the_shift_in_force_on_that_day_and_keeps_later_changes(client, db):
    shift(db, PAT, date(2025, 10, 22), SUN_THU)
    shift(db, PAT, date(2026, 4, 5), TUE_SAT)
    shift(db, PAT, date(2026, 8, 2), {1, 2, 3, 4, 5})
    assert client.put(f"/api/shifts/{PAT}/start", json={"started": "2026-05-01"}).status_code == 200
    assert versions_of(db) == [("2026-05-01", [2, 3, 4, 5, 6], 480, 1020, True),
                               ("2026-08-02", [1, 2, 3, 4, 5], 480, 1020, True)]


def test_an_earlier_start_moves_the_first_shift_back(client, db):
    shift(db, PAT, date(2026, 2, 16), {1, 2, 3, 4, 5})
    shift(db, PAT, date(2026, 4, 5), TUE_SAT)
    assert client.put(f"/api/shifts/{PAT}/start", json={"started": "2024-04-15"}).status_code == 200
    assert versions_of(db) == [("2024-04-15", [1, 2, 3, 4, 5], 480, 1020, True),
                               ("2026-04-05", [2, 3, 4, 5, 6], 480, 1020, True)]


@pytest.mark.parametrize("body, status", [
    ({"started": "2026-10-18"}, 400),   # after the pinned today
    ({"started": "soon"}, 400), ({"started": None}, 400), ({}, 400), (["2026-03-02"], 400),
])
def test_a_start_must_be_a_day_up_to_today(client, db, body, status):
    shift(db, PAT, date(2026, 9, 1), SUN_THU)
    response = client.put(f"/api/shifts/{PAT}/start", json=body)
    assert response.status_code == status, response.text
    assert versions_of(db) == [("2026-09-01", [0, 1, 2, 3, 4], 480, 1020, True)]


def test_a_start_needs_a_tech_on_the_team_with_a_shift(client, db):
    response = client.put("/api/shifts/stranger@example.com/start", json={"started": "2026-03-02"})
    assert response.status_code == 400 and "team" in response.json()["detail"]
    response = client.put(f"/api/shifts/{PAT}/start", json={"started": "2026-03-02"})
    assert response.status_code == 400 and "shift" in response.json()["detail"]
    assert versions_of(db) == []


def test_saving_a_shift_merges_repeats_of_the_one_before(client, db):
    shift(db, PAT, date(2025, 10, 22), SUN_THU)
    shift(db, PAT, date(2026, 10, 1), SUN_THU)
    response = client.put(f"/api/shifts/{PAT}", json={
        "effective_from": "2025-10-22", "work_days": [2, 3, 4, 5, 6], "start": "08:00", "end": "17:00"})
    assert response.status_code == 200
    assert versions_of(db) == [("2025-10-22", [2, 3, 4, 5, 6], 480, 1020, True),
                               ("2026-10-01", [0, 1, 2, 3, 4], 480, 1020, True)], "different now, so both stay"
    client.put(f"/api/shifts/{PAT}", json={
        "effective_from": "2026-10-01", "work_days": [2, 3, 4, 5, 6], "start": "08:00", "end": "17:00"})
    assert versions_of(db) == [("2025-10-22", [2, 3, 4, 5, 6], 480, 1020, True)]


def test_merging_keeps_a_removal(db):
    shift(db, PAT, date(2026, 9, 1), SUN_THU)
    shift(db, PAT, date(2026, 10, 1), SUN_THU, active=False)
    shift(db, PAT, date(2026, 10, 5), SUN_THU, active=False)
    store.merge_repeats(db, PAT)
    assert versions_of(db) == [("2026-09-01", [0, 1, 2, 3, 4], 480, 1020, True),
                               ("2026-10-01", [0, 1, 2, 3, 4], 480, 1020, False)]


# -- time off ---------------------------------------------------------------------------------------------


def test_time_off_roundtrip(client, db):
    assert client.get("/api/time-off").json() == {"items": []}
    created = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-14",
                                                 "note": "Vacation"})
    assert created.status_code == 201
    item = {"id": created.json()["id"], "email": PAT, "first": "2026-10-12", "last": "2026-10-14",
            "note": "Vacation", "kind": "pto"}
    assert created.json() == item
    assert client.get("/api/time-off").json() == {"items": [item]}
    shift(db)
    synced(db)
    assert cells(client.get("/api/activity", params={"start": "2026-10-12", "end": "2026-10-14"}).json()
                 )["2026-10-13"]["state"] == "pto"
    gone = client.delete(f"/api/time-off/{item['id']}")
    assert gone.status_code == 204 and gone.content == b""
    assert client.get("/api/time-off").json() == {"items": []}
    assert client.delete(f"/api/time-off/{item['id']}").status_code == 404


def test_time_off_note_is_optional_and_input_is_checked(client):
    ok = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-12"})
    assert ok.status_code == 201 and ok.json()["note"] == ""
    for change in ({"last": "2026-10-11"}, {"first": "soon"}, {"email": "stranger@example.com"}, {"email": None},
                   {"note": 5}):
        body = {"email": PAT, "first": "2026-10-12", "last": "2026-10-14", "note": "", **change}
        assert client.post("/api/time-off", json=body).status_code == 400, change


def test_a_removed_tech_s_past_missed_shift_can_be_marked_as_time_off(client, db):
    """Final review F3 (spec 6.6 and 7.1): "Mark as time off" works for anyone the section lists, including a
    tech who has left the team but has a shift on record."""
    shift(db, SAM)                                              # SAM had a shift, and is no longer on the team
    synced(db)
    assert SAM not in store.settings.team_emails(db)
    created = client.post("/api/time-off", json={"email": " SAM@example.com ", "first": "2026-10-07",
                                                 "last": "2026-10-07"})
    assert created.status_code == 201 and created.json()["email"] == SAM
    body = client.get("/api/activity", params={"tech": SAM, **LAST_WEEK}).json()
    assert cells(body, SAM)["2026-10-07"]["state"] == "pto"
    refused = client.post("/api/time-off", json={"email": "stranger@example.com", "first": "2026-10-07",
                                                 "last": "2026-10-07"})
    assert refused.status_code == 400 and "not a tech" in refused.json()["detail"]


def test_day_view_reports_covering_time_off(client, db):
    shift(db)
    synced(db)
    created = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-07", "last": "2026-10-08",
                                                 "note": "Vacation"}).json()
    covered = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-08"}).json()
    assert covered["time_off"] == {"id": created["id"], "first": "2026-10-07", "last": "2026-10-08",
                                   "note": "Vacation", "kind": "pto"}
    assert client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-06"}).json()["time_off"] is None


def test_delete_time_off_day_endpoint(client, db):
    shift(db)
    synced(db)
    client.post("/api/time-off", json={"email": PAT, "first": "2026-10-07", "last": "2026-10-07"})
    params = {"tech": PAT, "day": "2026-10-07"}
    assert client.get("/api/activity/day", params=params).json()["cell"]["state"] == "pto"
    gone = client.delete("/api/time-off/day", params={"email": " PAT@example.com ", "day": "2026-10-07"})
    assert gone.status_code == 204 and gone.content == b""
    after = client.get("/api/activity/day", params=params).json()
    assert after["cell"]["state"] == "missed" and after["time_off"] is None


def test_delete_time_off_day_404_when_none(client, db):
    shift(db)
    client.post("/api/time-off", json={"email": PAT, "first": "2026-10-07", "last": "2026-10-07"})
    assert client.delete("/api/time-off/day", params={"email": PAT, "day": "2026-10-08"}).status_code == 404
    assert client.delete("/api/time-off/day", params={"email": PAT, "day": "soon"}).status_code == 400
    assert client.delete("/api/time-off/day", params={"day": "2026-10-07"}).status_code == 400
    assert len(client.get("/api/time-off").json()["items"]) == 1


def test_marking_a_worked_day_off_clears_its_flags_and_counts(client, db):
    shift(db)
    synced(db)
    store.insert_actions(db, [act(PAT, 11, utc(5, 13, 30)), act(PAT, 11, utc(5, 21, 30))])   # Mon 08:30 and 16:30
    week = {"start": "2026-10-04", "end": "2026-10-10"}
    before = client.get("/api/activity", params=week).json()
    assert cells(before)["2026-10-05"]["state"] == "worked"
    assert [f["kind"] for f in cells(before)["2026-10-05"]["flags"]] == ["long_gap"]
    assert client.post("/api/time-off", json={"email": PAT, "first": "2026-10-05", "last": "2026-10-05",
                                              "note": ""}).status_code == 201
    after = client.get("/api/activity", params=week).json()
    monday = cells(after)["2026-10-05"]
    assert monday["state"] == "pto" and monday["flags"] == []
    scheduled = lambda body: sum(c["state"] in ("worked", "zero", "missed") for c in body["rows"][0]["cells"])
    assert scheduled(after) == scheduled(before) - 1


# -- team changes -----------------------------------------------------------------------------------------


def put_team(client, team):
    return client.put("/api/settings", json={"team": team})


def test_remove_tech_keeps_history_and_untracks_future(client, db, monkeypatch):
    set_setting(db, "team", [PAT, SAM])
    shift(db, PAT)
    shift(db, SAM)
    store.insert_actions(db, [act(PAT, 1, utc(5, 14, 0))])
    synced(db, through="2026-10-26T11:00:00Z")

    assert put_team(client, [SAM]).status_code == 200

    removed = store.shift_versions(db, PAT)[-1]
    assert (removed.effective_from, removed.active) == (date(2026, 10, 18), False)
    assert (removed.work_days, removed.start_min, removed.end_min) == (SUN_THU, 480, 1020)
    assert [v.active for v in store.shift_versions(db, SAM)] == [True], "the tech who stays is untouched"

    body = client.get("/api/activity", params={"period": "last_week"}).json()
    assert cells(body)["2026-10-05"]["state"] == "worked", "history is kept"
    assert [t["email"] for t in body["techs"]] == [PAT, SAM], "a removed tech stays listed"

    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 25))
    month = client.get("/api/activity", params={"start": "2026-10-15", "end": "2026-10-19"}).json()
    assert [cells(month)[d]["state"] for d in ("2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18",
                                               "2026-10-19")] == ["missed", "rest", "rest", "untracked", "untracked"]
    later = client.get("/api/activity", params={"start": "2026-10-19", "end": "2026-10-24"}).json()
    assert [r["email"] for r in later["rows"]] == [SAM], "a tech who left has no row for days after they left"
    assert [t["email"] for t in later["techs"]] == [SAM], "nor a place in the picker for a period after they left"
    covering = client.get("/api/activity", params={"start": "2026-10-17", "end": "2026-10-19"}).json()
    assert [t["email"] for t in covering["techs"]] == [PAT, SAM], "a period touching their tenure lists them"
    asked = client.get("/api/activity", params={"start": "2026-10-19", "end": "2026-10-24", "tech": PAT}).json()
    assert {c["state"] for c in cells(asked, PAT).values()} == {"untracked"}


def test_a_tech_removed_earlier_has_no_future_days_this_month(client, db):
    set_setting(db, "team", [SAM])
    shift(db, PAT, date(2026, 6, 1))
    shift(db, PAT, date(2026, 8, 15), active=False)
    shift(db, SAM)
    body = client.get("/api/activity", params={"period": "this_month"}).json()
    assert [r["email"] for r in body["rows"]] == [SAM], "no row, not even dashed days still to come"
    assert [t["email"] for t in body["techs"]] == [SAM]
    asked = client.get("/api/activity", params={"period": "this_month", "tech": PAT}).json()
    assert {c["state"] for c in cells(asked, PAT).values()} == {"untracked"}, "days after today too"
    august = client.get("/api/activity", params={"start": "2026-08-10", "end": "2026-08-20"}).json()
    assert [t["email"] for t in august["techs"]] == [PAT, SAM], "listed for a period covering their tenure"


def test_future_days_with_no_shift_are_untracked(client, db):
    body = client.get("/api/activity", params={"period": "this_month", "tech": PAT}).json()
    by_day = cells(body)
    assert all(by_day[f"2026-10-{d:02d}"]["state"] == "untracked" for d in range(18, 32))
    assert [t["email"] for t in body["techs"]] == [PAT], "someone on the team is listed even with no shift"
    assert body["techs"][0]["has_shift"] is False, "and says they have no shift version at all"


def test_add_tech_gets_placeholder_shift_from_retention_start(client, db):
    set_setting(db, "retention_days", 90)
    assert put_team(client, [PAT, NEW]).status_code == 200
    [version] = store.shift_versions(db, NEW)
    assert version == ShiftVersion(NEW, TODAY - timedelta(days=90), SUN_THU, 480, 1020, True)
    assert store.shift_versions(db, PAT) == [], "someone already on the team is not given a shift"


def test_add_tech_after_a_sync_has_completed_starts_today(client, db):
    db.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through)"
               " VALUES ('daily', 'complete', '2026-10-17T10:00:00Z', '2026-10-17T10:01:00Z', '2026-10-17T10:00:00Z')")
    db.commit()
    assert put_team(client, [PAT, NEW]).status_code == 200
    [version] = store.shift_versions(db, NEW)
    assert (version.effective_from, version.active) == (TODAY, True)
    by_day = cells(client.get("/api/activity", params={"start": "2026-10-14", "end": "2026-10-17"}).json(), NEW)
    assert [by_day[d]["state"] for d in ("2026-10-15", "2026-10-16", "2026-10-17")] == ["untracked", "untracked", "rest"]


def test_adding_a_tech_who_already_has_a_shift_keeps_it(client, db):
    shift(db, NEW, date(2026, 9, 1), {2}, 600, 900)
    assert put_team(client, [PAT, NEW]).status_code == 200
    assert [(v.effective_from, v.work_days) for v in store.shift_versions(db, NEW)] == [
        (date(2026, 9, 1), frozenset({2}))]


def test_readding_a_removed_tech_resumes_their_last_hours_from_today(client, db):
    shift(db, PAT, date(2026, 9, 1), {1, 2, 3}, 540, 1050)
    shift(db, PAT, date(2026, 10, 1), {2, 3}, 600, 960)
    assert put_team(client, []).status_code == 200
    # Their removal took effect before they came back.
    db.execute("UPDATE shift_versions SET effective_from = '2026-10-10' WHERE active = 0")
    db.commit()
    assert put_team(client, [PAT]).status_code == 200
    versions = store.shift_versions(db, PAT)
    assert [(v.effective_from, v.active) for v in versions] == [
        (date(2026, 9, 1), True), (date(2026, 10, 1), True), (date(2026, 10, 10), False),
        (TODAY, True)]
    assert (versions[-1].work_days, versions[-1].start_min, versions[-1].end_min) == (frozenset({2, 3}), 600, 960)


def test_readding_before_the_removal_takes_effect_cancels_it(client, db):
    shift(db, PAT)
    put_team(client, [])
    put_team(client, [PAT])
    versions = store.shift_versions(db, PAT)
    assert [(v.effective_from, v.active) for v in versions] == [
        (date(2026, 9, 1), True), (date(2026, 10, 18), True)]
    assert store.shift_on(db, PAT, date(2026, 10, 20)).active is True


def test_removing_a_tech_deactivates_a_shift_change_set_ahead(client, db, monkeypatch):
    """Final review F4 (spec 7.5): a version dated after the removal must not bring the tech back on its day."""
    shift(db, PAT)
    shift(db, PAT, date(2026, 11, 1), {2, 3, 4, 5, 6}, 540, 1080)       # a change the owner set ahead
    assert put_team(client, []).status_code == 200
    assert [(v.effective_from, v.active) for v in store.shift_versions(db, PAT)] == [
        (date(2026, 9, 1), True), (date(2026, 10, 18), False), (date(2026, 11, 1), False)]
    later = store.shift_versions(db, PAT)[-1]
    assert (later.work_days, later.start_min, later.end_min) == (frozenset({2, 3, 4, 5, 6}), 540, 1080), \
        "kept, with its hours"
    removal = store.shift_versions(db, PAT)[1]
    assert (removal.work_days, removal.start_min) == (SUN_THU, 480), "the removal keeps the hours then in force"
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 11, 20))
    november = client.get("/api/activity", params={"start": "2026-11-01", "end": "2026-11-07", "tech": PAT}).json()
    assert {c["state"] for c in cells(november).values()} == {"untracked"}, "not back on Nov 1"


def test_readding_a_tech_starts_today_not_at_their_last_version(client, db, monkeypatch):
    """Final review F4: tracking resumes from today, and the change set ahead applies again on its day."""
    shift(db, PAT)
    shift(db, PAT, date(2026, 11, 1), {2, 3, 4, 5, 6}, 540, 1080)
    put_team(client, [])
    monkeypatch.setattr(webapp, "_today", lambda tz: date(2026, 10, 20))   # removal in effect since Oct 18
    assert put_team(client, [PAT]).status_code == 200
    versions = store.shift_versions(db, PAT)
    assert [(v.effective_from, v.active) for v in versions] == [
        (date(2026, 9, 1), True), (date(2026, 10, 18), False), (date(2026, 10, 20), True), (date(2026, 11, 1), True)]
    assert (versions[2].work_days, versions[2].start_min, versions[2].end_min) == (SUN_THU, 480, 1020)
    assert store.shift_on(db, PAT, date(2026, 10, 25)).active is True
    assert store.shift_on(db, PAT, date(2026, 11, 5)).start_min == 540


def test_a_failed_settings_change_leaves_shifts_alone(client, db):
    set_setting(db, "team", [PAT, SAM])
    shift(db, SAM)
    response = client.put("/api/settings", json={"team": [PAT], "retention_days": 0})
    assert response.status_code == 400
    assert [v.active for v in store.shift_versions(db, SAM)] == [True]


# -- the anchor, and Trends ---------------------------------------------------------------------------------


def add_counted_review(db, call="2026-09-22T15:00:00Z"):
    """One counted Call QA review of a call on `call`, so Call QA's latest week with data is that week."""
    version = ensure_seeded(db)
    run_id = db.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at)"
                        " VALUES ('manual', '2026-09-20T05:00:00Z', '2026-09-27T05:00:00Z', ?, 'complete',"
                        " '2026-09-28T11:00:00Z')", (version.version_id,)).lastrowid
    db.execute("INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, attribution_ok,"
               " snapshot, fetched_at) VALUES (900, 'voice', ?, ?, 'Pat', 1, '{}', '2026-09-28T00:00:00Z')",
               (call, PAT))
    db.execute("INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at, counted)"
               " VALUES (?, 900, ?, 'ok', 1, '2026-09-28T00:00:00Z', 1)", (run_id, version.version_id))
    db.commit()


def test_activity_last_8_weeks_ends_last_saturday_whatever_call_qa_reviewed(client, db):
    """Final review F6: Team activity is current to today, so Last 8 weeks is the 8 whole weeks ending last
    Saturday (Oct 10), not the 8 ending with the latest week Call QA reviewed."""
    shift(db)
    add_counted_review(db)                       # Call QA's latest week with data is Sep 20-26
    body = client.get("/api/activity", params={"period": "last_8_weeks"}).json()
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-08-16", "2026-10-10")
    assert body["period"]["so_far"] is False
    trends = client.get("/api/activity/trends", params={"period": "last_8_weeks"}).json()
    assert (trends["period"]["start"], trends["period"]["end"]) == ("2026-08-16", "2026-10-10")


@pytest.fixture(scope="module")
def demo_client(tmp_path_factory):
    from work_assistant import demo

    root = tmp_path_factory.mktemp("trends-demo")
    now = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)          # noon on Monday Oct 19 in Chicago
    demo.build_demo_db(root / "demo.db", log_dir=root / "logs", now=now)
    app = create_app(root / "demo.db", run_starter=lambda spec: 4242, zendesk_factory=lambda: None,
                     log_dir=root / "logs", startup_work=False, demo=True, now=now)
    return TestClient(app, base_url=BASE)


def trends_measure(body, key):
    return next(m for m in body["measures"] if m["key"] == key)


def test_trends_endpoint_shape(demo_client):
    body = demo_client.get("/api/activity/trends", params={"compare": "previous"}).json()
    assert set(body) == {"period", "comparison", "tech", "today", "synced", "coverage_from", "unit", "steps",
                         "techs", "measures", "team", "picker"}
    assert body["picker"] == body["techs"], "All techs: the Tech picker's lines are the techs listed (Ruling 9)"
    assert body["team"] is None, "All techs is no one team (T-144)"
    assert body["unit"] == "week" and body["today"] == "2026-10-19"
    assert [m["key"] for m in body["measures"]] == [
        "tickets_per_shift", "replies_per_shift", "solved_per_shift", "shifts_worked", "missed_shifts",
        "signed_in_no_activity", "late_start_min", "early_finish_min", "longest_gap_min", "flags_per_shift",
        "calls_per_shift", "calls_share", "declined_missed_per_shift", "declined_missed_pct", "call_time_per_shift",
        "avg_call_time", "satisfaction", "aging_tickets"]
    assert len(body["steps"]) == 16 and body["steps"][0]["start"] == "2026-06-28"
    assert [s["in_period"] for s in body["steps"]] == [False] * 8 + [True] * 8
    assert len(body["techs"]) == 6
    for measure in body["measures"]:
        assert [t["email"] for t in measure["techs"]] == [t["email"] for t in body["techs"]]
        assert len(measure["team"]["steps"]) == 16


def test_trends_defaults_to_last_8_weeks_compared_with_nothing(demo_client):
    """T-143: Compare to defaults to Nothing; the period before is asked for as compare=previous."""
    body = demo_client.get("/api/activity/trends").json()
    assert (body["period"]["preset"], body["period"]["start"], body["period"]["end"]) == (
        "last_8_weeks", "2026-08-23", "2026-10-17")
    assert body["comparison"] is None
    assert trends_measure(body, "tickets_per_shift")["team"]["change"] is None


def test_trends_compare_previous_is_the_8_weeks_before(demo_client):
    body = demo_client.get("/api/activity/trends", params={"compare": "previous"}).json()
    assert (body["comparison"]["start"], body["comparison"]["end"]) == ("2026-06-28", "2026-08-22")
    assert body["coverage_from"] is None, "the demo's sync reads back through the comparison"
    assert trends_measure(body, "tickets_per_shift")["team"]["change"] is not None


def test_trends_last_6_months_by_month_against_the_6_before(demo_client):
    body = demo_client.get("/api/activity/trends", params={"period": "last_6_months", "compare": "previous"}).json()
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-04-01", "2026-09-30")
    assert (body["comparison"]["start"], body["comparison"]["end"]) == ("2025-10-01", "2026-03-31")
    assert body["unit"] == "month"
    assert [s["tick"] for s in body["steps"] if s["in_period"]] == ["Apr", "May", "Jun", "Jul", "Aug", "Sep"]


def test_trends_last_year_compares_with_the_year_before_when_asked(demo_client):
    """T-143 (undoing T-132's rule): the counts are kept beyond retention, so the year before can be compared once it
    is stored; coverage_from says where the stored history starts."""
    body = demo_client.get("/api/activity/trends", params={"period": "last_year", "compare": "previous"}).json()
    assert (body["period"]["start"], body["period"]["end"]) == ("2025-10-01", "2026-09-30")
    assert (body["comparison"]["start"], body["comparison"]["end"]) == ("2024-10-01", "2025-09-30")
    assert body["unit"] == "month" and body["coverage_from"] is not None
    assert demo_client.get("/api/activity/trends", params={"period": "last_year"}).json()["comparison"] is None


def test_trends_compare_none_has_no_comparison(demo_client):
    body = demo_client.get("/api/activity/trends", params={"compare": "none"}).json()
    assert body["comparison"] is None
    assert len(body["steps"]) == 8
    for measure in body["measures"]:
        assert measure["team"]["before"] is None and measure["team"]["change"] is None
        assert all(t["before"] is None and t["change"] is None for t in measure["techs"])
    assert trends_measure(body, "tickets_per_shift")["team"]["now"] is not None


def test_trends_custom_and_bad_input(demo_client):
    body = demo_client.get("/api/activity/trends", params={
        "start": "2026-09-06", "end": "2026-09-19", "compare": "custom",
        "compare_start": "2026-08-23", "compare_end": "2026-09-05"}).json()
    assert (body["period"]["start"], body["period"]["end"], body["period"]["preset"]) == (
        "2026-09-06", "2026-09-19", "custom")
    assert (body["comparison"]["start"], body["comparison"]["end"]) == ("2026-08-23", "2026-09-05")
    bad = demo_client.get("/api/activity/trends", params={"start": "2026-09-19", "end": "2026-09-06"})
    overview = demo_client.get("/api/overview", params={"start": "2026-09-19", "end": "2026-09-06"})
    assert bad.status_code == 400 and bad.json() == overview.json()
    assert demo_client.get("/api/activity/trends", params={"period": "someday"}).status_code == 400
    assert demo_client.get("/api/activity/trends", params={"compare": "someday"}).status_code == 400
    assert demo_client.get("/api/activity/trends", params={"start": "soon", "end": "2026-09-06"}
                           ).status_code == 400


def test_trends_takes_a_tech_and_echoes_it(demo_client):
    """Spec §6: the endpoint takes the tech parameter, checked as `/api/activity` checks it. The payload stays
    whole (the graph draws every tech) and says which tech was asked for."""
    whole = demo_client.get("/api/activity/trends").json()
    assert whole["tech"] is None
    jordan = "jordan.pike@example.com"
    one = demo_client.get("/api/activity/trends", params={"tech": " Jordan.Pike@example.com "}).json()
    assert one["tech"] == jordan
    assert one["techs"] == whole["techs"] and one["measures"] == whole["measures"]
    bad = demo_client.get("/api/activity/trends", params={"tech": "nobody@example.com"})
    grid = demo_client.get("/api/activity", params={"tech": "nobody@example.com"})
    assert bad.status_code == 400 and grid.status_code == 400 and bad.json() == grid.json()


def test_trends_period_still_running_counts_only_days_so_far(client, db):
    shift(db)
    synced(db)
    # This month runs to Oct 31, past the pinned Saturday (Oct 17): the period and the graph stop at today.
    body = client.get("/api/activity/trends", params={"period": "this_month", "compare": "none"}).json()
    assert body["period"]["so_far"] is True
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-10-01", "2026-10-17")
    assert body["steps"][0]["start"] == "2026-10-01"
    assert body["steps"][-1]["end"] == "2026-10-17", "future days are not part of the graph"


# -- the kind of time off (PTO or a marked sick day) ----------------------------------------------------------


def test_time_off_kind_round_trips_and_defaults_to_pto(client, db):
    pto = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-12"}).json()
    sick = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-13", "last": "2026-10-13",
                                              "kind": "sick"})
    assert pto["kind"] == "pto" and sick.status_code == 201 and sick.json()["kind"] == "sick"
    assert [i["kind"] for i in client.get("/api/time-off").json()["items"]] == ["pto", "sick"]


def test_time_off_with_a_bad_kind_is_refused(client):
    for kind in ("holiday", None, 3, ["sick"]):
        refused = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-12",
                                                     "kind": kind})
        assert refused.status_code == 400 and refused.json()["detail"] == "The kind must be pto or sick.", kind
    assert client.get("/api/time-off").json() == {"items": []}


def test_patch_time_off_changes_the_kind(client):
    made = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-14",
                                              "note": "flu"}).json()
    changed = client.patch(f"/api/time-off/{made['id']}", json={"kind": "sick"})
    assert changed.status_code == 200
    assert changed.json() == {**made, "kind": "sick"}
    assert client.get("/api/time-off").json() == {"items": [{**made, "kind": "sick"}]}
    assert client.patch(f"/api/time-off/{made['id']}", json={"kind": "pto"}).json()["kind"] == "pto"


def test_patch_time_off_edits_its_dates_and_note(client):
    made = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-14",
                                              "note": "flu"}).json()
    changed = client.patch(f"/api/time-off/{made['id']}", json={"first": "2026-10-13", "last": "2026-10-16",
                                                                 "note": " Vacation ", "kind": "pto"})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {**made, "first": "2026-10-13", "last": "2026-10-16", "note": "Vacation", "kind": "pto"}
    only_note = client.patch(f"/api/time-off/{made['id']}", json={"note": ""}).json()
    assert (only_note["first"], only_note["last"], only_note["note"]) == ("2026-10-13", "2026-10-16", "")


@pytest.mark.parametrize("body", [{"first": "2026-10-20"}, {"last": "2026-10-11"}, {"first": "soon"},
                                  {"note": 4}, {"note": "x" * 10001}, {}])
def test_patch_time_off_refuses_a_bad_edit(client, body):
    made = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-14"}).json()
    response = client.patch(f"/api/time-off/{made['id']}", json=body)
    assert response.status_code == 400, response.text
    assert client.get("/api/time-off").json()["items"] == [made]


def test_patch_time_off_404_and_400(client):
    made = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-12", "last": "2026-10-12"}).json()
    assert client.patch(f"/api/time-off/{made['id'] + 1}", json={"kind": "sick"}).status_code == 404
    for body in ({"kind": "holiday"}, {}, {"kind": None}, ["sick"]):
        assert client.patch(f"/api/time-off/{made['id']}", json=body).status_code == 400, body
    assert client.get("/api/time-off").json()["items"][0]["kind"] == "pto"


def test_a_marked_sick_day_is_a_sick_day_with_its_cell_saying_so(client, db):
    shift(db)
    synced(db)
    store.insert_actions(db, [act(PAT, 11, utc(5, 14, 0)), act(PAT, 12, utc(5, 14, 5))])    # Mon 09:00 and 09:05
    store.insert_signins(db, [SignIn(PAT, utc(5, 13, 10))])
    week = {"start": "2026-10-04", "end": "2026-10-10"}
    before = cells(client.get("/api/activity", params=week).json())["2026-10-05"]
    assert before["state"] == "worked" and before["time_off"] is None
    made = client.post("/api/time-off", json={"email": PAT, "first": "2026-10-05", "last": "2026-10-05",
                                              "kind": "sick"}).json()
    body = cells(client.get("/api/activity", params=week).json())
    assert body["2026-10-05"]["state"] == "missed" and body["2026-10-05"]["time_off"] == "sick"
    assert body["2026-10-05"]["flags"] == [{"kind": "missed", "minutes": None}]
    assert body["2026-10-04"]["time_off"] is None
    day = client.get("/api/activity/day", params={"tech": PAT, "day": "2026-10-05"}).json()
    assert day["cell"]["time_off"] == "sick" and day["time_off"]["kind"] == "sick"
    assert day["time_off"]["id"] == made["id"]
    assert len(day["actions"]) == 2, "work recorded on a marked sick day still lists in the day view"


def test_an_automatic_sick_day_has_no_time_off_and_a_pto_day_says_pto(client, db):
    shift(db)
    synced(db)
    client.post("/api/time-off", json={"email": PAT, "first": "2026-10-06", "last": "2026-10-06"})
    body = cells(client.get("/api/activity", params={"start": "2026-10-04", "end": "2026-10-10"}).json())
    assert body["2026-10-05"]["state"] == "missed" and body["2026-10-05"]["time_off"] is None
    assert body["2026-10-06"]["state"] == "pto" and body["2026-10-06"]["time_off"] == "pto"


def test_undoing_one_day_of_a_three_day_sick_entry_through_the_api_leaves_two_sick_days(client, db):
    shift(db)
    synced(db)
    client.post("/api/time-off", json={"email": PAT, "first": "2026-10-04", "last": "2026-10-06", "kind": "sick"})
    gone = client.delete("/api/time-off/day", params={"email": PAT, "day": "2026-10-05"})
    assert gone.status_code == 204
    body = cells(client.get("/api/activity", params={"start": "2026-10-04", "end": "2026-10-10"}).json())
    assert [(body[d]["state"], body[d]["time_off"]) for d in ("2026-10-04", "2026-10-05", "2026-10-06")] == [
        ("missed", "sick"), ("missed", None), ("missed", "sick")]
    assert [i["kind"] for i in client.get("/api/time-off").json()["items"]] == ["sick", "sick"]


def test_a_sick_mark_on_a_rest_day_counts_nothing_through_the_api(client, db):
    shift(db)
    synced(db)
    client.post("/api/time-off", json={"email": PAT, "first": "2026-10-09", "last": "2026-10-09", "kind": "sick"})
    week = {"start": "2026-10-04", "end": "2026-10-10"}
    friday = cells(client.get("/api/activity", params=week).json())["2026-10-09"]
    assert friday["state"] == "rest" and friday["flags"] == [] and friday["shift"] is None
