"""The Trends screen under Team activity (Team activity Trends design §3-§4, Task 20): the side panel section, the
address, the period heading, the ten metric panels, the sortable ranking table, the highlight graph and the closing
note. app.js runs in node on the fake DOM (tests/fake_dom.js) through the stand-in page of test_web_test_view, with
`GET /api/activity/trends` answered from fixtures in the shape trends.trends_view returns."""
from __future__ import annotations

import copy
import re
from urllib.parse import urlencode

import pytest

from css_rules import STATIC, app_css, media_block, rules, tokens, top_level, values
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_edition import signed_in_manager
from test_web_team_activity import closed
from test_web_test_view import answer, page_responses, run_page

AVERY, BLAKE, CASEY, DREW, ERIN = ("avery@example.com", "blake@example.com", "casey@example.com", "drew@example.com",
                                   "erin@example.com")
NAMES = {AVERY: "Avery Cole", BLAKE: "Blake Moreno", CASEY: "Casey Nguyen", DREW: "Drew Patel", ERIN: "Erin Shaw"}
PERIOD = {"start": "2026-09-27", "end": "2026-10-10", "label": "Last 8 weeks (Aug 16 – Oct 10)", "so_far": False,
          "preset": "last_8_weeks"}
BEFORE = {"start": "2026-09-13", "end": "2026-09-26", "label": "the 8 weeks before", "so_far": False,
          "preset": "previous"}
CLOSING = ("Days off, rest days, untracked days and days not yet synced aren't counted. "
           "Built from Zendesk's own records, read-only.")
TAIL = " · click a column to sort, a tech to focus"

# key, label, group, unit, digits, better (Task 17's table, exactly)
MEASURES = [
    ("tickets_per_shift", "Tickets per shift", "Output", "number", 1, "higher"),
    ("replies_per_shift", "Replies per shift", "Output", "number", 1, "higher"),
    ("solved_per_shift", "Solved per shift", "Output", "number", 1, "higher"),
    ("shifts_worked", "Shifts worked", "Reliability", "percent", 0, "higher"),
    ("missed_shifts", "Sick days", "Reliability", "count", 0, "lower"),
    ("signed_in_no_activity", "Signed in, no activity", "Reliability", "count", 0, "lower"),
    ("late_start_min", "First action after shift start", "Time on shift", "minutes", 0, "lower"),
    ("early_finish_min", "Last action before shift end", "Time on shift", "minutes", 0, "lower"),
    ("longest_gap_min", "Longest gap", "Time on shift", "minutes", 0, "lower"),
    ("flags_per_shift", "Flags per shift", "Flags", "number", 2, "lower"),
]

# Four weekly steps: the two before the period are context, the other two are the period.
STEPS = [
    {"start": "2026-09-13", "end": "2026-09-19", "label": "Sep 13", "in_period": False, "part": False},
    {"start": "2026-09-20", "end": "2026-09-26", "label": "Sep 20", "in_period": False, "part": False},
    {"start": "2026-09-27", "end": "2026-10-03", "label": "Sep 27", "in_period": True, "part": False},
    {"start": "2026-10-04", "end": "2026-10-10", "label": "Oct 4", "in_period": True, "part": False},
]


def figures(now, before, steps, digits=1, rolling=None) -> dict:
    """A tech's or the team's figures. The line pools a trailing window (`rolling`, by default the steps themselves),
    12 shifts behind each of its points and 3 behind each week's own figure."""
    change = None if now is None or before is None else round(now - before, digits)
    rolling = list(steps) if rolling is None else rolling
    return {"now": now, "before": before, "change": change, "steps": steps,
            "steps_n": [0 if v is None else 3 for v in steps], "rolling": rolling,
            "rolling_n": [0 if v is None else 12 for v in rolling]}


def card(title: str, sub: str, *rows: tuple[str, str, str], foot: str | None = None) -> str:
    """A hovercard's text as its <desc> holds it (charts.js plainText): the title, the days, a line per row (label,
    value and, when there is one, the note in brackets), then the foot."""
    lines = [title, sub] + [f"{label} {value}" + (f" ({note})" if note else "") for label, value, note in rows]
    return "\n".join(lines + ([foot] if foot else []))


def step_card(step: str, days: str, who: str, value: str, team: str, *, base: str = "shifts", own: str | None = None,
              window: int = 4, unit: str = "week", part: bool = False) -> str:
    """The hovercard of one step, as the test answer's figures give it: the tech's line (12 behind each point), that
    step alone (3 behind it) and the team's line (12)."""
    own = value if own is None else own
    average = f", {window}-{unit} average"
    return card(f"{'Month' if unit == 'month' else 'Week'} of {step}", days + (f" · part {unit}" if part else ""),
                (who + average, value, "" if value == "-" else f"12 {base}"),
                (f"That {unit} alone", own, "" if own == "-" else f"3 {base}"),
                ("Team" + average, team, f"12 {base}"),
                foot=f"The line pools every {base[:-1]} in the {window} {unit}s up to each point.")


def plain(i: int) -> dict:
    """Plain, distinct figures for a measure the tests don't read closely."""
    base = float(i + 2)
    return {"team": figures(base, base + 1, [base] * 4),
            "techs": [figures(base + t, base + 1, [base + t] * 4) for t in range(5)]}


# Tickets per shift (higher is better): Blake slipping, Drew steady, Avery improving, Casey new, Erin away.
TICKETS = {
    "team": figures(6.1, 6.5, [6.0, 6.4, 6.2, 6.0]),
    "techs": [
        figures(8.2, 7.0, [6.8, 7.2, 8.0, 8.4]),          # Avery
        figures(4.2, 6.3, [6.0, 6.6, 4.0, 4.4]),          # Blake
        figures(6.0, None, [None, None, 5.8, 6.2]),       # Casey: new
        figures(7.0, 7.0, [7.0, 7.0, 7.0, 7.0]),          # Drew: no change
        figures(None, 5.0, [5.0, 5.0, None, None]),       # Erin: away all period
    ],
}
# First action after shift start (lower is better).
LATE = {
    "team": figures(20, 18, [18, 18, 20, 20], 0),
    "techs": [
        figures(12, 24, [24, 24, 12, 12], 0),             # Avery: 12 min earlier, better
        figures(30, 10, [10, 10, 30, 30], 0),             # Blake: 20 min later, worse
        figures(15, None, [None, None, 15, 15], 0),
        figures(18, 18, [18, 18, 18, 18], 0),
        figures(None, 20, [20, 20, None, None], 0),
    ],
}
SHIFTS_WORKED = {
    "team": figures(95, 96, [96, 96, 95, 95], 0),
    "techs": [figures(100, 100, [100] * 4, 0), figures(90, 95, [95, 95, 90, 90], 0),
              figures(100, None, [None, None, 100, 100], 0), figures(95, 95, [95] * 4, 0),
              figures(None, 90, [90, 90, None, None], 0)],
}


def trends(*, comparison=BEFORE, coverage_from=None, unit="week", steps=None, overrides=None) -> dict:
    """The API's answer: every measure, with the ones in `overrides` (key -> {"team", "techs"}) given exact figures."""
    exact = {"tickets_per_shift": TICKETS, "late_start_min": LATE, "shifts_worked": SHIFTS_WORKED, **(overrides or {})}
    rows = []
    for i, (key, label, group, kind, digits, better) in enumerate(MEASURES):
        figs = copy.deepcopy(exact.get(key) or plain(i))
        rows.append({"key": key, "label": label, "group": group, "unit": kind, "digits": digits,
                     "team_digits": 1 if kind == "count" else digits, "better": better,
                     "window": ({"count": 8 if key == "satisfaction" else 4, "unit": "week"} if unit == "week"
                                else {"count": 2, "unit": "month"}),
                     "base": "ratings" if key == "satisfaction" else "calls" if kind == "duration" else "shifts",
                     "team": figs["team"], "techs": figs["techs"]})
    if comparison is None:
        for row in rows:
            for figs in [row["team"], *row["techs"]]:
                figs["before"] = None
                figs["change"] = None
    return {"period": PERIOD, "comparison": comparison, "today": "2026-10-17",
            "synced": {"at": "2026-10-17T14:42:00Z", "status": "complete", "running": False},
            "coverage_from": coverage_from, "unit": unit, "steps": steps or STEPS,
            "techs": [{"email": e, "name": NAMES[e]} for e in (AVERY, BLAKE, CASEY, DREW, ERIN)],
            "measures": rows, "picker": [{"email": e, "name": NAMES[e]} for e in (AVERY, BLAKE, CASEY, DREW, ERIN)]}


def url(**params) -> str:
    query = urlencode({k: v for k, v in params.items() if v})
    return "/api/activity/trends" + ("?" + query if query else "")


READ = """
function byId(id) { return document.getElementById(id); }
function head() { var el = screenEl.querySelectorAll(".period-head")[0]; return el ? el.textContent : null; }
function metricTiles() {
  return screenEl.querySelectorAll("button.metric-tile").map(function (b) {
    var d = b.querySelectorAll(".d")[0];
    var delta = d ? d.querySelectorAll(".mdelta")[0] : null;
    return { label: b.querySelectorAll(".k")[0].textContent, value: b.querySelectorAll(".v")[0].textContent,
      change: d ? d.textContent : null, changeTitle: d ? d.getAttribute("title") : null,
      changeCls: delta ? delta.className : null, group: b.querySelectorAll(".grp")[0].textContent,
      cls: b.className, pressed: b.getAttribute("aria-pressed") };
  });
}
function rank() { return screenEl.querySelectorAll("table.trend-rank")[0] || null; }
function rankRows() {
  return rank().querySelectorAll("tbody tr").map(function (tr) {
    var tds = tr.querySelectorAll("td");
    var delta = tds[2] ? tds[2].querySelectorAll(".mdelta")[0] : null;
    return { cls: tr.className, name: tds[0].textContent, now: tds[1].textContent,
      change: tds[2] ? tds[2].textContent : null, changeCls: delta ? delta.className : null };
  });
}
function names() { return rankRows().map(function (r) { return r.name; }); }
function lit() {
  return rankRows().filter(function (r) { return r.cls.split(" ").indexOf("sel") >= 0; }).map(function (r) { return r.name; });
}
function sortHeads() {
  return rank().querySelectorAll("thead th").map(function (th) {
    var days = th.querySelectorAll(".th-days")[0];
    return { text: th.querySelectorAll("button")[0].textContent, days: days ? days.textContent : null,
      sort: th.getAttribute("aria-sort"), cls: th.querySelectorAll("button")[0].className };
  });
}
function sortBtn(text) {
  return rank().querySelectorAll("thead button").filter(function (b) { return b.textContent.indexOf(text) === 0; })[0];
}
function nameBtn(name) {
  return rank().querySelectorAll("tbody button").filter(function (b) { return b.textContent === name; })[0];
}
function tileBtn(label) {
  return screenEl.querySelectorAll("button.metric-tile").filter(function (b) {
    return b.querySelectorAll(".k")[0].textContent === label;
  })[0];
}
function subs() { return panels().map(function (p) { var s = p.querySelectorAll(".sub")[0]; return s ? s.textContent : null; }); }
function titles() { return panels().map(titleOf); }
function svg() { return screenEl.querySelectorAll("svg.chart")[0]; }
function endLabels() {
  return svg().findAll(function (e) { return e.tagName === "TEXT" && e.classList.contains("dlabel"); })
    .map(function (t) { return t.textContent; });
}
function tooltips() {
  return svg().findAll(function (e) { return e.classList.contains("hit"); }).map(function (g) {
    return g.findAll(function (e) { return e.tagName === "DESC"; })[0].textContent;
  });
}
function axisTicks() {
  return svg().findAll(function (e) { return e.tagName === "TEXT" && e.classList.contains("tick") && e.getAttribute("text-anchor") === "end"; })
    .map(function (t) { return Number(t.textContent.replace("%", "")); });
}
function getRequests() { return requests.filter(function (r) { return r.method === "GET" && r.url.indexOf("/api/activity/trends") === 0; }); }
/* The stand-in page doesn't fire hashchange when the address is set, so an action that moves the address is
   followed by the route being run on it, as the browser would. */
async function act(el, type) { fire(el, type || "click"); await go(location.hash); }
async function pick(id, value) { var el = byId(id); el.value = value; await act(el, "change"); }
function hashNow() { return location.hash; }
"""


def trends_page(tmp_path, start_hash: str, bodies: dict[str, dict], scenario: str, setup: str = ""):
    """The page at `start_hash` with each of `bodies` answered. As the real API does, a read that names a tech gets the
    same answer as the one without (every tech is in it), unless `bodies` gives it its own. `setup` is script run
    before the page draws (a layout, a ResizeObserver)."""
    responses = page_responses(api_for(tmp_path))
    for path, body in bodies.items():
        if "tech=" not in path:
            for email in NAMES:
                tech = path + ("&" if "?" in path else "?") + urlencode({"tech": email})
                responses.setdefault("GET " + tech, answer(body))
    for path, body in bodies.items():
        responses["GET " + path] = answer(body)
    return run_page(start_hash, responses, READ + setup + "run(async function () {\n" + scenario + "\n});")


def default_page(tmp_path, scenario: str, body=None, start="#/team/trends", setup=""):
    return trends_page(tmp_path, start, {url(): body or trends()}, scenario, setup)


OPEN = "#/team/trends?measure=tickets_per_shift"   # Tickets per shift picked: its ranking and graph are open


def open_page(tmp_path, scenario: str, body=None):
    """The page with Tickets per shift open: nothing opens until a panel is picked (T-152)."""
    return default_page(tmp_path, scenario, body, start=OPEN)


# -- the side panel and the address -----------------------------------------------------------------------


def test_side_panel_has_team_activity_section_with_activity_and_trends(tmp_path, conn, version):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nav = re.search(r'<nav class="sidenav" aria-label="Sections">(.*?)</nav>', html, re.S).group(1)
    items = [(" ".join(attrs.split()), text.strip()) for _, attrs, text in re.findall(r"<(a)\b([^>]*)>([^<]*)", nav)]
    assert items[5:10] == [   # hidden until the Show Team activity setting is read (R9)
        ('class="parent" data-section="team" href="#/team" hidden', "Team"),
        ('class="sub" data-tab="team" href="#/team" hidden', "Activity"),
        ('class="sub" data-tab="trends" href="#/team/trends" hidden', "Trends"),
        ('class="sub" data-tab="oneonone" href="#/oneonone" hidden', "1:1 Prep"),
        ('data-tab="settings" href="#/settings"', "Settings"),
    ]
    out = default_page(tmp_path, "return links(sidenav).map(function (l) { return [l.text, l.href]; });")
    assert [text for text, _ in out][5:10] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"]
    assert dict(out)["Trends"] == "#/team/trends" and dict(out)["Activity"] == "#/team"


SHELL = """
function section(label) {
  return document.querySelectorAll(".sidenav a").filter(function (a) { return a.classList.contains("parent") && a.textContent === label; })[0];
}
function snap() {
  var crumb = all(titleEl, function (e) { return e.classList.contains("crumb"); })[0];
  return { lit: settingsTab(), teamOpen: section("Team").classList.contains("open"),
    callQaOpen: section("Call QA").classList.contains("open"), title: titleEl.textContent,
    crumb: crumb ? crumb.textContent : null, docTitle: document.title };
}
"""


def test_team_address_still_opens_activity_and_lights_the_section(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path), "/api/activity")
    out = run_page("#/team", responses, SHELL + READ + """
run(async function () {
  var out = { team: snap() };
  var before = requests.length;
  await go("#/team?period=this_week&tech=avery%40example.com");
  out.query = snap();
  out.queryRequests = requests.slice(before).map(function (r) { return r.url; });
  out.queryScreen = screenEl.textContent;
  await go("#/team/trends");
  out.trends = snap();
  await go("#/overview");
  out.overview = snap();
  return out;
});
""")
    assert out["team"]["lit"] == ["team"] and out["team"]["teamOpen"] and not out["team"]["callQaOpen"]
    assert out["query"]["lit"] == ["team"] and out["query"]["teamOpen"]
    assert out["queryRequests"][0].startswith("/api/activity?") and "this_week" in out["queryRequests"][0]
    assert not any("/trends" in u for u in out["queryRequests"]), "a #/team address opens Activity, not Trends"
    assert out["trends"]["lit"] == ["trends"] and out["trends"]["teamOpen"] and not out["trends"]["callQaOpen"]
    assert out["overview"]["lit"] == ["overview"] and out["overview"]["callQaOpen"] and not out["overview"]["teamOpen"]


def test_trends_title_and_crumb(tmp_path, conn, version):
    out = default_page(tmp_path, SHELL + """
  var trends = snap();
  await go("#/team");
  return { trends: trends, activity: snap() };
""")
    assert out["trends"]["title"] == "Team › Trends"
    assert out["trends"]["crumb"] == "Team ›"
    assert out["trends"]["docTitle"] == "Team › Trends · Work Assistant"
    assert out["activity"]["title"] == "Team › Activity"
    assert out["activity"]["crumb"] == "Team ›"
    assert out["activity"]["docTitle"] == "Team › Activity · Work Assistant"


# -- the heading and the filters --------------------------------------------------------------------------


def test_heading_names_period_comparison_and_tech_count(tmp_path, conn, version):
    out = default_page(tmp_path, "return head();")
    assert out == "Last 8 weeks (Aug 16 – Oct 10) vs the 8 weeks before · 5 techs"


def test_heading_without_a_comparison_has_no_vs_part(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?compare=none", {url(compare="none"): trends(comparison=None)},
                      "return head();")
    assert out == "Last 8 weeks (Aug 16 – Oct 10) · 5 techs"


def test_heading_says_data_from_when_coverage_starts_later(tmp_path, conn, version):
    out = default_page(tmp_path, "return head();", trends(coverage_from="2026-07-21"))
    assert out == "Last 8 weeks (Aug 16 – Oct 10) vs the 8 weeks before · 5 techs · data from Jul 21"


def test_filters_in_order_tech_period_compare_then_earlier_and_later(tmp_path, conn, version):
    out = default_page(tmp_path, """
  var f = screenEl.querySelectorAll(".filters")[0];
  return { order: f.children.map(function (c) { return c.className; }),
    labels: f.querySelectorAll("label.filt").map(function (l) { return l.querySelectorAll("b")[0].textContent; }),
    techs: byId("tr-tech").children.map(function (o) { return o.textContent; }),
    period: byId("tr-period").value, compare: byId("ov-compare").value };
""")
    assert out["order"] == ["filt", "period-pick", "period-pick", "stepper", "message"]
    assert out["labels"] == ["Tech", "Period", "Compare to"]
    assert out["techs"] == ["All techs", "Avery Cole", "Blake Moreno", "Casey Nguyen", "Drew Patel", "Erin Shaw"]
    assert out["period"] == "last_8_weeks" and out["compare"] == "previous"


def test_last_year_offers_compare_to(tmp_path, conn, version):
    """T-143 (undoing T-132's rule): the counts are kept beyond retention, so Last year can be compared with the year
    before once it is stored; the heading's "data from" note covers days before the stored history."""
    year = {"start": "2025-10-01", "end": "2026-09-30", "label": "Last year (Oct 2025 – Sep 2026)",
            "so_far": False, "preset": "last_year"}
    before = {"start": "2024-10-01", "end": "2025-09-30", "label": "the year before", "so_far": False,
              "preset": "previous"}
    body = trends(comparison=before, coverage_from="2025-06-01")
    body["period"] = year
    out = trends_page(tmp_path, "#/team/trends?period=last_year&compare=previous",
                      {url(period="last_year", compare="previous"): body}, """
  var f = screenEl.querySelectorAll(".filters")[0];
  return { labels: f.querySelectorAll("label.filt").map(function (l) { return l.querySelectorAll("b")[0].textContent; }),
    compare: byId("ov-compare").value, period: byId("tr-period").value, head: head() };
""")
    assert out == {"labels": ["Tech", "Period", "Compare to"], "compare": "previous", "period": "last_year",
                   "head": "Last year (Oct 2025 – Sep 2026) vs the year before · 5 techs · data from Jun 1"}


def test_compare_to_defaults_to_nothing_and_the_address_names_the_period_before(tmp_path, conn, version):
    """T-143: Compare to starts at Nothing, which the address leaves out; the period before is written as
    compare=previous. Call QA's Team overview keeps the period before as its default."""
    out = trends_page(tmp_path, OPEN, {url(): trends(comparison=None), url(compare="previous"): trends()}, """
  var out = { first: byId("ov-compare").value, heads: sortHeads().length };
  await pick("ov-compare", "previous");
  out.previous = { hash: hashNow(), value: byId("ov-compare").value, head: head() };
  await pick("ov-compare", "none");
  out.none = { hash: hashNow(), value: byId("ov-compare").value };
  out.urls = getRequests().map(function (r) { return r.url; });
  return out;
""")
    assert out["first"] == "none" and out["heads"] == 2, "no vs before column"
    assert out["previous"] == {"hash": "#/team/trends?compare=previous&measure=tickets_per_shift", "value": "previous",
                               "head": "Last 8 weeks (Aug 16 – Oct 10) vs the 8 weeks before · 5 techs"}
    assert out["none"] == {"hash": OPEN, "value": "none"}
    assert out["urls"] == [url(), url(compare="previous"), url()], "Nothing again is drawn from the kept answer"


def test_earlier_steps_the_period_back_by_its_own_length(tmp_path, conn, version):
    out = default_page(tmp_path, """
  await act(byId("tr-earlier"));
  return hashNow();
""")
    # The period is 14 days (Sep 27 - Oct 10): Earlier is the 14 days before, as a custom range.
    assert out == "#/team/trends?period=custom&start=2026-09-13&end=2026-09-26"


def test_measure_and_sort_draw_from_the_kept_answer_and_a_highlight_reads_nothing(tmp_path, conn, version):
    """T-143: one mechanism keeps every answer by its address. A tech or a comparison is its own read; a measure or a
    sort draws at once from the answer kept for the same read, which is read again in the background; a highlight
    reads nothing."""
    out = trends_page(tmp_path, "#/team/trends", {url(): trends(comparison=None), url(tech=BLAKE): trends(comparison=None),
                                                  url(compare="previous", tech=BLAKE): trends()}, """
  var counts = [getRequests().length];
  await pick("tr-tech", "blake@example.com");
  counts.push(getRequests().length);
  waitingRequests = [];
  await act(tileBtn("Longest gap"));
  var drawn = tileBtn("Longest gap").getAttribute("aria-pressed");
  counts.push(getRequests().length);
  waitingRequests.forEach(function (request) { request.answer(); });
  waitingRequests = null;
  await settle();
  await act(sortBtn("Average"));
  counts.push(getRequests().length);
  fire(nameBtn("Drew Patel"), "click");
  await settle();
  counts.push(getRequests().length);
  await pick("ov-compare", "previous");
  counts.push(getRequests().length);
  return { counts: counts, drawn: drawn, urls: getRequests().map(function (r) { return r.url; }), hash: hashNow() };
""")
    assert out["counts"] == [1, 2, 3, 4, 4, 5]
    assert out["drawn"] == "true", "the measure is drawn while its background read is still waiting"
    assert out["urls"] == [url(), url(tech=BLAKE), url(tech=BLAKE), url(tech=BLAKE), url(compare="previous", tech=BLAKE)]
    assert out["hash"] == "#/team/trends?compare=previous&tech=blake%40example.com&measure=longest_gap_min&sort=-now"


def test_a_tech_in_the_address_is_sent_to_the_api(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?tech=blake%40example.com", {url(tech=BLAKE): trends(), url(): trends()},
                      """
  var first = getRequests().map(function (r) { return r.url; });
  await pick("tr-tech", "");
  return { first: first, after: getRequests().length, line: screenEl.querySelectorAll(".metric-lead")[0].textContent };
""")
    assert out["first"] == [url(tech=BLAKE)]
    assert out["after"] == 2, "All techs is a read of its own (T-143: every answer is kept by its address)"
    assert out["line"].startswith("Team averages")


REGIONS = {AVERY: "NASA", BLAKE: "APAC", CASEY: "APAC", DREW: "NASA", ERIN: "NASA"}
LEAD = """
function lead() { return screenEl.querySelectorAll(".metric-lead")[0].textContent; }
function urls() { return getRequests().map(function (r) { return r.url; }); }
"""


def regional(emails: list[str] | None = None, regions: dict | None = None) -> dict:
    """The answer with each tech's region from `regions` (default REGIONS: Avery, Drew and Erin NASA; Blake and Casey
    APAC), narrowed to `emails` as the API narrows a read of a region (None: every tech). The picker is every tech,
    whatever was asked, as the API's is (Ruling 9)."""
    regions = regions or REGIONS
    body = trends(comparison=None)
    kept = [i for i, t in enumerate(body["techs"]) if emails is None or t["email"] in emails]
    body["picker"] = [{**t, "region": regions[t["email"]]} for t in body["techs"]]
    body["techs"] = [{**body["techs"][i], "region": regions[body["techs"][i]["email"]]} for i in kept]
    for m in body["measures"]:
        m["techs"] = [m["techs"][i] for i in kept]
    return body


LEAD_LEE, LEAD_KIM = "lee.lead@example.com", "kim.lead@example.com"


def teamed(emails: list[str] | None, team: str | None, tech: str | None = None) -> dict:
    """The answer for one tech (`tech`) or one team as the API gives it (T-144): the techs of `team`, a lead's email
    (None: no team, as on the PC, so every tech), as `emails` lists them (None: every tech), naming the tech and the
    team. The picker is every tech, whatever was asked."""
    body = regional(emails)
    body["tech"], body["team"] = tech, team
    return body


def test_a_picked_tech_is_read_as_their_team_kept_for_every_tech_on_it(tmp_path, conn, version):
    """T-144: one tech is compared with their team lead's team, so once an answer for one tech has said their team (the
    techs it lists), the read for any tech on it is the read for that team. Every tech on a team shares one kept
    answer: picking another of them draws at once, while the read again waits. A tech no answer has placed yet is asked
    for by name. The address keeps the tech, not the team."""
    out = trends_page(tmp_path, OPEN, {url(): regional(),
                                                  url(tech=BLAKE): teamed([AVERY, BLAKE], LEAD_LEE, BLAKE),
                                                  url(team=LEAD_LEE): teamed([AVERY, BLAKE], LEAD_LEE)}, LEAD + """
  await pick("tr-tech", "blake@example.com");
  var out = { blake: { line: lead(), names: names() } };
  await pick("tr-tech", "avery@example.com");
  out.avery = { line: lead(), names: names() };
  waitingRequests = [];
  await pick("tr-tech", "blake@example.com");
  out.again = { line: lead(), waiting: waitingRequests.map(function (r) { return r.url; }).filter(function (u) {
    return u.indexOf("/api/activity/trends") === 0;
  }) };
  waitingRequests.forEach(function (request) { request.answer(); });
  waitingRequests = null;
  await settle();
  await pick("tr-tech", "casey@example.com");
  out.casey = { line: lead(), hash: hashNow() };
  out.urls = urls();
  return out;
""")
    assert out["blake"] == {"line": "Blake's averages for this period · click a panel to see it week by week",
                            "names": ["Blake Moreno", "Avery Cole", "Team average"]}
    assert out["avery"] == {"line": "Avery's averages for this period · click a panel to see it week by week",
                            "names": ["Blake Moreno", "Avery Cole", "Team average"]}
    assert out["again"] == {"line": "Blake's averages for this period · click a panel to see it week by week",
                            "waiting": [url(team=LEAD_LEE)]}, "drawn from the team's kept answer"
    assert out["casey"] == {"line": "Casey's averages for this period · click a panel to see it week by week",
                            "hash": "#/team/trends?tech=casey%40example.com&measure=tickets_per_shift"}
    assert out["urls"] == [url(), url(tech=BLAKE), url(team=LEAD_LEE), url(team=LEAD_LEE), url(tech=CASEY)]


def test_a_tech_whose_team_no_answer_gives_is_asked_for_by_name(tmp_path, conn, version):
    """An address naming a tech no answer has placed yet asks for the tech by name: the API checks them and answers with
    their team, which places the rest of it. A write forgets what the answers said with the answers (it may have moved
    a tech), so the tech is asked for by name again."""
    settings = api_for(tmp_path).get("/api/settings").json()
    responses = page_responses(api_for(tmp_path))
    for path, tech in ((url(tech=BLAKE), BLAKE), (url(tech=AVERY), AVERY), (url(team=LEAD_LEE), None)):
        responses["GET " + path] = answer(teamed([AVERY, BLAKE], LEAD_LEE, tech))
    responses["PUT /api/settings"] = answer(settings)
    out = run_page("#/team/trends?tech=blake%40example.com", responses, READ + LEAD + """
run(async function () {
  var out = { opened: urls() };
  await pick("tr-tech", "avery@example.com");
  out.picked = urls().slice(out.opened.length);
  await go("#/settings");
  fire(buttonIn(panelTitled("Activity flags"), "Save flags"), "click");
  await settle();
  var before = urls().length;
  await go("#/team/trends?tech=avery%40example.com");
  out.after = urls().slice(before);
  out.line = lead();
  return out;
});
""")
    assert out == {"opened": [url(tech=BLAKE)], "picked": [url(team=LEAD_LEE)], "after": [url(tech=AVERY)],
                   "line": "Avery's averages for this period · click a panel to see it week by week"}


def test_a_tech_on_no_team_is_read_as_all_techs_kept_for_every_one_of_them(tmp_path, conn, version):
    """On the PC no tech is on a team, so one tech is compared with every tech: once an answer for one tech has said so,
    the read for any tech it lists is the read for All techs, kept for them all."""
    out = trends_page(tmp_path, OPEN, {url(): regional(), url(tech=BLAKE): teamed(None, None, BLAKE)},
                      LEAD + """
  await pick("tr-tech", "blake@example.com");
  waitingRequests = [];
  await pick("tr-tech", "casey@example.com");
  var out = { line: lead(), names: names(), waiting: waitingRequests.map(function (r) { return r.url; }).filter(
    function (u) { return u.indexOf("/api/activity/trends") === 0; }) };
  waitingRequests.forEach(function (request) { request.answer(); });
  waitingRequests = null;
  await settle();
  out.hash = hashNow();
  out.urls = urls();
  return out;
""")
    assert out == {"line": "Casey's averages for this period · click a panel to see it week by week",
                   "names": ["Blake Moreno", "Casey Nguyen", "Drew Patel", "Avery Cole", "Erin Shaw", "Team average"],
                   "waiting": [url()], "hash": "#/team/trends?tech=casey%40example.com&measure=tickets_per_shift",
                   "urls": [url(), url(tech=BLAKE), url()]}


REGION_IN, TECH_IN = "\u00a0" * 3, "\u00a0" * 6   # a region's indent under All techs, and a tech's under its region


def test_a_viewer_across_regions_picks_a_region_or_a_tech_from_the_grouped_picker(tmp_path, conn, version):
    """Regions design: for a viewer whose techs span regions, the Tech picker is All techs, then each region in order
    (NASA, EMEA, APAC: none here is EMEA's) slightly indented, each followed by its techs further in. A region is asked
    for as region= and written in the address; picking a tech drops it, and All techs drops both. The selected line is
    the tech when the address names one, else the region, else All techs. The lines are the answer's picker
    (Ruling 9): a region's answer lists only that region's techs, and every line is still offered."""
    out = trends_page(tmp_path, OPEN, {url(): regional(), url(region="APAC"): regional([BLAKE, CASEY])},
                      LEAD + """
  function options() { return byId("tr-tech").children.map(function (o) { return [o.value, o.textContent]; }); }
  var out = { options: options(), first: byId("tr-tech").value };
  await pick("tr-tech", "region:APAC");
  out.apac = { hash: hashNow(), selected: byId("tr-tech").value, options: options(), line: lead(), names: names() };
  await pick("tr-tech", "casey@example.com");
  out.casey = { hash: hashNow(), selected: byId("tr-tech").value, line: lead() };
  await pick("tr-tech", "");
  out.all = { hash: hashNow(), selected: byId("tr-tech").value };
  out.urls = urls();
  return out;
""")
    assert out["options"] == [
        ["", "All techs"],
        ["region:NASA", REGION_IN + "NASA"], [AVERY, TECH_IN + "Avery Cole"], [DREW, TECH_IN + "Drew Patel"],
        [ERIN, TECH_IN + "Erin Shaw"],
        ["region:APAC", REGION_IN + "APAC"], [BLAKE, TECH_IN + "Blake Moreno"], [CASEY, TECH_IN + "Casey Nguyen"],
    ]
    assert out["first"] == ""
    assert out["apac"] == {"hash": "#/team/trends?region=APAC&measure=tickets_per_shift", "selected": "region:APAC",
                           "options": closed(out["options"], "region:APAC"),
                           "line": "Team averages for this period · click a panel to see it week by week",
                           "names": ["Blake Moreno", "Casey Nguyen", "Team average"]}
    assert out["casey"] == {"hash": "#/team/trends?tech=casey%40example.com&measure=tickets_per_shift", "selected": CASEY,
                            "line": "Casey's averages for this period · click a panel to see it week by week"}
    assert out["all"] == {"hash": OPEN, "selected": ""}
    assert out["urls"] == [url(), url(region="APAC"), url(tech=CASEY), url()], \
        "a region's answer names no team, so Casey is asked for by name"


LEADS_BOOK = {"people": [{"email": LEAD_KIM, "role": "tl", "name": "Kim Lead", "techs": [CASEY, DREW]},
                         {"email": LEAD_LEE, "role": "tl", "name": "Lee Lead", "techs": [AVERY, BLAKE]}],
              "roster": []}   # a manager's people: Erin is on no team


def test_a_manager_picks_a_team_on_trends(tmp_path, conn, version):
    """Managers design: a manager's Tech picker on Trends is Activity's: each region, then each team lead's team in it,
    then its techs. A team is asked for as team= and written in the address (dropping tech and region); the picker
    shows it picked, without its indent. A picked team's answer doesn't say which team each of its techs is compared
    with (a tech on two teams is compared with one), so a tech picked next is asked for by name."""
    responses = signed_in_manager(page_responses(api_for(tmp_path)), LEADS_BOOK)
    for path, body in ((url(), regional()), (url(team=LEAD_LEE), teamed([AVERY, BLAKE], LEAD_LEE)),
                       (url(tech=AVERY), teamed([AVERY, BLAKE], LEAD_LEE, AVERY))):
        responses["GET " + path] = answer(body)
    out = run_page("#/team/trends?region=APAC&measure=longest_gap_min", {**responses,
                   "GET " + url(region="APAC"): answer(regional([BLAKE, CASEY]))}, READ + LEAD + """
run(async function () {
  function options() { return byId("tr-tech").children.map(function (o) { return [o.value, o.textContent]; }); }
  var out = { options: options() };
  await pick("tr-tech", "team:lee.lead@example.com");
  out.team = { hash: hashNow(), selected: byId("tr-tech").value, options: options(), line: lead(), names: names() };
  await pick("tr-tech", "avery@example.com");
  out.avery = { hash: hashNow(), line: lead() };
  out.urls = urls();
  return out;
});
""")
    lines = [
        ["", "All techs"],
        ["region:NASA", REGION_IN + "NASA"],
        ["team:" + LEAD_KIM, TECH_IN + "Kim Lead's team"], [DREW, TECH_IN + REGION_IN + "Drew Patel"],
        ["team:" + LEAD_LEE, TECH_IN + "Lee Lead's team"], [AVERY, TECH_IN + REGION_IN + "Avery Cole"],
        [ERIN, TECH_IN + "Erin Shaw (no team lead)"],
        ["region:APAC", REGION_IN + "APAC"],
        ["team:" + LEAD_KIM, TECH_IN + "Kim Lead's team"], [CASEY, TECH_IN + REGION_IN + "Casey Nguyen"],
        ["team:" + LEAD_LEE, TECH_IN + "Lee Lead's team"], [BLAKE, TECH_IN + REGION_IN + "Blake Moreno"],
    ]
    assert out["options"] == closed(lines, "region:APAC")
    assert out["team"] == {"hash": "#/team/trends?team=lee.lead%40example.com&measure=longest_gap_min",
                           "selected": "team:" + LEAD_LEE, "options": closed(lines, "team:" + LEAD_LEE),
                           "line": "Team averages for this period · click a panel to see it week by week",
                           "names": ["Blake Moreno", "Avery Cole", "Team average"]}
    assert out["avery"] == {"hash": "#/team/trends?tech=avery%40example.com&measure=longest_gap_min",
                            "line": "Avery's averages for this period · click a panel to see it week by week"}
    assert out["urls"] == [url(region="APAC"), url(team=LEAD_LEE), url(tech=AVERY)]


def test_a_picked_tech_missing_from_their_learned_teams_answer_is_asked_for_by_name(tmp_path, conn, version):
    """Task 3's review (F2), for teams (T-144): a tech's team learned from an answer goes stale when their team is
    changed elsewhere (another admin, another browser). When the read for the learned team comes back without the
    picked tech, the page forgets that team and asks for the tech by name, which the API answers with their team now."""
    out = trends_page(tmp_path, OPEN, {url(): regional(),
                                                  url(tech=AVERY): teamed([AVERY, BLAKE], LEAD_LEE, AVERY),
                                                  url(team=LEAD_LEE): teamed([AVERY], LEAD_LEE),
                                                  url(tech=BLAKE): teamed([BLAKE, CASEY], LEAD_KIM, BLAKE)}, LEAD + """
  await pick("tr-tech", "avery@example.com");
  await pick("tr-tech", "blake@example.com");
  return { urls: urls(), line: lead(), names: names(), hash: hashNow(), selected: byId("tr-tech").value };
""")
    assert out == {"urls": [url(), url(tech=AVERY), url(team=LEAD_LEE), url(tech=BLAKE)],
                   "line": "Blake's averages for this period · click a panel to see it week by week",
                   "names": ["Blake Moreno", "Casey Nguyen", "Team average"],
                   "hash": "#/team/trends?tech=blake%40example.com&measure=tickets_per_shift", "selected": BLAKE}


def test_address_leaves_out_defaults_and_keeps_the_rest(tmp_path, conn, version):
    out = default_page(tmp_path, """
  var hashes = [];
  await pick("tr-tech", "blake@example.com");
  hashes.push(hashNow());
  fire(tileBtn("Longest gap"), "click");
  await settle();
  hashes.push(hashNow());
  await act(sortBtn("Average"));
  hashes.push(hashNow());
  fire(tileBtn("Longest gap"), "click");
  await settle();
  hashes.push(hashNow());
  await pick("tr-tech", "");
  hashes.push(hashNow());
  fire(tileBtn("Tickets per shift"), "click");
  await settle();
  hashes.push(hashNow());
  await act(sortBtn("Average"));
  await act(sortBtn("vs before"));
  hashes.push(hashNow());
  fire(tileBtn("Tickets per shift"), "click");
  await settle();
  hashes.push(hashNow());
  return hashes;
""")
    assert out == [
        "#/team/trends?tech=blake%40example.com",
        "#/team/trends?tech=blake%40example.com&measure=longest_gap_min",
        "#/team/trends?tech=blake%40example.com&measure=longest_gap_min&sort=now",
        "#/team/trends?tech=blake%40example.com&sort=now",
        "#/team/trends?sort=now",
        "#/team/trends?measure=tickets_per_shift&sort=now",
        "#/team/trends?measure=tickets_per_shift",
        "#/team/trends",
    ]


# -- the ten metric panels --------------------------------------------------------------------------------


def test_ten_panels_in_order_with_value_change_and_group(tmp_path, conn, version):
    out = default_page(tmp_path, """
  return { tiles: metricTiles(), line: screenEl.querySelectorAll(".metric-lead")[0].textContent,
    row: screenEl.querySelectorAll(".metric-tiles")[0].children.length };
""")
    tiles = out["tiles"]
    assert out["row"] == 10
    assert [t["label"] for t in tiles] == [m[1] for m in MEASURES]
    assert [t["group"] for t in tiles] == [m[2] for m in MEASURES]
    by_label = {t["label"]: t for t in tiles}
    assert (by_label["Tickets per shift"]["value"], by_label["Tickets per shift"]["change"]) == ("6.1", "▼ 0.4")
    assert (by_label["Shifts worked"]["value"], by_label["Shifts worked"]["change"]) == ("95%", "▼ 1%")
    late = by_label["First action after shift start"]
    assert (late["value"], late["change"]) == ("20 min", "▲ 2 min")
    assert re.fullmatch(r"\d+\.\d\d",by_label["Flags per shift"]["value"]), "flags per shift shows two decimals"
    assert all(t["changeTitle"] == "the 8 weeks before" for t in tiles)
    assert out["line"] == "Team averages for this period · click a panel to see it week by week"


def test_panel_change_classes_follow_better_direction(tmp_path, conn, version):
    """First action after shift start is better when lower: the team's ▲ 2 min is worse, Avery's ▼ 12 is better."""
    out = default_page(tmp_path, """
  var team = metricTiles();
  await pick("tr-tech", "avery@example.com");
  return { team: team, avery: metricTiles() };
""")
    team = {t["label"]: t for t in out["team"]}
    assert "worse" in team["First action after shift start"]["changeCls"].split()
    assert "worse" in team["Tickets per shift"]["changeCls"].split(), "tickets fell, and higher is better"
    avery = {t["label"]: t for t in out["avery"]}
    late = avery["First action after shift start"]
    assert late["change"] == "▼ 12 min" and "better" in late["changeCls"].split()
    tickets = avery["Tickets per shift"]
    assert tickets["change"] == "▲ 1.2" and "better" in tickets["changeCls"].split()


def test_panel_no_change_new_and_dash(tmp_path, conn, version):
    out = default_page(tmp_path, """
  var result = {};
  await pick("tr-tech", "drew@example.com"); result.drew = metricTiles()[0];
  await pick("tr-tech", "casey@example.com"); result.casey = metricTiles()[0];
  await pick("tr-tech", "erin@example.com"); result.erin = metricTiles()[0];
  return result;
""")
    assert (out["drew"]["value"], out["drew"]["change"]) == ("7.0", "no change")
    assert not {"better", "worse"} & set(out["drew"]["changeCls"].split())
    assert (out["casey"]["value"], out["casey"]["change"]) == ("6.0", "new")
    assert (out["erin"]["value"], out["erin"]["change"]) == ("-", "-")


def test_clicking_a_panel_selects_it_and_writes_measure(tmp_path, conn, version):
    out = default_page(tmp_path, """
  var start = metricTiles().map(function (t) { return [t.cls, t.pressed]; });
  fire(tileBtn("Longest gap"), "click");
  await settle();
  var after = metricTiles().map(function (t) { return [t.label, t.cls, t.pressed]; });
  return { start: start, after: after, hash: hashNow(), titles: titles(), tag: tileBtn("Longest gap").tagName };
""")
    assert all(s == ["tile metric-tile", "false"] for s in out["start"]), "no panel is picked at first"
    assert [a for a in out["after"] if a[2] == "true"] == [["Longest gap", "tile metric-tile on", "true"]]
    assert out["hash"] == "#/team/trends?measure=longest_gap_min"
    assert out["titles"] == ["Longest gap by tech", "Longest gap, week by week"]
    assert out["tag"] == "BUTTON"


def test_one_tech_picked_panels_show_that_tech(tmp_path, conn, version):
    out = default_page(tmp_path, """
  await pick("tr-tech", "blake@example.com");
  return { tiles: metricTiles(), line: screenEl.querySelectorAll(".metric-lead")[0].textContent, hash: hashNow() };
""")
    assert out["line"] == "Blake's averages for this period · click a panel to see it week by week"
    tiles = {t["label"]: t for t in out["tiles"]}
    assert (tiles["Tickets per shift"]["value"], tiles["Tickets per shift"]["change"]) == ("4.2", "▼ 2.1")
    assert tiles["Shifts worked"]["value"] == "90%"
    assert out["hash"] == "#/team/trends?tech=blake%40example.com"


def test_compare_none_panels_have_no_change(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?compare=none", {url(compare="none"): trends(comparison=None)},
                      "return metricTiles();")
    assert all(t["change"] is None for t in out)
    assert out[0]["value"] == "6.1"


# -- the part that slides open under the picked panel's row (T-152) ---------------------------------------

# A grid of panels `across` wide (5 from 1400 px, 3 below), laid out as the browser does: the panels fill the rows
# in order, and an open part (grid-column 1 / -1) takes a whole row of its own, so panels after it start a new row.
# The grid is as wide as its columns. A .metric-detail read for its height while shut was laid out shut before it
# opened, so it slides (laidOutShut). The ResizeObserver stand-in keeps what the page watches; `resized()` reports a
# width change to the watchers of the panels' grid.
SLIDE = """
var across = 5;
var partHeight = 0;   // a .metric-detail's height as laid out now (0: shut)
var laidOutShut = [];
function hasClass(el, name) { return Boolean(el && el.classList && el.classList.contains(name)); }
/* Where `el` falls in the panels' grid: { row, col }, or null outside it. */
function gridPlace(el) {
  if (!hasClass(el.parentNode, "metric-tiles")) { return null; }
  var row = 0, col = 0, place = null;
  el.parentNode.children.forEach(function (c) {
    if (hasClass(c, "metric-detail")) {
      if (col > 0) { row += 1; }
      if (c === el) { place = { row: row, col: 0 }; }
      row += 1;
      col = 0;
      return;
    }
    if (col === across) { row += 1; col = 0; }
    if (c === el) { place = { row: row, col: col }; }
    col += 1;
  });
  return place;
}
globalThis.layout = function (el) {
  var place = gridPlace(el) || { row: 0, col: 0 };
  return { clientWidth: hasClass(el, "metric-tiles") ? across * 300 : 0, scrollWidth: 0,
    offsetLeft: place.col * 300,
    get offsetHeight() {
      if (hasClass(el, "metric-detail") && !hasClass(el, "open")) { laidOutShut.push(el); }
      return hasClass(el, "metric-detail") ? partHeight : 0;
    } };
};
var watchers = [];
globalThis.ResizeObserver = function (callback) { this.callback = callback; this.targets = []; this.on = true; watchers.push(this); };
ResizeObserver.prototype.observe = function (el) { this.targets.push(el); };
ResizeObserver.prototype.disconnect = function () { this.on = false; };
function resized() {
  watchers.forEach(function (w) {
    if (w.on && w.targets.some(function (t) { return hasClass(t, "metric-tiles"); })) { w.callback([]); }
  });
}
function grid() { return screenEl.querySelectorAll(".metric-tiles")[0]; }
function panelN(n) { return screenEl.querySelectorAll("button.metric-tile")[n - 1]; }
function parts() { return screenEl.querySelectorAll(".metric-detail"); }
/* Each part under a row: its classes and the panel it follows (1-10; 0 when it follows none). */
function partState() {
  var tiles = screenEl.querySelectorAll("button.metric-tile");
  return parts().map(function (d) {
    var kids = d.parentNode.children;
    return { cls: d.className, after: tiles.indexOf(kids[kids.indexOf(d) - 1]) + 1 };
  });
}
function pressed() {
  return screenEl.querySelectorAll("button.metric-tile").map(function (b, i) {
    return b.getAttribute("aria-pressed") === "true" ? i + 1 : 0;
  }).filter(Boolean);
}
function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
"""
LEAD_LINE = "Team averages for this period · click a panel to see it week by week"


def test_trends_opens_with_the_panels_only(tmp_path, conn, version):
    out = default_page(tmp_path, """
  return { rank: rank() !== null, graph: svg() !== undefined, parts: partState(), pressed: pressed(),
    line: screenEl.querySelectorAll(".metric-lead")[0].textContent, hash: hashNow() };
""", setup=SLIDE)
    assert out == {"rank": False, "graph": False, "parts": [], "pressed": [], "line": LEAD_LINE, "hash": "#/team/trends"}


def test_picking_panels_slides_the_part_open_under_their_rows(tmp_path, conn, version):
    """Five across: panel 1 opens the part after panel 5, panel 3 (the same row) redraws it in place, panel 8 shuts it
    and opens a new one after panel 10, and panel 8 again shuts it. Picking keeps the address but draws nothing again
    and reads nothing."""
    out = default_page(tmp_path, """
  var out = {};
  var startGrid = grid();
  var reads = getRequests().length;
  fire(panelN(1), "click");
  await settle();
  var first = parts()[0];
  out.one = { parts: partState(), slid: laidOutShut.indexOf(first) >= 0, hash: hashNow(), titles: titles(),
    pressed: pressed(), graph: svg() !== undefined };
  fire(panelN(3), "click");
  await settle();
  out.three = { parts: partState(), same: parts()[0] === first, hash: hashNow(), titles: titles(), pressed: pressed() };
  fire(panelN(8), "click");
  await settle();
  out.shutting = { parts: partState(), hash: hashNow(), pressed: pressed() };
  fire(first, "transitionend");
  await settle();
  var second = parts()[0];
  out.eight = { parts: partState(), slid: laidOutShut.indexOf(second) >= 0, gone: !first.isConnected, hash: hashNow(),
    titles: titles() };
  fire(panelN(8), "click");
  await settle();
  out.closing = { parts: partState(), hash: hashNow(), pressed: pressed() };
  await wait(450);
  out.closed = { parts: partState(), hash: hashNow(), rank: rank() !== null };
  out.redrawn = grid() !== startGrid;
  out.reads = getRequests().length - reads;
  return out;
""", setup=SLIDE)
    assert out["one"] == {"parts": [{"cls": "metric-detail open", "after": 5}], "slid": True,
                          "hash": "#/team/trends?measure=tickets_per_shift",
                          "titles": ["Tickets per shift by tech", "Tickets per shift, week by week"], "pressed": [1],
                          "graph": True}
    assert out["three"] == {"parts": [{"cls": "metric-detail open", "after": 5}], "same": True,
                            "hash": "#/team/trends?measure=solved_per_shift",
                            "titles": ["Solved per shift by tech", "Solved per shift, week by week"], "pressed": [3]}
    assert out["shutting"] == {"parts": [{"cls": "metric-detail", "after": 5}],
                               "hash": "#/team/trends?measure=early_finish_min", "pressed": [8]}
    assert out["eight"] == {"parts": [{"cls": "metric-detail open", "after": 10}], "slid": True, "gone": True,
                            "hash": "#/team/trends?measure=early_finish_min",
                            "titles": ["Last action before shift end by tech", "Last action before shift end, week by week"]}
    assert out["closing"] == {"parts": [{"cls": "metric-detail", "after": 10}], "hash": "#/team/trends", "pressed": []}
    assert out["closed"] == {"parts": [], "hash": "#/team/trends", "rank": False}, "shut without a transition's end too"
    assert out["redrawn"] is False and out["reads"] == 0


def test_a_late_transition_end_from_the_opening_slide_does_not_end_the_shut(tmp_path, conn, version):
    """A page whose frames are throttled delivers the opening slide's transitionend late, after a panel in another row
    has started the part shutting: the part is still tall then, so it keeps shutting, and the new one waits."""
    out = default_page(tmp_path, """
  fire(panelN(1), "click");
  await settle();
  var first = parts()[0];
  fire(panelN(8), "click");
  partHeight = 300;
  fire(first, "transitionend");
  await settle();
  var late = { parts: partState(), there: first.isConnected };
  partHeight = 0;
  fire(first, "transitionend");
  await settle();
  return { late: late, shut: partState(), gone: !first.isConnected };
""", setup=SLIDE)
    assert out["late"] == {"parts": [{"cls": "metric-detail", "after": 5}], "there": True}
    assert out["shut"] == [{"cls": "metric-detail open", "after": 10}] and out["gone"] is True


def test_a_measure_in_the_address_opens_under_its_row_without_a_slide(tmp_path, conn, version):
    out = default_page(tmp_path, """
  return { parts: partState(), slid: laidOutShut.length, titles: titles(), pressed: pressed() };
""", start="#/team/trends?measure=solved_per_shift", setup=SLIDE)
    assert out == {"parts": [{"cls": "metric-detail open", "after": 5}], "slid": 0,
                   "titles": ["Solved per shift by tech", "Solved per shift, week by week"], "pressed": [3]}


def test_three_across_the_part_opens_under_panel_3_and_follows_the_width(tmp_path, conn, version):
    """The move waits until after the observer's callback (a change made inside it is a ResizeObserver loop error)."""
    out = default_page(tmp_path, """
  var out = {};
  fire(panelN(1), "click");
  await settle();
  out.three = partState();
  across = 5;
  resized();
  out.during = partState();
  await settle();
  out.five = partState();
  across = 3;
  resized();
  await settle();
  out.back = partState();
  return out;
""", setup=SLIDE.replace("var across = 5;", "var across = 3;"))
    assert out["three"] == [{"cls": "metric-detail open", "after": 3}]
    assert out["during"] == [{"cls": "metric-detail open", "after": 3}], "not moved inside the observer"
    assert out["five"] == [{"cls": "metric-detail open", "after": 5}]
    assert out["back"] == [{"cls": "metric-detail open", "after": 3}]


# -- the ranking table ------------------------------------------------------------------------------------


def test_default_sort_is_biggest_slip_first_with_aria_sort(tmp_path, conn, version):
    out = open_page(tmp_path, """
  return { rows: rankRows(), heads: sortHeads(), sub: subs()[0], cls: rank().className,
    scrolled: rank().parentNode.className, names: rank().querySelectorAll("tbody button").length };
""")
    assert out["cls"].split() == ["atable", "trend-rank"] and out["scrolled"] == "scroll"
    assert [r["name"] for r in out["rows"]] == [
        "Blake Moreno", "Drew Patel", "Avery Cole", "Casey Nguyen", "Erin Shaw", "Team average"]
    assert [r["change"] for r in out["rows"]] == ["▼ 2.1", "no change", "▲ 1.2", "new", "-", "▼ 0.4"]
    assert [r["now"] for r in out["rows"]] == ["4.2", "7.0", "8.2", "6.0", "-", "6.1"]
    assert [h["sort"] for h in out["heads"]] == ["none", "none", "ascending"]
    assert [h["text"] for h in out["heads"]] == ["Tech", "Average", "vs before ▲"]
    assert out["heads"][2]["cls"] == "th-sort on" and out["heads"][0]["cls"] == "th-sort"
    assert out["sub"] == "Biggest slip first" + TAIL
    assert out["names"] == 5, "each tech's name is a button; the Team row has none"
    assert "worse" in out["rows"][0]["changeCls"].split() and "better" in out["rows"][2]["changeCls"].split()


def test_figure_heads_and_graph_subtitle_name_the_days_they_cover(tmp_path, conn, version):
    """The Total/Average column counts only the period (the graph's shaded steps), vs before only the
    comparison, so each head names its days and the graph says what its shading is."""
    out = open_page(tmp_path, "return { heads: sortHeads(), subs: subs() };")
    assert [h["days"] for h in out["heads"]] == [None, "Sep 27 – Oct 10", "Sep 13 – Sep 26"]
    assert out["subs"][1] == ("By week, Sep 13 – Oct 10 · shaded: Last 8 weeks (Aug 16 – Oct 10) · each point pools "
                              "the 4 weeks up to it (all shifts together)")

    without = open_page(tmp_path, "return { heads: sortHeads() };", trends(comparison=None))
    assert [h["days"] for h in without["heads"]] == [None, "Sep 27 – Oct 10"]


def test_sort_cycles_and_subtitles(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var seen = [];
  async function press(text) {
    await act(sortBtn(text));
    seen.push({ names: names().slice(0, 5), sub: subs()[0], heads: sortHeads().map(function (h) { return h.sort; }), hash: hashNow() });
  }
  await press("vs before");   // the active column reverses
  await press("Average");      // another column starts ascending
  await press("Average");
  await press("Tech");
  await press("Tech");
  await press("vs before");
  return seen;
""")
    assert out[0]["names"] == ["Avery Cole", "Drew Patel", "Blake Moreno", "Casey Nguyen", "Erin Shaw"]
    assert out[0]["sub"] == "Biggest improvement first" + TAIL
    assert out[0]["heads"] == ["none", "none", "descending"] and out[0]["hash"] == "#/team/trends?measure=tickets_per_shift&sort=-change"
    assert out[1]["names"] == ["Blake Moreno", "Casey Nguyen", "Drew Patel", "Avery Cole", "Erin Shaw"]
    assert out[1]["sub"] == "Weakest first" + TAIL and out[1]["hash"] == "#/team/trends?measure=tickets_per_shift&sort=now"
    assert out[1]["heads"] == ["none", "ascending", "none"]
    assert out[2]["names"] == ["Avery Cole", "Drew Patel", "Casey Nguyen", "Blake Moreno", "Erin Shaw"]
    assert out[2]["sub"] == "Strongest first" + TAIL and out[2]["hash"] == "#/team/trends?measure=tickets_per_shift&sort=-now"
    assert out[3]["names"] == ["Avery Cole", "Blake Moreno", "Casey Nguyen", "Drew Patel", "Erin Shaw"]
    assert out[3]["sub"] == "By name" + TAIL and out[3]["hash"] == "#/team/trends?measure=tickets_per_shift&sort=tech"
    assert out[4]["names"] == ["Erin Shaw", "Drew Patel", "Casey Nguyen", "Blake Moreno", "Avery Cole"]
    assert out[4]["sub"] == "By name" + TAIL and out[4]["hash"] == "#/team/trends?measure=tickets_per_shift&sort=-tech"
    assert out[5]["hash"] == OPEN, "Change ascending is the default, so it leaves the address"
    assert out[5]["sub"] == "Biggest slip first" + TAIL


def test_value_header_says_average_or_total_by_the_measures_unit(tmp_path, conn, version):
    """Every measure but the two counts shows a per-period average; Sick days and Signed in, no activity a total."""
    out = default_page(tmp_path, """
  var result = {};
  for (var label of ["Tickets per shift", "Shifts worked", "Longest gap", "Flags per shift", "Sick days",
                     "Signed in, no activity"]) {
    await act(tileBtn(label));
    result[label] = sortHeads().map(function (h) { return h.text; });
  }
  return result;
""")
    assert out["Tickets per shift"] == ["Tech", "Average", "vs before ▲"]
    for label in ("Shifts worked", "Longest gap", "Flags per shift"):
        assert out[label] == ["Tech", "Average", "vs before ▲"], label
    for label in ("Sick days", "Signed in, no activity"):
        assert out[label] == ["Tech", "Total", "vs before ▲"], label


def test_value_header_is_total_without_a_comparison_for_a_count(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?compare=none&measure=missed_shifts",
                      {url(compare="none"): trends(comparison=None)}, """
  var heads = sortHeads();
  await act(sortBtn("Total"));
  return { heads: heads.map(function (h) { return h.text; }), sorts: sortHeads().map(function (h) { return h.sort; }),
    hash: hashNow() };
""")
    assert out["heads"] == ["Tech", "Total ▲"]
    assert out["sorts"] == ["none", "descending"] and out["hash"] == "#/team/trends?compare=none&measure=missed_shifts&sort=-now"


def test_lower_is_better_measures_rank_their_highest_as_the_weakest(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?measure=late_start_min&sort=now", {
        url(): trends()}, """
  var weakest = names().slice(0, 5);
  await act(sortBtn("vs before"));
  return { weakest: weakest, byChange: names().slice(0, 5), sub: subs()[0] };
""")
    # 30 min late is the weakest and 12 the strongest; a tech with no value comes last.
    assert out["weakest"] == ["Blake Moreno", "Drew Patel", "Casey Nguyen", "Avery Cole", "Erin Shaw"]
    assert out["byChange"] == ["Blake Moreno", "Drew Patel", "Avery Cole", "Casey Nguyen", "Erin Shaw"]
    assert out["sub"].startswith("Biggest slip first")


def test_missing_values_sort_last_both_ways(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var result = {};
  async function press(text) { await act(sortBtn(text)); return names(); }
  result.changeDesc = await press("vs before");
  result.nowAsc = await press("Average");
  result.nowDesc = await press("Average");
  return result;
""")
    assert out["changeDesc"] == ["Avery Cole", "Drew Patel", "Blake Moreno", "Casey Nguyen", "Erin Shaw", "Team average"]
    assert out["nowAsc"][-2:] == ["Erin Shaw", "Team average"]
    assert out["nowDesc"][-2:] == ["Erin Shaw", "Team average"]


def test_a_tech_new_in_the_period_sorts_last_by_change_in_both_directions(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var asc = names();
  await act(sortBtn("vs before"));
  return { asc: asc, desc: names() };
""")
    assert out["asc"][3:5] == ["Casey Nguyen", "Erin Shaw"] and out["desc"][3:5] == ["Casey Nguyen", "Erin Shaw"]


def test_an_unknown_sort_falls_back_to_the_default_and_an_unknown_measure_opens_nothing(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?measure=tickets_per_shift&sort=sideways", {url(): trends()},
                      "return { names: names(), titles: titles() };")
    assert out["names"][0] == "Blake Moreno" and out["titles"][0] == "Tickets per shift by tech"
    out = trends_page(tmp_path, "#/team/trends?measure=nope", {url(): trends()},
                      "return { rank: rank() !== null, parts: screenEl.querySelectorAll('.metric-detail').length };")
    assert out == {"rank": False, "parts": 0}


def test_compare_none_hides_change_and_sorts_weakest_first(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?compare=none&measure=tickets_per_shift", {url(compare="none"): trends(comparison=None)}, """
  var out = { heads: sortHeads(), rows: rankRows(), sub: subs()[0], hash: hashNow() };
  await act(sortBtn("Average"));
  out.reversed = { sub: subs()[0], hash: hashNow() };
  return out;
""")
    assert [h["text"] for h in out["heads"]] == ["Tech", "Average ▲"]
    assert [h["sort"] for h in out["heads"]] == ["none", "ascending"]
    assert all(r["change"] is None for r in out["rows"])
    assert [r["name"] for r in out["rows"]] == [
        "Blake Moreno", "Casey Nguyen", "Drew Patel", "Avery Cole", "Erin Shaw", "Team average"]
    assert out["sub"] == "Weakest first" + TAIL
    assert out["hash"] == "#/team/trends?compare=none&measure=tickets_per_shift"
    assert out["reversed"] == {"sub": "Strongest first" + TAIL, "hash": "#/team/trends?compare=none&measure=tickets_per_shift&sort=-now"}


def test_sort_change_in_the_address_falls_back_to_now_without_a_comparison(tmp_path, conn, version):
    out = trends_page(tmp_path, "#/team/trends?compare=none&measure=tickets_per_shift&sort=change", {url(compare="none"): trends(comparison=None)},
                      "return { heads: sortHeads().map(function (h) { return h.text; }), sub: subs()[0] };")
    assert out["heads"] == ["Tech", "Average ▲"] and out["sub"].startswith("Weakest first")


# -- the highlight and the graph --------------------------------------------------------------------------


def test_row_click_highlights_that_tech(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var start = lit();
  fire(nameBtn("Drew Patel"), "click");
  await settle();
  return { start: start, lit: lit(), labels: endLabels(), hash: hashNow(), requests: getRequests().length,
    pressed: nameBtn("Drew Patel").getAttribute("aria-pressed") };
""")
    assert out["start"] == ["Blake Moreno"]
    assert out["lit"] == ["Drew Patel"] and "Drew 7.0" in out["labels"]
    assert out["requests"] == 1 and out["hash"] == OPEN, "the highlight is not in the address"
    assert out["pressed"] == "true"


def test_clicking_the_row_outside_the_name_highlights_too(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var row = rank().querySelectorAll("tbody tr")[2];
  fire(row, "click");
  await settle();
  return lit();
""")
    assert out == ["Avery Cole"]


def test_default_highlight_is_picked_tech_else_top_row(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var result = { top: { lit: lit(), labels: endLabels() } };
  await pick("tr-tech", "casey@example.com");
  result.picked = { lit: lit(), labels: endLabels() };
  fire(nameBtn("Avery Cole"), "click");
  await settle();
  result.clicked = endLabels();
  await act(sortBtn("Tech"));
  result.afterSort = endLabels();
  await act(sortBtn("vs before"));
  await pick("tr-tech", "");
  result.cleared = endLabels();
  return result;
""")
    assert out["top"]["lit"] == ["Blake Moreno"] and "Blake 4.4" in out["top"]["labels"]
    assert out["picked"]["lit"] == ["Casey Nguyen"] and "Casey 6.2" in out["picked"]["labels"]
    assert "Avery 8.4" in out["clicked"]
    assert "Avery 8.4" in out["afterSort"], "a clicked tech stays highlighted when the table is re-sorted"
    assert "Avery 8.4" not in out["cleared"] and "Blake 4.4" in out["cleared"], "picking All techs drops the old click"


def test_graph_draws_every_tech_the_team_and_the_chosen_period(tmp_path, conn, version):
    out = open_page(tmp_path, """
  var s = svg();
  return { polys: s.findAll(function (e) { return e.tagName === "POLYLINE"; }).map(function (l) { return l.className; }),
    bands: s.findAll(function (e) { return e.classList.contains("period-band"); }).length, labels: endLabels() };
""")
    # Blake is highlighted: the four other techs are faint lines, then the dashed team, then Blake.
    assert out["polys"] == ["line faint"] * 4 + ["line ref", "line"]
    assert out["bands"] == 1
    assert sorted(out["labels"]) == ["Blake 4.4", "Team 6.0"]


def test_tooltip_names_the_week_the_tech_and_the_team(tmp_path, conn, version):
    out = open_page(tmp_path, "return tooltips();")
    assert out == [step_card("Sep 13", "Sep 13–19", "Blake", "6.0", "6.0"),
                   step_card("Sep 20", "Sep 20–26", "Blake", "6.6", "6.4"),
                   step_card("Sep 27", "Sep 27–Oct 3", "Blake", "4.0", "6.2"),
                   step_card("Oct 4", "Oct 4–10", "Blake", "4.4", "6.0")]


def test_tooltip_for_part_weeks_uses_each_steps_own_days(tmp_path, conn, version):
    """A week split at the period's edge makes two steps with the same label: the days tell them apart."""
    steps = [
        {"start": "2026-10-25", "end": "2026-10-31", "label": "Oct 25", "in_period": True, "part": False},
        {"start": "2026-11-01", "end": "2026-11-04", "label": "Nov 1", "in_period": True, "part": True},
        {"start": "2026-11-05", "end": "2026-11-07", "label": "Nov 1", "in_period": True, "part": True},
        {"start": "2026-11-08", "end": "2026-11-08", "label": "Nov 8", "in_period": True, "part": True},
    ]
    out = open_page(tmp_path, "return tooltips();", trends(steps=steps))
    assert out[0] == step_card("Oct 25", "Oct 25–31", "Blake", "6.0", "6.0")
    assert out[1] == step_card("Nov 1", "Nov 1–4", "Blake", "6.6", "6.4", part=True)
    assert out[2] == step_card("Nov 1", "Nov 5–7", "Blake", "4.0", "6.2", part=True)
    assert out[3] == step_card("Nov 8", "Nov 8", "Blake", "4.4", "6.0", part=True)
    crossing = [
        {"start": "2026-10-28", "end": "2026-11-03", "label": "Oct 25", "in_period": True, "part": True}] + steps[:3]
    out = open_page(tmp_path, "return tooltips()[0];", trends(steps=crossing))
    assert out == step_card("Oct 25", "Oct 28–Nov 3", "Blake", "6.0", "6.0", part=True)


def test_months_step_by_month(tmp_path, conn, version):
    steps = [
        {"start": "2026-08-30", "end": "2026-08-31", "label": "Aug", "in_period": False, "part": True},
        {"start": "2026-09-01", "end": "2026-09-30", "label": "Sep", "in_period": True, "part": False},
        {"start": "2026-10-01", "end": "2026-10-31", "label": "Oct", "in_period": True, "part": False},
        {"start": "2026-11-01", "end": "2026-11-02", "label": "Nov", "in_period": True, "part": True},
    ]
    out = open_page(tmp_path, "return { tips: tooltips(), titles: titles() };", trends(unit="month", steps=steps))
    # Each point pools its month and the one before; the month's own figure is beside it.
    assert out["tips"] == [
        step_card("Aug", "Aug 30–31", "Blake", "6.0", "6.0", window=2, unit="month", part=True),
        step_card("Sep", "Sep 1–30", "Blake", "6.6", "6.4", window=2, unit="month"),
        step_card("Oct", "Oct 1–31", "Blake", "4.0", "6.2", window=2, unit="month"),
        step_card("Nov", "Nov 1–2", "Blake", "4.4", "6.0", window=2, unit="month", part=True)]
    assert out["titles"][1] == "Tickets per shift, month by month"


def test_tooltip_dash_for_a_missing_value_and_units_on_percent_and_minutes(tmp_path, conn, version):
    out = open_page(tmp_path, """
  await pick("tr-tech", "casey@example.com");
  var tickets = tooltips();
  await act(tileBtn("Shifts worked"));
  var pct = tooltips();
  await act(tileBtn("First action after shift start"));
  return { tickets: tickets, pct: pct, minutes: tooltips() };
""")
    assert out["tickets"][0] == step_card("Sep 13", "Sep 13–19", "Casey", "-", "6.0")
    assert out["tickets"][2] == step_card("Sep 27", "Sep 27–Oct 3", "Casey", "5.8", "6.2")
    assert out["pct"][2] == step_card("Sep 27", "Sep 27–Oct 3", "Casey", "100%", "95%")
    assert out["minutes"][2] == step_card("Sep 27", "Sep 27–Oct 3", "Casey", "15 min", "20 min")


def test_count_measure_graph_starts_at_zero_with_whole_ticks(tmp_path, conn, version):
    counts = {"team": figures(0.5, 0.25, [0, 0.5, 0.5, 0.5], 0),
              "techs": [figures(1, 0, [0, 0, 1, 1], 0), figures(0, 0, [0, 0, 0, 0], 0),
                        figures(0, None, [None, None, 0, 0], 0), figures(2, 1, [1, 1, 2, 2], 0),
                        figures(None, 0, [0, 0, None, None], 0)]}
    out = trends_page(tmp_path, "#/team/trends?measure=missed_shifts",
                      {url(): trends(overrides={"missed_shifts": counts})}, "return axisTicks();")
    assert min(out) == 0 and max(out) >= 3 and all(float(t).is_integer() for t in out)


def test_team_count_shows_one_decimal_and_tech_counts_stay_whole(tmp_path, conn, version):
    """Ruling 38: two of six techs missing one shift each is a team average of 0.3, not "0, no change". The team's
    figures use `team_digits`; the techs' stay whole and the axis keeps whole ticks."""
    missed = {"team": figures(0.3, 0, [0, 0, 0, 0.3], 1),
              "techs": [figures(1, 0, [0, 0, 0, 1], 0), figures(1, 0, [0, 0, 0, 1], 0),
                        figures(0, 0, [0, 0, 0, 0], 0), figures(0, 0, [0, 0, 0, 0], 0),
                        figures(0, 0, [0, 0, 0, 0], 0)]}
    out = trends_page(tmp_path, "#/team/trends?measure=missed_shifts",
                      {url(): trends(overrides={"missed_shifts": missed})}, """
  var tile = metricTiles().filter(function (t) { return t.label === "Sick days"; })[0];
  return { tile: tile, rows: rankRows(), tips: tooltips(), labels: endLabels(), ticks: axisTicks() };
""")
    assert (out["tile"]["value"], out["tile"]["change"]) == ("0.3", "▲ 0.3")
    rows = {r["name"]: r for r in out["rows"]}
    assert (rows["Team average"]["now"], rows["Team average"]["change"]) == ("0.3", "▲ 0.3")
    assert (rows["Avery Cole"]["now"], rows["Avery Cole"]["change"]) == ("1", "▲ 1")
    assert rows["Casey Nguyen"]["now"] == "0"
    assert out["tips"][3] == step_card("Oct 4", "Oct 4–10", "Avery", "1", "0.3")
    assert out["tips"][0] == step_card("Sep 13", "Sep 13–19", "Avery", "0", "0.0")
    assert "Team 0.3" in out["labels"] and "Avery 1" in out["labels"]
    assert min(out["ticks"]) == 0 and all(float(t).is_integer() for t in out["ticks"])


# Avg. call time (T-162): a "duration" measure in seconds, lower is better.
CALL_TIME = {
    "team": figures(125, 140, [150, 140, 130, 125], 0),
    "techs": [figures(95, 110, [110, 110, 100, 95], 0),           # Avery: 15 s shorter, better
              figures(190, 150, [150, 150, 180, 190], 0),         # Blake: 40 s longer, worse
              figures(125, None, [None, None, 120, 125], 0),      # Casey: new
              figures(130, 130, [130] * 4, 0),                    # Drew: no change
              figures(None, 140, [140, 140, None, None], 0)],     # Erin: away all period
}


def with_call_time() -> dict:
    """The answer with Avg. call time after the ten measures, as trends.trends_view gives it."""
    body = trends()
    body["measures"].append({"key": "avg_call_time", "label": "Avg. call time", "group": "Calls", "unit": "duration",
                             "digits": 0, "team_digits": 0, "better": "lower", "window": {"count": 4, "unit": "week"}, "base": "calls",
                             **copy.deepcopy(CALL_TIME)})
    return body


def test_avg_call_time_reads_as_minutes_and_seconds(tmp_path, conn, version):
    """The seconds of Avg. call time read as m:ss on the panel, in the ranking, in the change (▼ is better: lower
    is better), in the tooltips, on the end labels and on the graph's axis, which names no unit."""
    out = trends_page(tmp_path, "#/team/trends?measure=avg_call_time", {url(): with_call_time()}, """
  var tile = metricTiles().filter(function (t) { return t.label === "Avg. call time"; })[0];
  var ticks = svg().findAll(function (e) {
    return e.tagName === "TEXT" && e.classList.contains("tick") && e.getAttribute("text-anchor") === "end";
  }).map(function (t) { return t.textContent; });
  return { tile: tile, rows: rankRows(), tips: tooltips(), labels: endLabels(), ticks: ticks,
    legend: screenEl.querySelectorAll(".chart-legend")[0].textContent };
""")
    assert (out["tile"]["value"], out["tile"]["change"]) == ("2:05", "▼ 0:15")
    assert "better" in out["tile"]["changeCls"].split()
    rows = {r["name"]: r for r in out["rows"]}
    assert (rows["Team average"]["now"], rows["Team average"]["change"]) == ("2:05", "▼ 0:15")
    assert "better" in rows["Team average"]["changeCls"].split()
    assert (rows["Avery Cole"]["now"], rows["Avery Cole"]["change"]) == ("1:35", "▼ 0:15")
    assert (rows["Blake Moreno"]["now"], rows["Blake Moreno"]["change"]) == ("3:10", "▲ 0:40")
    assert "worse" in rows["Blake Moreno"]["changeCls"].split()
    assert (rows["Casey Nguyen"]["change"], rows["Drew Patel"]["change"]) == ("new", "no change")
    assert (rows["Erin Shaw"]["now"], rows["Erin Shaw"]["change"]) == ("-", "-")
    assert out["tips"][0] == step_card("Sep 13", "Sep 13–19", "Blake", "2:30", "2:30", base="calls")
    assert out["tips"][3] == step_card("Oct 4", "Oct 4–10", "Blake", "3:10", "2:05", base="calls")
    assert sorted(out["labels"]) == ["Blake 3:10", "Team 2:05"]
    assert out["ticks"] == ["1:00", "2:00", "3:00", "4:00"], "whole minutes: 1:35 to 3:10, with room"
    assert "minutes" not in out["legend"]


def test_the_line_is_the_trailing_average_with_each_weeks_figure_as_a_dot(tmp_path, conn, version):
    """The owner, 2026-10-22: every tech's line, and the team's, is their trailing pooled average (`rolling`); the
    highlighted tech's own weekly figures are solid dots behind their line, and their end label is the line's. The
    legend names the window and the dots, and the subtitle says what each point pools."""
    blake = figures(4.2, 6.3, [6.0, 6.6, 4.0, 4.4], rolling=[6.2, 6.3, 5.6, 5.3])
    team = figures(6.1, 6.5, [6.0, 6.4, 6.2, 6.0], rolling=[6.3, 6.3, 6.2, 6.1])
    tickets = {"team": team, "techs": [TICKETS["techs"][0], blake, *TICKETS["techs"][2:]]}
    out = trends_page(tmp_path, "#/team/trends?measure=tickets_per_shift",
                      {url(): trends(overrides={"tickets_per_shift": tickets})}, """
  var dots = svg().findAll(function (e) { return e.tagName === "CIRCLE" && e.classList.contains("raw"); });
  return { dots: dots.length, labels: endLabels(), legend: screenEl.querySelectorAll(".chart-legend")[0].textContent,
    sub: subs()[1], lines: svg().findAll(function (e) { return e.tagName === "POLYLINE"; }).map(function (l) {
      return l.className; }) };
""")
    assert out["dots"] == 4, "one dot for each of Blake's weeks"
    assert sorted(out["labels"]) == ["Blake 5.3", "Team 6.1"], "the line's own last points, not week Oct 4's 4.4"
    assert out["legend"].split() == ["Blake,", "4-week", "average", "each", "week", "Team", "average", "Other", "techs"]
    assert out["sub"].endswith(" · each point pools the 4 weeks up to it (all shifts together)")
    assert out["lines"] == ["line faint"] * 4 + ["line ref", "line"], "every line solid, none dotted"


def test_a_totals_line_keeps_a_decimal(tmp_path, conn, version):
    """Sick days is a total: its line averages each week's total over the window, so a tech's line keeps one decimal
    (`line_digits`) while each week's own figure, the panel and the ranking stay whole."""
    sick = {"team": figures(0.3, 0, [0, 0, 1, 0], 1, rolling=[0.0, 0.0, 0.3, 0.3]),
            "techs": [figures(1, 0, [0, 0, 1, 0], 0, rolling=[0.0, 0.0, 0.3, 0.3]),
                      *[figures(0, 0, [0] * 4, 0) for _ in range(4)]]}
    body = trends(overrides={"missed_shifts": sick})
    for m in body["measures"]:
        m["line_digits"] = 1 if m["key"] == "missed_shifts" else m["digits"]
    out = trends_page(tmp_path, "#/team/trends?measure=missed_shifts&tech=avery@example.com", {url(): body},
                      "return { labels: endLabels(), tips: tooltips() };")
    assert "Avery 0.3" in out["labels"]
    assert out["tips"][2] == step_card("Sep 27", "Sep 27–Oct 3", "Avery", "0.3", "0.3", own="1")


def test_a_monthly_satisfaction_line_pools_two_months(tmp_path, conn, version):
    """The owner, 2026-10-22: on a monthly graph Satisfaction's line pools each month and the one before; the legend,
    the subtitle and the tooltip say so, and the highlighted tech's own months are the dots."""
    steps = [{"start": "2026-07-01", "end": "2026-07-31", "label": "Jul", "in_period": True, "part": False},
             {"start": "2026-08-01", "end": "2026-08-31", "label": "Aug", "in_period": True, "part": False},
             {"start": "2026-09-01", "end": "2026-09-30", "label": "Sep", "in_period": True, "part": False},
             {"start": "2026-10-01", "end": "2026-10-31", "label": "Oct", "in_period": True, "part": False}]
    sat = {"team": figures(80, None, [82, 78, 80, 81], 0),
           "techs": [figures(90, None, [100, None, 80, 90], 0, rolling=[90, 100, 80, 85]),
                     figures(60, None, [60, 50, None, 70], 0, rolling=[62, 55, 50, 70]),
                     figures(80, None, [80] * 4, 0), figures(85, None, [85] * 4, 0), figures(70, None, [70] * 4, 0)]}
    body = trends(unit="month", steps=steps, comparison=None)
    body["measures"].append({"key": "satisfaction", "label": "Satisfaction", "group": "Customers", "unit": "percent",
                             "digits": 0, "team_digits": 0, "better": "higher",
                             "window": {"count": 2, "unit": "month"}, "base": "ratings", **sat})
    out = trends_page(tmp_path, "#/team/trends?measure=satisfaction", {url(): body}, """
  return { tips: tooltips(), sub: subs()[1], legend: screenEl.querySelectorAll(".chart-legend")[0].textContent,
    dots: svg().findAll(function (e) { return e.tagName === "CIRCLE" && e.classList.contains("raw"); }).length };
""")
    assert out["sub"].endswith(" · each point pools the 2 months up to it (all ratings together)")
    assert out["legend"].split()[:5] == ["Blake,", "2-month", "average", "each", "month"]
    assert out["tips"][2] == card("Month of Sep", "Sep 1–30",
                                  ("Blake, 2-month average", "50%", "12 ratings · likely 25–75%"),
                                  ("That month alone", "-", ""),
                                  ("Team, 2-month average", "80%", "12 ratings · likely 52–94%"),
                                  foot="The line pools every rating in the 2 months up to each point.")
    assert out["dots"] == 3, "Blake's three months with ratings"


def test_satisfaction_is_drawn_from_0_to_100_percent(tmp_path, conn, version):
    """The owner, 2026-10-22: Satisfaction's graph always runs 0% to 100%, even when every figure is high; the
    other percents are fitted to their figures."""
    high = {"team": figures(92, 90, [90, 95, 92, 94], 0),
            "techs": [figures(100, 100, [100] * 4, 0), figures(90, 85, [85, 90, 88, 92], 0),
                      figures(95, None, [None, None, 95, 95], 0), figures(88, 88, [88] * 4, 0),
                      figures(None, 90, [90, 90, None, None], 0)]}
    body = trends()
    body["measures"].append({"key": "satisfaction", "label": "Satisfaction", "group": "Customers", "unit": "percent",
                             "digits": 0, "team_digits": 0, "better": "higher", "window": {"count": 8, "unit": "week"}, "base": "ratings",
                             **high})
    out = trends_page(tmp_path, "#/team/trends?measure=satisfaction", {url(): body}, """
  var sat = axisTicks();
  await act(tileBtn("Shifts worked"));
  return { sat: sat, shifts: axisTicks(), sub: subs()[1] };
""")
    assert (min(out["sat"]), max(out["sat"])) == (0, 100)
    assert min(out["shifts"]) > 0, "Shifts worked stays fitted to its figures"


def call_time_ticks(tmp_path, low: int, high: int) -> list[str]:
    """The axis of Avg. call time's graph when every figure lies from `low` to `high` seconds."""
    middle = (low + high) // 2
    spread = {"team": figures(middle, middle, [low, middle, middle, high], 0),
              "techs": [figures(middle, middle, [low, middle, middle, high], 0) for _ in range(5)]}
    body = trends()
    body["measures"].append({"key": "avg_call_time", "label": "Avg. call time", "group": "Calls", "unit": "duration",
                             "digits": 0, "team_digits": 0, "better": "lower", "window": {"count": 4, "unit": "week"}, "base": "calls",
                             **spread})
    out = trends_page(tmp_path, "#/team/trends?measure=avg_call_time", {url(): body}, """
  return svg().findAll(function (e) {
    return e.tagName === "TEXT" && e.classList.contains("tick") && e.getAttribute("text-anchor") === "end";
  }).map(function (t) { return t.textContent; });
""")
    return out


@pytest.mark.parametrize("low, high, ticks", [
    (120, 150, ["1:30", "2:00", "2:30", "3:00"]),                     # whole minutes would leave 2 minutes: every 30 s
    (20, 40, ["0:00", "0:30", "1:00"]),
    (600, 1500, ["5:00", "10:00", "15:00", "20:00", "25:00", "30:00"]),   # 5-minute steps
], ids=["half minutes", "under a minute", "five minutes"])
def test_call_time_axis_steps_on_whole_minutes(tmp_path, conn, version, low, high, ticks):
    """A duration's axis steps on whole minutes (1, 2, 5, 10 ...), or every 30 seconds when whole minutes would leave
    it under 3 minutes tall."""
    assert call_time_ticks(tmp_path, low, high) == ticks


# -- states -----------------------------------------------------------------------------------------------


def test_closing_note_text(tmp_path, conn, version):
    out = default_page(tmp_path, "var n = screenEl.querySelectorAll('p.note'); return n[n.length - 1].textContent;")
    assert out == CLOSING


def test_no_agent_wording_on_trends(tmp_path, conn, version):
    out = default_page(tmp_path, """
  var text = screenEl.textContent + " " + titleEl.textContent;
  screenEl.findAll(function (e) { return e.getAttribute("title") || e.getAttribute("aria-label"); }).forEach(function (e) {
    text += " " + (e.getAttribute("title") || "") + " " + (e.getAttribute("aria-label") || "");
  });
  return text;
""")
    assert not re.search(r"\bagents?\b", out, re.IGNORECASE)
    assert "All techs" in out


def test_a_bad_request_shows_the_message_and_a_way_back(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url(period="custom", start="2026-10-10", end="2026-10-01")] = answer(
        {"detail": "The start day must be on or before the end day."}, 400)
    out = run_page("#/team/trends?period=custom&start=2026-10-10&end=2026-10-01", responses, READ + """
run(async function () {
  var alerts = screenEl.findAll(function (e) { return e.getAttribute("role") === "alert"; });
  return { alert: alerts.map(function (a) { return a.textContent; }), links: links(screenEl) };
});
""")
    assert out["alert"] == ["The start day must be on or before the end day."]
    assert out["links"] == [{"text": "Show the last 8 weeks", "href": "#/team/trends"}]


def test_returning_to_trends_draws_the_kept_answer_and_reads_it_again(tmp_path, conn, version):
    out = default_page(tmp_path, """
  await go("#/settings");
  await go("#/team/trends");
  return { gets: getRequests().length, head: head() };
""")
    assert out == {"gets": 2, "head": "Last 8 weeks (Aug 16 – Oct 10) vs the 8 weeks before · 5 techs"}


# -- styles -----------------------------------------------------------------------------------------------


def test_trends_styles_use_tokens_only_and_stack_below_1024():
    css = app_css()
    for selector in ("button.tile.metric-tile", "button.tile.metric-tile.on", ".metric-tiles", ".trends-row",
                     "button.th-sort", "button.rank-name", ".metric-lead"):
        assert rules(css, selector), selector + " is styled"
    # The highlighted ranking row is the shared selected row (`.atable tr.sel`), not a copy of its style.
    assert rules(css, ".atable tr.sel td")
    assert "tr.on" not in (STATIC / "app.css").read_text(encoding="utf-8")
    wide = top_level(css)
    assert values(wide, ".metric-tiles", "grid-template-columns") == ["repeat(5, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 1399px)"), ".metric-tiles", "grid-template-columns") == [
        "repeat(3, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 639px)"), ".metric-tiles", "grid-template-columns") == ["minmax(0, 1fr)"]
    assert values(media_block(css, "(max-width: 1023px)"), ".trends-row", "grid-template-columns") == ["minmax(0, 1fr)"]
    text = (STATIC / "app.css").read_text(encoding="utf-8")
    section = text[text.index("/* -- trends"):text.index("/* -- end trends */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", section)


def test_the_part_under_a_row_slides_for_280ms_and_adds_no_space_while_shut():
    """The panels' rows are spaced by each panel's foot margin, not a row gap, so a shut part (no height) adds none;
    it slides on --slide (TREND_SLIDE_MS in app.js), and not at all for reduced motion."""
    css = app_css()
    assert tokens(css)["slide"] == "280ms"
    assert "var TREND_SLIDE_MS = 280;" in (STATIC / "app.js").read_text(encoding="utf-8")
    wide = top_level(css)
    assert values(wide, ".metric-tiles", "column-gap") == ["var(--gap-panel)"]
    assert values(wide, ".metric-tiles", "gap") == [] and values(wide, ".metric-tiles", "row-gap") == []
    assert values(wide, ".metric-tiles", "margin-bottom") == []
    assert values(wide, ".metric-tiles > .metric-tile", "margin-bottom") == ["var(--gap-panel)"]
    assert values(wide, ".metric-detail", "grid-column") == ["1 / -1"]
    assert values(wide, ".metric-detail", "grid-template-rows") == ["0fr"]
    assert values(wide, ".metric-detail", "transition") == [
        "grid-template-rows var(--slide) ease, opacity var(--slide) ease"]
    assert values(wide, ".metric-detail.open", "grid-template-rows") == ["1fr"]
    assert values(wide, ".metric-detail-in", "overflow") == ["hidden"]
    assert values(wide, ".metric-detail-in", "min-height") == ["0"]
    assert values(media_block(css, "(prefers-reduced-motion: reduce)"), ".metric-detail", "transition") == ["none"]
