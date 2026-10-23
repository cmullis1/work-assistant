"""A tech's day chart on a phone (iPad and phone design, the owner's picks of 2026-10-23, T-179). Narrower than the
Trends graph's cutoff (TRENDS_NARROW, 560px) its hour labels thin out so none overlap, keeping the first and the last,
and the long gap's label gets a line of its own above the plot, never over the bars ("4 h 05 m gap" when the whole
words don't fit the plot). Wider charts draw as before. On a touch screen the first tap on a moment shows its card and
a second tap on it opens its ticket; a mouse click opens it at once. Runs charts.js in node on the fake DOM."""
from __future__ import annotations

import hashlib

from test_web_overview import node_prelude, run_node
from test_web_team_activity import DAY_DATA

CHAR_W = 7      # px, about the widest a 12px chart character runs (Segoe UI's digits and capitals)
DAY_SIDE = 22   # px kept clear at each end of the day chart's plot (charts.js)
HOUR_Y = 86     # the hour labels' line on a chart with no line above its plot

SETUP_JS = """
var width = 340;
globalThis.innerWidth = 360;
globalThis.innerHeight = 780;
globalThis.layout = function (el) {
  if (el.classList.contains("hovercard")) {
    return { offsetWidth: 300, offsetHeight: 120 };
  }
  return { clientWidth: width, scrollWidth: width };
};
var el = document.createElement("div");
document.body.appendChild(el);
function tag(e, name) { return e.tagName.toLowerCase() === name; }
function find(name, klass) {
  return el.findAll(function (e) { return tag(e, name) && e.className.split(/\\s+/).indexOf(klass) >= 0; });
}
function texts(klass) {
  return find("text", klass).map(function (t) {
    return { text: t.textContent, x: Number(t.getAttribute("x")), y: Number(t.getAttribute("y")) };
  });
}
function tops(name, klass) { return find(name, klass).map(function (r) { return Number(r.getAttribute("y")); }); }
// The drawing as text: each element's tag, its attributes in name order, then its children.
function ser(n) {
  if (n.nodeType === 3) { return JSON.stringify(n.data); }
  var attrs = Object.keys(n.attributes).sort().map(function (k) { return k + "=" + JSON.stringify(n.attributes[k]); });
  return "<" + n.tagName + " " + attrs.join(" ") + ">" + n.childNodes.map(ser).join("") + "</" + n.tagName + ">";
}
function draw(data) {
  Charts.dayChart(el, data, { describe: function (those) { return those[0].at + " " + those[0].kind; },
    open: function () {} });
}
"""

# A long day: an action at 3:10 AM and one at 10:40 PM widen the axis to 3 AM - 11 PM, so at 340px the ten even hours'
# labels would run into each other.
LONG_DAY = """{ shift: { start: "08:00", end: "17:00" }, signin: "08:11", gap: { start: "09:30", minutes: 245 },
  actions: [{ at: "03:10", kind: "reply" }, { at: "08:30", kind: "reply" }, { at: "09:00", kind: "note" },
            { at: "13:35", kind: "status" }, { at: "22:40", kind: "reply" }] }"""


def run(script: str):
    return run_node(node_prelude() + SETUP_JS + script)


def edges(label: dict) -> tuple[float, float]:
    half = len(label["text"]) * CHAR_W / 2
    return label["x"] - half, label["x"] + half


def test_a_wide_day_chart_draws_as_before():
    """At 900px nothing changes: every even hour labelled, the gap's label over the gap band, the same drawing."""
    out = run("width = 900; draw(" + DAY_DATA + """);
var svg = el.children[0];
process.stdout.write(JSON.stringify({ svg: ser(svg), height: svg.getAttribute("height"), ticks: texts("tick"),
  gap: texts("gaplabel"), band: tops("rect", "gapband") }));
""")
    assert [t["text"] for t in out["ticks"]] == ["6 AM", "8 AM", "10 AM", "Noon", "2 PM", "4 PM", "6 PM",
                                                 "signed in 8:11 AM"]
    assert out["gap"] == [{"text": "4 h 05 m with nothing recorded", "x": 386.9, "y": 47}]
    assert out["band"] == [20] and out["height"] == "100"
    # The whole drawing as it was before the narrow form (serialized by `ser`, 2840 characters).
    assert len(out["svg"]) == 2840
    assert hashlib.sha256(out["svg"].encode("utf-8")).hexdigest() == \
        "e97927a4bb700a1c7ac77dbbb65e114c71cd12fe900c6b668bfad6a911456a6b"


def test_a_narrow_day_charts_hour_labels_never_overlap():
    out = run("""
var out = {};
draw(""" + LONG_DAY + """); out.long = texts("tick");
draw(""" + DAY_DATA + """); out.day = texts("tick");
process.stdout.write(JSON.stringify(out));
""")
    for key in ("long", "day"):
        hours = [t for t in out[key] if not t["text"].startswith("signed in")]
        assert len({t["y"] for t in hours}) == 1, key
        for left, right in zip(hours, hours[1:]):
            assert edges(left)[1] <= edges(right)[0], (key, left, right)
    long_hours = [t["text"] for t in out["long"] if not t["text"].startswith("signed in")]
    assert long_hours[0] == "4 AM" and long_hours[-1] == "10 PM", "the first and the last stay"
    assert len(long_hours) < 10, "some of the ten even hours give way"


def test_a_narrow_day_chart_puts_the_gap_label_above_the_bars():
    out = run("draw(" + DAY_DATA + """);
var svg = el.children[0];
process.stdout.write(JSON.stringify({ gap: texts("gaplabel"), shift: texts("shiftlabel"), bars: tops("rect", "bar"),
  band: tops("rect", "period-band"), gapband: tops("rect", "gapband"), hits: tops("rect", "day-hit"),
  height: Number(svg.getAttribute("height")), ticks: texts("tick") }));
""")
    gap = out["gap"][0]
    assert gap["text"] == "4 h 05 m with nothing recorded"
    plot_top = out["band"][0]
    assert gap["y"] + 3 < plot_top, "the label's line ends above the plot"
    assert gap["y"] < min(out["bars"]) and gap["y"] < out["gapband"][0] and gap["y"] < min(out["hits"])
    assert gap["y"] - 12 > out["shift"][0]["y"], "a line of its own, under the shift's label"
    left, right = edges(gap)
    assert DAY_SIDE <= left and right <= 340 - DAY_SIDE, "inside the plot's width"
    assert out["height"] > 100, "the chart is a line taller"
    assert all(t["y"] > max(out["bars"]) for t in out["ticks"]), "the hour labels stay under the plot"


def test_a_gap_label_too_long_for_a_narrow_plot_is_shortened():
    out = run("width = 240; draw(" + DAY_DATA + "); process.stdout.write(JSON.stringify(texts('gaplabel')));")
    assert [t["text"] for t in out] == ["4 h 05 m gap"]
    left, right = edges(out[0])
    assert DAY_SIDE <= left and right <= 240 - DAY_SIDE


def test_a_narrow_chart_with_no_gap_keeps_its_height():
    out = run("""draw({ shift: { start: "08:00", end: "17:00" }, signin: null, gap: null,
  actions: [{ at: "08:30", kind: "reply" }] });
process.stdout.write(JSON.stringify({ height: el.children[0].getAttribute("height"),
  hours: texts("tick").map(function (t) { return t.y; }) }));
""")
    assert out["height"] == "100" and set(out["hours"]) == {HOUR_Y}


# -- taps --------------------------------------------------------------------------------------------------------------

# A chart whose moments open (`opened` collects them). `send` fires an event on a mark as the browser does: its own
# listeners, then the page's (charts.js's hovercard). A tap is a press, a lift and a click; a click carries no
# pointerType (Safari's doesn't).
TAP_JS = """
var opened = [];
Charts.dayChart(el, """ + DAY_DATA + """, { describe: function (those) { return "At " + those[0].at; },
  open: function (those) { opened.push(those[0].at); } });
var hits = find("rect", "day-hit");
var outside = document.createElement("p");
document.body.appendChild(outside);
var under = null;
document.elementFromPoint = function () { return under; };
function send(target, type, pointerType) {
  under = target;
  var event = { type: type, pointerType: pointerType, clientX: 120, clientY: 400, target: target };
  (target.listeners[type] || []).forEach(function (f) { f(event); });
  (document.listeners[type] || []).forEach(function (f) { f(event); });
}
function tap(target, pointerType) {
  send(target, "pointerdown", pointerType);
  send(target, "pointerup", pointerType);
  send(target, "click");
}
function state() {
  var c = document.body.findAll(function (e) { return e.classList.contains("hovercard"); })[0];
  return { opened: opened.slice(), card: c && !c.hidden ? c.textContent : null };
}
"""


def test_a_finger_taps_once_to_read_a_moment_and_again_to_open_it():
    out = run(TAP_JS + """
var out = [];
tap(hits[0], "touch"); out.push(state());
tap(hits[0], "touch"); out.push(state());
tap(hits[1], "touch"); out.push(state());
tap(hits[1], "touch"); out.push(state());
tap(outside, "touch"); out.push(state());
tap(hits[1], "touch"); out.push(state());
process.stdout.write(JSON.stringify(out));
""")
    assert out == [
        {"opened": [], "card": "At 08:30"},
        {"opened": ["08:30"], "card": "At 08:30"},
        {"opened": ["08:30"], "card": "At 09:00"},
        {"opened": ["08:30", "09:00"], "card": "At 09:00"},
        {"opened": ["08:30", "09:00"], "card": None},
        {"opened": ["08:30", "09:00"], "card": "At 09:00"}], \
        "a tap on another moment reads it; after a tap away the card is gone, so the next tap reads again"


def test_a_finger_slid_onto_a_moment_opens_it_with_one_tap():
    """Sliding sideways moves the card to the moment under the finger: that moment's card shows, so a tap opens it."""
    out = run(TAP_JS + """
send(hits[0], "pointerdown", "touch");
send(hits[3], "pointermove", "touch");
send(hits[3], "pointerup", "touch");
var slid = state();
tap(hits[3], "touch");
process.stdout.write(JSON.stringify({ slid: slid, tapped: state() }));
""")
    assert out == {"slid": {"opened": [], "card": "At 13:35"}, "tapped": {"opened": ["13:35"], "card": "At 13:35"}}


def test_a_mouse_click_opens_a_moment_at_once():
    out = run(TAP_JS + """
var out = [];
tap(hits[2], "mouse"); out.push(state().opened);
send(hits[3], "pointermove", "mouse");
tap(hits[3], "mouse"); out.push(state().opened);
send(hits[0], "click"); out.push(state().opened);
process.stdout.write(JSON.stringify(out));
""")
    assert out == [["09:30"], ["09:30", "13:35"], ["09:30", "13:35", "08:30"]], \
        "with or without a hover first, and a click with no press before it (as today's tests send)"
