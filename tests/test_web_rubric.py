"""The Rubric & rules screen's endpoints: the current rubric, its versions, saving a new version, and
the agreement rate. Synthetic data only."""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from test_queries import TZ, add_review, all_met, missing, version  # noqa: F401  (fixture)
from work_assistant import rubric
from work_assistant.timeutil import week_start
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app

STATIC = Path(webapp.__file__).parent / "static"
BASE = "http://127.0.0.1:8765"


def client(tmp_path):
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 4242,
                     zendesk_factory=lambda: None, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def api(tmp_path, version):
    return client(tmp_path)


def edit_body(api) -> dict:
    """The current rubric as the page sends it back: checks, reference, params, and a note."""
    current = api.get("/api/rubric").json()
    return {"checks": copy.deepcopy(current["checks"]), "reference": dict(current["reference"]),
            "params": dict(current["params"]), "note": "Test edit"}


def by_id(checks: list[dict]) -> dict[str, dict]:
    return {c["check_id"]: c for c in checks}


def new_rule(**fields) -> dict:
    rule = {"kind": "process", "summary": "Subject format",
            "definition": "The ticket subject follows the format Location - Issue - Station - Location ID.",
            "points": 0, "auto_fail": False, "applies_when": "Always", "enabled": True}
    rule.update(fields)
    return rule


def put(api, body: dict):
    return api.put("/api/rubric", json=body)


# -- the brief's tests ----------------------------------------------------------------------------------


def test_edit_keeps_id_and_bumps_version(api, conn, version):
    body = edit_body(api)
    by_id(body["checks"])["sc06"]["summary"] = "Triage: Confirm the issue back to the caller"
    by_id(body["checks"])["sc06"]["points"] = 10
    response = put(api, body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["version_id"] == version.version_id + 1
    assert saved["note"] == "Test edit"
    sc06 = by_id(saved["checks"])["sc06"]
    assert sc06["summary"] == "Triage: Confirm the issue back to the caller" and sc06["points"] == 10
    assert rubric.current_version(conn).version_id == saved["version_id"]
    assert rubric.get_version(conn, version.version_id).checks == version.checks  # the old one is untouched
    assert api.get("/api/rubric").json()["version_id"] == saved["version_id"]


def test_add_process_rule_gets_new_id(api, conn):
    body = edit_body(api)
    body["checks"].append(new_rule())
    response = put(api, body)
    assert response.status_code == 200, response.text
    added = [c for c in response.json()["checks"] if c["summary"] == "Subject format"]
    assert len(added) == 1
    assert re.fullmatch(r"c-[0-9a-f]{12}", added[0]["check_id"])
    assert added[0]["number"] is None and added[0]["timed"] is None
    stored = {c.check_id for c in rubric.current_version(conn).checks}
    assert added[0]["check_id"] in stored


def test_new_timed_rule_rejected(api, conn, version):
    body = edit_body(api)
    body["checks"].append(new_rule(timed="call_length"))
    response = put(api, body)
    assert response.status_code == 400
    assert response.json()["detail"] == "timed rules need code"
    assert rubric.current_version(conn).version_id == version.version_id


def test_retire_check(api, conn, version):
    review_id = add_review(conn, version, 1, missing(version, "sc14"))
    body = edit_body(api)
    body["checks"] = [c for c in body["checks"] if c["check_id"] != "sc14"]
    response = put(api, body)
    assert response.status_code == 200, response.text
    assert "sc14" not in by_id(response.json()["checks"])
    assert "sc14" not in {c.check_id for c in rubric.current_version(conn).checks}
    assert "sc14" in {c.check_id for c in rubric.get_version(conn, version.version_id).checks}
    detail = api.get("/api/tickets/1").json()
    assert detail["review"]["review_id"] == review_id
    sc14 = by_id(detail["checks"])["sc14"]
    assert sc14["outcome"] == "missed" and sc14["summary"]


def test_note_required(api, conn, version):
    for note in (None, "", "   "):
        body = edit_body(api)
        if note is None:
            del body["note"]
        else:
            body["note"] = note
        by_id(body["checks"])["sc06"]["points"] = 10
        response = put(api, body)
        assert response.status_code == 400, note
        assert "note" in response.json()["detail"].lower()
    assert rubric.current_version(conn).version_id == version.version_id


@pytest.mark.real_today   # the agreement weeks end on the app's today (`_today`), here the real one
def test_agreement_endpoint(api, conn, version):
    zone = ZoneInfo(TZ)
    today = datetime.now(zone).date()
    recent = datetime.combine(week_start(today) - timedelta(days=6), datetime.min.time(), zone).replace(hour=10)
    old = recent - timedelta(weeks=12)
    ten = {f"sc{n:02d}": "met" for n in range(1, 11)}
    add_review(conn, version, 1, ten, overrides={"sc03": "missed"}, call=recent.astimezone(ZoneInfo("UTC")).isoformat())
    add_review(conn, version, 2, ten, call=recent.astimezone(ZoneInfo("UTC")).isoformat())
    add_review(conn, version, 3, ten, overrides={"sc04": "missed"}, call=old.astimezone(ZoneInfo("UTC")).isoformat())
    response = api.get("/api/agreement?weeks=8")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["overall"] == pytest.approx(0.95)
    assert result["by_check"]["sc03"] == pytest.approx(0.5)
    assert result["by_check"]["sc04"] == pytest.approx(1.0)  # the old disagreement is outside 8 weeks
    assert result["weeks"] == 8
    assert date.fromisoformat(result["last_day"]) == today
    assert api.get("/api/agreement").json()["weeks"] == 8
    assert api.get("/api/agreement?weeks=52").status_code == 200
    for bad in ("0", "53", "x"):
        assert api.get(f"/api/agreement?weeks={bad}").status_code == 400


# -- reading the rubric and its versions ---------------------------------------------------------------


def test_get_rubric_shape(api, version):
    current = api.get("/api/rubric").json()
    assert current["version_id"] == version.version_id
    assert current["note"] == rubric.SEED_NOTE
    assert current["scorecard_total"] == 100
    assert set(current["params"]) == {"call_limit_minutes", "exempt_priorities", "exempt_tags", "email_deadline_minutes"}
    assert list(current["reference"]) == list(version.reference)
    checks = by_id(current["checks"])
    assert set(checks["sc01"]) == {"check_id", "kind", "number", "summary", "definition", "points", "auto_fail",
                                   "applies_when", "enabled", "timed"}
    assert checks["pr-call-length"]["timed"] == "call_length"
    assert [c["check_id"] for c in current["checks"]] == [c.check_id for c in version.checks]


def test_versions_list_and_read_only_view(api, version):
    body = edit_body(api)
    body["note"] = "Second"
    by_id(body["checks"])["sc06"]["points"] = 10
    assert put(api, body).status_code == 200
    versions = api.get("/api/rubric/versions").json()["versions"]
    assert [(v["version_id"], v["note"]) for v in versions] == [(version.version_id, rubric.SEED_NOTE),
                                                                (version.version_id + 1, "Second")]
    assert all(v["created_at"] for v in versions)
    old = api.get(f"/api/rubric/versions/{version.version_id}").json()
    assert by_id(old["checks"])["sc06"]["points"] == 5
    assert api.get("/api/rubric/versions/999").status_code == 404


# -- validation --------------------------------------------------------------------------------------------


def rejected(api, conn, version, body, needle: str):
    response = put(api, body)
    assert response.status_code == 400, response.text
    assert needle.lower() in response.json()["detail"].lower(), response.json()["detail"]
    assert rubric.current_version(conn).version_id == version.version_id
    return response


def test_points_must_be_whole_and_not_negative(api, conn, version):
    for bad in (-1, 2.5, "5", True, None):
        body = edit_body(api)
        by_id(body["checks"])["sc06"]["points"] = bad
        rejected(api, conn, version, body, "points")


def test_process_rules_have_no_points(api, conn, version):
    body = edit_body(api)
    by_id(body["checks"])["pr-scope"]["points"] = 5
    rejected(api, conn, version, body, "points")
    body = edit_body(api)
    body["checks"].append(new_rule(points=3))
    rejected(api, conn, version, body, "points")


def test_process_rules_cannot_be_auto_fail(api, conn, version):
    body = edit_body(api)
    by_id(body["checks"])["pr-scope"]["auto_fail"] = True
    rejected(api, conn, version, body, "auto-fail")


def test_existing_timed_value_cannot_change(api, conn, version):
    body = edit_body(api)
    by_id(body["checks"])["pr-call-length"]["timed"] = None
    rejected(api, conn, version, body, "timed")
    body = edit_body(api)
    by_id(body["checks"])["pr-call-length"]["timed"] = "email_deadline"
    rejected(api, conn, version, body, "timed")
    body = edit_body(api)
    by_id(body["checks"])["pr-scope"]["timed"] = "call_length"
    rejected(api, conn, version, body, "timed")
    # Leaving `timed` out keeps the stored value.
    body = edit_body(api)
    del by_id(body["checks"])["pr-call-length"]["timed"]
    by_id(body["checks"])["pr-call-length"]["summary"] = "Call length"
    response = put(api, body)
    assert response.status_code == 200, response.text
    assert by_id(response.json()["checks"])["pr-call-length"]["timed"] == "call_length"


def test_summary_and_definition_required(api, conn, version):
    for field in ("summary", "definition"):
        for bad in ("", "  ", None, 5):
            body = edit_body(api)
            by_id(body["checks"])["sc06"][field] = bad
            rejected(api, conn, version, body, field)


def test_kind_must_be_known_and_unchanged(api, conn, version):
    body = edit_body(api)
    body["checks"].append(new_rule(kind="bonus"))
    rejected(api, conn, version, body, "kind")
    body = edit_body(api)
    by_id(body["checks"])["pr-scope"]["kind"] = "scorecard"
    rejected(api, conn, version, body, "kind")


def test_unknown_fields_rejected(api, conn, version):
    body = edit_body(api)
    body["colour"] = "red"
    rejected(api, conn, version, body, "colour")
    body = edit_body(api)
    by_id(body["checks"])["sc06"]["weight"] = 2
    rejected(api, conn, version, body, "weight")
    body = edit_body(api)
    body["params"]["grace_minutes"] = 2
    rejected(api, conn, version, body, "grace_minutes")


def test_unknown_or_duplicate_check_id_rejected(api, conn, version):
    body = edit_body(api)
    body["checks"].append(new_rule(check_id="c-000000000000"))
    rejected(api, conn, version, body, "c-000000000000")
    body = edit_body(api)
    body["checks"].append(copy.deepcopy(by_id(body["checks"])["sc06"]))
    rejected(api, conn, version, body, "sc06")


def test_booleans_and_applies_when(api, conn, version):
    for field in ("auto_fail", "enabled"):
        body = edit_body(api)
        by_id(body["checks"])["sc06"][field] = "yes"
        rejected(api, conn, version, body, field.replace("_", "-"))
    body = edit_body(api)
    by_id(body["checks"])["sc06"]["enabled"] = False
    by_id(body["checks"])["sc06"]["applies_when"] = "  "
    by_id(body["checks"])["sc05"]["auto_fail"] = True
    response = put(api, body)
    assert response.status_code == 200, response.text
    saved = by_id(response.json()["checks"])
    assert saved["sc06"]["enabled"] is False and saved["sc06"]["applies_when"] == "Always"
    assert saved["sc05"]["auto_fail"] is True
    assert response.json()["scorecard_total"] == 95


def test_scorecard_numbers_follow_list_order(api):
    body = edit_body(api)
    checks = body["checks"]
    sc01 = checks.pop(0)
    checks.insert(3, sc01)  # move #1 below #4
    checks.insert(5, {**new_rule(kind="scorecard", points=5, summary="Closing: recap"), "number": 99})
    response = put(api, body)
    assert response.status_code == 200, response.text
    scorecard = [c for c in response.json()["checks"] if c["kind"] == "scorecard"]
    assert [c["number"] for c in scorecard] == list(range(1, len(scorecard) + 1))
    assert [c["check_id"] for c in scorecard][:4] == ["sc02", "sc03", "sc04", "sc01"]
    assert all(c["number"] is None for c in response.json()["checks"] if c["kind"] == "process")
    assert response.json()["scorecard_total"] == 105


def test_scorecard_needs_points(api, conn, version):
    body = edit_body(api)
    body["checks"] = [c for c in body["checks"] if c["kind"] == "process"]
    rejected(api, conn, version, body, "scorecard")


def test_reference_documents_open_list(api, conn, version):
    body = edit_body(api)
    body["reference"] = {"  Priority definitions ": "P1 means down.", "Escalation contacts": "Call Pat.",
                         "Redirect procedure": version.reference["Redirect procedure"]}
    response = put(api, body)
    assert response.status_code == 200, response.text
    assert response.json()["reference"] == {"Priority definitions": "P1 means down.",
                                            "Escalation contacts": "Call Pat.",
                                            "Redirect procedure": version.reference["Redirect procedure"]}
    assert rubric.current_version(conn).reference == response.json()["reference"]


def test_reference_titles_validated(api, conn, version):
    for reference, needle in (({"  ": "text"}, "title"), ({"Doc": "a", "doc ": "b"}, "doc"),
                              ({"Doc": 5}, "text"), (["Doc"], "reference")):
        body = edit_body(api)
        body["reference"] = reference
        rejected(api, conn, version, body, needle)


def test_params_validated_and_normalized(api, conn, version):
    for field, bad, needle in (("call_limit_minutes", 0, "call length limit"),
                               ("call_limit_minutes", 2.5, "call length limit"),
                               ("email_deadline_minutes", 0, "email deadline"),
                               ("email_deadline_minutes", "30", "email deadline"),
                               ("exempt_priorities", "urgent", "exempt priorities"),
                               ("exempt_tags", [1], "exempt tags")):
        body = edit_body(api)
        body["params"][field] = bad
        rejected(api, conn, version, body, needle)
    body = edit_body(api)
    del body["params"]["exempt_tags"]
    rejected(api, conn, version, body, "exempt_tags")
    body = edit_body(api)
    body["params"] = {"call_limit_minutes": 12, "exempt_priorities": ["Urgent", " HIGH ", "urgent", ""],
                      "exempt_tags": ["Site_Down"], "email_deadline_minutes": 45}
    response = put(api, body)
    assert response.status_code == 200, response.text
    assert response.json()["params"] == {"call_limit_minutes": 12, "exempt_priorities": ["urgent", "high"],
                                         "exempt_tags": ["site_down"], "email_deadline_minutes": 45}
    assert rubric.current_version(conn).params.exempt_priorities == ("urgent", "high")


def test_rubric_edit_deadline_minutes_validation(api, conn, version):
    """The deadline is whole minutes from 1 to 1440 (24 hours); anything else is refused in plain words."""
    for good in (1, 45, 1440):
        body = edit_body(api)
        body["params"]["email_deadline_minutes"] = good
        body["note"] = f"deadline {good}"
        response = put(api, body)
        assert response.status_code == 200, response.text
        assert response.json()["params"]["email_deadline_minutes"] == good
        assert rubric.current_version(conn).params.email_deadline_minutes == good
    current = rubric.current_version(conn)
    for bad in (0, 1441, 2.5):
        body = edit_body(api)
        body["params"]["email_deadline_minutes"] = bad
        response = rejected(api, conn, current, body, "email deadline")
        assert response.json()["detail"] == (
            "The summary email deadline (minutes) must be a whole number from 1 to 1440.")
    body = edit_body(api)
    body["params"]["email_deadline_hours"] = 24
    rejected(api, conn, current, body, "email_deadline_hours")


def test_unchanged_rubric_is_not_saved(api, conn, version):
    response = put(api, edit_body(api))
    assert response.status_code == 400
    assert "nothing" in response.json()["detail"].lower()
    assert rubric.current_version(conn).version_id == version.version_id


def test_stale_base_version_rejected(api, conn, version):
    body = edit_body(api)
    body["base_version_id"] = version.version_id
    by_id(body["checks"])["sc06"]["points"] = 10
    assert put(api, body).status_code == 200
    by_id(body["checks"])["sc07"]["points"] = 10
    response = put(api, body)
    assert response.status_code == 409
    assert rubric.current_version(conn).version_id == version.version_id + 1


def test_rubric_put_from_a_foreign_origin_rejected(api, conn, version):
    body = edit_body(api)
    by_id(body["checks"])["sc06"]["points"] = 10
    response = api.put("/api/rubric", json=body, headers={"origin": "http://evil.example"})
    assert response.status_code == 403
    assert rubric.current_version(conn).version_id == version.version_id


# -- page assets ----------------------------------------------------------------------------------------------


def test_rubric_screen_never_uses_inner_html():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "renderRubric" in script
    assert "Changes apply from the next run." in script
    assert "/api/agreement" in script


def test_rubric_panel_order():
    """Spec 5.4 and phase 4 spec 4.5: the banner, then one cols-2-1 row. The left column stacks Scorecard and
    Process rules; the right column stacks Save changes, Reference documents and Version history. Date-range
    runs live in Settings' Run now only."""
    from test_web_overview import js_function

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    draw = js_function(js, "drawRubric")
    assert '"grid2"' not in draw, "the rubric no longer uses the old two-up grids"

    row = re.search(r'h\("div", \{ class: "panel-row cols-2-1" \},\s*'
                    r'h\("div", \{ class: "stack" \}, rb\.els\.scorecard, rb\.els\.process\),\s*'
                    r'h\("div", \{ class: "stack" \}, side\)\)', draw)
    assert row, "one cols-2-1 row: Scorecard over Process rules on the left, the side stack on the right"
    assert draw.index("RUBRIC_BANNER") < row.start(), "the banner sits above the row, full width"

    side = re.search(r"var side = rb\.readOnly \? \[([^\]]*)\]\s*: \[([^\]]*)\];", draw)
    assert side, "the right column's panels are listed in one place"
    assert [p.strip() for p in side.group(1).split(",")] == ["rb.els.reference", "historyBox"]
    assert [p.strip() for p in side.group(2).split(",")] == \
        ["savePanel()", "rb.els.reference", "historyBox"]
    assert draw.index("var side") < draw.index("fillReference()") and draw.index("var side") < draw.index(
        "fillVersions(historyBox)"), "the panels are placed before they are filled"

    # The panels keep their current labels, and they read in spec order.
    source = "".join(js_function(js, name) for name in
                     ("fillChecks", "savePanel", "fillReference", "fillVersions"))
    position = 0
    for label in ('"Scorecard "', '"Process rules "', '"Unsaved changes"', '"Reference material the reviewer uses"',
                  '"Version history"'):
        found = source.find(label, position)
        assert found >= 0, label + " missing or out of order"
        position = found + len(label)


def test_rubric_editor_stays_in_view():
    """If a rubric table ever scrolls inside its panel, an open editor row is limited to the visible width
    and stays put, so long definitions wrap in view; the stacked narrow layout drops that limit."""
    from css_rules import app_css, has_element, media_block, top_level, values
    from test_web_overview import js_function

    fill = js_function((STATIC / "app.js").read_text(encoding="utf-8"), "fillChecks")
    assert has_element(fill, "table", "atable", "rb")
    box = re.search(r'box\.appendChild\(h\("div",\s*\{\s*class:\s*"([^"]*)"\s*\},\s*table\)\)', fill)
    assert box and {"scroll", "rb-scroll"} <= set(box.group(1).split()), "each rubric table sits in its scroll box"
    css = app_css()
    wide = top_level(css)
    assert values(wide, ".rb-scroll", "container-type") == ["inline-size"], "the editor can measure the box"
    editor = ".atable.rb tr.edit-row .rb-editor"
    assert values(wide, editor, "position") == ["sticky"]
    assert values(wide, editor, "max-width")[0].startswith("calc(100cqi")
    narrow = media_block(css, "(max-width: 640px)")
    assert values(narrow, editor, "position") == ["static"] and values(narrow, editor, "max-width") == ["none"]


def test_rubric_tables_are_two_line_rows():
    """Fix round 1: the rubric tables fit the left column at 1024px and wider with nothing scrolled out of view.
    Each check is its own row group (a tbody) of two lines: line one holds the number, summary, points (as
    text), on and agreement; line two holds Applies when (with any timed summary) and the actions, spanning the
    row; then the editor row. The tables share the two-line rules with the overview's Agents table and run history."""
    from css_rules import app_css, has_element, media_block, top_level, values
    from test_web_overview import js_function

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fill = js_function(js, "fillChecks")
    assert '["#", "Summary", "Points", "On", "Agreement"]' in fill, "auto-fail moved behind Edit (phase 4 spec 4.4)"
    assert '["Rule", "On", "Agreement"]' in fill
    assert has_element(fill, "table", "atable", "two-line", "rb"), "the rubric tables use the shared two-line rows"
    assert has_element(fill, "tr", "line2") and '"Applies when"' in fill, "the header's second line names Applies when"

    row = js_function(js, "appendCheckRow")
    assert has_element(row, "tbody", "row-group"), "each check is one row group"
    line1 = row.index('h("tr", { class: lineClass("line1") })')
    line2 = row.index('h("tr", { class: lineClass("line2") })')
    assert line1 < line2
    assert row.index("line1.appendChild(summaryCell)") < row.index("line1.appendChild(agreementCell(check))")
    assert row.index('line2.appendChild(h("td", { class: "rb-applies" }') < row.index("line2.appendChild(actions)")
    assert re.search(r'var actions = h\("td", \{ class: "act", colspan: String\(width - \(scorecard \? 2 : 1\)\) \}', row)
    assert "group.appendChild(editorRow)" in row
    # Turning a check off greys both of its lines.
    assert 'line1.classList.toggle("off", !check.enabled)' in row and \
        'line2.classList.toggle("off", !check.enabled)' in row

    css = app_css()
    wide = top_level(css)
    assert values(wide, ".atable.two-line tr.line1 td", "border-bottom") == ["0"], "no rule between a check's lines"
    assert values(wide, ".atable tbody.row-group td", "border-bottom") == ["0"]
    assert values(wide, ".atable tbody.row-group", "border-bottom") == ["1px solid var(--rule)"], \
        "one rule under each check, whichever of its rows are showing"
    assert values(media_block(app_css(), "(hover: hover)"), ".atable tbody.row-group:not(.add-row):hover tr:not(.edit-row) td", "background") == ["var(--hover)"], \
        "a check's lines highlight together, but not its open editor"
    assert values(wide, ".atable.rb tr.line1 td.rb-summary", "white-space") == ["normal"], "the summary wraps"
    assert values(wide, ".atable.rb th", "white-space") == ["nowrap"], "the rubric's headers stay on one line"
    narrow = media_block(css, "(max-width: 640px)")
    assert values(narrow, ".atable.rb tr", "border-bottom") == [] and \
        values(narrow, ".atable.rb tr.line1", "border-bottom") == [], \
        "a narrow card spans both lines, with only its group's rule under it"
    assert values(narrow, ".atable.rb .rb-pad", "display") == ["none"]
    assert values(narrow, ".atable.rb td.rb-summary", "flex") == ["1 1 calc(100% - var(--rb-num-slot))"], \
        "the summary fills its line beside the number, and the rest of line one wraps under it"


def test_version_history_note_gets_its_own_line():
    """Fix round 1: in the narrower right column the note reads in normal lines. Version, Saved and the link
    sit on line one; the note spans line two."""
    from test_web_overview import js_function

    versions = js_function((STATIC / "app.js").read_text(encoding="utf-8"), "fillVersions")
    assert 'h("table", { class: "atable two-line versions" }' in versions
    assert re.search(r'h\("tr", \{ class: "line2" \},\s*h\("th", \{ scope: "col", colspan: 3, text: "Note" \}\)\)', versions)
    assert re.search(r'h\("td", \{ class: "note-cell", colspan: 3, text: v\.note \}\)', versions)


# -- phase 4 (spec 4.4, 4.5): settings behind Edit, one home for range runs -----------------------------------

SAVE_POINTER = "To score past calls under this version, use Run now in Settings."

# The page helpers the Rubric & rules section uses from the top of app.js.
RUBRIC_PAGE_HELPERS = ("h", "append", "panel", "setMessage", "num", "pct", "plural", "formatMinutes", "timeLabel",
                       "clearScreen", "endLoading", "loadingLine", "readScreen", "readKept", "getKept", "readJson",
                       "keep")

# Stand-ins for the server and the browser, and small helpers for driving the drawn screen. `DATA` holds the
# rubric as GET /api/rubric returns it, the version a save returns, and the settings.
RUBRIC_HARNESS_JS = """
var screen = document.body.appendChild(h("div"));
var loadingBar = document.body.appendChild(h("div"));
var screenDrawn = false;
var kept = {}, keptEpoch = 0;
var renderToken = 1;
var calls = [];
function api(method, url, body) {
  calls.push({ method: method, url: url, body: body });
  if (method === "PUT" && url === "/api/rubric") { return Promise.resolve(DATA.saved); }
  if (url === "/api/rubric" || url.indexOf("/api/rubric/versions/") === 0) { return Promise.resolve(DATA.base); }
  if (url === "/api/rubric/versions") { return Promise.resolve({ versions: [] }); }
  if (url.indexOf("/api/agreement") === 0) { return Promise.resolve(null); }
  if (url === "/api/settings") { return Promise.resolve(DATA.settings); }
  return Promise.reject(new Error("unexpected request " + method + " " + url));
}
document.getElementById = function () { return null; };
window.confirm = function () { return true; };
function flush() { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }
function fields(el) {
  return el.findAll(function (e) { return e.tagName === "INPUT" || e.tagName === "TEXTAREA"; });
}
function fire(el, type) {
  (el.listeners[type] || []).forEach(function (f) { f({ target: el, preventDefault: function () {} }); });
}
function button(el, text) {
  return el.findAll(function (e) { return e.tagName === "BUTTON" && e.textContent === text; })[0];
}
function valueOf(input) {
  return input.value !== undefined ? String(input.value) : input.getAttribute("value");
}
// The input inside the label whose text starts with `start`.
function labelled(el, start) {
  var label = el.findAll(function (e) { return e.tagName === "LABEL" && e.textContent.indexOf(start) === 0; })[0];
  return label ? fields(label)[0] : null;
}
function describe(input) {
  return { tag: input.tagName, type: input.getAttribute("type"), label: input.getAttribute("aria-label") };
}
// A check's row group as drawn now: [line 1, line 2, editor row].
function group(id) {
  var kind = id.indexOf("sc") === 0 ? "scorecard" : "process";
  var index = rb.draft[kind].map(function (c) { return c.check_id; }).indexOf(id);
  return rb.els[kind].findAll(function (e) {
    return e.tagName === "TBODY" && e.classList.contains("row-group");
  })[index].children;
}
function cellText(td) {
  return td.findAll(function (e) { return e.classList.contains("mlabel"); }).reduce(function (text, label) {
    return text.replace(label.textContent, "");
  }, td.textContent);
}
function viewPoints(id) {
  return cellText(group(id)[0].findAll(function (e) { return e.tagName === "TD"; })[2]);
}
function viewApplies(id) {
  return cellText(group(id)[1].findAll(function (e) { return e.classList.contains("rb-applies"); })[0]);
}
function changes() {
  return rb.els.changes.children.map(function (li) { return li.textContent; });
}
function run(steps) {
  steps().then(function (out) { process.stdout.write(JSON.stringify(out)); })
    .catch(function (error) { console.error(error.stack); process.exit(1); });
}
"""


def run_rubric_screen(api, scenario: str):
    """Run the Rubric & rules section of app.js in node on the fake DOM, against the seed rubric as the real
    endpoint serves it, then `scenario` (JS that calls run(async function () { ... return out; }))."""
    from test_web_overview import js_function, node_prelude, run_node

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    section = js[js.index("// -- rubric & rules"):js.index("// -- settings")]
    base = api.get("/api/rubric").json()
    saved = dict(copy.deepcopy(base), version_id=base["version_id"] + 1)
    data = {"base": base, "saved": saved, "settings": {"problem_threshold": 1, "budget_limit_usd": 40}}
    page = "\n".join(js_function(js, name) for name in RUBRIC_PAGE_HELPERS) + "\n" + section
    # The harness's functions share the script's scope, so one named like a page function would replace it.
    clashes = {name for name in re.findall(r"^function (\w+)\(", RUBRIC_HARNESS_JS, flags=re.M)
               if "function " + name + "(" in page}
    assert not clashes, f"harness helpers would replace app.js functions: {sorted(clashes)}"
    script = node_prelude() + page + f"var DATA = {json.dumps(data)};\n" + RUBRIC_HARNESS_JS + scenario
    return run_node(script), base


def test_timed_summary(api):
    """Phase 4 spec 4.4: the one-line summary of a timed rule's settings, from the draft's values."""
    out, _ = run_rubric_screen(api, """
run(async function () {
  renderRubric(1, []);
  await flush();
  function check(id) {
    return rb.draft.scorecard.concat(rb.draft.process).filter(function (c) { return c.check_id === id; })[0];
  }
  var out = { length: timedSummary(check("pr-call-length")), email: timedSummary(check("pr-email-sent")),
              untimed: timedSummary(check("sc01")) };
  rb.draft.params.exempt_priorities = "urgent, high";
  rb.draft.params.exempt_tags = "site_down, vip";
  out.withTags = timedSummary(check("pr-call-length"));
  rb.draft.params.exempt_priorities = "";
  rb.draft.params.exempt_tags = "";
  out.noExempt = timedSummary(check("pr-call-length"));
  return out;
});
""")
    assert out == {"length": "Limit 10 min · Urgent exempt", "email": "Within 30 min of the call", "untimed": "",
                   "withTags": "Limit 10 min · Urgent, High exempt · Tags: site_down, vip", "noExempt": "Limit 10 min"}


def test_rubric_view_row_has_no_inputs_except_toggle(api):
    """Phase 4 spec 4.4: a check's normal view has no inputs except its on/off switch. Points show as text and
    a timed rule's settings as a summary after Applies when. The points input, auto-fail checkbox and timed
    inputs exist only in the editor, behind Edit. A read-only version has no inputs at all."""
    out, base = run_rubric_screen(api, """
run(async function () {
  renderRubric(1, []);
  await flush();
  var out = { view: {}, editor: {}, points: {}, applies: {} };
  rb.draft.scorecard.concat(rb.draft.process).forEach(function (c) {
    var rows = group(c.check_id);
    out.view[c.check_id] = fields(rows[0]).concat(fields(rows[1])).map(describe);
    out.applies[c.check_id] = viewApplies(c.check_id);
    if (c.kind === "scorecard") {
      out.points[c.check_id] = viewPoints(c.check_id);
    }
    fire(button(rows[1], "Edit"), "click");
    var editor = rows[2];
    out.editor[c.check_id] = {
      hidden: Boolean(editor.hidden),
      points: Boolean(labelled(editor, "Points")), autoFail: Boolean(labelled(editor, "Auto-fail")),
      limit: Boolean(labelled(editor, "Limit")), priorities: Boolean(labelled(editor, "Exempt priorities")),
      tags: Boolean(labelled(editor, "Exempt tags")), deadline: Boolean(labelled(editor, "Deadline"))
    };
  });
  renderRubric(1, ["version", "1"]);
  await flush();
  rb.draft.scorecard.concat(rb.draft.process).forEach(function (c) {
    fire(button(group(c.check_id)[1], "Definition"), "click");
  });
  out.readOnlyFields = fields(rb.els.scorecard).concat(fields(rb.els.process)).length;
  out.readOnlyApplies = viewApplies("pr-call-length");
  return out;
});
""")
    checks = by_id(base["checks"])
    for check_id, check in checks.items():
        view = out["view"][check_id]
        assert len(view) == 1, check_id + ": the view row's only input is the on/off switch"
        assert view[0]["type"] == "checkbox" and view[0]["label"].startswith("On: ")
        timed = {"pr-call-length": " · Limit 10 min · Urgent exempt", "pr-email-sent": " · Within 30 min of the call"}
        assert out["applies"][check_id] == check["applies_when"] + timed.get(check_id, ""), \
            check_id + ": Applies when, then any timed summary, with no empty separator"
        editor = out["editor"][check_id]
        assert editor["hidden"] is False, check_id + ": Edit opens the editor"
        scorecard = check["kind"] == "scorecard"
        assert editor["points"] is scorecard and editor["autoFail"] is scorecard, check_id
        if scorecard:
            assert out["points"][check_id] == str(check["points"]), check_id + ": points show as text"
        assert editor["limit"] is editor["priorities"] is editor["tags"] is (check_id == "pr-call-length"), check_id
        assert editor["deadline"] is (check_id == "pr-email-sent"), check_id
    assert out["readOnlyFields"] == 0, "a read-only version has no inputs, even with its definitions open"
    assert out["readOnlyApplies"] == checks["pr-call-length"]["applies_when"] + " · Limit 10 min · Urgent exempt"


def test_editor_values_persist_across_close(api):
    """Review Focus 5: values changed inside Edit live in the draft. They survive closing and reopening the
    editor and a rebuild of the table, show live in the view row, and appear in the change summary. Closing
    never discards; only Discard does. A bad timed value still blocks Save. The on/off switch toggles directly."""
    out, base = run_rubric_screen(api, """
run(async function () {
  renderRubric(1, []);
  await flush();
  var out = {};
  var NEW_POINTS = rb.base.checks.filter(function (c) { return c.check_id === "sc01"; })[0].points + 1;
  var rows = group("sc01");
  var edit = button(rows[1], "Edit");
  fire(edit, "click");
  var points = labelled(rows[2], "Points");
  points.value = String(NEW_POINTS);
  fire(points, "input");
  out.livePoints = viewPoints("sc01");
  var autoFail = labelled(rows[2], "Auto-fail");
  autoFail.checked = true;
  fire(autoFail, "change");
  out.liveLine1 = rows[0].textContent;
  fire(edit, "click");
  out.closed = Boolean(rows[2].hidden);
  fire(edit, "click");
  out.reopenedPoints = valueOf(labelled(rows[2], "Points"));
  out.reopenedAutoFail = Boolean(labelled(rows[2], "Auto-fail").checked);

  var timed = group("pr-call-length");
  fire(button(timed[1], "Edit"), "click");
  var limit = labelled(timed[2], "Limit");
  limit.value = "12";
  fire(limit, "input");
  out.liveApplies = viewApplies("pr-call-length");
  fire(button(timed[2], "Done"), "click");
  out.timedClosed = Boolean(timed[2].hidden);
  fire(button(timed[1], "Edit"), "click");
  out.reopenedLimit = valueOf(labelled(timed[2], "Limit"));

  var on = fields(group("sc02")[0])[0];
  on.checked = false;
  fire(on, "change");
  out.offClass = group("sc02")[0].classList.contains("off");

  moveCheck("scorecard", 0, 1);
  moveCheck("scorecard", 1, -1);
  out.rebuiltPoints = valueOf(labelled(group("sc01")[2], "Points"));
  out.rebuiltLimit = valueOf(labelled(group("pr-call-length")[2], "Limit"));
  out.changes = changes();
  out.saveEnabled = !rb.els.save.disabled;

  limit = labelled(group("pr-call-length")[2], "Limit");
  limit.value = "0";
  fire(limit, "input");
  fire(rb.els.save, "click");
  out.badLimit = rb.els.saveMessage.textContent;
  out.puts = calls.filter(function (c) { return c.method === "PUT"; }).length;

  fire(button(screen, "Discard changes"), "click");
  out.discarded = { changes: changes(), points: viewPoints("sc01"), applies: viewApplies("pr-call-length"),
                    open: Boolean(!group("sc01")[2].hidden) };
  return out;
});
""")
    checks = by_id(base["checks"])
    old, new = checks["sc01"]["points"], checks["sc01"]["points"] + 1
    assert out["livePoints"] == str(new), "the view row's points follow the editor as you type"
    assert "auto-fail" in out["liveLine1"], "turning on auto-fail shows its chip in the view row"
    assert out["closed"] is True
    assert out["reopenedPoints"] == str(new) and out["reopenedAutoFail"] is True, "closing the editor keeps its values"
    assert out["liveApplies"] == checks["pr-call-length"]["applies_when"] + " · Limit 12 min · Urgent exempt"
    assert out["timedClosed"] is True and out["reopenedLimit"] == "12", "Done closes the editor and keeps its values"
    assert out["offClass"] is True, "the on/off switch toggles directly from the view row"
    assert out["rebuiltPoints"] == str(new) and out["rebuiltLimit"] == "12", "a reorder keeps open editors and values"
    summary = checks["sc01"]["summary"]
    assert f"Edited “{summary}”: points {old} → {new}, now auto-fail" in out["changes"]
    assert f"Edited “{checks['sc02']['summary']}”: turned off" in out["changes"]
    assert "Call length limit: 10 → 12 minutes" in out["changes"]
    assert out["saveEnabled"] is True
    assert out["badLimit"] == "The call length limit must be a whole number of minutes, 1 or more."
    assert out["puts"] == 0, "a bad limit blocks Save"
    assert out["discarded"] == {"changes": ["No unsaved changes."], "points": str(old),
                                "applies": checks["pr-call-length"]["applies_when"] + " · Limit 10 min · Urgent exempt",
                                "open": False}, "only Discard throws the edits away"


def test_rubric_has_no_rereview_panel():
    """Phase 4 spec 4.5: Settings' Run now (with its Re-review option) is the one place to run or re-score a
    date range. Rubric & rules has no range panel of its own."""
    from test_web_overview import js_function

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Re-review a past range" not in js
    assert "rereviewPanel" not in js and "rereviewNote" not in js
    run_now = js_function(js, "runNowPanel")
    assert 'id: "run-rereview"' in run_now and '"Re-review"' in run_now, "Settings' Run now still offers Re-review"


def test_save_pointer_copy(api):
    """Phase 4 spec 4.5: after a version is saved, the save panel points to Run now in Settings for scoring past
    calls under it. The pointer isn't shown before a save."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert json.dumps(SAVE_POINTER) in js
    out, base = run_rubric_screen(api, """
run(async function () {
  renderRubric(1, []);
  await flush();
  var out = { before: Boolean(rb.els.pointer.hidden) };
  var on = fields(group("sc01")[0])[0];
  on.checked = false;
  fire(on, "change");
  var note = screen.findAll(function (e) { return e.getAttribute("id") === "rb-note"; })[0];
  note.value = "Turned off #1";
  fire(note, "input");
  fire(rb.els.save, "click");
  await flush();
  await flush();
  var pointer = rb.els.pointer;
  var links = pointer.findAll(function (e) { return e.tagName === "A"; });
  out.after = { hidden: Boolean(pointer.hidden), text: pointer.textContent, inPanel: pointer.isConnected,
                links: links.map(function (a) { return [a.textContent, a.getAttribute("href")]; }),
                message: rb.els.saveMessage.textContent };
  out.put = calls.filter(function (c) { return c.method === "PUT"; }).length;
  return out;
});
""")
    assert out["before"] is True, "no pointer before a save"
    assert out["put"] == 1
    assert out["after"] == {"hidden": False, "text": SAVE_POINTER, "inPanel": True,
                            "links": [["Run now in Settings", "#/settings"]],
                            "message": f"Saved as version {base['version_id'] + 1}. It applies from the next run."}


DEADLINE_WARNING = "The daily 06:00 run may review calls before this deadline has passed."


def test_editor_warns_over_360(api):
    """A deadline over 6 hours warns that the daily 06:00 run may review calls before it has passed, but the
    save still goes through. The field takes minutes, the change list says minutes, and a bad value blocks Save."""
    out, _ = run_rubric_screen(api, """
run(async function () {
  renderRubric(1, []);
  await flush();
  var rows = group("pr-email-sent");
  fire(button(rows[1], "Edit"), "click");
  var deadline = labelled(rows[2], "Deadline");
  var label = rows[2].findAll(function (e) { return e.tagName === "LABEL" && e.textContent.indexOf("Deadline") === 0; })[0];
  function warned() { return rows[2].textContent.indexOf(""" + json.dumps(DEADLINE_WARNING) + """) >= 0; }
  var out = { label: label.textContent, start: valueOf(deadline), max: String(deadline.max || deadline.getAttribute("max")),
              warnedAt30: warned() };
  deadline.value = "360";
  fire(deadline, "input");
  out.warnedAt360 = warned();
  deadline.value = "400";
  fire(deadline, "input");
  out.warnedAt400 = warned();
  deadline.value = "90";
  fire(deadline, "input");
  out.warnedAt90 = warned();
  deadline.value = "400";
  fire(deadline, "input");
  out.summary = viewApplies("pr-email-sent");
  out.changes = changes();
  deadline.value = "1441";
  fire(deadline, "input");
  fire(rb.els.save, "click");
  out.tooLong = rb.els.saveMessage.textContent;
  deadline.value = "400";
  fire(deadline, "input");
  var note = screen.findAll(function (e) { return e.getAttribute("id") === "rb-note"; })[0];
  note.value = "Longer deadline";
  fire(note, "input");
  fire(rb.els.save, "click");
  await flush();
  var puts = calls.filter(function (c) { return c.method === "PUT"; });
  out.puts = puts.length;
  out.sent = puts.length ? puts[0].body.params.email_deadline_minutes : null;
  return out;
});
""")
    assert out["label"].startswith("Deadline") and out["label"].endswith("minutes after the call ends"), out["label"]
    assert out["start"] == "30" and out["max"] == "1440"
    assert out["warnedAt30"] is False and out["warnedAt360"] is False
    assert out["warnedAt400"] is True and out["warnedAt90"] is False, "the warning follows the value"
    assert out["summary"].endswith(" · Within 6 h 40 min of the call")
    assert "Summary email deadline: 30 → 400 minutes" in out["changes"]
    assert out["tooLong"] == "The summary email deadline must be a whole number of minutes, from 1 to 1440."
    assert out["puts"] == 1 and out["sent"] == 400, "the warning never blocks the save"
