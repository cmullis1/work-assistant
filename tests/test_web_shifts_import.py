"""Import from Teams Shifts in Settings (Shifts import design, 2026-10-21-shift-finder-design.md; plan Task 3): the
hosted admin's panel, under Techs. The admin chooses the export files; Preview reads them in the page (FileReader),
sends them as base64 and shows what importing them would write for each tech, by region then name, then the people
skipped and the dates covered; Apply, once there is a preview, asks first, sends the same files, names the backup the
server made and reads Techs (and the admin's own Shifts and Time off) again.

app.js runs in node on the fake DOM through the stand-in page of test_web_test_view, answered with what the real
hosted app answers (test_web_scoping's people and synthetic workbooks). The preview drawn in full is one built here
with `shifts_import.preview_json`. Never real export files."""
from __future__ import annotations

import json
from datetime import date

from test_shifts_import import workbook
from test_web_people_ui import ADMIN_READS, SETTINGS_READS, client_as, read_all
from test_web_scoping import (ADMIN, APPLY, CHOOSE_FILES, LEE, PREVIEW, app, avery_files,  # noqa: F401
                              backups_beside, db_path, encoded, started)
from test_web_settings_app import HELPERS, responses_for
from test_web_test_view import answer, run_page
from work_assistant.activity.model import ShiftVersion
from work_assistant.shifts_import import ImportPlan, TechPlan, preview_json

TITLE = "Import from Teams Shifts"
SUB = ("Export the schedule in Teams Shifts (⋯ › Export schedule) and choose the files. You'll see what "
       "would change before anything is saved.")
ASK = "Apply this import? A backup is made first."

# The panel and a stand-in FileReader: readAsDataURL answers a moment later with the file (a {name, base64} stand-in
# for a File) as a data: URL, as the browser's does. `confirm` records what it was asked and answers CONFIRM_ANSWER.
IMPORT_HELPERS = """
function importEl() { return panelTitled("Import from Teams Shifts"); }
function fileInput() {
  return all(importEl(), function (e) { return e.tagName === "INPUT" && e.getAttribute("type") === "file"; })[0];
}
var asked = [];
var CONFIRM_ANSWER = true;
globalThis.confirm = function (text) { asked.push(text); return CONFIRM_ANSWER; };
globalThis.FileReader = function () {};
FileReader.prototype.readAsDataURL = function (file) {
  var reader = this;
  setTimeout(function () {
    reader.result = "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," + file.base64;
    reader.onload();
  }, 0);
};
function choose(files) {
  var input = fileInput();
  input.files = files;
  fire(input, "change");
}
function applyEl() { return buttonIn(importEl(), "Apply"); }
function importMessage() {
  var m = all(importEl(), function (e) { return e.classList.contains("message"); })[0];
  return [m.textContent, m.className];
}
function texts(root, test) { return all(root, test).map(function (e) { return e.textContent; }); }
function hasClass(cls) { return function (e) { return e.classList.contains(cls); }; }
// The preview as drawn: each tech's row group (name, Started, regions, shift changes, leave and problems), and the
// notes under the table.
function shownPreview() {
  var box = all(importEl(), hasClass("import-preview"))[0];
  if (!box || box.hidden) { return null; }
  var table = all(box, function (e) { return e.tagName === "TABLE"; })[0];
  return {
    head: texts(table, function (e) { return e.tagName === "TH"; }),
    techs: all(table, function (e) { return e.tagName === "TBODY"; }).map(function (group) {
      var cells = group.children[0].children;
      var problems = group.children[1] || null;
      return { name: texts(cells[0], function (e) { return e.tagName === "B"; }),
               started: texts(cells[0], hasClass("cell-sub")),
               regions: cells[1].children.map(function (e) { return e.textContent; }),
               changes: cells[2].children.map(function (e) { return e.textContent; }),
               leave: cells[3].textContent, problems: problems ? texts(problems, hasClass("warn")) : [] };
    }),
    notes: box.children.filter(hasClass("note")).map(function (e) { return [e.textContent, e.className]; })
  };
}
function reads(url) { return requests.filter(function (r) { return r.method === "GET" && r.url === url; }).length; }
"""


PANEL_TITLES = "run(async function () { return { titles: panels().map(titleOf) }; });"


def files_js(body: dict) -> str:
    """The files of a request body as the stand-in page's chosen files."""
    return json.dumps([{"name": f["name"], "base64": f["data"]} for f in body["files"]])


def test_the_panel_sits_under_techs_with_its_file_picker_preview_and_apply(app):  # noqa: F811
    """The admin's left column: Techs, then this panel, then their own Shifts and Time off. Its title and sub line,
    a file picker for one or more .xlsx files with its label, Preview, and Apply (the main button) off until a
    preview is shown."""
    out = run_page("#/settings", read_all(client_as(app, ADMIN), *ADMIN_READS), HELPERS + IMPORT_HELPERS + """
run(async function () {
  var box = importEl(), input = fileInput();
  var label = all(box, function (e) { return e.tagName === "LABEL"; })[0];
  return {
    columns: all(screenEl, hasClass("stack")).map(function (s) { return s.children.map(titleOf); }),
    sub: texts(box, hasClass("sub")),
    input: [input.getAttribute("accept"), input.getAttribute("multiple"), input.getAttribute("id")],
    label: [label.textContent, label.getAttribute("for")],
    buttons: all(box, function (e) { return e.tagName === "BUTTON"; }).map(function (b) {
      return [b.textContent, b.className, !!b.disabled]; }),
    preview: shownPreview(), writes: writes()
  };
});
""")
    assert out["columns"][0] == ["Techs", TITLE, "Shifts", "Time off"], "under Techs"
    assert out["sub"] == [SUB]
    assert out["input"] == [".xlsx", "", "shifts-files"], "one or more .xlsx files"
    assert out["label"] == ["Export files", "shifts-files"]
    assert out["buttons"] == [["Preview", "", False], ["Apply", "primary", True]], "Apply waits for a preview"
    assert (out["preview"], out["writes"]) == (None, [])


def test_only_the_hosted_admin_has_the_panel(app, tmp_path):  # noqa: F811
    """A team lead's Settings and the PC's have no import: the routes are the hosted admin's alone."""
    lead = run_page("#/settings", read_all(client_as(app, LEE), *SETTINGS_READS), HELPERS + PANEL_TITLES)
    assert TITLE not in lead["titles"] and lead["titles"], lead
    pc = run_page("#/settings", responses_for(tmp_path), HELPERS + PANEL_TITLES)
    assert TITLE not in pc["titles"] and "Team" in pc["titles"], pc


def day(month: int, dd: int, year: int = 2026) -> date:
    return date(year, month, dd)


def version(email: str, since: date, days: set[int], start_min: int, end_min: int) -> ShiftVersion:
    return ShiftVersion(email, since, frozenset(days), start_min, end_min, True)


CROSSING = ("The 20:00-04:00 shift on 2026-04-08 crosses midnight on Dallas time, so its week keeps the week "
            "before's shift.")
KEPT = "Kept the shift history before 2025-10-01: from 2025-06-01 Mon-Fri 08:00-17:00; from 2025-08-01 Sun,Tue 08:00-17:00."
NO_GROUP = "Pat Other has no shifts in NASA Team, EMEA Team or APAC Team, so nothing is imported for them."


def a_preview(unrecognised=(("Training", 12), ("Client onsite", 3))) -> dict:
    """What the preview route answers for a year of exports: three techs by region then name (a NASA tech with leave,
    a late joiner who moved from APAC to NASA, an EMEA tech whose history before was kept), two people skipped, a
    problem with no tech's plan to hold it and the labels read as working shifts (`unrecognised`). Built with the
    server's own `preview_json`."""
    avery, sam, emma = "avery.cole@example.com", "sam.swap@example.com", "emma.emea@example.com"
    first, last = day(10, 1, 2025), day(10, 31)
    plan = ImportPlan(first, last, (
        TechPlan(avery, "Avery Cole", ((first, "NASA"),),
                 (version(avery, first, {1, 2, 3, 4, 5}, 480, 1020), version(avery, day(3, 1), {0, 2}, 540, 1080),
                  version(avery, day(7, 5), set(), 540, 1080)),
                 True, None, (day(10, 7), day(10, 8)), (day(10, 13),), ()),
        TechPlan(sam, "Sam Swap", ((day(2, 2), "APAC"), (day(4, 1), "NASA")),
                 (version(sam, day(2, 2), {1, 2, 3, 4, 5}, 480, 1020), version(sam, day(4, 5), {2, 3, 4, 5, 6}, 480, 1020)),
                 False, day(2, 2), (), (), (CROSSING,)),
        TechPlan(emma, "Emma Emea", ((first, "EMEA"),), (version(emma, first, {0, 1, 2, 3, 4}, 540, 1080),),
                 False, None, (day(12, 24, 2025),), (), (KEPT,)),
    ), ("Kim Gone", "Lee Lead"), (NO_GROUP,), unrecognised)
    return preview_json(plan)


def test_preview_sends_the_files_as_base64_and_shows_each_tech(app):  # noqa: F811
    """Preview reads every chosen file in the page and posts them, base64, to the preview route; then shows each tech
    in the order the server gives (by region then name): their name, Started when the import sets it, their region
    history, each shift change as `from <date>  <days>  <HH:MM>-<HH:MM>`, their PTO and sick days and their problems.
    Under the table: problems for no one tech, the labels read as working shifts with their counts, the people
    skipped and the dates covered. Apply comes on."""
    body = {"files": [*avery_files()["files"], encoded("november.xlsx", workbook())]}
    responses = read_all(client_as(app, ADMIN), *ADMIN_READS)
    responses[f"POST {PREVIEW}"] = answer(a_preview())
    out = run_page("#/settings", responses, HELPERS + IMPORT_HELPERS + f"""
run(async function () {{
  choose({files_js(body)});
  click(importEl(), "Preview");
  await settle();
  return {{ writes: writes(), preview: shownPreview(), apply: !!applyEl().disabled, message: importMessage() }};
}});
""")
    assert out["writes"] == [["POST", PREVIEW, body]], "every file, by name, its bytes in base64"
    preview = out["preview"]
    assert preview["head"] == ["Tech", "Region", "Shift changes", "Time off"]
    assert preview["techs"] == [
        {"name": ["Avery Cole"], "started": [], "regions": ["NASA from Oct 1, 2025"],
         "changes": ["from Oct 1, 2025  Mon-Fri  08:00-17:00", "from Mar 1, 2026  Sun,Tue  09:00-18:00",
                     "from Jul 5, 2026  no days  09:00-18:00"],
         "leave": "2 PTO days, 1 sick day", "problems": []},
        {"name": ["Sam Swap"], "started": ["Started Feb 2, 2026"],
         "regions": ["APAC from Feb 2, 2026", "NASA from Apr 1, 2026"],
         "changes": ["from Feb 2, 2026  Mon-Fri  08:00-17:00", "from Apr 5, 2026  Tue-Sat  08:00-17:00"],
         "leave": "0 PTO days, 0 sick days", "problems": [CROSSING]},
        {"name": ["Emma Emea"], "started": [], "regions": ["EMEA from Oct 1, 2025"],
         "changes": ["from Oct 1, 2025  Sun-Thu  09:00-18:00"], "leave": "1 PTO day, 0 sick days",
         "problems": [KEPT]}]
    assert preview["notes"] == [
        [NO_GROUP, "note warn table-note"],
        ["Read as working shifts: Training (12), Client onsite (3)", "note table-note"],
        ["Skipped (not on the roster): Kim Gone, Lee Lead", "note table-note"],
        ["Dates covered: Oct 1, 2025 – Oct 31, 2026.", "note table-note"]]
    assert out["apply"] is False, "Apply comes on with a preview"
    assert out["message"] == ["", "message"]


def test_no_line_for_working_shift_labels_when_every_label_is_known(app):  # noqa: F811
    """The "Read as working shifts" line shows only when the files have a label the import doesn't know."""
    body = avery_files()
    responses = read_all(client_as(app, ADMIN), *ADMIN_READS)
    responses[f"POST {PREVIEW}"] = answer(a_preview(unrecognised=()))
    out = run_page("#/settings", responses, HELPERS + IMPORT_HELPERS + f"""
run(async function () {{
  choose({files_js(body)});
  click(importEl(), "Preview");
  await settle();
  return {{ notes: shownPreview().notes.map(function (n) {{ return n[0]; }}) }};
}});
""")
    assert out["notes"] == [NO_GROUP, "Skipped (not on the roster): Kim Gone, Lee Lead",
                            "Dates covered: Oct 1, 2025 – Oct 31, 2026."]


def test_apply_waits_for_a_preview_asks_first_and_names_the_backup(app, tmp_path):  # noqa: F811
    """Apply does nothing before a preview. After one it asks first; a No sends nothing. A Yes sends the same files to
    the apply route, says "Saved. Backup: <file>." with the copy's file name, turns Apply off again and reads Techs,
    and the admin's own Shifts and Time off, again."""
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    body = avery_files()
    responses[f"POST {PREVIEW}"] = answer(admin.post(PREVIEW, json=body).json())
    applied = admin.post(APPLY, json=body)
    assert applied.status_code == 200, applied.text
    [backup] = backups_beside(tmp_path)
    after = read_all(admin, "/api/people", "/api/shifts", "/api/time-off")
    responses[f"POST {APPLY}"] = answer(applied.json())
    out = run_page("#/settings", responses, HELPERS + IMPORT_HELPERS + f"""
run(async function () {{
  click(importEl(), "Apply");
  await settle();
  var out = {{ early: [asked.slice(), writes()] }};
  choose({files_js(body)});
  click(importEl(), "Preview");
  await settle();
  CONFIRM_ANSWER = false;
  click(importEl(), "Apply");
  await settle();
  out.declined = [asked.slice(), writes().length, !!applyEl().disabled];
  CONFIRM_ANSWER = true;
  Object.assign(RESPONSES, {json.dumps(after)});
  var before = [reads("/api/people"), reads("/api/shifts"), reads("/api/time-off")];
  click(importEl(), "Apply");
  await settle();
  out.applied = {{ writes: writes(), message: importMessage(), apply: !!applyEl().disabled,
                  reads: [reads("/api/people") - before[0], reads("/api/shifts") - before[1],
                          reads("/api/time-off") - before[2]],
                  preview: shownPreview() && shownPreview().techs.map(function (t) {{ return t.name[0]; }}) }};
  return out;
}});
""")
    assert out["early"] == [[], []], "no preview yet: nothing asked, nothing sent"
    assert out["declined"] == [[ASK], 1, False], "a No sends nothing and leaves Apply on"
    applied_out = out["applied"]
    assert applied_out["writes"] == [["POST", PREVIEW, body], ["POST", APPLY, body]], "the files previewed"
    assert applied_out["message"] == [f"Saved. Backup: {backup.name}.", "message ok"]
    assert applied_out["apply"] is True, "applied: Apply waits for the next preview"
    assert applied_out["reads"] == [1, 1, 1], "Techs, Shifts and Time off read again"
    assert applied_out["preview"] == ["Avery Stone"], "the plan as applied stays shown"


def test_a_refused_file_says_why_and_apply_stays_off(app):  # noqa: F811
    """A file that isn't an export: the preview route's 400 shows its own words, no preview shows, and Apply stays
    off. A refused Apply says why too, and stays on to try again."""
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    bad = {"files": [encoded("notes.xlsx", b"not a workbook")]}
    refused = admin.post(PREVIEW, json=bad)
    assert refused.status_code == 400
    good = avery_files()
    preview = admin.post(PREVIEW, json=good).json()
    out = run_page("#/settings", responses, HELPERS + IMPORT_HELPERS + f"""
run(async function () {{
  RESPONSES["POST {PREVIEW}"] = {{ status: 400, body: {json.dumps(refused.json())} }};
  choose({files_js(bad)});
  click(importEl(), "Preview");
  await settle();
  var out = {{ refused: [importMessage(), shownPreview(), !!applyEl().disabled] }};
  RESPONSES["POST {PREVIEW}"] = {{ status: 200, body: {json.dumps(preview)} }};
  RESPONSES["POST {APPLY}"] = {{ status: 400, body: {json.dumps(refused.json())} }};
  choose({files_js(good)});
  click(importEl(), "Preview");
  await settle();
  click(importEl(), "Apply");
  await settle();
  out.applyRefused = [importMessage(), !!applyEl().disabled];
  return out;
}});
""")
    words = "notes.xlsx isn't a Teams Shifts export (no Shifts sheet)."
    assert refused.json() == {"detail": words}
    assert out["refused"] == [[words, "message error"], None, True]
    assert out["applyRefused"] == [[words, "message error"], False]


def test_no_files_or_other_files(app):  # noqa: F811
    """Preview with no files says to choose them and sends nothing. Choosing other files after a preview takes the
    preview away and turns Apply off: it would no longer be what Apply writes."""
    admin = client_as(app, ADMIN)
    responses = read_all(admin, *ADMIN_READS)
    body = avery_files()
    responses[f"POST {PREVIEW}"] = answer(admin.post(PREVIEW, json=body).json())
    out = run_page("#/settings", responses, HELPERS + IMPORT_HELPERS + f"""
run(async function () {{
  click(importEl(), "Preview");
  await settle();
  var out = {{ none: [importMessage(), writes()] }};
  choose({files_js(body)});
  click(importEl(), "Preview");
  await settle();
  out.shown = [!!shownPreview(), !!applyEl().disabled];
  choose([{{ name: "december.xlsx", base64: "AAAA" }}]);
  out.other = [shownPreview(), !!applyEl().disabled, importMessage()];
  return out;
}});
""")
    assert out["none"] == [[CHOOSE_FILES, "message error"], []]
    assert out["shown"] == [True, False]
    assert out["other"] == [None, True, ["", "message"]]

