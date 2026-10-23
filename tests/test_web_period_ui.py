"""The period picker, the comparison control and the trend shading on the Team overview and Agents views
(phase 5 spec §6, Task 6). The whole page (app.js) runs in node on the fake DOM, with `fetch` answering from
what the real endpoints return for synthetic data. Today is pinned to Sat Oct 17, 2026 (tests/conftest.py).
"""
from __future__ import annotations

from urllib.parse import quote

from test_compare import api_for
from test_queries import LEE, SAM, add_calls, add_review, all_met, version  # noqa: F401  (fixture)
from test_web_overview import seeded  # noqa: F401  (fixture)
from test_web_test_view import page_responses, run_page

PRESETS = ["Yesterday", "This week (so far)", "Last week", "This month (so far)", "Last month", "Last 8 weeks",
           "Last 6 months", "Last year", "Custom range"]
COMPARES = ["The period before", "Same days last week", "Custom range", "Nothing"]
SAM_PATH = "/api/agents/" + quote(SAM[0], safe="")
LEE_PATH = "/api/agents/" + quote(LEE[0], safe="")

# Helpers to read and drive the filters, the period header, the tiles, the links and the trend charts.
PICKER_JS = """
function byId(id) { return document.getElementById(id); }
function picked(id) { var el = byId(id); return el ? el.value : null; }
function optionsOf(id) { return byTag(byId(id), "OPTION").map(function (o) { return o.textContent; }); }
function attrs(id, names) {
  var el = byId(id);
  return names.map(function (n) { return el.getAttribute(n); });
}
function choose(id, value) { var el = byId(id); el.value = value; fire(el, "change"); }
function classed(el, name) { return all(el, function (e) { return e.classList.contains(name); }); }
function headText() { var el = classed(screenEl, "period-head")[0]; return el ? el.textContent : null; }
function messages() {
  return all(screenEl, function (e) { return (e.className || "").split(" ")[0] === "message"; })
    .map(function (e) { return e.textContent; }).filter(Boolean);
}
function tiles() {
  return all(screenEl, function (e) { return e.className === "tile"; }).map(function (t) {
    return t.children.map(function (c) { return c.textContent; });
  });
}
function placeholders() {
  return all(screenEl, function (e) { return (e.className || "").indexOf("placeholder") === 0; })
    .map(function (e) { return e.textContent; });
}
function hrefs(prefix) {
  return all(screenEl, function (e) { return e.tagName === "A"; }).map(function (a) {
    return a.getAttribute("href") || a.href || "";
  }).filter(function (h) { return h.indexOf(prefix) === 0 && h.indexOf("ticket=") < 0; });
}
function apiUrls(prefix) {
  return requests.map(function (r) { return r.url; }).filter(function (u) { return u.indexOf(prefix) === 0; });
}
function view(p) {
  return { period: picked(p + "-period"), start: picked(p + "-start"), end: picked(p + "-end"),
           compare: picked(p + "-compare"), compare_start: picked(p + "-cstart"), compare_end: picked(p + "-cend"),
           head: headText(), hash: location.hash };
}
function trendCharts() {
  return all(screenEl, function (e) {
    return e.tagName === "SVG" && e.classList.contains("chart") && !e.classList.contains("spark") &&
      !e.classList.contains("hbar") && !e.classList.contains("passbar");
  }).map(function (svg) {
    return {
      bands: classed(svg, "period-band").map(function (b) {
        return [Number(b.getAttribute("x")), Number(b.getAttribute("width"))];
      }),
      width: Number(svg.getAttribute("width")),
      labels: all(svg, function (e) {
        return e.tagName === "TEXT" && e.getAttribute("class") === "tick" && e.getAttribute("text-anchor") === "middle";
      }).map(function (t) { return t.textContent; }),
      tips: all(svg, function (e) { return e.getAttribute("class") === "hit"; }).map(function (g) {
        return byTag(g, "DESC")[0].textContent.split(String.fromCharCode(10))[0];
      })
    };
  });
}
"""


def page(api, start_hash: str, paths, scenario: str):
    """Run app.js at `start_hash`, `fetch` answering what the real API answers for each of `paths`."""
    return run_page(start_hash, page_responses(api, *paths), PICKER_JS + scenario)


def test_picker_presets_and_custom(seeded, tmp_path):
    """One period select with the exact preset copy; Custom range shows a start and an end date, a single
    day is allowed (start = end), and a bad range says why without leaving the view."""
    api = api_for(tmp_path)
    custom = "/api/overview?period=custom&start=2026-10-04&end=2026-10-10"
    single = "/api/overview?period=custom&start=2026-10-04&end=2026-10-04"
    out = page(api, "#/overview", ["/api/overview", custom, single], """
run(async function () {
  var out = { options: optionsOf("ov-period"), first: view("ov") };
  choose("ov-period", "custom");
  out.hash = location.hash;
  await go(location.hash);
  out.custom = view("ov");
  out.limits = attrs("ov-start", ["type", "min", "max"]).concat(attrs("ov-end", ["type", "min", "max"]));
  choose("ov-end", "2026-10-04");
  await go(location.hash);
  out.single = view("ov");
  out.singleEmpty = placeholders();
  var before = location.hash;
  choose("ov-start", "2026-10-09");
  out.backwards = [messages(), location.hash === before];
  choose("ov-start", "1999-12-31");
  out.tooEarly = [messages(), location.hash === before];
  out.requests = apiUrls("/api/overview");
  return out;
});
""")
    assert out["options"] == PRESETS
    assert out["first"]["period"] == "last_week" and out["first"]["start"] is None   # no dates unless custom
    assert out["first"]["head"] == "Last week (Oct 4 – Oct 10) · 2 calls vs Sep 27 – Oct 3, 2026 · 1 call"
    # Custom range starts on the period shown, and the dates go in the address.
    assert out["hash"] == "#/overview?period=custom&start=2026-10-04&end=2026-10-10"
    assert out["custom"]["period"] == "custom"
    assert (out["custom"]["start"], out["custom"]["end"]) == ("2026-10-04", "2026-10-10")
    assert out["custom"]["head"] == "Oct 4 – Oct 10, 2026 · 2 calls vs Sep 27 – Oct 3, 2026 · 1 call"
    assert out["limits"] == ["date", "2000-01-01", "2099-12-31"] * 2
    # One day: start = end. Sun Oct 4 had no calls.
    assert out["single"]["hash"] == "#/overview?period=custom&start=2026-10-04&end=2026-10-04"
    assert out["single"]["head"] == "Sun, Oct 4, 2026 · 0 calls vs Sat, Oct 3, 2026 · 0 calls"
    assert "No calls in this period" in out["singleEmpty"]
    assert out["backwards"] == [["The start day must be on or before the end day."], True]
    assert out["tooEarly"] == [["Choose dates between 2000 and 2099."], True]
    assert out["requests"] == ["/api/overview", custom, single]


def test_compare_options(seeded, tmp_path):
    """"Same days last week" is offered only for periods of 7 days or less, and falls back to the period
    before when the period grows past that; Custom range shows two dates; Nothing shows no comparison."""
    api = api_for(tmp_path)
    same = "/api/overview?compare=same_days_last_week"
    eight = "/api/overview?period=last_8_weeks"
    custom = "/api/overview?period=last_8_weeks&compare=custom&compare_start=2026-06-21&compare_end=2026-08-15"
    nothing = "/api/overview?period=last_8_weeks&compare=none"
    out = page(api, "#/overview", ["/api/overview", same, eight, custom, nothing], """
run(async function () {
  var out = { week: [optionsOf("ov-compare"), picked("ov-compare"), picked("ov-cstart")] };
  choose("ov-compare", "same_days_last_week");
  await go(location.hash);
  out.same = [view("ov"), tiles()[0]];
  choose("ov-period", "last_8_weeks");
  out.grown = location.hash;
  await go(location.hash);
  out.eight = [optionsOf("ov-compare"), picked("ov-compare")];
  choose("ov-compare", "custom");
  out.customHash = location.hash;
  await go(location.hash);
  out.custom = view("ov");
  choose("ov-compare", "none");
  await go(location.hash);
  out.nothing = [view("ov"), tiles()];
  return out;
});
""")
    assert out["week"] == [COMPARES, "previous", None]
    same_view, avg_tile = out["same"]
    assert same_view["hash"] == "#/overview?compare=same_days_last_week"
    assert same_view["compare"] == "same_days_last_week"
    assert avg_tile[2].endswith(" vs same days last week")
    # Last 8 weeks is 56 days: Same days last week falls back to the period before, and isn't offered.
    assert out["grown"] == "#/overview?period=last_8_weeks"
    assert out["eight"] == [["The period before", "Custom range", "Nothing"], "previous"]
    # A custom comparison starts on the comparison shown, with two dates.
    assert out["customHash"] == "#" + custom.replace("/api/", "/")
    assert out["custom"]["compare"] == "custom"
    assert (out["custom"]["compare_start"], out["custom"]["compare_end"]) == ("2026-06-21", "2026-08-15")
    assert out["custom"]["head"] == "Last 8 weeks (Aug 16 – Oct 10) · 4 calls vs Jun 21 – Aug 15, 2026 · 0 calls"
    nothing_view, nothing_tiles = out["nothing"]
    assert nothing_view["compare"] == "none" and nothing_view["compare_start"] is None
    assert nothing_view["head"] == "Last 8 weeks (Aug 16 – Oct 10) · 4 calls"
    assert all(len(t) == 2 for t in nothing_tiles), "Nothing: no comparison line on any tile"


def test_same_days_bookmark_for_a_long_period_falls_back(seeded, tmp_path):
    """A bookmark asking for Same days last week on a period longer than 7 days (the API refuses it) opens
    the period against the period before, replacing the address rather than adding a history entry."""
    api = api_for(tmp_path)
    refused = "/api/overview?period=last_month&compare=same_days_last_week"
    out = page(api, "#/overview?period=last_month&compare=same_days_last_week",
               [refused, "/api/overview?period=last_month"], """
run(async function () {
  return [view("ov"), messages(), apiUrls("/api/overview")];
});
""")
    shown, notes, urls = out
    assert api.get(refused).status_code == 400
    assert shown["hash"] == "#/overview?period=last_month" and shown["compare"] == "previous"
    assert shown["head"] == "Last month (September 2026) · 2 calls vs Aug 1 – Aug 31, 2026 · 0 calls"
    assert notes == [] and urls == [refused, "/api/overview?period=last_month"]


def test_hash_state_round_trip(seeded, tmp_path):
    """The period, the comparison and the agent filter live in the address with the API's own names, so a
    reload and the back button bring back the exact view."""
    api = api_for(tmp_path)
    start = ("#/overview?period=custom&start=2026-09-28&end=2026-10-07&compare=custom&compare_start=2026-09-18"
             "&compare_end=2026-09-27&agent=sam%40example.com")
    yesterday = ("#/overview?period=yesterday&compare=custom&compare_start=2026-09-18&compare_end=2026-09-27"
                 "&agent=sam%40example.com")
    paths = ["/api/overview" + start[len("#/overview"):], "/api/overview" + yesterday[len("#/overview"):]]
    scenario = """
run(async function () {
  var out = { first: view("ov"), agent: picked("ov-agent") };
  choose("ov-period", "yesterday");
  out.moved = location.hash;
  await go(location.hash);
  out.yesterday = view("ov");
  await go(START_HASH);     // the back button: the browser goes back to the address before
  out.back = view("ov");
  out.requests = apiUrls("/api/overview");
  return out;
});
"""
    out = page(api, start, paths, scenario)
    first = out["first"]
    assert first == {"period": "custom", "start": "2026-09-28", "end": "2026-10-07", "compare": "custom",
                     "compare_start": "2026-09-18", "compare_end": "2026-09-27",
                     "head": "Sep 28 – Oct 7, 2026 · 2 calls vs Sep 18 – Sep 27, 2026 · 0 calls", "hash": start}
    assert out["agent"] == SAM[0]
    assert out["moved"] == yesterday
    assert out["yesterday"]["period"] == "yesterday" and out["yesterday"]["compare"] == "custom"
    # Nothing is reviewed after Oct 10 here, so Yesterday waits for the morning run (final review fix 1).
    assert out["yesterday"]["head"] == "Yesterday (nothing reviewed yet) vs Sep 18 – Sep 27, 2026 · 0 calls"
    assert out["back"] == first
    assert out["requests"] == paths + paths[:1]
    # A reload opens the same address the same way.
    reloaded = page(api, start, paths, """
run(async function () { return [view("ov"), picked("ov-agent")]; });
""")
    assert reloaded == [first, SAM[0]]


def test_legacy_hash_routes(seeded, tmp_path):
    """Review Focus 5: old bookmarks open the same data. `#/overview?week=…&compare=…` is that week against
    that week, and `#/agents/<email>?weeks=8&week=…` is those 8 weeks. The picker shows them as custom
    ranges, and the address is left as it was until the owner changes something."""
    api = api_for(tmp_path)
    overview = "/api/overview?compare=2026-09-16&week=2026-09-30"
    out = page(api, "#/overview?week=2026-09-30&compare=2026-09-16", [overview], """
run(async function () { return [view("ov"), tiles(), apiUrls("/api/overview")]; });
""")
    shown, shown_tiles, urls = out
    assert urls == [overview]
    assert shown["hash"] == "#/overview?week=2026-09-30&compare=2026-09-16"
    assert (shown["period"], shown["start"], shown["end"]) == ("custom", "2026-09-27", "2026-10-03")
    assert (shown["compare"], shown["compare_start"], shown["compare_end"]) == ("custom", "2026-09-13", "2026-09-19")
    assert shown["head"] == "Week of Sep 27 · 1 call vs Week of Sep 13 · 0 calls"
    week = api.get(overview).json()["tiles"]["problems"]
    assert shown_tiles[1][1] == f"{week['count']} · {week['share_pct']:g}% of calls"
    assert shown_tiles[1][2] == "Nothing to compare yet"      # nothing was reviewed in the week of Sep 13

    agent = SAM_PATH + "?weeks=8&week=2026-10-01"
    responses = page_responses(api, "/api/agents", agent)
    out = run_page("#/agents/" + quote(SAM[0], safe="") + "?weeks=8&week=2026-10-01", responses, PICKER_JS + """
run(async function () { return [view("ag"), apiUrls("/api/agents/"), hrefs("#/agents/")]; });
""")
    shown, urls, pills = out
    assert urls == [agent]
    assert (shown["period"], shown["start"], shown["end"]) == ("custom", "2026-08-09", "2026-10-03")
    assert shown["head"] == "Aug 9 – Oct 3, 2026 · 1 call by Sam Agent"
    assert shown["compare"] is None, "the Agents view has no compare control"
    assert all(p.endswith("?weeks=8&week=2026-10-01") for p in pills), "the pills keep the old period"


def test_agent_link_carries_period(seeded, tmp_path):
    """Overview → agent keeps the period (the overview's default, Last week, explicitly, since the Agents
    default is Last 8 weeks); the agent pills keep it; agent → Tickets carries its days."""
    api = api_for(tmp_path)
    days = "/api/overview?period=custom&start=2026-10-05&end=2026-10-06"
    out = page(api, "#/overview", ["/api/overview", days, "/api/overview?week=2026-09-30"], """
run(async function () {
  var out = { lastWeek: hrefs("#/agents/") };
  await go("#/overview?period=custom&start=2026-10-05&end=2026-10-06");
  out.custom = hrefs("#/agents/");
  await go("#/overview?week=2026-09-30");
  out.legacy = hrefs("#/agents/");
  return out;
});
""")
    sam, lee = "#/agents/" + quote(SAM[0], safe=""), "#/agents/" + quote(LEE[0], safe="")
    assert sorted(out["lastWeek"]) == [lee + "?period=last_week", sam + "?period=last_week"]
    assert sorted(out["custom"]) == [lee + "?period=custom&start=2026-10-05&end=2026-10-06",
                                     sam + "?period=custom&start=2026-10-05&end=2026-10-06"]
    assert out["legacy"] == [sam + "?period=custom&start=2026-09-27&end=2026-10-03"]

    # Sam missed the 10-minute and scope rules on Sep 28.
    query = "?period=custom&start=2026-09-27&end=2026-10-03"
    responses = page_responses(api, "/api/agents", SAM_PATH + query)
    out = run_page(sam + query, responses, PICKER_JS + """
run(async function () { return [view("ag"), hrefs("#/agents/"), hrefs("#/tickets?")]; });
""")
    shown, pills, tickets = out
    assert shown["period"] == "custom" and shown["head"] == "Sep 27 – Oct 3, 2026 · 1 call by Sam Agent"
    assert len(pills) == 2 and all(p.endswith(query) for p in pills)
    week = "start=2026-09-27&end=2026-10-03&agent=sam%40example.com"
    assert sorted(tickets) == sorted(["#/tickets?verdict=problem&" + week,
                                      "#/tickets?missed_check=pr-call-length&" + week,
                                      "#/tickets?missed_check=pr-scope&" + week])


def test_tile_share_and_count_text(conn, version, tmp_path):
    """Spec 6.2: count tiles read "4 · 20% of calls", the arrow follows the change in share, and the change
    in count sits beside it. The labels are short; the period is named once, in the header above the tiles,
    and each tile's "vs" names the comparison briefly."""
    add_calls(conn, version, 1, 20, 4, "2026-10-05T15:00:00Z")      # Last week: 4 problems in 20 calls
    add_calls(conn, version, 101, 10, 3, "2026-09-28T15:00:00Z")    # the week before: 3 in 10
    api = api_for(tmp_path)
    custom = "/api/overview?compare=custom&compare_start=2026-09-27&compare_end=2026-10-03"
    empty = "/api/overview?compare=custom&compare_start=2026-06-01&compare_end=2026-06-07"
    out = page(api, "#/overview", ["/api/overview", custom, empty, "/api/overview?compare=none"], """
run(async function () {
  var out = { head: headText(), tiles: tiles() };
  await go("#/overview?compare=custom&compare_start=2026-09-27&compare_end=2026-10-03");
  out.custom = [headText(), tiles()[1][2]];
  await go("#/overview?compare=custom&compare_start=2026-06-01&compare_end=2026-06-07");
  out.empty = [headText(), tiles().map(function (t) { return t[2]; })];
  await go("#/overview?compare=none");
  out.none = [headText(), tiles()];
  return out;
});
""")
    assert out["head"] == "Last week (Oct 4 – Oct 10) · 20 calls vs Sep 27 – Oct 3, 2026 · 10 calls"
    assert out["tiles"] == [
        ["Avg score", "99", "▲ 0.5 vs the period before"],
        ["Problem tickets", "4 · 20% of calls", "▼ 10 pts (+1 calls) vs the period before"],
        ["10-minute misses", "0 · 0% of calls", "No change vs the period before"],
        ["Scope violations", "0 · 0% of calls", "No change vs the period before"]]
    assert out["custom"] == ["Last week (Oct 4 – Oct 10) · 20 calls vs Sep 27 – Oct 3, 2026 · 10 calls",
                             "▼ 10 pts (+1 calls) vs Sep 27 – Oct 3, 2026"]
    assert out["empty"] == ["Last week (Oct 4 – Oct 10) · 20 calls vs Jun 1 – Jun 7, 2026 · 0 calls",
                            ["Nothing to compare yet"] * 4]
    assert out["none"] == ["Last week (Oct 4 – Oct 10) · 20 calls",
                           [["Avg score", "99"], ["Problem tickets", "4 · 20% of calls"],
                            ["10-minute misses", "0 · 0% of calls"], ["Scope violations", "0 · 0% of calls"]]]


def test_empty_period_and_comparison_copy(seeded, tmp_path):
    """Review Focus 2 on the page, with final review fix 1: a period the runs haven't reached yet (Yesterday
    before the morning run, This week so far on a Sunday) says "Nothing reviewed yet for this period", with
    or without an agent; a reviewed period without calls says "No calls in this period" (naming the agent
    when filtered); an empty comparison says "Nothing to compare yet"; no link into Tickets is offered."""
    api = api_for(tmp_path)
    week = "/api/overview?period=this_week"
    sam_week = week + "&agent=" + quote(SAM[0], safe="")
    yesterday = "/api/overview?period=yesterday"
    quiet = "/api/overview?period=custom&start=2026-10-07&end=2026-10-09"     # reviewed (to Oct 10), no calls
    sam_quiet = quiet + "&agent=" + quote(SAM[0], safe="")
    out = page(api, "#/overview?period=this_week", [week, sam_week, yesterday, quiet, sam_quiet], """
run(async function () {
  var out = { team: [headText(), placeholders(), hrefs("#/tickets?"), tiles()] };
  await go("#/overview?period=this_week&agent=sam%40example.com");
  out.sam = placeholders();
  await go("#/overview?period=yesterday");
  out.yesterday = [headText(), placeholders()];
  await go("#/overview?period=custom&start=2026-10-07&end=2026-10-09");
  out.quiet = [headText(), placeholders()];
  await go("#/overview?period=custom&start=2026-10-07&end=2026-10-09&agent=sam%40example.com");
  out.samQuiet = placeholders();
  return out;
});
""")
    head, notes, links, empty_tiles = out["team"]
    assert head == "This week so far (nothing reviewed yet) · Nothing to compare yet"
    assert "Nothing reviewed yet for this period" in notes and links == [] and empty_tiles == []
    assert "Nothing reviewed yet for this period" in out["sam"]
    assert out["yesterday"][0] == "Yesterday (nothing reviewed yet) · Nothing to compare yet"
    assert "Nothing reviewed yet for this period" in out["yesterday"][1]
    assert out["quiet"][0] == "Oct 7 – Oct 9, 2026 · 0 calls vs Oct 4 – Oct 6, 2026 · 2 calls"
    assert "No calls in this period" in out["quiet"][1]
    assert "No calls from Sam Agent in this period" in out["samQuiet"]
    assert not any("No calls" in text for text in notes + out["sam"] + out["yesterday"][1])

    # An agent page over an empty period has no Tickets links (they would end before they start), and
    # says nothing is reviewed yet rather than that there were no calls.
    responses = page_responses(api, "/api/agents", SAM_PATH + "?period=this_week")
    out = run_page("#/agents/" + quote(SAM[0], safe="") + "?period=this_week", responses, PICKER_JS + """
run(async function () { return [headText(), hrefs("#/tickets?"), placeholders(), tiles()]; });
""")
    assert out[0] == "This week so far (nothing reviewed yet)"
    assert out[1] == [] and "Nothing reviewed yet for this period" in out[2]
    assert [t[2] for t in out[3][1:3]] == ["Nothing reviewed yet for this period"] * 2
    assert not any("No calls" in text for text in out[2])

    # Lee had no calls on Mon Sep 28, Sam had one: the pass-rate table says so above the team's rates.
    query = "?period=custom&start=2026-09-28&end=2026-09-28"
    responses = page_responses(api, "/api/agents", LEE_PATH + query)
    out = run_page("#/agents/" + quote(LEE[0], safe="") + query, responses, PICKER_JS + """
run(async function () {
  var p = panels().filter(function (s) { return titleOf(s) === "Pass rate by check"; })[0];
  return [classed(p, "period-note").map(function (e) { return e.textContent; }), byTag(p, "TABLE").length,
          hrefs("#/tickets?"), tiles().map(function (t) { return t[0]; })];
});
""")
    assert out[0] == ["No calls from Lee Agent in this period"] and out[1] == 1 and out[2] == []
    assert out[3] == ["Avg score", "Problem rate", "Tickets", "Biggest gap vs team"]


def test_chart_shades_period_and_labels_steps(seeded, tmp_path):
    """Spec 6.3: the chosen period is shaded when the chart also shows context steps; steps are labelled
    by their unit ("Oct 14", "Oct 11" for a week's Sunday, "Sep" or "Jan 2026"), and tooltips name the step
    ("Tue, Oct 14", "Week of Oct 11", "September 2026"), with "(part)" on a clipped edge step ("(so far)"
    is test_so_far_label's). A run covered through Oct 16, so Yesterday is reviewed."""
    seeded.execute("UPDATE runs SET window_end = '2026-10-17T05:00:00Z'")
    seeded.commit()
    api = api_for(tmp_path)
    yesterday = "/api/overview?period=yesterday"
    eight = "/api/overview?period=last_8_weeks"
    months = "/api/overview?period=custom&start=2026-01-01&end=2026-09-30"
    clipped = "/api/overview?period=custom&start=2026-09-02&end=2026-10-06"
    out = page(api, "#/overview?period=yesterday", [yesterday, eight, months, clipped], """
run(async function () {
  var out = { yesterday: trendCharts() };
  await go("#/overview?period=last_8_weeks");
  out.eight = trendCharts();
  await go("#/overview?period=custom&start=2026-01-01&end=2026-09-30");
  out.months = trendCharts();
  await go("#/overview?period=custom&start=2026-09-02&end=2026-10-06");
  out.clipped = trendCharts();
  return out;
});
""")
    # Yesterday: 14 days, only Oct 16 in the period, shaded on both charts at the right end.
    score, rate = out["yesterday"]
    for chart in (score, rate):
        assert len(chart["bands"]) == 1
        x, width = chart["bands"][0]
        assert width > 0 and x > chart["width"] / 2
    assert rate["labels"][-1] == "Oct 16" and "Oct 14" in rate["labels"]
    assert rate["tips"][-1] == "Fri, Oct 16: no reviewed calls"
    assert rate["tips"][2:4] == ["Mon, Oct 5: 0% of tickets had a problem", "Tue, Oct 6: 100% of tickets had a problem"]
    # Last 8 weeks: every step is the period, so nothing is shaded; weeks are labelled by their Sunday.
    score, rate = out["eight"]
    assert score["bands"] == [] and rate["bands"] == []
    assert rate["labels"][-1] == "Oct 4" and rate["tips"][-1].startswith("Week of Oct 4:")
    # Months: the first label and each January carry the year.
    rate = out["months"][1]
    assert rate["labels"][0] == "Jan 2026" and "Sep" in rate["labels"]
    assert rate["tips"][-1].startswith("September 2026:")
    # A 35-day custom range by week: the context weeks are unshaded and the clipped edge weeks say "(part)".
    rate = out["clipped"][1]
    assert len(rate["bands"]) == 1
    tips = rate["tips"]
    assert tips[0].startswith("Week of Aug 16:") and tips[2].startswith("Week of Aug 30 (part):")
    assert tips[-1].startswith("Week of Oct 4 (part):") and tips[3].startswith("Week of Sep 6:")


def test_so_far_label(seeded, version, tmp_path):
    """A period that reaches today is labelled "so far" up to the last reviewed day (here Tue Oct 13), and a
    trend step the runs haven't finished says "(so far)", even when it is also cut back to that day."""
    add_review(seeded, version, 9, all_met(version), agent=LEE, call="2026-10-13T15:00:00Z")
    api = api_for(tmp_path)
    week = "/api/overview?period=this_week"
    eight = "/api/overview?period=last_8_weeks"
    out = page(api, "#/overview?period=this_week", [week, eight], """
run(async function () {
  var out = { head: headText(), option: byTag(byId("ov-period"), "OPTION").filter(function (o) {
    return o.getAttribute("value") === picked("ov-period"); })[0].textContent };
  await go("#/overview?period=last_8_weeks");
  out.eight = [headText(), trendCharts()[1].tips.slice(-1)[0]];
  return out;
});
""")
    assert out["head"] == "This week so far (Sun–Tue) · 1 call vs Oct 4 – Oct 6, 2026 · 2 calls"
    assert out["option"] == "This week (so far)"
    assert out["eight"][0].startswith("Last 8 weeks so far (Aug 23 – Oct 13)")
    assert out["eight"][1].startswith("Week of Oct 11 (so far):")
    assert "(part)" not in out["eight"][1]


# -- fix round 1: trend subtitles name the steps shown ------------------------------------------------------

SUBTITLES_JS = """
function subtitles() {
  var out = {};
  panels().forEach(function (p) {
    var sub = p.children.filter(function (c) { return c.className === "sub"; })[0];
    out[titleOf(p)] = sub ? sub.textContent : null;
  });
  return out;
}
"""


def test_trend_subtitles_name_the_steps_shown(seeded, tmp_path):
    """A trend's subtitle says how it steps and which days its steps cover, never "Last 14 days": a past
    custom range's chart doesn't end today. The shaded period is named when context steps are shown."""
    api = api_for(tmp_path)
    past = "/api/overview?period=custom&start=2026-09-21&end=2026-09-22"
    eight = "/api/overview?period=last_8_weeks"
    months = "/api/overview?period=custom&start=2026-01-01&end=2026-09-30"
    lee = past + "&agent=" + quote(LEE[0], safe="")     # Lee's Sep 21 call is in these days, Sam had none
    out = page(api, "#/overview?period=custom&start=2026-09-21&end=2026-09-22", [past, eight, months, lee],
               SUBTITLES_JS + """
run(async function () {
  var out = { past: subtitles() };
  await go("#/overview?period=custom&start=2026-09-21&end=2026-09-22&agent=lee%40example.com");
  out.lee = subtitles();
  await go("#/overview?period=last_8_weeks");
  out.eight = subtitles();
  await go("#/overview?period=custom&start=2026-01-01&end=2026-09-30");
  out.months = subtitles();
  return out;
});
""")
    shaded = " · shaded: Sep 21 – Sep 22, 2026"
    assert out["past"]["Team average score"] == "By day, Sep 9 – Sep 22, out of 100" + shaded
    assert out["past"]["Share of tickets with a problem"] == "By day, Sep 9 – Sep 22" + shaded
    assert out["lee"]["Average score"] == "Lee Agent. By day, Sep 9 – Sep 22, out of 100" + shaded
    assert out["eight"]["Team average score"] == "By week, Aug 16 – Oct 10, out of 100"
    assert out["months"]["Share of tickets with a problem"] == "By month, Jan – Sep 2026"

    # The Agents chart, and spans that cross a year.
    def agent_sub(query):
        responses = page_responses(api, "/api/agents", SAM_PATH + query)
        return run_page("#/agents/" + quote(SAM[0], safe="") + query, responses, PICKER_JS + SUBTITLES_JS + """
run(async function () { return subtitles()["Score vs team average"]; });
""")

    assert agent_sub("?period=custom&start=2026-09-21&end=2026-09-22") == \
        "By day, Sep 9 – Sep 22, out of 100" + shaded
    assert agent_sub("") == "By week, Aug 16 – Oct 10, out of 100"
    assert agent_sub("?period=custom&start=2025-12-20&end=2026-01-05") == \
        "By day, Dec 20, 2025 – Jan 5, 2026, out of 100"
    assert agent_sub("?period=custom&start=2025-11-01&end=2026-09-30") == "By month, Nov 2025 – Sep 2026, out of 100"


# -- fix round 2: the sparkline column names its steps ------------------------------------------------------

SPARK_JS = """
run(async function () {
  var table = all(screenEl, function (e) { return e.tagName === "TABLE" && e.classList.contains("atable"); })[0];
  var head = all(table, function (e) { return e.tagName === "TH"; }).map(function (th) { return th.textContent; });
  var arias = all(table, function (e) { return e.tagName === "SVG" && e.classList.contains("spark"); })
    .map(function (svg) { return svg.getAttribute("aria-label"); });
  return [head.filter(function (t) { return t.indexOf("Score") === 0; }), arias];
});
"""


def test_sparkline_header_names_its_steps(seeded, tmp_path):
    """The Agents table's sparkline column says how it steps, and each sparkline's description names the
    days it covers, so a past range never reads as "last 14 days"."""
    api = api_for(tmp_path)
    past = "/api/overview?period=custom&start=2026-09-21&end=2026-09-22"
    head, arias = page(api, "#/overview?period=custom&start=2026-09-21&end=2026-09-22", [past], SPARK_JS)
    assert head == ["Score by day"]
    assert arias == ["Lee Agent score by day, Sep 9 – Sep 22: " + ", ".join(["-"] * 12 + ["100", "-"])]
    head, arias = page(api, "#/overview?period=last_8_weeks", ["/api/overview?period=last_8_weeks"], SPARK_JS)
    assert head == ["Score by week"]
    assert all(a.split(":")[0].endswith(" score by week, Aug 16 – Oct 10") for a in arias) and len(arias) == 2


# -- final review ---------------------------------------------------------------------------------------


def test_period_heading_counts_reviewed_calls(conn, version, tmp_path):
    """Final review fix 2 and 11: the heading names how many calls were reviewed, in the period and in its
    comparison, and on Agents whose calls they are; the tiles keep their shares. It is a heading (h3, above
    the panels' h4), so a screen reader can find the period."""
    add_calls(conn, version, 1, 30, 4, "2026-10-05T15:00:00Z")      # Last week: 30 calls by Sam
    add_calls(conn, version, 101, 33, 3, "2026-09-28T15:00:00Z")    # the week before: 33
    api = api_for(tmp_path)
    out = page(api, "#/overview", ["/api/overview"], """
run(async function () {
  var head = classed(screenEl, "period-head")[0];
  return [head.tagName, head.textContent, tiles()[1][1]];
});
""")
    assert out == ["H3", "Last week (Oct 4 – Oct 10) · 30 calls vs Sep 27 – Oct 3, 2026 · 33 calls",
                   "4 · 13.3% of calls"]
    responses = page_responses(api, "/api/agents", SAM_PATH + "?period=last_week")
    out = run_page("#/agents/" + quote(SAM[0], safe="") + "?period=last_week", responses, PICKER_JS + """
run(async function () { var head = classed(screenEl, "period-head")[0]; return [head.tagName, head.textContent]; });
""")
    assert out == ["H3", "Last week (Oct 4 – Oct 10) · 30 calls by Sam Agent"]


def test_agents_period_picker_moves_the_page(seeded, tmp_path):
    """Leftover 8: on Agents, choosing a period drops an old address's `weeks` and `week`, Last 8 weeks (the
    default) stays out of the address, and a custom range's dates move the page."""
    api = api_for(tmp_path)
    sam = "#/agents/" + quote(SAM[0], safe="")
    legacy = SAM_PATH + "?weeks=8&week=2026-10-01"
    custom = SAM_PATH + "?period=custom&start=2026-09-01&end=2026-10-03"
    responses = page_responses(api, "/api/agents", legacy, SAM_PATH + "?period=last_week", SAM_PATH, custom)
    out = run_page(sam + "?weeks=8&week=2026-10-01", responses, PICKER_JS + """
run(async function () {
  var out = { legacy: view("ag") };
  choose("ag-period", "last_week");
  out.lastWeek = location.hash;
  await go(location.hash);
  out.lastWeekView = view("ag");
  choose("ag-period", "last_8_weeks");
  out.eight = location.hash;
  await go(START_HASH);
  choose("ag-period", "last_8_weeks");
  out.legacyToEight = location.hash;
  await go(START_HASH);
  choose("ag-start", "2026-09-01");
  out.customHash = location.hash;
  await go(location.hash);
  out.custom = view("ag");
  out.requests = apiUrls("/api/agents/");
  return out;
});
""")
    assert (out["legacy"]["period"], out["legacy"]["start"], out["legacy"]["end"]) == ("custom", "2026-08-09",
                                                                                      "2026-10-03")
    assert out["lastWeek"] == sam + "?period=last_week"
    assert out["lastWeekView"]["period"] == "last_week"
    assert out["eight"] == sam and out["legacyToEight"] == sam
    assert out["customHash"] == sam + "?period=custom&start=2026-09-01&end=2026-10-03"
    assert (out["custom"]["period"], out["custom"]["start"], out["custom"]["end"]) == ("custom", "2026-09-01",
                                                                                      "2026-10-03")
    assert out["custom"]["head"] == "Sep 1 – Oct 3, 2026 · 1 call by Sam Agent"
    assert out["requests"] == [legacy, SAM_PATH + "?period=last_week", legacy, legacy, custom]


def test_agents_refused_old_address_shows_a_custom_range(seeded, tmp_path):
    """Leftover 12: an old address the API refuses (weeks=99) shows the picker on Custom range with blank
    dates, never Last 8 weeks, so choosing Last 8 weeks still moves the page."""
    api = api_for(tmp_path)
    sam = "#/agents/" + quote(SAM[0], safe="")
    refused = SAM_PATH + "?weeks=99"
    responses = page_responses(api, "/api/agents", refused, SAM_PATH)
    out = run_page(sam + "?weeks=99", responses, PICKER_JS + """
run(async function () {
  var out = { shown: view("ag"), notes: messages() };
  choose("ag-period", "last_8_weeks");
  out.moved = location.hash;
  await go(location.hash);
  out.eight = view("ag");
  return out;
});
""")
    assert api.get(refused).status_code == 400
    assert (out["shown"]["period"], out["shown"]["start"], out["shown"]["end"]) == ("custom", "", "")
    assert out["notes"] == ["The weeks must be a whole number from 1 to 52."]
    assert out["moved"] == sam
    assert out["eight"]["period"] == "last_8_weeks" and out["eight"]["head"].startswith("Last 8 weeks (Aug 16")


def test_first_change_drops_old_overview_keys(seeded, tmp_path):
    """Leftover 9: the owner's first change drops an old address's `week` and dated `compare`. A new period
    replaces both (comparing with the period before); any other change keeps the days shown, as custom
    ranges, so the view doesn't jump."""
    api = api_for(tmp_path)
    start = "#/overview?week=2026-09-30&compare=2026-09-16"
    kept = ("?period=custom&start=2026-09-27&end=2026-10-03&compare=custom&compare_start=2026-09-13"
            "&compare_end=2026-09-19&agent=sam%40example.com")
    paths = ["/api/overview?compare=2026-09-16&week=2026-09-30", "/api/overview?period=last_month",
             "/api/overview" + kept]
    out = page(api, start, paths, """
run(async function () {
  var out = { first: view("ov") };
  choose("ov-period", "last_month");
  out.period = location.hash;
  await go(START_HASH);
  choose("ov-agent", "sam@example.com");
  out.agent = location.hash;
  await go(location.hash);
  out.kept = view("ov");
  await go(START_HASH);
  choose("ov-compare", "none");
  out.compare = location.hash;
  return out;
});
""")
    assert out["first"]["head"] == "Week of Sep 27 · 1 call vs Week of Sep 13 · 0 calls"
    assert out["period"] == "#/overview?period=last_month"
    assert out["agent"] == "#/overview" + kept
    assert out["kept"]["head"] == "Sep 27 – Oct 3, 2026 · 1 call vs Sep 13 – Sep 19, 2026 · 0 calls"
    assert out["compare"] == "#/overview?period=custom&start=2026-09-27&end=2026-10-03&compare=none"


def test_only_the_same_days_refusal_falls_back(seeded, tmp_path):
    """Leftover 10: an address with Same days last week that the API refuses for another reason (here a
    backwards custom range) shows that reason and stays, rather than retrying with the period before."""
    api = api_for(tmp_path)
    refused = "/api/overview?period=custom&start=2026-10-09&end=2026-10-05&compare=same_days_last_week"
    out = page(api, "#/" + refused[len("/api/"):], [refused], """
run(async function () { return [location.hash, messages(), apiUrls("/api/overview")]; });
""")
    assert api.get(refused).json()["detail"] == "The start day must be on or before the end day."
    assert out == ["#/" + refused[len("/api/"):], ["The start day must be on or before the end day."], [refused]]


STEPPER_JS = """
function stepper(prefix) {
  var wrap = classed(screenEl, "stepper")[0];
  var earlier = byId(prefix + "-earlier"), later = byId(prefix + "-later");
  return { tag: wrap ? wrap.tagName : null, inPicker: !!wrap && wrap.parentNode.classList.contains("period-pick"),
           earlier: earlier ? [earlier.textContent, earlier.className, !!earlier.disabled] : null,
           later: later ? [later.textContent, later.className, !!later.disabled] : null };
}
"""


def test_earlier_steps_last_week_back_seven_days(seeded, tmp_path):
    """Earlier shows the 7 days before Oct 11 - Oct 17 as a custom range, in the address."""
    api = api_for(tmp_path)
    shown = "/api/overview?period=custom&start=2026-10-11&end=2026-10-17"
    back = "/api/overview?period=custom&start=2026-10-04&end=2026-10-10"
    out = page(api, "#/overview?period=custom&start=2026-10-11&end=2026-10-17", [shown, back], STEPPER_JS + """
run(async function () {
  var out = { before: stepper("ov") };
  fire(byId("ov-earlier"), "click");
  out.hash = location.hash;
  await go(location.hash);
  out.after = view("ov");
  return out;
});
""")
    assert out["before"] == {"tag": "SPAN", "inPicker": True, "earlier": ["◀ Earlier", "small", False],
                             "later": ["Later ▶", "small", False]}
    assert out["hash"] == "#/overview?period=custom&start=2026-10-04&end=2026-10-10"
    assert (out["after"]["period"], out["after"]["start"], out["after"]["end"]) == ("custom", "2026-10-04", "2026-10-10")


def test_later_steps_a_month_by_its_length(seeded, tmp_path):
    """Later moves September 1 - 30 on by its 30 days, to October 1 - 30."""
    api = api_for(tmp_path)
    sept = "/api/overview?period=custom&start=2026-09-01&end=2026-09-30"
    octo = "/api/overview?period=custom&start=2026-10-01&end=2026-10-30"
    out = page(api, "#/overview?period=custom&start=2026-09-01&end=2026-09-30", [sept, octo], """
run(async function () {
  fire(byId("ov-later"), "click");
  var hash = location.hash;
  await go(hash);
  return [hash, view("ov")];
});
""")
    hash_, after = out
    assert hash_ == "#/overview?period=custom&start=2026-10-01&end=2026-10-30"
    assert (after["start"], after["end"]) == ("2026-10-01", "2026-10-30")


def test_overview_and_techs_screens_show_stepper(seeded, tmp_path):
    """The stepper is part of the shared picker: the Team overview and a tech's screen both have it, on a
    preset period as well as a custom one."""
    api = api_for(tmp_path)
    overview = page(api, "#/overview", ["/api/overview"],
                    STEPPER_JS + "run(async function () { return stepper('ov'); });")
    responses = page_responses(api, "/api/agents", SAM_PATH)
    tech = run_page("#/agents/" + quote(SAM[0], safe=""), responses,
                    PICKER_JS + STEPPER_JS + "run(async function () { return stepper('ag'); });")
    for out in (overview, tech):
        assert out["tag"] == "SPAN" and out["inPicker"]
        assert out["earlier"][:2] == ["◀ Earlier", "small"] and out["later"][:2] == ["Later ▶", "small"]


def test_stepper_buttons_disable_at_the_limits(seeded, tmp_path):
    """Later is disabled when the shown period ends today or later (nothing later to show) and at the last
    day the picker takes; Earlier is disabled when the step would start before the first day."""
    api = api_for(tmp_path)
    future = "/api/overview?period=custom&start=2090-01-01&end=2090-01-07"
    first = "/api/overview?period=custom&start=2000-01-03&end=2000-01-09"
    first_day = "/api/overview?period=custom&start=2000-01-01&end=2000-01-01"
    last = "/api/overview?period=custom&start=2099-12-25&end=2099-12-31"
    out = page(api, "#/overview?period=custom&start=2090-01-01&end=2090-01-07", [future, first, first_day, last],
               STEPPER_JS + """
run(async function () {
  var out = { future: stepper("ov") };
  await go("#/overview?period=custom&start=2000-01-03&end=2000-01-09");
  out.first = stepper("ov");
  await go("#/overview?period=custom&start=2000-01-01&end=2000-01-01");
  out.firstDay = stepper("ov");
  await go("#/overview?period=custom&start=2099-12-25&end=2099-12-31");
  out.last = stepper("ov");
  return out;
});
""")
    assert out["future"]["later"][2] is True and out["future"]["earlier"][2] is False
    assert out["first"]["earlier"][2] is True and out["first"]["later"][2] is False   # 1999-12-27 is before 2000
    assert out["firstDay"]["earlier"][2] is True
    assert out["last"]["later"][2] is True and out["last"]["earlier"][2] is False


def test_earlier_from_a_so_far_week_steps_a_whole_week(seeded, tmp_path):
    """This week (so far) is cut short at the last day reviewed, but Earlier steps back the whole week."""
    api = api_for(tmp_path)
    this_week = "/api/overview?period=this_week"
    back = "/api/overview?period=custom&start=2026-10-04&end=2026-10-10"
    assert api.get(this_week).json()["period"]["so_far"] is True
    out = page(api, "#/overview?period=this_week", [this_week, back], """
run(async function () { fire(byId("ov-earlier"), "click"); return location.hash; });
""")
    assert out == "#/overview?period=custom&start=2026-10-04&end=2026-10-10"
