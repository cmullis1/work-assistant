"""The hosted edition and the server config in the app (hosted Team activity design §3 and §6, plan Task 1):
every Call QA route answers 404, Anthropic is never touched, the public host is accepted, `/healthz` answers, the
config's time zone wins, and the page shows no Call QA at all."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from test_access import Certs, token
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_test_view import answer, page_responses, run_page
from work_assistant import access, people, pipeline, settings
from work_assistant.activity import store
from work_assistant.activity.model import Action, ShiftVersion
from work_assistant.db import connect, init_db
from work_assistant.rubric import ensure_seeded
from work_assistant.server_config import ServerConfig
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app

PUBLIC = "activity.example.com"
LOCAL = "http://127.0.0.1:8770"
PAT = "pat@example.com"
ADMIN = "owner@example.com"
UTC = timezone.utc

# Every Call QA route, as (method, path), with its path parameters filled in. The list in the plan, plus POST
# /api/quit (ruling R2: on the server systemd owns the process, so no signed-in person may stop it).
CALL_QA_ROUTES = [
    ("GET", "/api/runs"), ("POST", "/api/runs"), ("POST", "/api/runs/estimate"), ("POST", "/api/runs/1/confirm"),
    ("POST", "/api/runs/1/clear"), ("POST", "/api/runs/1/cancel"),
    ("POST", "/api/review-again"), ("POST", "/api/review-again/estimate"),
    ("GET", "/api/tests"), ("POST", "/api/tests"), ("POST", "/api/tests/estimate"), ("GET", "/api/tests/1"),
    ("GET", "/api/tests/1/calls/2"),
    ("GET", "/api/overview"),
    ("GET", "/api/agents"), ("GET", "/api/agents/lee@example.com"), ("POST", "/api/agents/lee@example.com/notes"),
    ("DELETE", "/api/notes/1"),
    ("GET", "/api/tickets"), ("GET", "/api/tickets/1"),
    ("GET", "/api/export.xlsx"),
    ("PUT", "/api/reviews/1/checks/sc01/override"), ("DELETE", "/api/reviews/1/checks/sc01/override"),
    ("GET", "/api/rubric"), ("PUT", "/api/rubric"), ("GET", "/api/rubric/versions"), ("GET", "/api/rubric/versions/1"),
    ("GET", "/api/agreement"),
    ("GET", "/api/schedule"),
    ("GET", "/api/status"),
    ("POST", "/api/quit"),
]
# What the hosted edition keeps: Settings and Team activity, and who is signed in and the people who can use
# it (plan Task 3; test_web_scoping places each of these by how it keeps a team lead to their techs).
KEPT_ROUTES = {
    ("GET", "/api/settings"), ("PUT", "/api/settings"), ("POST", "/api/team"), ("DELETE", "/api/team/{email}"),
    ("GET", "/api/activity"), ("GET", "/api/activity/trends"), ("GET", "/api/activity/day"),
    ("POST", "/api/activity/sync"),
    ("GET", "/api/shifts"), ("PUT", "/api/shifts/{email}"), ("PUT", "/api/shifts/{email}/start"),
    ("PUT", "/api/team/{email}"),
    ("GET", "/api/time-off"), ("POST", "/api/time-off"), ("PATCH", "/api/time-off/{item_id}"),
    ("DELETE", "/api/time-off/day"), ("DELETE", "/api/time-off/{item_id}"),
    ("GET", "/api/me"), ("PUT", "/api/me/regions"),
    ("GET", "/api/people"), ("POST", "/api/people"), ("DELETE", "/api/people/{email}"),
    ("PUT", "/api/people/{email}/techs"),
    ("PUT", "/api/people/{email}/regions"),
    ("POST", "/api/shifts-import/preview"), ("POST", "/api/shifts-import/apply"),
    ("GET", "/api/prep"), ("GET", "/api/prep/tech"), ("GET", "/api/prep/trends"), ("GET", "/api/prep/schedule"),
    ("PUT", "/api/prep/schedule/{email}"),
}


def hosted_config(tmp_path, **changes) -> ServerConfig:
    values = dict(edition="hosted", public_host=PUBLIC, db_path=tmp_path / "work-assistant.db",
                  log_dir=tmp_path / "logs", secrets_file=tmp_path / "secrets.json", admin_email="owner@example.com",
                  access_team_domain="example.cloudflareaccess.com", access_aud="aud-tag-for-tests",
                  timezone="America/Chicago")
    values.update(changes)
    return ServerConfig(**values)


def signed_in() -> dict:
    """The header Cloudflare Access puts on every request of a signed-in person (here the config's admin)."""
    return {"Cf-Access-Jwt-Assertion": token(ADMIN)}


@pytest.fixture(autouse=True)
def certificates(monkeypatch):
    """The config's Cloudflare team answers with the test keys (test_access), so the clients below sign in."""
    monkeypatch.setattr(access, "fetch_certs", Certs())


def never_called():
    raise AssertionError("the hosted edition must never call this here (Anthropic, Zendesk or quitting)")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "work-assistant.db"
    conn = connect(path)
    init_db(conn)
    ensure_seeded(conn)
    settings.set_setting(conn, "team", [PAT])
    conn.close()
    return path


@pytest.fixture
def started():
    return []


@pytest.fixture
def client_hosted(db_path, tmp_path, started):
    app = create_app(db_path, config=hosted_config(tmp_path),
                     run_starter=lambda spec: started.append(spec) or 7,
                     zendesk_factory=never_called, anthropic_factory=never_called,
                     log_dir=tmp_path / "logs", startup_work=False, on_quit=never_called)
    return TestClient(app, base_url=LOCAL, headers=signed_in())


@pytest.fixture
def client_full(db_path, tmp_path):
    app = create_app(db_path, run_starter=lambda spec: 1, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def send(client, method, path, **kwargs):
    if method in ("POST", "PUT", "PATCH"):
        kwargs.setdefault("json", {})
    return client.request(method, path, **kwargs)


# -- the PC copy ------------------------------------------------------------------------------------------


def test_full_edition_without_config_is_unchanged(db_path, tmp_path):
    client = TestClient(create_app(db_path, run_starter=lambda spec: 1, log_dir=tmp_path / "logs",
                                   startup_work=False), base_url="http://127.0.0.1:8765")
    for path in ("/api/runs", "/api/status", "/api/overview", "/api/agents", "/api/rubric", "/api/schedule",
                 "/api/tests", "/api/tickets", "/api/activity"):
        assert client.get(path).status_code == 200, path
    body = client.get("/api/settings").json()
    assert body["edition"] == "full"
    assert body["credentials"] == {"zendesk_token": False, "anthropic_api_key": False}
    assert client.post("/api/quit").status_code == 503, "no quit hook given: refused as before"
    assert client.get("/api/settings", headers={"Host": PUBLIC}).status_code == 403
    assert client.get("/api/settings", headers={"Host": "127.0.0.1:8770"}).status_code == 403
    assert client.put("/api/settings", json={}, headers={"Origin": f"https://{PUBLIC}"}).status_code == 403


# -- the hosted edition ----------------------------------------------------------------------------


def test_hosted_edition_hides_every_call_qa_route(client_hosted, started):
    for method, path in CALL_QA_ROUTES:
        response = send(client_hosted, method, path)
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "Not found."}, (method, path)
    assert started == [], "nothing was started"
    for path in ("/api/settings", "/api/activity", "/api/activity/trends", "/api/shifts", "/api/time-off"):
        assert client_hosted.get(path).status_code == 200, path
    assert client_hosted.post("/api/activity/sync", json={"kind": "refresh"}).status_code == 202
    assert started == [{"command": "activity-sync", "kind": "refresh"}]


def test_every_route_is_either_call_qa_or_kept(client_full):
    """A guard for later work: a new /api/ route has to be placed on one side or the other."""
    routes = [route for route in client_full.app.routes
              if isinstance(route, APIRoute) and route.path.startswith("/api/")]
    every = {(method, route.path) for route in routes for method in route.methods}
    assert KEPT_ROUTES <= every
    covered = {(method, route.path) for method, path in CALL_QA_ROUTES for route in routes
               if method in route.methods and route.path_regex.fullmatch(path)}
    assert covered | KEPT_ROUTES == every, f"routes on neither side: {sorted(every - covered - KEPT_ROUTES)}"
    assert not covered & KEPT_ROUTES


def test_hosted_never_builds_an_anthropic_client(db_path, tmp_path):
    conn = connect(db_path)
    conn.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at, pid) "
                 "VALUES ('manual', '2026-10-04T05:00:00Z', '2026-10-11T05:00:00Z', 1, 'submitted', "
                 "'2026-10-12T00:00:00Z', NULL)")
    conn.commit()
    conn.close()
    called = []

    def factory():   # startup work contains any exception, so the call itself is what is recorded
        called.append(1)
        raise AssertionError("the hosted edition must never build an Anthropic client")

    app = create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 1,
                     zendesk_factory=never_called, anthropic_factory=factory,
                     log_dir=tmp_path / "logs", startup_work=True)
    with TestClient(app, base_url=LOCAL, headers=signed_in()) as client:
        for thread in threading.enumerate():
            if thread.name == "work-assistant-startup":
                thread.join(timeout=30)
        for method, path in CALL_QA_ROUTES:
            send(client, method, path)
        assert client.get("/api/settings").status_code == 200
    assert called == []
    check = connect(db_path)
    assert check.execute("SELECT status FROM runs").fetchone()[0] == "submitted", "never collected"
    check.close()


def test_startup_work_without_call_qa_skips_collecting_runs(db_path, tmp_path, monkeypatch):
    conn = connect(db_path)
    conn.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at, pid) "
                 "VALUES ('manual', '2026-10-04T05:00:00Z', '2026-10-11T05:00:00Z', 1, 'submitted', "
                 "'2026-10-12T00:00:00Z', NULL)")
    conn.commit()
    conn.close()
    purged, built, collected = [], [], []
    monkeypatch.setattr(webapp, "purge_expired", lambda conn, now, days, log_dir: purged.append(days) or 0)
    monkeypatch.setattr(pipeline, "resume_runs", lambda *a, **k: collected.append(1) or [])
    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=lambda: built.append(1) or ("c", ()),
                        now=datetime(2026, 10, 17, 12, tzinfo=UTC), call_qa=False)
    assert purged == [90], "the retention purge (which covers Team activity too) still runs"
    assert built == [] and collected == []


def test_public_host_and_https_origin_allowed(client_hosted):
    public = {"Host": PUBLIC}
    assert client_hosted.get("/api/settings", headers=public).status_code == 200
    changed = client_hosted.put("/api/settings", json={"activity_gap_minutes": 100},
                            headers={**public, "Origin": f"https://{PUBLIC}"})
    assert changed.status_code == 200 and changed.json()["activity_gap_minutes"] == 100
    assert client_hosted.get("/api/settings", headers={"Host": "localhost:8770"}).status_code == 200
    assert client_hosted.put("/api/settings", json={}, headers={"Origin": LOCAL}).status_code == 200


def test_other_host_refused_with_config(client_hosted):
    for host in ("evil.example", "127.0.0.1:8765", f"{PUBLIC}.evil.example", "activity.example.org"):
        assert client_hosted.get("/api/settings", headers={"Host": host}).status_code == 403, host
        assert client_hosted.get("/api/runs", headers={"Host": host}).status_code == 403, host
    for origin in ("https://evil.example", f"http://{PUBLIC}", "http://127.0.0.1:8765"):
        refused = client_hosted.put("/api/settings", json={"activity_gap_minutes": 100},
                                headers={"Host": PUBLIC, "Origin": origin})
        assert refused.status_code == 403, origin
    assert client_hosted.get("/api/settings").json()["activity_gap_minutes"] == 120, "nothing was changed"


def test_healthz_returns_ok_only(client_hosted, client_full):
    for client in (client_hosted, client_full):
        for host in (None, "evil.example"):
            response = client.get("/healthz", headers={"Host": host} if host else {})
            assert response.status_code == 200
            assert response.json() == {"ok": True}


def test_settings_reports_edition(client_hosted, client_full):
    body = client_hosted.get("/api/settings").json()
    assert body["edition"] == "hosted"
    assert body["credentials"] == {"zendesk_token": False}, "the hosted copy never asks about an Anthropic key"
    assert client_full.get("/api/settings").json()["edition"] == "full"


def test_a_full_edition_config_keeps_call_qa_and_accepts_its_public_host(db_path, tmp_path):
    config = hosted_config(tmp_path, edition="full", port=8771)
    client = TestClient(create_app(db_path, config=config, run_starter=lambda spec: 1, log_dir=tmp_path / "logs",
                                   startup_work=False), base_url="http://127.0.0.1:8771")
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/status", headers={"Host": PUBLIC}).status_code == 200
    assert client.get("/api/status", headers={"Host": "127.0.0.1:8765"}).status_code == 403, "the config's port"
    assert client.get("/api/settings").json()["edition"] == "full"


def test_config_timezone_overrides_setting_and_machine(db_path, tmp_path, monkeypatch):
    """Review Focus 4: the VM's clock is UTC, and the stored setting says another zone, but every day is judged in
    the config's America/Chicago."""
    import tzlocal

    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "UTC")
    conn = connect(db_path)
    settings.set_setting(conn, "timezone", "Asia/Tokyo")
    store.add_shift_version(conn, ShiftVersion(PAT, date(2026, 9, 1), frozenset(range(7)), 0, 1439, True))
    # 02:30 UTC on Tuesday Oct 6: Monday Oct 5 at 21:30 in Chicago, Tuesday in UTC and in Tokyo.
    store.insert_actions(conn, [Action(PAT, 11, datetime(2026, 10, 6, 2, 30, tzinfo=UTC), "reply", None, None, 1, 1)])
    people.set_assignments(conn, ADMIN, [PAT])   # the admin, like any lead, sees their own team
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", ("2026-10-17T11:00:00Z",) * 3 + ("2026-09-01T05:00:00Z",))
    conn.commit()
    conn.close()
    # 03:00 UTC on Sunday Oct 18 is still Saturday Oct 17 in Chicago.
    app = create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 1,
                     log_dir=tmp_path / "logs", startup_work=False, now=datetime(2026, 10, 18, 3, tzinfo=UTC))
    client = TestClient(app, base_url=LOCAL, headers=signed_in())
    assert client.get("/api/settings").json()["timezone_effective"] == "America/Chicago"
    body = client.get("/api/activity", params={"start": "2026-10-05", "end": "2026-10-06", "tech": PAT}).json()
    cells = {c["day"]: c for c in body["rows"][0]["cells"]}
    assert (cells["2026-10-05"]["replies"], cells["2026-10-06"]["replies"]) == (1, 0)
    assert cells["2026-10-05"]["last"] == "21:30"
    assert client.get("/api/activity").json()["today"] == "2026-10-17"


# -- the page ---------------------------------------------------------------------------------------------

HELPERS = """
function shownLinks() {
  return all(sidenav, function (e) { return e.tagName === "A" && !e.hidden; }).map(function (a) { return a.textContent; });
}
function callQaLinks() {
  return all(sidenav, function (e) {
    return e.tagName === "A" && (e.getAttribute("data-section") === "callqa" ||
      ["overview", "agents", "tickets", "rubric"].indexOf(e.getAttribute("data-tab")) >= 0);
  }).length;
}
function statusReads() { return requests.filter(function (r) { return r.url === "/api/status"; }).length; }
"""


def hosted_responses(tmp_path, *paths) -> dict:
    responses = page_responses(api_for(tmp_path), "/api/activity", *paths)
    body = responses["GET /api/settings"]["body"]
    body["edition"] = "hosted"
    body["show_call_qa"] = True   # the edition wins over the PC's switch
    body["credentials"] = {"zendesk_token": True}
    responses["GET /api/status"] = answer({"last_run": {"run_id": 3, "status": "failed", "alert": True,
                                                        "error_label": "Zendesk error"},
                                           "awaiting_confirmation": [], "submission_unknown": []})
    responses.update(signed_in_admin())
    return responses


def signed_in_admin() -> dict:
    """The hosted page's reads of who is signed in (the admin) and the people (plan Task 3), as the stand-in wants."""
    return {"GET /api/me": answer({"email": ADMIN, "role": "admin", "edition": "hosted"}),
            "GET /api/people": answer({"people": [{"email": ADMIN, "role": "admin", "techs": []}], "roster": []})}


MANAGER = "parker.lane@example.com"


def signed_in_manager(responses: dict, people: dict | None = None) -> dict:
    """`responses` (the PC page's stand-ins) as the hosted page reads them for a manager, Parker Lane, who picked NASA
    and APAC (Managers design): the hosted edition, `/api/me` saying who is signed in, and `/api/people`, the People
    answer the page reads at start for their teams (`people`; default: nobody)."""
    responses["GET /api/settings"]["body"]["edition"] = "hosted"
    responses["GET /api/me"] = answer({
        "email": MANAGER, "role": "manager", "edition": "hosted", "name": "Parker Lane", "regions": ["NASA", "APAC"],
        "all_regions": [{"region": "NASA", "city": "Dallas"}, {"region": "EMEA", "city": "Dubai"},
                        {"region": "APAC", "city": "Manila"}]})
    responses["GET /api/people"] = answer(people or {"people": [], "roster": []})
    return responses


def test_page_hides_call_qa_in_hosted_edition(tmp_path, version):
    out = run_page("", hosted_responses(tmp_path), HELPERS + """
run(async function () {
  var out = { hash: location.hash, shown: shownLinks(), callQaLinks: callQaLinks(), title: titleEl.textContent,
              banners: document.getElementById("banners").textContent };
  out.visited = [];
  for (var hash of ["#/overview", "#/tickets?ticket=5", "#/agents/lee@example.com", "#/rubric", "#/settings/tests/5"]) {
    await go(hash);
    out.visited.push(location.hash);
  }
  await go("#/settings");
  out.panels = panels().map(titleOf);
  out.heads = all(screenEl, function (e) { return e.tagName === "H3"; }).length;
  out.switches = all(screenEl, function (e) { return e.getAttribute("id") === "set-show-call-qa"; }).length;
  out.anthropic = screenEl.textContent.indexOf("Anthropic") >= 0;
  out.statusReads = statusReads();
  out.callQaReads = requests.filter(function (r) {
    return ["/api/runs", "/api/tests", "/api/schedule", "/api/agreement", "/api/overview", "/api/tickets",
            "/api/agents", "/api/rubric"].some(function (p) { return r.url.indexOf(p) === 0; });
  }).length;
  out.banners2 = document.getElementById("banners").textContent;
  return out;
});
""")
    assert out["hash"] == "#/team", "the hosted copy opens to Team activity"
    assert out["shown"] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"], "1:1 prep is everyone's"
    assert out["callQaLinks"] == 0, "the Call QA links are gone from the page, not just hidden"
    assert out["title"] == "Team › Activity"
    assert out["visited"] == ["#/team", "#/team", "#/team", "#/team", "#/settings"]
    assert out["panels"] == ["Techs", "Import from Teams Shifts", "Shifts", "Time off", "1:1 schedule", "Users",
                             "Activity flags", "Zendesk and credentials"], \
        "no Sections panel: its only switch is Show Call QA"
    assert out["heads"] == 0 and out["switches"] == 0
    assert out["anthropic"] is False
    assert out["banners"] == "" and out["banners2"] == ""
    assert out["statusReads"] == 0 and out["callQaReads"] == 0


def test_page_in_the_full_edition_still_shows_call_qa(tmp_path, version):
    responses = page_responses(api_for(tmp_path))
    assert responses["GET /api/settings"]["body"]["edition"] == "full"
    out = run_page("", responses, HELPERS + """
run(async function () {
  await go("#/settings");
  return { shown: shownLinks().length, panels: panels().map(titleOf).slice(0, 5),
           anthropic: screenEl.textContent.indexOf("Anthropic API key") >= 0 };
});
""")
    assert out["shown"] == 10
    assert out["panels"] == ["Team", "Time off", "1:1 schedule", "Sections", "Activity flags"]
    assert out["anthropic"] is True


def test_page_team_panel_says_nothing_about_call_qa_in_hosted(tmp_path):
    """Adding and removing a tech (the hosted admin's Techs panel): the hint about Call QA's reviews and Run now, and
    the note that new calls won't be reviewed, belong to Call QA, so the hosted edition leaves them out."""
    from test_web_settings_app import AVERY, BLAKE, DANA, settings_body
    from test_web_settings_app import HELPERS as SETTINGS_HELPERS
    from test_web_settings_app import responses_for as settings_responses

    def hosted(body):
        return dict(body, edition="hosted", credentials={"zendesk_token": True})

    def roster(*techs):
        return {"people": [{"email": ADMIN, "role": "admin", "name": None, "techs": list(techs)}],
                "roster": [{"email": t, "name": t, "leads": [ADMIN]} for t in techs]}

    responses = settings_responses(tmp_path)
    responses["GET /api/settings"] = answer(hosted(settings_body(tmp_path)))
    responses["POST /api/team"] = answer({**hosted(settings_body(tmp_path, team=(AVERY, BLAKE, DANA))), "added": DANA})
    responses["DELETE /api/team/blake%40example.com"] = answer(
        {**hosted(settings_body(tmp_path, team=(AVERY, DANA))), "removed": BLAKE})
    responses.update(signed_in_admin())
    responses["GET /api/people"] = answer(roster(AVERY, BLAKE))
    out = run_page("#/settings", responses, SETTINGS_HELPERS + """
var confirms = [];
globalThis.confirm = function (text) { confirms.push(text); return true; };
function techsEl() { return panelTitled("Techs"); }
function rosterRow(email) {
  return all(techsEl(), function (e) { return e.tagName === "TR" && e.classList.contains("roster-row") &&
    notes(e)[0] === email; })[0];
}
run(async function () {
  RESPONSES["GET /api/people"] = { status: 200, body: %s };
  field(techsEl(), "Tech name").value = "Dana Fox";
  click(techsEl(), "Add tech");
  await settle();
  var added = { message: messageOf(techsEl()), text: screenEl.textContent };
  RESPONSES["GET /api/people"] = { status: 200, body: %s };
  click(rosterRow("blake@example.com"), "Remove");
  await settle();
  return { added: added, confirms: confirms, removed: messageOf(techsEl()) };
});
""" % (json.dumps(roster(AVERY, BLAKE, DANA)), json.dumps(roster(AVERY, DANA))))
    assert any(m.startswith("Added " + DANA) for m in out["added"]["message"]), "the tech was added"
    assert "Run now" not in out["added"]["text"] and "reviewed" not in out["added"]["text"]
    assert out["confirms"] == [f"Remove {BLAKE}? They come off every team, and the app stops following them."]
    assert f"Removed {BLAKE}." in out["removed"]
