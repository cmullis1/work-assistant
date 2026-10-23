"""The Team overview endpoint and page assets. Synthetic data only."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_queries import LEE, SAM, add_review, all_met, missing, version  # noqa: F401  (fixture)
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app

STATIC = Path(webapp.__file__).parent / "static"
BASE = "http://127.0.0.1:8765"


def client(tmp_path):
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 4242,
                     zendesk_factory=lambda: None, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def seeded(conn, version):
    """Calls in the weeks of Sep 20, Sep 27 and Oct 4 (America/Chicago); Oct 4 is the latest."""
    add_review(conn, version, 1, all_met(version), agent=SAM, call="2026-10-05T15:00:00Z")
    add_review(conn, version, 2, missing(version, "sc06"), agent=LEE, call="2026-10-06T15:00:00Z")
    add_review(conn, version, 3, missing(version, "pr-call-length", "pr-scope"), agent=SAM,
               call="2026-09-28T15:00:00Z")
    add_review(conn, version, 4, all_met(version), agent=LEE, call="2026-09-21T15:00:00Z")
    return conn


# -- the brief's tests ----------------------------------------------------------------------------------


def test_overview_endpoint_defaults(seeded, tmp_path):
    response = client(tmp_path).get("/api/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["period"]["start"] == "2026-10-04"          # Last week (today is pinned to Oct 17)
    assert body["comparison"]["start"] == "2026-09-27"      # the week before it
    tiles = body["tiles"]
    assert tiles["calls"] == {"count": 2, "compare_count": 1}
    assert tiles["problems"]["count"] == 1 and tiles["problems"]["share_pct"] == 50.0
    assert tiles["call_length_misses"]["compare_count"] == 1 and tiles["scope_misses"]["compare_count"] == 1
    assert body["trend"]["unit"] == "day" and len(body["trend"]["steps"]) == 14
    assert "weeks" not in body, "the week list went with the week picker"
    assert body["has_data"] is True and body["agent"] is None
    assert body["has_data_in_period"] is True and body["has_data_in_comparison"] is True


def test_overview_endpoint_empty_db(conn, version, tmp_path):
    response = client(tmp_path).get("/api/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["has_data"] is False and body["has_data_in_period"] is False
    assert body["agent_options"] == []
    assert body["agents"] == [] and body["most_missed"] == [] and body["problems"] == []
    assert body["tiles"]["avg_score"]["avg_percent"] is None and body["tiles"]["calls"]["count"] is None
    assert len(body["trend"]["steps"]) == 14 and all(s["avg_percent"] is None for s in body["trend"]["steps"])
    start, compare = date.fromisoformat(body["period"]["start"]), date.fromisoformat(body["comparison"]["start"])
    assert start.weekday() == 6 and compare == start - timedelta(days=7)  # a Sunday, and the week before


# -- more -----------------------------------------------------------------------------------------------


def test_overview_empty_db_without_any_setup(tmp_path):
    """A brand-new database: no runs, no team, no time zone chosen."""
    response = client(tmp_path).get("/api/overview")
    assert response.status_code == 200
    assert response.json()["has_data"] is False


def test_overview_explicit_weeks_snap_to_sunday(seeded, tmp_path):
    body = client(tmp_path).get("/api/overview?week=2026-09-30&compare=2026-09-24").json()
    assert (body["period"]["start"], body["comparison"]["start"]) == ("2026-09-27", "2026-09-20")
    assert body["tiles"]["calls"] == {"count": 1, "compare_count": 1}


def test_overview_compare_defaults_to_the_week_before_the_chosen_week(seeded, tmp_path):
    body = client(tmp_path).get("/api/overview?week=2026-09-20").json()
    assert (body["period"]["start"], body["comparison"]["start"]) == ("2026-09-20", "2026-09-13")
    assert body["tiles"]["calls"]["compare_count"] is None and body["has_data_in_comparison"] is False


def test_overview_agent_filter(seeded, tmp_path):
    body = client(tmp_path).get("/api/overview", params={"agent": LEE[0].upper()}).json()
    assert body["agent"] == LEE[0]
    assert body["tiles"]["calls"]["count"] == 1 and [a["email"] for a in body["agents"]] == [LEE[0]]
    # The choices stay team-wide so the owner can switch agents.
    assert body["agent_options"] == [{"email": LEE[0], "name": LEE[1]}, {"email": SAM[0], "name": SAM[1]}]


def test_overview_week_without_data(seeded, tmp_path):
    body = client(tmp_path).get("/api/overview?week=2026-08-02").json()
    assert body["has_data"] is True and body["has_data_in_period"] is False and body["agents"] == []
    assert body["tiles"]["calls"]["count"] is None


@pytest.mark.parametrize("query", ["week=2026-13-01", "compare=yesterday", "week=2026-10-4"])
def test_overview_rejects_bad_dates(seeded, tmp_path, query):
    response = client(tmp_path).get("/api/overview?" + query)
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_not_counted_only_week_is_not_a_week_with_data(conn, version, tmp_path):
    add_review(conn, version, 1, all_met(version), call="2026-09-28T15:00:00Z")
    add_review(conn, version, 2, missing(version, "sc06"), counted=False, call="2026-10-05T15:00:00Z")
    api = client(tmp_path)
    # Last 8 weeks ends with the latest week with data (the default is Last week since T-041).
    body = api.get("/api/overview?period=last_8_weeks").json()
    assert body["period"]["end"] == "2026-10-03" and body["trend"]["steps"][-1]["start"] == "2026-09-27"


def test_review_failed_calls_are_not_problems(conn, version, tmp_path):
    """T-045: a review that did not finish is Review failed, not Problem, even with a measured miss. The
    miss still counts in its own tile, and the call still counts as a call."""
    add_review(conn, version, 1, {"pr-call-length": "missed"}, status="error", call="2026-10-05T15:00:00Z")
    body = client(tmp_path).get("/api/overview").json()
    assert body["problems"] == [] and body["tiles"]["problems"]["count"] == 0
    assert body["tiles"]["calls"]["count"] == 1 and body["tiles"]["call_length_misses"]["count"] == 1
    assert body["tiles"]["avg_score"]["avg_percent"] is None


@pytest.mark.parametrize("query", ["period=yesterday", "period=this_week",
                                   "period=custom&start=2026-08-03&end=2026-08-09&compare=none",
                                   "period=custom&start=2026-08-01&end=2026-08-05&compare=custom"
                                   "&compare_start=2026-07-01&compare_end=2026-07-05"])
def test_overview_empty_periods_answer(seeded, tmp_path, query):
    """Review Focus 2 through the API: periods and comparisons without calls answer with no figures."""
    body = client(tmp_path).get("/api/overview?" + query).json()
    assert body["has_data_in_period"] is False and body["has_data_in_comparison"] is False
    assert body["tiles"]["calls"] == {"count": None, "compare_count": None}
    assert body["tiles"]["problems"]["share_pct"] is None and body["problems"] == [] and body["agents"] == []


# -- page assets ----------------------------------------------------------------------------------------


def test_charts_module_exports_the_chart_functions():
    js = (STATIC / "charts.js").read_text(encoding="utf-8")
    for name in ("lineChart", "barChart", "hBars", "sparkline", "dayChart"):
        assert re.search(r"\b" + name + r"\s*:", js), name
    for unit in ("day", "week", "month"):
        assert "Rubric changed this " + unit in js and "Model changed this " + unit in js
    assert 'role", "img"' in js and "aria-label" in js


def test_overview_page_has_empty_states_and_links():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"No calls in this period"' in js and "No reviewed calls for this week yet." not in js
    assert "/api/overview" in js
    assert "#/agents/" in js and "#/tickets?ticket=" in js


def test_chart_classes_styled_in_css():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for selector in (".chart .line", ".chart .bar", ".chart .marker", ".chart .tick", ".chart .dlabel",
                     ".chart .hitarea", ".ptk", ".filters", ".mrow"):
        assert selector in css, selector


def js_function(js: str, name: str) -> str:
    """The source of `function name(...) { ... }` in app.js, braces matched."""
    start = js.index("function " + name + "(")
    depth = 0
    for i in range(js.index("{", start), len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError("unbalanced braces in " + name)


def test_overview_panel_order():
    """Spec 5.1: filters, a lead tile row, the two trends side by side, most-missed beside agents,
    then the problem tickets list full width."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    render = js_function(js, "renderOverview")
    helpers = ["overviewFilters", "overviewTiles", "trendPanels", "mostMissedPanel", "agentsPanel", "problemsPanel"]
    calls = [render.index(name + "(") for name in helpers]
    assert calls == sorted(calls), "renderOverview builds the panels out of spec order"

    # The rows themselves are laid out in renderOverview.
    assert '"panel-row cols-4-lead"' in render
    assert re.search(r'"panel-row cols-2[^"]*"[^;]*trends\.panels', render), "trend charts share a cols-2 row"
    assert re.search(r'"panel-row cols-2[^"]*"[^;]*mostMissedPanel\(data\)[^;]*agentsPanel\(data\b', render), \
        "most-missed and agents share one cols-2 row"
    assert not re.search(r'"panel-row[^"]*"[^;]*problemsPanel\(data\)', render), "problem tickets stay full width"

    # The labels read in spec order across the helpers, in the order renderOverview calls them.
    source = "".join(js_function(js, name) for name in helpers)
    position = 0
    for label in ("Avg score", "Problem tickets", "10-minute", "Scope", "Team average score",
                  "Share of tickets with a problem", "Most-missed", '"Techs"', '"Problem tickets ("'):
        found = source.find(label, position)
        assert found >= 0, label + " missing or out of order"
        position = found + len(label)

    # The overview no longer uses the old auto-fit wrappers; Avg score is the lead number.
    tiles = js_function(js, "overviewTiles")
    assert '"tiles"' not in tiles and '"grid2"' not in js_function(js, "trendPanels")
    assert re.search(r'tile\("Avg score",[\s\S]*?"Average score, out of 100[^"]*",\s*true\)', tiles)
    assert '"lead-num"' in js_function(js, "tile")


# -- running the page's builders in node --------------------------------------------------------------

TESTS = Path(__file__).parent


def js_var(js: str, name: str) -> str:
    """The source of `var name = ...;` in app.js, up to the semicolon that ends it."""
    start = js.index("var " + name + " = ")
    depth, quoted = 0, False
    for i in range(start, len(js)):
        char = js[i]
        if quoted:
            quoted = char != '"' or js[i - 1] == "\\"
        elif char == '"':
            quoted = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ";" and depth == 0:
            return js[start:i + 1]
    raise AssertionError("no end to var " + name)


def node_prelude() -> str:
    """Script lines that give node the fake DOM (tests/fake_dom.js), app.css's tokens and charts.js."""
    from css_rules import app_css, tokens

    return (f"require({json.dumps(str(TESTS / 'fake_dom.js'))});\n"
            f"var TOKENS = {json.dumps(tokens(app_css()))};\n"
            "globalThis.getComputedStyle = function () {\n"
            "  return { getPropertyValue: function (n) { return TOKENS[n.replace(/^--/, '')] || ''; } };\n"
            "};\n"
            f"require({json.dumps(str(STATIC / 'charts.js'))});\n")


def run_node(script: str):
    """Run `script` in node and return the JSON it writes to stdout. The script goes in on stdin, because a
    script the size of a whole app.js section is longer than Windows allows on a command line."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


APP_HELPERS = ("h", "append", "panel", "num", "pct", "localDate", "dayLabel", "weekLabel")

# Snapshots of a built Agents panel: its table's class, header rows and each agent's row group.
SNAPSHOT_JS = """
function cells(tr) {
  return tr.children.map(function (td) {
    return { cls: td.className, text: td.textContent, colspan: td.getAttribute("colspan"),
             charts: td.findAll(function (e) { return e.tagName === "SVG"; }).length };
  });
}
function snapshot(section) {
  var tables = section.findAll(function (e) { return e.tagName === "TABLE"; });
  var table = tables[0];
  var head = table.children.filter(function (c) { return c.tagName === "THEAD"; })[0];
  return {
    tables: tables.length,
    cls: table.className,
    head: head.children.map(function (tr) { return tr.children.map(function (th) { return th.textContent; }); }),
    bodies: table.children.filter(function (c) { return c.tagName === "TBODY"; }).map(function (tb) {
      return { cls: tb.className, rows: tb.children.map(cells) };
    })
  };
}
"""

ONE_LINE_HEAD = ["Tech", "Tickets", "Avg score", "Problem tickets", "Problem rate", "Score by week",
                 "Most-missed check"]


def agents_data():
    weeks = ["2026-08-16", "2026-08-23", "2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27",
             "2026-10-04"]
    return {
        "tiles": {"problems": {"count": 3, "share_pct": 30.0}},
        "trend": {"unit": "week", "steps": [{"start": w, "end": (date.fromisoformat(w) + timedelta(days=6)).isoformat()}
                                             for w in weeks]},
        "agents": [
            {"email": "sam@example.com", "name": "Sam Okafor", "contacts": 6, "avg_percent": 88.25, "problems": 1,
             "problem_rate": 16.7, "trend": [None] * 6 + [80, 88.25], "top_miss": "10-minute rule"},
            {"email": "lee@example.com", "name": "Lee Park", "contacts": 4, "avg_percent": 71.3, "problems": 2,
             "problem_rate": 50.0, "trend": [None] * 7 + [71.3], "top_miss": None},
        ],
    }


# The fake layout: the Agents table's box (and the ruler in it) is `width` px wide; a one-line table needs
# 700px, a two-line one 380px, and a table wider than its box makes the box scroll.
AGENTS_RUN_JS = """
var observers = [];
globalThis.ResizeObserver = function (callback) {
  this.observe = function (el) { observers.push({ callback: callback, el: el }); };
  this.disconnect = function () {};
};
var width = 0;
globalThis.layout = function (el) {
  if (el.className === "fit-ruler") {
    return { clientWidth: width, scrollWidth: width };
  }
  var table = el.children.filter(function (c) { return c.tagName === "TABLE"; })[0];
  if (el.className !== "scroll" || !table) {
    return { clientWidth: 0, scrollWidth: 0 };
  }
  var needs = table.classList.contains("one-line") ? 700 : 380;
  return { clientWidth: width, scrollWidth: Math.max(width, needs) };
};
var section = agentsPanel(DATA);
var out = { built: snapshot(section) };
document.body.appendChild(section);
out.steps = WIDTHS.map(function (w) {
  width = w;
  observers.forEach(function (o) { o.callback([{ target: o.el }]); });
  return snapshot(section);
});
out.observed = observers.map(function (o) { return o.el.className; });
process.stdout.write(JSON.stringify(out));
"""


def run_agents_panel(widths):
    """Build the overview's Agents panel in node, lay it out at each of `widths` in turn (the first layout
    and then resizes), and return a snapshot of the table as built and after each layout."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    source = "\n".join(js_function(js, name) for name in APP_HELPERS + (
        "plural", "stepName", "stepTip", "trendDays", "queryString", "agentsTable", "agentsPanel", "fitRowMode"))
    script = (node_prelude() + js_var(js, "FLAG_POINTS") + "\n" + js_var(js, "STEP_UNITS") + "\n" + source +
              SNAPSHOT_JS +
              f"var DATA = {json.dumps(agents_data())};\nvar WIDTHS = {json.dumps(widths)};\n" + AGENTS_RUN_JS)
    return run_node(script)


def test_overview_agents_one_line_mode():
    """Spec 4.2: one line per agent when the Agents panel is wide enough, with all seven columns in order
    in one row; the figures, the sparkline and the most-missed check keep today's values."""
    out = run_agents_panel([800])
    for shot in (out["built"], out["steps"][0]):
        assert shot["tables"] == 1
        assert shot["cls"].split() == ["atable", "one-line"]
        assert shot["head"] == [ONE_LINE_HEAD]
        assert [b["cls"] for b in shot["bodies"]] == ["row-group", "row-group"], "one row group per agent"
        sam, lee = (b["rows"] for b in shot["bodies"])
        assert len(sam) == 1 and len(lee) == 1, "one tr per agent"
        assert [c["text"] for c in sam[0]][:5] == ["Sam Okafor", "6", "88.3", "1", "16.7%"]
        assert lee[0][0]["text"] == "Lee Park"
        assert "▲" in lee[0][4]["text"], "a problem rate 10+ points above the team's is flagged"
        assert [c["charts"] for c in sam[0]] == [0, 0, 0, 0, 0, 1, 0], "the sparkline is the sixth column"
        assert sam[0][6]["text"] == "10-minute rule" and lee[0][6]["text"] == "-"
        assert "miss" in sam[0][6]["cls"].split()


def test_overview_agents_fall_back_to_two_lines_when_narrow():
    """Spec 4.2: a panel too narrow for one line gets today's two-line rows, and widening it brings the one
    line back. The table is rebuilt, never kept twice."""
    out = run_agents_panel([800, 500, 800])
    wide, narrow, wide_again = out["steps"]
    assert wide["cls"].split() == ["atable", "one-line"]
    assert narrow["tables"] == 1 and narrow["cls"].split() == ["atable", "two-line"]
    assert narrow["head"] == [ONE_LINE_HEAD[:5], ONE_LINE_HEAD[5:]]
    for body in narrow["bodies"]:
        assert body["cls"] == "row-group"
        line1, line2 = body["rows"]
        assert len(line1) == 5 and [c["charts"] for c in line2] == [1, 0]
        assert line2[1]["colspan"] == "4" and "miss" in line2[1]["cls"].split()
    assert narrow["bodies"][0]["rows"][0][0]["text"] == "Sam Okafor"
    assert wide_again == wide
    # The width is watched on the ruler, whose size a swap never changes, not on the box the swap resizes
    # (that would be a ResizeObserver loop error in the browser).
    assert out["observed"] == ["fit-ruler"]


def test_overview_agents_row_styles():
    from css_rules import app_css, media_block, top_level, values

    css = top_level(app_css())
    assert values(css, ".atable.two-line th", "white-space") == ["normal"], "headers may wrap to fit the panel"
    assert values(css, ".atable.two-line tr.line1 td", "white-space") == ["nowrap"], "the figures stay on one line"
    assert values(media_block(app_css(), "(hover: hover)"), ".atable tbody.row-group:not(.add-row):hover tr:not(.edit-row) td", "background") == ["var(--hover)"], \
        "an agent's rows highlight together"
    assert values(css, ".atable.two-line td.miss", "white-space") == ["normal"]
    assert values(css, ".atable.two-line td.miss", "overflow-wrap") == ["anywhere"]
    # One line: the headers may wrap, the figures don't, and the most-missed check wraps in a column wide
    # enough to read (when it can't have that width, the panel falls back to two lines).
    assert values(css, ".atable.one-line th", "white-space") == ["normal"]
    assert values(css, ".atable.one-line td", "white-space") == ["nowrap"]
    assert values(css, ".atable.one-line td.miss", "white-space") == ["normal"]
    assert values(css, ".atable.one-line td.miss", "overflow-wrap") == ["anywhere"]
    assert values(css, ".atable.one-line td.miss", "min-width") == ["var(--miss-min)"]


CHART_RESIZE_JS = """
var frames = [], observers = [];
globalThis.requestAnimationFrame = function (f) { frames.push(f); };
globalThis.ResizeObserver = function (callback) {
  this.observe = function () { observers.push(callback); };
  this.disconnect = function () {};
};
var width = 500;
globalThis.layout = function () { return { clientWidth: width, scrollWidth: width }; };
var el = document.createElement("div");
Charts.lineChart(el, [{ name: "Team", values: [90, 91, null, 93] }], { labels: ["a", "b", "c", "d"] });
var first = el.children[0];
var out = { drawnAt: first.getAttribute("width") };
width = 700;
observers.forEach(function (cb) { cb([]); });
observers.forEach(function (cb) { cb([]); });
out.keptInsideObserver = el.children[0] === first;
out.frames = frames.length;
frames.splice(0).forEach(function (f) { f(); });
out.redrawnAt = el.children[0].getAttribute("width");
observers.forEach(function (cb) { cb([]); });
frames.splice(0).forEach(function (f) { f(); });
out.sameWidthKept = el.children[0].getAttribute("width") === "700" && frames.length === 0;
process.stdout.write(JSON.stringify(out));
"""


def test_chart_redraws_on_the_next_frame_after_a_resize():
    """A chart redraws at its new width on the next animation frame, never inside the ResizeObserver
    callback: the redraw changes the chart's height, and doing that inside the observer is a ResizeObserver
    loop error in the browser. Several notifications in one frame make one redraw."""
    out = run_node(node_prelude() + CHART_RESIZE_JS)
    assert out["drawnAt"] == "500"
    assert out["keptInsideObserver"] is True and out["frames"] == 1
    assert out["redrawnAt"] == "700" and out["sameWidthKept"] is True


SPARK_RUN_JS = """
var cell = document.createElement("td");
Charts.sparkline(cell, VALUES, { ariaLabel: "trend" });
var svg = cell.children[0];
function all(tag) { return svg.findAll(function (e) { return e.tagName === tag; }); }
process.stdout.write(JSON.stringify({
  width: svg.getAttribute("width"),
  texts: all("TEXT").map(function (t) { return { cls: t.className, text: t.textContent }; }),
  lines: all("POLYLINE").length,
  dots: all("CIRCLE").map(function (c) { return c.className; })
}));
"""


def run_sparkline(points):
    return run_node(node_prelude() + f"var VALUES = {json.dumps(points)};\n" + SPARK_RUN_JS)


def test_sparkline_min_width_and_single_point_label():
    """Spec 4.2: the sparkline is a real small chart, at least about 80px wide. With one week of data it
    shows the point labelled with its value; with more it draws the line, unlabelled, as before."""
    from css_rules import app_css, top_level, values

    [width] = values(top_level(app_css()), ":root", "--spark-w")
    assert width.endswith("px") and float(width[:-2]) >= 80

    one = run_sparkline([None] * 7 + [71.3])
    assert one["width"] == width[:-2], "drawn at the token's width"
    assert one["lines"] == 0 and one["dots"] == ["dot last"]
    assert one["texts"] == [{"cls": "dlabel", "text": "71.3"}], "the lone point carries its value"

    two = run_sparkline([None] * 6 + [80, 88.25])
    assert two["lines"] == 1 and two["texts"] == []
    assert two["width"] == width[:-2]




# -- trend markers for rubric and model changes (Task 4, T-034) ---------------------------------------

MARKER_RUN_JS = """
globalThis.requestAnimationFrame = function () {};
globalThis.ResizeObserver = function () { this.observe = function () {}; this.disconnect = function () {}; };
globalThis.layout = function () { return { clientWidth: 600, scrollWidth: 600 }; };
var markers = POINTS.map(function (p) { return Charts.stepMarkers(p, UNIT); });
function drawn(el) {
  var svg = el.children[0];
  var titles = function (root) {
    return root.findAll(function (e) { return e.tagName === "DESC"; }).map(function (t) { return t.textContent; });
  };
  var flags = svg.findAll(function (e) { return e.className === "marker-hit"; });
  return { flags: flags.map(function (g) { return titles(g); }),
           weeks: titles(svg).filter(function (t) { return t.indexOf("week ") === 0; }) };
}
var line = document.createElement("div");
Charts.lineChart(line, [{ name: "Team", values: [90, 91, 92, 93] }], {
  labels: ["a", "b", "c", "d"], markers: markers, tooltip: function (i) { return "week " + i; } });
var bars = document.createElement("div");
Charts.barChart(bars, [10, 20, 30, 40], {
  labels: ["a", "b", "c", "d"], markers: markers, tooltip: function (i) { return "week " + i; } });
process.stdout.write(JSON.stringify({ markers: markers, line: drawn(line), bars: drawn(bars) }));
"""


@pytest.mark.parametrize("unit", ["day", "week", "month"])
def test_chart_marker_labels(unit):
    """A step can carry either marker or both, worded for the trend's step ("Rubric changed this day",
    "… this week", "… this month"): one marker line per step, whose tooltip, like the step's own, lists each
    change that applies."""
    points = [{"rubric_changed": False, "model_changed": False}, {"rubric_changed": True, "model_changed": False},
              {"rubric_changed": False, "model_changed": True}, {"rubric_changed": True, "model_changed": True}]
    out = run_node(node_prelude() + f"var POINTS = {json.dumps(points)};\nvar UNIT = {json.dumps(unit)};\n" +
                   MARKER_RUN_JS)
    rubric, model = "Rubric changed this " + unit, "Model changed this " + unit
    assert out["markers"] == [[], [rubric], [model], [rubric, model]]
    for chart in ("line", "bars"):
        assert out[chart]["flags"] == [[rubric], [model], [rubric + "\n" + model]], chart
        assert out[chart]["weeks"] == ["week 0", "week 1\n" + rubric, "week 2\n" + model,
                                       "week 3\n" + rubric + "\n" + model], chart


# Each drawn chart's marker lines, as their tooltips, in the order they are drawn.
CHART_MARKERS_JS = """
run(async function () {
  return all(screenEl, function (e) { return e.tagName === "svg" || e.tagName === "SVG"; }).map(function (svg) {
    return all(svg, function (e) { return e.className === "marker-hit"; }).map(function (g) {
      return byTag(g, "desc").concat(byTag(g, "DESC")).map(function (t) { return t.textContent; }).join("|");
    });
  }).filter(function (marks) { return marks.length; });
});
"""


def test_trend_charts_show_both_markers(conn, version, tmp_path):
    """On the page, the Team overview's two trend charts and an agent's trend mark each week the API marks,
    with the rubric marker, the model marker, or both on one line."""
    from urllib.parse import quote

    from test_compare import api_for
    from test_web_test_view import page_responses, run_page
    from work_assistant import rubric

    v2 = rubric.save_version(conn, version.checks, version.reference, version.params, "v2")
    add_review(conn, version, 1, all_met(version), call="2026-09-14T15:00:00Z")                  # Sep 13: Opus
    add_review(conn, version, 2, all_met(version), call="2026-09-21T15:00:00Z", model="claude-sonnet-5-5")
    add_review(conn, v2, 3, all_met(v2), agent=LEE)                                              # Oct 4: Opus, v2
    api = api_for(tmp_path)
    steps = api.get("/api/overview").json()["trend"]["steps"]
    trend = api.get(f"/api/agents/{LEE[0]}").json()["trend"]["steps"]

    def expected(points, unit):
        labels = (("rubric_changed", "Rubric changed this " + unit), ("model_changed", "Model changed this " + unit))
        marks = ["\n".join(text for key, text in labels if p[key]) for p in points]
        return [m for m in marks if m]

    # Last week trends by day: Oct 5 is the first day on v2, and on Opus after Sonnet on Sep 21.
    assert expected(steps, "day") == ["Rubric changed this day\nModel changed this day"]
    overview = run_page("#/overview", page_responses(api, "/api/overview"), CHART_MARKERS_JS)
    assert overview == [expected(steps, "day")] * 2, "the score line and the problem-rate bars"
    path = "/api/agents/" + quote(LEE[0], safe="")
    agent = run_page("#/agents/" + quote(LEE[0], safe=""), page_responses(api, "/api/agents", path),
                     CHART_MARKERS_JS)
    # The Agents default, Last 8 weeks, trends by week.
    assert expected(trend, "week") == ["Model changed this week", "Rubric changed this week\nModel changed this week"]
    assert agent == [expected(trend, "week")]


# -- links carry the period (Task 4, T-042) ----------------------------------------------------------------

TICKET_LINKS_JS = """
run(async function () {
  // charts.js sets a bar's link as the href property, the page's own links as the attribute.
  return all(screenEl, function (e) { return e.tagName === "A"; }).map(function (a) {
    return { text: a.textContent, href: a.getAttribute("href") || a.href };
  }).filter(function (l) {
    return (l.href || "").indexOf("#/tickets?") === 0 && l.href.indexOf("ticket=") < 0;
  });
});
"""


def test_overview_links_carry_the_period(seeded, tmp_path):
    """The most-missed checks and "See all problem tickets" open the Tickets tab on the period's days, with
    the Tickets tab's own start, end and verdict filters."""
    from test_compare import api_for
    from test_web_test_view import page_responses, run_page

    api = api_for(tmp_path)
    out = run_page("#/overview?week=2026-09-30", page_responses(api, "/api/overview?week=2026-09-30"),
                   TICKET_LINKS_JS)
    days = "start=2026-09-27&end=2026-10-03"
    assert {"text": "See all problem tickets", "href": "#/tickets?verdict=problem&" + days} in out
    assert "#/tickets?missed_check=pr-call-length&" + days in [link["href"] for link in out]


# -- fix round 1 (T-042) ----------------------------------------------------------------------------------

MOST_MISSED_ROWS_JS = """
run(async function () {
  return all(screenEl, function (e) { return e.className === "mrow"; }).map(function (row) {
    return row.children.filter(function (c) { return c.className !== "mtrack"; })
      .map(function (c) { return c.textContent; });
  });
});
"""


def test_most_missed_rows_show_share_and_count_change(seeded, tmp_path):
    """Spec 6.2: a most-missed check reads like a count tile: the count, its share of the calls, and the
    change in share with the change in count beside it. Last week: Lee missed #6 on 1 of 2 calls; the week
    before, 0 of 1."""
    from test_compare import api_for
    from test_web_test_view import page_responses, run_page

    api = api_for(tmp_path)
    rows = run_page("#/overview", page_responses(api, "/api/overview"), MOST_MISSED_ROWS_JS)
    assert rows == [["#6 Triage: Verify understanding", "1", "50% of calls · +50 pts (+1 calls) vs the period before"]]
    # Against a comparison without calls there is no change to show, but the share still reads.
    query = "?compare=2026-09-13&week=2026-09-27"
    rows = run_page("#/overview" + query, page_responses(api, "/api/overview" + query), MOST_MISSED_ROWS_JS)
    assert rows == [["10-minute rule", "1", "100% of calls · Nothing to compare yet"],
                    ["Stayed in scope", "1", "100% of calls · Nothing to compare yet"]]
    # Compared with nothing: the share alone.
    query = "?compare=none"
    rows = run_page("#/overview" + query, page_responses(api, "/api/overview" + query), MOST_MISSED_ROWS_JS)
    assert rows == [["#6 Triage: Verify understanding", "1", "50% of calls"]]
