"""The Test a model panel and the comparison views (phase 4 spec §5.2–5.3, Task 7). The whole page (app.js) runs
in node on the fake DOM (tests/fake_dom.js), with `fetch` answering from what the real endpoints return for
synthetic data. Nothing here calls Anthropic or Zendesk, and no test is ever started for real.
"""
from __future__ import annotations

import json
import re

import pytest

from test_compare import add_shown, add_test_run, add_tested, api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_overview import STATIC, js_function, node_prelude, run_node
from work_assistant.settings import set_setting

SONNET = "claude-sonnet-5-5"
SCHEDULE = {"installed": False, "text": "Not installed yet"}
TEST_STARTED = "Test started. It appears in Run history and under Model tests; the comparison opens when it finishes."
MISSING_TEST = "This model test doesn't exist."
ERASED = "Erased after the retention period."

# A stand-in browser around the real app.js: the page shell, `fetch` answering from RESPONSES ("METHOD url" ->
# {status, body}), the window's listeners, and helpers to drive and read the page. Every request is recorded.
PAGE_JS = """
var requests = [];
var windowListeners = {};
globalThis.addEventListener = function (type, f) { (windowListeners[type] = windowListeners[type] || []).push(f); };
globalThis.setInterval = function () { return 0; };
globalThis.location = { hash: START_HASH };
globalThis.history = { replaceState: function (state, title, url) { location.hash = url; } };
globalThis.confirm = function () { return true; };
globalThis.alert = function () {};
// Every media query is false; the page's change listeners are kept, and `mediaChange(query, matches)` plays a change.
var mediaListeners = [];
globalThis.matchMedia = function (query) {
  return { matches: false, media: query,
    addEventListener: function (type, f) { if (type === "change") { mediaListeners.push([query, f]); } } };
};
function mediaChange(query, matches) {
  mediaListeners.forEach(function (l) { if (l[0] === query) { l[1]({ matches: matches, media: query }); } });
}
// While a test sets `waitingRequests` to an array, each request waits in it ({ url, answer }) until the test calls
// its `answer`: what the page draws before an answer comes.
var waitingRequests = null;
globalThis.fetch = function (url, options) {
  var method = (options && options.method) || "GET";
  requests.push({ method: method, url: url, body: options && options.body ? JSON.parse(options.body) : null });
  var answer = RESPONSES[method + " " + url] || { status: 404, body: { detail: "no stand-in for " + method + " " + url } };
  var response = { ok: answer.status < 400, status: answer.status,
    json: function () { return Promise.resolve(JSON.parse(JSON.stringify(answer.body))); } };
  if (waitingRequests) {
    return new Promise(function (resolve) {
      waitingRequests.push({ url: url, answer: function () { resolve(response); } });
    });
  }
  return Promise.resolve(response);
};
function shellEl(tag, attrs, parent, text) {
  var el = document.createElement(tag);
  Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
  if (text) { el.appendChild(document.createTextNode(text)); }
  return (parent || document.body).appendChild(el);
}
// The page's own markup (index.html): the side panel of sections, then the app bar with its menu button and title.
var sidenav = shellEl("nav", { "class": "sidenav", "aria-label": "Sections" });
shellEl("div", { "class": "brand" }, sidenav, "Work Assistant");
shellEl("a", { "class": "parent", "data-section": "callqa", href: "#/overview" }, sidenav, "Call QA");
[["overview", "Team overview"], ["agents", "Techs"], ["tickets", "Tickets"], ["rubric", "Rubric & rules"]]
  .forEach(function (t) { shellEl("a", { "class": "sub", "data-tab": t[0], href: "#/" + t[0] }, sidenav, t[1]); });
shellEl("a", { "class": "parent", "data-section": "team", href: "#/team" }, sidenav, "Team");
shellEl("a", { "class": "sub", "data-tab": "team", href: "#/team" }, sidenav, "Activity");
shellEl("a", { "class": "sub", "data-tab": "trends", href: "#/team/trends" }, sidenav, "Trends");
shellEl("a", { "class": "sub", "data-tab": "oneonone", href: "#/oneonone" }, sidenav, "1:1 Prep");
shellEl("span", { "class": "gap" }, sidenav);
shellEl("a", { "data-tab": "settings", href: "#/settings" }, sidenav, "Settings");
var appbar = shellEl("header", { "class": "appbar" });
var menuButton = shellEl("button", { "class": "menu", "aria-expanded": "false" }, appbar, "\\u2630 Menu");
var titleEl = shellEl("div", { "class": "title" }, appbar);
shellEl("span", { id: "demo-badge" });
shellEl("div", { id: "banners" });
var loadingBarEl = shellEl("div", { "class": "loading-bar", id: "loading-bar", role: "progressbar",
  "aria-label": "Loading" });
loadingBarEl.hidden = true;
var screenEl = shellEl("main", { id: "screen" });
require(APP_JS);

var clockShift = 0, realNow = Date.now;
Date.now = function () { return realNow() + clockShift; };
function later(ms) { clockShift += ms || 1000; }
function flush() { return new Promise(function (resolve) { setTimeout(resolve, 2); }); }
async function settle() { for (var i = 0; i < 6; i++) { await flush(); } }
async function go(hash) {
  location.hash = hash;
  (windowListeners.hashchange || []).forEach(function (f) { f(); });
  await settle();
}
function fire(el, type) {
  (el.listeners[type] || []).forEach(function (f) { f({ target: el, preventDefault: function () {} }); });
}
function all(el, test) { return el.findAll(test); }
function byTag(el, tag) { return all(el, function (e) { return e.tagName === tag; }); }
function buttonIn(el, text) {
  return all(el, function (e) { return e.tagName === "BUTTON" && e.textContent.indexOf(text) === 0; })[0] || null;
}
function panels(el) {
  return all(el || screenEl, function (e) { return e.tagName === "SECTION" && e.classList.contains("panel"); });
}
function titleOf(section) {
  var label = section.children.filter(function (c) { return c.tagName === "H4"; })[0];
  return label ? label.textContent : null;
}
function panelTitled(title) { return panels().filter(function (p) { return titleOf(p) === title; })[0] || null; }
function settingsTab() {
  return document.querySelectorAll(".sidenav a").filter(function (a) { return a.classList.contains("on"); })
    .map(function (a) { return a.getAttribute("data-tab"); });
}
function links(el) {
  return all(el, function (e) { return e.tagName === "A"; }).map(function (a) {
    return { text: a.textContent, href: a.getAttribute("href") };
  });
}
function run(steps) {
  settle().then(steps).then(function (out) {
    process.stdout.write(JSON.stringify(out));
    process.exit(0);
  }).catch(function (error) { console.error(error.stack); process.exit(1); });
}
"""


def run_page(start_hash: str, responses: dict, scenario: str):
    """Load app.js at `start_hash` in node with `responses`, then run `scenario` (JS that calls
    run(async function () { ... return out; })) and return what it returns."""
    script = (node_prelude() + f"var START_HASH = {json.dumps(start_hash)};\n"
              f"var RESPONSES = {json.dumps(responses)};\n"
              f"var APP_JS = {json.dumps(str(STATIC / 'app.js'))};\n" + PAGE_JS + scenario)
    return run_node(script)


def js_pct(value) -> str:
    """app.js's pct(): one decimal place at most, then "%"."""
    return ("%g" % round(value, 1)) + "%"


def answer(body, status=200):
    return {"status": status, "body": body}


def page_responses(api, *paths):
    """What the real endpoints answer for the page's own reads plus `paths`, keyed as the stand-in fetch wants."""
    responses = {"GET /api/schedule": answer(SCHEDULE)}
    for path in ("/api/settings", "/api/status", "/api/runs", "/api/tests", "/api/agreement?weeks=8", "/api/shifts",
                 "/api/time-off", "/api/prep/schedule", *paths):
        got = api.get(path)
        responses["GET " + path] = answer(got.json(), got.status_code)
    return responses


# -- the Settings left column -----------------------------------------------------------------------------


def test_settings_left_column_order(conn, version, tmp_path):
    """Ruling 1: under the Call QA heading the left column is Run now, Test a model, Run history, Schedule; the
    right column is Reviews and Reviewer agreement. Above it sit Team, Time off and the 1:1 schedule beside
    Sections, Activity flags and Zendesk and credentials (Task 12; 1:1 schedule T-092). The Settings tab is the one
    highlighted."""
    out = run_page("#/settings", page_responses(api_for(tmp_path)), """
run(async function () {
  var stacks = all(screenEl, function (e) { return e.tagName === "DIV" && e.classList.contains("stack"); });
  return { columns: stacks.map(function (s) {
    return s.children.map(titleOf);
  }), tab: settingsTab() };
});
""")
    assert out["columns"] == [["Team", "Time off", "1:1 schedule"],
                              ["Sections", "Activity flags", "Zendesk and credentials"],
                              ["Run now for a date range", "Test a model", "Run history", "Schedule"],
                              ["Reviews", "Reviewer agreement"]]
    assert out["tab"] == ["settings"]


# -- the Test a model panel -------------------------------------------------------------------------------

ESTIMATE = {"estimate_usd": 1.234, "contacts": 8, "excluded_purged": 1, "excluded_errored": 2,
            "excluded_unreviewed": 3}

PANEL_JS = """
function testPanel() { return panelTitled("Test a model"); }
function control(id) { return all(testPanel(), function (e) { return e.getAttribute("id") === id; })[0] || null; }
function shown(el) {
  for (var node = el; node && node.tagName; node = node.parentNode) { if (node.hidden) { return false; } }
  return true;
}
function noteShown() {
  var note = all(testPanel(), function (e) { return e.textContent === SONNET_NOTE && e.classList.contains("hint"); })[0];
  return note ? shown(note) : null;
}
function estimateBox() {
  return all(testPanel(), function (e) { return e.classList.contains("estimate"); })[0];
}
function messageText() {
  return all(testPanel(), function (e) { return e.classList.contains("message"); }).map(function (m) {
    return m.textContent; }).filter(Boolean);
}
"""


def test_test_panel_controls(conn, version, tmp_path):
    """Ruling 2: a Model select fed from settings.models with each option's approximate cost, an Effort select,
    first and last days defaulting to last week like Run now, and an optional call limit. The Sonnet effort
    note shows only while Sonnet is picked. Get estimate shows the calls tested, those skipped and why, and the
    estimate; then Start test (in the Run now pattern) posts the same request."""
    api = api_for(tmp_path)
    set_setting(conn, "effort", "high")
    responses = page_responses(api)
    responses["POST /api/tests/estimate"] = answer(ESTIMATE)
    responses["POST /api/tests"] = answer({"started": True, "pid": 1}, 202)
    from test_web_settings import SONNET_NOTE

    out = run_page("#/settings", responses, f"var SONNET_NOTE = {json.dumps(SONNET_NOTE)};\n" + PANEL_JS + """
run(async function () {
  var out = {};
  var model = control("test-model");
  out.options = byTag(model, "OPTION").map(function (o) { return [o.value, o.textContent]; });
  out.model = model.value;
  out.effort = [byTag(control("test-effort"), "OPTION").map(function (o) { return o.value; }), control("test-effort").value];
  out.days = [control("test-from").value, control("test-to").value];
  out.runNowDays = [document.getElementById("run-from").value, document.getElementById("run-to").value];
  out.limit = [control("test-limit").getAttribute("type"), control("test-limit").value];
  out.labels = byTag(testPanel(), "LABEL").map(function (l) { return l.textContent; });
  out.noteFirst = noteShown();
  model.value = "claude-opus-5-5";
  fire(model, "change");
  out.noteOpus = noteShown();
  model.value = "claude-sonnet-5-5";
  fire(model, "change");
  out.noteSonnet = noteShown();
  var buttons = byTag(testPanel(), "BUTTON").map(function (b) { return b.textContent; });
  out.buttonsBefore = buttons;
  fire(buttonIn(testPanel(), "Get estimate"), "click");
  await settle();
  out.estimate = estimateBox().textContent;
  out.estimateShown = shown(estimateBox());
  out.buttonsAfter = byTag(testPanel(), "BUTTON").map(function (b) { return b.textContent; });
  fire(buttonIn(estimateBox(), "Start test"), "click");
  await settle();
  out.started = messageText();
  out.requests = requests.filter(function (r) { return r.method === "POST"; });
  control("test-limit").value = "10";
  fire(control("test-limit"), "change");
  out.staleHidden = !shown(estimateBox());
  fire(buttonIn(testPanel(), "Get estimate"), "click");
  await settle();
  out.limitBody = requests[requests.length - 1].body;
  return out;
});
""")
    models = api.get("/api/settings").json()["models"]
    assert out["options"] == [
        [m["id"], m["label"] + " · about $" + format(m["approx_cost_per_call"], ".2f") + " per call (" +
         ("measured" if m["measured"] else "estimate") + ")"] for m in models]
    assert out["model"] == SONNET, "a model other than the one reviewing now is picked to start with"
    assert out["effort"] == [["low", "medium", "high", "xhigh", "max"], "high"]
    assert out["days"] == out["runNowDays"] and all(re.fullmatch(r"\d{4}-\d\d-\d\d", d) for d in out["days"])
    assert out["limit"] == ["number", ""]
    assert [label for label in out["labels"] if label] == ["Model", "Effort", "First day", "Last day", "Call limit"]
    assert (out["noteFirst"], out["noteOpus"], out["noteSonnet"]) == (True, False, True)
    assert out["buttonsBefore"] == ["Get estimate"]
    assert out["estimateShown"] is True
    assert out["estimate"].startswith(
        "8 calls will be tested · 6 skipped (3 with no review, 1 erased by retention, 2 with an errored review) · "
        "estimate $1.23")
    assert out["buttonsAfter"].index("Get estimate") < out["buttonsAfter"].index("Start test")
    body = {"from": out["days"][0], "to": out["days"][1], "model": SONNET, "effort": "high", "limit": None}
    assert out["requests"] == [{"method": "POST", "url": "/api/tests/estimate", "body": body},
                               {"method": "POST", "url": "/api/tests", "body": body}]
    assert TEST_STARTED in out["started"]
    assert out["staleHidden"] is True, "changing a choice hides the old estimate"
    assert out["limitBody"] == dict(body, limit=10)


def test_test_estimate_over_budget_and_nothing_to_test(conn, version, tmp_path):
    """Over the budget limit, the estimate says what Run now says and Start test will wait for the OK; with
    nothing to test there is no Start."""
    api = api_for(tmp_path)
    set_setting(conn, "budget_limit_usd", 5)
    responses = page_responses(api)
    responses["POST /api/tests/estimate"] = answer(dict(ESTIMATE, estimate_usd=12.5, contacts=1))
    responses["POST /api/runs/estimate"] = answer({"estimate_usd": 12.5, "contacts": 3, "excluded_purged": 0})
    from test_web_settings import SONNET_NOTE

    script = f"var SONNET_NOTE = {json.dumps(SONNET_NOTE)};\n" + PANEL_JS + """
run(async function () {
  var out = {};
  fire(buttonIn(testPanel(), "Get estimate"), "click");
  await settle();
  out.over = all(estimateBox(), function (e) { return e.classList.contains("over"); }).map(function (e) { return e.textContent; });
  out.start = buttonIn(estimateBox(), "Start test").textContent;
  out.line = estimateBox().children[0].textContent;
  var runNow = panelTitled("Run now for a date range");
  fire(buttonIn(runNow, "Get estimate"), "click");
  await settle();
  out.runNowOver = all(runNow, function (e) { return e.classList.contains("over"); }).map(function (e) { return e.textContent; });
  RESPONSES["POST /api/tests/estimate"] = { status: 200, body: { estimate_usd: 0, contacts: 0, excluded_purged: 0,
    excluded_errored: 0, excluded_unreviewed: 4 } };
  fire(buttonIn(testPanel(), "Get estimate"), "click");
  await settle();
  out.empty = estimateBox().textContent;
  out.emptyStart = buttonIn(estimateBox(), "Start test");
  return out;
});
"""
    out = run_page("#/settings", responses, script)
    assert out["over"] == [
        "This is over your $5.00 budget limit. Starting it reads the stored calls, then pauses for your OK in the "
        "banner before anything is paid for."], "a test reads the stored calls; it never fetches them"
    assert out["runNowOver"] == [
        "This is over your $5.00 budget limit. Starting it fetches the calls, then pauses for your OK in the banner "
        "before anything is paid for."]
    assert out["start"] == "Start test (will wait for your OK)"
    assert out["line"] == ("1 call will be tested · 6 skipped (3 with no review, 1 erased by retention, 2 with an "
                           "errored review) · estimate $12.50")
    assert out["empty"].startswith("0 calls will be tested · 4 skipped (4 with no review, 0 erased by retention, "
                                   "0 with an errored review) · estimate $0.00")
    assert "Nothing to test in this range." in out["empty"]
    assert out["emptyStart"] is None


def test_model_tests_list(conn, version, tmp_path):
    """Ruling 1: under the form, the model tests newest first, each with its model, effort, window, calls,
    status and cost, and each linking to its report."""
    add_shown(conn, version, 1, all_met(version))
    older = add_test_run(conn, model=SONNET, effort="low", created_at="2026-10-13T00:00:00Z", actual_usd=0.25)
    conn.execute("UPDATE runs SET contact_count = 3 WHERE run_id = ?", (older,))
    conn.commit()
    newer = add_test_run(conn, created_at="2026-10-14T00:00:00Z", status="awaiting_confirmation", actual_usd=None,
                         effort="max")
    out = run_page("#/settings", page_responses(api_for(tmp_path)), """
run(async function () {
  var p = panelTitled("Test a model");
  var heads = all(p, function (e) { return e.classList.contains("group-head"); }).map(function (e) { return e.textContent; });
  var table = all(p, function (e) { return e.tagName === "TABLE"; })[0];
  return { heads: heads, cls: table.className, rows: byTag(table, "TBODY").map(function (tb) {
    return { cls: tb.className, links: links(tb), cells: byTag(tb, "TD").map(function (td) {
      return td.textContent.replace(/ /g, " "); }) };
  }) };
});
""")
    assert out["heads"] == ["Model tests"]
    assert set(out["cls"].split()) >= {"atable", "two-line"}
    assert [r["links"] for r in out["rows"]] == [
        [{"text": "Claude Fable 5.1", "href": f"#/settings/tests/{newer}"}],
        [{"text": "Claude Sonnet 5.5", "href": f"#/settings/tests/{older}"}]]
    assert out["rows"][1]["cells"] == ["Claude Sonnet 5.5", "low", "Complete", "Oct 4 – Oct 10, 2026", "3", "$0.25"]
    assert out["rows"][0]["cells"][:3] == ["Claude Fable 5.1", "max", "Waiting for your OK"]
    assert out["rows"][0]["cells"][4:] == ["0", "-"]
    assert all("row-group" in r["cls"].split() for r in out["rows"])


def test_model_tests_list_empty(conn, version, tmp_path):
    out = run_page("#/settings", page_responses(api_for(tmp_path)), """
run(async function () { return panelTitled("Test a model").textContent; });
""")
    assert "No model tests yet." in out


# -- Run history ------------------------------------------------------------------------------------------


def test_run_history_labels_model_test(conn, version, tmp_path):
    """Run history labels a test run "Model test"; once the test is complete its row links to the comparison.
    Each row's window is the usual range label, kept whole on each side of the dash."""
    add_shown(conn, version, 1, all_met(version))
    done = add_test_run(conn, created_at="2026-10-13T00:00:00Z")
    waiting = add_test_run(conn, created_at="2026-10-14T00:00:00Z", status="awaiting_confirmation", actual_usd=None)
    out = run_page("#/settings", page_responses(api_for(tmp_path)), """
run(async function () {
  var table = all(panelTitled("Run history"), function (e) { return e.tagName === "TABLE"; })[0];
  return byTag(table, "TBODY").map(function (tb) {
    var line1 = byTag(tb, "TR")[0];
    return { kind: byTag(line1, "TD")[1].textContent, window: byTag(line1, "TD")[2].textContent, links: links(tb) };
  });
});
""")
    by_kind = {}
    for row in out:
        by_kind.setdefault(row["kind"], []).append(row["links"])
    assert by_kind["Model test"] == [[], [{"text": "Model test", "href": f"#/settings/tests/{done}"}]]
    assert waiting > done
    assert {row["window"] for row in out} == {"Oct 4 – Oct 10, 2026"}
    assert all(links == [] for kind, rows in by_kind.items() if kind != "Model test" for links in rows)


# -- the comparison view ----------------------------------------------------------------------------------


@pytest.fixture
def tested(conn, version):
    """A finished Sonnet test of three calls. Call 1: the owner overruled sc02 and sc06 on the shown review;
    the test matches the owner on sc02. Call 2: Lee's, the test disagrees on sc06 and sc15. Call 3: a test
    error."""
    one = add_shown(conn, version, 1, all_met(version), cost=0.06, overrides={"sc02": "missed", "sc06": "missed"})
    two = add_shown(conn, version, 2, missing(version, "sc06", "sc15"), cost=0.07, agent=LEE,
                    call="2026-10-06T15:00:00Z")
    three = add_shown(conn, version, 3, all_met(version), cost=0.05, call="2026-10-07T15:00:00Z")
    run_id = add_test_run(conn, model=SONNET, effort="high", actual_usd=0.31)
    add_tested(conn, run_id, one, missing(version, "sc02"), cost=0.03, model=SONNET)
    add_tested(conn, run_id, two, all_met(version), cost=0.04, model=SONNET)
    add_tested(conn, run_id, three, {}, status="error", cost=0.02, model=SONNET)
    return run_id


REPORT_JS = """
function sectionsInOrder() {
  // Each top-level block on the screen: a panel's title, a tile row's tile labels.
  return screenEl.findAll(function (e) {
    return (e.tagName === "SECTION" && e.classList.contains("panel")) || e.classList.contains("tile");
  }).map(function (e) {
    if (e.classList.contains("tile")) { return "tile:" + e.children[0].textContent; }
    return titleOf(e);
  });
}
function textOf(cls) {
  return all(screenEl, function (e) { return e.classList.contains(cls); }).map(function (e) { return e.textContent; });
}
"""


def test_comparison_view_sections(tested, tmp_path):
    """Ruling 3: the header, the summary tiles (check agreement the lead), the verdict changes, the override
    line, the checks they disagree on most, the other judgments and the calls, in that order, then the test
    errors with their plain text. Each call row links to its side-by-side view. Settings stays highlighted."""
    run_id = tested
    api = api_for(tmp_path)
    report = api.get(f"/api/tests/{run_id}").json()
    out = run_page(f"#/settings/tests/{run_id}", page_responses(api, f"/api/tests/{run_id}"), REPORT_JS + """
run(async function () {
  var out = { order: sectionsInOrder(), tab: settingsTab() };
  out.big = textOf("big");
  out.header = panels()[0].textContent;
  out.headerLinks = links(panels()[0]);
  out.lead = all(screenEl, function (e) { return e.classList.contains("lead-num"); }).map(function (e) {
    return e.parentNode.children[0].textContent + "=" + e.textContent; });
  out.tiles = all(screenEl, function (e) { return e.classList.contains("tile"); }).map(function (t) { return t.textContent; });
  out.overrides = panelTitled("Checks you overruled").textContent;
  out.changes = panelTitled("Verdict changes").textContent;
  var checks = panelTitled("Checks they disagree on most");
  out.checks = byTag(byTag(checks, "TBODY")[0], "TR").map(function (tr) {
    return byTag(tr, "TD").filter(function (td) { return !td.classList.contains("barcell"); })
      .map(function (td) { return td.textContent; }); });
  out.checkBars = all(checks, function (e) { return e.tagName === "SVG"; }).length;
  out.judgments = byTag(byTag(panelTitled("Other judgments"), "TBODY")[0], "TR").map(function (tr) {
    return byTag(tr, "TD").map(function (td) { return td.textContent; }); });
  var calls = panelTitled("Calls");
  out.calls = byTag(calls, "TBODY").map(function (tb) {
    return { links: links(tb), text: tb.textContent, badges: all(tb, function (e) {
      return e.classList.contains("verdict"); }).map(function (e) { return e.textContent; }) };
  });
  out.errors = panelTitled("Calls the test couldn't review").textContent;
  return out;
});
""")
    assert out["tab"] == ["settings"]
    assert out["order"] == [
        out["order"][0], "tile:Check agreement", "tile:Calls compared", "tile:Avg score difference",
        "tile:Test run total", "tile:Cost per call", "Verdict changes", "Checks you overruled",
        "Checks they disagree on most", "Other judgments", "Calls", "Calls the test couldn't review"]
    assert out["big"][0] == ("Model test: Claude Sonnet 5.5 · effort high vs shown results (Claude Opus 5.5 · "
                             "effort not recorded)")
    assert "Oct 4 – Oct 10, 2026" in out["header"]
    assert {"text": "← Back to Settings", "href": "#/settings"} in out["headerLinks"]
    summary = report["summary"]
    assert out["lead"] == [f"Check agreement={js_pct(summary['check_agreement_pct'])}"]
    assert out["tiles"][1].startswith("Calls compared2") and "1 test error left out" in out["tiles"][1]
    diff = summary["avg_score_diff"]
    assert diff != 0 and out["tiles"][2].startswith("Avg score difference" + ("+" if diff > 0 else "−") +
                                                    js_pct(abs(diff))[:-1])
    assert out["tiles"][3].startswith("Test run total$0.31")
    assert out["tiles"][4].startswith("Cost per call$0.04 test") and "$0.07 shown results" in out["tiles"][4]
    assert out["overrides"] == (
        "Checks you overruledOn the 2 checks you overruled: Claude Opus 5.5 matched you 0 of 2, Claude Sonnet 5.5 "
        "matched you 1 of 2.The original model matches 0 by definition: overruling a check means you disagreed "
        "with its outcome.")
    assert "Needs review" in out["changes"] or "Problem" in out["changes"]

    labels = [c["label"] for c in report["checks"]]
    assert [row[0] for row in out["checks"]] == labels, "every compared check, least agreement first"
    assert out["checks"][0][1:] == [js_pct(report["checks"][0]["agreement_pct"]), "1"]
    assert out["checkBars"] == len(labels)
    assert out["judgments"] == [["Counted in scores", "2", "0", "0"], ["Resolved on the call", "2", "0", "0"],
                                ["Dropped call", "2", "0", "0"]]

    tickets = [c["ticket_id"] for c in report["calls"]]
    assert [c["links"][0] for c in out["calls"]] == [
        {"text": f"#{t}", "href": f"#/settings/tests/{run_id}/{t}"} for t in tickets]
    error_row = out["calls"][tickets.index(3)]
    assert error_row["badges"] == ["✓ Good", "! Review failed"]
    assert "Lee Agent" in out["calls"][tickets.index(2)]["text"]
    assert "#3" in out["errors"] and "The model declined to review this call." in out["errors"]


def test_comparison_without_overrides_leaves_the_line_out(conn, version, tmp_path):
    shown = add_shown(conn, version, 1, all_met(version))
    run_id = add_test_run(conn, model=SONNET)
    add_tested(conn, run_id, shown, all_met(version), model=SONNET)
    api = api_for(tmp_path)
    out = run_page(f"#/settings/tests/{run_id}", page_responses(api, f"/api/tests/{run_id}"), REPORT_JS + """
run(async function () { return { order: sectionsInOrder(), changes: panelTitled("Verdict changes").textContent }; });
""")
    assert "Checks you overruled" not in out["order"] and "Calls the test couldn't review" not in out["order"]
    assert out["changes"] == "Verdict changesNo call's verdict changed."


def test_unknown_test_says_so(conn, version, tmp_path):
    """Ruling 5: an unknown test, or a run that isn't a test, reads "This model test doesn't exist." with the
    back link."""
    api = api_for(tmp_path)
    out = run_page("#/settings/tests/99", page_responses(api, "/api/tests/99"), """
run(async function () {
  var first = { text: screenEl.textContent, links: links(screenEl), tab: settingsTab() };
  await go("#/settings/tests/abc");
  return { first: first, bad: { text: screenEl.textContent, requests: requests.map(function (r) { return r.url; }) } };
});
""")
    assert out["first"]["text"] == MISSING_TEST + "← Back to Settings"
    assert out["first"]["links"] == [{"text": "← Back to Settings", "href": "#/settings"}]
    assert out["first"]["tab"] == ["settings"]
    assert out["bad"]["text"] == MISSING_TEST + "← Back to Settings"
    assert "/api/tests/abc" not in out["bad"]["requests"]


# -- the side-by-side view --------------------------------------------------------------------------------

SIDE_JS = """
function rows() {
  var table = all(panelTitled("Every check"), function (e) { return e.tagName === "TABLE"; })[0];
  return byTag(table, "TR").filter(function (tr) { return tr.parentNode.tagName === "TBODY"; }).map(function (tr) {
    var cells = byTag(tr, "TD");
    return { cls: tr.className, cells: cells.map(function (td) { return td.textContent; }),
             marks: cells.map(function (td) { var s = byTag(td, "SPAN")[0]; return s ? s.getAttribute("title") : null; }) };
  });
}
"""


def test_side_by_side_three_outcomes(tested, tmp_path):
    """Ruling 4: the header gives the ticket, agent, date and both sides' scores and verdicts. One row per check:
    its label, the original outcome, the owner's call (only when overruled, otherwise "-"), the test outcome and
    each side's evidence. Differing checks come first, marked with the attention style."""
    run_id = tested
    api = api_for(tmp_path)
    detail = api.get(f"/api/tests/{run_id}/calls/1").json()
    out = run_page(f"#/settings/tests/{run_id}/1", page_responses(api, f"/api/tests/{run_id}/calls/1",
                                                                  "/api/tests/" + str(run_id)), SIDE_JS + """
run(async function () {
  var head = panels()[0];
  return { tab: settingsTab(), big: all(head, function (e) { return e.classList.contains("big"); })[0].textContent,
           head: head.textContent, links: links(head), rows: rows(),
           heads: byTag(panelTitled("Every check"), "TH").map(function (th) { return th.textContent; }) };
});
""")
    assert out["tab"] == ["settings"]
    assert out["big"].startswith("Ticket #1")
    assert "Sam Agent" in out["head"] and "Oct 5, 2026" in out["head"]
    assert "Shown results: Claude Opus 5.5 · effort not recorded" in out["head"]
    assert "Model test: Claude Sonnet 5.5 · effort high" in out["head"]
    assert {"text": "← Back to the comparison", "href": f"#/settings/tests/{run_id}"} in out["links"]
    assert out["heads"] == ["Check", "Original", "Your call", "Test", "Original evidence", "Test evidence"]

    by_label = {r["cells"][0]: r for r in out["rows"]}
    checks = {c["check_id"]: c for c in detail["checks"]}
    sc02 = by_label[checks["sc02"]["label"]]
    assert sc02["marks"][1:4] == ["Met", "Missed", "Missed"], "original, your call, test"
    assert sc02["cells"][4:] == ["evidence", "test evidence"]
    sc06 = by_label[checks["sc06"]["label"]]
    assert sc06["marks"][1:4] == ["Met", "Missed", "Met"]
    plain = by_label[checks["sc01"]["label"]]
    assert plain["cells"][2] == "-" and plain["marks"][2] is None
    differing = [c["label"] for c in detail["checks"] if c["differs"]]
    assert [r["cells"][0] for r in out["rows"][:len(differing)]] == differing == [checks["sc02"]["label"]]
    assert "flagrow" in out["rows"][0]["cls"].split()
    assert all("flagrow" not in r["cls"].split() for r in out["rows"][len(differing):])


def test_side_by_side_purged_and_errors(tested, conn, tmp_path):
    """Purged evidence shows the API's exact text; a test error reads in plain words; an unknown call says so."""
    run_id = tested
    conn.execute("UPDATE contacts SET purged_at = '2027-01-01T00:00:00Z' WHERE ticket_id = 2")
    conn.execute("UPDATE check_results SET evidence = 'Details removed after 30 days (retention).' WHERE review_id IN "
                 "(SELECT review_id FROM reviews WHERE ticket_id = 2)")
    conn.commit()
    api = api_for(tmp_path)
    paths = [f"/api/tests/{run_id}/calls/2", f"/api/tests/{run_id}/calls/3", f"/api/tests/{run_id}/calls/9",
             f"/api/tests/{run_id}"]
    out = run_page(f"#/settings/tests/{run_id}/2", page_responses(api, *paths), SIDE_JS + """
run(async function () {
  var purged = rows().map(function (r) { return r.cells.slice(4); });
  await go("#/settings/tests/" + RUN + "/3");
  var errored = panels()[0].textContent;
  await go("#/settings/tests/" + RUN + "/9");
  return { purged: purged, errored: errored, unknown: screenEl.textContent, links: links(screenEl) };
});
""".replace("RUN", str(run_id)))
    assert {tuple(cells) for cells in out["purged"]} == {(ERASED, ERASED)}
    assert "The model declined to review this call." in out["errored"]
    assert out["unknown"].startswith(f"Model test {run_id} has no result for call 9.")
    assert {"text": "← Back to the comparison", "href": f"#/settings/tests/{run_id}"} in out["links"]


def test_compare_views_use_shared_styles():
    """Tokens only: the new views reuse the shared classes, and their own rules use custom properties."""
    from css_rules import app_css, last_value, media_block, top_level, values

    css = app_css()
    wide = top_level(css)
    assert values(wide, ".panel-row.cols-5-lead", "grid-template-columns") == [
        "minmax(0, 1.4fr) repeat(4, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 900px)"), ".panel-row.cols-5-lead", "grid-template-columns") == [
        "repeat(2, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 640px)"), ".panel-row.cols-5-lead", "grid-template-columns") == [
        "repeat(2, minmax(0, 1fr))"]
    assert last_value(wide, ".cmp td.ev", "min-width") == "var(--cmp-ev-min)", "two evidence columns share a row"
    for prop in ("color", "font-size", "overflow-wrap"):   # the rest is the ticket detail's evidence style
        assert values(wide, ".cmp td.ev", prop) == values(wide, ".det td.ev", prop) != [], prop
    assert values(wide, ".form-row select", "max-width") == ["100%"], \
        "the model select, with each model's cost, fits the 400px panel"


# -- runs waiting for your OK: Cancel, and a model test's own words (final review) ---------------------------

UNRECORDED = ("Submission may have reached Anthropic without being recorded; check the Batches page in the "
              "Anthropic Console before re-running.")
CANCEL_RUN = "Cancel run {n}? Nothing has been sent or charged; you can start it again any time."
CANCEL_TEST = "Cancel model test {n}? Nothing has been sent or charged; you can start it again any time."

# Every dialog the page opens is recorded; ANSWER is what the owner clicks.
BANNER_JS = """
var dialogs = [];
var ANSWER = true;
globalThis.confirm = function (text) { dialogs.push(text); return ANSWER; };
var bannersEl = document.getElementById("banners");
function bannerList() {
  return bannersEl.children.map(function (b) {
    return { text: all(b, function (e) { return e.classList.contains("text"); })[0].textContent,
             buttons: byTag(b, "BUTTON").map(function (x) { return x.textContent; }) };
  });
}
function bannerOf(text) {
  return bannersEl.children.filter(function (b) { return b.textContent.indexOf(text) >= 0; })[0];
}
function historyRow(runId) {
  var table = all(panelTitled("Run history"), function (e) { return e.tagName === "TABLE"; })[0];
  return byTag(table, "TBODY").filter(function (tb) {
    return byTag(byTag(tb, "TR")[0], "TD")[0].textContent === String(runId);
  })[0];
}
function rowState(runId) {
  var cell = all(historyRow(runId), function (e) { return e.classList.contains("run-state"); })[0];
  return { status: all(cell, function (e) { return e.classList.contains("run-status"); })[0].textContent,
           buttons: byTag(cell, "BUTTON").map(function (b) { return b.textContent; }) };
}
function posts() {
  return requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return r.url; });
}
"""


@pytest.fixture
def waiting(conn, version):
    """Run 1 (Run now) and model test 2 (Sonnet) wait for the OK; model test 3 and run 4 need a check."""
    from factories import add_run

    run = add_run(conn, status="awaiting_confirmation", estimate_usd=55.0)
    test = add_test_run(conn, model=SONNET, status="awaiting_confirmation", actual_usd=None)
    conn.execute("UPDATE runs SET estimate_usd = 12.5 WHERE run_id = ?", (test,))
    conn.commit()
    unknown_test = add_test_run(conn, status="submission_unknown", actual_usd=None)
    unknown_run = add_run(conn, status="submission_unknown", error=UNRECORDED)
    assert (run, test, unknown_test, unknown_run) == (1, 2, 3, 4)
    return conn


def test_banner_names_a_model_test_and_offers_cancel(waiting, tmp_path):
    """Each run waiting for the OK has Confirm… and Cancel… in its banner; a model test's banner names it and
    its model. A model test's unknown submission says model test too."""
    out = run_page("#/settings", page_responses(api_for(tmp_path)), BANNER_JS + """
run(async function () { return bannerList(); });
""")
    assert out == [
        {"text": "Run 1 is waiting for your OK: its estimate is over the budget limit.",
         "buttons": ["Confirm…", "Cancel…"]},
        {"text": "Model test 2 (Claude Sonnet 5.5) is waiting for your OK: its estimate is over the budget limit.",
         "buttons": ["Confirm…", "Cancel…"]},
        {"text": "Model test 3: " + UNRECORDED, "buttons": ["Clear…"]},
        {"text": "Run 4: " + UNRECORDED, "buttons": ["Clear…"]}]


def test_cancel_from_the_banner_and_the_history(waiting, tmp_path):
    """Cancel… asks first, in the app's dialog, with the run's own words; No sends nothing. Yes posts the
    cancel, then the banners and the run history are read again."""
    api = api_for(tmp_path)
    before = page_responses(api)
    api.post("/api/runs/2/cancel")
    after_test = page_responses(api)
    api.post("/api/runs/1/cancel")
    after_both = page_responses(api)
    responses = dict(before, **{"POST /api/runs/1/cancel": answer({"run_id": 1, "status": "cancelled"}),
                                "POST /api/runs/2/cancel": answer({"run_id": 2, "status": "cancelled"})})
    out = run_page("#/settings", responses, BANNER_JS + f"""
var AFTER_TEST = {json.dumps(after_test)};
var AFTER_BOTH = {json.dumps(after_both)};
""" + """
run(async function () {
  var out = {};
  out.rowBefore = [rowState(1), rowState(2)];
  ANSWER = false;
  fire(buttonIn(bannerOf("Model test 2"), "Cancel"), "click");
  await settle();
  out.declined = { dialogs: dialogs.slice(), posts: posts(), banners: bannerList().length };

  ANSWER = true;
  dialogs = [];
  Object.assign(RESPONSES, AFTER_TEST);
  fire(buttonIn(bannerOf("Model test 2"), "Cancel"), "click");
  await settle();
  out.test = { dialogs: dialogs.slice(), posts: posts(), banners: bannerList().map(function (b) { return b.text; }),
               row: rowState(2) };

  dialogs = [];
  Object.assign(RESPONSES, AFTER_BOTH);
  fire(buttonIn(historyRow(1), "Cancel"), "click");
  await settle();
  out.run = { dialogs: dialogs.slice(), posts: posts(), banners: bannerList().map(function (b) { return b.text; }),
              row: rowState(1) };
  return out;
});
""")
    assert out["rowBefore"] == [{"status": "Waiting for your OK", "buttons": ["Confirm…", "Cancel…"]}] * 2
    assert out["declined"] == {"dialogs": [CANCEL_TEST.format(n=2)], "posts": [], "banners": 4}
    assert out["test"]["dialogs"] == [CANCEL_TEST.format(n=2)]
    assert out["test"]["posts"] == ["/api/runs/2/cancel"]
    assert [t[:12] for t in out["test"]["banners"]] == ["Run 1 is wai", "Model test 3", "Run 4: Submi"]
    assert out["test"]["row"] == {"status": "Cancelled", "buttons": []}
    assert out["run"]["dialogs"] == [CANCEL_RUN.format(n=1)]
    assert out["run"]["posts"] == ["/api/runs/2/cancel", "/api/runs/1/cancel"]
    assert [t[:12] for t in out["run"]["banners"]] == ["Model test 3", "Run 4: Submi"]
    assert out["run"]["row"] == {"status": "Cancelled", "buttons": []}


def test_dialogs_use_a_model_test_s_own_words(waiting, tmp_path):
    """Confirm… and Clear… on a model test say model test; Clear… never speaks of releasing a test's calls,
    since a test holds none. A normal run's Clear… still does."""
    out = run_page("#/settings", page_responses(api_for(tmp_path)), BANNER_JS + """
run(async function () {
  ANSWER = false;
  fire(buttonIn(bannerOf("Model test 2"), "Confirm"), "click");
  await settle();
  fire(buttonIn(bannerOf("Model test 3"), "Clear"), "click");
  fire(buttonIn(bannerOf("Run 4"), "Clear"), "click");
  fire(buttonIn(historyRow(3), "Clear"), "click");
  await settle();
  return { dialogs: dialogs, posts: posts() };
});
""")
    confirm_test, clear_test, clear_run, clear_test_row = out["dialogs"]
    assert confirm_test.startswith("Model test 2's estimate is $12.50, over your $40.00 budget limit.")
    assert clear_test == clear_test_row
    assert clear_test.startswith("Only clear model test 3 after checking the Batches page in the Anthropic Console.")
    assert clear_test.endswith("Clear model test 3?")
    assert "release" not in clear_test and "its calls" not in clear_test
    assert "Clearing releases its calls so later runs can review them again." in clear_run
    assert clear_run.endswith("Clear run 4?")
    assert out["posts"] == []
