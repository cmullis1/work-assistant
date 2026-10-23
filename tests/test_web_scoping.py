"""People and per-TL scoping in the hosted copy (hosted Team activity design §4, plan Task 3, Review Focus 1, 3 and
5): the admin from the config, the "not added yet" refusal, each team lead (TL) seeing and changing only their
assigned techs through every Team activity address, the admin-only People API and settings, and the owner's PC
copy unchanged.

The roster is Avery, Blake, Casey and Dana. The admin's own team is Avery and Casey, Lee leads Avery and Blake, Kim
leads Blake and Casey, and Dana is on no team. Everyone, the admin included, sees only their own team (hosted Team
design §3, plan Task 2).
Each works Sunday to Thursday, 08:00 to 17:00, from Sep 1. On Monday Oct 5 Avery acted on 1 ticket, Blake on 2, Casey
on 3 and Dana on 4, and each has a PTO day on Wednesday Oct 7. Tokens are signed with test_access's keys, and the
certificates come from a stand-in: nothing here reaches Cloudflare or Zendesk."""
from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date, datetime, timezone

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from test_access import AUD, DOMAIN, Certs, token
from test_shifts_import import NASA as NASA_TEAM
from test_shifts_import import week, workbook
from test_web_edition import CALL_QA_ROUTES, KEPT_ROUTES, LOCAL, hosted_config
from work_assistant import access, people, regions, settings
from work_assistant.activity import store
from work_assistant.activity.model import Action, ShiftVersion
from work_assistant.db import connect, init_db
from work_assistant.rubric import ensure_seeded
from work_assistant.web.app import create_app

UTC = timezone.utc
ADMIN = "owner@example.com"                    # hosted_config's admin_email
LEE, KIM = "lee.lead@example.com", "kim.lead@example.com"
STRANGER = "someone@example.com"               # Cloudflare let them in; the app doesn't know them
AVERY, BLAKE, CASEY, DANA = "avery@example.com", "blake@example.com", "casey@example.com", "dana@example.com"
NOBODY = "nobody@example.com"                  # no such tech anywhere
ERIN, FAY = "erin.park@example.com", "fay.hill@example.com"   # techs added in a test (any email is an agent here)
TEAM = [AVERY, BLAKE, CASEY, DANA]
ADMIN_TEAM = [AVERY, CASEY]                    # the admin's own team (hosted Team design §3)
NOT_FOUND = {"detail": "Not found."}
NOT_ADDED = {"error": "not_added", "email": STRANGER, "admin": ADMIN}
WEEK = {"period": "custom", "start": "2026-10-04", "end": "2026-10-10"}
ALL_REGIONS = [{"region": "NASA", "city": "Dallas"}, {"region": "EMEA", "city": "Dubai"},
               {"region": "APAC", "city": "Manila"}]   # `/api/me`'s, for Your regions
SHIFT = {"effective_from": "2026-10-18", "work_days": [1, 2, 3], "start": "09:00", "end": "17:00"}


def as_(email: str) -> dict:
    """The header Cloudflare Access puts on a request of `email`, signed in."""
    return {"Cf-Access-Jwt-Assertion": token(email)}


class FakeZendesk:
    def users_by_email(self, emails):
        return {e: {"id": i + 1, "email": e, "role": "agent"} for i, e in enumerate(emails)}

    def close(self):
        pass


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
        actions = []
        for n in range(count):   # 09:0n on Monday Oct 5 in Chicago, each on its own ticket
            actions.append(Action(email, 100 * count + n, datetime(2026, 10, 5, 14, n, tzinfo=UTC), "reply", None,
                                  None, event, 1))
            event += 1
        store.insert_actions(conn, actions)
        store.add_time_off(conn, email, date(2026, 10, 7), date(2026, 10, 7), "dentist", "pto")
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", ("2026-10-17T11:00:00Z",) * 3 + ("2026-09-01T05:00:00Z",))
    conn.commit()
    people.ensure_admin(conn, ADMIN)
    people.set_assignments(conn, ADMIN, ADMIN_TEAM)
    people.add_person(conn, LEE, "tl")
    people.set_assignments(conn, LEE, [AVERY, BLAKE])
    people.add_person(conn, KIM, "tl")
    people.set_assignments(conn, KIM, [BLAKE, CASEY])
    conn.close()
    return path


@pytest.fixture
def started():
    return []


def hosted_app(db_path, tmp_path, started, now=None):
    """The hosted app on the database at `db_path`; `now` pins its clock."""
    return create_app(db_path, config=hosted_config(tmp_path),
                      run_starter=lambda spec: started.append(spec) or 7, zendesk_factory=FakeZendesk,
                      log_dir=tmp_path / "logs", startup_work=False,
                      access_verifier=access.Verifier(DOMAIN, AUD, fetch=Certs()), now=now)


@pytest.fixture
def app(db_path, tmp_path, started):
    return hosted_app(db_path, tmp_path, started)


@pytest.fixture
def client_hosted(app):
    """No one signed in by default: each request says who it comes from (`as_`)."""
    return TestClient(app, base_url=LOCAL)


@pytest.fixture
def client_hosted_admin(app):
    return TestClient(app, base_url=LOCAL, headers=as_(ADMIN))


@pytest.fixture
def client_hosted_tl(app):
    """Lee, who leads Avery and Blake."""
    return TestClient(app, base_url=LOCAL, headers=as_(LEE))


@pytest.fixture
def client_full(db_path, tmp_path):
    app = create_app(db_path, run_starter=lambda spec: 1, zendesk_factory=FakeZendesk, log_dir=tmp_path / "logs",
                     startup_work=False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def emails(items) -> list[str]:
    return [item["email"] for item in items]


def time_off_id(db_path, email) -> int:
    conn = connect(db_path)
    try:
        return next(i.id for i in store.time_off(conn, email))
    finally:
        conn.close()


def add_reassignments(db_path) -> None:
    """Assignee changes, whose old and new values name techs (`classify._person`). On Monday Oct 5 Avery took ticket
    100 from Casey, passed it to Dana, took it from Blake and passed it to someone known only by Zendesk id; on
    Wednesday Oct 7 Blake took ticket 200 from Dana."""
    conn = connect(db_path)
    try:
        store.insert_actions(conn, [
            Action(AVERY, 100, datetime(2026, 10, 5, 14, 30, tzinfo=UTC), "assignee", CASEY, AVERY, 900, 1),
            Action(AVERY, 100, datetime(2026, 10, 5, 14, 31, tzinfo=UTC), "assignee", AVERY, DANA, 901, 1),
            Action(AVERY, 100, datetime(2026, 10, 5, 14, 32, tzinfo=UTC), "assignee", BLAKE, AVERY, 902, 1),
            Action(AVERY, 100, datetime(2026, 10, 5, 14, 33, tzinfo=UTC), "assignee", AVERY, "4242", 903, 1),
            Action(BLAKE, 200, datetime(2026, 10, 7, 15, 0, tzinfo=UTC), "assignee", DANA, BLAKE, 904, 1),
        ])
    finally:
        conn.close()


def tickets_per_shift(body) -> dict:
    measure = next(m for m in body["measures"] if m["key"] == "tickets_per_shift")
    return {"team": measure["team"]["now"], **{t["email"]: t["now"] for t in measure["techs"]}}


# -- the admin ----------------------------------------------------------------------------------------------


def test_admin_seeded_from_config_and_cannot_remove_self(client_hosted_admin, db_path):
    me = client_hosted_admin.get("/api/me")
    assert (me.status_code, me.json()) == (200, {"email": ADMIN, "role": "admin", "edition": "hosted", "name": None,
                                                 "regions": None, "all_regions": ALL_REGIONS})
    listed = client_hosted_admin.get("/api/people").json()
    assert listed["people"] == [{"email": ADMIN, "role": "admin", "name": None, "techs": ADMIN_TEAM},
                                {"email": KIM, "role": "tl", "name": None, "techs": [BLAKE, CASEY]},
                                {"email": LEE, "role": "tl", "name": None, "techs": [AVERY, BLAKE]}]
    assert [{k: r[k] for k in ("email", "name", "leads")} for r in listed["roster"]] == [
        {"email": AVERY, "name": AVERY, "leads": [ADMIN, LEE]},
        {"email": BLAKE, "name": BLAKE, "leads": [KIM, LEE]},
        {"email": CASEY, "name": CASEY, "leads": [ADMIN, KIM]},
        {"email": DANA, "name": DANA, "leads": []}], "the admin's own row is included"
    assert all("started" in r for r in listed["roster"]), "each tech's first tracked day, for the Techs panel"
    refused = client_hosted_admin.delete(f"/api/people/{ADMIN}")
    assert (refused.status_code, refused.json()) == (400, {"detail": "You can't remove yourself."})
    demoted = client_hosted_admin.post("/api/people", json={"email": "Owner@Example.com", "role": "tl"})
    assert (demoted.status_code, demoted.json()) == (400, {"detail": "You can't change your own role."})
    assert client_hosted_admin.post("/api/people", json={"email": ADMIN, "role": "admin"}).status_code == 200, \
        "naming their own role again changes nothing"
    assert client_hosted_admin.get("/api/me").json()["role"] == "admin"
    conn = connect(db_path)
    assert people.role(conn, ADMIN) == "admin"
    conn.close()


def test_admin_adds_assigns_and_removes_a_tl(client_hosted_admin, client_hosted):
    new = "pat.lead@example.com"
    assert client_hosted.get("/api/me", headers=as_(new)).status_code == 403
    added = client_hosted_admin.post("/api/people", json={"email": " Pat.Lead@Example.com", "role": "tl"})
    assert added.status_code == 200
    assert {"email": new, "role": "tl", "name": None, "techs": []} in added.json()["people"]
    assert client_hosted.get("/api/activity", headers=as_(new)).json()["techs"] == [], "no techs assigned yet"
    assigned = client_hosted_admin.put(f"/api/people/{new}/techs", json={"techs": [DANA, "Avery@Example.com"]})
    assert assigned.status_code == 200
    assert {"email": new, "role": "tl", "name": None, "techs": [AVERY, DANA]} in assigned.json()["people"]
    assert emails(client_hosted.get("/api/activity", headers=as_(new)).json()["techs"]) == [AVERY, DANA]
    removed = client_hosted_admin.delete(f"/api/people/{new}")
    assert removed.status_code == 200 and new not in emails(removed.json()["people"])
    assert client_hosted.get("/api/me", headers=as_(new)).status_code == 403


def test_people_api_checks_what_it_is_sent(client_hosted_admin):
    for body in ({"email": "not an email", "role": "tl"}, {"email": "x@example.com", "role": "boss"}, ["x"], {}):
        assert client_hosted_admin.post("/api/people", json=body).status_code == 400, body
    for body in ({"techs": [NOBODY]}, {"techs": "avery@example.com"}, {"techs": [1]}, {}):
        assert client_hosted_admin.put(f"/api/people/{LEE}/techs", json=body).status_code == 400, body
    missing = client_hosted_admin.put(f"/api/people/{NOBODY}/techs", json={"techs": []})
    assert (missing.status_code, missing.json()) == (404, {"detail": f"{NOBODY} hasn't been added."})
    assert client_hosted_admin.delete(f"/api/people/{NOBODY}").status_code == 404
    assert client_hosted_admin.get("/api/people").json()["people"][2]["techs"] == [AVERY, BLAKE], "nothing changed"


def test_admin_sees_only_own_team_on_every_team_route(client_hosted_admin, db_path):
    """The owner's decision (2026-10-21, hosted Team design §2-§3), replacing "the admin sees everyone": the admin is a
    team lead with extra Settings panels, so every Team activity address answers for their own team (Avery and
    Casey) only, and they edit their own techs' shifts, time off and sick/PTO marks."""
    grid = client_hosted_admin.get("/api/activity", params=WEEK).json()
    assert emails(grid["techs"]) == ADMIN_TEAM and emails(grid["rows"]) == ADMIN_TEAM
    trends = client_hosted_admin.get("/api/activity/trends", params=WEEK).json()
    assert emails(trends["techs"]) == ADMIN_TEAM
    assert all(emails(m["techs"]) == ADMIN_TEAM for m in trends["measures"])
    assert client_hosted_admin.get("/api/activity/day", params={"tech": CASEY, "day": "2026-10-05"}).json()["cell"][
        "tickets"] == 3
    assert emails(client_hosted_admin.get("/api/shifts").json()["techs"]) == ADMIN_TEAM
    assert sorted(emails(client_hosted_admin.get("/api/time-off").json()["items"])) == ADMIN_TEAM
    assert client_hosted_admin.get("/api/settings").json()["team"] == ADMIN_TEAM
    for tech in ADMIN_TEAM:
        assert client_hosted_admin.put(f"/api/shifts/{tech}", json=SHIFT).status_code == 200, tech
        added = client_hosted_admin.post("/api/time-off", json={"email": tech, "first": "2026-10-12", "last": "2026-10-12",
                                                            "kind": "sick"})
        assert added.status_code == 201, tech
        item = added.json()["id"]
        assert client_hosted_admin.patch(f"/api/time-off/{item}", json={"kind": "pto"}).status_code == 200, tech
        assert client_hosted_admin.delete("/api/time-off/day", params={"email": tech, "day": "2026-10-12"}).status_code \
            == 204, tech
        assert client_hosted_admin.delete(f"/api/time-off/{time_off_id(db_path, tech)}").status_code == 204, tech
    for path in ("/api/activity", f"/api/activity?{'&'.join(f'{k}={v}' for k, v in WEEK.items())}",
                 "/api/activity/trends", f"/api/activity/day?tech={AVERY}&day=2026-10-05", "/api/shifts",
                 "/api/time-off", "/api/settings"):
        text = client_hosted_admin.get(path).text
        assert BLAKE not in text and DANA not in text, path


def test_admin_guessing_other_leads_tech_gets_404(client_hosted_admin, db_path):
    """Review Focus 1: another lead's tech (Blake), a tech on no team (Dana) and a tech that doesn't exist all get the
    admin the same 404 a lead gets, on every Team activity address and on 1:1 prep's."""
    def answers(tech: str) -> list:
        calls = [
            client_hosted_admin.get("/api/activity", params={"tech": tech}),
            client_hosted_admin.get("/api/activity/trends", params={"tech": tech}),
            client_hosted_admin.get("/api/activity/day", params={"tech": tech, "day": "2026-10-05"}),
            client_hosted_admin.put(f"/api/shifts/{tech}", json=SHIFT),
            client_hosted_admin.post("/api/time-off", json={"email": tech, "first": "2026-10-12", "last": "2026-10-12"}),
            client_hosted_admin.delete("/api/time-off/day", params={"email": tech, "day": "2026-10-07"}),
            client_hosted_admin.get("/api/prep/tech", params={"email": tech}),
            client_hosted_admin.get("/api/prep/trends", params={"email": tech}),
        ]
        return [(r.status_code, r.json()) for r in calls]

    for tech in (BLAKE, DANA, NOBODY, BLAKE.upper()):
        assert answers(tech) == [(404, NOT_FOUND)] * 8, tech
    for tech in (BLAKE, DANA):
        item = time_off_id(db_path, tech)
        for response in (client_hosted_admin.patch(f"/api/time-off/{item}", json={"kind": "sick"}),
                         client_hosted_admin.delete(f"/api/time-off/{item}")):
            assert (response.status_code, response.json()) == (404, NOT_FOUND), tech
    for response in (client_hosted_admin.patch("/api/time-off/9999", json={"kind": "sick"}),
                     client_hosted_admin.delete("/api/time-off/9999")):
        assert (response.status_code, response.json()) == (404, NOT_FOUND), "an entry that doesn't exist: the same"
    conn = connect(db_path)
    try:
        assert [(o.email, o.kind) for o in store.time_off(conn)] == [(t, "pto") for t in TEAM], "nothing changed"
        assert all(len(store.shift_versions(conn, t)) == 1 for t in TEAM)
    finally:
        conn.close()
    prep = client_hosted_admin.get("/api/prep").json()
    assert emails(prep["rows"]) == ADMIN_TEAM, "1:1 prep's list holds only the admin's team"
    assert emails(client_hosted_admin.get("/api/prep/schedule").json()) == ADMIN_TEAM


def test_tech_on_no_team_hidden_everywhere_and_marked_in_roster(client_hosted_admin, client_hosted, started, db_path):
    """Review Focus 2: when the admin unticks a tech from every team (Blake, from Lee's and Kim's), the tech disappears
    from every Team screen for everyone, the admin included, shows with no leads in the roster (the Techs panel's "No
    team lead"), and is still synced (still on the roster the sync reads). Dana, on no team from the start, the
    same."""
    assert client_hosted_admin.put(f"/api/people/{LEE}/techs", json={"techs": [AVERY]}).status_code == 200
    body = client_hosted_admin.put(f"/api/people/{KIM}/techs", json={"techs": [CASEY]}).json()
    roster = {t["email"]: t["leads"] for t in body["roster"]}
    assert roster[BLAKE] == [] and roster[DANA] == []
    for viewer in (ADMIN, LEE, KIM):
        for tech in (BLAKE, DANA):
            grid = client_hosted.get("/api/activity", params=WEEK, headers=as_(viewer)).json()
            assert tech not in emails(grid["techs"]) and tech not in emails(grid["rows"]), (viewer, tech)
            refused = client_hosted.get("/api/activity/day", params={"tech": tech, "day": "2026-10-05"},
                                    headers=as_(viewer))
            assert (refused.status_code, refused.json()) == (404, NOT_FOUND), (viewer, tech)
            assert tech not in client_hosted.get("/api/settings", headers=as_(viewer)).text, (viewer, tech)
    assert client_hosted_admin.get("/api/settings").json()["team"] == ADMIN_TEAM
    conn = connect(db_path)
    try:
        assert settings.team_emails(conn) == TEAM, "still on the roster the sync reads"
    finally:
        conn.close()
    assert client_hosted_admin.post("/api/activity/sync", json={"kind": "refresh"}).status_code == 202
    assert started == [{"command": "activity-sync", "kind": "refresh"}], "the sync reads the whole roster"


def test_add_tech_with_lead_assigns_in_same_change(client_hosted_admin, client_hosted, monkeypatch):
    added = client_hosted_admin.post("/api/team", json={"name": "Erin Park", "email": ERIN, "lead": " Lee.Lead@Example.com"})
    assert added.status_code == 200, added.text
    assert added.json()["added"] == ERIN
    assert added.json()["team"] == ADMIN_TEAM, "the answer is the admin's settings: their own team"
    assert {t["email"]: t["leads"] for t in client_hosted_admin.get("/api/people").json()["roster"]}[ERIN] == [LEE]
    assert ERIN in emails(client_hosted.get("/api/activity", headers=as_(LEE)).json()["techs"])
    assert ERIN not in emails(client_hosted_admin.get("/api/activity").json()["techs"])

    def fail(conn, values):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(settings, "set_settings", fail)
    with pytest.raises(sqlite3.OperationalError):
        client_hosted_admin.post("/api/team", json={"name": "Fay Hill", "email": FAY, "lead": LEE})
    monkeypatch.undo()
    listed = client_hosted_admin.get("/api/people").json()
    assert FAY not in emails(listed["roster"])
    assert FAY not in next(p for p in listed["people"] if p["email"] == LEE)["techs"], \
        "the roster change failed, so Lee wasn't given the tech either: one change"


def test_add_tech_lead_defaults_to_admin(client_hosted_admin):
    added = client_hosted_admin.post("/api/team", json={"name": "Erin Park", "email": ERIN})
    assert added.status_code == 200, added.text
    assert added.json()["team"] == [AVERY, CASEY, ERIN], "the new tech is on the admin's own team"
    assert {t["email"]: t["leads"] for t in client_hosted_admin.get("/api/people").json()["roster"]}[ERIN] == [ADMIN]
    assert ERIN in emails(client_hosted_admin.get("/api/shifts").json()["techs"])
    blank = client_hosted_admin.post("/api/team", json={"name": "Fay Hill", "email": FAY, "lead": " "})
    assert blank.status_code == 200 and FAY in blank.json()["team"], "a blank lead is the admin too"


def test_add_tech_unknown_lead_refused(client_hosted_admin, client_hosted_tl):
    for lead, detail in ((NOBODY, f"{NOBODY} hasn't been added."), (STRANGER, f"{STRANGER} hasn't been added."),
                         (7, "The team lead must be an email.")):
        refused = client_hosted_admin.post("/api/team", json={"name": "Erin Park", "email": ERIN, "lead": lead})
        assert (refused.status_code, refused.json()) == (400, {"detail": detail}), lead
    listed = client_hosted_admin.get("/api/people").json()
    assert ERIN not in emails(listed["roster"]), "nothing is saved"
    refused = client_hosted_tl.post("/api/team", json={"name": "Erin Park", "email": ERIN, "lead": LEE})
    assert (refused.status_code, refused.json()) == (403, {"detail": "Only the admin can do this."})


def test_delete_team_removes_tech_from_every_team(client_hosted_admin, client_hosted_tl, db_path):
    refused = client_hosted_tl.delete(f"/api/team/{AVERY}")
    assert (refused.status_code, refused.json()) == (403, {"detail": "Only the admin can do this."})
    removed = client_hosted_admin.delete("/api/team/Avery@Example.com")
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] == AVERY and removed.json()["team"] == [CASEY]
    listed = client_hosted_admin.get("/api/people").json()
    assert emails(listed["roster"]) == [BLAKE, CASEY, DANA]
    assert {p["email"]: p["techs"] for p in listed["people"]} == {ADMIN: [CASEY], KIM: [BLAKE, CASEY], LEE: [BLAKE]}
    conn = connect(db_path)
    try:
        assert settings.team_emails(conn) == [BLAKE, CASEY, DANA]
        assert [v.active for v in store.shift_versions(conn, AVERY)] == [True, False], \
            "their days after tomorrow stop being theirs, as a team save does"
    finally:
        conn.close()
    again = client_hosted_admin.delete(f"/api/team/{AVERY}")
    assert (again.status_code, again.json()) == (404, {"detail": f"{AVERY} is not on the team."})
    assert client_hosted_admin.post("/api/team", json={"name": "Avery Stone", "email": AVERY}).status_code == 200
    assert {t["email"]: t["leads"] for t in client_hosted_admin.get("/api/people").json()["roster"]}[AVERY] == [ADMIN], \
        "added back, they join only the lead chosen now, never quietly their old leads' teams"


def test_edit_techs_replaces_team_with_overlap_allowed(client_hosted_admin, client_hosted):
    body = client_hosted_admin.put(f"/api/people/{LEE}/techs", json={"techs": [CASEY, DANA, AVERY]})
    assert body.status_code == 200
    assert {p["email"]: p["techs"] for p in body.json()["people"]} == {
        ADMIN: ADMIN_TEAM, KIM: [BLAKE, CASEY], LEE: [AVERY, CASEY, DANA]}, "Casey is on three teams now"
    assert {t["email"]: t["leads"] for t in body.json()["roster"]} == {
        AVERY: [ADMIN, LEE], BLAKE: [KIM], CASEY: [ADMIN, KIM, LEE], DANA: [LEE]}
    assert emails(client_hosted.get("/api/activity", headers=as_(LEE)).json()["techs"]) == [AVERY, CASEY, DANA]
    refused = client_hosted.get("/api/activity/day", params={"tech": BLAKE, "day": "2026-10-05"}, headers=as_(LEE))
    assert (refused.status_code, refused.json()) == (404, NOT_FOUND), "unticked: gone from Lee's screens"
    assert emails(client_hosted.get("/api/activity", headers=as_(KIM)).json()["techs"]) == [BLAKE, CASEY]


def test_admin_can_edit_own_team(client_hosted_admin):
    body = client_hosted_admin.put(f"/api/people/{ADMIN}/techs", json={"techs": [DANA, BLAKE]})
    assert body.status_code == 200
    assert body.json()["people"][0] == {"email": ADMIN, "role": "admin", "name": None, "techs": [BLAKE, DANA]}
    assert emails(client_hosted_admin.get("/api/activity").json()["techs"]) == [BLAKE, DANA]
    assert client_hosted_admin.get("/api/settings").json()["team"] == [BLAKE, DANA]
    refused = client_hosted_admin.get("/api/activity/day", params={"tech": AVERY, "day": "2026-10-05"})
    assert (refused.status_code, refused.json()) == (404, NOT_FOUND), "Avery is no longer on the admin's team"


def test_settings_team_is_own_team_for_admin(client_hosted_admin, client_hosted_tl, db_path):
    """`GET /api/settings`, and every settings answer the admin gets (a settings save, adding a tech), names only
    their own team in `team` and the 1:1 schedule. The general save refuses the schedule; the admin saves their own
    techs' through the scoped route, which changes only that tech's entry."""
    every = {email: {"date": "2026-10-20", "weeks": 2} for email in TEAM}
    conn = connect(db_path)
    try:
        settings.set_setting(conn, "oneonone_schedule", every)
    finally:
        conn.close()
    got = client_hosted_admin.get("/api/settings").json()
    assert got["team"] == ADMIN_TEAM
    assert got["oneonone_schedule"] == {AVERY: every[AVERY], CASEY: every[CASEY]}
    saved = client_hosted_admin.put("/api/settings", json={"activity_gap_minutes": 100}).json()
    assert saved["team"] == ADMIN_TEAM and set(saved["oneonone_schedule"]) == set(ADMIN_TEAM)
    mine = {AVERY: {"date": "2026-10-21", "weeks": 1}, BLAKE: {"date": "2026-10-22", "weeks": 3}}
    refused = client_hosted_admin.put("/api/settings", json={"oneonone_schedule": mine})
    assert (refused.status_code, refused.json()) == (400, {"detail": "Save 1:1 schedules from the 1:1 schedule panel."})
    assert client_hosted_admin.put(f"/api/prep/schedule/{AVERY}", json=mine[AVERY]).status_code == 200
    assert client_hosted_admin.put(f"/api/prep/schedule/{BLAKE}", json=mine[BLAKE]).status_code == 404, "Blake isn't theirs"
    conn = connect(db_path)
    try:
        assert settings.get_setting(conn, "oneonone_schedule") == {**every, AVERY: mine[AVERY]}, \
            "only the admin's own tech's entry changed"
    finally:
        conn.close()
    assert client_hosted_tl.get("/api/settings").json()["oneonone_schedule"] == {AVERY: mine[AVERY], BLAKE: every[BLAKE]}


def test_settings_save_cannot_change_the_roster(client_hosted_admin, db_path):
    """Ruling S7: the admin reads `team` as their own team only, so the general save refuses `team` on the hosted copy,
    even sent back unchanged; the roster changes only through `POST` and `DELETE /api/team`. Nothing in a refused save
    is kept, and every lead's techs stay theirs."""
    panel = {"detail": "Add and remove techs in the Techs panel."}
    for body in ({"team": ADMIN_TEAM}, {"team": [AVERY]}, {"team": ADMIN_TEAM, "activity_gap_minutes": 100}):
        refused = client_hosted_admin.put("/api/settings", json=body)
        assert (refused.status_code, refused.json()) == (400, panel), body
    assert client_hosted_admin.get("/api/settings").json()["activity_gap_minutes"] == 120, "nothing of it saved"
    listed = client_hosted_admin.get("/api/people").json()
    assert emails(listed["roster"]) == TEAM
    assert {p["email"]: p["techs"] for p in listed["people"]} == {ADMIN: ADMIN_TEAM, KIM: [BLAKE, CASEY],
                                                                    LEE: [AVERY, BLAKE]}
    conn = connect(db_path)
    try:
        assert settings.team_emails(conn) == TEAM
    finally:
        conn.close()


# -- someone the app doesn't know --------------------------------------------------------------------------


def test_unknown_signed_in_person_gets_not_added(client_hosted, started):
    for method, path in [("GET", "/api/settings"), ("GET", "/api/me"), ("GET", "/api/activity"),
                         ("GET", "/api/activity/trends"), ("GET", f"/api/activity/day?tech={AVERY}&day=2026-10-05"),
                         ("GET", "/api/shifts"), ("GET", "/api/time-off"), ("GET", "/api/people"),
                         ("PUT", "/api/settings"), ("POST", "/api/activity/sync"), ("PUT", f"/api/shifts/{AVERY}"),
                         ("POST", "/api/time-off"), ("POST", "/api/people"), ("GET", "/api/runs"),
                         ("GET", "/api/status")]:
        kwargs = {"json": {"kind": "refresh"}} if method in ("POST", "PUT") else {}
        response = client_hosted.request(method, path, headers=as_("Someone@Example.com"), **kwargs)
        assert (response.status_code, response.json()) == (403, NOT_ADDED), (method, path)
    assert started == [], "nothing was started"
    assert client_hosted.get("/", headers=as_(STRANGER)).status_code == 200, "the page itself loads and says so"


def test_removed_tl_gets_not_added_next_request(client_hosted):
    assert client_hosted.get("/api/activity", headers=as_(LEE)).status_code == 200
    assert client_hosted.delete(f"/api/people/{LEE}", headers=as_(ADMIN)).status_code == 200
    refused = client_hosted.get("/api/activity", headers=as_(LEE))
    assert (refused.status_code, refused.json()) == (403, {"error": "not_added", "email": LEE, "admin": ADMIN})
    assert emails(client_hosted.get("/api/activity", headers=as_(KIM)).json()["techs"]) == [BLAKE, CASEY], \
        "Blake stays Kim's"


# -- what a TL sees -------------------------------------------------------------------------------------------


def test_tl_sees_only_assigned_techs_in_activity_trends_day(client_hosted_tl):
    me = client_hosted_tl.get("/api/me").json()
    assert me == {"email": LEE, "role": "tl", "edition": "hosted", "name": None, "regions": None,
                  "all_regions": ALL_REGIONS}
    grid = client_hosted_tl.get("/api/activity", params=WEEK).json()
    assert emails(grid["techs"]) == [AVERY, BLAKE], "the Tech picker"
    assert emails(grid["rows"]) == [AVERY, BLAKE], "All techs"
    assert emails(client_hosted_tl.get("/api/activity", params={**WEEK, "tech": "Blake@Example.com"}).json()["rows"]) == \
        [BLAKE]
    trends = client_hosted_tl.get("/api/activity/trends", params=WEEK).json()
    assert emails(trends["techs"]) == [AVERY, BLAKE]
    assert all(emails(m["techs"]) == [AVERY, BLAKE] for m in trends["measures"])
    day = client_hosted_tl.get("/api/activity/day", params={"tech": AVERY, "day": "2026-10-05"})
    assert day.status_code == 200 and day.json()["cell"]["tickets"] == 1
    assert emails(client_hosted_tl.get("/api/shifts").json()["techs"]) == [AVERY, BLAKE]
    assert sorted(emails(client_hosted_tl.get("/api/time-off").json()["items"])) == [AVERY, BLAKE]
    assert client_hosted_tl.get("/api/settings").json()["team"] == [AVERY, BLAKE], "ruling R3"


def test_tl_settings_name_only_their_techs(client_hosted_tl, client_hosted_admin):
    """Ruling R3: a TL's settings carry only their techs in `team`; the admin's carry their own team's."""
    tl, admin = client_hosted_tl.get("/api/settings").json(), client_hosted_admin.get("/api/settings").json()
    assert tl["team"] == [AVERY, BLAKE] and admin["team"] == ADMIN_TEAM
    assert {k: v for k, v in tl.items() if k != "team"} == {k: v for k, v in admin.items() if k != "team"}
    text = json.dumps(tl)
    assert CASEY not in text and DANA not in text


def test_tl_settings_carry_only_their_techs_1_1_schedules(client_hosted_tl, client_hosted_admin, db_path):
    """The 1:1 schedule setting names techs, so a TL's settings keep only their techs' entries, as `team` does; the
    admin's keep only their own team's."""
    every = {email: {"date": "2026-10-20", "weeks": 2} for email in TEAM}
    conn = connect(db_path)
    try:
        settings.set_setting(conn, "oneonone_schedule", every)
    finally:
        conn.close()
    assert client_hosted_tl.get("/api/settings").json()["oneonone_schedule"] == {AVERY: every[AVERY], BLAKE: every[BLAKE]}
    assert client_hosted_admin.get("/api/settings").json()["oneonone_schedule"] == {AVERY: every[AVERY],
                                                                                CASEY: every[CASEY]}
    text = client_hosted_tl.get("/api/settings").text
    assert CASEY not in text and DANA not in text


def roster_entry(client, tech):
    return next(r for r in client.get("/api/people").json()["roster"] if r["email"] == tech)


def test_the_admin_edits_a_techs_name_start_and_leads_in_one_save(client_hosted_admin, db_path):
    response = client_hosted_admin.put(f"/api/team/{BLAKE}", json={"name": " Blake  Moreno ", "started": "2026-09-14",
                                                                    "leads": [ADMIN, KIM]})
    assert response.status_code == 200, response.text
    assert BLAKE in response.json()["team"], "the answer is the settings, with the admin's own team now holding Blake"
    entry = roster_entry(client_hosted_admin, BLAKE)
    assert (entry["name"], entry["started"], entry["leads"]) == ("Blake Moreno", "2026-09-14", [ADMIN, KIM])
    alone = client_hosted_admin.put(f"/api/team/{BLAKE}", json={"leads": []})
    assert alone.status_code == 200 and roster_entry(client_hosted_admin, BLAKE)["leads"] == []
    assert roster_entry(client_hosted_admin, BLAKE)["name"] == "Blake Moreno", "what isn't sent stays"


def test_the_admin_sets_a_techs_dated_regions(client_hosted_admin):
    """Regions design: a tech's regions are set in Techs' Edit, alone or with the other fields, as their whole dated
    history (the owner, 2026-10-21: a tech can swap regions); the roster carries the current region and the history.
    A tech never set lists as NASA with no history, and an empty history clears it."""
    roster = client_hosted_admin.get("/api/people").json()["roster"]
    assert [(r["region"], r["regions"]) for r in roster] == [("NASA", [])] * 4, "never set"
    swap = [{"since": "2026-10-12", "region": "NASA"}, {"since": "2026-06-01", "region": "APAC"}]
    response = client_hosted_admin.put(f"/api/team/{BLAKE}", json={"regions": swap})
    assert response.status_code == 200, response.text
    entry = roster_entry(client_hosted_admin, BLAKE)
    assert entry["region"] == "NASA", "the latest change's"
    assert entry["regions"] == [{"since": "2026-06-01", "region": "APAC"}, {"since": "2026-10-12", "region": "NASA"}]
    assert roster_entry(client_hosted_admin, AVERY)["regions"] == [], "only Blake's changed"
    both = client_hosted_admin.put(f"/api/team/{BLAKE}", json={"name": "Blake Moreno",
                                                                "regions": [{"since": "2026-06-01", "region": "EMEA"}]})
    assert both.status_code == 200, both.text
    entry = roster_entry(client_hosted_admin, BLAKE)
    assert (entry["name"], entry["region"], entry["regions"]) == (
        "Blake Moreno", "EMEA", [{"since": "2026-06-01", "region": "EMEA"}])
    assert client_hosted_admin.put(f"/api/team/{BLAKE}", json={"leads": [KIM]}).status_code == 200
    assert roster_entry(client_hosted_admin, BLAKE)["region"] == "EMEA", "what isn't sent stays"
    cleared = client_hosted_admin.put(f"/api/team/{BLAKE}", json={"regions": []})
    assert cleared.status_code == 200, cleared.text
    assert (roster_entry(client_hosted_admin, BLAKE)["region"], roster_entry(client_hosted_admin, BLAKE)["regions"]) \
        == ("NASA", [])


REGION_ERROR = "The region must be NASA, EMEA or APAC."
JUNE = {"since": "2026-06-01", "region": "APAC"}


@pytest.mark.parametrize("body, words", [
    ({"leads": [NOBODY]}, "isn't added"), ({"leads": "kim"}, "list"), ({"started": "2026-10-18"}, "after today"),
    ({"started": "soon"}, "YYYY-MM-DD"), ({"name": "Blake"}, "first and last"), ([], "object"),
    ({"regions": [{"since": "2026-06-01", "region": "LATAM"}]}, REGION_ERROR),
    ({"regions": [{"since": "2026-06-01", "region": ["APAC"]}]}, REGION_ERROR),
    ({"regions": [{"since": "June", "region": "APAC"}]}, "The region date must be a date in the form YYYY-MM-DD."),
    ({"regions": [{"region": "APAC"}]}, "The region date must be a date in the form YYYY-MM-DD."),
    ({"regions": [JUNE, {"since": "2026-06-01", "region": "EMEA"}]}, "Each region change needs its own date."),
    ({"regions": "APAC"}, "list"), ({"regions": ["APAC"]}, "list"),
    ({"name": "Blake Moreno", "started": "2026-09-14", "leads": [KIM],
      "regions": [{"since": "2026-06-01", "region": "LATAM"}]}, REGION_ERROR),
])
def test_a_bad_tech_edit_changes_nothing(client_hosted_admin, body, words):
    before = roster_entry(client_hosted_admin, BLAKE)
    response = client_hosted_admin.put(f"/api/team/{BLAKE}", json=body)
    assert response.status_code == 400 and words in response.json()["detail"], response.text
    assert roster_entry(client_hosted_admin, BLAKE) == before, "everything is checked before anything is saved"


def test_only_the_admin_edits_a_tech_and_only_one_on_the_roster(client_hosted_admin, client_hosted_tl, client_full):
    assert client_hosted_tl.put(f"/api/team/{AVERY}", json={"name": "Avery Cole"}).status_code == 403
    assert client_hosted_admin.put(f"/api/team/{NOBODY}", json={"name": "No Body"}).status_code == 404
    assert client_full.put(f"/api/team/{AVERY}", json={"name": "Avery Cole"}).status_code == 404, "the hosted copy's"


def test_only_the_admin_sets_start_dates_for_any_tech_on_the_roster(client_hosted_admin, client_hosted_tl):
    """Started is in the admin's Techs panel (the owner, 2026-10-21): the admin sets it for every tech, another lead's
    and one on no team too; a lead can't, not even for their own tech."""
    for tech in (AVERY, BLAKE, DANA):
        response = client_hosted_admin.put(f"/api/shifts/{tech}/start", json={"started": "2026-03-02"})
        assert response.status_code == 200, (tech, response.text)
    for tech in (AVERY, BLAKE, CASEY):
        response = client_hosted_tl.put(f"/api/shifts/{tech}/start", json={"started": "2026-03-02"})
        assert response.status_code == 403, tech
    assert client_hosted_admin.put(f"/api/shifts/{NOBODY}/start", json={"started": "2026-03-02"}).status_code == 400


def test_tl_guessing_other_tech_gets_404_everywhere(client_hosted_tl, db_path):
    """Review Focus 1: another TL's tech (Casey), a tech no TL leads (Dana) and a tech that doesn't exist all get the
    same 404, so a guessed address tells a TL nothing."""
    def answers(tech: str) -> list:
        calls = [
            client_hosted_tl.get("/api/activity", params={"tech": tech}),
            client_hosted_tl.get("/api/activity", params={**WEEK, "tech": tech}),
            client_hosted_tl.get("/api/activity/trends", params={"tech": tech}),
            client_hosted_tl.get("/api/activity/day", params={"tech": tech, "day": "2026-10-05"}),
            client_hosted_tl.put(f"/api/shifts/{tech}", json=SHIFT),
            client_hosted_tl.post("/api/time-off", json={"email": tech, "first": "2026-10-12", "last": "2026-10-12"}),
            client_hosted_tl.delete("/api/time-off/day", params={"email": tech, "day": "2026-10-07"}),
        ]
        return [(r.status_code, r.json()) for r in calls]

    expected = [(404, NOT_FOUND)] * 7
    for tech in (CASEY, DANA, NOBODY, CASEY.upper()):
        assert answers(tech) == expected, tech
    for tech in (CASEY, DANA):
        item = time_off_id(db_path, tech)
        for response in (client_hosted_tl.patch(f"/api/time-off/{item}", json={"kind": "sick"}),
                         client_hosted_tl.delete(f"/api/time-off/{item}")):
            assert (response.status_code, response.json()) == (404, NOT_FOUND), tech
    for response in (client_hosted_tl.patch("/api/time-off/9999", json={"kind": "sick"}),
                     client_hosted_tl.delete("/api/time-off/9999")):
        assert (response.status_code, response.json()) == (404, NOT_FOUND), "an entry that doesn't exist: the same"
    conn = connect(db_path)
    try:
        assert [(o.email, o.kind) for o in store.time_off(conn)] == [(t, "pto") for t in TEAM], "nothing changed"
        assert all(len(store.shift_versions(conn, t)) == 1 for t in TEAM)
    finally:
        conn.close()


def test_tl_edits_their_own_techs_shifts_and_time_off(client_hosted_tl, db_path):
    """[builder's call] in the spec: TLs edit their own techs' shifts and time off, including sick and PTO marks."""
    assert client_hosted_tl.put(f"/api/shifts/{AVERY}", json=SHIFT).status_code == 200
    added = client_hosted_tl.post("/api/time-off", json={"email": BLAKE, "first": "2026-10-12", "last": "2026-10-13",
                                                     "kind": "sick"})
    assert added.status_code == 201
    assert client_hosted_tl.patch(f"/api/time-off/{added.json()['id']}", json={"kind": "pto"}).status_code == 200
    assert client_hosted_tl.delete("/api/time-off/day", params={"email": BLAKE, "day": "2026-10-12"}).status_code == 204
    assert client_hosted_tl.delete(f"/api/time-off/{time_off_id(db_path, AVERY)}").status_code == 204
    missing = client_hosted_tl.delete("/api/time-off/day", params={"email": AVERY, "day": "2026-10-20"})
    assert (missing.status_code, missing.json()) == (404, {"detail": f"{AVERY} has no time off on 2026-10-20."}), \
        "their own tech keeps the usual answer"


def test_tech_assigned_to_two_tls_visible_to_both(client_hosted):
    """Review Focus 3: Blake is Lee's and Kim's."""
    for lead, theirs in ((LEE, [AVERY, BLAKE]), (KIM, [BLAKE, CASEY])):
        body = client_hosted.get("/api/activity", params=WEEK, headers=as_(lead)).json()
        assert emails(body["techs"]) == theirs and emails(body["rows"]) == theirs, lead
        day = client_hosted.get("/api/activity/day", params={"tech": BLAKE, "day": "2026-10-05"}, headers=as_(lead))
        assert day.status_code == 200 and day.json()["cell"]["tickets"] == 2, lead
        assert client_hosted.put(f"/api/shifts/{BLAKE}", json=SHIFT, headers=as_(lead)).status_code == 200, lead


def assigned(client) -> dict[str, list[str]]:
    return {p["email"]: p["techs"] for p in client.get("/api/people").json()["people"] if p["role"] == "tl"}


def test_a_tech_taken_off_the_team_is_taken_off_every_tl(client_hosted_admin, client_hosted):
    """Final review F7 (anti-goal: a TL seeing a tech who isn't theirs): removing Blake from the roster drops him from
    Lee's and Kim's techs, so adding him back later shows him to neither, only to the lead chosen then. The roster
    changes through `DELETE` and `POST /api/team`: the general settings save refuses `team` on the hosted copy
    (ruling S7, test_settings_save_cannot_change_the_roster)."""
    assert client_hosted_admin.delete(f"/api/team/{BLAKE}").status_code == 200
    assert assigned(client_hosted_admin) == {KIM: [CASEY], LEE: [AVERY]}
    added = client_hosted_admin.post("/api/team", json={"name": "Blake Moss", "email": BLAKE, "lead": ADMIN})
    assert added.status_code == 200, added.text
    assert assigned(client_hosted_admin) == {KIM: [CASEY], LEE: [AVERY]}, "his old TLs aren't quietly given him back"
    for lead in (LEE, KIM):
        assert BLAKE not in emails(client_hosted.get("/api/activity", headers=as_(lead)).json()["techs"]), lead
        refused = client_hosted.get("/api/activity/day", params={"tech": BLAKE, "day": "2026-10-05"}, headers=as_(lead))
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND), lead
    assert {t["email"]: t["leads"] for t in client_hosted_admin.get("/api/people").json()["roster"]}[BLAKE] == [ADMIN], \
        "the Techs panel shows only the lead chosen now"
    assert client_hosted_admin.put("/api/settings", json={"retention_days": 60}).status_code == 200
    assert assigned(client_hosted_admin) == {KIM: [CASEY], LEE: [AVERY]}, "a change that keeps the team keeps them"


def test_a_team_change_that_fails_keeps_every_tl_s_techs(client_hosted_admin, monkeypatch):
    """F7's change is one transaction with the team's: if taking a tech off the roster fails, nobody loses a tech."""
    def fail(conn, values):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(settings, "set_settings", fail)
    with pytest.raises(sqlite3.OperationalError):
        client_hosted_admin.delete(f"/api/team/{BLAKE}")
    monkeypatch.undo()
    assert assigned(client_hosted_admin) == {KIM: [BLAKE, CASEY], LEE: [AVERY, BLAKE]}


def test_tl_trends_team_average_over_their_techs_only(client_hosted_tl, client_hosted_admin):
    """Review Focus 5: the team average and the ranking cover the viewer's techs, never the whole roster, the admin's
    included. Tickets per shift on the one worked shift: Avery 1, Blake 2, Casey 3, Dana 4."""
    admin = tickets_per_shift(client_hosted_admin.get("/api/activity/trends", params=WEEK).json())
    assert admin == {"team": 2, AVERY: 1, CASEY: 3}
    lee = client_hosted_tl.get("/api/activity/trends", params=WEEK).json()
    assert tickets_per_shift(lee) == {"team": 1.5, AVERY: 1, BLAKE: 2}
    for m in lee["measures"]:
        assert emails(m["techs"]) == [AVERY, BLAKE], m["key"]
    picked = client_hosted_tl.get("/api/activity/trends", params={**WEEK, "tech": BLAKE}).json()
    assert picked["tech"] == BLAKE and tickets_per_shift(picked)["team"] == 1.5
    assert (picked["team"], emails(picked["techs"])) == (LEE, [AVERY, BLAKE]), "Blake with Lee's team"


def test_one_tech_is_compared_with_their_team_leads_team(db_path):
    """Regions design, "Team average in Trends" (the owner's revision, T-144): the team one tech is compared with, as
    its lead's email. A team lead's is their own team; the admin's is their own team when the tech is on it, else the
    team of the tech's lead, the first by the lead's name (typed, else their email; in any case) when there are
    several. A tech on no team, and every tech on the PC (no sign-in), has none: they are compared with everyone the
    viewer sees. The admin sees only their own team, so the route reaches the admin's other cases only through
    `team=`; this pins the rule itself."""
    conn = connect(db_path)
    assert people.comparison_team(conn, LEE, BLAKE) == LEE, "a team lead: their own team"
    assert people.comparison_team(conn, ADMIN, CASEY) == ADMIN, "the admin's own team, though Casey is Kim's too"
    people.set_leads(conn, DANA, [LEE])
    assert people.comparison_team(conn, ADMIN, DANA) == LEE, "another lead's tech: that lead's team"
    assert people.comparison_team(conn, ADMIN, BLAKE) == KIM, "on two other teams: kim.lead@ is before lee.lead@"
    people.add_person(conn, KIM, "tl", "Zoe Kim")
    assert people.comparison_team(conn, ADMIN, BLAKE) == LEE, "lee.lead@example.com is before Zoe Kim"
    people.add_person(conn, LEE, "tl", "zed Lee")
    assert people.comparison_team(conn, ADMIN, BLAKE) == LEE, "zed Lee is before Zoe Kim, in any case"
    people.set_leads(conn, DANA, [])
    assert people.comparison_team(conn, ADMIN, DANA) is None, "on no team"
    assert people.comparison_team(conn, LEE, DANA) is None, "on no team"
    assert people.comparison_team(conn, None, BLAKE) is None, "the PC"
    conn.close()


def test_trends_for_one_tech_or_one_team_lists_that_team_among_the_viewers_techs(client_hosted_tl,
                                                                                client_hosted_admin, client_full):
    """T-144: one tech's answer lists, and averages, their comparison team's techs among the viewer's and names the
    team's lead as `team`. team=LEAD (in any case) asks for that team's techs among the viewer's, as the page does for
    every tech on it. A team lead sees only their own team, the admin every lead's; another lead's, anyone who isn't a
    person, and any team on the PC are refused. All techs and a region name no team, and a region asked for with a tech
    wins. Tickets per shift on the one worked shift: Avery 1, Blake 2, Casey 3, Dana 4."""
    def read(client, **params) -> tuple:
        response = client.get("/api/activity/trends", params={**WEEK, **params})
        assert response.status_code == 200, params
        body = response.json()
        return body["tech"], body["team"], emails(body["techs"]), tickets_per_shift(body)["team"]

    assert read(client_hosted_tl, tech=BLAKE) == (BLAKE, LEE, [AVERY, BLAKE], 1.5)
    assert read(client_hosted_tl, team=LEE) == read(client_hosted_tl, team=" Lee.Lead@Example.com ") == (
        None, LEE, [AVERY, BLAKE], 1.5)
    assert read(client_hosted_tl) == (None, None, [AVERY, BLAKE], 1.5)
    assert read(client_hosted_admin, tech=CASEY) == (CASEY, ADMIN, [AVERY, CASEY], 2), "the admin's own team"
    assert read(client_hosted_admin, team=KIM) == (None, KIM, [CASEY], 3), "Kim's techs among the admin's"
    assert read(client_hosted_admin, team=LEE) == (None, LEE, [AVERY], 1)
    assert read(client_full, tech=BLAKE) == (BLAKE, None, TEAM, 2.5), "the PC: every tech"
    assert read(client_full, tech=BLAKE, region="NASA") == (BLAKE, None, TEAM, 2.5)
    for client, team in ((client_hosted_tl, KIM), (client_hosted_tl, ADMIN), (client_hosted_admin, NOBODY),
                         (client_hosted_admin, AVERY), (client_full, LEE)):
        refused = client.get("/api/activity/trends", params={**WEEK, "team": team})
        assert (refused.status_code, refused.json()["detail"]) == (400, "Choose a team you can see."), team


def test_tl_trends_by_region_keeps_to_their_techs(db_path, tmp_path, started):
    """Regions design: a region is its techs among the viewer's, and one tech is compared with their team lead's team
    (T-144: for a lead, their own), whose average the team's figures are. Lee leads Avery (NASA), Blake and Dana
    (APAC); Casey, in APAC too, is not Lee's and never shows. EMEA, where Lee has no one, is no techs and no figures.
    Tickets per shift on the one worked shift: Avery 1, Blake 2, Dana 4. The app's clock is pinned, so the APAC techs'
    today doesn't move with the real calendar."""
    conn = connect(db_path)
    people.set_assignments(conn, LEE, [AVERY, BLAKE, DANA])
    for tech in (BLAKE, CASEY, DANA):
        regions.set_regions(conn, tech, [(date(2026, 1, 1), "APAC")])
    conn.close()
    lee = TestClient(hosted_app(db_path, tmp_path, started, now=datetime(2026, 10, 17, 16, tzinfo=UTC)),
                     base_url=LOCAL, headers=as_(LEE))

    def read(**params) -> dict:
        response = lee.get("/api/activity/trends", params={**WEEK, **params})
        assert response.status_code == 200 and CASEY not in response.text, params
        return response.json()

    everyone = read()
    assert emails(everyone["techs"]) == [AVERY, BLAKE, DANA] and tickets_per_shift(everyone)["team"] == 2.3
    apac = read(region="APAC")
    assert [(t["email"], t["region"]) for t in apac["techs"]] == [(BLAKE, "APAC"), (DANA, "APAC")]
    assert tickets_per_shift(apac) == {"team": 3, BLAKE: 2, DANA: 4}
    assert all(emails(m["techs"]) == [BLAKE, DANA] for m in apac["measures"])
    assert read(region="apac") == apac and apac["team"] is None
    assert read(tech=BLAKE) == {**everyone, "tech": BLAKE, "team": LEE}, "Blake with Lee's team and their average"
    assert read(tech=AVERY) == {**everyone, "tech": AVERY, "team": LEE}
    assert read(tech=BLAKE, region="APAC") == {**apac, "tech": BLAKE}, "a region asked for wins"
    emea = read(region="EMEA")
    assert emea["techs"] == []
    assert all(m["techs"] == [] and m["team"] == {"now": None, "before": None, "change": None, "steps": [None],
                                                  "steps_n": [0], "rolling": [None], "rolling_n": [0]}
               for m in emea["measures"])


def test_the_picker_holds_every_tech_the_viewer_sees_whatever_is_asked(db_path, tmp_path, started):
    """Ruling 9 (Regions): a region or a tech asked for narrows an answer's techs to one region's, so Activity's and
    Trends' answers both carry the Tech picker's own list: every tech the viewer sees, as the answer for All techs lists
    them, each with their current region. Lee leads Avery (NASA), Blake and Dana (APAC); Casey, in APAC too, is not
    Lee's and is in no answer of Lee's. The admin's own team is Avery and Casey."""
    conn = connect(db_path)
    people.set_assignments(conn, LEE, [AVERY, BLAKE, DANA])
    for tech in (BLAKE, CASEY, DANA):
        regions.set_regions(conn, tech, [(date(2026, 1, 1), "APAC")])
    conn.close()
    app = hosted_app(db_path, tmp_path, started, now=datetime(2026, 10, 17, 16, tzinfo=UTC))
    lee = TestClient(app, base_url=LOCAL, headers=as_(LEE))
    admin = TestClient(app, base_url=LOCAL, headers=as_(ADMIN))

    def picker(client, route, **params) -> list[tuple[str, str]]:
        response = client.get(route, params={**WEEK, **params})
        assert response.status_code == 200, (route, params)
        assert client is admin or CASEY not in response.text, (route, params)
        return [(t["email"], t["region"]) for t in response.json()["picker"]]

    for route in ("/api/activity", "/api/activity/trends"):
        everyone = lee.get(route, params=WEEK).json()
        assert everyone["picker"] == [{"email": t["email"], "name": t["name"], "region": t["region"]}
                                      for t in everyone["techs"]], route
        for asked in ({}, {"region": "APAC"}, {"region": "nasa"}, {"region": "EMEA"}, {"tech": BLAKE},
                      {"tech": AVERY}):
            assert picker(lee, route, **asked) == [(AVERY, "NASA"), (BLAKE, "APAC"), (DANA, "APAC")], (route, asked)
        assert picker(admin, route, region="NASA") == [(AVERY, "NASA"), (CASEY, "APAC")], route


def test_tl_cannot_change_settings_or_people(client_hosted_tl, client_hosted_admin):
    admin_only = {"detail": "Only the admin can do this."}
    for method, path, body in [("PUT", "/api/settings", {"activity_gap_minutes": 100}),
                               ("PUT", "/api/settings", {"team": [AVERY]}),
                               ("GET", "/api/people", None),
                               ("POST", "/api/people", {"email": "pat.lead@example.com", "role": "tl"}),
                               ("POST", "/api/people", {"email": LEE, "role": "admin"}),
                               ("DELETE", f"/api/people/{KIM}", None),
                               ("PUT", f"/api/people/{LEE}/techs", {"techs": TEAM}),
                               ("POST", "/api/team", {"name": "Erin Park", "email": ERIN, "lead": LEE}),
                               ("DELETE", f"/api/team/{AVERY}", None),
                               ("POST", "/api/activity/sync", {"kind": "backfill"})]:
        response = client_hosted_tl.request(method, path, json=body)
        assert (response.status_code, response.json()) == (403, admin_only), (method, path)
    after = client_hosted_admin.get("/api/settings").json()
    assert after["activity_gap_minutes"] == 120
    listed = client_hosted_admin.get("/api/people").json()
    assert emails(listed["roster"]) == TEAM
    assert listed["people"][1:] == [
        {"email": KIM, "role": "tl", "name": None, "techs": [BLAKE, CASEY]},
        {"email": LEE, "role": "tl", "name": None, "techs": [AVERY, BLAKE]}]


def test_backfill_is_the_admins_and_refresh_is_everyones(client_hosted_tl, client_hosted_admin, started):
    """Controller ruling S2: a backfill, the long read back through the retention period, is the admin's only; Refresh
    stays open to every known person."""
    refused = client_hosted_tl.post("/api/activity/sync", json={"kind": "backfill"})
    assert (refused.status_code, refused.json()) == (403, {"detail": "Only the admin can do this."})
    assert started == [], "nothing started"
    assert client_hosted_tl.post("/api/activity/sync", json={"kind": "refresh"}).status_code == 202, \
        "Refresh is open to every known person"
    assert client_hosted_admin.post("/api/activity/sync", json={"kind": "backfill"}).status_code == 202
    assert [s["kind"] for s in started] == ["refresh", "backfill"]


# -- Import from Teams Shifts (Shifts import design): the hosted admin's only -------------------------------------

PREVIEW, APPLY = "/api/shifts-import/preview", "/api/shifts-import/apply"
CHOOSE_FILES = "Choose the Teams Shifts export files."


def encoded(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode("ascii")}


def avery_files() -> dict:
    """An export of Avery's Mon-Fri weeks from Sunday Oct 4 to Saturday Oct 17, Dallas 8 to 5."""
    avery = ("Avery Stone", AVERY)
    rows = [*week(avery, NASA_TEAM, date(2026, 10, 4), "08:00", "17:00"),
            *week(avery, NASA_TEAM, date(2026, 10, 11), "08:00", "17:00")]
    return {"files": [encoded("october.xlsx", workbook(rows))]}


def import_tables(db_path) -> dict:
    """Every table an import could write, and the settings."""
    conn = connect(db_path)
    try:
        return {table: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))
                for table in ("shift_versions", "time_off", "tech_regions", "settings")}
    finally:
        conn.close()


def backups_beside(tmp_path) -> list:
    return sorted(tmp_path.glob("work-assistant.db.before-shifts-import-*.db"))


def test_the_admin_previews_a_shifts_import_and_nothing_is_written(client_hosted_admin, db_path, tmp_path):
    before = import_tables(db_path)
    response = client_hosted_admin.post(PREVIEW, json=avery_files())
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["first"], body["last"], body["skipped"], body["problems"]) == ("2026-10-04", "2026-10-17", [], [])
    [avery] = body["techs"]
    assert (avery["email"], avery["regions"]) == (AVERY, [{"since": "2026-10-04", "region": "NASA"}])
    assert avery["changes"] == [{"since": "2026-10-04", "days": [1, 2, 3, 4, 5], "start": "08:00", "end": "17:00"}], \
        "the files begin after the stored activity (Sep 1), so they keep her entry before them (Ruling 18)"
    assert (avery["replace_before"], avery["started"]) == (False, None)
    assert import_tables(db_path) == before and backups_beside(tmp_path) == [], "a preview writes nothing"


def test_apply_backs_up_the_database_before_writing(client_hosted_admin, db_path, tmp_path):
    preview = client_hosted_admin.post(PREVIEW, json=avery_files()).json()
    response = client_hosted_admin.post(APPLY, json=avery_files())
    assert response.status_code == 200, response.text
    [backup] = backups_beside(tmp_path)
    assert response.json() == {"backup": str(backup), "techs": 1, "entries": 1, "time_off": 0, "preview": preview}
    sun_thu = ShiftVersion(AVERY, date(2026, 9, 1), frozenset({0, 1, 2, 3, 4}), 480, 1020, True)
    as_it_was = [(backup, [sun_thu], [])]   # the copy: Avery's entry and regions from before
    imported = [sun_thu, ShiftVersion(AVERY, date(2026, 10, 4), frozenset({1, 2, 3, 4, 5}), 480, 1020, True)]
    for path, versions, history in [*as_it_was, (db_path, imported, [(date(2026, 10, 4), "NASA")])]:
        conn = connect(path)
        try:
            assert store.shift_versions(conn, AVERY) == versions, path
            assert regions.region_history(conn, AVERY) == history, path
        finally:
            conn.close()


def test_only_the_admin_imports_from_shifts_and_only_on_the_hosted_copy(client_hosted_tl, client_full, db_path,
                                                                        tmp_path):
    before = import_tables(db_path)
    for path in (PREVIEW, APPLY):
        refused = client_hosted_tl.post(path, json=avery_files())
        assert (refused.status_code, refused.json()) == (403, {"detail": "Only the admin can do this."}), path
        missing = client_full.post(path, json=avery_files())
        assert (missing.status_code, missing.json()) == (404, NOT_FOUND), path
    assert import_tables(db_path) == before and backups_beside(tmp_path) == []


NOT_AN_EXPORT = "notes.xlsx isn't a Teams Shifts export (no Shifts sheet)."


@pytest.mark.parametrize("body, detail", [
    ({}, CHOOSE_FILES), ({"files": []}, CHOOSE_FILES), ({"files": "october.xlsx"}, CHOOSE_FILES),
    ({"files": [{"name": "october.xlsx"}]}, CHOOSE_FILES), ([], CHOOSE_FILES),
    ({"files": [encoded("notes.xlsx", b"not a workbook")]}, NOT_AN_EXPORT),
    ({"files": [{"name": "notes.xlsx", "data": "not base64!"}]}, NOT_AN_EXPORT),
    ({"files": [encoded("notes.xlsx", workbook(sheets=("Time Off",)))]}, NOT_AN_EXPORT),
    ({"files": [*avery_files()["files"], encoded("notes.xlsx", b"not a workbook")]}, NOT_AN_EXPORT),
    ({"files": [encoded("empty.xlsx", workbook())]}, "The files have no shifts to import."),
])
def test_a_bad_shifts_import_is_refused_and_writes_nothing(client_hosted_admin, db_path, tmp_path, body, detail):
    before = import_tables(db_path)
    for path in (PREVIEW, APPLY):
        response = client_hosted_admin.post(path, json=body)
        assert (response.status_code, response.json()) == (400, {"detail": detail}), path
    assert import_tables(db_path) == before and backups_beside(tmp_path) == [], "refused before any backup or write"


# -- every route --------------------------------------------------------------------------------------------

# Each route the hosted edition keeps (test_web_edition.KEPT_ROUTES plus this task's), by how it keeps a person
# to their own team. Scoped: limited to `visible_techs`, and another tech is 404, for the admin too (each write of a
# shift, time off or a 1:1 date also refuses a manager: test_web_managers). Admin only: a TL and a manager get 403.
# Admin or manager: a TL gets 403, and a manager is limited to their regions (test_web_managers). Names no tech: the
# answer is about the request itself (a sync's backfill is the admin's only; a manager picks their own regions).
SCOPED = {
    ("GET", "/api/settings"),
    ("GET", "/api/activity"), ("GET", "/api/activity/trends"), ("GET", "/api/activity/day"),
    ("GET", "/api/shifts"), ("PUT", "/api/shifts/{email}"),
    ("GET", "/api/time-off"), ("POST", "/api/time-off"), ("PATCH", "/api/time-off/{item_id}"),
    ("DELETE", "/api/time-off/day"), ("DELETE", "/api/time-off/{item_id}"),
    # 1:1 prep: each lead's own techs, and the admin's own (test_web_oneonone_scoping)
    ("GET", "/api/prep"), ("GET", "/api/prep/tech"), ("GET", "/api/prep/trends"), ("GET", "/api/prep/schedule"),
    ("PUT", "/api/prep/schedule/{email}"),
}
ADMIN_ONLY = {("PUT", "/api/settings"), ("POST", "/api/shifts-import/preview"), ("POST", "/api/shifts-import/apply"),
              ("PUT", "/api/people/{email}/regions")}
ADMIN_OR_MANAGER = {
    ("PUT", "/api/shifts/{email}/start"), ("PUT", "/api/team/{email}"), ("POST", "/api/team"),
    ("DELETE", "/api/team/{email}"),
    ("GET", "/api/people"), ("POST", "/api/people"), ("DELETE", "/api/people/{email}"),
    ("PUT", "/api/people/{email}/techs"),
}
NAMES_NO_TECH = {("POST", "/api/activity/sync"), ("GET", "/api/me"), ("PUT", "/api/me/regions")}


def test_every_kept_route_is_scoped_admin_only_or_names_no_tech(app):
    routes = [route for route in app.routes if isinstance(route, APIRoute) and route.path.startswith("/api/")]
    every = {(method, route.path) for route in routes for method in route.methods}
    call_qa = {(method, route.path) for method, path in CALL_QA_ROUTES for route in routes
               if method in route.methods and route.path_regex.fullmatch(path)}
    kept = every - call_qa
    assert kept == KEPT_ROUTES
    groups = (SCOPED, ADMIN_ONLY, ADMIN_OR_MANAGER, NAMES_NO_TECH)
    assert set().union(*groups) == kept, f"unplaced: {sorted(kept - set().union(*groups))}"
    assert sum(map(len, groups)) == len(kept), "each route in one group only"


def test_tl_day_view_hides_assignees_outside_their_techs(client_hosted_tl, client_hosted_admin, client_full, db_path):
    """A reassignment's old and new values name techs, so a TL's day view gives null for each that isn't one of
    theirs, or is known only by Zendesk id; the page still reads "changed the assignee", and "took the ticket" still
    matches the day's own tech. The admin's day view does the same for their own team (Avery and Casey); the PC's
    keeps every value."""
    add_reassignments(db_path)

    def assignees(client) -> list:
        body = client.get("/api/activity/day", params={"tech": AVERY, "day": "2026-10-05"}).json()
        return [(a["old"], a["new"]) for a in body["actions"] if a["kind"] == "assignee"]

    assert assignees(client_hosted_tl) == [(None, AVERY), (AVERY, None), (BLAKE, AVERY), (AVERY, None)]
    assert assignees(client_hosted_admin) == [(CASEY, AVERY), (AVERY, None), (None, AVERY), (AVERY, None)]
    assert assignees(client_full) == [(CASEY, AVERY), (AVERY, DANA), (BLAKE, AVERY), (AVERY, "4242")]


def test_no_read_a_tl_can_make_names_another_tech(client_hosted_tl, db_path):
    """Every GET a TL can make, with and without a tech of theirs, names only Avery and Blake, though their own
    reassignments name Casey and Dana."""
    add_reassignments(db_path)
    reads =["/api/settings", "/api/me", "/api/activity", "/api/activity?" + "&".join(f"{k}={v}" for k, v in WEEK.items()),
             f"/api/activity?tech={AVERY}", "/api/activity/trends", f"/api/activity/trends?tech={BLAKE}",
             f"/api/activity/day?tech={AVERY}&day=2026-10-05", f"/api/activity/day?tech={BLAKE}&day=2026-10-07",
             "/api/shifts", "/api/time-off", "/api/prep",
             f"/api/prep/trends?email={BLAKE}", "/api/prep/schedule"]
    assert {path.split("?")[0] for path in reads} | {"/api/prep/tech"} == {path for method, path in SCOPED | NAMES_NO_TECH
                                                      if method == "GET"}
    for path in reads:
        response = client_hosted_tl.get(path)
        assert response.status_code == 200, path
        assert CASEY not in response.text and DANA not in response.text, path


# -- the owner's PC copy --------------------------------------------------------------------------------------


def test_pc_copy_unscoped(client_full, db_path):
    """The owner's PC copy has no sign-in: everyone is the admin and sees every tech, though this database has people
    and teams in it."""
    me = client_full.get("/api/me")
    assert (me.status_code, me.json()) == (200, {"email": None, "role": "admin", "edition": "full"})
    assert emails(client_full.get("/api/activity").json()["techs"]) == TEAM
    assert emails(client_full.get("/api/activity/trends", params=WEEK).json()["techs"]) == TEAM
    assert client_full.get("/api/settings").json()["team"] == TEAM
    assert emails(client_full.get("/api/prep").json()["rows"]) == TEAM
    assert emails(client_full.get("/api/prep/schedule").json()) == TEAM
    assert client_full.get("/api/activity/day", params={"tech": DANA, "day": "2026-10-05"}).status_code == 200
    assert client_full.put("/api/settings", json={"activity_gap_minutes": 100}).status_code == 200
    assert client_full.put(f"/api/shifts/{DANA}", json=SHIFT).status_code == 200
    gone = client_full.patch("/api/time-off/9999", json={"kind": "pto"})
    assert (gone.status_code, gone.json()) == (404, {"detail": "There is no time off numbered 9999."})
    for method, path in (("GET", "/api/people"), ("POST", "/api/people"), ("DELETE", f"/api/people/{LEE}"),
                         ("PUT", f"/api/people/{LEE}/techs"), ("DELETE", f"/api/team/{DANA}")):
        response = client_full.request(method, path, json={"email": "x@example.com", "role": "tl", "techs": []})
        assert (response.status_code, response.json()) == (404, NOT_FOUND), (method, path)
    assert client_full.get("/api/settings").json()["team"] == TEAM, "the hosted-only Remove took no one off"
    saved = client_full.put("/api/settings", json={"team": [AVERY, BLAKE, CASEY]})
    assert saved.status_code == 200, "the PC's Team panel still saves its whole team here"
    assert saved.json()["team"] == [AVERY, BLAKE, CASEY]


def test_pc_copy_never_seeds_people(tmp_path):
    path = tmp_path / "pc.db"
    create_app(path, run_starter=lambda spec: 1, log_dir=tmp_path / "logs", startup_work=False)
    conn = connect(path)
    try:
        assert people.everyone(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_rejected_token_names_the_servers_set_secret(db_path, tmp_path, started):
    """T-169: when Zendesk rejects the token as the admin adds a tech, the hosted copy says to save a new one with
    `set-secret` and the server's config (`create_app(config_path=...)`), never the PC's `setup`."""
    from work_assistant import pipeline
    from work_assistant.zendesk.client import ZendeskAuthError

    class Rejecting(FakeZendesk):
        def users_by_email(self, emails):
            raise ZendeskAuthError("Zendesk rejected the credentials (HTTP 401) for /api/v2/users/show_many")

    config = tmp_path / "config.toml"
    app = create_app(db_path, config=hosted_config(tmp_path), config_path=config,
                     run_starter=lambda spec: started.append(spec) or 7, zendesk_factory=Rejecting,
                     log_dir=tmp_path / "logs", startup_work=False,
                     access_verifier=access.Verifier(DOMAIN, AUD, fetch=Certs()))
    refused = TestClient(app, base_url=LOCAL, headers=as_(ADMIN)).post(
        "/api/team", json={"name": "Erin Park", "email": ERIN, "region": "NASA"})
    assert refused.status_code == 503
    assert refused.json()["detail"] == pipeline.zendesk_token_rejected(config)
    assert f"set-secret zendesk_token --config {config}" in refused.json()["detail"]
    assert "work-assistant setup" not in refused.json()["detail"]
