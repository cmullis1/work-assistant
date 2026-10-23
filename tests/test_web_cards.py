"""Rows as cards and Show all (iPad and phone design §8 and §9). The shared table box (`tableBox` in app.js) labels each
body cell with its column's header, turns the table into cards when it doesn't fit its box and back when it does
(rotation, a resized window), and on a narrow screen shows a long list's first rows with a Show all button under them."""
from __future__ import annotations

import json
import re

import test_web_settings_app as settings
import test_web_team_activity as activity
from css_rules import app_css, media_block, rules, tokens, top_level, values
from test_queries import version  # noqa: F401  (fixture)
from test_web_overview import STATIC, js_function, node_prelude, run_node
from test_web_test_view import run_page

NARROW = "(max-width: 1023px)"
HELPERS = ("h", "append", "tableBox", "labelCells", "isFigure", "isBlank", "headLabel", "hasField", "firstRows",
           "focusRow", "fitCards", "fitNow", "tableRoles", "tableChanged")


def helpers() -> str:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    return "\n".join(js_function(js, name) for name in HELPERS)


# The fake layout: the ruler (and so the box) is `width` px wide; the table needs `needs` px as a table and fits its
# box as cards. `resize(w)` lays the page out at `w` and calls each ResizeObserver, as the browser does before it
# paints.
HARNESS_JS = """
var observers = [];
globalThis.ResizeObserver = function (callback) {
  this.observe = function (el) { observers.push({ callback: callback, el: el }); };
  this.disconnect = function () {};
};
var width = 0, needs = 900;
globalThis.layout = function (el) {
  if (el.className === "fit-ruler") {
    return { clientWidth: width, scrollWidth: width };
  }
  var table = el.children.filter(function (c) { return c.tagName === "TABLE"; })[0];
  if (!table) {
    return { clientWidth: 0, scrollWidth: 0 };
  }
  return { clientWidth: width, scrollWidth: Math.max(width, table.classList.contains("cards") ? 0 : needs) };
};
function resize(w) {
  width = w;
  observers.forEach(function (o) { o.callback([{ target: o.el }]); });
}
function rowsOf(n) {
  var rows = [];
  for (var i = 0; i < n; i++) {
    rows.push(h("tr", null, h("td", { text: "9/" + (i + 1) }), h("td", null, h("a", { href: "#", text: "#8040" + i })),
      h("td", { class: "num", text: "4:28" }), h("td", { text: "Normal" })));
  }
  return rows;
}
function callsTable(n) {
  return h("table", { class: "atable" }, h("thead", null, h("tr", null, h("th", { text: "Day" }),
    h("th", { text: "Ticket" }), h("th", { class: "num", text: "Talk" }), h("th", { text: "Priority" }))),
    h("tbody", null, rowsOf(n)));
}
function click(el) {
  (el.listeners.click || []).forEach(function (f) { f({ target: el }); });
}
function rolesOf(table) {
  return {
    table: table.getAttribute("role"),
    groups: table.findAll(function (e) { return e.tagName === "THEAD" || e.tagName === "TBODY"; })
      .map(function (e) { return e.getAttribute("role"); }),
    rows: table.querySelectorAll("tr").map(function (e) { return e.getAttribute("role"); }),
    heads: table.querySelectorAll("th").map(function (e) { return e.getAttribute("role"); }),
    cells: table.querySelectorAll("td").map(function (e) { return e.getAttribute("role"); })
  };
}
function shot(box, table) {
  return { cards: table.classList.contains("cards"), roles: rolesOf(table) };
}
"""


def run(script: str):
    return run_node(node_prelude() + helpers() + HARNESS_JS + script)


LABELS_JS = """
var sort = h("th", { class: "num" }, h("button", { type: "button", class: "th-sort on", text: "Talk ▲" }),
  h("span", { class: "th-days", text: "Oct 11 – Oct 17" }));
var table = h("table", { class: "atable" },
  h("thead", null, h("tr", null, h("th", { text: "Day" }), h("th", { text: "Ticket" }), sort, h("th", { text: "Priority" }))),
  h("tbody", null,
    h("tr", null, h("td", null, h("button", { type: "button", text: "9/22" })), h("td", null, h("a", { href: "#", text: "#80405" })),
      h("td", { class: "num", text: "4:28" }), h("td", { text: "Normal" })),
    h("tr", null, h("td", { text: "9/23" }), h("td", null, h("a", { href: "#", text: "#80404" })),
      h("td", { class: "num", text: "Oct 18, 11:05 AM" }), h("td", null, h("button", { type: "button", text: "Edit" }))),
    h("tr", null, h("td", { text: "9/24" }), h("td", null, h("select"), h("button", { type: "button", text: "Go" })),
      h("td", { class: "num" }, h("button", { type: "button", text: "Copy row" })), h("td", null, h("input"))),
    h("tr", null, h("td", { colspan: "4", text: "No calls" }))));
labelCells(table);
process.stdout.write(JSON.stringify(table.querySelectorAll("tbody tr").map(function (tr) {
  return tr.children.map(function (td) { return [td.getAttribute("data-label"), td.className]; });
})));
"""


def test_label_cells():
    """Each body cell carries its column's header (a sort header's name, without its arrow or days); a cell of buttons
    (no field, no link) is the row's actions, the first cell is neither, and a row across the columns is left alone.
    Talk holds a long figure in one row, so it is a line in every row (test_a_column_is_figures_or_lines)."""
    rows = run(LABELS_JS)
    assert rows[0] == [["Day", ""], ["Ticket", ""], ["Talk", "num"], ["Priority", ""]]
    assert rows[1] == [["Day", ""], ["Ticket", ""], ["Talk", "num"], ["Priority", "act"]], \
        "a long figure is a line; a cell with only a button is the actions"
    assert rows[2] == [["Day", ""], ["Ticket", ""], ["Talk", "num act"], ["Priority", ""]], \
        "a field and a button are a line; a number cell of buttons is the actions, not a figure"
    assert rows[3] == [[None, ""]], "a row across the columns is left alone"


def test_a_column_is_figures_or_lines():
    """Review F5: a column is figures (small boxes) only when every row's cell is a short number with no field;
    otherwise it is a line in every card, so cards of one list look alike. First – last action holds "-" on a day
    with no activity and "8:05 AM – 4:37 PM" on others: a line in every card. Signed in and Tickets stay figures."""
    rows = run("""
var head = h("tr", null, ["Day", "Signed in", "First – last action", "Tickets"].map(function (text, i) {
  return h("th", { class: i ? "num" : null, text: text });
}));
function line(day, signin, span, tickets) {
  return h("tr", null, h("td", { text: day }), h("td", { class: "num", text: signin }),
    h("td", { class: "num", text: span }), h("td", { class: "num", text: tickets }));
}
var table = h("table", { class: "atable" }, h("thead", null, head), h("tbody", null,
  line("Mon Oct 5", "8:01 AM", "8:05 AM – 4:37 PM", "11"), line("Tue Oct 6", "-", "-", "0"),
  line("Wed Oct 7", "7:58 AM", "8:02 AM – 4:51 PM", "9")));
labelCells(table);
var body = table.querySelector("tbody");
body.appendChild(line("Thu Oct 8", "8:00 AM", "-", "Twelve tickets today"));
labelCells(table);   // drawn again with a long Tickets cell: Tickets is lines from now on
process.stdout.write(JSON.stringify(table.querySelectorAll("tbody tr").map(function (tr) {
  return tr.children.slice(1).map(function (td) { return td.className; });
})));
""")
    assert rows == [["num fig", "num", "num"]] * 4


def test_empty_card_lines_are_hidden(tmp_path, conn, version):
    """Review F4: as cards, a cell with no text (the Flags cell of a day with no flags, an empty `.chips`) shows
    neither its label nor its line: labelCells marks it `blank`. On Everyone and on Day by day."""
    avery = activity.cell(activity.DAY, tickets=6, signin="08:04", first="08:10", last="16:35", gap_min=150,
                          gap_start="11:00", flags=[activity.flag("long_gap", 150)])
    casey = activity.cell(activity.DAY, tickets=2, signin="08:00", first="08:20", last="12:00")
    body = activity.activity([activity.DAY], [activity.row(activity.AVERY, "Avery Cole", [avery]),
                                              activity.row(activity.CASEY, "Casey Nguyen", [casey])],
                             preset="custom", label="Mon Oct 5")
    hash_, api_url = activity.custom([activity.DAY])
    read = """
run(async function () {
  fit();
  var table = panelTitled(%s).querySelectorAll("table")[0];
  return { cards: table.classList.contains("cards"), flags: table.querySelectorAll("tbody tr").map(function (tr) {
    var td = tr.children[10];
    return [td.getAttribute("data-label"), td.className, td.querySelectorAll(".chips").length,
      td.querySelectorAll(".chip").length];
  }) };
});
"""
    everyone = activity.team(tmp_path, hash_, {api_url: body}, NARROW_JS + read % '"Everyone"')
    assert everyone == {"cards": True, "flags": [["Flags", "", 1, 1], ["Flags", "blank", 1, 0]]}, \
        "Casey's Flags cell holds an empty .chips and nothing else"
    days = activity.day_by_day_page(tmp_path, NARROW_JS + read % '"Day by day"')
    assert days["cards"] is True
    assert {(cls, chips > 0) for _, cls, _, chips in days["flags"]} == {("blank", False), ("", True)}, \
        "a day with no flags has a blank Flags cell, a flagged day doesn't"
    css = top_level(app_css())
    assert values(css, ".atable.cards tbody tr td.blank", "display") == ["none"]
    selectors = [" ".join(s.split()) for found in re.findall(r"([^{}]+)\{", app_css()) for s in found.split(",")]
    assert all(s.startswith(".atable.cards ") for s in selectors if "blank" in s), "a table on the desktop is unchanged"


def test_show_all_moves_focus_to_the_first_row_it_shows():
    """Review F9: Show all goes away once clicked, so focus moves to the first row it showed: its first link or button
    (in the row's order), or the row itself, focusable by script only (tabindex -1), when it has neither."""
    out = run("""
function boxed(table) {
  var box = tableBox(table, { list: true, first: 10, noun: "rows" });
  document.body.appendChild(box);
  click(box.querySelector("button.show-all"));
  return box;
}
var out = {};
boxed(callsTable(12));
out.linked = [focused.tagName, focused.textContent];
var buttoned = callsTable(12);
var eleventh = buttoned.querySelectorAll("tbody tr")[10];
eleventh.children[0].textContent = "";
eleventh.children[0].appendChild(h("button", { type: "button", text: "9/11" }));
boxed(buttoned);
out.buttoned = [focused.tagName, focused.textContent];
var plain = h("table", { class: "atable" }, h("thead", null, h("tr", null, h("th", { text: "Day" }))),
  h("tbody", null, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(function (n) { return h("tr", null, h("td", { text: "9/" + n })); })));
boxed(plain);
out.plain = [focused.tagName, focused.textContent, focused.getAttribute("tabindex")];
var list = h("div", null, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(function (n) {
  return h("div", { class: "rating-item", text: "Rating " + n });
}));
boxed(list);
out.list = [focused.tagName, focused.textContent, focused.getAttribute("tabindex")];
process.stdout.write(JSON.stringify(out));
""")
    assert out["linked"] == ["A", "#804010"]
    assert out["buttoned"] == ["BUTTON", "9/11"], "the first control in the row, a button before the link"
    assert out["plain"] == ["TR", "9/11", "-1"]
    assert out["list"] == ["DIV", "Rating 11", "-1"]


def test_loading_lists_drop_a_stale_show_all():
    """Review F11: Settings' Techs, Team leads and Time off lists drawn again as their one "Loading…" line (a new read
    began) work out Show all again, so no "Show all 12 …" is left under it."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    source = "\n".join(js_function(js, name) for name in ("renderRoster", "renderLeads", "renderTimeOff"))
    out = run(source + """
var view = {};
var out = {};
[["rosterBody", renderRoster], ["peopleBody", renderLeads], ["offBody", renderTimeOff]].forEach(function (pair) {
  var table = callsTable(12);
  var box = tableBox(table, { list: true, first: 10, noun: "rows" });
  document.body.appendChild(box);
  view[pair[0]] = table.querySelector("tbody");
  var before = box.querySelectorAll("button.show-all").length;
  pair[1]();
  out[pair[0]] = { before: before, after: box.querySelectorAll("button.show-all").length,
    rows: view[pair[0]].children.map(function (tr) { return [tr.className, tr.textContent]; }) };
});
process.stdout.write(JSON.stringify(out));
""")
    for key in ("rosterBody", "peopleBody", "offBody"):
        assert out[key] == {"before": 1, "after": 0, "rows": [["", "Loading…"]]}, key


def test_cards_only_when_too_wide():
    out = run("""
var narrow = callsTable(3);
var box = tableBox(narrow, {});
document.body.appendChild(box);
var wide = callsTable(3);
var box2 = tableBox(wide, {});
document.body.appendChild(box2);
var built = shot(box, narrow);
width = 300;
observers[0].callback([{ target: observers[0].el }]);
var tight = shot(box, narrow);
width = 1200;
observers[1].callback([{ target: observers[1].el }]);
process.stdout.write(JSON.stringify({ built: built, tight: tight, roomy: shot(box2, wide), cls: box.className,
  kids: box.children.map(function (e) { return e.tagName === "TABLE" ? "TABLE" : e.className; }) }));
""")
    assert out["cls"] == "scroll"
    assert out["kids"] == ["fit-ruler", "TABLE"], "the ruler sits at the top of the box, before the table"
    assert out["built"]["cards"] is False, "nothing is decided before the box is laid out"
    assert out["tight"]["cards"] is True, "a table wider than its box becomes cards"
    assert out["tight"]["roles"] == {"table": "table", "groups": ["rowgroup", "rowgroup"], "rows": ["row"] * 4,
                                     "heads": ["columnheader"] * 4, "cells": ["cell"] * 12}, \
        "cards keep the table's meaning for a screen reader"
    assert out["roomy"]["cards"] is False, "a table that fits stays a table"
    assert out["roomy"]["roles"] == {"table": None, "groups": [None, None], "rows": [None] * 4, "heads": [None] * 4,
                                     "cells": [None] * 12}


def test_cards_follow_resize():
    """An iPad turned sideways: cards go back to a table when the box widens, and come back when it narrows."""
    out = run("""
var table = callsTable(2);
var box = tableBox(table, {});
document.body.appendChild(box);
var shots = [300, 1200, 1200, 300].map(function (w) { resize(w); return shot(box, table); });
process.stdout.write(JSON.stringify({ shots: shots, observed: observers.map(function (o) { return o.el.className; }) }));
""")
    assert [s["cards"] for s in out["shots"]] == [True, False, False, True]
    assert out["shots"][1]["roles"]["table"] is None and out["shots"][1]["roles"]["cells"] == [None] * 8
    assert out["shots"][3]["roles"]["table"] == "table"
    # The width is watched on the ruler, whose size the switch never changes (a ResizeObserver loop otherwise).
    assert out["observed"] == ["fit-ruler"]


SHOW_ALL_JS = """
function build(n, opts) {
  var table = callsTable(n);
  var box = tableBox(table, opts);
  document.body.appendChild(box);
  return { box: box, table: table };
}
function state(b) {
  var buttons = b.box.querySelectorAll("button.show-all");
  return {
    cls: b.box.className,
    more: b.table.querySelectorAll("tbody tr").map(function (tr) { return tr.classList.contains("more"); }),
    buttons: buttons.map(function (e) { return [e.className, e.textContent]; }),
    last: b.box.children[b.box.children.length - 1].tagName
  };
}
var out = {};
var long = build(31, { list: true, first: 10, noun: "calls" });
out.long = state(long);
width = 300;
click(long.box.querySelector("button.show-all"));
out.clicked = state(long);
out.fitted = long.table.classList.contains("cards");
out.ten = state(build(10, { list: true, first: 10, noun: "calls" }));
out.eleven = state(build(11, { first: 10, noun: "calls" }));
out.none = state(build(0, { list: true, tall: true, first: 10, noun: "calls" }));
out.one = state(build(1, { first: 10, noun: "calls" }));
var redrawn = build(3, { list: true, first: 2, noun: "techs" });
var body = redrawn.table.querySelector("tbody");
body.textContent = "";
rowsOf(4).forEach(function (tr) { body.appendChild(tr); });
tableChanged(body);
out.grown = state(redrawn);
out.grownLabel = body.children[3].children[2].getAttribute("data-label");
body.textContent = "";
rowsOf(2).forEach(function (tr) { body.appendChild(tr); });
tableChanged(body);
out.shrunk = state(redrawn);
var loose = h("tbody");
tableChanged(loose);
var picked = callsTable(12);
picked.querySelectorAll("tbody tr")[6].className = "rowlink sel";
out.picked = tableBox(picked, { list: true, first: 4, noun: "periods" }).className;
process.stdout.write(JSON.stringify(out));
"""


def test_show_all():
    out = run(SHOW_ALL_JS)
    assert out["long"]["cls"] == "scroll list-scroll"
    assert out["long"]["more"] == [False] * 10 + [True] * 21, "rows 11-31 wait behind Show all"
    assert out["long"]["buttons"] == [["small show-all", "Show all 31 calls"]]
    assert out["long"]["last"] == "BUTTON", "the button is under the rows"
    assert out["clicked"]["cls"] == "scroll list-scroll all", "Show all shows every row"
    assert out["fitted"] is True, "the rows shown are fitted again"
    for key in ("ten", "none", "one"):
        assert out[key]["buttons"] == [] and not any(out[key]["more"]), key
    assert out["none"]["cls"] == "scroll list-scroll tall"
    assert out["eleven"]["buttons"] == [["small show-all", "Show all 11 calls"]]
    assert out["eleven"]["more"] == [False] * 10 + [True]
    # A list drawn again (its answer came) is worked out again: one button, for its new count, and new rows labelled.
    assert out["grown"]["buttons"] == [["small show-all", "Show all 4 techs"]]
    assert out["grown"]["more"] == [False, False, True, True]
    assert out["grownLabel"] == "Talk"
    assert out["shrunk"]["buttons"] == [] and out["shrunk"]["more"] == [False, False]
    assert out["picked"] == "scroll list-scroll all", "a picked row past the first ones is never hidden"


def test_cards_css():
    css = app_css()
    wide = top_level(css)
    assert tokens(css)["card-min"] == "300px"
    # Cards apply by fit at any width, so they live outside every media block.
    assert any("var(--card-min)" in body for body in rules(wide, ".atable.cards tbody"))
    assert values(wide, ".atable.cards tbody tr", "display") == ["grid"]
    assert any("attr(data-label)" in body for body in rules(wide, ".atable.cards tbody tr td::before"))
    assert any("attr(data-label)" in body for body in rules(wide, ".atable.cards tbody tr td.fig::after"))
    assert values(wide, ".atable.cards tbody tr td.act", "text-align") == ["right"]
    assert values(wide, ".atable.cards tbody tr td:empty", "display") == ["none"]
    for state in ("flagrow", "failrow", "sel", "restrow"):
        assert rules(wide, ".atable.cards tbody tr." + state), state
    assert any("position: absolute" in body for body in rules(wide, ".atable.cards thead th")), \
        "headers stay for screen readers"
    assert any("position: static" in body for body in rules(wide, ".atable.cards thead th:has(button)")), \
        "a header that acts (sort, Copy all) shows above the cards"
    # Show all is narrow-only: wider, the button never shows and every row does.
    assert values(wide, ".show-all", "display") == ["none"]
    narrow = media_block(css, NARROW)
    assert values(narrow, ".scroll:not(.all) .more", "display") == ["none"]
    assert values(narrow, ".scroll:not(.all) > .show-all", "display") == ["block"]
    assert not rules(wide, ".scroll:not(.all) .more")
    # Below 1024px a list never scrolls inside the page.
    assert values(narrow, ".list-scroll", "max-height") == ["none"]
    assert values(narrow, ".list-scroll.tall", "max-height") == ["none"]


def test_card_fields_take_the_line():
    """Settings as cards (design §8, mockup 11): a field in a card's line (Hours' times, Effective from's date, a
    choice, a number) is as wide as the line, at any width; a day's tick boxes keep their size and wrap."""
    css = app_css()
    wide = top_level(css)
    line = ".atable.cards td:not(.fig):not(.act) "
    assert values(wide, line + 'input:not([type="checkbox"]):not([type="radio"])', "width") == ["100%"]
    assert values(wide, line + "select", "width") == ["100%"]
    assert values(wide, line + "textarea", "width") == ["100%"]
    assert "100%" not in values(wide, ".atable.cards td input", "width"), "tick boxes keep their own size"
    assert values(wide, ".atable.cards .days-pick", "flex-wrap") == ["wrap"]


# -- the hosted screens' tables (task IP3) ----------------------------------------------------------------------------

# Each builds its tables in tableBox. allTechsDayView (Everyone) and dayByDay (Day by day) go through dayTable.
HOSTED = ("techsPanel", "showShiftsPreview", "teamLeadsPanel", "shiftTable", "timeOffPanel", "prepSchedulePanel",
          "teamsPanel", "flagsByTech", "everyFlag", "dayTable", "actionsPanel", "recentDaysPanel", "openTicketsPanel",
          "rankingPanel", "prepTable", "pastPeriodsPanel", "answeredPanel", "ratingsPanel", "agingPanel",
          "daysOutPanel")
CALL_QA = ("agentsPanel", "agentsTable", "checksPanel", "drawTicketList", "scorecardPanel", "fillChecks", "fillVersions",
           "testsTable", "historyPanel", "disagreePanel", "judgmentsPanel", "compareCallsPanel", "compareCallsTable",
           "testCallView")


def test_hosted_tables_use_table_box():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for name in HOSTED + ("allTechsDayView", "dayByDay", "listTable"):
        source = js_function(js, name)
        assert 'class: "scroll' not in source and 'class: "list-scroll' not in source, name
    for name in HOSTED:
        assert "tableBox(" in js_function(js, name), name
    for name in ("allTechsDayView", "dayByDay"):
        assert "dayTable(" in js_function(js, name), name
    # Long lists show their first rows on a narrow screen; Settings' forms (Shifts, Time off, 1:1 schedule) every row.
    for name, first in (("techsPanel", 'first: 10, noun: "techs"'), ("everyFlag", 'first: 10, noun: "flags"'),
                        ("dayByDay", 'first: 10, noun: "days"'), ("pastPeriodsPanel", 'first: 4, noun: "periods"'),
                        ("answeredPanel", 'first: 10, noun: "calls"'), ("ratingsPanel", 'first: 10, noun: "ratings"'),
                        ("agingPanel", 'first: 10, noun: "tickets"'), ("daysOutPanel", 'first: 10, noun: "days"')):
        assert first in js_function(js, name), name
    for name in ("shiftTable", "timeOffPanel", "prepSchedulePanel", "allTechsDayView"):
        assert "first:" not in js_function(js, name), name


def test_call_qa_tables_not_opted_in():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for name in CALL_QA:
        assert "tableBox(" not in js_function(js, name), name


# A narrow page: every table box's ruler is 300px wide, and a table in a box needs TABLE_W px unless it shows as cards.
# `fit()` calls each table box's ResizeObserver (on its ruler), as the browser does once the page is laid out.
NARROW_JS = """
var TABLE_W = 900;
var observers = [];
globalThis.ResizeObserver = function (callback) {
  this.observe = function (el) { observers.push({ callback: callback, el: el }); };
  this.disconnect = function () {};
};
globalThis.layout = function (el) {
  if (el.className === "fit-ruler") {
    return { clientWidth: 300, scrollWidth: 300 };
  }
  var table = el.children.filter(function (c) { return c.tagName === "TABLE"; })[0];
  if (!table) {
    return { clientWidth: 0, scrollWidth: 0 };
  }
  return { clientWidth: 300, scrollWidth: table.classList.contains("cards") ? 300 : TABLE_W };
};
function fit() {
  observers.filter(function (o) { return o.el.className === "fit-ruler"; })
    .forEach(function (o) { o.callback([{ target: o.el }]); });
}
"""


def test_open_tickets_never_spill(tmp_path, conn, version):
    """A tech's day on a phone: the open tickets table used to push the whole page sideways. It sits in a box now,
    and turns into cards when it doesn't fit."""
    tickets = [{"ticket_id": 21, "subject": "Login loop", "status": "open", "last_acted": "2026-10-05T10:00",
                "age_days": 12, "aging": True}]
    out = activity.day_page(tmp_path, activity.AVERY, activity.FULL_DAY,
                            activity.day_body(activity.FULL_DAY, activity.FULL_ACTIONS, tickets=tickets),
                            NARROW_JS + "TABLE_W = 450;\n" + """
run(async function () {
  fit();
  var p = panelTitled("Open tickets assigned to Avery now");
  var table = p.querySelectorAll("table")[0];
  return { cls: table.className, box: table.parentNode.className, inPanel: table.parentNode.parentNode === p,
    labels: p.querySelectorAll("tbody td").map(function (td) { return td.getAttribute("data-label"); }) };
});
""")
    assert out == {"cls": "atable cards", "box": "scroll plain", "inPanel": True,
                   "labels": ["Ticket", "Status", "Avery last acted"]}


def test_shift_save_reads_every_row(tmp_path):
    """Settings' shifts as cards on a narrow screen: Save still reads every tech's row (drawn once the shifts came,
    then labelled and fitted again). Shifts get no Show all: every row shows."""
    twelve = [f"tech{n:02d}@example.com" for n in range(12)]
    shifts = {"techs": [{"email": e, "name": f"Tech {n:02d}", "versions": []} for n, e in enumerate(twelve)]}
    responses = settings.responses_for(tmp_path, team=twelve, shifts=shifts, time_off={"items": []}, schedule=[])
    out = run_page("#/settings", responses, settings.HELPERS + NARROW_JS + """
run(async function () {
  fit();
  var table = all(teamEl(), function (e) { return e.tagName === "TABLE"; })[0];
  var rows = techRows();
  click(teamEl(), "Save team");
  await settle();
  var sent = {};
  writes().forEach(function (w) { if (w[0] === "PUT") { sent[w[1].split("/")[3]] = true; } });
  return { cards: table.classList.contains("cards"), rows: rows.length,
    labelled: rows.filter(function (r) { return r.children[0].getAttribute("data-label") === "Tech"; }).length,
    more: rows.filter(function (r) { return r.classList.contains("more"); }).length, sent: Object.keys(sent).length };
});
""")
    assert out == {"cards": True, "rows": 12, "labelled": 12, "more": 0, "sent": 12}


def test_card_row_click_still_opens_day(tmp_path, conn, version):
    """A card is the row: a click anywhere on it but its link or buttons opens that tech's day, as today."""
    hash_, api_url = activity.custom(activity.iso_days("2026-10-04", 8))
    out = activity.team(tmp_path, hash_, {api_url: activity.flagged_days(8)}, NARROW_JS + """
run(async function () {
  fit();
  var p = panels().filter(function (p) { return /^Every flag/.test(titleOf(p)); })[0];
  var table = p.querySelectorAll("table")[0];
  var tr = p.querySelectorAll("tbody tr")[1];
  (tr.listeners.click || []).forEach(function (f) { f({ target: tr.children[3] }); });
  return { cards: table.classList.contains("cards"), hash: location.hash, act: tr.children[4].className,
    box: table.parentNode.className };
});
""")
    assert out == {"cards": True, "hash": activity.day_hash(activity.AVERY, "2026-10-06"), "act": "num act",
                   "box": "scroll list-scroll"}


def test_flags_by_tech_cards_leave_out_the_kinds_at_zero(tmp_path, conn, version):
    """Flags by tech as cards (mockup 05): a kind a tech never had (td.zero) is left out, the others read as chips
    "Early finish · 1", a sick day in the failure colour, and the total sits on the tech's line."""
    hash_, api_url = activity.custom(activity.iso_days("2026-10-04", 8))
    out = activity.team(tmp_path, hash_, {api_url: activity.flagged_days(8)}, NARROW_JS + """
run(async function () {
  fit();
  var table = panelTitled("Flags by tech").querySelectorAll("table")[0];
  return { cls: table.className, rows: table.querySelectorAll("tbody tr").map(function (tr) {
    return tr.children.map(function (td) { return td.className; }); }) };
});
""")
    assert out["cls"] == "atable flag-counts cards"
    assert out["rows"][0] == ["", "num fig", "num zero fig", "num fig", "num zero fig", "num fig", "num fig"]
    css = top_level(app_css())
    assert values(css, ".atable.cards td.zero", "display") == ["none"]
    assert values(css, ".atable.cards.flag-counts tbody tr td.fig:not(:last-child)", "background") == \
        ["var(--attention-tint)"]
    assert values(css, ".atable.cards.flag-counts tbody tr td:has(.chip.fail)", "background") == ["var(--failure-tint)"]
    assert any('attr(data-label) " · "' in body
               for body in rules(css, ".atable.cards.flag-counts tbody tr td.fig:not(:last-child)::before"))


def test_every_flag_cards_are_compact(tmp_path, conn, version):
    """The owner's pick (mockup 05): as cards, Every flag (and a week's Flags) is a compact card per flag: the day (its
    link) on the left and the tech on the right of the title line, what was recorded as a muted line under it with no
    label, the flag's chip on the left and Mark as day off on the right of the foot line, the row's tint and edge kept.
    The same table with a class of its own (`flag-list`), drawn by app.css alone; as a table nothing changes."""
    hash_, api_url = activity.custom(activity.iso_days("2026-10-04", 8))
    out = activity.team(tmp_path, hash_, {api_url: activity.flagged_days(8)}, NARROW_JS + """
run(async function () {
  fit();
  var table = panels().filter(function (p) { return /^Every flag/.test(titleOf(p)); })[0].querySelectorAll("table")[0];
  var tr = table.querySelectorAll("tbody tr")[1];
  return { cls: table.className, row: tr.className, cells: tr.children.map(function (td) { return td.className; }) };
});
""")
    assert out == {"cls": "atable flag-list cards", "row": "rowlink flagrow",
                   "cells": ["", "", "", "muted", "num act"]}
    css = top_level(app_css())
    card = ".atable.cards.flag-list tbody tr"
    assert values(css, card, "grid-template-areas") == ['"day tech" "seen seen" "flag act"']
    for n, area in ((1, "day"), (2, "tech"), (3, "flag"), (4, "seen"), (5, "act")):
        assert values(css, f"{card} td:nth-child({n})", "grid-area") == [area], area
    assert values(css, card + " td:nth-child(2)", "text-align") == ["right"], "the tech on the right"
    assert values(css, card + " td::before", "content") == ["none"], "no line is labelled"
    assert values(css, card + " td", "padding-left") == ["0"]
    # Every rule naming flag-list styles the cards only: the desktop table is drawn as before.
    selectors = [" ".join(s.split()) for found in re.findall(r"([^{}]+)\{", app_css()) for s in found.split(",")]
    named = [s for s in selectors if "flag-list" in s]
    assert named and all(s.startswith(".atable.cards.flag-list ") for s in named), named


def test_plain_boxes_keep_their_rendering():
    """Lists that never had a positioned .scroll box on master keep an unpositioned one (tableBox's `plain`): a
    positioned scroll box gets its own layer in Chrome and draws its text differently, which changed the desktop."""
    from css_rules import app_css, rules, top_level
    assert any("position: static" in body for body in rules(top_level(app_css()), ".scroll.plain"))
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for name in ("recentDaysPanel", "openTicketsPanel", "answeredPanel", "ratingsPanel", "agingPanel", "daysOutPanel"):
        assert "plain: true" in js_function(js, name), name
