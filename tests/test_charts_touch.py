"""A chart's hovercard on a touch screen (iPad and phone design §10.1): a finger on a chart reads the mark under it,
with the card floating above the finger and following it as it slides, kept inside the window; the mark it reads is
lit (`on`) as a hover lights it. The card stays when the finger lifts, until a touch away from the marks or a scroll.
A mouse keeps today's card beside the pointer. Runs charts.js in node on the fake DOM."""
from __future__ import annotations

from css_rules import app_css, media_block, rules, top_level
from test_web_overview import node_prelude, run_node

SETUP_JS = """
globalThis.innerWidth = 360;
globalThis.innerHeight = 780;
globalThis.layout = function (el) {
  if (el.classList.contains("hovercard")) {
    return { offsetWidth: 300, offsetHeight: 120 };
  }
  return { clientWidth: 340, scrollWidth: 340 };
};
var box = document.createElement("div");
document.body.appendChild(box);
Charts.lineChart(box, [{ name: "Tickets", values: [3, 5, 4] }], { tooltip: function (i) { return "Step " + i; } });
var hits = box.findAll(function (e) { return e.classList.contains("hit"); });
var outside = document.createElement("p");
document.body.appendChild(outside);
var under = null;   // what the finger or the pointer is over
document.elementFromPoint = function () { return under; };
function rectOf(g) { return g.findAll(function (e) { return e.classList.contains("hitarea"); })[0]; }
function fire(type, pointerType, x, y, over) {
  under = over;
  (document.listeners[type] || []).forEach(function (f) {
    f({ type: type, pointerType: pointerType, clientX: x, clientY: y, target: over || document.body });
  });
}
function card() { return document.body.findAll(function (e) { return e.classList.contains("hovercard"); })[0]; }
function state() {
  var c = card();
  return { shown: Boolean(c) && !c.hidden, left: c ? parseFloat(c.style.left) : null, top: c ? parseFloat(c.style.top) : null,
    title: c ? c.textContent : null,
    on: hits.map(function (g, i) { return g.classList.contains("on") ? i : -1; }).filter(function (i) { return i >= 0; }) };
}
"""


def run(script: str):
    return run_node(node_prelude() + SETUP_JS + script)


def test_a_finger_reads_the_mark_under_it_with_the_card_above_it():
    out = run("""
var out = {};
fire("pointerdown", "touch", 180, 400, rectOf(hits[1])); out.down = state();
fire("pointermove", "touch", 300, 400, rectOf(hits[2])); out.moved = state();
fire("pointerup", "touch", 300, 400, rectOf(hits[2])); out.up = state();
process.stdout.write(JSON.stringify(out));
""")
    # Centred above the finger, 36px clear of it: 180 - 300 / 2 = 30, 400 - 120 - 36 = 244.
    assert out["down"] == {"shown": True, "left": 30, "top": 244, "title": "Step 1", "on": [1]}
    assert out["moved"] == {"shown": True, "left": 52, "top": 244, "title": "Step 2", "on": [2]}, \
        "the card follows the finger to the next mark, and only that mark is lit"
    assert out["up"] == out["moved"], "lifting the finger leaves the card"


def test_the_card_stays_inside_the_window_and_goes_below_a_finger_near_the_top():
    out = run("""
var out = {};
fire("pointerdown", "touch", 10, 400, rectOf(hits[0])); out.left = state();
fire("pointerdown", "touch", 350, 400, rectOf(hits[2])); out.right = state();
fire("pointerdown", "touch", 180, 100, rectOf(hits[1])); out.top = state();
process.stdout.write(JSON.stringify(out));
""")
    assert out["left"]["left"] == 8, "8px from the left edge"
    assert out["right"]["left"] == 360 - 300 - 8, "8px from the right edge"
    assert out["top"]["top"] == 100 + 36, "no room above the finger: below it"
    assert out["top"]["on"] == [1]


def test_a_finger_sliding_off_the_marks_keeps_the_card_and_a_touch_away_or_a_scroll_hides_it():
    out = run("""
var out = {};
fire("pointerdown", "touch", 180, 400, rectOf(hits[1]));
fire("pointermove", "touch", 180, 400, null); out.slidOff = state();
fire("pointerdown", "touch", 20, 700, outside); out.away = state();
fire("pointerdown", "touch", 180, 400, rectOf(hits[1]));
(document.listeners.scroll || []).forEach(function (f) { f({ type: "scroll" }); }); out.scrolled = state();
process.stdout.write(JSON.stringify(out));
""")
    assert out["slidOff"]["shown"] is True and out["slidOff"]["on"] == [1]
    assert out["away"]["shown"] is False and out["away"]["on"] == []
    assert out["scrolled"]["shown"] is False and out["scrolled"]["on"] == []


def test_a_mouse_keeps_the_card_beside_the_pointer():
    out = run("""
globalThis.innerWidth = 1280;
var out = {};
fire("pointermove", "mouse", 100, 100, rectOf(hits[0])); out.over = state();
fire("pointerdown", "mouse", 500, 500, outside); out.pressed = state();
fire("pointermove", "mouse", 500, 500, outside); out.off = state();
process.stdout.write(JSON.stringify(out));
""")
    assert out["over"] == {"shown": True, "left": 116, "top": 116, "title": "Step 0", "on": []}, \
        "16px right of and below the pointer, as before; a hover lights the mark, not `on`"
    assert out["pressed"]["shown"] is True, "a mouse press does nothing to the card"
    assert out["off"]["shown"] is False


def test_the_card_is_never_wider_than_a_phone_less_its_edges():
    """Kept 8px inside each side (charts.js), the card can only fit if it is at most the window less 16px wide."""
    css = top_level(app_css())
    assert any("max-width: min(380px, calc(100vw - 2 * var(--sp-4)))" in " ".join(body.split())
               for body in rules(css, ".hovercard"))


def test_a_lit_mark_shows_as_a_hovered_one_and_a_chart_takes_sideways_slides_only():
    css = app_css()
    plain = top_level(css)
    assert any("touch-action: pan-y" in body for body in rules(plain, "svg.chart"))
    hover = media_block(css, "(hover: hover)")
    for part, declaration in ((".guide", "visibility: visible"), (".dot", "stroke-width: 3"),
                              (".bar", "opacity: 1"), (".dot.faint", "opacity: 1")):
        assert any(declaration in body for body in rules(hover, ".chart .hit:hover " + part)), part
        assert any(declaration in body for body in rules(plain, ".chart .hit.on " + part)), part


def test_moving_to_another_screen_puts_a_left_up_card_away():
    """A card a finger left up belongs to its screen: a change of address (another screen) hides it and its lit mark."""
    windows = "globalThis.windowListeners = {};\n" \
              "globalThis.addEventListener = function (type, f) { (windowListeners[type] = windowListeners[type] || []).push(f); };\n"
    out = run_node(windows + node_prelude() + SETUP_JS + """
var out = {};
fire("pointerdown", "touch", 180, 400, rectOf(hits[1])); out.read = state();
(windowListeners.hashchange || []).forEach(function (f) { f({ type: "hashchange" }); }); out.moved = state();
process.stdout.write(JSON.stringify(out));
""")
    assert out["read"]["shown"] is True and out["read"]["on"] == [1]
    assert out["moved"]["shown"] is False and out["moved"]["on"] == []
