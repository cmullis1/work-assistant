"""The PC copy's cut-over switches (hosted Team activity plan, Task 4; hosted Team plan, Task 4): "Show Team activity"
hides Activity and Trends as "Show Call QA" hides Call QA, "Show 1:1 Prep" hides 1:1 Prep, and "Sync Team activity"
off hides Refresh (the daily task's side is in tests/test_transfer.py). All three are
switches in the Sections panel, all are on by default, and the hosted edition ignores them. app.js runs in
node on the fake DOM through the stand-in page of test_web_test_view."""
from __future__ import annotations

import json

from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_edition import hosted_responses
from test_web_test_view import answer, page_responses, run_page
from work_assistant.settings import DEFAULTS

FULL_LINKS = ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team", "Activity", "Trends",
              "1:1 Prep", "Settings"]
CALL_QA_LINKS = ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Settings"]
# Show Team activity off: 1:1 Prep stays under the Team heading.
NO_ACTIVITY_LINKS = ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team", "1:1 Prep", "Settings"]
NO_PREP_LINKS = ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team", "Activity", "Trends",
                 "Settings"]
NO_FAILED_RUN = {"last_run": None, "awaiting_confirmation": [], "submission_unknown": []}

HELPERS = """
function shownLinks() {
  return all(sidenav, function (e) { return e.tagName === "A" && !e.hidden; }).map(function (a) { return a.textContent; });
}
function refresh() { return all(screenEl, function (e) { return e.getAttribute("id") === "ta-refresh"; }).length; }
function updated() {
  return all(screenEl, function (e) {
    return e.tagName === "SPAN" && e.classList.contains("message") && e.textContent.indexOf("pdated") >= 0;
  }).length;
}
function sectionBox(id) { return all(screenEl, function (e) { return e.getAttribute("id") === id; })[0]; }
function puts() {
  return requests.filter(function (r) { return r.method === "PUT"; }).map(function (r) { return r.body; });
}
"""


def responses_for(tmp_path, **changes) -> dict:
    responses = page_responses(api_for(tmp_path), "/api/activity", "/api/activity/trends")
    responses["GET /api/settings"]["body"].update(changes)
    responses["GET /api/status"] = answer(NO_FAILED_RUN)
    return responses


def test_all_default_on():
    assert DEFAULTS["activity_sync_enabled"] is True and DEFAULTS["show_team_activity"] is True
    assert DEFAULTS["show_oneonone"] is True


def test_sections_switches_hide_team_activity_and_refresh(tmp_path, version):
    """Show Team activity off: the side panel drops Activity and Trends (1:1 Prep stays, under Team) and their
    addresses open the Team overview (replaced, so Back doesn't bounce), and nothing of them is read. Sync Team
    activity off: Activity has no Refresh, but still says when it was last updated. On (the defaults), both are
    there."""
    hidden = run_page("", responses_for(tmp_path, show_team_activity=False), HELPERS + """
run(async function () {
  var out = { opened: location.hash, shown: shownLinks(), visited: [] };
  for (var hash of ["#/team", "#/team/trends", "#/team?tech=lee@example.com"]) {
    await go(hash);
    out.visited.push(location.hash);
  }
  out.reads = requests.filter(function (r) { return r.url.indexOf("/api/activity") === 0; }).length;
  return out;
});
""")
    assert hidden["opened"] == "#/overview"
    assert hidden["shown"] == NO_ACTIVITY_LINKS
    assert hidden["visited"] == ["#/overview"] * 3
    assert hidden["reads"] == 0, "no Team activity screen is read"

    no_sync = run_page("#/team", responses_for(tmp_path, activity_sync_enabled=False), HELPERS + """
run(async function () { return { refresh: refresh(), updated: updated(), shown: shownLinks() }; });
""")
    assert no_sync == {"refresh": 0, "updated": 1, "shown": FULL_LINKS}

    defaults = run_page("#/team", responses_for(tmp_path), HELPERS + """
run(async function () { return { refresh: refresh(), shown: shownLinks() }; });
""")
    assert defaults == {"refresh": 1, "shown": FULL_LINKS}


def test_with_every_section_hidden_the_app_opens_settings(tmp_path, version):
    out = run_page("", responses_for(tmp_path, show_team_activity=False, show_oneonone=False, show_call_qa=False),
                   HELPERS + """
run(async function () {
  var out = { opened: location.hash, shown: shownLinks(), visited: [] };
  for (var hash of ["#/team", "#/overview", "#/tickets?ticket=5"]) {
    await go(hash);
    out.visited.push(location.hash);
  }
  return out;
});
""")
    assert out == {"opened": "#/settings", "shown": ["Settings"], "visited": ["#/settings"] * 3}


def test_sections_panel_switches_save_and_apply_without_a_reload(tmp_path, version):
    responses = responses_for(tmp_path)
    body = responses["GET /api/settings"]["body"]
    out = run_page("#/settings", responses, "var BODY = %s;\n" % json.dumps(body) + HELPERS + """
run(async function () {
  var sections = panelTitled("Sections");
  var out = {
    labels: all(sections, function (e) { return e.tagName === "LABEL"; }).map(function (e) { return e.textContent; }),
    checked: ["set-show-call-qa", "set-show-team-activity", "set-show-oneonone", "set-activity-sync"].map(function (id) {
      return !!sectionBox(id).checked;
    }),
    hints: all(sections, function (e) { return e.classList.contains("hint"); }).map(function (e) { return e.textContent; })
  };
  RESPONSES["PUT /api/settings"] = { status: 200, body: Object.assign({}, BODY, { show_team_activity: false }) };
  sectionBox("set-show-team-activity").checked = false;
  fire(sectionBox("set-show-team-activity"), "change");
  await settle();
  out.hiddenLinks = shownLinks();
  out.panels = panels().map(titleOf);
  out.note = all(sections, function (e) { return e.classList.contains("message"); })[0].textContent;
  sectionBox("set-show-team-activity").checked = true;
  RESPONSES["PUT /api/settings"] = { status: 200, body: Object.assign({}, BODY) };
  fire(sectionBox("set-show-team-activity"), "change");
  await settle();
  out.shownAgain = shownLinks();
  RESPONSES["PUT /api/settings"] = { status: 200, body: Object.assign({}, BODY, { activity_sync_enabled: false }) };
  sectionBox("set-activity-sync").checked = false;
  fire(sectionBox("set-activity-sync"), "change");
  await settle();
  out.puts = puts();
  await go("#/team");
  out.refresh = refresh();
  return out;
});
""")
    assert out["labels"] == ["Show Call QA", "Show Team activity", "Show 1:1 Prep", "Sync Team activity"]
    assert out["checked"] == [True, True, True, True]
    assert out["hints"] == ["Turn off only after the hosted copy collects Team activity and counts 1:1 preps from "
                            "the collected data: the daily task stops reading it, and Refresh is hidden."]
    assert out["hiddenLinks"] == NO_ACTIVITY_LINKS
    assert out["note"] == "Saved."
    assert "Sections" in out["panels"] and "Run now for a date range" in out["panels"], "Settings stays as it was"
    assert out["shownAgain"] == FULL_LINKS
    assert out["puts"] == [{"show_team_activity": False}, {"show_team_activity": True}, {"activity_sync_enabled": False}]
    assert out["refresh"] == 0, "Activity, opened after Sync Team activity was turned off, has no Refresh"


def test_a_refused_switch_is_put_back_and_says_why(tmp_path, version):
    responses = responses_for(tmp_path)
    responses["PUT /api/settings"] = answer({"detail": "Show Team activity must be on or off"}, 400)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var box = sectionBox("set-show-team-activity");
  box.checked = false;
  fire(box, "change");
  await settle();
  var message = all(panelTitled("Sections"), function (e) { return e.classList.contains("message"); })[0];
  return { checked: !!box.checked, disabled: box.disabled, message: message.textContent, shown: shownLinks() };
});
""")
    assert out == {"checked": True, "disabled": False, "message": "Show Team activity must be on or off",
                   "shown": FULL_LINKS}


def test_the_hosted_edition_ignores_them(tmp_path, version):
    """The hosted copy's own timer syncs and the Team section is all it shows: the PC's switches change nothing, and
    it has no Refresh (the owner, 2026-10-22: the page says when the next scheduled sync comes instead)."""
    responses = hosted_responses(tmp_path)
    responses["GET /api/settings"]["body"].update(show_team_activity=False, show_oneonone=False,
                                                  activity_sync_enabled=False)
    out = run_page("", responses, HELPERS + """
run(async function () {
  var out = { opened: location.hash, shown: shownLinks(), refresh: refresh() };
  await go("#/settings");
  out.switches = ["set-show-team-activity", "set-show-oneonone", "set-activity-sync"].filter(function (id) {
    return sectionBox(id);
  });
  return out;
});
""")
    assert out == {"opened": "#/team", "shown": ["Team", "Activity", "Trends", "1:1 Prep", "Settings"],
                   "refresh": 0, "switches": []}


# -- Show 1:1 Prep (hosted Team design §5) --------------------------------------------------------------------------


def test_show_oneonone_switch_hides_section(tmp_path, version):
    """Show 1:1 Prep off: the side panel drops 1:1 Prep alone, its addresses open the home, and no prep is read.
    With Show Team activity off instead, 1:1 Prep stays under the Team heading, which leads to it. The switch in the
    Sections panel saves at once and applies without a reload."""
    script = HELPERS + """
function teamHref() {
  return all(sidenav, function (e) { return e.tagName === "A" && e.textContent === "Team"; })[0].getAttribute("href");
}
run(async function () {
  var out = { opened: location.hash, shown: shownLinks(), visited: [] };
  for (var hash of ["#/oneonone", "#/oneonone?tech=lee@example.com", "#/team"]) {
    await go(hash);
    out.visited.push(location.hash);
  }
  out.prepReads = requests.filter(function (r) { return r.url.indexOf("/api/prep") === 0; }).length;
  out.teamHref = teamHref();
  return out;
});
"""
    off = run_page("", responses_for(tmp_path, show_oneonone=False), script)
    assert off == {"opened": "#/overview", "shown": NO_PREP_LINKS,
                   "visited": ["#/overview", "#/overview", "#/team"], "prepReads": 0, "teamHref": "#/team"}

    prep_only = run_page("", responses_for(tmp_path, show_team_activity=False), script)
    assert prep_only["shown"] == NO_ACTIVITY_LINKS
    assert prep_only["visited"] == ["#/oneonone", "#/oneonone?tech=lee@example.com", "#/overview"]
    assert prep_only["teamHref"] == "#/oneonone", "the Team heading leads to the one screen under it"

    alone = run_page("", responses_for(tmp_path, show_team_activity=False, show_call_qa=False), HELPERS + """
run(async function () { return { opened: location.hash, shown: shownLinks() }; });
""")
    assert alone == {"opened": "#/oneonone", "shown": ["Team", "1:1 Prep", "Settings"]}

    responses = responses_for(tmp_path)
    body = responses["GET /api/settings"]["body"]
    switched = run_page("#/settings", responses, "var BODY = %s;\n" % json.dumps(body) + HELPERS + """
run(async function () {
  RESPONSES["PUT /api/settings"] = { status: 200, body: Object.assign({}, BODY, { show_oneonone: false }) };
  var box = sectionBox("set-show-oneonone");
  box.checked = false;
  fire(box, "change");
  await settle();
  var out = { puts: puts(), hidden: shownLinks(), panels: panels().map(titleOf) };
  RESPONSES["PUT /api/settings"] = { status: 200, body: Object.assign({}, BODY) };
  box.checked = true;
  fire(box, "change");
  await settle();
  out.puts = puts();
  out.shownAgain = shownLinks();
  return out;
});
""")
    assert switched["puts"] == [{"show_oneonone": False}, {"show_oneonone": True}]
    assert switched["hidden"] == NO_PREP_LINKS
    assert "Sections" in switched["panels"] and "1:1 schedule" in switched["panels"], \
        "Settings stays as it was, the 1:1 schedule panel included"
    assert switched["shownAgain"] == FULL_LINKS


# -- no flash of the Team activity links before the setting is read (R9) -------------------------------------------

# As the real page starts: the links index.html hides start hidden, and the first settings read waits until the
# scenario calls release(), so what the side panel shows before the answer can be read.
HELD_START = """
sidenav.findAll(function (e) { return e.tagName === "A" && HIDDEN_AT_START.indexOf(e.textContent) >= 0; })
  .forEach(function (a) { a.hidden = true; });
var held = [], released = false, answerNow = globalThis.fetch;
globalThis.fetch = function (url, options) {
  if (url === "/api/settings" && !released) {
    return new Promise(function (resolve) { held.push(function () { resolve(answerNow(url, options)); }); });
  }
  return answerNow(url, options);
};
function release() { released = true; held.forEach(function (f) { f(); }); }
"""


def hidden_at_start() -> list[str]:
    """The side panel's links that index.html starts hidden, by their text."""
    import html as markup
    import re

    from test_web_overview import STATIC

    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return [markup.unescape(text.strip()) for attrs, text in re.findall(r"<a\b([^>]*)>([^<]*)</a>", page)
            if re.search(r"\shidden\b", attrs)]


def held_page(responses: dict) -> dict:
    from test_web_overview import STATIC, node_prelude, run_node
    from test_web_test_view import PAGE_JS

    script = (node_prelude() + 'var START_HASH = "";\n' + f"var RESPONSES = {json.dumps(responses)};\n"
              f"var APP_JS = {json.dumps(str(STATIC / 'app.js'))};\n"
              f"var HIDDEN_AT_START = {json.dumps(hidden_at_start())};\n"
              + PAGE_JS.replace("require(APP_JS);", HELD_START + "require(APP_JS);") + HELPERS + """
run(async function () {
  var out = { waiting: shownLinks(), held: held.length, reads: requests.length };
  release();
  await settle();
  out.after = shownLinks();
  return out;
});
""")
    return run_node(script)


def test_team_activity_links_start_hidden_until_the_setting_is_read(tmp_path, version):
    """On the PC with Show Team activity off, its links never show, not even while the first settings read is on
    its way: they start hidden like Call QA's, and the page shows each section once the setting is read."""
    assert hidden_at_start() == ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team",
                                 "Activity", "Trends", "1:1 Prep"], "only Settings shows before the settings are read"
    off = held_page(responses_for(tmp_path, show_team_activity=False, show_oneonone=False))
    assert off == {"waiting": ["Settings"], "held": 1, "reads": 0, "after": CALL_QA_LINKS}, \
        "the page waits on the settings, reading nothing else"
    assert held_page(responses_for(tmp_path, show_oneonone=False))["after"] == NO_PREP_LINKS
    on = held_page(responses_for(tmp_path))
    assert (on["waiting"], on["after"]) == (["Settings"], FULL_LINKS)
    hosted = held_page(hosted_responses(tmp_path))
    assert (hosted["waiting"], hosted["after"]) == (["Settings"], ["Team", "Activity", "Trends", "1:1 Prep", "Settings"])
    down = responses_for(tmp_path)
    down["GET /api/settings"] = answer({"detail": "The app is starting."}, 503)
    assert held_page(down)["after"] == ["Settings"], "a failed first read shows no section, as for Call QA"
