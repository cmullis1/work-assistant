"""1:1 prep's small trend chart, Charts.trendLine (1:1 prep schedules design §8): the tech's line and the team's
dashed line with gaps where a value is missing, min and max ticks, the first, middle and last 1:1 days, and a dot per
1:1 titled with its value and the team's. Runs charts.js in node on the fake DOM."""
from __future__ import annotations

from test_web_overview import node_prelude, run_node

SETUP_JS = """
globalThis.layout = function () { return { clientWidth: 480, scrollWidth: 480 }; };
function byClass(root, cls) { return root.findAll(function (e) { return e.classList.contains(cls); }); }
function lines(svg) { return svg.findAll(function (e) { return e.tagName === "POLYLINE"; }); }
function texts(svg, cls) {
  return svg.findAll(function (e) { return e.tagName === "TEXT" && e.classList.contains(cls); });
}
function yTicks(svg) {
  return texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end" && Number(t.getAttribute("x")) < 60; });
}
function xTicks(svg) { return texts(svg, "tick").filter(function (t) { return !yTicks(svg).includes(t); }); }
function titleOf(g) { var t = g.findAll(function (e) { return e.tagName === "DESC"; })[0]; return t ? t.textContent : null; }
function draw(spec) {
  var el = document.createElement("div");
  Charts.trendLine(el, spec);
  return el.children[0];
}
var DAYS = ["2026-08-25", "2026-09-08", "2026-09-22", "2026-10-06", "2026-10-20"];
"""


def run(script: str):
    return run_node(node_prelude() + SETUP_JS + script)


def test_trend_line_draws_gaps_and_ticks():
    out = run("""
var svg = draw({ values: [5, null, 7, 8, 6], team: [6, 6.5, null, 7, 7], days: DAYS, unit: "count" });
var dots = byClass(svg, "dot").filter(function (d) { return !d.classList.contains("ref"); });
process.stdout.write(JSON.stringify({
  width: [svg.getAttribute("width"), svg.getAttribute("viewBox")],
  classes: lines(svg).map(function (l) { return l.className; }),
  points: lines(svg).map(function (l) { return l.getAttribute("points").split(" ").length; }),
  dots: dots.length,
  titles: dots.map(function (d) { return titleOf(d.parentNode); }),
  y: yTicks(svg).map(function (t) { return t.textContent; }),
  x: xTicks(svg).map(function (t) { return [t.textContent, t.getAttribute("text-anchor")]; })
}));
""")
    assert out["width"][0] == "480" and out["width"][1].startswith("0 0 480 "), "drawn at the real pixel width"
    # The team's two runs dashed, then the tech's one run after the gap; the tech's lone first point is a dot only.
    assert out["classes"] == ["line ref", "line ref", "line"]
    assert out["points"] == [2, 2, 3]
    assert out["dots"] == 4, "a dot per 1:1 with a value"
    assert out["titles"] == ["1:1 8/25: 5 (team 6)", "1:1 9/22: 7 (team -)", "1:1 10/6: 8 (team 7)",
                             "1:1 10/20: 6 (team 7)"]
    assert len(out["y"]) == 2 and out["y"][0] == "0", "min and max ticks only; a count starts at 0"
    assert int(out["y"][1]) >= 8
    assert out["x"] == [["8/25", "start"], ["9/22", "middle"], ["10/20", "end"]], "first, middle and last 1:1"


def test_trend_line_caps_percent_at_100():
    out = run("""
function read(spec) {
  var svg = draw(spec);
  var dots = byClass(svg, "dot").filter(function (d) { return !d.classList.contains("ref"); });
  return { y: yTicks(svg).map(function (t) { return [t.textContent, Number(t.getAttribute("y"))]; }),
    top: Math.min.apply(null, dots.map(function (d) { return Number(d.getAttribute("cy")); })) };
}
process.stdout.write(JSON.stringify({
  capped: read({ values: [96, 100, 98, 100, 100], team: [94, 97, 99, 100, 99], days: DAYS, unit: "percent", scaleMax: 100 }),
  free: read({ values: [96, 100, 98, 100, 100], team: [94, 97, 99, 100, 99], days: DAYS, unit: "percent" })
}));
""")
    lo, hi = out["capped"]["y"]
    assert hi[0] == "100%", "Satisfaction's scale tops out at 100%"
    assert float(lo[0].rstrip("%")) < 94
    assert abs(out["capped"]["top"] - (hi[1] - 4)) < 0.5, "a 100% point sits on the top line"
    assert float(out["free"]["y"][1][0].rstrip("%")) > 100, "without scaleMax the scale is fitted past the data"


def test_trend_line_seconds_ticks_read_m_ss():
    out = run("""
var svg = draw({ values: [425, 470, 510, 528, 500], team: [450, 455, 460, 470, 465], days: DAYS, unit: "seconds" });
var dots = byClass(svg, "dot").filter(function (d) { return !d.classList.contains("ref"); });
process.stdout.write(JSON.stringify({ y: yTicks(svg).map(function (t) { return t.textContent; }),
  last: titleOf(dots[dots.length - 1].parentNode) }));
""")
    assert len(out["y"]) == 2
    assert all(t.count(":") == 1 and len(t.split(":")[1]) == 2 for t in out["y"])
    lo, hi = [int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for t in out["y"]]
    assert lo <= 425 and hi >= 528 and lo % 30 == 0 and hi % 30 == 0
    assert out["last"] == "1:1 10/20: 8:20 (team 7:45)"


def test_trend_line_uses_the_callers_format():
    out = run("""
var svg = draw({ values: [7.1, 7.0, 7.64, 7.36], team: [6.2, 6.5, 6.25, 6.4], days: DAYS.slice(1), unit: "percent",
  scaleMax: 100, digits: 2, format: function (v) { return v.toFixed(2) + "%"; } });
var dots = byClass(svg, "dot").filter(function (d) { return !d.classList.contains("ref"); });
process.stdout.write(JSON.stringify({ y: yTicks(svg).map(function (t) { return t.textContent; }),
  last: titleOf(dots[3].parentNode) }));
""")
    assert out["last"] == "1:1 10/20: 7.36% (team 6.40%)"
    assert all(t.endswith("%") and len(t.split(".")[1]) == 3 for t in out["y"])
    lo, hi = [float(t.rstrip("%")) for t in out["y"]]
    assert 5 < lo <= 6.2 and 7.64 <= hi < 9, "a fitted scale, not 0 to 100"


def test_trend_line_with_no_values_says_so():
    """The note sits in the middle of the plot: between its left and right margins, and halfway between the top
    margin (10px) and the base line, plus 4px to centre the text on that line."""
    out = run("""
var svg = draw({ values: [null, null], team: [null, null], days: DAYS.slice(3), unit: "count" });
var base = byClass(svg, "base")[0], note = texts(svg, "empty")[0];
process.stdout.write(JSON.stringify({ empty: texts(svg, "empty").map(function (t) { return t.textContent; }),
  lines: lines(svg).length, dots: byClass(svg, "dot").length,
  base: ["x1", "x2", "y1"].map(function (k) { return Number(base.getAttribute(k)); }),
  note: [Number(note.getAttribute("x")), Number(note.getAttribute("y")), note.getAttribute("text-anchor")] }));
""")
    assert {k: out[k] for k in ("empty", "lines", "dots")} == {"empty": ["No values in these 1:1s"], "lines": 0,
                                                              "dots": 0}
    left, right, base = out["base"]
    x, y, anchor = out["note"]
    assert anchor == "middle" and abs(x - (left + right) / 2) < 0.5
    assert abs(y - ((10 + base) / 2 + 4)) < 0.5


def test_a_lone_team_point_gets_a_ref_dot():
    out = run("""
var svg = draw({ values: [1, 2, 3], team: [null, 2, null], days: DAYS.slice(2), unit: "count" });
process.stdout.write(JSON.stringify({ ref: byClass(svg, "dot").filter(function (d) { return d.classList.contains("ref"); }).length,
  refLines: lines(svg).filter(function (l) { return l.classList.contains("ref"); }).length }));
""")
    assert out == {"ref": 1, "refLines": 0}
