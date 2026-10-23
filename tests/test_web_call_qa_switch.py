"""The "Show Call QA" switch (T-076): off, the side panel lists only Team activity and Settings, Call QA's
addresses open Team activity, the run and budget banners stay away and Settings drops the Call QA group; on, the
page is as it always was. app.js runs in node on the fake DOM through the stand-in page of test_web_test_view."""
from __future__ import annotations

import json
import re
from pathlib import Path

from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_test_view import answer, page_responses, run_page

CALL_QA_ADDRESSES = ["#/overview", "#/agents", "#/techs", "#/tickets", "#/rubric", "#/overview?compare=none",
                     "#/agents/lee@example.com", "#/tickets?ticket=5", "#/nonsense"]
FAILED_RUN = {"last_run": {"run_id": 3, "status": "failed", "alert": True, "error_label": "Zendesk error"},
              "awaiting_confirmation": [], "submission_unknown": []}

HELPERS = """
function shownLinks() {
  return all(sidenav, function (e) { return e.tagName === "A" && !e.hidden; }).map(function (a) { return a.textContent; });
}
function bannersText() { return document.getElementById("banners").textContent; }
function box() {
  return all(panelTitled("Sections"), function (e) { return e.tagName === "INPUT"; })[0];
}
function puts() {
  return requests.filter(function (r) { return r.method === "PUT"; }).map(function (r) { return r.body; });
}
"""


def responses_for(tmp_path, show_call_qa: bool, *paths) -> dict:
    responses = page_responses(api_for(tmp_path), *paths)
    body = responses["GET /api/settings"]["body"]
    body["show_call_qa"] = show_call_qa
    responses["GET /api/status"] = answer(FAILED_RUN)
    return responses


def test_off_the_side_panel_lists_only_team_activity_and_settings(tmp_path, version):
    out = run_page("#/team", responses_for(tmp_path, False, "/api/activity"), HELPERS + """
run(async function () { return shownLinks(); });
""")
    assert out == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"]


def test_on_the_side_panel_lists_everything(tmp_path, version):
    out = run_page("#/overview", responses_for(tmp_path, True), HELPERS + """
run(async function () { return { shown: shownLinks(), hash: location.hash }; });
""")
    assert out["shown"] == ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team",
                            "Activity", "Trends", "1:1 Prep", "Settings"]
    assert out["hash"] == "#/overview"


def test_off_the_app_opens_to_team_activity(tmp_path, version):
    out = run_page("", responses_for(tmp_path, False, "/api/activity"), HELPERS + """
run(async function () {
  return { hash: location.hash, lit: settingsTab(), title: titleEl.textContent };
});
""")
    assert out == {"hash": "#/team", "lit": ["team"], "title": "Team › Activity"}


def test_on_the_app_still_opens_to_the_team_overview(tmp_path, version):
    out = run_page("", responses_for(tmp_path, True), HELPERS + """
run(async function () { return { hash: location.hash, lit: settingsTab() }; });
""")
    assert out == {"hash": "#/overview", "lit": ["overview"]}


def test_off_every_call_qa_address_is_replaced_with_team_activity(tmp_path, version):
    out = run_page("#/team", responses_for(tmp_path, False, "/api/activity"), HELPERS + """
run(async function () {
  var seen = [];
  var replaced = [];
  var original = history.replaceState;
  history.replaceState = function (state, title, url) { replaced.push(url); original(state, title, url); };
  for (var hash of %s) {
    await go(hash);
    seen.push({ hash: location.hash, lit: settingsTab(), title: titleEl.textContent });
  }
  await go("#/team/trends");
  seen.push({ hash: location.hash, lit: settingsTab() });
  return { seen: seen, replaced: replaced, pushed: requests.filter(function (r) {
    return r.url.indexOf("/api/overview") === 0 || r.url.indexOf("/api/tickets") === 0 || r.url.indexOf("/api/agents") === 0;
  }).length };
});
""" % json.dumps(CALL_QA_ADDRESSES))
    team = {"hash": "#/team", "lit": ["team"], "title": "Team › Activity"}
    assert out["seen"] == [team] * len(CALL_QA_ADDRESSES) + [{"hash": "#/team/trends", "lit": ["trends"]}]
    assert out["replaced"] == ["#/team"] * len(CALL_QA_ADDRESSES), "replaced, not pushed, so Back doesn't bounce"
    assert out["pushed"] == 0, "no Call QA screen is read"


def test_off_no_banner_is_drawn_and_the_status_is_not_read(tmp_path, version):
    out = run_page("#/team", responses_for(tmp_path, False, "/api/activity"), HELPERS + """
run(async function () {
  await go("#/settings");
  return { banners: bannersText(), status: requests.filter(function (r) { return r.url === "/api/status"; }).length };
});
""")
    assert out == {"banners": "", "status": 0}


def test_on_the_failed_run_banner_is_drawn(tmp_path, version):
    out = run_page("#/team", responses_for(tmp_path, True, "/api/activity"), HELPERS + """
run(async function () { return bannersText(); });
""")
    assert "The last run (run 3) failed" in out


def test_off_settings_keeps_the_app_wide_panels_and_drops_call_qa(tmp_path, version):
    out = run_page("#/settings", responses_for(tmp_path, False), HELPERS + """
run(async function () {
  return {
    panels: panels().map(titleOf),
    heads: all(screenEl, function (e) { return e.tagName === "H3"; }).map(function (e) { return e.textContent; }),
    checked: !!box().checked,
    sub: panelTitled("Sections").children.filter(function (c) { return c.classList.contains("sub"); })[0].textContent,
    label: all(panelTitled("Sections"), function (e) { return e.tagName === "LABEL"; })[0].textContent
  };
});
""")
    assert out["panels"] == ["Team", "Time off", "1:1 schedule", "Sections", "Activity flags", "Zendesk and credentials"]
    assert out["heads"] == []
    assert out["checked"] is False
    assert out["label"] == "Show Call QA"
    assert out["sub"] == ("What this PC shows, and whether it reads Team activity from Zendesk. A hidden section "
                          "keeps its data, and Call QA's daily review keeps running.")


def test_on_settings_has_the_call_qa_group_and_the_switch_is_on(tmp_path, version):
    out = run_page("#/settings", responses_for(tmp_path, True), HELPERS + """
run(async function () {
  return { panels: panels().map(titleOf), checked: !!box().checked,
    heads: all(screenEl, function (e) { return e.tagName === "H3"; }).map(function (e) { return e.textContent; }) };
});
""")
    assert out["heads"] == ["Call QA"] and out["checked"] is True
    assert out["panels"][:6] == ["Team", "Time off", "1:1 schedule", "Sections", "Activity flags", "Zendesk and credentials"]
    assert "Run now for a date range" in out["panels"] and "Reviews" in out["panels"]


def test_toggling_the_switch_saves_and_updates_the_side_panel_without_a_reload(tmp_path, version):
    responses = responses_for(tmp_path, True)
    saved_off = dict(responses["GET /api/settings"]["body"], show_call_qa=False)
    saved_on = dict(responses["GET /api/settings"]["body"], show_call_qa=True)
    responses["PUT /api/settings"] = answer(saved_off)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  var out = { before: shownLinks().length };
  box().checked = false;
  fire(box(), "change");
  await settle();
  out.puts = puts();
  out.after = shownLinks();
  out.panels = panels().map(titleOf);
  out.heads = all(screenEl, function (e) { return e.tagName === "H3"; }).length;
  out.banners = bannersText();
  out.note = all(panelTitled("Sections"), function (e) { return e.classList.contains("message"); })[0].textContent;
  out.hash = location.hash;
  return out;
});
""")
    assert out["before"] == 10
    assert out["puts"] == [{"show_call_qa": False}]
    assert out["after"] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"]
    assert out["panels"] == ["Team", "Time off", "1:1 schedule", "Sections", "Activity flags", "Zendesk and credentials"]
    assert out["heads"] == 0 and out["banners"] == ""
    assert out["note"] == "Saved." and out["hash"] == "#/settings"

    responses["PUT /api/settings"] = answer(saved_on)
    responses["GET /api/settings"] = answer(saved_off)
    back = run_page("#/settings", responses, HELPERS + """
run(async function () {
  box().checked = true;
  fire(box(), "change");
  await settle();
  return { puts: puts(), shown: shownLinks(), heads: all(screenEl, function (e) { return e.tagName === "H3"; }).length,
           banners: bannersText() };
});
""")
    assert back["puts"] == [{"show_call_qa": True}]
    assert back["shown"] == ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team",
                             "Activity", "Trends", "1:1 Prep", "Settings"]
    assert back["heads"] == 1
    assert "The last run (run 3) failed" in back["banners"]


def test_a_failed_save_puts_the_switch_back_and_says_why(tmp_path, version):
    responses = responses_for(tmp_path, True)
    responses["PUT /api/settings"] = answer({"detail": "Show Call QA must be on or off"}, 400)
    out = run_page("#/settings", responses, HELPERS + """
run(async function () {
  box().checked = false;
  fire(box(), "change");
  await settle();
  var message = all(panelTitled("Sections"), function (e) { return e.classList.contains("message"); })[0];
  return { checked: !!box().checked, disabled: box().disabled, message: message.textContent,
           kind: message.className, shown: shownLinks().length };
});
""")
    assert out == {"checked": True, "disabled": False, "message": "Show Call QA must be on or off",
                   "kind": "message error", "shown": 10}


def test_toggling_keeps_what_is_typed_in_the_other_panels(tmp_path, version):
    responses = responses_for(tmp_path, True)
    body = responses["GET /api/settings"]["body"]
    responses["PUT /api/settings"] = answer(dict(body, show_call_qa=False))
    out = run_page("#/settings", responses, "var ON_BODY = %s;\n" % json.dumps(body) + HELPERS + """
function flagBox() { return all(panelTitled("Activity flags"), function (e) { return e.tagName === "INPUT"; })[0]; }
function subdomain() { return all(screenEl, function (e) { return e.getAttribute("id") === "set-subdomain"; })[0]; }
function groupHeads() {
  return all(screenEl, function (e) { return e.tagName === "H3"; }).map(function (e) { return e.textContent; });
}
run(async function () {
  var flag = flagBox(), zendesk = subdomain();
  flag.value = "77";
  zendesk.value = "typed-not-saved";
  box().checked = false;
  fire(box(), "change");
  await settle();
  var off = { flag: flagBox().value, same: flagBox() === flag && subdomain() === zendesk,
              zendesk: subdomain().value, heads: groupHeads(), panels: panels().map(titleOf) };
  RESPONSES["PUT /api/settings"] = { status: 200, body: ON_BODY };
  box().checked = true;
  fire(box(), "change");
  await settle();
  var back = { flag: flagBox().value, same: flagBox() === flag && subdomain() === zendesk,
               zendesk: subdomain().value, heads: groupHeads(), panels: panels().map(titleOf),
               enabled: !box().disabled };
  return { off: off, back: back };
});
""")
    assert out["off"]["flag"] == "77" and out["off"]["same"] and out["off"]["zendesk"] == "typed-not-saved"
    assert out["off"]["heads"] == []
    assert out["off"]["panels"] == ["Team", "Time off", "1:1 schedule", "Sections", "Activity flags", "Zendesk and credentials"]
    assert out["back"]["flag"] == "77" and out["back"]["same"] and out["back"]["zendesk"] == "typed-not-saved"
    assert out["back"]["heads"] == ["Call QA"] and out["back"]["enabled"]
    assert "Run now for a date range" in out["back"]["panels"] and "Reviews" in out["back"]["panels"]


def test_the_static_page_starts_with_the_call_qa_links_hidden():
    """Call QA's links start hidden until the Show Call QA setting is read; Team's do too, 1:1 prep's included (R9:
    they wait for Show Team activity), so only Settings shows before the first read."""
    html = (Path(__file__).resolve().parent.parent / "src" / "work_assistant" / "web" / "static" / "index.html").read_text(
        encoding="utf-8")
    links = re.findall(r"<a\b[^>]*>", html)
    call_qa = [a for a in links
               if 'data-section="callqa"' in a or re.search(r'data-tab="(overview|agents|tickets|rubric)"', a)]
    team = [a for a in links if 'data-section="team"' in a or re.search(r'data-tab="(team|trends|oneonone)"', a)]
    assert len(call_qa) == 5 and len(team) == 4
    assert all(re.search(r"\shidden[\s>]", a) for a in call_qa + team)
    assert not any(re.search(r"\shidden[\s>]", a) for a in links if a not in call_qa + team)


def test_off_an_old_model_test_address_opens_settings(tmp_path, version):
    out = run_page("#/team", responses_for(tmp_path, False, "/api/activity"), HELPERS + """
run(async function () {
  var replaced = [];
  var original = history.replaceState;
  history.replaceState = function (state, title, url) { replaced.push(url); original(state, title, url); };
  var seen = [];
  for (var hash of ["#/settings/tests/5", "#/settings/tests/5/12"]) {
    await go(hash);
    seen.push({ hash: location.hash, lit: settingsTab(), panels: panels().map(titleOf).length > 0 });
  }
  return { seen: seen, replaced: replaced,
           testReads: requests.filter(function (r) { return r.url.indexOf("/api/tests/") === 0; }).length };
});
""")
    settings = {"hash": "#/settings", "lit": ["settings"], "panels": True}
    assert out["seen"] == [settings, settings]
    assert out["replaced"] == ["#/settings", "#/settings"]
    assert out["testReads"] == 0
