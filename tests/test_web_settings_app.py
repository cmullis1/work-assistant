"""The app-wide Settings screen (Team activity design §9, Task 12): Team with each tech's shift, Time off and the
activity flag thresholds on top, Zendesk and credentials, and the Call QA panels under their own heading. app.js
runs in node on the fake DOM (tests/fake_dom.js) through the stand-in page of test_web_test_view, with the
endpoints answered from fixtures in the shapes the real ones return. Nothing here calls Zendesk or Anthropic."""
from __future__ import annotations

import json
import re
from datetime import date

from css_rules import STATIC, app_css, values
from test_compare import api_for
from test_web_test_view import answer, page_responses, run_page
from work_assistant.oneonone import report
from work_assistant.settings import set_setting

AVERY, BLAKE, DANA = "avery@example.com", "blake@example.com", "dana@example.com"
NINE_TO_FIVE = {"start": "08:00", "end": "17:00"}

# The page's reads for Settings with two techs on the team: Avery has a saved shift, Blake none yet (ruling 12).
SHIFTS = {"techs": [
    {"email": AVERY, "name": "Avery Cole", "versions": [
        {"effective_from": "2026-08-01", "work_days": [1, 2, 3, 4, 5], "active": True, **NINE_TO_FIVE}]},
    {"email": BLAKE, "name": "Blake Reed", "versions": []}]}
TIME_OFF = {"items": [
    {"id": 7, "email": AVERY, "first": "2026-10-12", "last": "2026-10-12", "note": "PTO", "kind": "pto"},
    {"id": 8, "email": BLAKE, "first": "2026-10-12", "last": "2026-10-14", "note": "", "kind": "sick"}]}
# GET /api/prep/schedule (oneonone.report.schedule_rows): Avery meets every 2 weeks from Oct 20, Blake has no 1:1 date.
UNSCHEDULED = {"date": None, "weeks": None, "latest_label": None, "next_label": None, "covers": None}
SCHEDULE_ROWS = [
    {"email": AVERY, "name": "Avery Cole", "date": "2026-10-20", "weeks": 2, "latest_label": "Tue Oct 20",
     "next_label": "Tue Nov 3", "covers": "10/20 – 11/2"},
    {"email": BLAKE, "name": "Blake Reed", **UNSCHEDULED}]
# A schedule entry for someone no longer on the team: kept, but ignored (spec §3).
GONE = {"gone@example.com": {"date": "2026-09-01", "weeks": 4}}
SCHEDULE_NOTE = ("Changing a date or the repeat takes effect at once. A tech added to the team needs a 1:1 date before "
                 "their preps start.")

HELPERS = """
var TODAY = new Date().toISOString().slice(0, 10);   // the settings below name UTC as the owner's zone
function dayShift(iso, n) {
  var d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
var RETENTION_START = dayShift(TODAY, -90);
function teamEl() { return panelTitled("Team"); }
function techRows() {
  return all(teamEl(), function (e) { return e.tagName === "TR" && e.classList.contains("tech-row"); });
}
function techRow(email) {
  return techRows().filter(function (r) {
    return all(r, function (e) { return e.classList.contains("note") && e.textContent === email; }).length;
  })[0];
}
function field(root, label) { return all(root, function (e) { return e.getAttribute("aria-label") === label; })[0]; }
function checks(root) {
  return all(root, function (e) { return e.tagName === "INPUT" && e.getAttribute("type") === "checkbox"; });
}
function days(root) {
  return checks(root).map(function (c, i) { return c.checked ? i : null; }).filter(function (i) { return i !== null; });
}
function setDays(root, picked) { checks(root).forEach(function (c, i) { c.checked = picked.indexOf(i) >= 0; }); }
function notes(root) {
  return all(root, function (e) { return e.classList.contains("note"); }).map(function (e) { return e.textContent; });
}
function messageOf(root) {
  return all(root, function (e) { return e.classList.contains("message"); }).map(function (e) { return e.textContent; })
    .filter(Boolean);
}
function writes() {
  return requests.filter(function (r) { return r.method !== "GET"; }).map(function (r) {
    return [r.method, decodeURIComponent(r.url), r.body];
  });
}
function click(root, text) { fire(buttonIn(root, text), "click"); }
"""


def settings_body(tmp_path, *, team=(AVERY, BLAKE)) -> dict:
    body = api_for(tmp_path).get("/api/settings").json()
    body.update(team=list(team), timezone="UTC", timezone_effective="UTC", retention_days=90,
                oneonone_schedule={AVERY: {"date": "2026-10-20", "weeks": 2}, **GONE})
    return body


def responses_for(tmp_path, *, team=(AVERY, BLAKE), shifts=SHIFTS, time_off=TIME_OFF,
                  schedule=SCHEDULE_ROWS) -> dict:
    responses = page_responses(api_for(tmp_path))
    responses["GET /api/settings"] = answer(settings_body(tmp_path, team=team))
    responses["GET /api/shifts"] = answer(shifts)
    responses["GET /api/time-off"] = answer(time_off)
    responses["GET /api/prep/schedule"] = answer(schedule)
    return responses


def shifts_after(email, name, work_days, start="08:00", end="17:00", effective="2026-10-19") -> dict:
    return {"email": email, "name": name, "versions": [
        {"effective_from": effective, "work_days": work_days, "start": start, "end": end, "active": True}]}


# -- the Team table --------------------------------------------------------------------------------------


STARTED_NOTE = " Started is a tech's first day here: days before it aren't tracked, so they're never a sick day."


def test_team_table_shows_each_techs_shift_and_unsaved_rows(tmp_path):
    out = run_page("#/settings", responses_for(tmp_path), HELPERS + """
run(async function () {
  var avery = techRow("avery@example.com"), blake = techRow("blake@example.com");
  var head = all(teamEl(), function (e) { return e.tagName === "TH"; }).map(function (e) { return e.textContent; });
  return {
    head: head, sub: teamEl().children.filter(function (e) { return e.classList.contains("sub"); })[0].textContent,
    names: techRows().map(function (r) { return all(r, function (e) { return e.tagName === "B"; })[0].textContent; }),
    avery: { days: days(avery), start: field(avery, "Start time").value, end: field(avery, "End time").value,
             date: field(avery, "Effective from").value, notes: notes(avery),
             types: [field(avery, "Start time").getAttribute("type"), field(avery, "Effective from").getAttribute("type")] },
    blake: { days: days(blake), start: field(blake, "Start time").value, end: field(blake, "End time").value,
             date: field(blake, "Effective from").value, notes: notes(blake) },
    today: TODAY, retention: RETENTION_START, writes: writes(),
    pickClass: all(avery, function (e) { return e.classList.contains("days-pick"); }).length,
    removes: buttonIn(avery, "Remove").classList.contains("small"),
    dayInitials: all(avery, function (e) { return e.tagName === "LABEL"; }).map(function (e) { return e.textContent; })
  };
});
""")
    assert out["head"][:6] == ["Tech", "Started", "Work days", "Hours", "Effective from", ""] or \
        out["head"][:5] == ["Tech", "Started", "Work days", "Hours", "Effective from"]
    assert out["sub"] == ("Who the app follows, and each tech's usual shift. A change starts from its effective-from "
                          "date, so earlier days keep the shift that applied then." + STARTED_NOTE)
    assert out["names"] == ["Avery Cole", "Blake Reed"]
    assert out["avery"]["days"] == [1, 2, 3, 4, 5]
    assert (out["avery"]["start"], out["avery"]["end"]) == ("08:00", "17:00")
    assert out["avery"]["date"] == out["today"], "a tech with a shift offers today as the effective-from date"
    assert out["avery"]["notes"] == [AVERY]
    assert out["avery"]["types"] == ["time", "date"]
    # Ruling 12: no versions means an unsaved row, pre-filled Sun-Thu 08:00-17:00 from the retention window's start.
    assert out["blake"]["days"] == [0, 1, 2, 3, 4]
    assert (out["blake"]["start"], out["blake"]["end"]) == ("08:00", "17:00")
    assert out["blake"]["date"] == out["retention"]
    assert out["blake"]["notes"] == [BLAKE, "Not saved yet"]
    assert out["pickClass"] == 1 and out["removes"]
    assert out["dayInitials"] == ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
    assert out["writes"] == [], "nothing is stored until the owner clicks Save team"


def test_team_row_saves_shift_version(tmp_path):
    responses = responses_for(tmp_path)
    responses["PUT /api/shifts/avery%40example.com"] = answer(shifts_after(AVERY, "Avery Cole", [1, 2, 3, 4], end="16:00"))
    responses["PUT /api/shifts/blake%40example.com"] = answer(shifts_after(BLAKE, "Blake Reed", [0, 1, 2, 3, 4]))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var avery = techRow("avery@example.com");
  setDays(avery, [1, 2, 3, 4]);
  field(avery, "End time").value = "16:00";
  click(teamEl(), "Save team");
  await settle();
  var first = writes();
  var after = { notes: notes(techRow("blake@example.com")), avery: messageOf(avery), blake: messageOf(techRow("blake@example.com")) };
  var date = field(avery, "Effective from").value;
  click(teamEl(), "Save team");
  await settle();
  return { first: first, again: writes().length, after: after, today: TODAY, retention: RETENTION_START, date: date,
           saved: messageOf(teamEl()) };
});
""")
    today, retention = out["today"], out["retention"]
    assert out["first"] == [
        ["PUT", f"/api/shifts/{AVERY}", {"effective_from": today, "work_days": [1, 2, 3, 4], "start": "08:00", "end": "16:00"}],
        ["PUT", f"/api/shifts/{BLAKE}", {"effective_from": retention, "work_days": [0, 1, 2, 3, 4], "start": "08:00",
                                          "end": "17:00"}]]
    assert out["after"]["notes"] == [BLAKE], "once stored the row is no longer marked unsaved"
    assert out["again"] == 2, "a second Save team sends nothing: no row differs from what was saved"
    assert out["after"]["avery"] and out["after"]["blake"], "each saved row says so"
    assert out["date"] == today


def test_team_save_sends_nothing_for_unchanged_rows(tmp_path):
    shifts = {"techs": [SHIFTS["techs"][0], shifts_after(BLAKE, "Blake Reed", [0, 1, 2, 3, 4])]}
    out = run_page("#/settings", responses_for(tmp_path, shifts=shifts), HELPERS + """
run(async function () {
  click(teamEl(), "Save team");
  await settle();
  return { writes: writes(), message: messageOf(teamEl()) };
});
""")
    assert out["writes"] == []
    assert out["message"] == ["Nothing to change."]


def test_team_row_date_change_alone_saves_a_version(tmp_path):
    """The owner may pick an earlier effective-from date to correct history."""
    responses = responses_for(tmp_path)
    responses["PUT /api/shifts/avery%40example.com"] = answer(shifts_after(AVERY, "Avery Cole", [1, 2, 3, 4, 5],
                                                                          effective="2026-09-01"))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  field(techRow("avery@example.com"), "Effective from").value = "2026-09-01";
  click(teamEl(), "Save team");
  await settle();
  return writes().filter(function (w) { return w[1].indexOf("avery") >= 0; });
});
""")
    assert out == [["PUT", f"/api/shifts/{AVERY}",
                    {"effective_from": "2026-09-01", "work_days": [1, 2, 3, 4, 5], "start": "08:00", "end": "17:00"}]]


def test_started_shows_each_techs_first_day_and_waits_for_a_saved_shift(tmp_path):
    shifts = {"techs": [{"email": AVERY, "name": "Avery Cole", "versions": [
        {"effective_from": "2026-03-02", "work_days": [1, 2, 3, 4, 5], "active": True, **NINE_TO_FIVE},
        {"effective_from": "2026-08-01", "work_days": [2, 3, 4, 5, 6], "active": True, **NINE_TO_FIVE}]},
        SHIFTS["techs"][1]]}
    out = run_page("#/settings", responses_for(tmp_path, shifts=shifts), HELPERS + """
run(async function () {
  var avery = field(techRow("avery@example.com"), "Started"), blake = field(techRow("blake@example.com"), "Started");
  return { avery: avery.value, averyOff: !!avery.disabled, max: avery.getAttribute("max"), today: TODAY,
           blake: blake.value, blakeOff: !!blake.disabled, blakeTitle: blake.getAttribute("title"),
           days: days(techRow("avery@example.com")) };
});
""")
    assert (out["avery"], out["averyOff"], out["max"]) == ("2026-03-02", False, out["today"])
    assert out["days"] == [2, 3, 4, 5, 6], "the hours shown are still the latest shift's"
    assert (out["blake"], out["blakeOff"]) == ("", True)
    assert out["blakeTitle"] == "Save the tech's shift first, then their start date."


def test_a_started_change_alone_sends_only_the_start(tmp_path):
    responses = responses_for(tmp_path)
    responses["PUT /api/shifts/avery%40example.com/start"] = answer(
        shifts_after(AVERY, "Avery Cole", [1, 2, 3, 4, 5], effective="2026-09-14"))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var avery = techRow("avery@example.com");
  field(avery, "Started").value = "2026-09-14";
  click(teamEl(), "Save team");
  await settle();
  return { writes: writes().filter(function (w) { return w[1].indexOf("avery") >= 0; }), message: messageOf(avery),
           started: field(avery, "Started").value };
});
""")
    assert out["writes"] == [["PUT", f"/api/shifts/{AVERY}/start", {"started": "2026-09-14"}]]
    assert out["message"] == ["Saved. Tracked from Sep 14, 2026."]
    assert out["started"] == "2026-09-14"


def test_a_start_and_hours_on_the_same_day_send_the_start_first(tmp_path):
    """A new tech's real hours from their first day: the start makes that day their first, then the hours replace
    the shift on it."""
    responses = responses_for(tmp_path)
    responses["PUT /api/shifts/avery%40example.com/start"] = answer(
        shifts_after(AVERY, "Avery Cole", [1, 2, 3, 4, 5], effective="2026-03-02"))
    responses["PUT /api/shifts/avery%40example.com"] = answer(
        shifts_after(AVERY, "Avery Cole", [2, 3, 4, 5, 6], effective="2026-03-02"))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var avery = techRow("avery@example.com");
  field(avery, "Started").value = "2026-03-02";
  field(avery, "Effective from").value = "2026-03-02";
  setDays(avery, [2, 3, 4, 5, 6]);
  click(teamEl(), "Save team");
  await settle();
  return { writes: writes().filter(function (w) { return w[1].indexOf("avery") >= 0; }), message: messageOf(avery) };
});
""")
    assert out["writes"] == [
        ["PUT", f"/api/shifts/{AVERY}/start", {"started": "2026-03-02"}],
        ["PUT", f"/api/shifts/{AVERY}", {"effective_from": "2026-03-02", "work_days": [2, 3, 4, 5, 6],
                                          "start": "08:00", "end": "17:00"}]]
    assert out["message"] == ["Saved. Tracked from Mar 2, 2026. The shift applies from Mar 2, 2026."]


def test_team_save_shows_the_error_on_its_own_row(tmp_path):
    responses = responses_for(tmp_path)
    responses["PUT /api/shifts/avery%40example.com"] = answer({"detail": "The shift must end after it starts."}, 400)
    responses["PUT /api/shifts/blake%40example.com"] = answer(shifts_after(BLAKE, "Blake Reed", [0, 1, 2, 3, 4]))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var avery = techRow("avery@example.com"), blake = techRow("blake@example.com");
  field(avery, "End time").value = "07:00";
  click(teamEl(), "Save team");
  await settle();
  return { avery: messageOf(avery), blake: messageOf(blake), avereyEnd: field(avery, "End time").value,
           writes: writes().length };
});
""")
    assert out["avery"] == ["The shift must end after it starts."]
    assert out["avery"] != out["blake"]
    assert out["avereyEnd"] == "07:00", "a failed row keeps what the owner typed"
    assert out["writes"] == 2, "one row failing does not stop the next"


# -- adding and removing a tech ---------------------------------------------------------------------------


def added_settings(tmp_path):
    return settings_body(tmp_path, team=(AVERY, BLAKE, DANA))


def add_tech_page(tmp_path, placeholder_from: str) -> dict:
    """Add Dana with Mon-Thu 08:00-15:30, where saving the team made the server start her on `placeholder_from`
    (the retention start before the first activity sync, else today). "RETENTION_START" stands for that day."""
    responses = responses_for(tmp_path)
    responses["POST /api/team"] = answer({**added_settings(tmp_path), "added": DANA})
    responses["PUT /api/shifts/dana%40example.com"] = answer(shifts_after(DANA, "Dana Fox", [1, 2, 3, 4], end="15:30"))
    return run_page("#/settings", responses, HELPERS + f"""
run(async function () {{
  var from = {json.dumps(placeholder_from)};
  if (from === "RETENTION_START") {{ from = RETENTION_START; }}
  var placed = {{ email: "dana@example.com", name: "Dana Fox", versions: [
    {{ effective_from: from, work_days: [0, 1, 2, 3, 4], start: "08:00", end: "17:00", active: true }}] }};
  RESPONSES["GET /api/shifts"] = {{ status: 200, body: {{ techs: {json.dumps(SHIFTS["techs"])}.concat([placed]) }} }};
  var add = all(teamEl(), function (e) {{ return e.classList.contains("add-row"); }})[0];
  field(add, "Tech name").value = " Dana Fox ";
  setDays(add, [1, 2, 3, 4]);
  field(add, "End time").value = "15:30";
  var addStart = field(add, "Start time").value;
  click(teamEl(), "Add tech");
  await settle();
  return {{ writes: writes(), names: techRows().map(function (r) {{ return notes(r)[0]; }}),
           message: messageOf(teamEl()), from: from, name: field(add, "Tech name").value, addStart: addStart,
           addDays: days(add), dana: days(techRow("dana@example.com")) }};
}});
""")


def test_add_tech_saves_team_then_shift(tmp_path):
    """The shift goes in on the day the server started the tech (here after a sync: its today), replacing the
    placeholder, never earlier: days before the tech joined must not read as "Sick day"."""
    out = add_tech_page(tmp_path, "2026-10-17")
    assert out["writes"] == [
        ["POST", "/api/team", {"name": "Dana Fox"}],
        ["PUT", f"/api/shifts/{DANA}", {"effective_from": "2026-10-17", "work_days": [1, 2, 3, 4],
                                        "start": "08:00", "end": "15:30"}]]
    assert out["addStart"] == "08:00"
    assert out["names"] == [AVERY, BLAKE, DANA]
    assert out["dana"] == [1, 2, 3, 4]
    assert out["name"] == "" and out["addDays"] == [0, 1, 2, 3, 4], "the add row starts over"
    assert out["message"] and out["message"][-1].startswith("Added " + DANA)


def test_add_tech_before_first_sync_starts_at_retention_start(tmp_path):
    """Before any activity sync the server starts a new tech at the retention start; the shift goes in there."""
    out = add_tech_page(tmp_path, "RETENTION_START")
    assert out["writes"][1] == ["PUT", f"/api/shifts/{DANA}", {"effective_from": out["from"],
                                                                "work_days": [1, 2, 3, 4], "start": "08:00",
                                                                "end": "15:30"}]


def test_add_row_has_no_effective_from_date(tmp_path):
    out = run_page("#/settings", responses_for(tmp_path), HELPERS + """
run(async function () {
  var add = all(teamEl(), function (e) { return e.classList.contains("add-row"); })[0];
  return { date: !!field(add, "Effective from"),
           dateInputs: all(add, function (e) { return e.tagName === "INPUT" && e.getAttribute("type") === "date"; }).length,
           hours: [!!field(add, "Start time"), !!field(add, "End time")], days: days(add) };
});
""")
    assert out["date"] is False and out["dateInputs"] == 0, "the server picks a new tech's first day"
    assert out["hours"] == [True, True] and out["days"] == [0, 1, 2, 3, 4], "it keeps work days and hours"


def test_add_tech_whose_shift_fails_stays_on_the_team(tmp_path):
    responses = responses_for(tmp_path)
    responses["POST /api/team"] = answer({**added_settings(tmp_path), "added": DANA})
    responses["PUT /api/shifts/dana%40example.com"] = answer({"detail": "The shift must end after it starts."}, 400)
    refreshed = {"techs": SHIFTS["techs"] + [shifts_after(DANA, "Dana Fox", [0, 1, 2, 3, 4], effective="2026-10-19")]}
    out = run_page("#/settings", responses, HELPERS + f"""
run(async function () {{
  RESPONSES["GET /api/shifts"] = {{ status: 200, body: {json.dumps(refreshed)} }};   // what a re-read now finds
  var add = all(teamEl(), function (e) {{ return e.classList.contains("add-row"); }})[0];
  field(add, "Tech name").value = "Dana Fox";
  click(teamEl(), "Add tech");
  await settle();
  return {{ message: messageOf(teamEl()), dana: techRow("dana@example.com") ? notes(techRow("dana@example.com")) : null,
           writes: writes().map(function (w) {{ return w[1]; }}), addMessage: messageOf(add) }};
}});
""")
    assert out["writes"] == ["/api/team", f"/api/shifts/{DANA}"]
    assert any("The shift must end after it starts." in m for m in out["message"])
    assert len(out["addMessage"]) == 1 and "The shift must end after it starts." in out["addMessage"][0], \
        "the error shows on the add row"
    assert any(DANA in m and "team" in m for m in out["message"]), "it says the tech was added to the team"
    assert out["dana"] == [DANA], "the tech stays on the team, shown with the server's placeholder"


def test_add_tech_unknown_in_zendesk_saves_no_shift(tmp_path):
    responses = responses_for(tmp_path)
    responses["POST /api/team"] = answer({"detail": "No Zendesk agent at typo.name@example.com",
                                          "email": "typo.name@example.com"}, 400)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var add = all(teamEl(), function (e) { return e.classList.contains("add-row"); })[0];
  field(add, "Tech name").value = "Typo Name";
  click(teamEl(), "Add tech");
  await settle();
  return { writes: writes().map(function (w) { return w[1]; }), message: messageOf(teamEl()), rows: techRows().length };
});
""")
    assert out["writes"] == ["/api/team"]
    assert out["message"] == ["No Zendesk agent at typo.name@example.com. Correct the email and click Add tech "
                              "again."]
    assert out["rows"] == 2


def test_remove_tech_keeps_other_rows_edits(tmp_path):
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(settings_body(tmp_path, team=(AVERY,)))
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  setDays(techRow("avery@example.com"), [2, 3]);
  click(techRow("blake@example.com"), "Remove");
  await settle();
  return { writes: writes(), rows: techRows().map(function (r) { return notes(r)[0]; }),
           avery: days(techRow("avery@example.com")) };
});
""")
    assert out["writes"] == [["PUT", "/api/settings", {"team": [AVERY]}]]
    assert out["rows"] == [AVERY]
    assert out["avery"] == [2, 3], "removing a tech does not discard another row's unsaved edit"


# -- time off ---------------------------------------------------------------------------------------------


def test_time_off_add_and_remove(tmp_path):
    responses = responses_for(tmp_path)
    responses["POST /api/time-off"] = answer({"id": 9, "email": BLAKE, "first": "2026-10-20", "last": "2026-10-21",
                                              "note": "Dentist", "kind": "sick"}, 201)
    responses["DELETE /api/time-off/7"] = {"status": 204, "body": {}}
    out = run_page("#/settings", responses, HELPERS + """
function offEl() { return panelTitled("Time off"); }
function offRows() {
  return all(offEl(), function (e) { return e.tagName === "TR" && e.classList.contains("off-row"); })
    .map(function (r) { return all(r, function (e) { return e.tagName === "TD"; }).map(function (c) { return c.textContent; }); });
}
run(async function () {
  var out = { before: offRows() };
  out.defaults = { first: field(offEl(), "First day").value, last: field(offEl(), "Last day").value,
                   note: field(offEl(), "Note").value, today: TODAY };
  var select = all(offEl(), function (e) { return e.tagName === "SELECT"; })[0];
  out.options = all(select, function (e) { return e.tagName === "OPTION"; }).map(function (o) { return [o.value, o.textContent]; });
  select.value = "blake@example.com";
  field(offEl(), "First day").value = "2026-10-20";
  field(offEl(), "Last day").value = "2026-10-21";
  field(offEl(), "Note").value = " Dentist ";
  out.kinds = all(field(offEl(), "Kind"), function (e) { return e.tagName === "OPTION"; }).map(function (o) { return [o.value, o.textContent]; });
  out.kindDefault = field(offEl(), "Kind").value;
  field(offEl(), "Kind").value = "sick";
  click(offEl(), "Add time off");
  await settle();
  out.added = offRows();
  click(all(offEl(), function (e) { return e.tagName === "TR" && e.classList.contains("off-row"); })[0], "Remove");
  await settle();
  out.removed = offRows();
  out.writes = writes();
  out.noteAfter = field(offEl(), "Note").value;
  return out;
});
""")
    assert out["before"] == [["Avery Cole", "Mon Oct 12", "PTO", "PTO", "Edit Remove"],
                             ["Blake Reed", "Oct 12 – Oct 14", "Sick", "", "Edit Remove"]]
    assert out["kinds"] == [["pto", "PTO"], ["sick", "Sick"]] and out["kindDefault"] == "pto", "PTO first, the default"
    assert out["options"] == [[AVERY, "Avery Cole"], [BLAKE, "Blake Reed"]], "the select lists the current team"
    assert out["added"][-1] == ["Blake Reed", "Oct 20 – Oct 21", "Sick", "Dentist", "Edit Remove"]
    assert out["writes"] == [
        ["POST", "/api/time-off", {"email": BLAKE, "first": "2026-10-20", "last": "2026-10-21", "note": "Dentist",
                                   "kind": "sick"}],
        ["DELETE", "/api/time-off/7", None]]
    assert [r[0] for r in out["removed"]] == ["Blake Reed", "Blake Reed"], "Remove deletes the first item"
    assert out["defaults"]["first"] == out["defaults"]["last"] == out["defaults"]["today"]
    assert out["defaults"]["note"] == "" and out["noteAfter"] == "", "the note box empties once the time off is added"


def test_time_off_is_edited_in_a_window(tmp_path):
    responses = responses_for(tmp_path)
    responses["PATCH /api/time-off/7"] = answer({"id": 7, "email": AVERY, "first": "2026-10-12", "last": "2026-10-12",
                                                 "note": "PTO", "kind": "sick"})
    out = run_page("#/settings", responses, HELPERS + """
function offEl() { return panelTitled("Time off"); }
function kinds() {
  return all(offEl(), function (e) { return e.tagName === "TR" && e.classList.contains("off-row"); })
    .map(function (r) { return all(r, function (e) { return e.tagName === "TD"; })[2].textContent; });
}
function dialogEl() { return all(document.body, function (e) { return e.classList.contains("dialog"); })[0] || null; }
function byId(id) { return document.getElementById(id); }
run(async function () {
  var before = kinds();
  click(all(offEl(), function (e) { return e.classList.contains("off-row"); })[0], "Edit");
  var d = dialogEl();
  var opened = { title: all(d, function (e) { return e.classList.contains("dialog-title"); })[0].textContent,
    sub: all(d, function (e) { return e.classList.contains("sub"); })[0].textContent,
    first: byId("edit-off-first").value, last: byId("edit-off-last").value, kind: byId("edit-off-kind").value,
    note: byId("edit-off-note").value };
  byId("edit-off-last").value = "2026-10-13";
  byId("edit-off-kind").value = "sick";
  byId("edit-off-note").value = " Flu ";
  click(d, "Save");
  await settle();
  return { before: before, opened: opened, after: kinds(), writes: writes(), message: messageOf(offEl()),
           open: !!dialogEl() };
});
""")
    assert out["opened"] == {"title": "Edit time off", "sub": "Avery Cole", "first": "2026-10-12",
                             "last": "2026-10-12", "kind": "pto", "note": "PTO"}
    assert out["before"] == ["PTO", "Sick"] and out["after"] == ["Sick", "Sick"]
    assert out["writes"] == [["PATCH", "/api/time-off/7",
                              {"first": "2026-10-12", "last": "2026-10-13", "kind": "sick", "note": "Flu"}]]
    assert out["message"] == ["Saved."] and out["open"] is False


def test_time_off_kind_error_is_shown(tmp_path):
    responses = responses_for(tmp_path)
    responses["PATCH /api/time-off/8"] = answer({"detail": "There is no time off numbered 8."}, 404)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var off = panelTitled("Time off");
  click(all(off, function (e) { return e.classList.contains("off-row"); })[1], "Edit");
  var d = all(document.body, function (e) { return e.classList.contains("dialog"); })[0];
  click(d, "Save");
  await settle();
  return { message: messageOf(d), open: !!d.parentNode,
           kind: all(off, function (e) { return e.classList.contains("off-row"); })[1].children[2].textContent };
});
""")
    assert out == {"message": ["There is no time off numbered 8."], "open": True, "kind": "Sick"}, \
        "a refused edit keeps the window open and says why"


def test_time_off_error_is_shown(tmp_path):
    responses = responses_for(tmp_path)
    responses["POST /api/time-off"] = answer({"detail": "The first day must be on or before the last day."}, 400)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var off = panelTitled("Time off");
  field(off, "First day").value = "2026-10-22";
  field(off, "Last day").value = "2026-10-20";
  click(off, "Add time off");
  await settle();
  return { message: messageOf(off), rows: all(off, function (e) { return e.classList.contains("off-row"); }).length };
});
""")
    assert out == {"message": ["The first day must be on or before the last day."], "rows": 2}


# -- activity flags ---------------------------------------------------------------------------------------


def test_flag_thresholds_save(tmp_path):
    body = settings_body(tmp_path)
    body.update(activity_late_minutes=45, activity_early_minutes=60, activity_gap_minutes=150)
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(body)
    out = run_page("#/settings", responses, HELPERS + """
function flagsEl() { return panelTitled("Activity flags"); }
run(async function () {
  var out = { sub: flagsEl().children.filter(function (e) { return e.classList.contains("sub"); })[0].textContent,
              labels: all(flagsEl(), function (e) { return e.tagName === "LABEL" || e.classList.contains("label"); })
                .map(function (e) { return e.textContent; }),
              start: ["Late start", "Early finish", "Long gap"].map(function (l) { return field(flagsEl(), l).value; }),
              text: flagsEl().textContent };
  field(flagsEl(), "Late start").value = "45";
  field(flagsEl(), "Early finish").value = "60";
  field(flagsEl(), "Long gap").value = "150";
  click(flagsEl(), "Save flags");
  await settle();
  out.writes = writes();
  out.message = messageOf(flagsEl());
  // Not a whole number of at least 1: nothing is sent.
  field(flagsEl(), "Long gap").value = "0";
  click(flagsEl(), "Save flags");
  await settle();
  out.invalid = { writes: writes().length, message: messageOf(flagsEl()) };
  field(flagsEl(), "Long gap").value = "2.5";
  click(flagsEl(), "Save flags");
  await settle();
  out.fraction = writes().length;
  return out;
});
""")
    assert out["sub"] == "When Team activity flags a day"
    assert out["labels"] == ["Late start", "Early finish", "Long gap", "Always flagged"]
    assert out["start"] == ["90", "90", "120"]
    assert "Sick day" in out["text"] and "Signed in, no activity" in out["text"]
    assert out["writes"] == [["PUT", "/api/settings", {"activity_late_minutes": 45, "activity_early_minutes": 60,
                                                       "activity_gap_minutes": 150}]]
    assert out["message"] == ["Saved."]
    assert out["invalid"]["writes"] == 1 and out["invalid"]["message"]
    assert out["fraction"] == 1


# -- the 1:1 schedule -------------------------------------------------------------------------------------

SCHEDULE_HELPERS = """
function schedEl() { return panelTitled("1:1 schedule"); }
function schedRows() {
  return all(schedEl(), function (e) { return e.tagName === "TR" && e.classList.contains("sched-row"); });
}
function schedRow(name) { return schedRows().filter(function (r) { return r.children[0].textContent === name; })[0]; }
function rowOut(r) {
  return { name: r.children[0].textContent, date: field(r, "A 1:1 date").value, weeks: field(r, "Repeats every").value,
           unit: r.children[2].textContent.trim(),
           worked: r.children.slice(3).map(function (c) { return c.textContent; }) };
}
function scheduleReads() {
  return requests.filter(function (r) { return r.method === "GET" && r.url === "/api/prep/schedule"; }).length;
}
"""


def test_settings_has_one_on_one_schedule_panel(tmp_path):
    """Spec §3: one row per tech on the team with a 1:1 date, a repeat (1 to 8 weeks; 2 for a tech with none yet)
    and the days worked out from them. Saving sends the whole map, a past tech's entry included, then re-reads the
    rows so the worked-out columns update. A refused save shows the setting's own message, keeping what was typed."""
    refused = api_for(tmp_path).put("/api/settings", json={"oneonone_schedule": {
        BLAKE: {"date": "2026-10-21", "weeks": 9}}})
    assert refused.status_code == 400
    saved = {AVERY: {"date": "2026-10-20", "weeks": 4}, BLAKE: {"date": "2026-10-21", "weeks": 3}, **GONE}
    after = [
        {"email": AVERY, "name": "Avery Cole", "date": "2026-10-20", "weeks": 4, "latest_label": "Tue Oct 20",
         "next_label": "Tue Nov 17", "covers": "11/3 – 11/16"},
        {"email": BLAKE, "name": "Blake Reed", "date": "2026-10-21", "weeks": 3, "latest_label": "Wed Sep 30",
         "next_label": "Wed Oct 21", "covers": "10/7 – 10/20"}]
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(dict(settings_body(tmp_path), oneonone_schedule=saved))
    out = run_page("#/settings", responses, HELPERS + SCHEDULE_HELPERS + f"""
run(async function () {{
  var box = schedEl();
  var weeks = field(schedRow("Avery Cole"), "Repeats every");
  var out = {{
    sub: box.children.filter(function (e) {{ return e.classList.contains("sub"); }})[0].textContent,
    head: all(box, function (e) {{ return e.tagName === "TH"; }}).map(function (e) {{ return e.textContent; }}),
    rows: schedRows().map(rowOut),
    types: [field(schedRow("Avery Cole"), "A 1:1 date").getAttribute("type"), weeks.getAttribute("type")],
    range: [weeks.getAttribute("min"), weeks.getAttribute("max"), weeks.getAttribute("step")],
    note: all(box, function (e) {{ return e.classList.contains("table-note"); }})
      .map(function (e) {{ return e.textContent; }}),
    column: all(screenEl, function (e) {{ return e.classList.contains("stack"); }})[0].children.map(titleOf),
    reads: scheduleReads(), writes: writes()
  }};
  RESPONSES["GET /api/prep/schedule"] = {{ status: 200, body: {json.dumps(after)} }};
  field(schedRow("Avery Cole"), "Repeats every").value = "4";
  field(schedRow("Blake Reed"), "A 1:1 date").value = "2026-10-21";
  field(schedRow("Blake Reed"), "Repeats every").value = "3";
  click(box, "Save schedule");
  await settle();
  out.saved = {{ writes: writes(), rows: schedRows().map(rowOut), message: messageOf(schedEl()),
                reads: scheduleReads() }};
  RESPONSES["PUT /api/settings"] = {{ status: {refused.status_code}, body: {json.dumps(refused.json())} }};
  field(schedRow("Blake Reed"), "Repeats every").value = "6";
  click(schedEl(), "Save schedule");
  await settle();
  var message = all(schedEl(), function (e) {{ return e.classList.contains("message"); }})[0];
  out.refused = {{ sent: writes()[1][2], message: message.textContent, kind: message.className,
                  weeks: field(schedRow("Blake Reed"), "Repeats every").value, reads: scheduleReads() }};
  return out;
}});
""")
    assert out["sub"] == ("Pick any one of a tech's 1:1 dates and how often they repeat. Each prep covers the 14 days "
                          "before that tech's 1:1.")
    assert out["head"] == ["Tech", "A 1:1 date", "Repeats every", "Latest 1:1", "Next 1:1", "That prep covers"]
    assert out["rows"] == [
        {"name": "Avery Cole", "date": "2026-10-20", "weeks": "2", "unit": "weeks",
         "worked": ["Tue Oct 20", "Tue Nov 3", "10/20 – 11/2"]},
        {"name": "Blake Reed", "date": "", "weeks": "2", "unit": "weeks", "worked": ["-", "-", "-"]}]
    assert out["types"] == ["date", "number"] and out["range"] == ["1", "8", "1"]
    assert out["note"] == [SCHEDULE_NOTE]
    assert out["column"] == ["Team", "Time off", "1:1 schedule"]
    assert out["reads"] == 1 and out["writes"] == [], "nothing is stored until the owner clicks Save schedule"
    assert out["saved"]["writes"] == [["PUT", "/api/settings", {"oneonone_schedule": saved}]]
    assert out["saved"]["rows"] == [
        {"name": "Avery Cole", "date": "2026-10-20", "weeks": "4", "unit": "weeks",
         "worked": ["Tue Oct 20", "Tue Nov 17", "11/3 – 11/16"]},
        {"name": "Blake Reed", "date": "2026-10-21", "weeks": "3", "unit": "weeks",
         "worked": ["Wed Sep 30", "Wed Oct 21", "10/7 – 10/20"]}]
    assert out["saved"]["message"] == ["Saved."] and out["saved"]["reads"] == 2
    # The server's own refusal (a stand-in answer here) shows as it is; the page checks the repeat itself too.
    assert out["refused"]["sent"] == {"oneonone_schedule": {**saved, BLAKE: {"date": "2026-10-21", "weeks": 6}}}
    assert out["refused"]["message"] == "The repeat for blake@example.com must be a whole number of weeks from 1 to 8"
    assert out["refused"]["kind"] == "message error"
    assert out["refused"]["weeks"] == "6" and out["refused"]["reads"] == 2, "a refused save keeps what was typed"


def test_schedule_cleared_date_drops_that_tech(tmp_path):
    """Clearing a tech's 1:1 date takes them off the schedule; a tech with no date sends nothing, whatever their
    repeat; a blank repeat beside a date is refused in the page, naming the tech, and nothing is sent."""
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(dict(settings_body(tmp_path), oneonone_schedule=GONE))
    unscheduled = [dict(row, **UNSCHEDULED) for row in SCHEDULE_ROWS]
    out = run_page("#/settings", responses, HELPERS + SCHEDULE_HELPERS + f"""
run(async function () {{
  RESPONSES["GET /api/prep/schedule"] = {{ status: 200, body: {json.dumps(unscheduled)} }};   // after the first save
  field(schedRow("Avery Cole"), "A 1:1 date").value = "";
  field(schedRow("Blake Reed"), "Repeats every").value = "6";
  click(schedEl(), "Save schedule");
  await settle();
  field(schedRow("Blake Reed"), "A 1:1 date").value = "2026-10-21";
  field(schedRow("Blake Reed"), "Repeats every").value = "";
  click(schedEl(), "Save schedule");
  await settle();
  return {{ sent: writes().map(function (w) {{ return w[2]; }}), message: messageOf(schedEl()) }};
}});
""")
    assert out == {"sent": [{"oneonone_schedule": GONE}],
                   "message": ["The repeat for Blake Reed must be a whole number of weeks from 1 to 8"]}


def test_schedule_days_are_worked_out_as_the_owner_edits(tmp_path, conn):
    """Spec §3: Latest 1:1, Next 1:1 and That prep covers are worked out as the owner edits, with the server's math
    (report.schedule_rows, in the owner's today) and no request; an unfinished date or a repeat outside 1-8 shows
    dashes. Saving still reads the server's rows again."""
    out = run_page("#/settings", responses_for(tmp_path), HELPERS + SCHEDULE_HELPERS + """
run(async function () {
  var avery = schedRow("Avery Cole"), blake = schedRow("Blake Reed");
  var date = field(blake, "A 1:1 date"), weeks = field(blake, "Repeats every");
  function worked(r) { return rowOut(r).worked; }
  var out = { today: TODAY };
  date.value = "2026-10-21";
  fire(date, "input");
  out.typed = worked(blake);
  weeks.value = "3";
  fire(weeks, "change");
  out.three = worked(blake);
  field(avery, "Repeats every").value = "4";
  fire(field(avery, "Repeats every"), "input");
  out.avery = worked(avery);
  out.bad = [];
  ["9", "0", "2.5", ""].forEach(function (value) {
    weeks.value = value;
    fire(weeks, "input");
    out.bad.push(worked(blake));
  });
  weeks.value = "3";
  date.value = "";
  date.validity = { badInput: true, valid: false };   // a half-typed date
  fire(date, "input");
  out.unfinished = worked(blake);
  out.reads = scheduleReads();
  out.writes = writes();
  return out;
});
""")
    today = date.fromisoformat(out["today"])
    set_setting(conn, "team", [AVERY, BLAKE])

    def expected(schedule: dict) -> dict[str, list[str]]:
        set_setting(conn, "oneonone_schedule", schedule)
        return {row["email"]: [row["latest_label"], row["next_label"], row["covers"]]
                for row in report.schedule_rows(conn, today=today)}

    avery_2 = {"date": "2026-10-20", "weeks": 2}
    assert out["typed"] == expected({AVERY: avery_2, BLAKE: {"date": "2026-10-21", "weeks": 2}})[BLAKE]
    assert out["three"] == expected({AVERY: avery_2, BLAKE: {"date": "2026-10-21", "weeks": 3}})[BLAKE]
    assert out["avery"] == expected({AVERY: {"date": "2026-10-20", "weeks": 4}})[AVERY]
    assert out["bad"] == [["-", "-", "-"]] * 4
    assert out["unfinished"] == ["-", "-", "-"]
    assert out["reads"] == 1 and out["writes"] == [], "worked out in the page, with no request"


def test_schedule_refuses_an_unfinished_date(tmp_path):
    """Spec §3: a bad date or a repeat outside 1-8 is refused with a plain message. A half-typed date (the browser
    gives it an empty value and validity.badInput) is refused, naming the tech, instead of taking them off the
    schedule; only a truly empty date means no date. A repeat outside 1-8, or not whole, is refused the same way.
    Nothing is sent until every row is whole."""
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(dict(settings_body(tmp_path), oneonone_schedule=GONE))
    out = run_page("#/settings", responses, HELPERS + SCHEDULE_HELPERS + """
run(async function () {
  var avery = schedRow("Avery Cole"), blake = schedRow("Blake Reed");
  var date = field(avery, "A 1:1 date");
  function state() {
    var message = all(schedEl(), function (e) { return e.classList.contains("message"); })[0];
    return [message.textContent, message.className, writes().length];
  }
  date.value = "";
  date.validity = { badInput: true, valid: false };
  click(schedEl(), "Save schedule");
  await settle();
  var out = { unfinished: state() };
  date.validity = { badInput: false, valid: true };
  date.value = "2026-10-20";
  field(blake, "A 1:1 date").value = "2026-10-21";
  out.repeats = [];
  for (var value of ["9", "0", "1.5", "abc"]) {
    field(blake, "Repeats every").value = value;
    click(schedEl(), "Save schedule");
    await settle();
    out.repeats.push(state());
  }
  field(blake, "A 1:1 date").value = "";
  date.validity = { badInput: false, valid: false, valueMissing: true };
  date.value = "";
  click(schedEl(), "Save schedule");
  await settle();
  out.cleared = { state: state(), sent: writes().map(function (w) { return w[2]; }) };
  return out;
});
""")
    assert out["unfinished"] == ["Enter the whole 1:1 date for Avery Cole, or clear it to take them off the schedule",
                                 "message error", 0]
    repeat = ["The repeat for Blake Reed must be a whole number of weeks from 1 to 8", "message error", 0]
    assert out["repeats"] == [repeat] * 4
    assert out["cleared"] == {"state": ["Saved.", "message ok", 1], "sent": [{"oneonone_schedule": GONE}]}


def test_schedule_rows_follow_the_team(tmp_path):
    """Removing a tech in Team re-reads the schedule: their row goes, and another row's unsaved edit stays."""
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer(settings_body(tmp_path, team=(AVERY,)))
    out = run_page("#/settings", responses, HELPERS + SCHEDULE_HELPERS + f"""
run(async function () {{
  RESPONSES["GET /api/prep/schedule"] = {{ status: 200, body: {json.dumps(SCHEDULE_ROWS[:1])} }};
  field(schedRow("Avery Cole"), "Repeats every").value = "5";
  click(techRow("blake@example.com"), "Remove");
  await settle();
  return {{ rows: schedRows().map(rowOut), reads: scheduleReads() }};
}});
""")
    assert out["reads"] == 2
    assert out["rows"] == [{"name": "Avery Cole", "date": "2026-10-20", "weeks": "5", "unit": "weeks",
                            "worked": ["Tue Oct 20", "Tue Nov 3", "10/20 – 11/2"]}]


# -- the Call QA part --------------------------------------------------------------------------------------


def test_call_qa_settings_unchanged_under_heading(tmp_path):
    out = run_page("#/settings", responses_for(tmp_path), HELPERS + """
run(async function () {
  var heads = all(screenEl, function (e) { return e.tagName === "H3" && e.classList.contains("group-head"); });
  var kids = screenEl.children;
  var at = kids.indexOf(heads[0]);
  var rowTitles = function (row) { return row.children.map(function (s) { return s.children.map(titleOf); }); };
  var reviews = panelTitled("Reviews");
  return { heads: heads.map(function (e) { return e.textContent; }), at: at,
           kinds: kids.map(function (e) { return e.tagName + "." + e.className; }),
           before: rowTitles(kids[at - 1]), after: rowTitles(kids[at + 1]),
           controls: ["set-zone", "set-budget", "set-threshold", "set-model", "set-effort", "set-retention"]
             .map(function (id) { return all(reviews, function (e) { return e.getAttribute("id") === id; }).length; }),
           zendesk: all(panelTitled("Zendesk and credentials"), function (e) { return e.getAttribute("id") === "set-subdomain"; }).length };
});
""")
    assert out["heads"] == ["Call QA"]
    assert out["kinds"] == ["DIV.panel-row cols-2-1", "H3.group-head", "DIV.panel-row cols-2"]
    assert out["before"] == [["Team", "Time off", "1:1 schedule"],
                             ["Sections", "Activity flags", "Zendesk and credentials"]]
    assert out["after"] == [["Run now for a date range", "Test a model", "Run history", "Schedule"],
                            ["Reviews", "Reviewer agreement"]]
    assert out["controls"] == [1] * 6 and out["zendesk"] == 1


# -- the stylesheet ---------------------------------------------------------------------------------------


def test_settings_styles_use_tokens():
    css = app_css()
    assert values(css, ".days-pick", "display") == ["inline-flex"]
    assert re.search(r"var\(--sp-\d\)", values(css, ".days-pick", "gap")[0])
    assert values(css, "h3.group-head", "padding") and values(css, "h3.group-head", "margin"), \
        "the Call QA heading gets the block-header box like the other group heads"
    assert 'input[type="time"]' in css, "hours are styled like the other inputs"
    assert "#" not in "".join(values(css, ".days-pick label", "color"))


def test_team_and_schedule_lists_scroll_inside_their_panels(tmp_path):
    """T-143: the PC's Team list and the 1:1 schedule scroll inside their panels past a set height (`.list-scroll`, as
    Time off does), with their Save buttons under the list."""
    out = run_page("#/settings", responses_for(tmp_path), HELPERS + """
run(async function () {
  function wrap(title, save) {
    var p = panelTitled(title);
    return [all(p, function (e) { return e.tagName === "TABLE"; })[0].parentNode.className,
            !!buttonIn(p, save).closest(".list-scroll")];
  }
  return { team: wrap("Team", "Save team"), schedule: wrap("1:1 schedule", "Save schedule"),
           off: wrap("Time off", "Add time off") };
});
""")
    assert out == {"team": ["scroll list-scroll", False], "schedule": ["scroll list-scroll", False],
                   "off": ["scroll list-scroll", False]}
