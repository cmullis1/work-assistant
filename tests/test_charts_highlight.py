"""The Trends graph, Charts.highlightLines, and niceScale's new options. Runs charts.js in node on the fake DOM."""
from __future__ import annotations


from css_rules import app_css, rules
from test_web_overview import node_prelude, run_node

SETUP_JS = """
globalThis.layout = function () { return { clientWidth: 900, scrollWidth: 900 }; };
function all(root, test) { return root.findAll(test); }
function byClass(root, cls) { return root.findAll(function (e) { return e.classList.contains(cls); }); }
function lines(svg) { return svg.findAll(function (e) { return e.tagName === "POLYLINE"; }); }
function texts(svg, cls) {
  return svg.findAll(function (e) { return e.tagName === "TEXT" && e.classList.contains(cls); });
}
function draw(data, opts) {
  var el = document.createElement("div");
  Charts.highlightLines(el, data, Object.assign({ format: String, kind: "number", digits: 1 }, opts || {}));
  return { el: el, svg: el.querySelector("svg") };
}
function ys(svg, cls) {
  return svg.findAll(function (e) { return e.tagName === "TEXT" && e.classList.contains(cls); })
    .map(function (t) { return Number(t.getAttribute("y")); });
}
function data(extra) {
  return Object.assign({
    labels: ["a", "b", "c", "d"], shade: [false, false, true, true], part: [false, false, false, true],
    series: [
      { key: "ann", name: "Ann Lee", values: [5, 6, 7, 8] },
      { key: "bo", name: "Bo Ray", values: [4, 4, null, 6] },
      { key: "cy", name: "Cy Fox", values: [7, 6, 5, 4] }
    ],
    team: { name: "Team", values: [5, 5.5, 6, 6] },
    highlight: "ann"
  }, extra || {});
}
"""


def run(script: str):
    return run_node(node_prelude() + SETUP_JS + script)


def test_draws_one_faint_line_per_other_tech_the_team_dashed_and_the_highlight_on_top():
    out = run("""
var svg = draw(data()).svg;
var classes = lines(svg).map(function (l) { return l.className; });
process.stdout.write(JSON.stringify({ classes: classes }));
""")
    # Bo's run before his gap (two steps) and Cy's whole run are the two faint lines.
    assert out["classes"] == ["line faint", "line faint", "line ref", "line"]


def test_a_lone_point_between_gaps_is_not_drawn_as_a_line():
    out = run("""
var d = data({ series: [{ key: "ann", name: "Ann", values: [5, 6, 7, 8] }, { key: "bo", name: "Bo", values: [4, null, 5, null] }] });
process.stdout.write(JSON.stringify({ classes: lines(draw(d).svg).map(function (l) { return l.className; }) }));
""")
    assert out["classes"] == ["line ref", "line"]


def test_highlight_null_draws_no_bold_line():
    out = run("""
var svg = draw(data({ highlight: null })).svg;
process.stdout.write(JSON.stringify({
  classes: lines(svg).map(function (l) { return l.className; }),
  dots: byClass(svg, "dot").length,
  labels: texts(svg, "dlabel").map(function (t) { return t.textContent; })
}));
""")
    assert "line" not in out["classes"]
    assert out["classes"].count("line ref") == 1
    assert out["dots"] == 0
    assert out["labels"] == ["Team 6"]


def test_highlight_has_a_dot_per_value_and_a_name_label():
    out = run("""
var svg = draw(data()).svg;
process.stdout.write(JSON.stringify({
  dots: byClass(svg, "dot").map(function (d) { return [d.getAttribute("r"), d.className]; }),
  labels: texts(svg, "dlabel").map(function (t) { return t.textContent; })
}))
""")
    assert len(out["dots"]) == 4
    assert all(float(r) >= 4 for r, _ in out["dots"])  # 8 px across at least
    assert sorted(out["labels"]) == ["Ann 8", "Team 6"]


def test_part_steps_have_hollow_dots():
    out = run("""
var svg = draw(data()).svg;
process.stdout.write(JSON.stringify(byClass(svg, "dot").map(function (d) { return d.className; })));
""")
    assert out == ["dot", "dot", "dot", "dot part"]
    css = app_css()
    assert rules(css, ".chart .dot.part")
    assert rules(css, ".chart .line.faint")


def test_count_scale_starts_at_zero_with_whole_ticks():
    out = run("""
var d = data({ series: [{ key: "ann", name: "Ann", values: [0, 1, 2, 1] }, { key: "bo", name: "Bo", values: [0, 0, 1, 2] }],
               team: { name: "Team", values: [0, 0.5, 1, 1] } });
var svg = draw(d, { kind: "count", digits: 0 }).svg;
process.stdout.write(JSON.stringify(texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
  .map(function (t) { return t.textContent; })));
""")
    assert out == ["0", "1", "2", "3"] or out == ["3", "2", "1", "0"]


def test_team_format_labels_the_team_while_the_axis_stays_whole():
    """Ruling 38: a team count is a mean per tech, so its end label and default tooltip keep a decimal; the axis and
    the tech's own figures stay whole."""
    out = run("""
var d = data({ series: [{ key: "ann", name: "Ann", values: [0, 1, 0, 1] }, { key: "bo", name: "Bo", values: [0, 0, 0, 0] }],
               team: { name: "Team", values: [0, 0.5, 0, 0.3] } });
var svg = draw(d, { kind: "count", digits: 0, format: function (v) { return v.toFixed(0); },
                    teamFormat: function (v) { return v.toFixed(1); } }).svg;
process.stdout.write(JSON.stringify({
  ticks: texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; }).map(function (t) { return t.textContent; }),
  labels: texts(svg, "dlabel").map(function (t) { return t.textContent; }),
  tip: byClass(svg, "hit")[3].findAll(function (e) { return e.tagName === "DESC"; })[0].textContent
}));
""")
    assert sorted(out["ticks"]) == ["0", "1", "2", "3"]
    assert sorted(out["labels"]) == ["Ann 1", "Team 0.3"]
    assert out["tip"] == "d\nAnn: 1\nTeam: 0.3"


def test_count_scale_grows_to_the_data_with_whole_ticks():
    out = run("""
var d = data({ series: [{ key: "ann", name: "Ann", values: [0, 7.2, 2, 1] }], team: { name: "Team", values: [1, 2, 3, 4] } });
var svg = draw(d, { kind: "count", digits: 0 }).svg;
process.stdout.write(JSON.stringify(texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
  .map(function (t) { return Number(t.textContent); })));
""")
    assert min(out) == 0 and max(out) >= 8
    assert all(float(v).is_integer() for v in out)


def test_fitted_scale_for_numbers_and_percent_cap():
    out = run("""
function tickRange(kind) {
  var d = data({
    series: [{ key: "ann", name: "Ann", values: [82, 90, 94, 96] }, { key: "bo", name: "Bo", values: [85, 86, 88, 91] }],
    team: { name: "Team", values: [84, 88, 91, 93] } });
  var svg = draw(d, { kind: kind, digits: 0 }).svg;
  var t = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
    .map(function (t) { return Number(t.textContent); });
  return [Math.min.apply(null, t), Math.max.apply(null, t)];
}
process.stdout.write(JSON.stringify({ percent: tickRange("percent"), number: tickRange("number") }));
""")
    lo, hi = out["percent"]
    assert 0 < lo <= 82 and 96 <= hi <= 100
    lo, hi = out["number"]
    assert 0 < lo <= 82 and hi >= 96


def test_tiny_ranges_do_not_blow_up():
    out = run("""
var d = data({
  series: [{ key: "ann", name: "Ann", values: [0.10, 0.11, 0.12, 0.1] }, { key: "bo", name: "Bo", values: [0.11, 0.11, 0.1, 0.12] }],
  team: { name: "Team", values: [0.1, 0.11, 0.11, 0.11] } });
var svg = draw(d, { kind: "number", digits: 2 }).svg;
var t = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
  .map(function (t) { return Number(t.textContent); });
process.stdout.write(JSON.stringify(t));
""")
    assert max(out) - min(out) < 0.2
    assert len(set(out)) == len(out)  # ticks do not collapse to the same figure


def test_percent_never_goes_below_zero_or_above_100():
    out = run("""
var d = data({
  series: [{ key: "ann", name: "Ann", values: [99, 100, 100, 99.5] }], team: { name: "Team", values: [98, 99, 100, 100] } });
var svg = draw(d, { kind: "percent", digits: 0 }).svg;
var t = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
  .map(function (t) { return Number(t.textContent); });
process.stdout.write(JSON.stringify(t));
""")
    assert max(out) == 100 and min(out) >= 0


def test_end_labels_never_overlap():
    out = run("""
var d = data({
  series: [{ key: "ann", name: "Ann Lee", values: [5, 6, 7, 6] }, { key: "bo", name: "Bo", values: [4, 4, 5, 5] }],
  team: { name: "Team", values: [5, 6, 7, 6] } });
var svg = draw(d).svg;
process.stdout.write(JSON.stringify(ys(svg, "dlabel")));
""")
    assert len(out) == 2
    assert abs(out[0] - out[1]) >= 14


def _end_label_run(values, kind="count"):
    return run("""
var d = data({
  series: [{ key: "jo", name: "Jordan Ames", values: %s }],
  team: { name: "Team", values: %s }, highlight: "jo" });
var svg = draw(d, { kind: "%s", digits: 0 }).svg;
var tick = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "middle"; })
  .map(function (t) { return Number(t.getAttribute("y")); });
process.stdout.write(JSON.stringify({ ys: ys(svg, "dlabel"), tickRow: tick[0], labels: texts(svg, "dlabel").length }));
""" % (values, values, kind))


def test_end_labels_at_the_bottom_stay_inside_the_plot_and_off_the_x_axis_row():
    out = _end_label_run("[2, 1, 0, 0]")
    assert out["labels"] == 2
    low, high = sorted(out["ys"], reverse=True)[0], sorted(out["ys"])[0]
    plot_bottom = 360 - 26
    assert low <= plot_bottom + 4
    assert low < out["tickRow"] - 11          # clear of the x-axis label row
    assert low - high >= 14


def test_end_labels_at_the_top_stay_inside_the_plot():
    out = _end_label_run("[90, 95, 100, 100]", "percent")
    assert out["labels"] == 2
    low, high = sorted(out["ys"], reverse=True)[0], sorted(out["ys"])[0]
    assert high >= 22 + 4
    assert low - high >= 14


def test_end_labels_sit_at_the_last_non_null_point():
    out = run("""
var d = data({
  series: [{ key: "ann", name: "Ann Lee", values: [5, 6, 7, null] }, { key: "bo", name: "Bo", values: [4, 4, 5, 5] }],
  team: { name: "Team", values: [5, 6, 7, 6] } });
var svg = draw(d).svg;
var dots = byClass(svg, "dot");
var label = texts(svg, "dlabel").filter(function (t) { return t.textContent.indexOf("Ann") === 0; })[0];
process.stdout.write(JSON.stringify({ lastDotX: Number(dots[dots.length - 1].getAttribute("cx")),
  labelX: Number(label.getAttribute("x")), text: label.textContent, dots: dots.length }));
""")
    assert out["dots"] == 3 and out["text"] == "Ann 7"
    assert out["labelX"] > out["lastDotX"]


def test_tooltip_per_step():
    out = run("""
var calls = [];
var svg = draw(data(), { tooltip: function (i, key) { calls.push([i, key]); return "step " + i + " of " + key; } }).svg;
var hits = byClass(svg, "hit");
process.stdout.write(JSON.stringify({
  hits: hits.length,
  titles: hits.map(function (g) { return g.findAll(function (e) { return e.tagName === "DESC"; })[0].textContent; }),
  calls: calls
}));
""")
    assert out["hits"] == 4
    assert out["titles"] == ["step 0 of ann", "step 1 of ann", "step 2 of ann", "step 3 of ann"]
    assert out["calls"] == [[0, "ann"], [1, "ann"], [2, "ann"], [3, "ann"]]


def test_tooltip_gets_null_without_a_highlight():
    out = run("""
var svg = draw(data({ highlight: null }), { tooltip: function (i, key) { return "k=" + key; } }).svg;
process.stdout.write(JSON.stringify(byClass(svg, "hit").map(function (g) {
  return g.findAll(function (e) { return e.tagName === "DESC"; })[0].textContent; })));
""")
    assert out == ["k=null"] * 4


def test_empty_when_no_values():
    out = run("""
var d = data({ series: [{ key: "ann", name: "Ann", values: [null, null, null, null] }], team: { name: "Team", values: [null, null, null, null] } });
var r = draw(d, { empty: "Nothing to graph" });
process.stdout.write(JSON.stringify({ text: texts(r.svg, "empty").map(function (t) { return t.textContent; }), lines: lines(r.svg).length }));
""")
    assert out == {"text": ["Nothing to graph"], "lines": 0}


def test_repeated_x_labels_are_skipped():
    """Steps are split at the period's edges, so neighbouring steps can share a label ("Aug 30" twice)."""
    out = run("""
var d = data({ labels: ["Aug 23", "Aug 30", "Aug 30", "Sep 6", "Sep 13", "Sep 13"], shade: [false, true, true, true, true, true],
  part: [false, true, false, false, false, true],
  series: [{ key: "ann", name: "Ann", values: [1, 2, 3, 4, 5, 6] }], team: { name: "Team", values: [1, 2, 3, 4, 5, 6] } });
var svg = draw(d).svg;
var x = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "middle"; })
  .map(function (t) { return t.textContent; });
process.stdout.write(JSON.stringify(x));
""")
    assert len(out) == len(set(out))
    assert "Aug 30" in out and "Sep 13" in out and "Sep 6" in out


def test_x_labels_are_thinned_so_they_do_not_overlap():
    out = run("""
globalThis.layout = function () { return { clientWidth: 400, scrollWidth: 400 }; };
var labels = [], vals = [];
for (var i = 0; i < 17; i++) { labels.push("W" + (100 + i)); vals.push(i); }
var d = { labels: labels, shade: labels.map(function () { return true; }), part: labels.map(function () { return false; }),
  series: [{ key: "ann", name: "Ann", values: vals }], team: { name: "Team", values: vals }, highlight: "ann" };
var svg = draw(d).svg;
var xs = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "middle"; })
  .map(function (t) { return Number(t.getAttribute("x")); });
process.stdout.write(JSON.stringify(xs));
""")
    assert len(out) >= 2
    out.sort()
    assert min(b - a for a, b in zip(out, out[1:])) >= 40


def test_period_band_shades_the_chosen_steps():
    out = run("""
var svg = draw(data()).svg;
process.stdout.write(JSON.stringify(byClass(svg, "period-band").length));
""")
    assert out == 1


def test_draws_at_the_real_pixel_width():
    out = run("""
globalThis.layout = function () { return { clientWidth: 731, scrollWidth: 731 }; };
var svg = draw(data()).svg;
process.stdout.write(JSON.stringify([svg.getAttribute("width"), svg.getAttribute("viewBox")]));
""")
    assert out[0] == "731" and out[1].startswith("0 0 731 ")


OLD_NICE_SCALE = """
function oldNiceScale(values, opts) {
  var NICE_STEPS = [1, 2, 2.5, 5, 10, 20, 25, 50];
  function r1(n) { return Math.round(n * 10) / 10; }
  var lo = Math.min.apply(null, values);
  var hi = Math.max.apply(null, values);
  var floor = opts.min != null ? opts.min : -Infinity;
  var ceil = opts.max != null ? opts.max : Infinity;
  if (opts.zero) {
    lo = floor === -Infinity ? 0 : floor;
  } else {
    var pad = Math.max((hi - lo) * 0.15, 2);
    lo -= pad;
    hi += pad;
  }
  if (hi <= lo) {
    hi = lo + 1;
  }
  var step = NICE_STEPS[NICE_STEPS.length - 1];
  for (var i = 0; i < NICE_STEPS.length; i++) {
    if ((hi - lo) / NICE_STEPS[i] <= 4) {
      step = NICE_STEPS[i];
      break;
    }
  }
  lo = Math.max(floor, Math.floor(lo / step) * step);
  hi = Math.min(ceil, Math.ceil(hi / step) * step);
  if (hi <= lo) {
    hi = lo + step;
  }
  var ticks = [];
  for (var t = lo; t <= hi + 1e-9; t += step) {
    ticks.push(r1(t));
  }
  return { lo: lo, hi: hi, ticks: ticks };
}
"""


def test_niceScale_defaults_unchanged():
    """Every existing chart's ticks are what they were before niceScale gained options."""
    out = run(OLD_NICE_SCALE + """
var inputs = [];
var seed = 12345;
function rnd() { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }
for (var n = 0; n < 400; n++) {
  var base = rnd() * 120, span = rnd() * (n % 5 === 0 ? 1 : 80);
  var vals = [base, base + span * rnd(), base + span];
  inputs.push([vals, [{}, { min: 0, max: 100 }, { zero: true }, { zero: true, min: 0 }, { min: 0 }][n % 5]]);
}
inputs.push([[90, 91, 92, 93], { min: 0, max: 100 }], [[0], { zero: true }], [[5, 5], {}], [[100, 100], { max: 100 }]);
var same = 0, diffs = [];
inputs.forEach(function (c) {
  var a = JSON.stringify(oldNiceScale(c[0], c[1])), b = JSON.stringify(Charts.niceScale(c[0], c[1]));
  if (a === b) { same += 1; } else { diffs.push([c, a, b]); }
});
process.stdout.write(JSON.stringify({ same: same, total: inputs.length, diffs: diffs.slice(0, 3) }));
""")
    assert out["diffs"] == []
    assert out["same"] == out["total"]


def test_niceScale_integer_ticks_and_pad_options():
    out = run("""
var integer = Charts.niceScale([0, 10], { zero: true, integer: true });
var tight = Charts.niceScale([100, 101], { pad: 0.12, minPad: 0.01 });
var wide = Charts.niceScale([100, 101], {});
process.stdout.write(JSON.stringify({ integer: integer, tight: tight, wide: wide }));
""")
    assert all(float(t).is_integer() for t in out["integer"]["ticks"])
    assert out["integer"]["lo"] == 0 and out["integer"]["hi"] >= 10
    assert out["tight"]["hi"] - out["tight"]["lo"] < out["wide"]["hi"] - out["wide"]["lo"]


def test_identical_values_never_get_a_step_finer_than_the_measure_precision():
    """Task 19 review F1: Shifts worked at 100% everywhere read "99%, 100%, 100%" from a half-percent step."""
    out = run("""
var d = data({
  series: [{ key: "ann", name: "Ann", values: [100, 100, 100, 100] }, { key: "bo", name: "Bo", values: [100, 100, 100, 100] }],
  team: { name: "Team", values: [100, 100, 100, 100] } });
var svg = draw(d, { kind: "percent", digits: 0 }).svg;
var t = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
  .map(function (t) { return Number(t.textContent); });
var one = Charts.niceScale([100, 100], { pad: 0.12, minPad: 1, min: 0, max: 100 });
var tenth = Charts.niceScale([5, 5], { pad: 0.12, minPad: 0.1, min: 0 });
process.stdout.write(JSON.stringify({ ticks: t, one: one.ticks, tenth: tenth.ticks }));
""")
    assert len(set(out["ticks"])) == len(out["ticks"]) and max(out["ticks"]) == 100
    assert all(float(v).is_integer() for v in out["ticks"])
    assert out["one"] == [99, 100]
    steps = [round(b - a, 6) for a, b in zip(out["tenth"], out["tenth"][1:])]
    assert steps and min(steps) >= 0.1


def test_niceScale_tolerances_apply_only_with_minPad():
    """Task 19 review F2: without minPad the scale is the old code exactly, even a hair over a step boundary."""
    out = run(OLD_NICE_SCALE + """
var inputs = [[[0, 4.0000000001], { zero: true }], [[0, 6.0000000001], { zero: true }], [[0, 8.0000000004], { zero: true }],
  [[1, 3.0000000002], { zero: true, min: 0 }], [[0, 20.0000000001], { zero: true, max: 100 }]];
var diffs = [];
inputs.forEach(function (c) {
  var a = JSON.stringify(oldNiceScale(c[0], c[1])), b = JSON.stringify(Charts.niceScale(c[0], c[1]));
  if (a !== b) { diffs.push([c, a, b]); }
});
process.stdout.write(JSON.stringify(diffs));
""")
    assert out == []


PRECISION_JS = """
function tickTexts(d, digits) {
  var svg = draw(d, { kind: "number", digits: digits, format: function (v) { return v.toFixed(digits); } }).svg;
  return texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "end"; })
    .map(function (t) { return t.textContent; });
}
function spread(values) {
  return data({ series: [{ key: "ann", name: "Ann", values: values }, { key: "bo", name: "Bo", values: values }],
    team: { name: "Team", values: values } });
}
"""


def test_whole_number_measure_ticks_are_whole_numbers():
    """Task 20 review F1: minutes 0-7 at 0 digits read "0, 3, 5, 8, 10" from a 2.5 step."""
    out = run(PRECISION_JS + """
var scale = Charts.niceScale([0, 1, 2, 3, 4, 5, 6, 7], { pad: 0.12, minPad: 1, min: 0 });
process.stdout.write(JSON.stringify({ ticks: scale.ticks, labels: tickTexts(spread([0, 3, 5, 7]), 0) }));
""")
    assert out["ticks"] and all(float(t).is_integer() for t in out["ticks"])
    assert len(set(out["labels"])) == len(out["labels"])
    assert all(float(label).is_integer() for label in out["labels"])


def test_hundredths_measure_ticks_are_exact_hundredths():
    """Task 20 review F1: flags per shift 0-0.07 at 2 digits read "0.00, 0.03, 0.05, 0.07, 0.10"."""
    out = run(PRECISION_JS + """
var scale = Charts.niceScale([0, 0.01, 0.03, 0.05, 0.07], { pad: 0.12, minPad: 0.01, min: 0 });
process.stdout.write(JSON.stringify({ ticks: scale.ticks, labels: tickTexts(spread([0, 0.02, 0.05, 0.07]), 2) }))
""")
    assert out["ticks"]
    assert all(abs(t * 100 - round(t * 100)) < 1e-9 for t in out["ticks"])
    assert len(set(out["labels"])) == len(out["labels"])
    assert all(abs(float(label) * 100 - round(float(label) * 100)) < 1e-9 for label in out["labels"])


def test_every_fine_tick_is_distinct_at_the_measure_precision():
    """Task 20 review F1: across many ranges, toFixed(digits) never repeats or rounds a tick."""
    out = run("""
var seed = 777, bad = [];
function rnd() { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }
[0, 1, 2].forEach(function (digits) {
  var unit = Math.pow(10, -digits);
  for (var n = 0; n < 300; n++) {
    var base = rnd() * 50 * unit * (n % 3 === 0 ? 0 : 1), span = rnd() * 40 * unit;
    var vals = [base, base + span * rnd(), base + span];
    var ticks = Charts.niceScale(vals, { pad: 0.12, minPad: unit, min: 0 }).ticks;
    var labels = ticks.map(function (t) { return t.toFixed(digits); });
    var exact = ticks.every(function (t) { return Math.abs(Number(t.toFixed(digits)) - t) < 1e-9; });
    if (!exact || new Set(labels).size !== labels.length) { bad.push([digits, vals, ticks]); }
  }
});
process.stdout.write(JSON.stringify(bad.slice(0, 3)));
""")
    assert out == []


def test_a_lone_team_point_gets_a_ref_dot_beside_its_label():
    """Task 19 review F3: a team value between gaps draws no line, so its "Team" label needs a mark."""
    out = run("""
var d = data({ team: { name: "Team", values: [null, 5, null, null] } });
var svg = draw(d).svg;
var refDots = byClass(svg, "dot").filter(function (c) { return c.classList.contains("ref"); });
var label = texts(svg, "dlabel").filter(function (t) { return t.textContent.indexOf("Team") === 0; })[0];
process.stdout.write(JSON.stringify({ n: refDots.length, cx: Number(refDots[0].getAttribute("cx")),
  labelX: Number(label.getAttribute("x")), refLines: byClass(svg, "line").filter(function (l) { return l.classList.contains("ref"); }).length }));
""")
    assert out["n"] == 1 and out["refLines"] == 0
    assert out["labelX"] > out["cx"]


def test_team_with_a_run_draws_a_line_and_no_ref_dot():
    out = run("""
process.stdout.write(JSON.stringify(byClass(draw(data()).svg, "dot").filter(function (c) { return c.classList.contains("ref"); }).length));
""")
    assert out == 0


# -- steps sized by their days -------------------------------------------------------------------------------

DATED_JS = """
function dated(extra) {
  return Object.assign({
    labels: ["Aug 23", "Aug 30", "Sep 1", "Sep 6"], sizes: [7, 2, 5, 7],
    shade: [false, false, true, true], part: [false, true, true, false],
    series: [{ key: "zane", name: "Zane", values: [15.4, null, 14.0, 12.4] }],
    team: { name: "Team", values: [12, 8, 11, 12] }, highlight: "zane"
  }, extra || {});
}
function hits(svg) {
  return svg.findAll(function (e) { return e.tagName === "RECT" && e.classList.contains("hitarea"); })
    .map(function (r) { return { x: Number(r.getAttribute("x")), w: Number(r.getAttribute("width")) }; });
}
"""


def test_each_step_is_as_wide_as_its_days():
    out = run(DATED_JS + """
process.stdout.write(JSON.stringify(hits(draw(dated()).svg)));
""")
    widths = [h["w"] for h in out]
    assert abs(widths[1] / widths[0] - 2 / 7) < 0.01 and abs(widths[2] / widths[0] - 5 / 7) < 0.01
    assert all(abs(a["x"] + a["w"] - b["x"]) < 0.5 for a, b in zip(out, out[1:])), "side by side, no overlap"


def test_a_part_step_with_no_figure_does_not_break_the_line():
    """A part step exists only because the period's edge cut its week: with no shifts in it, the line carries on."""
    out = run(DATED_JS + """
var svg = draw(dated()).svg;
var bold = lines(svg).filter(function (l) { return l.className === "line"; });
var dots = byClass(svg, "dot").filter(function (d) { return !d.classList.contains("ref"); });
process.stdout.write(JSON.stringify({ bold: bold.length, points: bold[0].getAttribute("points").split(" ").length,
  dots: dots.length }));
""")
    assert out == {"bold": 1, "points": 3, "dots": 3}, "one line through the three figures; no dot where there is none"


def test_a_whole_step_with_no_figure_still_breaks_the_line():
    out = run(DATED_JS + """
var d = dated({ part: [false, false, true, false], series: [{ key: "zane", name: "Zane", values: [15.4, null, 14.0, 12.4] }] });
var bold = lines(draw(d).svg).filter(function (l) { return l.className === "line"; });
process.stdout.write(JSON.stringify(bold.length));
""")
    assert out == 1, "only Sep 1 to Sep 6 is a line: the empty whole step breaks it"


def test_dated_labels_never_overlap_and_the_band_starts_at_its_first_day():
    out = run(DATED_JS + """
var svg = draw(dated()).svg;
var ticks = texts(svg, "tick").filter(function (t) { return t.getAttribute("text-anchor") === "middle"; })
  .map(function (t) { return { x: Number(t.getAttribute("x")), text: t.textContent }; });
var band = byClass(svg, "period-band")[0];
process.stdout.write(JSON.stringify({ ticks: ticks, band: Number(band.getAttribute("x")), hits: hits(svg) }));
""")
    xs = sorted(t["x"] for t in out["ticks"])
    assert min(b - a for a, b in zip(xs, xs[1:])) >= 40
    assert {"Aug 23", "Sep 6"} <= {t["text"] for t in out["ticks"]}
    assert abs(out["band"] - out["hits"][2]["x"]) < 0.5, "the shading starts where Sep 1 does"


# -- a narrow Trends graph (iPad and phone design §10.3) ------------------------------------------------------

NARROW_JS = """
function at(width) {
  globalThis.layout = function () { return { clientWidth: width, scrollWidth: width }; };
}
function readout(el) { return el.querySelector("div.trend-readout"); }
function textOf(node) { return node ? node.textContent : null; }
"""


def test_a_narrow_trends_graph_takes_the_full_width_with_its_latest_figures_above_it():
    """Under TRENDS_NARROW (560px) the plot keeps no room for end labels and draws none, its height follows the width
    (0.8 of it), and a readout above the graph gives the focus's first name and latest figure, the team's, and the
    step they are from."""
    out = run(NARROW_JS + """
at(340);
var r = draw(data());
var svg = r.el.querySelector("svg");
var box = readout(r.el);
var guides = svg.findAll(function (e) { return e.tagName === "LINE" && e.classList.contains("guide"); })
  .map(function (l) { return Number(l.getAttribute("x1")); });
process.stdout.write(JSON.stringify({
  order: r.el.children.map(function (c) { return c.tagName + "." + c.className; }),
  height: svg.getAttribute("height"), viewBox: svg.getAttribute("viewBox"),
  dlabels: texts(svg, "dlabel").length,
  me: textOf(box.querySelector("span.me")), meValue: textOf(box.querySelector("span.me b")),
  ref: textOf(box.querySelector("span.ref")), refValue: textOf(box.querySelector("span.ref b")),
  when: textOf(box.querySelector("span.when")),
  lastX: Math.max.apply(null, guides)
}));
""")
    assert out["order"] == ["DIV.trend-readout", "SVG.chart"], "the readout sits in the chart's box, above the graph"
    assert out["height"] == "272" and out["viewBox"] == "0 0 340 272", "0.8 of the width"
    assert out["dlabels"] == 0, "no end labels"
    assert out["me"] == "Ann 8" and out["meValue"] == "8"
    assert out["ref"] == "Team 6" and out["refValue"] == "6"
    assert out["when"] == "latest · d"
    assert 300 < out["lastX"] <= 340 - 18, "the last step sits at the plot's right edge, not before an end-label gap"


def test_a_narrow_trends_graph_is_kept_between_230_and_300_px_tall():
    out = run(NARROW_JS + """
var heights = {};
[0, 250, 340, 500, 559, 560].forEach(function (w) {
  at(w);
  var r = draw(data());
  heights[w] = [r.el.querySelector("svg").getAttribute("height"), readout(r.el) !== null];
});
process.stdout.write(JSON.stringify(heights));
""")
    assert out == {"250": ["230", True], "340": ["272", True], "500": ["300", True], "559": ["300", True],
                   "560": ["360", False],
                   "0": ["360", False]}, "a graph not laid out yet (0 wide) keeps the wide form until it has a width"


def test_a_wide_trends_graph_is_as_it_was():
    out = run(NARROW_JS + """
at(900);
var r = draw(data());
var svg = r.el.querySelector("svg");
var guides = svg.findAll(function (e) { return e.tagName === "LINE" && e.classList.contains("guide"); })
  .map(function (l) { return Number(l.getAttribute("x1")); });
process.stdout.write(JSON.stringify({
  order: r.el.children.map(function (c) { return c.tagName; }), height: svg.getAttribute("height"),
  labels: texts(svg, "dlabel").map(function (t) { return t.textContent; }).sort(), readout: readout(r.el) !== null,
  lastX: Math.max.apply(null, guides)
}));
""")
    assert out["order"] == ["SVG"] and out["readout"] is False
    assert out["height"] == "360"
    assert out["labels"] == ["Ann 8", "Team 6"]
    assert out["lastX"] == 900 - 104, "the end labels' room is kept right of the plot"


def test_peek_on_a_narrow_graph_adds_the_lifted_tech_to_the_readout():
    out = run(NARROW_JS + """
at(340);
var r = draw(data());
var svg = r.el.querySelector("svg");
var box = readout(r.el);
function state() {
  return { peek: box.querySelectorAll("span.peek").map(function (s) { return s.textContent; }),
    value: textOf(box.querySelector("span.peek b")),
    order: box.children.map(function (c) { return c.className; }),
    labels: texts(svg, "dlabel").length,
    lifted: lines(svg).filter(function (l) { return l.classList.contains("peek"); }).map(function (l) { return l.getAttribute("data-key"); }) };
}
Charts.peek(r.el, "bo");
var on = state();
Charts.peek(r.el, "cy");
var other = state();
Charts.peek(r.el, null);
process.stdout.write(JSON.stringify({ on: on, other: other, off: state() }));
""")
    assert out["on"] == {"peek": ["Bo 6"], "value": "6", "order": ["me", "ref", "peek", "when"], "labels": 0,
                         "lifted": ["bo"]}
    assert out["other"]["peek"] == ["Cy 4"], "one lifted tech at a time"
    assert out["off"] == {"peek": [], "value": None, "order": ["me", "ref", "when"], "labels": 0, "lifted": []}


def test_peek_on_a_wide_graph_still_adds_an_end_label():
    out = run(NARROW_JS + """
at(900);
var r = draw(data());
var svg = r.el.querySelector("svg");
Charts.peek(r.el, "bo");
var on = texts(svg, "dlabel").map(function (t) { return t.textContent; }).sort();
Charts.peek(r.el, null);
process.stdout.write(JSON.stringify({ on: on, off: texts(svg, "dlabel").length, readout: readout(r.el) !== null }));
""")
    assert out == {"on": ["Ann 8", "Bo 6", "Team 6"], "off": 2, "readout": False}


def test_a_narrow_graph_with_no_figures_has_no_readout():
    out = run(NARROW_JS + """
at(340);
var d = data({ series: [{ key: "ann", name: "Ann", values: [null, null, null, null] }], team: { name: "Team", values: [null, null, null, null] } });
var r = draw(d, { empty: "Nothing to graph" });
process.stdout.write(JSON.stringify({ readout: readout(r.el) !== null, empty: texts(r.el.querySelector("svg"), "empty").length }));
""")
    assert out == {"readout": False, "empty": 1}


def test_a_narrow_graph_without_a_focus_reads_the_team_only():
    out = run(NARROW_JS + """
at(340);
var box = readout(draw(data({ highlight: null })).el);
process.stdout.write(JSON.stringify(box.children.map(function (c) { return [c.className, c.textContent]; })));
""")
    assert out == [["ref", "Team 6"], ["when", "latest · d"]]


def test_trend_readout_is_a_shared_part_sized_from_tokens():
    css = app_css()
    assert rules(css, ".trend-readout")
    for selector in (".trend-readout b", ".trend-readout .when"):
        assert any("font-size: var(--fs-" in body for body in rules(css, selector)), selector
    assert any("color: var(--series-1)" in body for body in rules(css, ".trend-readout .me b"))
    # Not the Rubric's .ref block: no rule line above the team's figure.
    assert any("border-top: 0" in body and "padding: 0" in body for body in rules(css, ".trend-readout .ref"))
