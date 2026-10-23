"""The side panel that replaces the top-bar tabs, and the routes it links to (Team activity design §5, Task 7).
app.js runs in node on the fake DOM through the stand-in page of test_web_test_view, whose markup mirrors
index.html."""
from __future__ import annotations

import json
import re

from css_rules import STATIC, app_css, media_block, rules, top_level, values
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_test_view import page_responses, run_page

# What every scenario reads back: which links are lit, the breadcrumb, the document title and the screen.
READ_PAGE = """
function lit() {
  return document.querySelectorAll(".sidenav a").filter(function (a) { return a.classList.contains("on"); })
    .map(function (a) { return a.getAttribute("data-tab"); });
}
function parentOpen() {
  return document.querySelectorAll(".sidenav a").filter(function (a) { return a.classList.contains("parent"); })[0]
    .classList.contains("open");
}
function snapshot() {
  var crumb = all(titleEl, function (e) { return e.classList.contains("crumb"); })[0];
  return { lit: lit(), parentOpen: parentOpen(), title: titleEl.textContent, crumb: crumb ? crumb.textContent : null,
    docTitle: document.title, screen: screenEl.textContent };
}
"""


def visit(tmp_path, start, *hashes):
    """The snapshot for `start`, then for each of `hashes` visited in turn."""
    out = run_page(start, page_responses(api_for(tmp_path), "/api/activity"), READ_PAGE + """
run(async function () {
  var out = [snapshot()];
  for (var hash of %s) { await go(hash); out.push(snapshot()); }
  return out;
});
""" % json.dumps(list(hashes)))
    return out


def test_sidenav_lists_sections_in_order(tmp_path, conn, version):
    out = run_page("#/overview", page_responses(api_for(tmp_path)), """
run(async function () {
  return { links: links(sidenav).map(function (l) { return l.text; }),
    classes: sidenav.children.map(function (c) { return c.tagName + "." + c.className; }) };
});
""")
    assert out["links"] == ["Call QA", "Team overview", "Techs", "Tickets", "Rubric & rules", "Team",
                            "Activity", "Trends", "1:1 Prep", "Settings"]


def test_index_markup_matches_the_stand_in():
    """The real index.html has the same sections, classes and data attributes the stand-in page models (the Call
    QA and Team activity links start hidden, until the Show Call QA and Show Team activity settings are read)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="tabs"' not in html
    nav = re.search(r'<nav class="sidenav" aria-label="Sections">(.*?)</nav>', html, re.S).group(1)
    items = re.findall(r"<(div|a|span)\b([^>]*)>([^<]*)", nav)
    assert [(tag, " ".join(attrs.split()), text.strip()) for tag, attrs, text in items] == [
        ("div", 'class="brand"', "Work Assistant"),
        ("a", 'class="parent" data-section="callqa" href="#/overview" hidden', "Call QA"),
        ("a", 'class="sub" data-tab="overview" href="#/overview" hidden', "Team overview"),
        ("a", 'class="sub" data-tab="agents" href="#/agents" hidden', "Techs"),
        ("a", 'class="sub" data-tab="tickets" href="#/tickets" hidden', "Tickets"),
        ("a", 'class="sub" data-tab="rubric" href="#/rubric" hidden', "Rubric &amp; rules"),
        ("a", 'class="parent" data-section="team" href="#/team" hidden', "Team"),
        ("a", 'class="sub" data-tab="team" href="#/team" hidden', "Activity"),
        ("a", 'class="sub" data-tab="trends" href="#/team/trends" hidden', "Trends"),
        ("a", 'class="sub" data-tab="oneonone" href="#/oneonone" hidden', "1:1 Prep"),
        ("span", 'class="gap"', ""),
        ("a", 'data-tab="settings" href="#/settings"', "Settings"),
    ]
    assert re.search(r'<button class="menu"[^>]*aria-expanded="false"[^>]*>\s*☰ Menu', html)
    assert 'class="title"' in html and 'id="demo-badge"' in html


def test_call_qa_lit_on_its_screens(tmp_path, conn, version):
    snaps = visit(tmp_path, "#/agents", "#/overview", "#/tickets", "#/rubric", "#/team", "#/settings")
    assert [(s["lit"], s["parentOpen"]) for s in snaps] == [
        (["agents"], True), (["overview"], True), (["tickets"], True), (["rubric"], True),
        (["team"], False), (["settings"], False)]


def test_breadcrumb_and_title(tmp_path, conn, version):
    snaps = visit(tmp_path, "#/overview", "#/agents", "#/tickets", "#/rubric", "#/team", "#/settings")
    assert [(s["title"], s["crumb"], s["docTitle"]) for s in snaps] == [
        ("Call QA › Team overview", "Call QA ›", "Call QA › Team overview · Work Assistant"),
        ("Call QA › Techs", "Call QA ›", "Call QA › Techs · Work Assistant"),
        ("Call QA › Tickets", "Call QA ›", "Call QA › Tickets · Work Assistant"),
        ("Call QA › Rubric & rules", "Call QA ›", "Call QA › Rubric & rules · Work Assistant"),
        ("Team › Activity", "Team ›", "Team › Activity · Work Assistant"),
        ("Settings", None, "Settings · Work Assistant")]


def test_team_route_shows_the_team_activity_screen(tmp_path, conn, version):
    snaps = visit(tmp_path, "#/team")
    assert snaps[0]["lit"] == ["team"]
    assert "Tickets touched per day" in snaps[0]["screen"] and "Built from Zendesk's own records" in snaps[0]["screen"]
    assert "Coming in the next tasks" not in snaps[0]["screen"]


def test_techs_alias_routes_to_agents(tmp_path, conn, version):
    snaps = visit(tmp_path, "#/agents", "#/techs")
    assert snaps[1]["lit"] == ["agents"] and snaps[1]["parentOpen"]
    assert snaps[1]["screen"] == snaps[0]["screen"] and snaps[0]["screen"]
    assert snaps[1]["docTitle"] == "Call QA › Techs · Work Assistant"


def test_old_hashes_still_route(tmp_path, conn, version):
    snaps = visit(tmp_path, "#/overview", "#/agents", "#/tickets", "#/rubric", "#/settings", "#/nonsense",
                  "#/settings/tests/9")
    assert [s["lit"] for s in snaps] == [["overview"], ["agents"], ["tickets"], ["rubric"], ["settings"],
                                         ["overview"], ["settings"]]


def test_menu_button_opens_and_a_link_closes_the_panel(tmp_path, conn, version):
    out = run_page("#/overview", page_responses(api_for(tmp_path)), """
run(async function () {
  function state() { return { open: sidenav.classList.contains("open"), expanded: menuButton.getAttribute("aria-expanded") }; }
  var out = { start: state() };
  fire(menuButton, "click");
  out.opened = state();
  fire(menuButton, "click");
  out.toggled = state();
  fire(menuButton, "click");
  fire(links_a("Tickets"), "click");
  out.afterLink = state();
  return out;
});
function links_a(text) {
  return all(sidenav, function (e) { return e.tagName === "A" && e.textContent === text; })[0];
}
""")
    assert out["start"] == {"open": False, "expanded": "false"}
    assert out["opened"] == {"open": True, "expanded": "true"}
    assert out["toggled"] == {"open": False, "expanded": "false"}
    assert out["afterLink"] == {"open": False, "expanded": "false"}


def test_sidenav_collapses_below_1024():
    css = app_css()
    below = media_block(css, "(max-width: 1023px), (pointer: coarse)")   # and on touch screens (iPad and phone §6)
    wide = top_level(css)
    assert values(wide, ".appbar .menu", "display") == ["none"], "the menu button only shows when the panel collapses"
    assert values(below, ".appbar .menu", "display") and values(below, ".appbar .menu", "display") != ["none"]
    assert values(wide, ".sidenav", "display") == ["flex"]
    assert values(below, ".sidenav", "display") == ["none"]
    assert values(below, ".sidenav.open", "display") == ["flex"]
    assert values(below, ".sidenav", "position") == ["fixed"], "the open panel sits over the page"
    assert values(below, ".shell", "display") == ["block"]
    for selector in (".shell", ".sidenav", ".sidenav a", ".sidenav a.sub", ".sidenav a.parent.open",
                     ".sidenav a.on", ".appbar .title .crumb"):
        assert rules(css, selector), selector + " is styled"
    assert "--side-w" in css


def test_side_panel_styles_use_tokens_only():
    panel = (STATIC / "app.css").read_text(encoding="utf-8")
    section = panel[panel.index("/* -- side panel"):panel.index("@media (min-width: 1120px)")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", section)


def test_the_side_panel_stays_put_while_the_page_scrolls():
    """One screen tall and sticky, so Settings stays at its foot and the page scrolls beside it (owner, 2026-10-21);
    a narrow screen keeps its own fixed, slide-in panel."""
    css = top_level(app_css())
    assert values(css, ".sidenav", "position") == ["sticky"]
    assert values(css, ".sidenav", "top") == ["0"]
    assert values(css, ".sidenav", "height") == ["100vh"]
    assert values(css, ".sidenav", "overflow-y") == ["auto"]
    assert values(css, ".sidenav .gap", "flex") == ["1"]
