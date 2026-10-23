"""The hosted page by role (hosted Team activity design §4, plan Task 3; hosted Team design §3 and §6, plan Task 2;
Managers design, plan Task 3 of the managers plan): the admin's Settings (Shifts, Time off and the 1:1 schedule for
their own team, then the Techs panel, the Users panel and its Edit techs picker), a manager's Settings (Techs, Your
regions, Team leads and the note), a team lead's Settings with only Shifts and Time off for their techs, the Tech picker
listing only their techs, and the "not added yet" page. app.js runs in node on the fake DOM through the stand-in page
of test_web_test_view, answered with what the real hosted app answers each person (test_web_scoping's people and
techs: the admin's own team is Avery and Casey, Lee's Avery and Blake, Kim's Blake and Casey, and Dana is on no
team)."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from css_rules import STATIC, app_css, media_block, px, rules, tokens, top_level, values
from test_web_edition import LOCAL, MANAGER
from test_web_scoping import (ADMIN, AVERY, BLAKE, CASEY, DANA, KIM, LEE, STRANGER, app, as_, db_path,  # noqa: F401
                              started)
from test_web_settings_app import HELPERS, SCHEDULE_HELPERS
from test_web_test_view import answer, run_page
from work_assistant.db import connect

PAT = "pat.lead@example.com"
ERIN = "erin.park@example.com"       # what "Erin Park" makes at the default work email domain
NOT_ADDED_TEXT = (f"You're signed in as {STRANGER}, but you haven't been added yet. Ask {ADMIN} to add you.")
MO = "mo.grant@example.com"               # a manager the admin adds in a test
USERS_SUB = ("Everyone who can sign in: you, the team leads and the managers. Also add each one to the Cloudflare "
             "Access policy, or they can't reach this page.")
LEADS_SUB = "Also add them to the Cloudflare Access policy, or they can't reach this page."
REGIONS_SUB = ("Pick the regions you manage. Activity, Trends and 1:1 Prep show every team in them, and Techs and Team "
               "leads here follow them too.")
MANAGER_NOTE = ("Shifts, time off and 1:1 dates: each team lead sets these for their own techs. You see them on "
                "Activity, Trends and 1:1 Prep.")
ADD_ROW_REGIONS = [["NASA", "NASA"], ["EMEA", "EMEA"], ["APAC", "APAC"]]   # the add row's Region choice: whole names


def client_as(app, email: str) -> TestClient:  # noqa: F811
    return TestClient(app, base_url=LOCAL, headers=as_(email))


def read_all(client: TestClient, *paths: str) -> dict:
    """What the real app answers `client` for each GET, keyed as the stand-in fetch wants."""
    responses = {}
    for path in paths:
        got = client.get(path)
        responses["GET " + path] = answer(got.json(), got.status_code)
    return responses


SETTINGS_READS = ("/api/settings", "/api/me", "/api/shifts", "/api/time-off", "/api/prep/schedule")
ADMIN_READS = (*SETTINGS_READS, "/api/people")
MANAGER_READS = ("/api/settings", "/api/me", "/api/people")   # a manager has no Shifts, Time off or 1:1 schedule


def with_manager(app, regions=("NASA", "APAC")) -> TestClient:  # noqa: F811
    """The admin moves Blake to EMEA and Casey to APAC (Avery and Dana have no region set, so NASA) and adds Parker
    Lane as a manager, who picks `regions`: Parker's client. With NASA and APAC Parker sees Avery, Casey and Dana, and
    the people with any of them are the admin (Avery and Casey), Kim (Casey) and Lee (Avery)."""
    admin = client_as(app, ADMIN)
    for tech, region in ((BLAKE, "EMEA"), (CASEY, "APAC")):
        moved = admin.put(f"/api/team/{tech}", json={"regions": [{"since": "2026-01-01", "region": region}]})
        assert moved.status_code == 200, moved.text
    added = admin.post("/api/people", json={"name": "Parker Lane", "email": MANAGER, "role": "manager"})
    assert added.status_code == 200, added.text
    manager = client_as(app, MANAGER)
    assert manager.put("/api/me/regions", json={"regions": list(regions)}).status_code == 200
    return manager


def named_lee(app) -> TestClient:  # noqa: F811
    """The admin, after giving Lee the name typed when a TL is added by name (Kim keeps none)."""
    admin = client_as(app, ADMIN)
    assert admin.post("/api/people", json={"email": LEE, "role": "tl", "name": "Lee Lead"}).status_code == 200
    return admin


PEOPLE_HELPERS = """
function techsEl() { return panelTitled("Techs"); }
// The admin's Users panel, or a manager's Team leads.
function leadsEl() { return panelTitled("Users") || panelTitled("Team leads"); }
function rowsOf(root, cls) {
  return root ? all(root, function (e) { return e.tagName === "TR" && e.classList.contains(cls); }) : [];
}
function buttonsOf(row) {
  return all(row, function (e) { return e.tagName === "BUTTON"; }).map(function (e) { return e.textContent; });
}
function shownRoster() {
  return rowsOf(techsEl(), "roster-row").map(function (r) {
    return { tech: notes(r)[0], lead: r.children[3].textContent, muted: r.children[3].classList.contains("muted"),
             buttons: buttonsOf(r) };
  });
}
function rosterRow(email) {
  return rowsOf(techsEl(), "roster-row").filter(function (r) { return notes(r)[0] === email; })[0] || null;
}
function personRows() { return rowsOf(leadsEl(), "person-row"); }
function personRow(who) {
  return personRows().filter(function (r) {
    return all(r, function (e) { return e.tagName === "B" && e.textContent === who; }).length;
  })[0] || null;
}
// A person's techs: one tag each, in a wrapping row of tags, or the muted line saying there are none.
function shownPeople() {
  return personRows().map(function (r) {
    var cell = r.children[1];
    var tags = all(cell, function (e) { return e.classList.contains("tag"); }).map(function (e) { return e.textContent; });
    return { who: all(r, function (e) { return e.tagName === "B"; })[0].textContent, notes: notes(r),
             techs: tags.length ? tags : cell.textContent, buttons: buttonsOf(r) };
  });
}
function leadsMessage() {
  return messageOf(all(leadsEl(), function (e) { return e.classList.contains("people-add"); })[0]);
}
function dialogEl() { return all(screenEl, function (e) { return e.getAttribute("role") === "dialog"; })[0] || null; }
function dialogText(cls) {
  return all(dialogEl(), function (e) { return e.classList.contains(cls); }).map(function (e) { return e.textContent; });
}
function picks() {
  return all(dialogEl(), function (e) { return e.tagName === "LI" && checks(e).length; }).map(function (li) {
    var box = checks(li)[0];
    var mark = all(li, function (e) { return e.classList.contains("note"); })[0];
    return { tech: box.getAttribute("value"), label: all(li, function (e) { return e.tagName === "LABEL"; })[0].textContent,
             ticked: !!box.checked, mark: mark ? mark.textContent : null };
  });
}
function tick(email, on) {
  var box = checks(dialogEl()).filter(function (c) { return c.getAttribute("value") === email; })[0];
  box.checked = on;
  fire(box, "change");
}
// The Techs panel's list and its add row: where each is, and whether their columns are the same.
function techsLayout() {
  var techs = techsEl();
  var add = all(techs, function (e) { return e.classList.contains("add-row"); })[0];
  var tables = all(techs, function (e) { return e.tagName === "TABLE"; });
  var list = tables[0].parentNode, foot = add.parentNode.parentNode;
  function cols(table) {
    return all(table, function (e) { return e.tagName === "COL"; }).map(function (c) { return c.className; });
  }
  return { list: list.className, addInList: !!add.closest(".list-scroll"), addTable: add.parentNode.className,
           foot: foot.className, footAfterList: list.parentNode.children.indexOf(foot) === list.parentNode.children.indexOf(list) + 1,
           sameCols: JSON.stringify(cols(tables[0])) === JSON.stringify(cols(add.parentNode)), cols: cols(tables[0]).length,
           heads: all(techs, function (e) { return e.tagName === "THEAD"; }).length,
           headInFoot: all(foot, function (e) { return e.tagName === "THEAD"; }).length };
}
// Shifts' and the 1:1 schedule's list box, and whether the panel's Save button is inside it.
function listLayout() {
  var out = {};
  [["Shifts", "Save shifts"], ["1:1 schedule", "Save schedule"]].forEach(function (pair) {
    var p = panelTitled(pair[0]);
    var table = all(p, function (e) { return e.tagName === "TABLE"; })[0];
    out[pair[0]] = [table.parentNode.className, !!buttonIn(p, pair[1]).closest(".list-scroll")];
  });
  return out;
}
function shiftEmails() {
  return all(panelTitled("Shifts"), function (e) { return e.tagName === "TR" && e.classList.contains("tech-row"); })
    .map(function (r) { return notes(r)[0]; });
}
"""


def test_admin_settings_panels_in_order(app):  # noqa: F811
    """Hosted Team design §6: the admin gets Shifts, Time off and the 1:1 schedule for their own team, plus Techs (with
    Import from Teams Shifts under it), Users (Managers design), Activity flags and Zendesk, in one row of two columns; the Shifts panel holds only the admin's techs, with no Add tech or Remove
    (the roster is the Techs panel's)."""
    out = run_page("#/settings", read_all(client_as(app, ADMIN), *ADMIN_READS), HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var shifts = panelTitled("Shifts"), off = panelTitled("Time off");
  return { panels: panels().map(titleOf),
           rows: screenEl.children.filter(function (e) { return e.classList.contains("panel-row"); }).map(function (row) {
             return [row.getAttribute("class"), row.children.map(function (col) { return panels(col).map(titleOf); })];
           }),
           shifts: shiftEmails(),
           shiftButtons: all(shifts, function (e) { return e.tagName === "BUTTON"; }).map(function (e) {
             return e.textContent; }),
           offTechs: all(field(off, "Tech"), function (e) { return e.tagName === "OPTION"; }).map(function (e) {
             return e.getAttribute("value"); }),
           schedule: all(panelTitled("1:1 schedule"), function (e) { return e.classList.contains("sched-row"); }).length,
           techs: techsLayout(), lists: listLayout() };
});
""")
    assert out["panels"] == ["Techs", "Import from Teams Shifts", "Shifts", "Time off", "1:1 schedule", "Users",
                             "Activity flags", "Zendesk and credentials"]
    assert out["rows"] == [
        ["panel-row cols-2-1 even", [["Techs", "Import from Teams Shifts", "Shifts", "Time off"],
                                     ["1:1 schedule", "Users", "Activity flags", "Zendesk and credentials"]]]], \
        "one row of two columns, so the right column carries on under the 1:1 schedule (owner, 2026-10-21)"
    assert out["techs"] == {
        "list": "scroll list-scroll tall", "addInList": False, "addTable": "atable roster", "foot": "list-foot",
        "footAfterList": True, "sameCols": True, "cols": 5, "heads": 1, "headInFoot": 0}, \
        "a long Techs list scrolls in a taller box; the add row is its own table, same columns, always under it"
    assert out["lists"] == {"Shifts": ["scroll list-scroll", False], "1:1 schedule": ["scroll list-scroll", False]}, \
        "Shifts and the 1:1 schedule scroll in their panels, their Save buttons under the list"
    assert out["shifts"] == [AVERY, CASEY], "their own team only"
    assert out["shiftButtons"] == ["Save shifts"]
    assert out["offTechs"] == [AVERY, CASEY]
    assert out["schedule"] == 2


def test_techs_panel_has_lead_select_and_no_shift_columns(app):  # noqa: F811
    """Hosted Team design §3: the Techs panel lists every tech the server follows with their team leads ("You" for the
    admin, "No team lead" for a tech on none) and Remove; its add row is a name, the email made from it, and a Team
    lead choice listing every person with "You" first and chosen. No work days or hours: each person sets their own
    team's shifts in Shifts."""
    admin = named_lee(app)
    responses = read_all(admin, *ADMIN_READS)
    # What the real API answers each change the page makes, and the reads after each, in order.
    added = admin.post("/api/team", json={"name": "Erin Park", "lead": LEE, "region": "NASA"})
    assert added.status_code == 200, added.text
    after_add = admin.get("/api/people").json()
    removed = admin.delete(f"/api/team/{DANA}").json()
    after_remove = admin.get("/api/people").json()
    responses["POST /api/team"] = answer(added.json())
    responses[f"DELETE /api/team/{DANA.replace('@', '%40')}"] = answer(removed)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + f"""
run(async function () {{
  var techs = techsEl();
  var add = all(techs, function (e) {{ return e.classList.contains("add-row"); }})[0];
  var lead = field(techs, "Team lead"), name = field(techs, "Tech name");
  var out = {{ sub: techs.children.filter(function (e) {{ return e.classList.contains("sub"); }})[0].textContent,
              head: all(techs, function (e) {{ return e.tagName === "TH"; }}).map(function (e) {{ return e.textContent; }}),
              roster: shownRoster(),
              boxes: checks(techs).length,
              times: all(techs, function (e) {{ return e.tagName === "INPUT" && e.getAttribute("type") === "time"; }}).length,
              options: all(lead, function (e) {{ return e.tagName === "OPTION"; }}).map(function (e) {{
                return [e.textContent, e.getAttribute("value")]; }}),
              chosen: lead.value }};
  name.value = "Erin Park";
  fire(name, "input");
  out.made = notes(add);
  lead.value = "{LEE}";
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(after_add)} }};
  click(techs, "Add tech");
  await settle();
  out.added = {{ roster: shownRoster(), message: messageOf(techsEl()), name: name.value, made: notes(add),
                chosen: lead.value }};
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(after_remove)} }};
  click(rosterRow("{DANA}"), "Remove");
  await settle();
  out.removed = {{ roster: shownRoster().map(function (r) {{ return r.tech; }}), message: messageOf(techsEl()) }};
  out.writes = writes();
  return out;
}});
""")
    assert out["sub"] == ("Every tech the server follows. Add a tech by name and pick their team lead; Edit techs under "
                          "Users can add them to more teams. Started is a tech's first day here: days before it "
                          "aren't tracked, so they're never a sick day.")
    assert out["head"] == ["Tech", "Started", "Region", "Team lead", "Edit or remove"]
    assert out["roster"] == [
        {"tech": AVERY, "lead": "You, Lee Lead", "muted": False, "buttons": ["Edit", "Remove"]},
        {"tech": BLAKE, "lead": f"{KIM}, Lee Lead", "muted": False, "buttons": ["Edit", "Remove"]},
        {"tech": CASEY, "lead": f"You, {KIM}", "muted": False, "buttons": ["Edit", "Remove"]},
        {"tech": DANA, "lead": "No team lead", "muted": True, "buttons": ["Edit", "Remove"]}]
    assert (out["boxes"], out["times"]) == (0, 0), "no work days or hours"
    assert out["options"] == [["You", ADMIN], [KIM, KIM], ["Lee Lead", LEE]], "every person, You first"
    assert out["chosen"] == ADMIN, "You starts chosen"
    assert out["made"] == [ERIN], "the email made from the name shows as it is typed"
    assert {"tech": ERIN, "lead": "Lee Lead", "muted": False, "buttons": ["Edit", "Remove"]} in out["added"]["roster"]
    assert out["added"]["message"] == [f"Added {ERIN}."]
    assert (out["added"]["name"], out["added"]["made"], out["added"]["chosen"]) == ("", [""], ADMIN), \
        "the add row starts over, with You chosen"
    assert DANA not in out["removed"]["roster"] and out["removed"]["message"] == [f"Removed {DANA}."]
    assert out["writes"] == [["POST", "/api/team", {"name": "Erin Park", "lead": LEE, "region": "NASA"}],
                             ["DELETE", f"/api/team/{DANA}", None]]


def test_techs_panel_add_for_self_refreshes_own_shifts(app):  # noqa: F811
    """A tech added with "You" as their lead joins the admin's own team at once: Shifts lists them too."""
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    added = admin.post("/api/team", json={"name": "Erin Park", "lead": ADMIN, "region": "NASA"}).json()
    after = read_all(admin, "/api/people", "/api/shifts", "/api/time-off", "/api/prep/schedule")
    responses["POST /api/team"] = answer(added)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + f"""
run(async function () {{
  var before = shiftEmails();
  field(techsEl(), "Tech name").value = "Erin Park";
  Object.assign(RESPONSES, {json.dumps(after)});
  click(techsEl(), "Add tech");
  await settle();
  return {{ before: before, after: shiftEmails(), writes: writes() }};
}});
""")
    assert out["before"] == [AVERY, CASEY]
    assert out["after"] == [AVERY, CASEY, ERIN]
    assert out["writes"] == [["POST", "/api/team", {"name": "Erin Park", "lead": ADMIN, "region": "NASA"}]]


REGION_HELPERS = """
function regionLis() {
  return all(dialogEl(), function (e) { return e.tagName === "LI" && !!field(e, "Region from"); });
}
function regionLines() {
  return regionLis().map(function (li) { return [field(li, "Region from").value, field(li, "Region").value]; });
}
function regionChoices(li) {
  return all(field(li, "Region"), function (e) { return e.tagName === "OPTION"; }).map(function (e) {
    return [e.textContent, e.getAttribute("value")]; });
}
function formLabels() {
  return all(dialogEl(), function (e) { return e.classList.contains("form-row"); }).map(function (r) {
    return r.children[0].textContent; });
}
function shownRegions() {
  return rowsOf(techsEl(), "roster-row").map(function (r) { return [notes(r)[0], r.children[2].textContent]; });
}
"""
REGION_CHOICES = [["NASA (Dallas time)", "NASA"], ["EMEA (Dubai time)", "EMEA"], ["APAC (Manila time)", "APAC"]]
SWAP = [{"since": "2026-06-01", "region": "APAC"}, {"since": "2026-10-12", "region": "EMEA"}]


def test_techs_edit_window_saves_name_start_regions_and_leads(app):  # noqa: F811
    """Techs' Edit (the owner, 2026-10-21): the tech's name, Started, regions (Regions design: one line per region
    change, a date and a region) and team leads in one window; the email shows but can't change. A tech with no
    region yet shows one line, NASA from their Started. Save sends them together, closes the window and says so."""
    admin = named_lee(app)
    responses = read_all(admin, *ADMIN_READS)
    regions = [{"since": "2026-09-01", "region": "APAC"}, {"since": "2026-10-12", "region": "NASA"}]
    saved = admin.put(f"/api/team/{BLAKE}", json={"name": "Blake Moreno", "started": "2026-09-14",
                                                   "leads": [ADMIN, KIM], "regions": regions}).json()
    responses[f"PUT /api/team/{BLAKE.replace('@', '%40')}"] = answer(saved)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + REGION_HELPERS + """
run(async function () {
  click(rosterRow("blake@example.com"), "Edit");
  var out = { title: dialogText("dialog-title"), sub: dialogText("sub"), labels: formLabels(),
              name: document.getElementById("edit-tech-name").value,
              started: document.getElementById("edit-tech-started").value,
              lines: regionLines(), choices: regionChoices(regionLis()[0]),
              leads: picks().map(function (p) { return [p.label, p.ticked]; }), buttons: buttonsOf(dialogEl()) };
  document.getElementById("edit-tech-name").value = "Blake Moreno";
  document.getElementById("edit-tech-started").value = "2026-09-14";
  field(regionLis()[0], "Region").value = "APAC";
  click(dialogEl(), "Add a region change");
  out.added = regionLines();
  field(regionLis()[1], "Region from").value = "2026-10-12";
  tick("lee.lead@example.com", false);
  tick("owner@example.com", true);
  click(dialogEl(), "Save");
  await settle();
  out.writes = writes();
  out.open = dialogEl() !== null;
  out.message = messageOf(techsEl());
  return out;
});
""")
    assert out["title"] == [f"Edit {BLAKE}"]
    assert out["sub"] == [f"{BLAKE} · The email can't change: it's how Zendesk and their history know them."]
    assert out["labels"] == ["Name", "Started", "Region", "Team leads"], "Region between Started and Team leads"
    assert (out["name"], out["started"]) == ("", "2026-09-01"), "no typed name yet; the first day of their shift"
    assert out["lines"] == [["2026-09-01", "NASA"]], "no region yet: NASA from their Started"
    assert out["choices"] == REGION_CHOICES
    assert out["added"] == [["2026-09-01", "APAC"], ["", "NASA"]], "a new line: no date yet, NASA"
    assert out["leads"] == [["You", False], [KIM, True], ["Lee Lead", True]]
    assert out["buttons"] == ["Remove", "Add a region change", "Cancel", "Save"]
    assert out["writes"] == [["PUT", f"/api/team/{BLAKE}", {"leads": [ADMIN, KIM], "name": "Blake Moreno",
                                                           "started": "2026-09-14", "regions": regions}]]
    assert out["open"] is False and out["message"] == ["Saved Blake Moreno."]


def test_techs_rows_show_each_techs_current_region_and_edit_lists_the_changes(app):  # noqa: F811
    """Regions design: each Techs row shows the tech's current region as text, after Started; Edit lists their region
    changes, oldest first. A save that leaves the regions as they were doesn't send them, so a tech never set stays
    unset (judged on the app's zone, as before regions)."""
    admin = client_as(app, ADMIN)
    assert admin.put(f"/api/team/{BLAKE}", json={"regions": SWAP}).status_code == 200
    responses = read_all(admin, *ADMIN_READS)
    for tech, leads in ((BLAKE, [KIM, LEE]), (AVERY, [ADMIN, LEE])):
        responses[f"PUT /api/team/{tech.replace('@', '%40')}"] = answer(
            admin.put(f"/api/team/{tech}", json={"leads": leads}).json())
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + REGION_HELPERS + """
run(async function () {
  var out = { rows: shownRegions() };
  click(rosterRow("blake@example.com"), "Edit");
  out.blake = regionLines();
  click(dialogEl(), "Save");
  await settle();
  click(rosterRow("avery@example.com"), "Edit");
  out.avery = regionLines();
  click(dialogEl(), "Save");
  await settle();
  out.writes = writes();
  return out;
});
""")
    assert out["rows"] == [[AVERY, "NASA"], [BLAKE, "EMEA"], [CASEY, "NASA"], [DANA, "NASA"]]
    assert out["blake"] == [["2026-06-01", "APAC"], ["2026-10-12", "EMEA"]]
    assert out["avery"] == [["2026-09-01", "NASA"]]
    assert out["writes"] == [["PUT", f"/api/team/{BLAKE}", {"leads": [KIM, LEE]}],
                             ["PUT", f"/api/team/{AVERY}", {"leads": [ADMIN, LEE]}]], "the regions stayed: not sent"


def test_techs_edit_region_lines_add_remove_and_need_their_dates(app):  # noqa: F811
    """Add a region change adds a line with no date, NASA; Remove takes a line away; a line without a date is refused
    in the window, which stays open and sends nothing; Save sends the whole list."""
    admin = client_as(app, ADMIN)
    assert admin.put(f"/api/team/{BLAKE}", json={"regions": SWAP}).status_code == 200
    responses = read_all(admin, *ADMIN_READS)
    after = [{"since": "2026-10-12", "region": "EMEA"}, {"since": "2026-11-02", "region": "NASA"}]
    responses[f"PUT /api/team/{BLAKE.replace('@', '%40')}"] = answer(
        admin.put(f"/api/team/{BLAKE}", json={"leads": [KIM, LEE], "regions": after}).json())
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + REGION_HELPERS + """
run(async function () {
  var out = {};
  click(rosterRow("blake@example.com"), "Edit");
  click(dialogEl(), "Add a region change");
  click(dialogEl(), "Save");
  await settle();
  out.refused = { open: dialogEl() !== null, message: messageOf(dialogEl()), writes: writes().length,
                  enabled: !buttonIn(dialogEl(), "Save").disabled };
  click(regionLis()[0], "Remove");
  out.removed = regionLines();
  field(regionLis()[1], "Region from").value = "2026-11-02";
  click(dialogEl(), "Save");
  await settle();
  out.writes = writes();
  out.open = dialogEl() !== null;
  return out;
});
""")
    assert out["refused"] == {"open": True, "message": ["Give each region change its date."], "writes": 0,
                              "enabled": True}
    assert out["removed"] == [["2026-10-12", "EMEA"], ["", "NASA"]]
    assert out["writes"] == [["PUT", f"/api/team/{BLAKE}", {"leads": [KIM, LEE], "regions": after}]]
    assert out["open"] is False


def test_team_leads_panel_edit_techs_dialog_saves_ticked_set(app):  # noqa: F811
    """Hosted Team design §3: each person, the admin first as "You", with their techs as a list, an Edit techs button
    on every row and Remove on the others' (the panel is Users since the Managers design). Edit techs opens a picker listing every tech, ticked for that person's
    team, the admin's own techs marked "Your tech"; Save replaces that person's whole team with what is ticked."""
    admin = named_lee(app)
    responses = read_all(admin, *ADMIN_READS)
    saved = admin.put(f"/api/people/{LEE}/techs", json={"techs": [AVERY, DANA]}).json()
    responses[f"PUT /api/people/{LEE.replace('@', '%40')}/techs"] = answer(saved)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var leads = leadsEl();
  var out = { head: all(leads, function (e) { return e.tagName === "TH"; }).map(function (e) { return e.textContent; }),
              people: shownPeople(), before: dialogEl(),
              tagRows: personRows().map(function (r) { return r.children[1].children[0].className; }) };
  click(personRow("Lee Lead"), "Edit techs");
  out.dialog = { title: dialogText("dialog-title"), sub: dialogText("sub"), picks: picks(),
                 buttons: buttonsOf(dialogEl()), modal: dialogEl().getAttribute("aria-modal") };
  tick("blake@example.com", false);
  tick("dana@example.com", true);
  click(dialogEl(), "Save");
  await settle();
  out.saved = { open: dialogEl() !== null, people: shownPeople()[2], message: leadsMessage(),
                roster: shownRoster().map(function (r) { return r.lead; }) };
  click(personRow("You"), "Edit techs");
  out.mine = { title: dialogText("dialog-title"), sub: dialogText("sub"),
               ticked: picks().filter(function (p) { return p.ticked; }).map(function (p) { return p.tech; }) };
  click(dialogEl(), "Cancel");
  out.cancelled = dialogEl();
  out.writes = writes();
  return out;
});
""")
    assert out["head"] == ["User", "What they see", "Edit techs or remove"]
    assert out["people"] == [
        {"who": "You", "notes": ["Admin"], "techs": [AVERY, CASEY], "buttons": ["Edit techs"]},
        {"who": KIM, "notes": ["TL"], "techs": [BLAKE, CASEY], "buttons": ["Edit techs", "Remove"]},
        {"who": "Lee Lead", "notes": [f"{LEE} · TL"], "techs": [AVERY, BLAKE], "buttons": ["Edit techs", "Remove"]}]
    assert out["tagRows"] == ["tags", "tags", "tags"], "each person's techs, one tag per tech, in a wrapping row"
    assert out["before"] is None
    assert out["dialog"] == {
        "title": ["Lee Lead's techs"], "sub": ["Tick the techs Lee sees and edits."],
        "picks": [{"tech": AVERY, "label": AVERY, "ticked": True, "mark": "Your tech"},
                  {"tech": BLAKE, "label": BLAKE, "ticked": True, "mark": None},
                  {"tech": CASEY, "label": CASEY, "ticked": False, "mark": "Your tech"},
                  {"tech": DANA, "label": DANA, "ticked": False, "mark": None}],
        "buttons": ["Cancel", "Save"], "modal": "true"}
    assert out["saved"]["open"] is False, "Save closes the picker"
    assert out["saved"]["people"]["techs"] == [AVERY, DANA]
    assert out["saved"]["message"] == ["Saved Lee Lead's techs."]
    assert out["saved"]["roster"] == ["You, Lee Lead", KIM, f"You, {KIM}", "Lee Lead"], "the Techs panel follows"
    assert out["mine"] == {"title": ["Your techs"], "sub": ["Tick the techs you see and edit."],
                           "ticked": [AVERY, CASEY]}
    assert out["cancelled"] is None, "Cancel closes it"
    assert out["writes"] == [["PUT", f"/api/people/{LEE}/techs", {"techs": [AVERY, DANA]}]], "Cancel sends nothing"


def test_admin_editing_own_techs_refreshes_their_own_panels(app):  # noqa: F811
    """Edit techs on the admin's own row changes their team, so Shifts, Time off and the 1:1 schedule are read
    again."""
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    saved = admin.put(f"/api/people/{ADMIN}/techs", json={"techs": [AVERY, BLAKE, CASEY]}).json()
    responses[f"PUT /api/people/{ADMIN.replace('@', '%40')}/techs"] = answer(saved)
    after = read_all(admin, "/api/settings", "/api/shifts", "/api/time-off", "/api/prep/schedule")
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + f"""
run(async function () {{
  click(personRow("You"), "Edit techs");
  tick("blake@example.com", true);
  Object.assign(RESPONSES, {json.dumps(after)});
  click(dialogEl(), "Save");
  await settle();
  return {{ shifts: shiftEmails(), message: leadsMessage(),
           off: all(field(panelTitled("Time off"), "Tech"), function (e) {{ return e.tagName === "OPTION"; }}).map(
             function (e) {{ return e.getAttribute("value"); }}),
           schedule: all(panelTitled("1:1 schedule"), function (e) {{ return e.classList.contains("sched-row"); }}).length,
           writes: writes() }};
}});
""")
    assert out["shifts"] == [AVERY, BLAKE, CASEY]
    assert out["off"] == [AVERY, BLAKE, CASEY]
    assert out["schedule"] == 3
    assert out["message"] == ["Saved your techs."]
    assert out["writes"] == [["PUT", f"/api/people/{ADMIN}/techs", {"techs": [AVERY, BLAKE, CASEY]}]]


def test_team_leads_panel_add_remove_and_refusals(app):  # noqa: F811
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    responses["POST /api/people"] = answer(admin.post("/api/people", json={"email": PAT, "role": "tl"}).json())
    responses[f"DELETE /api/people/{KIM.replace('@', '%40')}"] = answer(admin.delete(f"/api/people/{KIM}").json())
    responses[f"PUT /api/people/{LEE.replace('@', '%40')}/techs"] = answer(
        {"detail": "dana@example.com is not on the team."}, 400)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var leads = leadsEl();
  var out = {};
  click(leads, "Add");
  out.empty = leadsMessage();
  field(leads, "User name").value = "Pat Lead";
  field(leads, "User email").value = " Pat.Lead@Example.com ";
  click(leads, "Add");
  await settle();
  out.added = { people: shownPeople().map(function (p) { return p.who; }), message: leadsMessage(),
                email: field(leads, "User email").value,
                none: personRow("pat.lead@example.com").children[1].className,
                patTechs: shownPeople().filter(function (p) { return p.who === "pat.lead@example.com"; })[0].techs };
  click(personRow("kim.lead@example.com"), "Remove");
  await settle();
  out.removed = { people: shownPeople().map(function (p) { return p.who; }), message: leadsMessage() };
  click(personRow("lee.lead@example.com"), "Edit techs");
  tick("dana@example.com", true);
  click(dialogEl(), "Save");
  await settle();
  out.refused = { open: dialogEl() !== null, message: messageOf(dialogEl()),
                  enabled: !buttonIn(dialogEl(), "Save").disabled,
                  techs: shownPeople().filter(function (p) { return p.who === "lee.lead@example.com"; })[0].techs };
  out.writes = writes();
  return out;
});
""")
    assert out["empty"] == ["Enter their name."]
    assert out["added"]["people"] == ["You", KIM, LEE, PAT]
    assert out["added"]["message"] == [f"Added {PAT}. Click Edit techs to choose their techs."]
    assert out["added"]["email"] == ""
    assert (out["added"]["patTechs"], out["added"]["none"]) == ("No techs yet", "muted"), "no tags: the muted line"
    assert out["removed"] == {"people": ["You", LEE, PAT], "message": [f"Removed {KIM}."]}
    assert out["refused"] == {"open": True, "message": ["dana@example.com is not on the team."], "enabled": True,
                              "techs": [AVERY, BLAKE]}, "a refused save keeps the picker open and says why"
    assert out["writes"] == [
        ["POST", "/api/people", {"name": "Pat Lead", "email": "Pat.Lead@Example.com", "role": "tl"}],
        ["DELETE", f"/api/people/{KIM}", None],
        ["PUT", f"/api/people/{LEE}/techs", {"techs": [AVERY, BLAKE, DANA]}]]


def test_techs_and_team_leads_only_for_the_hosted_admin(app, tmp_path):  # noqa: F811
    from test_web_settings_app import responses_for

    pc = run_page("#/settings", responses_for(tmp_path), PEOPLE_HELPERS + """
run(async function () {
  return { techs: techsEl() !== null, leads: leadsEl() !== null, team: panelTitled("Team") !== null,
           reads: requests.map(function (r) { return r.url; }) };
});
""")
    assert pc == {"techs": False, "leads": False, "team": True, "reads": pc["reads"]}, "the PC keeps its Team panel"
    assert "/api/me" not in pc["reads"] and "/api/people" not in pc["reads"], "the PC copy reads neither"


def test_tl_settings_show_only_their_own_team_panels(app):  # noqa: F811
    out = run_page("#/settings", read_all(client_as(app, LEE), *SETTINGS_READS), HELPERS + """
run(async function () {
  var shifts = panelTitled("Shifts"), off = panelTitled("Time off");
  var rows = all(shifts, function (e) { return e.tagName === "TR" && e.classList.contains("tech-row"); });
  return {
    panels: panels().map(titleOf),
    row: shifts.parentNode.parentNode.getAttribute("class"),
    stacks: all(screenEl, function (e) { return e.classList.contains("stack"); }).map(function (s) {
      return s.children.map(titleOf); }),
    schedule: all(panelTitled("1:1 schedule"), function (e) { return e.tagName === "TR" && e.classList.contains("sched-row"); })
      .map(function (r) { return r.children[0].textContent; }),
    sub: shifts.children.filter(function (e) { return e.classList.contains("sub"); })[0].textContent,
    head: all(shifts, function (e) { return e.tagName === "TH"; }).map(function (e) { return e.textContent; }),
    rows: rows.map(function (r) { return notes(r)[0]; }),
    shiftButtons: all(shifts, function (e) { return e.tagName === "BUTTON"; }).map(function (e) { return e.textContent; }),
    addRows: all(shifts, function (e) { return e.classList.contains("add-row"); }).length,
    offTechs: all(field(off, "Tech"), function (e) { return e.tagName === "OPTION"; }).map(function (e) {
      return e.getAttribute("value"); }),
    offRows: all(off, function (e) { return e.classList.contains("off-row"); }).map(function (r) {
      return r.children[0].textContent; }),
    switches: all(screenEl, function (e) { return e.tagName === "INPUT" && e.getAttribute("type") === "number"; }).length,
    text: screenEl.textContent,
    reads: requests.map(function (r) { return r.url; })
  };
});
""")
    assert out["panels"] == ["Shifts", "Time off", "1:1 schedule"]
    assert out["row"] == "panel-row cols-2-1 even", "the admin's proportions: the shifts fit their panel at 4K (Eyes)"
    assert out["stacks"] == [["Shifts", "Time off"], ["1:1 schedule"]], "Shifts and Time off left, the schedule right"
    assert out["schedule"] == [AVERY, BLAKE], "the lead's own techs' 1:1 schedule rows"
    assert out["sub"] == ("Each of your techs' usual shift. A change starts from its effective-from date, so earlier "
                          "days keep the shift that applied then.")
    assert out["head"] == ["Tech", "Work days", "Hours", "Effective from"], "Started is the admin's, in Techs"
    assert out["rows"] == [AVERY, BLAKE]
    assert out["shiftButtons"] == ["Save shifts"], "no Add tech and no Remove: the roster is the admin's"
    assert out["addRows"] == 0
    assert out["offTechs"] == [AVERY, BLAKE]
    assert sorted(out["offRows"]) == [AVERY, BLAKE]
    assert out["switches"] == 2, "only the 1:1 schedule's two repeat fields: no flag limits"
    for hidden in ("Zendesk", "Sections", "Activity flags", "Team leads", "No team lead", CASEY, DANA):
        assert hidden not in out["text"], hidden
    assert "/api/people" not in out["reads"]


def test_tl_has_1_1_prep_for_their_own_techs(app):  # noqa: F811
    """1:1 prep is every person's on the hosted copy (hosted Team design §4): a TL's side panel has the link, its
    address opens the page, and the page lists only their techs."""
    from test_web_edition import HELPERS as EDITION_HELPERS

    lee = client_as(app, LEE)
    out = run_page("#/oneonone", read_all(lee, "/api/settings", "/api/me", "/api/prep"), EDITION_HELPERS + """
run(async function () {
  return { hash: location.hash, shown: shownLinks(), text: screenEl.textContent,
           reads: requests.map(function (r) { return r.url; }) };
});
""")
    assert out["hash"] == "#/oneonone"
    assert out["shown"] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"]
    assert "/api/prep" in out["reads"]
    assert AVERY in out["text"] and BLAKE in out["text"] and CASEY not in out["text"] and DANA not in out["text"]


def test_tl_saves_their_techs_1_1_schedules_through_the_scoped_route(app):  # noqa: F811
    """The 1:1 schedule panel of the hosted copy saves each changed tech through `PUT /api/prep/schedule/{email}`,
    never through the general settings save (which refuses the schedule); a cleared date sends a null date; a tech
    whose entry didn't change sends nothing; a refusal shows the server's message and keeps what was typed."""
    responses = read_all(client_as(app, LEE), *SETTINGS_READS)
    responses["PUT /api/prep/schedule/avery%40example.com"] = answer(
        {"email": AVERY, "date": "2026-10-20", "weeks": 4})
    responses["PUT /api/prep/schedule/blake%40example.com"] = answer(
        {"detail": "The repeat for blake@example.com must be a whole number of weeks from 1 to 8"}, 400)
    rows = json.loads(json.dumps(responses["GET /api/prep/schedule"]["body"]))
    rows[0].update(date="2026-10-20", weeks=4)
    out = run_page("#/settings", responses, HELPERS + SCHEDULE_HELPERS + f"""
run(async function () {{
  var out = {{}};
  field(schedRow("{AVERY}"), "A 1:1 date").value = "2026-10-20";
  field(schedRow("{AVERY}"), "Repeats every").value = "4";
  RESPONSES["GET /api/prep/schedule"] = {{ status: 200, body: {json.dumps(rows)} }};   // after the save
  click(schedEl(), "Save schedule");
  await settle();
  out.saved = {{ writes: writes(), message: messageOf(schedEl()), rows: schedRows().map(rowOut).map(function (r) {{
    return [r.name, r.date, r.weeks]; }}) }};
  click(schedEl(), "Save schedule");
  await settle();
  out.again = writes().length;
  field(schedRow("{AVERY}"), "A 1:1 date").value = "";
  field(schedRow("{BLAKE}"), "A 1:1 date").value = "2026-10-21";
  field(schedRow("{BLAKE}"), "Repeats every").value = "6";
  RESPONSES["PUT /api/prep/schedule/avery%40example.com"] = {{ status: 200, body: {{ email: "{AVERY}", date: null }} }};
  click(schedEl(), "Save schedule");
  await settle();
  out.second = {{ writes: writes().slice(1), message: messageOf(schedEl()),
                 typed: field(schedRow("{BLAKE}"), "Repeats every").value }};
  return out;
}});
""")
    assert out["saved"]["writes"] == [["PUT", f"/api/prep/schedule/{AVERY}", {"date": "2026-10-20", "weeks": 4}]]
    assert out["saved"]["message"] == ["Saved."]
    assert out["again"] == 1, "nothing changed: nothing sent, and never to /api/settings"
    assert out["second"]["writes"] == [["PUT", f"/api/prep/schedule/{AVERY}", {"date": None}],
                                       ["PUT", f"/api/prep/schedule/{BLAKE}", {"date": "2026-10-21", "weeks": 6}]]
    assert out["second"]["message"] == [
        "The repeat for blake@example.com must be a whole number of weeks from 1 to 8"]
    assert out["second"]["typed"] == "6", "a refused save keeps what was typed"


def test_tl_tech_picker_lists_only_their_techs(app):  # noqa: F811
    lee = client_as(app, LEE)
    responses = read_all(lee, "/api/settings", "/api/me", "/api/activity", "/api/activity/trends")
    out = run_page("#/team", responses, """
function options(id) {
  var select = all(screenEl, function (e) { return e.getAttribute("id") === id; })[0];
  return all(select, function (e) { return e.tagName === "OPTION"; }).map(function (e) { return e.textContent; });
}
run(async function () {
  var out = { activity: options("ta-tech"), text: screenEl.textContent };
  await go("#/team/trends");
  out.trends = screenEl.textContent;
  return out;
});
""")
    assert out["activity"] == ["All techs", AVERY, BLAKE]
    for text in (out["text"], out["trends"]):
        assert CASEY not in text and DANA not in text


def test_not_added_page_text(app):  # noqa: F811
    responses = read_all(client_as(app, STRANGER), "/api/settings")
    assert responses["GET /api/settings"]["status"] == 403
    out = run_page("", responses, """
run(async function () {
  var shown = all(screenEl, function (e) { return e.classList.contains("placeholder"); });
  var out = { text: screenEl.textContent, shown: shown.map(function (e) { return e.getAttribute("role"); }),
              errors: all(screenEl, function (e) { return e.classList.contains("error"); }).length,
              hash: location.hash, reads: requests.map(function (r) { return r.url; }) };
  await go("#/settings");
  out.after = { text: screenEl.textContent, reads: requests.length };
  return out;
});
""")
    assert out["text"] == NOT_ADDED_TEXT
    assert out["shown"] == ["status"] and out["errors"] == 0, "a plain page, not an error"
    assert out["hash"] == "" and out["reads"] == ["/api/settings"], "nothing routes and nothing else is read"
    assert out["after"] == {"text": NOT_ADDED_TEXT, "reads": 1}


def test_a_tl_removed_while_the_page_is_open_is_told_on_the_next_read(app):  # noqa: F811
    responses = read_all(client_as(app, LEE), "/api/settings", "/api/me", "/api/activity")
    responses["GET /api/activity/trends"] = answer({"error": "not_added", "email": LEE, "admin": ADMIN}, 403)
    out = run_page("#/team", responses, """
run(async function () {
  await go("#/team/trends");
  return all(screenEl, function (e) { return e.classList.contains("message"); }).map(function (e) {
    return e.textContent; });
});
""")
    assert f"You're signed in as {LEE}, but you haven't been added yet. Ask {ADMIN} to add you." in out


def test_team_leads_panel_links_the_access_policy(app):  # noqa: F811
    """Hosted design §4: the admin adds a TL in both places, and "the Settings page says so, with the link to the
    Access policy" (final review F1). The app doesn't know the Cloudflare account, so the link opens Cloudflare Zero
    Trust's dashboard, in a new tab. Since the Managers design the panel is Users, for team leads and managers."""
    out = run_page("#/settings", read_all(client_as(app, ADMIN), *ADMIN_READS), HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var sub = leadsEl().children.filter(function (e) { return e.classList.contains("sub"); })[0];
  return { text: sub.textContent, links: all(sub, function (e) { return e.tagName === "A"; }).map(function (a) {
    return { href: a.getAttribute("href"), target: a.getAttribute("target"), rel: a.getAttribute("rel"),
             text: a.textContent }; }) };
});
""")
    assert out["text"] == USERS_SUB
    assert out["links"] == [{"href": "https://one.dash.cloudflare.com/", "target": "_blank",
                             "rel": "noopener noreferrer", "text": "Cloudflare Access policy"}]


# -- managers (Managers design, plan Task 3) ------------------------------------------------------------------------

MANAGER_HELPERS = """
function subOf(p) { return p.children.filter(function (e) { return e.classList.contains("sub"); })[0] || null; }
function headsOf(p) { return all(p, function (e) { return e.tagName === "TH"; }).map(function (e) { return e.textContent; }); }
function addRow() { return all(techsEl(), function (e) { return e.classList.contains("add-row"); })[0]; }
function choices(select) {
  return all(select, function (e) { return e.tagName === "OPTION"; }).map(function (e) {
    return [e.textContent, e.getAttribute("value")]; });
}
// The Techs add row's Region and Team lead choices: the column the Region choice is in, its heading, and each choice's
// options and value.
function addChoices() {
  var add = addRow(), region = field(add, "Region"), lead = field(add, "Team lead");
  var column = add.children[0].children.indexOf(region.parentNode);
  return { column: column, head: headsOf(techsEl())[column], regions: choices(region), region: region.value,
           leads: choices(lead), lead: lead.value };
}
// Your regions: each region's tick box, its label, the note after it and whether it is ticked.
function regionPicks() {
  return all(panelTitled("Your regions"), function (e) { return e.tagName === "LI"; }).map(function (li) {
    var box = checks(li)[0];
    return [box.getAttribute("value"), all(li, function (e) { return e.tagName === "LABEL"; })[0].textContent,
            notes(li), !!box.checked];
  });
}
"""


def test_manager_settings_has_techs_your_regions_team_leads_and_the_note(app):  # noqa: F811
    """Managers design: a manager's Settings is Techs on the left (the techs in their regions), then Your regions, Team
    leads and the note on the right; no Shifts, Time off or 1:1 schedule (each team lead's), and none of them read."""
    manager = with_manager(app)
    out = run_page("#/settings", read_all(manager, *MANAGER_READS), HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + """
run(async function () {
  return { rows: screenEl.children.filter(function (e) { return e.classList.contains("panel-row"); }).map(function (row) {
             return [row.getAttribute("class"), row.children.map(function (col) {
               return panels(col).map(function (p) { return titleOf(p) === null ? ["note", p.textContent] : titleOf(p); });
             })];
           }),
           roster: shownRoster().map(function (r) { return r.tech; }),
           people: shownPeople().map(function (p) { return p.who; }),
           reads: requests.map(function (r) { return r.url; }) };
});
""")
    assert out["rows"] == [["panel-row cols-2-1 even", [["Techs"], ["Your regions", "Team leads",
                                                                    ["note", MANAGER_NOTE]]]]]
    assert out["roster"] == [AVERY, CASEY, DANA], "the techs in NASA and APAC"
    assert out["people"] == [ADMIN, KIM, LEE]
    assert sorted(set(out["reads"])) == ["/api/me", "/api/people", "/api/settings"], \
        "no Shifts, Time off or 1:1 schedule read"


def test_manager_picks_their_regions(app):  # noqa: F811
    """Your regions: a tick box for each region with its city's time, the manager's picks ticked; Save regions puts the
    ticked ones and says what they now see, and Techs, Team leads and the Techs add row's region follow."""
    manager = with_manager(app, ("APAC",))
    responses = read_all(manager, *MANAGER_READS)
    saved = manager.put("/api/me/regions", json={"regions": ["NASA", "APAC"]})
    responses["PUT /api/me/regions"] = answer(saved.json())
    after = manager.get("/api/people").json()
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  var regions = panelTitled("Your regions");
  var out = {{ sub: subOf(regions).textContent, picks: regionPicks(), buttons: buttonsOf(regions),
              roster: shownRoster().map(function (r) {{ return r.tech; }}),
              people: shownPeople().map(function (p) {{ return p.who; }}), region: field(addRow(), "Region").value }};
  checks(regions)[0].checked = true;
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(after)} }};
  click(regions, "Save regions");
  await settle();
  out.saved = {{ message: messageOf(regions), picks: regionPicks().map(function (p) {{ return p[3]; }}),
                roster: shownRoster().map(function (r) {{ return r.tech; }}),
                people: shownPeople().map(function (p) {{ return p.who; }}),
                region: field(addRow(), "Region").value, enabled: !buttonIn(regions, "Save regions").disabled }};
  out.writes = writes();
  return out;
}});
""")
    assert out["sub"] == REGIONS_SUB
    assert out["picks"] == [["NASA", "NASA", ["Dallas time"], False], ["EMEA", "EMEA", ["Dubai time"], False],
                            ["APAC", "APAC", ["Manila time"], True]]
    assert out["buttons"] == ["Save regions"]
    assert (out["roster"], out["people"], out["region"]) == ([CASEY], [ADMIN, KIM], "APAC")
    assert out["writes"] == [["PUT", "/api/me/regions", {"regions": ["NASA", "APAC"]}]]
    assert out["saved"] == {"message": ["Saved. You now see NASA and APAC."], "picks": [True, False, True],
                            "roster": [AVERY, CASEY, DANA], "people": [ADMIN, KIM, LEE], "region": "NASA",
                            "enabled": True}


def test_a_managers_teams_follow_their_people_from_start_and_after_save_regions(app):  # noqa: F811
    """Managers design: the teams in a manager's Tech picker come from their people, read at start, and read again
    when Save regions changes which techs (and so which teams) they see."""
    manager = with_manager(app, ("APAC",))
    responses = read_all(manager, *MANAGER_READS, "/api/activity")
    saved = manager.put("/api/me/regions", json={"regions": ["NASA", "APAC"]})
    responses["PUT /api/me/regions"] = answer(saved.json())
    after = {"GET /api/people": answer(manager.get("/api/people").json()),
             "GET /api/activity": answer(manager.get("/api/activity").json())}
    out = run_page("#/team", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  function teams() {{
    return document.getElementById("ta-tech").children.map(function (o) {{ return o.value; }})
      .filter(function (v) {{ return v.indexOf("team:") === 0; }});
  }}
  var out = {{ before: teams() }};
  await go("#/settings");
  var regions = panelTitled("Your regions");
  checks(regions)[0].checked = true;
  Object.assign(RESPONSES, {json.dumps(after)});
  click(regions, "Save regions");
  await settle();
  await go("#/team");
  out.after = teams();
  return out;
}});
""")
    assert out["before"] == ["team:" + KIM, "team:" + ADMIN], "APAC: Casey, on Kim's team and the admin's"
    assert out["after"] == ["team:" + LEE, "team:" + ADMIN, "team:" + KIM, "team:" + ADMIN], \
        "NASA too: Avery, on Lee's team and the admin's"


@pytest.mark.parametrize("shown", ["#/team", "#/settings"])
def test_a_managers_teams_follow_save_regions_whatever_screen_is_shown(app, shown):  # noqa: F811
    """Managers design (the review of plan Task 4): after Save regions the manager's people are read again and kept as
    their teams whatever screen is shown when the save is answered, even once Settings has been drawn again since (the
    manager left it and came back before the answer came, so the Settings that sent it is gone). Settings draws them
    only while it is the screen shown: on Activity it isn't drawn; back on Settings its Techs and Team leads follow."""
    manager = with_manager(app, ("APAC",))
    responses = read_all(manager, *MANAGER_READS, "/api/activity")
    saved = manager.put("/api/me/regions", json={"regions": ["NASA", "APAC"]})
    responses["PUT /api/me/regions"] = answer(saved.json())
    after = {"GET /api/people": answer(manager.get("/api/people").json()),
             "GET /api/activity": answer(manager.get("/api/activity").json())}
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  function teams() {{
    return document.getElementById("ta-tech").children.map(function (o) {{ return o.value; }})
      .filter(function (v) {{ return v.indexOf("team:") === 0; }});
  }}
  var regions = panelTitled("Your regions");
  checks(regions)[0].checked = true;
  var held = [];
  waitingRequests = held;
  click(regions, "Save regions");
  waitingRequests = null;
  await go("#/team");
  await go("#/settings");   // Settings drawn again, from the people as they were before the save
  await go({json.dumps(shown)});
  Object.assign(RESPONSES, {json.dumps(after)});
  var out = {{ held: held.map(function (r) {{ return r.url; }}) }};
  held.forEach(function (r) {{ r.answer(); }});
  await settle();
  out.settings = !!panelTitled("Your regions");
  out.roster = techsEl() ? shownRoster().map(function (r) {{ return r.tech; }}) : null;
  out.people = leadsEl() ? shownPeople().map(function (p) {{ return p.who; }}) : null;
  await go("#/team");
  out.teams = teams();
  return out;
}});
""")
    assert out["held"] == ["/api/me/regions"]
    assert out["teams"] == ["team:" + LEE, "team:" + ADMIN, "team:" + KIM, "team:" + ADMIN], \
        "NASA's teams too, though the Settings that saved was gone when the answer came"
    if shown == "#/team":
        assert (out["settings"], out["roster"], out["people"]) == (False, None, None), "Activity stays on show"
    else:
        assert out["settings"] is True
        assert (out["roster"], out["people"]) == ([AVERY, CASEY, DANA], [ADMIN, KIM, LEE]), \
            "the Settings on show draws the techs and team leads in NASA and APAC"


@pytest.mark.parametrize("shown", ["#/team", "#/settings"])
def test_a_managers_people_follow_a_roster_change_answered_after_settings_is_left(app, shown):  # noqa: F811
    """T-169: a manager adds a tech, leaves Settings and comes back (Settings drawn again, from the people as they
    were) before the server answers. Their people are still read again: kept as their teams, so Activity's Tech
    picker shows the new tech under their team lead's team, and drawn on the Settings shown when the answer comes."""
    manager = with_manager(app, ("NASA", "APAC"))
    responses = read_all(manager, *MANAGER_READS, "/api/activity")
    body = {"name": "Erin Park", "lead": LEE, "region": "APAC"}
    added = manager.post("/api/team", json=body)
    assert added.status_code == 200, added.text
    responses["POST /api/team"] = answer(added.json())
    after = {"GET /api/people": answer(manager.get("/api/people").json()),
             "GET /api/activity": answer(manager.get("/api/activity").json())}
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  function teams() {{
    return document.getElementById("ta-tech").children.map(function (o) {{ return o.value; }})
      .filter(function (v) {{ return v.indexOf("team:") === 0; }});
  }}
  field(techsEl(), "Tech name").value = "Erin Park";
  field(addRow(), "Team lead").value = "{LEE}";
  field(addRow(), "Region").value = "APAC";
  var held = [];
  waitingRequests = held;
  click(techsEl(), "Add tech");
  waitingRequests = null;
  await go("#/team");
  var out = {{ held: held.map(function (r) {{ return r.url; }}), before: teams() }};
  await go("#/settings");   // Settings drawn again, from the people as they were before the add
  await go({json.dumps(shown)});
  Object.assign(RESPONSES, {json.dumps(after)});
  held.forEach(function (r) {{ r.answer(); }});
  await settle();
  out.roster = techsEl() ? shownRoster().map(function (r) {{ return r.tech; }}) : null;
  await go("#/team");
  out.after = teams();
  out.writes = writes();
  return out;
}});
""")
    assert out["held"] == ["/api/team"]
    assert out["writes"] == [["POST", "/api/team", body]]
    assert out["before"] == ["team:" + LEE, "team:" + ADMIN, "team:" + KIM, "team:" + ADMIN]
    assert out["after"] == ["team:" + LEE, "team:" + ADMIN, "team:" + KIM, "team:" + LEE, "team:" + ADMIN],         "APAC now has Erin, on Lee's team, though the Settings that added her was gone when the answer came"
    if shown == "#/team":
        assert out["roster"] is None, "Activity stays on show"
    else:
        assert out["roster"] == [AVERY, CASEY, DANA, ERIN], "the Settings on show lists Erin"


def test_techs_edit_says_who_sets_a_missing_shift(app, db_path):  # noqa: F811
    """Techs' Edit for a tech with no shift on record (added before shifts were kept, or brought over without one):
    Started waits for their first shift. The admin is told to set it in Shifts; a manager, who sets no shifts (each team
    lead does), that their team lead sets it first."""
    manager = with_manager(app)
    added = manager.post("/api/team", json={"name": "Erin Park", "lead": LEE, "region": "NASA"})
    assert added.status_code == 200, added.text
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM shift_versions WHERE email = ?", (ERIN,))   # adding gives a shift from today
        conn.commit()
    finally:
        conn.close()
    out = {}
    for who, client, reads in (("admin", client_as(app, ADMIN), ADMIN_READS), ("manager", manager, MANAGER_READS)):
        out[who] = run_page("#/settings", read_all(client, *reads), HELPERS + PEOPLE_HELPERS + f"""
run(async function () {{
  click(rosterRow("{ERIN}"), "Edit");
  return {{ disabled: document.getElementById("edit-tech-started").disabled, hint: dialogText("hint") }};
}});
""")
    assert out["admin"] == {"disabled": True, "hint": ["Set their shift in Shifts first."]}
    assert out["manager"] == {"disabled": True, "hint": ["Their team lead sets their shift first."]}


def test_techs_add_row_has_a_region_choice_and_never_a_manager_as_lead(app):  # noqa: F811
    """Managers design: the Techs add row has a Region choice in the Region column showing each region's whole name,
    starting on NASA for the admin and on a manager's first region for them. Managers hold no team: the add row's Team
    lead choice and Edit's Team leads never list a manager, and a manager's never list "You". The add sends the region
    with the lead, and the add row starts over."""
    manager = with_manager(app, ("EMEA", "APAC"))
    admin = client_as(app, ADMIN)
    out = run_page("#/settings", read_all(admin, *ADMIN_READS), HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + """
run(async function () {
  var out = addChoices();
  click(rosterRow("avery@example.com"), "Edit");
  out.edit = picks().map(function (p) { return p.label; });
  return out;
});
""")
    assert out == {"column": 2, "head": "Region", "regions": ADD_ROW_REGIONS, "region": "NASA",
                   "leads": [["You", ADMIN], [KIM, KIM], [LEE, LEE]], "lead": ADMIN,
                   "edit": ["You", KIM, LEE]}, "Parker Lane, a manager, is in neither"

    responses = read_all(manager, *MANAGER_READS)
    added = manager.post("/api/team", json={"name": "Erin Park", "lead": LEE, "region": "APAC"})
    assert added.status_code == 200, added.text
    responses["POST /api/team"] = answer(added.json())
    after = manager.get("/api/people").json()
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  var out = addChoices();
  click(rosterRow("{BLAKE}"), "Edit");
  out.edit = picks().map(function (p) {{ return p.label; }});
  click(dialogEl(), "Cancel");
  field(techsEl(), "Tech name").value = "Erin Park";
  field(addRow(), "Region").value = "APAC";
  field(addRow(), "Team lead").value = "{LEE}";
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(after)} }};
  click(techsEl(), "Add tech");
  await settle();
  out.added = {{ message: messageOf(techsEl()), roster: shownRoster().map(function (r) {{ return r.tech; }}),
                region: field(addRow(), "Region").value, lead: field(addRow(), "Team lead").value }};
  out.writes = writes();
  return out;
}});
""")
    assert (out["column"], out["head"], out["regions"], out["region"]) == (2, "Region", ADD_ROW_REGIONS, "EMEA"), \
        "a manager's starts on their first region"
    assert (out["leads"], out["lead"]) == ([[KIM, KIM], [LEE, LEE], [ADMIN, ADMIN]], KIM), \
        "the people with a tech in their regions, by name: never You, never a manager"
    assert out["edit"] == [KIM, LEE, ADMIN]
    assert out["writes"] == [["POST", "/api/team", {"name": "Erin Park", "lead": LEE, "region": "APAC"}]]
    assert out["added"]["message"] == [f"Added {ERIN}."]
    assert sorted(out["added"]["roster"]) == sorted([BLAKE, CASEY, ERIN])
    assert (out["added"]["region"], out["added"]["lead"]) == ("EMEA", KIM), "the add row starts over"


def test_admin_users_panel_adds_team_leads_and_managers(app):  # noqa: F811
    """Managers design: the admin's panel is Users, for everyone who can sign in, with a Team lead or Manager choice. A
    manager's row shows their regions as tags (or that they have none yet) and no Edit techs: managers hold no team."""
    with_manager(app)
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    mo = admin.post("/api/people", json={"name": "Mo Grant", "email": MO, "role": "manager"})
    assert mo.status_code == 200, mo.text
    responses["POST /api/people"] = answer(mo.json())
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  var users = leadsEl();
  var role = field(users, "Role");
  var out = {{ title: titleOf(users), sub: subOf(users).textContent, links: links(subOf(users)), head: headsOf(users),
              roles: choices(role), role: role.value, people: shownPeople() }};
  field(users, "User name").value = "Mo Grant";
  field(users, "User email").value = "{MO}";
  role.value = "manager";
  click(users, "Add");
  await settle();
  out.added = {{ message: leadsMessage(), mo: shownPeople().filter(function (p) {{ return p.who === "Mo Grant"; }})[0],
                role: role.value }};
  out.writes = writes();
  return out;
}});
""")
    assert (out["title"], out["sub"]) == ("Users", USERS_SUB)
    assert out["links"] == [{"text": "Cloudflare Access policy", "href": "https://one.dash.cloudflare.com/"}]
    assert out["head"] == ["User", "What they see", "Edit techs or remove"]
    assert (out["roles"], out["role"]) == ([["Team lead", "tl"], ["Manager", "manager"]], "tl")
    assert out["people"] == [
        {"who": "You", "notes": ["Admin"], "techs": [AVERY, CASEY], "buttons": ["Edit techs"]},
        {"who": KIM, "notes": ["TL"], "techs": [BLAKE, CASEY], "buttons": ["Edit techs", "Remove"]},
        {"who": LEE, "notes": ["TL"], "techs": [AVERY, BLAKE], "buttons": ["Edit techs", "Remove"]},
        {"who": "Parker Lane", "notes": [f"{MANAGER} · Manager"], "techs": ["NASA", "APAC"], "buttons": ["Edit regions", "Remove"]}]
    assert out["writes"] == [["POST", "/api/people", {"name": "Mo Grant", "email": MO, "role": "manager"}]]
    assert out["added"] == {
        "message": [f"Added {MO}. Click Edit regions to choose their regions, or they pick them in their own Settings."],
        "mo": {"who": "Mo Grant", "notes": [f"{MO} · Manager"], "techs": "No regions yet", "buttons": ["Edit regions", "Remove"]},
        "role": "tl"}, "the add row starts over"


def test_manager_team_leads_panel_adds_team_leads_only(app):  # noqa: F811
    """Managers design: a manager's panel is Team leads (the admin and the leads with a tech in their regions, and the
    leads with none yet), with no role choice: they add team leads only, and remove only team leads."""
    manager = with_manager(app)
    responses = read_all(manager, *MANAGER_READS)
    pat = manager.post("/api/people", json={"name": "Pat Lead", "email": PAT, "role": "tl"})
    assert pat.status_code == 200, pat.text
    responses["POST /api/people"] = answer(pat.json())
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + MANAGER_HELPERS + f"""
run(async function () {{
  var leads = leadsEl();
  var out = {{ title: titleOf(leads), sub: subOf(leads).textContent, head: headsOf(leads), role: !!field(leads, "Role"),
              people: shownPeople(), add: buttonsOf(all(leads, function (e) {{
                return e.classList.contains("people-add"); }})[0]) }};
  click(leads, "Add team lead");
  out.empty = leadsMessage();
  field(leads, "Team lead name").value = "Pat Lead";
  field(leads, "Team lead email").value = "{PAT}";
  click(leads, "Add team lead");
  await settle();
  out.added = {{ message: leadsMessage(), pat: shownPeople().filter(function (p) {{ return p.who === "Pat Lead"; }})[0] }};
  out.writes = writes();
  return out;
}});
""")
    assert (out["title"], out["sub"], out["role"]) == ("Team leads", LEADS_SUB, False)
    assert out["head"] == ["Lead", "Techs they see", "Edit techs or remove"]
    assert out["add"] == ["Add team lead"]
    assert out["people"] == [
        {"who": ADMIN, "notes": ["Admin"], "techs": [AVERY, CASEY], "buttons": ["Edit techs"]},
        {"who": KIM, "notes": ["TL"], "techs": [CASEY], "buttons": ["Edit techs", "Remove"]},
        {"who": LEE, "notes": ["TL"], "techs": [AVERY], "buttons": ["Edit techs", "Remove"]}], \
        "their techs only; Remove only on team leads"
    assert out["empty"] == ["Enter the team lead's name."]
    assert out["writes"] == [["POST", "/api/people", {"name": "Pat Lead", "email": PAT, "role": "tl"}]]
    assert out["added"] == {
        "message": [f"Added {PAT}. Click Edit techs to choose their techs."],
        "pat": {"who": "Pat Lead", "notes": [f"{PAT} · TL"], "techs": "No techs yet", "buttons": ["Edit techs", "Remove"]}}


def test_manager_edit_that_moves_a_tech_out_says_so(app):  # noqa: F811
    """Managers design: a manager may move a tech to any region; when the save answers `moved_out` the tech has left
    their view, and the message says where they are now."""
    admin = client_as(app, ADMIN)
    assert admin.put(f"/api/team/{AVERY}", json={"name": "Avery Cole"}).status_code == 200
    manager = with_manager(app)
    responses = read_all(manager, *MANAGER_READS)
    moved = manager.put(f"/api/team/{AVERY}", json={"leads": [LEE, ADMIN],
                                                    "regions": [{"since": "2026-09-01", "region": "EMEA"}]})
    assert moved.status_code == 200 and moved.json()["moved_out"] is True, moved.text
    responses[f"PUT /api/team/{AVERY.replace('@', '%40')}"] = answer(moved.json())
    after = manager.get("/api/people").json()
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + REGION_HELPERS + f"""
run(async function () {{
  click(rosterRow("{AVERY}"), "Edit");
  var out = {{ leads: picks().map(function (p) {{ return [p.label, p.ticked]; }}), lines: regionLines() }};
  field(regionLis()[0], "Region").value = "EMEA";
  RESPONSES["GET /api/people"] = {{ status: 200, body: {json.dumps(after)} }};
  click(dialogEl(), "Save");
  await settle();
  out.open = dialogEl() !== null;
  out.message = messageOf(techsEl());
  out.roster = shownRoster().map(function (r) {{ return r.tech; }});
  out.writes = writes();
  return out;
}});
""")
    assert out["leads"] == [[KIM, False], [LEE, True], [ADMIN, True]]
    assert out["lines"] == [["2026-09-01", "NASA"]]
    assert out["writes"] == [["PUT", f"/api/team/{AVERY}", {
        "leads": [LEE, ADMIN], "regions": [{"since": "2026-09-01", "region": "EMEA"}]}]]
    assert out["open"] is False
    assert out["message"] == ["Avery Cole is in EMEA now, outside your regions."]
    assert out["roster"] == [CASEY, DANA]


EMAIL_NOTES = """
function parts(note) { return note.childNodes.map(function (n) { return n.tagName || n.textContent; }); }
function emailNotes(root, rowClass) {
  return all(root, function (e) { return e.tagName === "TR" && e.classList.contains(rowClass); }).map(function (r) {
    var note = all(r, function (e) { return e.classList.contains("note"); })[0];
    return { text: note.textContent, parts: parts(note) };
  });
}
"""


def broken_after_at(email: str, after: tuple = ()) -> dict:
    """A note holding `email` as the page shows it under a name: a line may break just after the "@" (a wbr, which
    copies as nothing), so the text is still the plain address."""
    local, domain = email.split("@")
    return {"text": email + "".join(after), "parts": [local + "@", "WBR", domain, *after]}


def test_an_email_under_a_name_may_break_after_its_at(app):  # noqa: F811
    """Final review F2 (ruling R12): a long real address under a tech's or a TL's name must not widen its column, which
    made the Team and Shifts tables scroll inside their panels at 4K. Techs and Shifts (the admin's), Team leads and a
    TL's Shifts each let the line break right after the "@", and nowhere else."""
    admin = client_as(app, ADMIN)
    assert admin.post("/api/people", json={"name": "Pat Lead", "email": PAT, "role": "tl"}).status_code == 200
    out = run_page("#/settings", read_all(admin, *ADMIN_READS),
                   HELPERS + PEOPLE_HELPERS + EMAIL_NOTES + """
run(async function () {
  return { techs: emailNotes(techsEl(), "roster-row"), shifts: emailNotes(panelTitled("Shifts"), "tech-row"),
           people: emailNotes(leadsEl(), "person-row") };
});
""")
    assert out["techs"] == [broken_after_at(tech) for tech in (AVERY, BLAKE, CASEY, DANA)]
    assert out["shifts"] == [broken_after_at(tech) for tech in (AVERY, CASEY)]
    assert out["people"] == [{"text": "Admin", "parts": ["Admin"]}, {"text": "TL", "parts": ["TL"]},
                             {"text": "TL", "parts": ["TL"]}, broken_after_at(PAT, (" · ", "TL"))], \
        "only a person added by name shows their email under it"
    lead = run_page("#/settings", read_all(client_as(app, LEE), *SETTINGS_READS), HELPERS + EMAIL_NOTES + """
run(async function () { return emailNotes(panelTitled("Shifts"), "tech-row"); });
""")
    assert lead == [broken_after_at(AVERY), broken_after_at(BLAKE)]


def test_tags_and_the_taller_list_are_shared_parts_built_from_tokens():
    """T-143: a tag is a small rounded label on the panel's subtle background, in ink at the small size, in a row of
    tags that wraps; the Techs list's box is about half as tall again as any other list's; the add row's table under
    it keeps the list's columns (fixed widths from their colgroups, the scroll bar's room kept on both)."""
    css = app_css()
    assert values(css, ".tag", "background") == ["var(--inset)"]
    assert values(css, ".tag", "color") == ["var(--ink)"]
    assert values(css, ".tag", "font-size") == ["var(--fs-sm)"]
    assert values(css, ".tag", "border-radius") == ["var(--radius-sm)"]
    assert values(css, ".tags", "flex-wrap") == ["wrap"]
    # From 1024px; below it no list scrolls inside the page (iPad and phone design §9, tests/test_web_cards.py).
    assert values(top_level(css), ".list-scroll.tall", "max-height") == ["var(--list-max-tall)"]
    assert px(css, "var(--list-max-tall)") == 1.5 * px(css, "var(--list-max)")
    assert values(css, ".atable.roster", "table-layout") == ["fixed"]
    assert values(css, ".list-scroll:has(+ .list-foot)", "scrollbar-gutter") == ["stable"]
    assert values(css, ".list-foot", "scrollbar-gutter") == ["stable"]
    for rule in (".tag", ".tags", ".list-foot", ".list-scroll.tall"):
        assert not any(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body) for body in rules(css, rule)), rule
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'class: type === "number" ? "pts" : "tags"' not in js, "the rubric's tag field no longer takes .tags"


def test_the_region_column_and_the_note_panel_are_built_from_tokens():
    """Managers design: the Techs tables' Region column is wide enough for the add row's Region choice, a region's
    whole name and its arrow (7em); a panel that only holds a note (a manager's Settings) is a shared part, the note
    with none of a paragraph's own margins, so the panel's padding is all its room."""
    css = app_css()
    assert tokens(css)["roster-region-w"] == "7em"
    assert values(css, ".atable.roster col.col-region", "width") == ["var(--roster-region-w)"]
    assert values(css, ".panel > .note:only-child", "margin") == ["0"]


# -- the Settings layout (Managers design, plan Task 5) -------------------------------------------------------------

# Each panel row of the screen: its classes, and each column's panels, each as its title ("note" for the note panel),
# the classes it carries besides "panel", and the classes of the list box directly in it (null when it has none).
EVEN_HELPERS = """
function evenLayout() {
  return screenEl.children.filter(function (e) { return e.classList.contains("panel-row"); }).map(function (row) {
    return [row.getAttribute("class"), row.children.map(function (col) {
      return panels(col).map(function (p) {
        var box = p.children.filter(function (e) { return e.classList.contains("list-scroll"); })[0];
        return [titleOf(p) || "note", p.className.split(" ").filter(function (c) { return c && c !== "panel"; }).sort(),
                box ? box.className : null];
      });
    })];
  });
}
"""
EVEN = "panel-row cols-2-1 even"
LIST, TALL = "scroll list-scroll", "scroll list-scroll tall"


def test_every_hosted_settings_view_is_one_even_row_with_fixed_list_boxes(app):  # noqa: F811
    """Managers design, the Settings layout: every hosted person's Settings is one even row of two columns. In each
    column one panel (.fill) takes the room left; the others' lists keep their fixed boxes, .roomy ones a few entries'
    room and .roomy.lg most of a team's. Every list is in a box that scrolls (`list-scroll`), Users and Team leads
    included; Import from Teams Shifts has no list of its own, so it keeps its own height."""
    run = HELPERS + PEOPLE_HELPERS + EVEN_HELPERS + "run(async function () { return evenLayout(); });"
    admin = run_page("#/settings", read_all(client_as(app, ADMIN), *ADMIN_READS), run)
    assert admin == [[EVEN, [
        [["Techs", [], TALL], ["Import from Teams Shifts", [], None], ["Shifts", ["lg", "roomy"], LIST],
         ["Time off", ["fill"], LIST]],
        [["1:1 schedule", ["lg", "roomy"], LIST], ["Users", ["fill", "lg", "roomy"], LIST], ["Activity flags", [], None],
         ["Zendesk and credentials", [], None]]]]], "the admin's: Time off and Users take the room left"
    lead = run_page("#/settings", read_all(client_as(app, LEE), *SETTINGS_READS), run)
    assert lead == [[EVEN, [[["Shifts", ["fill"], LIST], ["Time off", ["roomy"], LIST]],
                            [["1:1 schedule", ["fill"], LIST]]]]], "a team lead's: Shifts and the 1:1 schedule"
    manager = run_page("#/settings", read_all(with_manager(app), *MANAGER_READS), run)
    assert manager == [[EVEN, [[["Techs", ["fill"], TALL]],
                               [["Your regions", [], None], ["Team leads", ["fill"], LIST], ["note", [], None]]]]], \
        "a manager's: Techs and Team leads"


def test_the_even_row_and_its_fixed_list_boxes_are_shared_parts_from_1024px():
    """Managers design, the Settings layout: from 1024px an even row's two columns stretch to the row, which reaches at
    least the window's foot (less the page above and below it, --even-reserve), so their last panels end level; every
    list in it has a fixed box (height, not max-height) that scrolls, --list-max, a .tall list's --list-max-tall, a
    .roomy panel's --list-roomy and a .roomy.lg one's --list-roomy-lg, so adding an entry never moves a panel; a .fill
    panel takes the room its column leaves, its list growing from that box's height (flex basis 0, so its entries never
    size it). Below 1024px none of it applies: the columns stack and each list keeps its max-height."""
    css = app_css()
    found = tokens(css)
    assert (found["even-reserve"], found["list-roomy"], found["list-roomy-lg"]) == ("120px", "240px", "480px")
    assert px(css, "var(--list-roomy-lg)") == 2 * px(css, "var(--list-roomy)")
    wide = media_block(css, "(min-width: 1024px)")
    assert values(wide, ".panel-row.even", "align-items") == ["stretch"]
    assert values(wide, ".panel-row.even", "min-height") == ["calc(100vh - var(--even-reserve))"]
    even = ".panel-row.even > .stack > "
    assert values(wide, even + ".panel > .list-scroll", "max-height") == ["none"]
    assert values(wide, even + ".panel > .list-scroll", "overflow-y") == ["auto"]
    for box, height in ((".panel > .list-scroll", "var(--list-max)"),
                        (".panel > .list-scroll.tall", "var(--list-max-tall)"),
                        (".roomy > .list-scroll", "var(--list-roomy)"),
                        (".roomy.lg > .list-scroll", "var(--list-roomy-lg)")):
        assert values(wide, even + box, "height") == [height], box
    assert (values(wide, even + ".fill", "flex"), values(wide, even + ".fill", "display"),
            values(wide, even + ".fill", "flex-direction")) == (["1 1 auto"], ["flex"], ["column"])
    assert values(wide, even + ".fill > .list-scroll", "flex") == ["1 1 0"]
    for box, least in ((".fill > .list-scroll", "var(--list-max)"),
                       (".fill > .list-scroll.tall", "var(--list-max-tall)"),
                       (".fill.roomy > .list-scroll", "var(--list-roomy)"),
                       (".fill.roomy.lg > .list-scroll", "var(--list-roomy-lg)")):
        assert values(wide, even + box, "height") == ["auto"], box
        assert values(wide, even + box, "min-height") == [least], box
    assert css.count(".panel-row.even") == wide.count(".panel-row.even") > 0, "the even row's rules are all from 1024px"
    assert ".panel-row.even" not in top_level(css)
    for body in re.findall(r"\.panel-row\.even[^{]*\{([^}]*)\}", wide):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|\d+px", body), "tokens only: " + body


def test_the_admin_edits_a_managers_regions(app):  # noqa: F811
    """The owner, 2026-10-22: the admin sets a manager's regions from Users, as Edit techs sets a lead's team (the
    manager can still change them in their own Settings)."""
    admin = client_as(app, ADMIN)
    assert admin.post("/api/people", json={"name": "Parker Lane", "email": "parker.lane@example.com",
                                           "role": "manager"}).status_code == 200
    admin.put("/api/people/parker.lane@example.com/regions", json={"regions": ["NASA"]})
    responses = read_all(admin, *ADMIN_READS)
    saved = admin.put("/api/people/parker.lane@example.com/regions", json={"regions": ["NASA", "APAC"]}).json()
    responses["PUT /api/people/parker.lane%40example.com/regions"] = answer(saved)
    out = run_page("#/settings", responses, HELPERS + PEOPLE_HELPERS + """
run(async function () {
  var out = { buttons: buttonsOf(personRow("Parker Lane")) };
  click(personRow("Parker Lane"), "Edit regions");
  out.dialog = { title: dialogText("dialog-title"), sub: dialogText("sub"),
                 picks: picks().map(function (p) { return [p.tech, p.ticked, p.mark]; }) };
  tick("APAC", true);
  click(dialogEl(), "Save");
  await settle();
  out.open = dialogEl() !== null;
  out.message = leadsMessage();
  out.writes = writes();
  return out;
});
""")
    assert out["buttons"] == ["Edit regions", "Remove"]
    assert out["dialog"] == {
        "title": ["Parker Lane's regions"],
        "sub": ["Tick the regions Parker sees. They can change these in their own Settings too."],
        "picks": [["NASA", True, "Dallas time"], ["EMEA", False, "Dubai time"], ["APAC", False, "Manila time"]]}
    assert out["writes"] == [["PUT", "/api/people/parker.lane@example.com/regions", {"regions": ["NASA", "APAC"]}]]
    assert out["open"] is False
    assert out["message"] == ["Saved Parker Lane's regions."]
