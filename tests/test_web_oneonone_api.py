"""The 1:1 prep endpoints: each tech's latest 1:1 period, one tech's detail and trends, and the schedule rows, each
counted from the collected data when asked. Nothing is made: Make prep again and its route are gone.

A meets every other Tuesday from Oct 20; on conftest's today (Sat Oct 17) their latest 1:1 is Oct 6 (Sep 22 to Oct
5). Every kind is collected from Aug 1, so their periods go back to Aug 25's (Aug 11's starts before it)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from test_oneonone_live import call, cover, leg, sync_activity

from work_assistant.activity import collected
from work_assistant.db import connect, init_db
from work_assistant.rubric import ensure_seeded
from work_assistant.settings import set_setting
from work_assistant.web.app import create_app

BASE = "http://127.0.0.1:8765"
A, B = "a@example.com", "b@example.com"
A_ID, OTHER_ID = 11, 99
UTC = timezone.utc
SYNCED = datetime(2026, 10, 17, 12, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    """A's period of Oct 6: two answered calls (5 and 10 minutes) and a missed one on Sep 30, and two the rest of the
    department answered; Team activity synced to noon Oct 17."""
    path = tmp_path / "work-assistant.db"
    conn = connect(path)
    init_db(conn)
    ensure_seeded(conn)
    set_setting(conn, "timezone", "America/Chicago")
    set_setting(conn, "team", [A])
    set_setting(conn, "oneonone_schedule", {A: {"date": "2026-10-20", "weeks": 2}})
    sync_activity(conn, through=SYNCED)
    collected.set_agents(conn, {A: A_ID})
    on = datetime(2026, 9, 30, 15, tzinfo=UTC)
    collected.upsert_legs(conn, [leg(1, 101, A_ID, talk=300, at=on), leg(2, 102, A_ID, talk=600, at=on),
                                 leg(3, 103, A_ID, status="agent_missed", talk=0, at=on),
                                 leg(4, 104, OTHER_ID, at=on), leg(5, 105, OTHER_ID, at=on)])
    collected.upsert_calls(conn, [call(101 + n, 5001 + n) for n in range(5)])
    cover(conn, through=SYNCED)
    conn.close()
    return path


@pytest.fixture
def started():
    return []


@pytest.fixture
def client(db_path, tmp_path, started):
    app = create_app(db_path, run_starter=lambda spec: started.append(spec) or 4242, log_dir=tmp_path / "logs",
                     startup_work=False)
    return TestClient(app, base_url=BASE)


def test_prep_endpoint_counts_the_latest_period(client):
    body = client.get("/api/prep").json()
    assert set(body) == {"synced", "rows"}, "no job and no next prep: nothing is made"
    assert body["synced"] == "2026-10-17T12:00:00Z"
    [row] = body["rows"]
    assert (row["email"], row["state"], row["day"], row["next_day"]) == (A, "counted", "2026-10-06", "2026-10-20")
    assert row["copy"] == "10/6/2026\t9/22 - 10/5\t2\t50.00%\t1\t50.00%\t0:15:00\t07:30\t0\t\t\t0"


def test_make_route_gone(client, started):
    """Make prep again is gone: its address answers as one that doesn't exist, and nothing starts."""
    response = client.post("/api/prep/make", json={"email": A, "day": "2026-10-06"})
    assert response.status_code in (404, 405)
    assert started == []
    assert "job" not in client.get("/api/prep").json()


def test_tech_view_endpoint(client):
    body = client.get("/api/prep/tech", params={"email": " A@Example.com "}).json()
    assert body["email"] == A and body["selected"] == "2026-10-06"
    assert [p["day"] for p in body["past"]] == ["2026-10-06", "2026-09-22", "2026-09-08", "2026-08-25"]
    assert body["behind"]["label"] == "1:1 10/6 (9/22 – 10/5)"
    assert [c["ticket_id"] for c in body["behind"]["answered"]] == [5001, 5002]
    older = client.get("/api/prep/tech", params={"email": A, "day": "2026-09-22"}).json()
    assert older["selected"] == "2026-09-22"


def test_each_answer_is_counted_from_the_data_as_it_is_now(client, db_path):
    """Asking again gives the same answers, and a call the sync stores in between shows in the next ones at once:
    nothing counted is kept from one answer to the next (T-168)."""
    asks = (("/api/prep", {}), ("/api/prep/tech", {"email": A}), ("/api/prep/trends", {"email": A, "n": "4"}))
    first = [client.get(url, params=params).json() for url, params in asks]
    assert [client.get(url, params=params).json() for url, params in asks] == first
    conn = connect(db_path)
    on = datetime(2026, 10, 1, 15, tzinfo=UTC)
    collected.upsert_legs(conn, [leg(6, 106, A_ID, talk=120, at=on)])
    collected.upsert_calls(conn, [call(106, 5006)])
    conn.close()
    latest, tech, trends = [client.get(url, params=params).json() for url, params in asks]
    assert latest["rows"][0]["cells"]["calls"]["text"] == tech["past"][0]["cells"]["calls"]["text"] == "3"
    assert first[1]["past"][0]["cells"]["calls"]["text"] == "2"
    assert [c["ticket_id"] for c in tech["behind"]["answered"]] == [5001, 5002, 5006]
    assert trends["measures"][0]["values"] == [0, 0, 0, 3]


def test_tech_view_bad_input_is_400(client):
    for params in ({"email": B, "day": "2026-10-06"}, {"email": A, "day": "2026-10-13"}, {"email": B}):
        response = client.get("/api/prep/tech", params=params)
        assert response.status_code == 400, params
        assert response.json()["detail"] == "No prep for that tech in that period.", params
    no_tech = client.get("/api/prep/tech", params={"day": "2026-10-06"})
    assert no_tech.status_code == 400 and no_tech.json()["detail"] == "Choose a tech."
    bad_day = client.get("/api/prep/tech", params={"email": A, "day": "10/06/2026"})
    assert bad_day.status_code == 400 and "YYYY-MM-DD" in bad_day.json()["detail"]


def test_trends_endpoint(client):
    body = client.get("/api/prep/trends", params={"email": A, "n": "4"}).json()
    assert body["days"] == ["2026-08-25", "2026-09-08", "2026-09-22", "2026-10-06"]
    assert [m["key"] for m in body["measures"]][:2] == ["calls", "share"]
    assert body["measures"][0]["values"] == [0, 0, 0, 2]
    assert client.get("/api/prep/trends", params={"email": A}).json()["days"] == body["days"]   # 8 by default
    for n in ("5", "x", "-4", "8.0"):
        response = client.get("/api/prep/trends", params={"email": A, "n": n})
        assert response.status_code == 400 and response.json()["detail"] == "Choose 4, 8, 13 or 26 1:1s.", n
    no_tech = client.get("/api/prep/trends", params={"n": "4"})
    assert no_tech.status_code == 400 and no_tech.json()["detail"] == "Choose a tech."


def test_schedule_endpoint(client, db_path):
    conn = connect(db_path)
    set_setting(conn, "team", [A, B])
    conn.close()
    assert client.get("/api/prep/schedule").json() == [
        {"email": A, "name": A, "date": "2026-10-20", "weeks": 2, "latest_label": "Tue Oct 6",
         "next_label": "Tue Oct 20", "covers": "10/6 – 10/19"},
        {"email": B, "name": B, "date": None, "weeks": None, "latest_label": None, "next_label": None,
         "covers": None},
    ]
