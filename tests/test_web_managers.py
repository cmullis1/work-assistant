"""What a manager may see and change, checked by the server (Managers design, plan Task 2, Review Focus 1 and 2): a
manager sees every tech in the regions they pick, as `_scope` gives them; runs the roster there (adds, edits and removes
techs, assigns techs to teams, adds and removes team leads); and never writes shifts, time off or 1:1 dates, the
settings, a backfill or a Teams Shifts import, never adds a manager and never changes anyone's role. Regions are a view
the manager chooses, not a limit (the spec's anti-goals): what is enforced is the role, and that a save from a
manager's view never drops a lead's techs outside it.

The roster is Avery (NASA), Blake (EMEA), Casey (APAC), Dana (EMEA) and Evan (no region set: NASA). The admin's own
team is Casey, Lee leads Avery and Blake, Kim leads Dana, and Evan is on no team. Parker Lane manages NASA and APAC, so
sees Avery, Casey and Evan. Each tech works Sunday to Thursday, 08:00 to 17:00, from Sep 1, acted on tickets on Monday
Oct 5 and has a PTO day on Wednesday Oct 7. The app's clock is pinned to Saturday Oct 17. Tokens are signed with
test_access's keys, and the certificates come from a stand-in: nothing here reaches Cloudflare or Zendesk."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from test_web_edition import LOCAL
from test_web_scoping import (ADMIN, ALL_REGIONS, APPLY, AVERY, BLAKE, CASEY, DANA, ERIN, FAY, KIM, LEE, NOBODY,
                              NOT_FOUND, PREVIEW, SHIFT, WEEK, FakeZendesk, as_, avery_files, backups_beside, emails,
                              hosted_app, roster_entry, time_off_id)
from work_assistant import people, regions, settings
from work_assistant.activity import store
from work_assistant.activity.model import Action, ShiftVersion
from work_assistant.db import connect, init_db
from work_assistant.rubric import ensure_seeded
from work_assistant.web.app import create_app

UTC = timezone.utc
NOW = datetime(2026, 10, 17, 16, tzinfo=UTC)    # Saturday Oct 17, 11:00 in Chicago
EVAN = "evan@example.com"
TEAM = [AVERY, BLAKE, CASEY, DANA, EVAN]
REGION = {AVERY: "NASA", BLAKE: "EMEA", CASEY: "APAC", DANA: "EMEA"}   # Evan has none set: NASA
MANAGER = "parker.lane@example.com"             # manages NASA and APAC
OTHER_MANAGER = "mo.grant@example.com"
PAT, QUINN = "pat.lead@example.com", "quinn.lead@example.com"   # team leads added in a test
GRACE = "grace.lee@example.com"                 # a tech a refused add would have added
THEIRS = [AVERY, CASEY, EVAN]                   # the manager's techs, in roster order
NOT_A_REGION = "The region must be NASA, EMEA or APAC."
NOT_A_TEAM = "Choose a team you can see."
CANT_LEAD = "A manager can't lead a team."
ADMIN_ONLY = {"detail": "Only the admin can do this."}
LEADS_SET_THESE = {"detail": "Shifts, time off and 1:1 dates: each team lead sets these for their own techs."}
MANAGERS_ONLY = {"detail": "Only a manager picks the regions they see."}
ADD_LEADS_ONLY = "A manager can add team leads only."
REMOVE_LEADS_ONLY = "A manager can remove team leads only."
OUTSIDE = "This team lead has techs outside your regions. Ask the admin to remove them."


def already(email: str) -> str:
    return f"{email} is already added, as someone other than a team lead."


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "work-assistant.db"
    conn = connect(path)
    init_db(conn)
    ensure_seeded(conn)
    settings.set_setting(conn, "team", TEAM)
    event = 1
    for count, email in enumerate(TEAM, start=1):
        store.add_shift_version(conn, ShiftVersion(email, date(2026, 9, 1), frozenset({0, 1, 2, 3, 4}), 480, 1020,
                                                   True))
        store.insert_actions(conn, [Action(email, 100 * count + n, datetime(2026, 10, 5, 14, n, tzinfo=UTC), "reply",
                                           None, None, event + n, 1) for n in range(count)])
        event += count
        store.add_time_off(conn, email, date(2026, 10, 7), date(2026, 10, 7), "dentist", "pto")
        if email in REGION:
            regions.set_regions(conn, email, [(date(2026, 1, 1), REGION[email])])
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", ("2026-10-17T11:00:00Z",) * 3 + ("2026-09-01T05:00:00Z",))
    conn.commit()
    people.ensure_admin(conn, ADMIN)
    people.set_assignments(conn, ADMIN, [CASEY])
    people.add_person(conn, LEE, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, KIM, [DANA])
    people.add_person(conn, MANAGER, "manager", "Parker Lane")
    people.set_manager_regions(conn, MANAGER, ["NASA", "APAC"])
    conn.close()
    return path


@pytest.fixture
def started():
    return []


@pytest.fixture
def app(db_path, tmp_path, started):
    return hosted_app(db_path, tmp_path, started, now=NOW)


@pytest.fixture
def manager(app):
    return TestClient(app, base_url=LOCAL, headers=as_(MANAGER))


@pytest.fixture
def admin(app):
    return TestClient(app, base_url=LOCAL, headers=as_(ADMIN))


@pytest.fixture
def lee(app):
    return TestClient(app, base_url=LOCAL, headers=as_(LEE))


@pytest.fixture
def client_full(db_path, tmp_path):
    app = create_app(db_path, run_starter=lambda spec: 1, zendesk_factory=FakeZendesk, log_dir=tmp_path / "logs",
                     startup_work=False, now=NOW)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def edit(db_path, change) -> None:
    """Change the database directly, as a test's setup."""
    conn = connect(db_path)
    try:
        change(conn)
    finally:
        conn.close()


def tables(db_path) -> dict:
    """Every table a manager's request could write: who is who, the teams, the regions, shifts, time off, the
    settings and the techs' names."""
    conn = connect(db_path)
    try:
        return {table: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))
                for table in ("people", "assignments", "manager_regions", "tech_regions", "shift_versions",
                              "time_off", "settings", "tech_names")}
    finally:
        conn.close()


def teams(db_path) -> dict:
    """Each person's assigned techs, as stored."""
    conn = connect(db_path)
    try:
        return {p["email"]: p["techs"] for p in people.everyone(conn)}
    finally:
        conn.close()


def person(body: dict, email: str) -> dict:
    return next(p for p in body["people"] if p["email"] == email)


# -- what a manager sees -------------------------------------------------------------------------------------------


def test_manager_sees_exactly_their_regions(manager, db_path):
    """Every tech whose current region the manager picked, whoever's team they are on (Evan, on no team and with no
    region set, is NASA), on every Team route; another region's tech is 404 as for one that doesn't exist, and the team
    of a lead with only another region's techs isn't one they can see."""
    grid = manager.get("/api/activity", params=WEEK).json()
    assert emails(grid["rows"]) == THEIRS and emails(grid["picker"]) == THEIRS
    assert emails(manager.get("/api/activity/trends", params=WEEK).json()["techs"]) == THEIRS
    assert manager.get("/api/settings").json()["team"] == THEIRS
    assert emails(manager.get("/api/shifts").json()["techs"]) == THEIRS
    assert sorted(emails(manager.get("/api/time-off").json()["items"])) == THEIRS
    assert emails(manager.get("/api/prep").json()["rows"]) == THEIRS
    assert emails(manager.get("/api/prep/schedule").json()) == THEIRS
    for tech in (BLAKE, DANA, NOBODY):
        for response in (manager.get("/api/activity", params={"tech": tech}),
                         manager.get("/api/activity/trends", params={"tech": tech}),
                         manager.get("/api/activity/day", params={"tech": tech, "day": "2026-10-05"}),
                         manager.get("/api/prep/tech", params={"email": tech}),
                         manager.get("/api/prep/trends", params={"email": tech}),
                         manager.put(f"/api/team/{tech}", json={"name": "Blake Reed"}),
                         manager.put(f"/api/shifts/{tech}/start", json={"started": "2026-09-14"}),
                         manager.delete(f"/api/team/{tech}")):
            assert (response.status_code, response.json()) == (404, NOT_FOUND), (tech, str(response.request.url))
    for response in (manager.get("/api/activity", params={**WEEK, "team": KIM}),
                     manager.get("/api/activity/trends", params={**WEEK, "team": KIM})):
        assert (response.status_code, response.json()["detail"]) == (400, NOT_A_TEAM), str(response.request.url)
    for path in ("/api/activity", "/api/activity/trends", "/api/settings", "/api/shifts", "/api/time-off", "/api/prep",
                 "/api/prep/schedule", "/api/me"):
        text = manager.get(path).text
        assert BLAKE not in text and DANA not in text, path
    conn = connect(db_path)
    try:
        assert settings.team_emails(conn) == TEAM, "the refused Removes took no one off"
    finally:
        conn.close()


def test_manager_regions_round_trip(manager, admin):
    """A manager picks their regions in Your regions (`PUT /api/me/regions`), and `/api/me` says who is signed in, their
    regions and every region with its city. What they see follows the regions picked, none picked being no techs."""
    me = manager.get("/api/me")
    assert (me.status_code, me.json()) == (200, {"email": MANAGER, "role": "manager", "edition": "hosted",
                                                 "name": "Parker Lane", "regions": ["NASA", "APAC"],
                                                 "all_regions": ALL_REGIONS})
    saved = manager.put("/api/me/regions", json={"regions": ["EMEA"]})
    assert (saved.status_code, saved.json()) == (200, {"regions": ["EMEA"]})
    assert manager.get("/api/me").json()["regions"] == ["EMEA"]
    assert emails(manager.get("/api/activity", params=WEEK).json()["rows"]) == [BLAKE, DANA]
    assert manager.put("/api/me/regions", json={"regions": ["APAC", "NASA", "APAC"]}).json() == \
        {"regions": ["NASA", "APAC"]}, "in the regions' order, once each"
    assert emails(manager.get("/api/activity", params=WEEK).json()["rows"]) == THEIRS
    assert manager.put("/api/me/regions", json={"regions": []}).json() == {"regions": []}
    assert manager.get("/api/activity", params=WEEK).json()["rows"] == []
    assert manager.get("/api/settings").json()["team"] == [], "none picked: no techs"
    for body, detail in (({"regions": ["LATAM"]}, NOT_A_REGION), ({"regions": ["NASA", "apac"]}, NOT_A_REGION),
                         ({"regions": [7]}, NOT_A_REGION), ({"regions": "NASA"}, "Send the regions as a list."),
                         ({}, "Send the regions as a list."), (["NASA"], "Send the regions as a list.")):
        refused = manager.put("/api/me/regions", json=body)
        assert (refused.status_code, refused.json()) == (400, {"detail": detail}), body
    assert manager.get("/api/me").json()["regions"] == [], "nothing changed"
    assert admin.get("/api/me").json() == {"email": ADMIN, "role": "admin", "edition": "hosted", "name": None,
                                           "regions": None, "all_regions": ALL_REGIONS}


def test_only_managers_pick_regions(lee, admin, client_full, db_path):
    for client in (lee, admin):
        refused = client.put("/api/me/regions", json={"regions": ["NASA"]})
        assert (refused.status_code, refused.json()) == (403, MANAGERS_ONLY)
    assert lee.get("/api/me").json()["regions"] is None
    missing = client_full.put("/api/me/regions", json={"regions": ["NASA"]})
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND), "the PC copy has no managers"
    assert tables(db_path)["manager_regions"] == [(MANAGER, "APAC"), (MANAGER, "NASA")]


# -- the roster in a manager's regions -----------------------------------------------------------------------------


def test_manager_adds_edits_and_removes_their_techs(manager, admin, db_path):
    """A manager adds a tech with a region and a team lead (the admin or a team lead; none puts them on no team), edits
    any tech in their regions, moving them to any region (the answer says when that takes them out of the manager's
    view), sets their first tracked day and removes them, from the roster and every team. The admin's add takes a
    region too. Refused adds and edits save nothing."""
    added = manager.post("/api/team", json={"name": "Erin Park", "email": ERIN, "lead": LEE, "region": "APAC"})
    assert added.status_code == 200, added.text
    assert added.json()["added"] == ERIN
    assert added.json()["team"] == [*THEIRS, ERIN], "the answer is the manager's settings: their regions' techs"
    entry = roster_entry(admin, ERIN)
    assert (entry["leads"], entry["region"], entry["regions"]) == (
        [LEE], "APAC", [{"since": "2026-10-17", "region": "APAC"}]), "the region from the app's today"
    alone = manager.post("/api/team", json={"name": "Fay Hill", "email": FAY, "lead": " ", "region": "NASA"})
    assert alone.status_code == 200, alone.text
    assert (roster_entry(admin, FAY)["leads"], roster_entry(admin, FAY)["region"]) == ([], "NASA"), \
        "a manager's tech with no team lead chosen is on no team: managers hold none"
    for body, detail in (({"lead": MANAGER}, CANT_LEAD), ({"lead": NOBODY}, f"{NOBODY} hasn't been added."),
                         ({"region": "LATAM"}, NOT_A_REGION), ({"region": "apac"}, NOT_A_REGION),
                         ({"region": 7}, NOT_A_REGION)):
        refused = manager.post("/api/team", json={"name": "Grace Lee", "email": GRACE, "lead": LEE, **body})
        assert (refused.status_code, refused.json()) == (400, {"detail": detail}), body
    refused = admin.post("/api/team", json={"name": "Grace Lee", "email": GRACE, "lead": MANAGER})
    assert (refused.status_code, refused.json()) == (400, {"detail": CANT_LEAD}), "for the admin too"
    assert GRACE not in emails(admin.get("/api/people").json()["roster"]), "nothing is saved"
    by_admin = admin.post("/api/team", json={"name": "Grace Lee", "email": GRACE, "region": "EMEA"})
    assert by_admin.status_code == 200, by_admin.text
    assert (roster_entry(admin, GRACE)["leads"], roster_entry(admin, GRACE)["region"]) == ([ADMIN], "EMEA")

    edited = manager.put(f"/api/team/{CASEY}", json={"name": "Casey Quinn", "started": "2026-09-14",
                                                     "leads": [ADMIN, LEE]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["edited"] == CASEY and "moved_out" not in edited.json()
    entry = roster_entry(admin, CASEY)
    assert (entry["name"], entry["started"], entry["leads"]) == ("Casey Quinn", "2026-09-14", [ADMIN, LEE])
    before = roster_entry(admin, CASEY)
    refused = manager.put(f"/api/team/{CASEY}", json={"name": "Casey Moss", "leads": [LEE, MANAGER]})
    assert (refused.status_code, refused.json()) == (400, {"detail": CANT_LEAD})
    assert roster_entry(admin, CASEY) == before, "checked before anything is saved"
    refused = admin.put(f"/api/team/{CASEY}", json={"leads": [MANAGER]})
    assert (refused.status_code, refused.json()) == (400, {"detail": CANT_LEAD}), "for the admin too"

    moved = manager.put(f"/api/team/{AVERY}", json={"regions": [{"since": "2026-10-12", "region": "EMEA"}]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["moved_out"] is True and AVERY not in moved.json()["team"]
    assert manager.get("/api/activity", params={"tech": AVERY}).status_code == 404, "out of their view now"
    back = admin.put(f"/api/team/{AVERY}", json={"regions": [{"since": "2026-01-01", "region": "NASA"}]})
    assert back.status_code == 200 and "moved_out" not in back.json(), "only a manager's answer says it"
    stays = manager.put(f"/api/team/{CASEY}", json={"regions": [{"since": "2026-01-01", "region": "NASA"}]})
    assert stays.status_code == 200 and "moved_out" not in stays.json(), "NASA is one of theirs"

    start = manager.put(f"/api/shifts/{EVAN}/start", json={"started": "2026-09-14"})
    assert start.status_code == 200, start.text
    assert roster_entry(admin, EVAN)["started"] == "2026-09-14"

    removed = manager.delete(f"/api/team/{ERIN}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] == ERIN and ERIN not in removed.json()["team"]
    assert ERIN not in emails(admin.get("/api/people").json()["roster"])
    assert ERIN not in teams(db_path)[LEE], "off every team, whoever leads them"


def test_manager_edit_techs_keeps_other_regions(manager, admin, db_path):
    """Review Focus 2: a manager's Edit techs changes only the techs in their regions; the lead's techs elsewhere stay,
    whatever the manager sends. A tech outside their regions is 404, and a manager is never a team, for anyone."""
    saved = manager.put(f"/api/people/{LEE}/techs", json={"techs": []})
    assert saved.status_code == 200, saved.text
    assert LEE not in emails(saved.json()["people"]), "no tech of Lee's in the manager's regions now: not listed"
    assert teams(db_path)[LEE] == [BLAKE], "the EMEA tech the manager doesn't see stays Lee's"
    again = manager.put(f"/api/people/{LEE}/techs", json={"techs": [EVAN, "Avery@Example.com"]})
    assert again.status_code == 200, again.text
    assert person(again.json(), LEE)["techs"] == [AVERY, EVAN]
    assert teams(db_path)[LEE] == [AVERY, BLAKE, EVAN]
    kim = manager.put(f"/api/people/{KIM}/techs", json={"techs": [CASEY]})
    assert kim.status_code == 200, kim.text
    assert teams(db_path)[KIM] == [CASEY, DANA], "Kim keeps Dana"
    before = teams(db_path)
    refused = manager.put(f"/api/people/{LEE}/techs", json={"techs": [AVERY, BLAKE]})
    assert (refused.status_code, refused.json()) == (404, NOT_FOUND), "Blake is outside their regions"
    for client in (manager, admin):
        refused = client.put(f"/api/people/{MANAGER}/techs", json={"techs": [AVERY]})
        assert (refused.status_code, refused.json()) == (400, {"detail": CANT_LEAD})
    assert teams(db_path) == before, "nothing changed"
    whole = admin.put(f"/api/people/{LEE}/techs", json={"techs": []})
    assert whole.status_code == 200 and teams(db_path)[LEE] == [], "the admin's save is the whole team, as before"


def test_manager_adds_team_leads_only(manager, db_path):
    """A manager adds team leads, never a manager or an admin, and changes nobody's role: an email already added as the
    admin or a manager is refused, and nothing of a refused add is saved."""
    edit(db_path, lambda conn: people.add_person(conn, OTHER_MANAGER, "manager", "Mo Grant"))
    added = manager.post("/api/people", json={"name": "Pat Lead", "email": PAT, "role": "tl"})
    assert added.status_code == 200, added.text
    assert person(added.json(), PAT) == {"email": PAT, "role": "tl", "name": "Pat Lead", "techs": []}, \
        "a team lead with no techs yet is in the manager's list"
    before = tables(db_path)
    for body, detail in (({"name": "Quinn Lead", "email": QUINN, "role": "manager"}, ADD_LEADS_ONLY),
                         ({"name": "Quinn Lead", "email": QUINN, "role": "admin"}, ADD_LEADS_ONLY),
                         ({"email": LEE, "role": "manager"}, ADD_LEADS_ONLY),
                         ({"email": ADMIN, "role": "tl"}, already(ADMIN)),
                         ({"email": "Mo.Grant@Example.com", "role": "tl"}, already(OTHER_MANAGER))):
        refused = manager.post("/api/people", json=body)
        assert (refused.status_code, refused.json()) == (400, {"detail": detail}), body
    assert tables(db_path) == before


def test_manager_removes_leads_within_regions(manager, db_path):
    """A manager removes a team lead whose techs are all in the manager's regions (or who has none); a lead with any
    tech elsewhere stays, with the exact copy, and so does anyone who isn't a team lead."""
    def setup(conn):
        people.add_person(conn, PAT, "tl")
        people.set_assignments(conn, PAT, [AVERY, CASEY])
        people.add_person(conn, QUINN, "tl")
        people.add_person(conn, OTHER_MANAGER, "manager")

    edit(db_path, setup)
    removed = manager.delete(f"/api/people/{PAT}")
    assert removed.status_code == 200, removed.text
    assert PAT not in emails(removed.json()["people"]) and PAT not in teams(db_path)
    assert manager.delete(f"/api/people/{QUINN}").status_code == 200, "no techs: none outside their regions"
    before = tables(db_path)
    for address, detail in ((LEE, OUTSIDE), (KIM, OUTSIDE), (ADMIN, REMOVE_LEADS_ONLY),
                            (OTHER_MANAGER, REMOVE_LEADS_ONLY), (MANAGER, "You can't remove yourself.")):
        refused = manager.delete(f"/api/people/{address}")
        assert (refused.status_code, refused.json()) == (400, {"detail": detail}), address
    missing = manager.delete(f"/api/people/{NOBODY}")
    assert (missing.status_code, missing.json()) == (404, {"detail": f"{NOBODY} hasn't been added."})
    assert tables(db_path) == before


def test_manager_role_limits(manager, db_path, tmp_path, started):
    """Review Focus 1: whatever a manager asks, they write no shift, time off or 1:1 date, no setting (activity flags,
    Zendesk), start no backfill, add no manager or admin, change nobody's role and import nothing from Teams Shifts,
    for a tech of theirs or not; the database is as it was."""
    item = time_off_id(db_path, AVERY)
    before = tables(db_path)
    adds = {"name": "Quinn Lead", "email": QUINN}
    for method, path, kwargs, answer in [
            ("PUT", f"/api/shifts/{AVERY}", {"json": SHIFT}, (403, LEADS_SET_THESE)),
            ("PUT", f"/api/shifts/{BLAKE}", {"json": SHIFT}, (403, LEADS_SET_THESE)),
            ("POST", "/api/time-off", {"json": {"email": AVERY, "first": "2026-10-12", "last": "2026-10-12",
                                                "kind": "sick"}}, (403, LEADS_SET_THESE)),
            ("PATCH", f"/api/time-off/{item}", {"json": {"kind": "sick"}}, (403, LEADS_SET_THESE)),
            ("DELETE", "/api/time-off/day", {"params": {"email": AVERY, "day": "2026-10-07"}}, (403, LEADS_SET_THESE)),
            ("DELETE", f"/api/time-off/{item}", {}, (403, LEADS_SET_THESE)),
            ("PUT", f"/api/prep/schedule/{AVERY}", {"json": {"date": "2026-10-19", "weeks": 2}},
             (403, LEADS_SET_THESE)),
            ("PUT", f"/api/prep/schedule/{CASEY}", {"json": {"date": None}}, (403, LEADS_SET_THESE)),
            ("PUT", "/api/settings", {"json": {"activity_gap_minutes": 100}}, (403, ADMIN_ONLY)),
            ("PUT", "/api/settings", {"json": {"activity_late_minutes": 30, "activity_early_minutes": 30}},
             (403, ADMIN_ONLY)),
            ("PUT", "/api/settings", {"json": {"zendesk_subdomain": "elsewhere", "zendesk_email": "x@example.com"}},
             (403, ADMIN_ONLY)),
            ("POST", "/api/activity/sync", {"json": {"kind": "backfill"}}, (403, ADMIN_ONLY)),
            ("POST", "/api/people", {"json": {**adds, "role": "manager"}}, (400, {"detail": ADD_LEADS_ONLY})),
            ("POST", "/api/people", {"json": {**adds, "role": "admin"}}, (400, {"detail": ADD_LEADS_ONLY})),
            ("POST", "/api/people", {"json": {"email": LEE, "role": "manager"}}, (400, {"detail": ADD_LEADS_ONLY})),
            ("POST", "/api/people", {"json": {"email": LEE, "role": "admin"}}, (400, {"detail": ADD_LEADS_ONLY})),
            ("POST", "/api/people", {"json": {"email": ADMIN, "role": "tl"}}, (400, {"detail": already(ADMIN)})),
            ("POST", "/api/people", {"json": {"email": MANAGER, "role": "tl"}},
             (400, {"detail": "You can't change your own role."})),
            ("POST", PREVIEW, {"json": avery_files()}, (403, ADMIN_ONLY)),
            ("POST", APPLY, {"json": avery_files()}, (403, ADMIN_ONLY))]:
        response = manager.request(method, path, **kwargs)
        assert (response.status_code, response.json()) == answer, (method, path, kwargs)
    assert tables(db_path) == before
    assert started == [] and backups_beside(tmp_path) == [], "nothing started, no import's backup"


def test_people_for_a_manager(manager, db_path):
    """A manager's People answer: the roster is their regions' techs; the people are the admin and the team leads with
    a tech among them, each with only those techs, and the team leads with no techs yet; never a manager."""
    def setup(conn):
        people.add_person(conn, PAT, "tl", "Pat Lead")
        people.add_person(conn, OTHER_MANAGER, "manager", "Mo Grant")
        people.set_manager_regions(conn, OTHER_MANAGER, ["NASA"])

    edit(db_path, setup)
    response = manager.get("/api/people")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["people"] == [{"email": ADMIN, "role": "admin", "name": None, "techs": [CASEY]},
                              {"email": LEE, "role": "tl", "name": None, "techs": [AVERY]},
                              {"email": PAT, "role": "tl", "name": "Pat Lead", "techs": []}]
    assert [(t["email"], t["leads"], t["region"]) for t in body["roster"]] == [
        (AVERY, [LEE], "NASA"), (CASEY, [ADMIN], "APAC"), (EVAN, [], "NASA")]
    for hidden in (BLAKE, DANA, KIM, MANAGER, OTHER_MANAGER):
        assert hidden not in response.text, hidden
    assert manager.put("/api/me/regions", json={"regions": ["NASA"]}).status_code == 200
    assert manager.get("/api/people").json()["people"] == [
        {"email": LEE, "role": "tl", "name": None, "techs": [AVERY]},
        {"email": PAT, "role": "tl", "name": "Pat Lead", "techs": []}], "the admin has no tech in NASA"


def test_admin_people_lists_manager_regions(admin):
    """The admin's People answer lists everyone and the whole roster, as before, and each manager's row carries the
    regions they picked."""
    body = admin.get("/api/people").json()
    assert body["people"] == [{"email": ADMIN, "role": "admin", "name": None, "techs": [CASEY]},
                              {"email": KIM, "role": "tl", "name": None, "techs": [DANA]},
                              {"email": LEE, "role": "tl", "name": None, "techs": [AVERY, BLAKE]},
                              {"email": MANAGER, "role": "manager", "name": "Parker Lane", "techs": [],
                               "regions": ["NASA", "APAC"]}]
    assert emails(body["roster"]) == TEAM
    added = admin.post("/api/people", json={"name": "Mo Grant", "email": OTHER_MANAGER, "role": "manager"})
    assert added.status_code == 200, added.text
    assert person(added.json(), OTHER_MANAGER) == {"email": OTHER_MANAGER, "role": "manager", "name": "Mo Grant",
                                                   "techs": [], "regions": []}
    refused = admin.post("/api/people", json={"email": OTHER_MANAGER, "role": "boss"})
    assert (refused.status_code, refused.json()) == (400, {"detail": "The role must be admin, tl or manager."})


def test_a_role_change_drops_what_the_old_role_held(admin, db_path):
    """The admin re-adding someone with a new role: a team lead made a manager loses their team, and a manager made a
    team lead loses their regions, in the same change (made a manager again, they pick again)."""
    made = admin.post("/api/people", json={"email": LEE, "role": "manager"})
    assert made.status_code == 200, made.text
    assert person(made.json(), LEE) == {"email": LEE, "role": "manager", "name": None, "techs": [], "regions": []}
    assert {t["email"]: t["leads"] for t in made.json()["roster"]}[AVERY] == [], "Avery was only Lee's"
    assert LEE not in [row[0] for row in tables(db_path)["assignments"]]
    back = admin.post("/api/people", json={"email": MANAGER, "role": "tl"})
    assert back.status_code == 200, back.text
    assert person(back.json(), MANAGER) == {"email": MANAGER, "role": "tl", "name": "Parker Lane", "techs": []}
    assert tables(db_path)["manager_regions"] == []
    again = admin.post("/api/people", json={"email": MANAGER, "role": "manager"})
    assert person(again.json(), MANAGER)["regions"] == [], "made a manager again, they pick again"


# -- one team on Activity (T-144's team rule, widened for managers) ------------------------------------------------


def test_activity_by_team(manager, admin, lee, client_full):
    """`team` on Activity narrows the grid to that lead's team among the viewer's techs, as it does on Trends (T-144),
    through the same check: a team lead names their own team, the admin any lead's, a manager any lead with a tech among
    theirs; a manager is never a team. The Tech picker keeps every tech the viewer sees."""
    def grid(client, **params) -> tuple:
        response = client.get("/api/activity", params={**WEEK, **params})
        assert response.status_code == 200, (params, response.text)
        return emails(response.json()["rows"]), emails(response.json()["picker"])

    assert grid(manager, team=LEE) == ([AVERY], THEIRS), "Lee's techs among the manager's; Blake is EMEA"
    assert grid(manager, team=" Owner@Example.com ") == ([CASEY], THEIRS), "in any case"
    assert grid(manager, team=LEE, region="APAC") == ([], THEIRS)
    assert grid(manager) == (THEIRS, THEIRS)
    assert grid(lee, team=LEE) == grid(lee) == ([AVERY, BLAKE], [AVERY, BLAKE])
    assert grid(admin, team=ADMIN) == ([CASEY], [CASEY])
    assert grid(admin, team=LEE) == ([], [CASEY]), "Lee's techs among the admin's own: none"
    for client, lead in ((manager, KIM), (manager, MANAGER), (manager, NOBODY), (lee, KIM), (lee, ADMIN),
                         (admin, MANAGER), (admin, AVERY), (client_full, LEE)):
        for response in (client.get("/api/activity", params={**WEEK, "team": lead}),
                         client.get("/api/activity/trends", params={**WEEK, "team": lead})):
            assert (response.status_code, response.json()["detail"]) == (400, NOT_A_TEAM), \
                (lead, str(response.request.url))

    def trends(**params) -> tuple:
        response = manager.get("/api/activity/trends", params={**WEEK, **params})
        assert response.status_code == 200, (params, response.text)
        return response.json()["team"], emails(response.json()["techs"])

    assert trends(team=LEE) == (LEE, [AVERY])
    assert trends(tech=AVERY) == (LEE, [AVERY]), "one tech is compared with their team lead's team, among theirs"
    assert trends(tech=CASEY) == (ADMIN, [CASEY])
    assert trends(tech=EVAN) == (None, THEIRS), "a tech on no team is compared with every tech they see"


# -- the admin sets a manager's regions (the owner, 2026-10-22: the admin and the manager both can) ------------


def test_admin_sets_a_managers_regions(admin, manager, db_path):
    body = admin.put(f"/api/people/{MANAGER}/regions", json={"regions": ["EMEA"]})
    assert body.status_code == 200
    assert person(body.json(), MANAGER)["regions"] == ["EMEA"]
    with connect(db_path) as conn:
        assert people.manager_regions(conn, MANAGER) == ["EMEA"]
    assert manager.put("/api/me/regions", json={"regions": ["NASA", "APAC"]}).json() == {"regions": ["NASA", "APAC"]}


def test_only_the_admin_sets_regions_and_only_a_managers(admin, manager, lee):
    assert manager.put(f"/api/people/{MANAGER}/regions", json={"regions": ["EMEA"]}).status_code == 403
    assert lee.put(f"/api/people/{MANAGER}/regions", json={"regions": ["EMEA"]}).status_code == 403
    refused = admin.put(f"/api/people/{LEE}/regions", json={"regions": ["EMEA"]})
    assert refused.status_code == 400 and refused.json()["detail"] == "Only a manager has regions."
    assert admin.put(f"/api/people/{NOBODY}/regions", json={"regions": []}).status_code == 404
    bad = admin.put(f"/api/people/{MANAGER}/regions", json={"regions": ["Mars"]})
    assert bad.status_code == 400 and bad.json()["detail"] == NOT_A_REGION
    assert admin.put(f"/api/people/{MANAGER}/regions", json={"regions": "NASA"}).status_code == 400


# -- the next scheduled sync, in place of Refresh on the hosted copy ------------------------------------------


def test_hosted_answers_say_when_the_next_sync_comes(admin, client_full):
    # NOW is Saturday Oct 17, 11:00 in Chicago: the next sync is noon.
    assert admin.get("/api/activity").json()["synced"]["next"] == "2026-10-17T17:00:00Z"
    assert admin.get("/api/prep").json()["next_sync"] == "2026-10-17T17:00:00Z"
    assert "next" not in client_full.get("/api/activity").json()["synced"], "the PC has Refresh instead"
    assert "next_sync" not in client_full.get("/api/prep").json()
