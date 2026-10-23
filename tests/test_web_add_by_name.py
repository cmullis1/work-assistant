"""Adding techs and team leads by name (hosted Team activity design §4, "Adding people by name", plan Task 7): the
Team panel's add field takes a name and the app makes the email and checks it with Zendesk, offering the email to
correct when Zendesk has no agent there; the People panel makes a TL's email and shows it, editable, before saving;
the typed name is stored and shown; and a TL can neither add anyone nor learn anyone else's name.

The roster and the TLs are test_web_scoping's (Lee leads Avery and Blake, Kim leads Blake and Casey). The Zendesk
stand-in knows a few agents at the company's domain; nothing here reaches Zendesk or Cloudflare."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from css_rules import app_css, values
from test_access import AUD, DOMAIN, Certs
from test_web_edition import LOCAL, hosted_config
from test_web_people_ui import ADMIN_READS, PEOPLE_HELPERS, read_all
from test_web_scoping import (ADMIN, AVERY, BLAKE, CASEY, DANA, KIM, LEE, NAMES_NO_TECH, SCOPED,  # noqa: F401
                              TEAM, WEEK, add_reassignments, as_, db_path)
from test_web_settings import FakeZendesk
from test_web_settings_app import HELPERS
from test_web_test_view import answer, page_responses, run_page
from work_assistant import access, names, people
from work_assistant.db import connect
from work_assistant.web import app as app_module
from work_assistant.web.app import create_app

DESMOND = "desmond.salazar@example.com"       # a Zendesk agent
DES = "des.salazar@example.com"             # what "Des Salazar" makes: no agent there
JOSE = "jose.obrien-lee@example.com"        # a Zendesk agent
CUSTOMER = "customer@example.com"           # a Zendesk end user, not an agent
ADMIN_ONLY = {"detail": "Only the admin can do this."}


@pytest.fixture
def zendesk():
    return FakeZendesk(known={DESMOND, JOSE}, end_users={CUSTOMER})


@pytest.fixture
def client_full(db_path, tmp_path, zendesk):
    app = create_app(db_path, run_starter=lambda spec: 1, zendesk_factory=lambda: zendesk, log_dir=tmp_path / "logs",
                     startup_work=False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def app_hosted(db_path, tmp_path, zendesk):
    return create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 7,
                      zendesk_factory=lambda: zendesk, log_dir=tmp_path / "logs", startup_work=False,
                      access_verifier=access.Verifier(DOMAIN, AUD, fetch=Certs()))


@pytest.fixture
def client_hosted_admin(app_hosted):
    return TestClient(app_hosted, base_url=LOCAL, headers=as_(ADMIN))


@pytest.fixture
def client_hosted_tl(app_hosted):
    """Lee, who leads Avery and Blake."""
    return TestClient(app_hosted, base_url=LOCAL, headers=as_(LEE))


def stored_names(db_path) -> dict[str, str]:
    conn = connect(db_path)
    try:
        return names.stored(conn)
    finally:
        conn.close()


def shifts_by_email(client) -> dict:
    return {t["email"]: t for t in client.get("/api/shifts").json()["techs"]}


# -- the Team panel's API (PC and hosted) -------------------------------------------------------------------


def test_add_tech_by_name_found_stores_name(client_full, zendesk, db_path):
    added = client_full.post("/api/team", json={"name": "  Desmond   Salazar "})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["added"] == DESMOND
    assert body["team"] == [*TEAM, DESMOND], "the answer is the settings, as saving them gives"
    assert zendesk.lookups == [[DESMOND]] and zendesk.closed, "checked with Zendesk as Settings' team is"
    assert stored_names(db_path) == {DESMOND: "Desmond Salazar"}
    techs = shifts_by_email(client_full)
    assert techs[DESMOND]["name"] == "Desmond Salazar", "the typed name is the name shown"
    assert [v["effective_from"] for v in techs[DESMOND]["versions"]] == ["2026-10-17"], \
        "the server starts their shift as for any tech added (apply_team_change)"
    shown = {t["email"]: t["name"] for t in client_full.get("/api/activity").json()["techs"]}
    assert shown[DESMOND] == "Desmond Salazar" and shown[AVERY] == AVERY


def test_add_tech_by_name_not_found_offers_email(client_full, zendesk, db_path):
    refused = client_full.post("/api/team", json={"name": "Des Salazar"})
    assert (refused.status_code, refused.json()) == (400, {"detail": f"No Zendesk agent at {DES}", "email": DES})
    assert zendesk.lookups == [[DES]]
    assert client_full.get("/api/settings").json()["team"] == TEAM, "a wrong email is never saved"
    assert DES not in shifts_by_email(client_full) and stored_names(db_path) == {}

    one_word = client_full.post("/api/team", json={"name": "Cher"})
    assert (one_word.status_code, one_word.json()) == (400, {"detail": names.TWO_WORDS, "email": ""}), \
        "no email can be made: the panel offers the field, empty"
    assert zendesk.lookups == [[DES]], "nothing to check"
    again = client_full.post("/api/team", json={"name": "Avery Cole", "email": AVERY})
    assert (again.status_code, again.json()) == (400, {"detail": f"{AVERY} is already on the team."})
    for body in ({"name": "  "}, {"email": DESMOND}, {"name": 7}, {"name": "Des Salazar", "email": 7}, ["Des"]):
        assert client_full.post("/api/team", json=body).status_code == 400, body
    assert zendesk.lookups == [[DES]] and client_full.get("/api/settings").json()["team"] == TEAM


def test_add_tech_with_corrected_email_still_checked(client_full, zendesk, db_path):
    typo = client_full.post("/api/team", json={"name": "Des Salazar", "email": " Desmond.Salazr@Example.com "})
    assert (typo.status_code, typo.json()) == (400, {"detail": "No Zendesk agent at desmond.salazr@example.com",
                                                     "email": "desmond.salazr@example.com"})
    customer = client_full.post("/api/team", json={"name": "Des Salazar", "email": CUSTOMER})
    assert (customer.status_code, customer.json()) == (400, {"detail": f"No Zendesk agent at {CUSTOMER}",
                                                             "email": CUSTOMER}), "an end user isn't an agent"
    bad = client_full.post("/api/team", json={"name": "Des Salazar", "email": "des salazar"})
    assert (bad.status_code, bad.json()) == (400, {"detail": "'des salazar' is not an email address",
                                                   "email": "des salazar"})
    fixed = client_full.post("/api/team", json={"name": "Des Salazar", "email": " Desmond.Salazar@Example.com "})
    assert fixed.status_code == 200 and fixed.json()["added"] == DESMOND
    assert zendesk.lookups == [["desmond.salazr@example.com"], [CUSTOMER], [DESMOND]], \
        "an email given skips the making, not the check"
    one_word = client_full.post("/api/team", json={"name": "José", "email": JOSE})
    assert one_word.status_code == 200, "with the email given, one word is enough for a name"
    assert stored_names(db_path) == {DESMOND: "Des Salazar", JOSE: "José"}
    assert client_full.get("/api/settings").json()["team"] == [*TEAM, DESMOND, JOSE]


def test_zendesk_unreachable_adds_nobody(db_path, tmp_path):
    from work_assistant.zendesk.client import ZendeskError

    class Down(FakeZendesk):
        def users_by_email(self, emails):
            raise ZendeskError("down")

    app = create_app(db_path, run_starter=lambda spec: 1, zendesk_factory=Down, log_dir=tmp_path / "logs",
                     startup_work=False)
    refused = TestClient(app, base_url="http://127.0.0.1:8765").post("/api/team", json={"name": "Desmond Salazar"})
    assert refused.status_code == 503 and "Zendesk" in refused.json()["detail"]
    assert stored_names(db_path) == {}


# -- the People panel's API (hosted) ---------------------------------------------------------------------------


def test_people_add_tl_by_name_shows_email(client_hosted_admin, zendesk):
    made = client_hosted_admin.post("/api/people", json={"name": " Pat  Lead ", "role": "tl"})
    assert made.status_code == 200, made.text
    assert {"email": "pat.lead@example.com", "role": "tl", "name": "Pat Lead", "techs": []} in \
        made.json()["people"]
    given = client_hosted_admin.post("/api/people", json={"name": "Robin Ash", "email": " Robin.Ashworth@Example.com",
                                                      "role": "tl"})
    assert {"email": "robin.ashworth@example.com", "role": "tl", "name": "Robin Ash", "techs": []} in \
        given.json()["people"], "the email as the admin corrected it"
    one_word = client_hosted_admin.post("/api/people", json={"name": "Cher", "role": "tl"})
    assert (one_word.status_code, one_word.json()) == (400, {"detail": names.TWO_WORDS})
    nothing = client_hosted_admin.post("/api/people", json={"name": " ", "role": "tl"})
    assert (nothing.status_code, nothing.json()) == (400, {"detail": "Enter their name."})
    assert zendesk.lookups == [], "TLs aren't checked against Zendesk"

    # The page: the made email shows as the name is typed, stays as the admin edits it, and is what's saved.
    admin = client_hosted_admin
    responses = read_all(admin, *ADMIN_READS)
    responses["POST /api/people"] = answer(admin.post("/api/people", json={
        "name": "José O'Brien-Lee", "email": "jose.obrien@example.com", "role": "tl"}).json())
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + """
function type(el, text) { el.value = text; fire(el, "input"); }
run(async function () {
  var name = field(leadsEl(), "User name"), email = field(leadsEl(), "User email");
  var out = { placeholder: email.getAttribute("placeholder") };
  type(name, "José O");
  out.partly = email.value;
  type(name, "José O'Brien-Lee");
  out.made = email.value;
  type(email, "jose.obrien@example.com");
  type(name, "José O'Brien-Lee ");
  out.kept = email.value;
  click(leadsEl(), "Add");
  await settle();
  var row = personRow("José O'Brien-Lee");
  out.row = row ? notes(row) : null;
  out.message = leadsMessage();
  out.cleared = [name.value, email.value];
  type(name, "Pat Lead");
  out.again = email.value;
  out.writes = writes();
  return out;
});
""")
    assert out["placeholder"] == "firstname.lastname@example.com"
    assert out["partly"] == "jose.o@example.com" and out["made"] == "jose.obrien-lee@example.com", \
        "the made email shows before saving"
    assert out["kept"] == "jose.obrien@example.com", "once edited, typing the name leaves it alone"
    assert out["writes"] == [["POST", "/api/people", {"name": "José O'Brien-Lee",
                                                      "email": "jose.obrien@example.com", "role": "tl"}]]
    assert out["row"] == ["jose.obrien@example.com · TL"], "Users shows the TL's name, then the email"
    assert out["message"] == ["Added jose.obrien@example.com. Click Edit techs to choose their techs."]
    assert out["cleared"] == ["", ""] and out["again"] == "pat.lead@example.com", "the next name makes it again"


# -- a TL --------------------------------------------------------------------------------------------------------

NAMED = {AVERY: "Avery Cole", BLAKE: "Blake Reed", CASEY: "Casey Quinn", DANA: "Dana Fox"}


def test_tl_cannot_add_or_see_other_names(client_hosted_tl, client_hosted_admin, zendesk, db_path):
    conn = connect(db_path)
    try:
        for email, name in NAMED.items():
            names.store(conn, email, name)
        people.add_person(conn, KIM, "tl", name="Kim Lead")
    finally:
        conn.close()
    add_reassignments(db_path)
    team = client_hosted_admin.get("/api/settings").json()["team"]
    everyone = client_hosted_admin.get("/api/people").json()

    for path, body in (("/api/team", {"name": "Desmond Salazar"}), ("/api/team", {"name": "X Y", "email": DESMOND}),
                       ("/api/people", {"name": "Pat Lead", "role": "tl"})):
        refused = client_hosted_tl.post(path, json=body)
        assert (refused.status_code, refused.json()) == (403, ADMIN_ONLY), path
    assert client_hosted_tl.get("/api/people").status_code == 403
    assert zendesk.lookups == [], "a TL's add never reaches Zendesk"
    assert client_hosted_admin.get("/api/settings").json()["team"] == team
    assert client_hosted_admin.get("/api/people").json() == everyone

    week = "&".join(f"{k}={v}" for k, v in WEEK.items())
    reads = ["/api/settings", "/api/me", "/api/activity", f"/api/activity?{week}",
             f"/api/activity?tech={AVERY}", "/api/activity/trends", f"/api/activity/trends?tech={BLAKE}",
             f"/api/activity/day?tech={AVERY}&day=2026-10-05", f"/api/activity/day?tech={BLAKE}&day=2026-10-07",
             "/api/shifts", "/api/time-off", "/api/prep", f"/api/prep/trends?email={BLAKE}", "/api/prep/schedule"]
    # (a tech's prep detail answers 400 here, with no 1:1 date set: test_web_oneonone_scoping reads it)
    assert {path.split("?")[0] for path in reads} | {"/api/prep/tech"} == {path for method, path in SCOPED | NAMES_NO_TECH
                                                      if method == "GET"}
    seen = ""
    for path in reads:
        response = client_hosted_tl.get(path)
        assert response.status_code == 200, path
        for hidden in (CASEY, DANA, "Casey Quinn", "Dana Fox", KIM, "Kim Lead"):
            assert hidden not in response.text, (path, hidden)
        seen += response.text
    assert "Avery Cole" in seen and "Blake Reed" in seen, "their own techs' names reach them"


def test_email_domain_setting_admin_only(client_hosted_tl, client_hosted_admin):
    assert client_hosted_tl.get("/api/settings").json()["email_domain"] == "example.com"
    refused = client_hosted_tl.put("/api/settings", json={"email_domain": "example.com"})
    assert (refused.status_code, refused.json()) == (403, ADMIN_ONLY)
    assert client_hosted_admin.get("/api/settings").json()["email_domain"] == "example.com"
    changed = client_hosted_admin.put("/api/settings", json={"email_domain": " Example.ORG "})
    assert changed.status_code == 200 and changed.json()["email_domain"] == "example.org"
    made = client_hosted_admin.post("/api/people", json={"name": "Pat Lead", "role": "tl"})
    assert "pat.lead@example.org" in [p["email"] for p in made.json()["people"]], "names make emails at the new domain"
    wrong = client_hosted_admin.put("/api/settings", json={"email_domain": "not a domain"})
    assert wrong.status_code == 400
    cleared = client_hosted_admin.put("/api/settings", json={"email_domain": ""})
    assert cleared.status_code == 200 and cleared.json()["email_domain"] == "example.com",         "blank goes back to the domain the team's own emails have"


def test_no_email_domain_known_asks_for_the_email(db_path, client_full):
    conn = connect(db_path)
    try:   # a fresh PC copy: no saved domain, and no team, people or Zendesk login to work one out from
        conn.execute("DELETE FROM settings")
        conn.execute("DELETE FROM people")
        conn.commit()
    finally:
        conn.close()
    assert client_full.get("/api/settings").json()["email_domain"] is None
    tech = client_full.post("/api/team", json={"name": "Des Salazar"})
    assert (tech.status_code, tech.json()) == (400, {"detail": app_module.NO_EMAIL_DOMAIN, "email": ""}),         "the panel offers the email field, empty"
    given = client_full.post("/api/team", json={"name": "Des Salazar", "email": DESMOND})
    assert given.status_code == 200, "an email typed in needs no domain"
    # (the hosted copy always has its admin in people, so it always has a domain to go on)
    with pytest.raises(app_module._BadRequest, match=app_module.NO_EMAIL_DOMAIN):
        app_module._person_request({"name": "Pat Lead", "role": "tl"}, None)
    assert app_module._person_request({"name": "Pat Lead", "email": "pat@example.org", "role": "tl"}, None) ==         ("pat@example.org", "tl", "Pat Lead")


# -- the Team panel on the page --------------------------------------------------------------------------------

PANEL_HELPERS = """
function addRow() { return all(teamEl(), function (e) { return e.classList.contains("add-row"); })[0]; }
function shown(el) {
  for (var node = el; node && node.tagName; node = node.parentNode) { if (node.hidden) { return false; } }
  return true;
}
function rowNames() {
  return techRows().map(function (r) { return [all(r, function (e) { return e.tagName === "B"; })[0].textContent,
    notes(r)[0]]; });
}
"""


def test_team_panel_name_field_and_correction(client_full):
    """The add field takes a name. Zendesk has no agent at the made address, so the panel says so and shows the
    address, editable, keeping the name and the shift; the corrected address goes in with the name and is checked
    again; once added, the tech's row shows the name and the add row starts over."""
    responses = page_responses(client_full)
    responses["POST /api/team"] = answer(client_full.post("/api/team", json={"name": "Des Salazar"}).json(), 400)
    added = client_full.post("/api/team", json={"name": "Des Salazar", "email": DESMOND}).json()
    shifts = client_full.get("/api/shifts").json()
    placed = next(t for t in shifts["techs"] if t["email"] == DESMOND)["versions"][-1]["effective_from"]
    shift = {"effective_from": placed, "work_days": [1, 2, 3, 4], "start": "08:00", "end": "15:30"}
    put = client_full.put(f"/api/shifts/{DESMOND}", json=shift).json()
    out = run_page("#/settings", responses, HELPERS + PANEL_HELPERS + f"""
run(async function () {{
  var add = addRow(), name = field(add, "Tech name"), email = field(add, "Tech email");
  var out = {{ before: {{ name: !!name && shown(name), email: shown(email),
                        placeholder: name.getAttribute("placeholder"), type: name.getAttribute("type") }} }};
  name.value = "Des Salazar";
  setDays(add, [1, 2, 3, 4]);
  field(add, "End time").value = "15:30";
  click(teamEl(), "Add tech");
  await settle();
  out.refused = {{ message: messageOf(add), email: email.value, shown: shown(email), name: name.value, days: days(add),
                  end: field(add, "End time").value, rows: techRows().length }};
  email.value = "desmond.salazar@example.com";
  RESPONSES["POST /api/team"] = {{ status: 200, body: {json.dumps(added)} }};
  RESPONSES["GET /api/shifts"] = {{ status: 200, body: {json.dumps(shifts)} }};
  RESPONSES["PUT /api/shifts/desmond.salazar%40example.com"] = {{ status: 200, body: {json.dumps(put)} }};
  click(teamEl(), "Add tech");
  await settle();
  out.added = {{ rows: rowNames(), message: messageOf(teamEl()), name: name.value, email: email.value,
                shown: shown(email), days: days(add) }};
  out.writes = writes();
  return out;
}});
""")
    assert out["before"] == {"name": True, "email": False, "placeholder": "First and last name", "type": "text"}, \
        "a name field; the email field waits until it's needed"
    assert out["refused"] == {"message": [f"No Zendesk agent at {DES}. Correct the email and click Add tech again."],
                              "email": DES, "shown": True, "name": "Des Salazar", "days": [1, 2, 3, 4],
                              "end": "15:30", "rows": 4}
    assert out["writes"] == [
        ["POST", "/api/team", {"name": "Des Salazar"}],
        ["POST", "/api/team", {"name": "Des Salazar", "email": DESMOND}],
        ["PUT", f"/api/shifts/{DESMOND}", shift]]
    assert ["Des Salazar", DESMOND] in out["added"]["rows"], "the row shows the typed name, then the email"
    assert out["added"]["message"] == [f"Added {DESMOND}."]
    assert (out["added"]["name"], out["added"]["email"], out["added"]["shown"]) == ("", "", False)
    assert out["added"]["days"] == [0, 1, 2, 3, 4], "the add row starts over"


def test_team_panel_one_word_name_offers_an_empty_email(client_full):
    responses = page_responses(client_full)
    responses["POST /api/team"] = answer(client_full.post("/api/team", json={"name": "Cher"}).json(), 400)
    out = run_page("#/settings", responses, HELPERS + PANEL_HELPERS + """
run(async function () {
  var add = addRow(), name = field(add, "Tech name"), email = field(add, "Tech email");
  click(teamEl(), "Add tech");
  var empty = messageOf(teamEl());
  name.value = "Cher";
  click(teamEl(), "Add tech");
  await settle();
  return { empty: empty, message: messageOf(add), email: [email.value, shown(email)], writes: writes() };
});
""")
    assert out["empty"] == ["Enter the tech's name."], "nothing is sent without a name"
    assert out["message"] == ["Enter their first and last name. Or enter their work email."]
    assert out["email"] == ["", True]
    assert out["writes"] == [["POST", "/api/team", {"name": "Cher"}]]


def test_zendesk_panel_holds_the_email_domain(client_full):
    responses = page_responses(client_full)
    saved = client_full.put("/api/settings", json={"zendesk_subdomain": "acme", "zendesk_email": "owner@example.com",
                                                   "email_domain": "example.com"})
    responses["PUT /api/settings"] = answer(saved.json())
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var zendesk = panelTitled("Zendesk and credentials");
  var domain = all(zendesk, function (e) { return e.getAttribute("id") === "set-email-domain"; })[0];
  var label = all(zendesk, function (e) {
    return e.tagName === "LABEL" && e.getAttribute("for") === "set-email-domain"; })[0];
  var out = { value: domain.value, label: label.textContent,
              hint: all(label.parentNode, function (e) { return e.classList.contains("hint"); })[0].textContent };
  all(zendesk, function (e) { return e.getAttribute("id") === "set-subdomain"; })[0].value = "acme";
  all(zendesk, function (e) { return e.getAttribute("id") === "set-login"; })[0].value = "owner@example.com";
  domain.value = "example.com";
  click(zendesk, "Save");
  await settle();
  out.writes = writes();
  out.after = domain.value;
  return out;
});
""")
    assert out["value"] == "example.com" and out["label"] == "Work email domain"
    assert out["hint"] == "A tech added by name gets the email firstname.lastname at this domain."
    assert out["writes"] == [["PUT", "/api/settings", {"zendesk_subdomain": "acme",
                                                       "zendesk_email": "owner@example.com",
                                                       "email_domain": "example.com"}]]
    assert out["after"] == "example.com"


def test_the_email_to_correct_gets_a_line_of_its_own(client_full, client_hosted_admin):
    """Ruling R10 (b): at the 4K display the Tech column is narrow, so the email to correct goes on the line under the
    add row, across the Tech and Work days columns, with the row's message; that line shows only with something on
    it. People's TL email takes the room left in its row. Neither field can widen a column (CSS)."""
    out = run_page("#/settings", page_responses(client_full), HELPERS + PANEL_HELPERS + """
run(async function () {
  var add = addRow(), table = add.parentNode;
  var lines = add.children.filter(function (e) { return e.tagName === "TR"; });
  var cellOf = function (label) { return field(add, label).parentNode; };
  return { tag: add.tagName, classes: add.getAttribute("class"),
           order: table.children.map(function (e) { return e.tagName; }),
           last: table.children[table.children.length - 1] === add, lines: lines.length,
           second: lines[1].getAttribute("class"),
           nameLine: lines.indexOf(cellOf("Tech name").parentNode),
           emailLine: lines.indexOf(cellOf("Tech email").parentNode),
           emailSpan: cellOf("Tech email").getAttribute("colspan"),
           messageWithEmail: all(cellOf("Tech email"), function (e) { return e.classList.contains("message"); }).length,
           spans: lines[1].children.map(function (td) { return td.getAttribute("colspan"); }) };
});
""")
    assert (out["tag"], out["classes"], out["order"], out["last"]) == (
        "TBODY", "row-group add-row", ["THEAD", "TBODY", "TBODY"], True), "a row group of its own after the techs"
    assert (out["lines"], out["second"], out["nameLine"], out["emailLine"]) == (2, "add-more", 0, 1)
    assert out["emailSpan"] == "3" and out["messageWithEmail"] == 1, \
        "across Tech, Started and Work days, with the message"
    assert out["spans"] == ["3", "3"], "the line spans the table's six columns"

    hosted = run_page("#/settings", read_all(client_hosted_admin, *ADMIN_READS), HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var add = all(techsEl(), function (e) { return e.classList.contains("add-row"); })[0];
  var lines = add.children.filter(function (e) { return e.tagName === "TR"; });
  return { lead: field(leadsEl(), "User email").parentNode.getAttribute("class"),
           table: add.parentNode.getAttribute("class"), lines: lines.length, second: lines[1].getAttribute("class"),
           emailLine: lines.indexOf(field(add, "Tech email").parentNode.parentNode),
           emailSpan: field(add, "Tech email").parentNode.getAttribute("colspan"),
           spans: lines[1].children.map(function (td) { return td.getAttribute("colspan"); }) };
});
""")
    assert hosted == {"lead": "actions people-add", "table": "atable roster", "lines": 2, "second": "add-more",
                      "emailLine": 1, "emailSpan": "4", "spans": ["4", None]}, \
        "the Techs panel's email to correct gets the same line, across the Tech, Started, Region and Team lead columns"

    css = app_css()
    for table in (".atable.team-shifts", ".atable.roster"):
        assert values(css, f"{table} tr.add-more:has(input[hidden]):has(.message:empty)", "display") == ["none"]
        assert values(css, f"{table} tr.add-more .message", "contain") == ["inline-size"]
        assert values(css, f'{table} input[type="email"]', "width") == ["100%"]
        assert values(css, f'{table} input[type="email"]', "min-width") == ["0"]
    assert values(css, '.people-add input[type="email"]', "flex") == ["1 1 var(--input-wide)"]


def test_the_add_rows_dont_light_up_as_one_row_group():
    """Final review F3 (ruling R12): the add rows are a row group for their rule and layout, but the shared row-group
    highlight (a tech's, check's or run's lines lit together) leaves them out."""
    css = app_css()
    assert values(css, ".atable tbody.row-group:not(.add-row):hover tr:not(.edit-row) td", "background") ==         ["var(--hover)"]
    assert values(css, ".atable tbody.row-group:hover tr:not(.edit-row) td", "background") == [],         "no rule lights every row group, the add rows included"


def test_techs_panel_name_field_and_correction(client_hosted_admin, zendesk):
    """The hosted Techs panel adds by name as the PC's Team panel does: no Zendesk agent at the made address, so the
    panel says so and shows the address, editable, keeping the name and the Team lead chosen; the corrected address
    goes in with the name and the lead and is checked again."""
    admin = client_hosted_admin
    responses = read_all(admin, *ADMIN_READS)
    refused = admin.post("/api/team", json={"name": "Des Salazar", "lead": LEE, "region": "NASA"})
    assert refused.status_code == 400
    responses["POST /api/team"] = answer(refused.json(), 400)
    added = admin.post("/api/team", json={"name": "Des Salazar", "email": DESMOND, "lead": LEE, "region": "NASA"}).json()
    people_after = admin.get("/api/people").json()
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + f"""
function shown(el) {{
  for (var node = el; node && node.tagName; node = node.parentNode) {{ if (node.hidden) {{ return false; }} }}
  return true;
}}
run(async function () {{
  var techs = techsEl(), add = all(techs, function (e) {{ return e.classList.contains("add-row"); }})[0];
  var name = field(add, "Tech name"), email = field(add, "Tech email"), lead = field(add, "Team lead");
  var out = {{ before: shown(email) }};
  click(techs, "Add tech");
  out.empty = messageOf(add);
  name.value = "Des Salazar";
  fire(name, "input");
  lead.value = "{LEE}";
  click(techs, "Add tech");
  await settle();
  out.refused = {{ message: messageOf(add), email: email.value, shown: shown(email), name: name.value,
                  lead: lead.value, made: notes(add) }};
  email.value = "{DESMOND}";
  RESPONSES["POST /api/team"] = {{ status: 200, body: {json.dumps(added)} }};
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(people_after)} }};
  click(techs, "Add tech");
  await settle();
  out.added = {{ message: messageOf(techsEl()), shown: shown(email), name: name.value,
                rows: shownRoster().map(function (r) {{ return [r.tech, r.lead]; }}) }};
  out.writes = writes();
  return out;
}});
""")
    assert out["before"] is False, "the email field waits until it's needed"
    assert out["empty"] == ["Enter the tech's name."]
    assert out["refused"] == {"message": [f"No Zendesk agent at {DES}. Correct the email and click Add tech again."],
                              "email": DES, "shown": True, "name": "Des Salazar", "lead": LEE, "made": [""]}
    assert out["writes"] == [["POST", "/api/team", {"name": "Des Salazar", "lead": LEE, "region": "NASA"}],
                             ["POST", "/api/team", {"name": "Des Salazar", "email": DESMOND, "lead": LEE,
                                                    "region": "NASA"}]], "the add row's region goes with each add"
    assert out["added"]["message"] == [f"Added {DESMOND}."]
    assert (out["added"]["shown"], out["added"]["name"]) == (False, "")
    assert [DESMOND, LEE] in out["added"]["rows"]
