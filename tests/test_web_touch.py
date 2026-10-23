"""Touch basics (iPad and phone design §6): the side menu folds on every touch screen and opens over a dimmed page,
fields are 16px so iOS doesn't zoom, controls are finger-sized, and hover styling applies only where there is hover.
The desktop (a mouse) keeps everything as it was."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from css_rules import app_css, media_block, rules, tokens, top_level
from test_web_overview import STATIC, js_function, node_prelude, run_node

FOLD = "(max-width: 1023px), (pointer: coarse)"
TOUCH = "(pointer: coarse)"


def without_hover_blocks(css: str) -> str:
    """`css` with every `@media (hover: hover) { ... }` block cut out."""
    out, i = [], 0
    for match in re.finditer(re.escape("@media (hover: hover)") + r"\s*\{", css):
        if match.start() < i:
            continue
        depth, j = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append(css[i:match.start()])
        i = j
    out.append(css[i:])
    return "".join(out)


def test_side_menu_folds_on_touch_at_any_width():
    css = app_css()
    fold = media_block(css, FOLD)
    assert any("display: block" in body for body in rules(fold, ".shell"))
    assert any("display: none" in body and "position: fixed" in body for body in rules(fold, ".sidenav"))
    assert any("display: flex" in body for body in rules(fold, ".sidenav.open"))
    assert any("display: inline-flex" in body for body in rules(fold, ".appbar .menu"))
    narrow_only = media_block(css, "(max-width: 1023px)")
    assert not rules(narrow_only, ".sidenav.open")


def test_touch_fields_are_16px():
    css = app_css()
    assert tokens(css)["fs-touch-field"] == "16px"
    touch = media_block(css, TOUCH)
    for selector in ("input", "select", "textarea"):
        assert any("font-size: var(--fs-touch-field)" in body for body in rules(touch, selector)), selector


def coarse_rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every selector of every rule inside an @media block whose query names (pointer: coarse)."""
    found = []
    for query in dict.fromkeys(q.strip() for q in re.findall(r"@media([^{]*)\{", css) if "pointer: coarse" in q):
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", media_block(css, query)):
            found += [(" ".join(s.split()), body) for s in selectors.split(",")]
    return found


def test_touch_targets_use_tap_min():
    css = app_css()
    assert tokens(css)["tap-min"] == "44px"
    touch = media_block(css, TOUCH)
    for selector in ("button", "button.small", "select", "a.pill", "button.pill", "button.th-sort", ".sidenav a",
                     "button.rank-name", ".pick-list label", ".days-pick label"):
        assert any("min-height: var(--tap-min)" in body for body in rules(touch, selector)), selector


def test_only_pills_that_are_controls_are_finger_sized():
    """A pill that is a link or a button is a control; a pill beside a date (1:1 Prep's Today, Tomorrow) is a label,
    and stays its own size on touch rather than a tall blob that wraps under its date."""
    tall = [selector for selector, body in coarse_rules(app_css()) if "min-height" in body]
    assert [s for s in tall if re.search(r"\.pill(?![\w-])", s)] == ["a.pill", "button.pill"]
    assert not any("cell-pill" in s for s in tall)


def test_work_day_tick_boxes_are_finger_sized():
    """Shifts' Work days tick boxes: each day's label (its tick box and name, the box's hit area) is at least a finger
    tall and wide on touch, and the days still wrap inside a card."""
    css = app_css()
    touch = media_block(css, TOUCH)
    assert any("min-height: var(--tap-min)" in body for body in rules(touch, ".days-pick label"))
    assert any("min-width: var(--tap-min)" in body for body in rules(touch, ".days-pick label"))
    assert any("flex-wrap: wrap" in body for body in rules(top_level(css), ".atable.cards .days-pick"))
    assert not [body for body in rules(top_level(css), ".days-pick label") if "min-" in body], "a mouse keeps its size"


def test_hover_only_under_hover_media():
    rest = without_hover_blocks(app_css())
    assert ":hover" not in rest
    # The hover rules are still there, each in place inside its own block.
    assert app_css().count("@media (hover: hover)") >= 30


def test_hover_blocks_keep_plain_selectors_outside():
    """A rule that listed a hover and a plain selector (the selected row) keeps the plain one for everyone."""
    css = top_level(app_css())
    assert any("background: var(--sel-tint)" in body for body in rules(css, ".atable tr.sel td"))
    assert any("color: var(--accent)" in body for body in rules(css, "button.th-sort.on"))


MENU_JS = """
var nav = document.createElement("nav"); nav.className = "sidenav";
var link = document.createElement("a"); nav.appendChild(link);
var scrim = document.createElement("div"); scrim.className = "nav-scrim"; scrim.hidden = true;
var bar = document.createElement("header"); bar.className = "appbar";
var button = document.createElement("button"); button.className = "menu"; bar.appendChild(button);
document.body.appendChild(nav); document.body.appendChild(scrim); document.body.appendChild(bar);
function fire(node, type, props) {
  (node.listeners[type] || []).forEach(function (f) { f(Object.assign({ target: node, preventDefault: function () {} }, props)); });
}
function state() {
  return { open: nav.classList.contains("open"), scrim: !scrim.hidden, body: document.body.classList.contains("menu-open"),
    expanded: button.getAttribute("aria-expanded") };
}
wireSidePanel();
var out = {};
fire(button, "click"); out.opened = state();
fire(scrim, "click"); out.scrim = state();
fire(button, "click"); fire(link, "click"); out.link = state();
fire(button, "click"); fire(document, "keydown", { key: "Escape" }); out.escape = state();
process.stdout.write(JSON.stringify(out));
"""


def test_menu_scrim_closes():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    source = js_function(js, "setMenuOpen") + "\n" + js_function(js, "wireSidePanel")
    out = run_node(node_prelude() + source + MENU_JS)
    assert out["opened"] == {"open": True, "scrim": True, "body": True, "expanded": "true"}
    closed = {"open": False, "scrim": False, "body": False, "expanded": "false"}
    assert out["scrim"] == closed
    assert out["link"] == closed
    assert out["escape"] == closed


SCENARIO = Path(__file__).resolve().parents[1] / ".eyes" / "scenarios" / "hosted-touch.yaml"

# Runs the scenario's "no list or table needs sideways scrolling" check against stand-in boxes: each `[wide, split]`,
# a box too narrow for what it holds or not, holding the all-techs split grid or not, at a window `width` wide.
WIDE_BOX_JS = """
function box(spec) {
  return { offsetParent: {}, scrollWidth: spec[0] ? 900 : 300, clientWidth: 300,
    querySelector: function (selector) { return spec[1] && selector === ".heat.split" ? {} : null; } };
}
function check(width, specs) {
  globalThis.innerWidth = width;
  globalThis.document = { querySelectorAll: function () { return specs.map(box); } };
  return eval(CHECK);
}
process.stdout.write(JSON.stringify(CASES.map(function (c) { return check(c[0], c[1]); })));
"""


@pytest.mark.skipif(not SCENARIO.exists(), reason="the Eyes scenarios are not in this copy of the repo")
def test_touch_scenario_lets_only_the_split_grid_scroll_and_only_from_1024px():
    """Review F12: hosted-touch's check that no box needs sideways scrolling exempts the all-techs grid's box (whose
    days part scrolls in the split grid, spec §7) only at 1024px and wider; below that the box holds the stacked grid,
    which must fit too."""
    text = SCENARIO.read_text(encoding="utf-8")
    expression = re.search(r"&no-wide-box >-\n\s+(.+)\n", text).group(1).strip()
    cases = [[360, [[True, True]]], [1023, [[True, True]]], [1024, [[True, True]]], [1366, [[True, True]]],
             [1366, [[True, False]]], [360, [[False, True], [False, False]]]]
    out = run_node(f"var CHECK = {json.dumps(expression)};\nvar CASES = {json.dumps(cases)};\n" + WIDE_BOX_JS)
    assert out == [False, False, True, True, False, True]


def test_scrim_in_page():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<div class="nav-scrim" hidden></div>' in html
    assert html.index('class="sidenav"') < html.index('class="nav-scrim"') < html.index('class="app"')
    assert json.dumps(tokens(app_css())["z-scrim"])


def test_moment_card_says_tap_again_on_touch():
    """On a touch screen the first tap on a day-chart moment reads it and a second tap opens the ticket (the owner's
    pick, 2026-10-23), so the card's foot says so; with a mouse it still says Click."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    stubs = "function joinActions() { return []; }\nfunction hhmmLabel() { return '8:30 AM'; }\n"   # only the foot matters
    script = node_prelude() + 'var TOUCH = "(pointer: coarse)";\n' + stubs + js_function(js, "momentCard") + """
var actions = [{ at: "2026-10-15T08:30", kind: "reply", ticket_id: 1, subject: "Printer", said: ["public reply"],
  changes: [] }];
var out = {};
window.matchMedia = function (q) { return { matches: q === "(pointer: coarse)" }; };
try { out.touch = momentCard(actions, "a@example.com").foot; } catch (e) { out.touch = "error: " + e.message; }
window.matchMedia = function () { return { matches: false }; };
try { out.mouse = momentCard(actions, "a@example.com").foot; } catch (e) { out.mouse = "error: " + e.message; }
process.stdout.write(JSON.stringify(out));
"""
    out = run_node(script)
    assert out == {"touch": "Tap again to open the ticket in Zendesk.", "mouse": "Click to open the ticket in Zendesk."}
