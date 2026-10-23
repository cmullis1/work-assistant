"""No-flash loading and kept answers (owner, 2026-10-21; T-143). A screen being drawn again keeps what it shows,
faded (`.stale`) under the loading bar, until the new answer replaces it; only an empty screen says "Loading…".
Returning to a screen, or to filters seen before, draws its kept answer at once and reads again in the background,
drawing again in place only when the answer changed. Any write forgets every kept answer, and so does a sync that
finishes, so a save is never hidden. app.js runs in node on the fake DOM through the stand-in page of
test_web_test_view, whose `waitingRequests` makes requests wait until the test lets them answer."""
from __future__ import annotations

import json
import re

from css_rules import STATIC, app_css, rules, values
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_team_activity import running_body, week_body
from test_web_team_activity import url as activity_url
from test_web_test_view import answer, page_responses, run_page
from test_web_trends import trends
from test_web_trends import url as trends_url

HELPERS = """
var timers = [];
var realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = function (f, ms) { if (ms === 5000) { timers.push(f); return 0; } return realSetTimeout(f, ms); };
function gets(prefix) {
  return requests.filter(function (r) { return r.method === "GET" && r.url === prefix; }).length;
}
function head() { var el = screenEl.querySelectorAll(".period-head")[0]; return el ? el.textContent : null; }
function stale() { return screenEl.classList.contains("stale"); }
function barShown() { return !loadingBarEl.hidden; }
function loadingLines() {
  return screenEl.querySelectorAll(".message").map(function (m) { return m.textContent; })
    .filter(function (t) { return /^Loading/.test(t); });
}
function letGo() {
  var waiting = waitingRequests;
  waitingRequests = null;
  waiting.forEach(function (request) { request.answer(); });
}
function waitingFor(url) { return waitingRequests.filter(function (request) { return request.url === url; }).length; }
function looks() { return { head: head(), stale: stale(), bar: barShown(), loading: loadingLines() }; }
"""

TRENDS_HEAD = "Last 8 weeks (Aug 16 – Oct 10) · 5 techs"
ACTIVITY_HEAD = "Last week (Oct 4 – Oct 10) · 2 techs · 8 scheduled shifts"


def page(tmp_path, start_hash: str, bodies: dict, scenario: str):
    responses = page_responses(api_for(tmp_path))
    for key, body in bodies.items():
        responses[key if " " in key else "GET " + key] = body if " " in key else answer(body)
    return run_page(start_hash, responses, HELPERS + "run(async function () {\n" + scenario + "\n});")


def test_returning_to_a_screen_draws_its_kept_answer_at_once_and_reads_again(tmp_path, conn, version):
    out = page(tmp_path, "#/team/trends", {trends_url(): trends(comparison=None)}, """
  await go("#/settings");
  var before = gets("%s");
  waitingRequests = [];
  await go("#/team/trends");
  var out = { drawn: looks(), asked: gets("%s") - before, waiting: waitingFor("%s") };
  var first = screenEl.children[0];
  letGo();
  await settle();
  out.same = screenEl.children[0] === first;
  return out;
""" % (trends_url(), trends_url(), trends_url()))
    assert out["drawn"] == {"head": TRENDS_HEAD, "stale": False, "bar": False, "loading": []}, \
        "drawn from the kept answer before any answer came"
    assert out["asked"] == 1 and out["waiting"] == 1, "and read again in the background"
    assert out["same"] is True, "the same answer draws nothing again"


def test_a_changed_answer_draws_again_in_place(tmp_path, conn, version):
    changed = trends(comparison=None, coverage_from="2026-07-21")
    out = page(tmp_path, "#/team/trends", {trends_url(): trends(comparison=None)}, """
  await go("#/settings");
  RESPONSES["GET %s"] = { status: 200, body: %s };
  waitingRequests = [];
  await go("#/team/trends");
  var out = { kept: looks() };
  letGo();
  await settle();
  out.fresh = looks();
  return out;
""" % (trends_url(), json.dumps(changed)))
    assert out["kept"]["head"] == TRENDS_HEAD
    assert out["fresh"] == {"head": TRENDS_HEAD + " · data from Jul 21", "stale": False, "bar": False, "loading": []}


def test_a_screen_keeps_its_content_stale_while_the_next_answer_loads(tmp_path, conn, version):
    year = trends(comparison=None)
    year["period"] = {"start": "2025-10-01", "end": "2026-09-30", "label": "Last year (Oct 2025 – Sep 2026)",
                      "so_far": False, "preset": "last_year"}
    out = page(tmp_path, "#/team/trends", {trends_url(): trends(comparison=None),
                                           trends_url(period="last_year"): year}, """
  waitingRequests = [];
  await go("#/team/trends?period=last_year");
  var out = { loading: looks() };
  letGo();
  await settle();
  out.drawn = looks();
  return out;
""")
    assert out["loading"] == {"head": TRENDS_HEAD, "stale": True, "bar": True, "loading": []}, \
        "the screen shown stays, faded under the loading bar, with no Loading line"
    assert out["drawn"] == {"head": "Last year (Oct 2025 – Sep 2026) · 5 techs", "stale": False, "bar": False,
                            "loading": []}


def test_switching_screens_keeps_the_screen_shown_until_the_next_one_draws(tmp_path, conn, version):
    out = page(tmp_path, "#/team/trends", {trends_url(): trends(comparison=None), activity_url(): week_body()}, """
  waitingRequests = [];
  await go("#/team");
  var out = { loading: looks() };
  letGo();
  await settle();
  out.drawn = looks();
  return out;
""")
    assert out["loading"] == {"head": TRENDS_HEAD, "stale": True, "bar": True, "loading": []}
    assert out["drawn"]["head"].startswith(ACTIVITY_HEAD) and not out["drawn"]["stale"] and not out["drawn"]["bar"]


def test_a_write_forgets_every_kept_answer(tmp_path, conn, version):
    settings = api_for(tmp_path).get("/api/settings").json()
    out = page(tmp_path, "#/team/trends", {trends_url(): trends(comparison=None),
                                           "PUT /api/settings": answer(settings)}, """
  await go("#/settings");
  fire(buttonIn(panelTitled("Activity flags"), "Save flags"), "click");
  await settle();
  waitingRequests = [];
  await go("#/team/trends");
  var out = { after: looks(), title: panels()[0] ? titleOf(panels()[0]) : null };
  letGo();
  await settle();
  out.drawn = looks();
  return out;
""")
    assert out["after"]["stale"] is True and out["after"]["bar"] is True, "nothing kept: it waits for the answer"
    assert out["title"] != "Tickets per shift by tech", "Settings stays shown, faded"
    assert out["drawn"]["head"] == TRENDS_HEAD and not out["drawn"]["stale"]


def test_a_sync_that_finishes_forgets_the_kept_answers(tmp_path, conn, version):
    """While a sync runs, an answer read now is kept; once the sync has finished, it no longer is."""
    out = page(tmp_path, "#/team", {activity_url(): running_body(True),
                                    activity_url("last_month"): running_body(True),
                                    trends_url(): trends(comparison=None)}, """
  await go("#/team/trends");
  await go("#/team?period=last_month");
  RESPONSES["GET %s"] = { status: 200, body: %s };
  timers[timers.length - 1]();   // the last month screen's poll: the sync has finished
  await settle();
  waitingRequests = [];
  await go("#/team/trends");
  var out = { trends: looks() };
  letGo();
  await settle();
  return out;
""" % (activity_url("last_month"), json.dumps(running_body(False))))
    assert out["trends"]["stale"] is True and out["trends"]["bar"] is True, \
        "the trends answer kept during the sync is forgotten when it finishes"


def test_the_loading_look_is_built_from_tokens():
    """The stale screen fades by a token, and the loading bar is a thin accent line at the top of the screen, from
    tokens only; it takes no room, so showing it moves nothing. index.html has the bar, hidden, just above the
    screen."""
    css = app_css()
    assert values(css, ".stale", "opacity") == ["var(--stale-opacity)"]
    assert values(css, ".loading-bar", "height") == ["var(--loading-bar-h)"]
    assert values(css, ".loading-bar", "margin") == [
        "calc(-0.5 * (var(--pad-page) + var(--loading-bar-h))) 0 calc(0.5 * (var(--pad-page) - var(--loading-bar-h)))"],         "centred in the gap above the screen, its margins adding up to minus its height"
    assert values(css, ".loading-bar[hidden]", "display") == ["none"]
    for rule in (".stale", ".loading-bar", ".loading-bar::before"):
        assert rules(css, rule), rule
        assert not any(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body) for body in rules(css, rule)), rule
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<div class="loading-bar" id="loading-bar" role="progressbar" aria-label="Loading" hidden>'
                     r'</div>\s*<main id="screen">', html)
