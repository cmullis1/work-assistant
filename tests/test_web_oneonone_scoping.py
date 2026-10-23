"""1:1 prep on the hosted copy (hosted Team design §4, plan Task 3, Review Focus 1 and 3): every prep route goes
through the same scoping as Team activity for a lead and for the admin alike (another team's tech is 404), the list
and the Trends "Team average" are over the viewer's team only, a lead saves their own techs' 1:1 schedules through a
scoped route (the general settings save refuses the key), and typed names come first. Each prep is counted from the
collected data when asked; nothing is made.

Same people as test_web_scoping: the admin leads Avery and Casey, Lee leads Avery and Blake, Kim leads Blake and Casey,
and Dana is on no team. Today is Sat Oct 17. Every tech has a 1:1 on Tue Oct 13 every 2 weeks (Sep 29 to Oct 12),
except Dana, whose 1:1 is Sun Oct 18 (her latest Oct 4: Sep 20 to Oct 3). On Oct 1, in all four periods, they answer
10, 20, 40 and 80 calls. The calls are collected from Dana's period on, so each tech has their latest period only."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from test_oneonone_live import call, cover, leg
from test_web_scoping import (  # noqa: F401 (fixtures: pytest finds them by name here)
    ADMIN, ADMIN_TEAM, AVERY, BLAKE, CASEY, DANA, KIM, LEE, LOCAL, NOBODY, NOT_FOUND, app, as_, client_full,
    client_hosted, client_hosted_admin, client_hosted_tl, emails, started)
from test_web_scoping import db_path as scoping_db_path

from work_assistant import names, settings
from work_assistant.activity import collected
from work_assistant.db import connect

CALLS = {AVERY: 10, BLAKE: 20, CASEY: 40, DANA: 80}
AGENT_IDS = {AVERY: 1, BLAKE: 2, CASEY: 3, DANA: 4}
SCHEDULE = {AVERY: {"date": "2026-10-13", "weeks": 2}, BLAKE: {"date": "2026-10-13", "weeks": 2},
            CASEY: {"date": "2026-10-13", "weeks": 2}, DANA: {"date": "2026-10-18", "weeks": 2}}
UTC = timezone.utc
ON = datetime(2026, 10, 1, 15, tzinfo=UTC)                 # 10:00 Oct 1 in Chicago
COLLECTED_FROM = datetime(2026, 9, 20, 5, tzinfo=UTC)      # midnight starting Dana's period, Sep 20
SAVE_REFUSED = {"detail": "Save 1:1 schedules from the 1:1 schedule panel."}


@pytest.fixture
def db_path(scoping_db_path):
    conn = connect(scoping_db_path)
    try:
        settings.set_setting(conn, "oneonone_schedule", SCHEDULE)
        collected.set_agents(conn, AGENT_IDS)
        legs = [email for email, calls in CALLS.items() for _ in range(calls)]
        collected.upsert_legs(conn, [leg(k, k, AGENT_IDS[email], at=ON) for k, email in enumerate(legs, 1)])
        collected.upsert_calls(conn, [call(k, 5000 + k) for k in range(1, len(legs) + 1)])
        cover(conn, start=COLLECTED_FROM, through=datetime(2026, 10, 17, 11, tzinfo=UTC))
    finally:
        conn.close()
    return scoping_db_path


def stored_schedule(db_path) -> dict:
    conn = connect(db_path)
    try:
        return settings.get_setting(conn, "oneonone_schedule")
    finally:
        conn.close()


def team_average(client, email) -> list:
    measures = client.get("/api/prep/trends", params={"email": email}).json()["measures"]
    return next(m for m in measures if m["key"] == "calls")["team"]


# -- every route, scoped -------------------------------------------------------------------------------------


@pytest.mark.parametrize("who, outside", [("lead", (CASEY, DANA, NOBODY, CASEY.upper())),
                                          ("admin", (BLAKE, DANA, NOBODY, BLAKE.upper()))])
def test_every_prep_route_scoped_for_lead_and_admin(who, outside, client_hosted_tl, client_hosted_admin, started, db_path):
    """Review Focus 1: a tech on another team, on no team, or that doesn't exist answers 404 on every prep address,
    the schedule save included, and nothing changes."""
    client = client_hosted_tl if who == "lead" else client_hosted_admin
    before = stored_schedule(db_path)
    for email in outside:
        answers = [
            client.get("/api/prep/tech", params={"email": email}),
            client.get("/api/prep/trends", params={"email": email}),
            client.put(f"/api/prep/schedule/{email}", json={"date": "2027-01-05", "weeks": 3}),
            client.put(f"/api/prep/schedule/{email}", json={"date": None, "weeks": 3}),
        ]
        assert [(r.status_code, r.json()) for r in answers] == [(404, NOT_FOUND)] * 4, email
    assert started == [] and stored_schedule(db_path) == before


def test_prep_routes_answer_for_a_leads_own_techs(client_hosted_tl, started):
    avery = client_hosted_tl.get("/api/prep/tech", params={"email": AVERY})
    assert avery.status_code == 200 and [p["day"] for p in avery.json()["past"]] == ["2026-10-13"]
    assert avery.json()["past"][0]["cells"]["calls"]["text"] == "10"
    assert client_hosted_tl.get("/api/prep/trends", params={"email": BLAKE.upper()}).status_code == 200
    made = client_hosted_tl.post("/api/prep/make", json={"email": AVERY, "day": "2026-10-13"})
    assert made.status_code in (404, 405) and started == [], "nothing is made"


def test_prep_is_open_to_every_known_person_and_not_to_strangers(client_hosted_tl, client_hosted_admin, client_hosted):
    """A lead and the admin are let through (1:1 prep is no longer the admin's only); someone the admin hasn't added
    never reaches a prep route."""
    for client in (client_hosted_tl, client_hosted_admin):
        assert client.get("/api/prep").status_code == 200
        assert client.get("/api/prep/schedule").status_code == 200
    stranger = client_hosted.get("/api/prep", headers=as_("someone@example.com"))
    assert stranger.status_code == 403 and stranger.json()["error"] == "not_added"


def test_prep_latest_lists_only_team(client_hosted_tl, client_hosted_admin, client_full):
    lee = client_hosted_tl.get("/api/prep").json()
    assert emails(lee["rows"]) == [AVERY, BLAKE] and "next" not in lee
    assert [r["cells"]["calls"]["text"] for r in lee["rows"]] == ["10", "20"]
    admin = client_hosted_admin.get("/api/prep").json()
    assert emails(admin["rows"]) == ADMIN_TEAM
    assert emails(client_hosted_tl.get("/api/prep/schedule").json()) == [AVERY, BLAKE]
    assert emails(client_hosted_admin.get("/api/prep/schedule").json()) == ADMIN_TEAM
    everyone = client_full.get("/api/prep").json()
    assert emails(everyone["rows"]) == [AVERY, BLAKE, CASEY, DANA], "the PC copy is unchanged"
    assert [r["day"] for r in everyone["rows"]] == ["2026-10-13"] * 3 + ["2026-10-04"]
    assert DANA not in client_hosted_tl.get("/api/prep").text and CASEY not in client_hosted_tl.get("/api/prep").text


def test_prep_trends_team_average_over_team_only(client_hosted_tl, client_hosted_admin, client_full):
    """Review Focus 3: the average of the viewer's own team, not the roster's (37.5)."""
    assert team_average(client_hosted_tl, AVERY) == [15], "Lee leads Avery (10) and Blake (20)"
    assert team_average(client_hosted_admin, AVERY) == [25], "the admin leads Avery (10) and Casey (40)"
    assert team_average(client_full, AVERY) == [37.5], "the PC copy averages the whole team"
    kim = TestClient(client_hosted_tl.app, base_url=LOCAL, headers=as_(KIM))
    assert team_average(kim, BLAKE) == [30], "Kim leads Blake (20) and Casey (40)"


# -- schedules ---------------------------------------------------------------------------------------------


def test_schedule_save_scoped_and_validated(client_hosted_tl, db_path):
    saved = client_hosted_tl.put(f"/api/prep/schedule/{AVERY.upper()}", json={"date": "2026-10-20", "weeks": 3})
    assert saved.status_code == 200
    assert saved.json() == {"email": AVERY, "name": AVERY, "date": "2026-10-20", "weeks": 3,
                            "latest_label": "Tue Sep 29", "next_label": "Tue Oct 20", "covers": "10/6 – 10/19"}
    after = stored_schedule(db_path)
    assert after[AVERY] == {"date": "2026-10-20", "weeks": 3}
    assert {e: v for e, v in after.items() if e != AVERY} == {e: v for e, v in SCHEDULE.items() if e != AVERY}, \
        "no other tech's entry changes"
    # A tech with no entry yet gets one
    conn = connect(db_path)
    settings.set_setting(conn, "oneonone_schedule", {e: v for e, v in SCHEDULE.items() if e != BLAKE})
    conn.close()
    assert client_hosted_tl.put(f"/api/prep/schedule/{BLAKE}", json={"date": "2026-10-14", "weeks": 1}).status_code == 200
    assert stored_schedule(db_path)[BLAKE] == {"date": "2026-10-14", "weeks": 1}
    # null clears that tech's entry only
    cleared = client_hosted_tl.put(f"/api/prep/schedule/{AVERY}", json={"date": None, "weeks": 2})
    assert cleared.status_code == 200 and cleared.json()["date"] is None
    assert AVERY not in stored_schedule(db_path) and CASEY in stored_schedule(db_path)
    assert client_hosted_tl.put(f"/api/prep/schedule/{AVERY}", json={"date": None}).status_code == 200, "nothing to clear"

    before = stored_schedule(db_path)
    for body, message in [
            ({"date": "2026-10-20", "weeks": 0}, f"The repeat for {AVERY} must be a whole number of weeks from 1 to 8"),
            ({"date": "2026-10-20", "weeks": 9}, f"The repeat for {AVERY} must be a whole number of weeks from 1 to 8"),
            ({"date": "2026-10-20", "weeks": 2.5}, f"The repeat for {AVERY} must be a whole number of weeks from 1 to 8"),
            ({"date": "2026-10-20", "weeks": True}, f"The repeat for {AVERY} must be a whole number of weeks from 1 to 8"),
            ({"date": "2026-10-20"}, f"The repeat for {AVERY} must be a whole number of weeks from 1 to 8"),
            ({"date": "next Tuesday", "weeks": 2}, f"Enter the 1:1 date for {AVERY} as a date, like 2026-10-20"),
            ({"date": "", "weeks": 2}, f"Enter the 1:1 date for {AVERY} as a date, like 2026-10-20"),
            ({"weeks": 2}, "Send the 1:1 date (a day, or null to clear it) and the repeat."),
            ([], "Send the 1:1 date (a day, or null to clear it) and the repeat."),
            ("2026-10-20", "Send the 1:1 date (a day, or null to clear it) and the repeat.")]:
        response = client_hosted_tl.put(f"/api/prep/schedule/{AVERY}", json=body)
        assert (response.status_code, response.json()["detail"]) == (400, message), body
    assert stored_schedule(db_path) == before


def test_the_admin_saves_their_own_techs_schedules_the_same_way(client_hosted_admin, db_path):
    assert client_hosted_admin.put(f"/api/prep/schedule/{CASEY}", json={"date": "2026-10-21", "weeks": 4}).status_code == 200
    assert stored_schedule(db_path)[CASEY] == {"date": "2026-10-21", "weeks": 4}


def test_settings_schedule_filtered_and_put_refuses_schedule_key(client_hosted_admin, client_hosted_tl, client_full, db_path):
    assert set(client_hosted_admin.get("/api/settings").json()["oneonone_schedule"]) == set(ADMIN_TEAM)
    assert set(client_hosted_tl.get("/api/settings").json()["oneonone_schedule"]) == {AVERY, BLAKE}
    before = stored_schedule(db_path)
    refused = client_hosted_admin.put("/api/settings", json={"oneonone_schedule": {AVERY: {"date": "2026-10-20", "weeks": 3}}})
    assert (refused.status_code, refused.json()) == (400, SAVE_REFUSED)
    mixed = client_hosted_admin.put("/api/settings", json={"oneonone_schedule": {}, "activity_gap_minutes": 99})
    assert (mixed.status_code, mixed.json()) == (400, SAVE_REFUSED), "nothing else in that save is made either"
    assert client_hosted_admin.get("/api/settings").json()["activity_gap_minutes"] != 99
    assert stored_schedule(db_path) == before
    assert client_hosted_admin.put("/api/settings", json={"activity_gap_minutes": 99}).status_code == 200
    assert stored_schedule(db_path) == before, "a general save leaves every schedule as stored"
    # The PC copy saves its schedule through the settings, as it always has.
    ok = client_full.put("/api/settings", json={"oneonone_schedule": {AVERY: {"date": "2026-10-20", "weeks": 3}}})
    assert ok.status_code == 200 and stored_schedule(db_path) == {AVERY: {"date": "2026-10-20", "weeks": 3}}


# -- names ------------------------------------------------------------------------------------------------


def test_prep_shows_typed_names_first(client_hosted_tl, db_path):
    conn = connect(db_path)
    names.store(conn, AVERY, "Avery Typed")
    names.store(conn, BLAKE, "  Blake   Typed ")
    conn.close()
    page = client_hosted_tl.get("/api/prep").json()
    assert [r["name"] for r in page["rows"]] == ["Avery Typed", "Blake Typed"]
    assert client_hosted_tl.get("/api/prep/tech", params={"email": AVERY}).json()["name"] == "Avery Typed"
    assert [r["name"] for r in client_hosted_tl.get("/api/prep/schedule").json()] == ["Avery Typed", "Blake Typed"]
