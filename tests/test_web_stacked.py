"""Activity's grids stacked for a narrow screen (iPad and phone design §7, §8 "One tech's calendar") and the number
tiles two to a row on a phone (§8b). app.js builds the stacked form beside the three-part grid (or one tech's
calendar table) in the same box, and app.css shows one or the other by the width alone, so a grid follows the
cutoff live as an iPad turns or a window is resized: nothing is rebuilt. The pages run in node on the fake DOM
through the Team activity harness (test_web_team_activity)."""
from __future__ import annotations

from urllib.parse import urlencode

from css_rules import app_css, media_block, top_level, values
from test_queries import version  # noqa: F401  (fixture)
from test_web_team_activity import (AVERY, BLAKE, READ, WEEK, activity, cell, custom, flag, iso_days, long_body, row,
                                    snapshot, team, tech_body, url, week_body)

NARROW = "(max-width: 1023px)"
PHONE = "(max-width: 640px)"

STACK = READ + """
function stack() { return screenEl.querySelectorAll(".heat-stack")[0] || null; }
// The stacked grid as a test reads it: its head line, and each block's heading, sum and slots (a slot is filled when
// it holds a cell, empty for a calendar's blank days).
function readStack(s) {
  var headRow = s.children.filter(function (c) { return c.classList.contains("hs-head"); })[0];
  return { cls: s.className.split(" "), role: s.getAttribute("role"), label: s.getAttribute("aria-label"),
    style: s.getAttribute("style"), box: s.parentNode.className,
    beside: s.parentNode.children.map(function (c) { return c.className; }),
    head: headRow ? { role: headRow.getAttribute("role"),
      cells: headRow.children.map(function (c) { return [c.getAttribute("role"), c.textContent]; }) } : null,
    blocks: s.querySelectorAll(".hs-block").map(function (b) {
      var line = b.querySelectorAll(".hs-line")[0];
      var who = line.children[0];
      var sum = line.querySelectorAll(".hs-sum")[0];
      var link = who.querySelectorAll("a")[0];
      var slots = b.querySelectorAll(".hs-cells")[0].children;
      return { role: b.getAttribute("role"), who: [who.getAttribute("role"), who.textContent],
        link: link ? link.getAttribute("href") : null,
        sum: [sum.getAttribute("role"), sum.textContent],
        flags: sum.querySelectorAll("b").map(function (n) { return n.className; }),
        slots: slots.map(function (sl) { return sl.getAttribute("role"); }),
        filled: slots.map(function (sl) { return sl.children.length > 0; }),
        cells: hcs(b) };
    }) };
}
"""


def stacked(tmp_path, start_hash: str, bodies: dict, reader: str):
    return team(tmp_path, start_hash, bodies, STACK + "run(async function () {\n" + reader + "\n});")


def without_date(cells: list[dict]) -> list[dict]:
    return [{k: v for k, v in c.items() if k != "date"} for c in cells]


# -- all techs, up to a week ---------------------------------------------------------------------------------


def test_a_week_stacks_a_block_per_tech_under_one_head_line_of_the_days(tmp_path, conn, version):
    out = stacked(tmp_path, "#/team", {url(): week_body()}, """
  var split = heat();
  return { stack: readStack(stack()), split: bodyRows(split).map(function (tr) {
    return { cells: hcs(tr), link: tr.querySelectorAll(".who a")[0].getAttribute("href") }; }) };
""")
    s = out["stack"]
    assert s["cls"] == ["heat-stack"]
    assert s["role"] == "table" and s["label"] == "Tickets touched per day"
    assert s["style"] == "--cols: 7", "a week is 7 equal columns"
    assert s["box"] == "scroll" and s["beside"] == ["heat split", "heat-stack"], \
        "both forms sit in the grid's box; app.css shows one by the width"
    assert s["head"] == {"role": "row", "cells": [["columnheader", d] for d in
                                                  ("Sun4", "Mon5", "Tue6", "Wed7", "Thu8", "Fri9", "Sat10")]}
    avery, blake = s["blocks"]
    assert avery["role"] == "row" and avery["who"] == ["rowheader", "Avery Cole"]
    assert blake["who"] == ["rowheader", "Blake Moreno"]
    assert [avery["link"], blake["link"]] == [line["link"] for line in out["split"]], "a name opens that tech, as today"
    assert avery["sum"] == ["cell", "Total 29 · Flags 2"] and blake["sum"] == ["cell", "Total 6 · Flags 1"]
    assert avery["flags"] == ["", "warn"], "flags in the attention color when there are any"
    assert avery["slots"] == ["cell"] * 7 and all(avery["filled"]) and all(blake["filled"])
    assert [avery["cells"], blake["cells"]] == [line["cells"] for line in out["split"]], \
        "the same cells as the three-part grid: classes, words, titles, links and dots"


def test_a_shorter_period_keeps_the_seven_column_width(tmp_path, conn, version):
    days = WEEK[1:4]
    body = activity(days, [row(AVERY, "Avery Cole", [cell(d, tickets=5) for d in days])], preset="custom",
                    label="Oct 5 – Oct 7, 2026")
    hash_, api_url = custom(days)
    out = stacked(tmp_path, hash_, {api_url: body}, "return readStack(stack());")
    assert out["style"] == "--cols: 7", "cells keep one size whatever the period's length"
    assert [c[1] for c in out["head"]["cells"]] == ["Mon5", "Tue6", "Wed7"]
    assert len(out["blocks"][0]["cells"]) == 3
    assert out["blocks"][0]["sum"][1] == "Total 15 · Flags 0" and out["blocks"][0]["flags"] == ["", ""]


# -- all techs, By day past a week: a calendar per tech --------------------------------------------------------


SEPT = iso_days("2026-09-01", 30)   # Tuesday Sep 1 to Wednesday Sep 30


def month_body() -> dict:
    avery = [cell(d, "missed", flags=[flag("missed")]) if i == 1 else cell(d, "rest") if i in (4, 5) else
             cell(d, tickets=9) for i, d in enumerate(SEPT)]
    blake = [cell(d, tickets=6) for d in SEPT]
    return activity(SEPT, [row(AVERY, "Avery Cole", avery), row(BLAKE, "Blake Moreno", blake)], preset="last_month",
                    label="Last month (Sep 1 – Sep 30)")


def test_by_day_past_a_week_is_a_calendar_per_tech_under_one_sun_to_sat_head(tmp_path, conn, version):
    hash_, api_url = custom(SEPT)
    out = stacked(tmp_path, hash_ + "&view=day", {api_url: month_body()}, """
  return { stack: readStack(stack()), split: bodyRows(heat()).map(hcs) };
""")
    s = out["stack"]
    assert s["cls"] == ["heat-stack", "dated", "mid"], "dated cells; past 14 days the denser look"
    assert s["style"] == "--cols: 7" and s["label"] == "Tickets touched per day"
    assert s["head"]["cells"] == [["columnheader", d] for d in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]
    for block, split in zip(s["blocks"], out["split"]):
        assert block["filled"] == [False] * 2 + [True] * 30 + [False] * 3, \
            "blank slots before Tuesday the 1st and after Wednesday the 30th"
        assert block["slots"] == ["cell"] * 35
        assert [c["date"] for c in block["cells"]] == ["Sep 1"] + [str(n) for n in range(2, 31)], \
            "each day's date in its corner, the month named on the 1st"
        assert without_date(block["cells"]) == without_date(split)
    avery, blake = s["blocks"]
    assert avery["cells"][1]["text"] == "✕" and avery["cells"][4]["cls"] == "hc rest"
    assert avery["sum"][1] == "Total 243 · Flags 1" and avery["flags"] == ["", "warn"]
    assert blake["sum"][1] == "Total 180 · Flags 0" and blake["flags"] == ["", ""]


def test_eight_to_fourteen_days_by_day_is_a_calendar_without_the_denser_look(tmp_path, conn, version):
    days = iso_days("2026-10-04", 10)
    body = activity(days, [row(AVERY, "Avery Cole", [cell(d, "missed", flags=[flag("missed")]) if i == 0 else
                                                     cell(d, tickets=4) for i, d in enumerate(days)])],
                    preset="custom", label="Oct 4 – Oct 13, 2026")
    hash_, api_url = custom(days)
    out = stacked(tmp_path, hash_, {api_url: body}, "return readStack(stack());")
    assert out["cls"] == ["heat-stack", "dated"]
    block = out["blocks"][0]
    assert block["filled"] == [True] * 10 + [False] * 4
    assert block["cells"][0]["text"] == "Sick"


# -- all techs, By week --------------------------------------------------------------------------------------


def test_by_week_puts_each_techs_weeks_four_to_a_row_each_dated_by_its_first_day(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = stacked(tmp_path, hash_, {api_url: body}, """
  return { stack: readStack(stack()), split: bodyRows(heat()).map(hcs) };
""")
    s = out["stack"]
    assert s["cls"] == ["heat-stack", "weeks", "dated"]
    assert s["style"] == "--cols: 4", "four weeks to a row"
    assert s["label"] == "Tickets touched per week"
    assert s["head"] is None, "each week names itself"
    block = s["blocks"][0]
    assert block["who"] == ["rowheader", "Avery Cole"] and block["sum"][1] == "Total 200 · Flags 1"
    assert block["filled"] == [True] * 8
    assert [c["date"] for c in block["cells"]] == ["Aug 23", "Aug 30", "Sep 6", "Sep 13", "Sep 20", "Sep 27", "Oct 4",
                                                   "Oct 11"]
    assert without_date(block["cells"]) == without_date(out["split"][0]), \
        "the same week cells: tickets, shifts, dot and link"


# -- one tech's calendar ---------------------------------------------------------------------------------------


def one_tech(body: dict) -> dict:
    body["rows"] = body["rows"][:1]
    return body


def test_one_techs_week_stacks_as_a_line_over_its_seven_days(tmp_path, conn, version):
    out = stacked(tmp_path, "#/team?tech=" + AVERY.replace("@", "%40"), {url(tech=AVERY): one_tech(week_body())}, """
  var table = heat();
  return { stack: readStack(stack()), table: table.className, weeks: bodyRows(table).map(hcs) };
""")
    s = out["stack"]
    assert out["table"] == "heat cal"
    assert s["cls"] == ["heat-stack", "cal", "dated"]
    assert s["beside"] == ["heat cal", "heat-stack cal dated"] and s["box"] == "scroll"
    assert s["role"] == "table" and s["style"] == "--cols: 7"
    assert s["head"]["cells"] == [["columnheader", d] for d in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]
    (week,) = s["blocks"]
    assert week["role"] == "row" and week["who"] == ["rowheader", "Week of Oct 4"] and week["link"] is None
    assert week["sum"] == ["cell", "Total 29 · Flags 2"] and week["flags"] == ["", "warn"]
    assert week["filled"] == [True] * 7
    assert week["cells"] == out["weeks"][0], "the calendar's own cells, each dated in its corner"


def test_one_techs_calendar_pads_a_partial_week_with_blank_days(tmp_path, conn, version):
    days = iso_days("2026-10-07", 14)
    hash_ = "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": days[0], "end": days[-1]})
    out = stacked(tmp_path, hash_, {url("custom", days[0], days[-1], AVERY): tech_body()}, """
  return { stack: readStack(stack()), weeks: bodyRows(heat()).map(hcs) };
""")
    s = out["stack"]
    assert s["cls"] == ["heat-stack", "cal", "dated"], "14 days: not the denser look"
    assert [b["who"][1] for b in s["blocks"]] == ["Week of Oct 4", "Week of Oct 11", "Week of Oct 18"]
    assert [b["filled"] for b in s["blocks"]] == [[False] * 3 + [True] * 4, [True] * 7, [True] * 3 + [False] * 4]
    assert [b["sum"][1] for b in s["blocks"]] == ["Total 8 · Flags 2", "Total 19 · Flags 1", "Total 0 · Flags 0"]
    assert [b["cells"] for b in s["blocks"]] == out["weeks"]


def test_one_techs_long_calendar_takes_the_denser_look(tmp_path, conn, version):
    days = iso_days("2026-08-23", 56)
    hash_ = "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": days[0], "end": days[-1]})
    out = stacked(tmp_path, hash_, {custom(days, AVERY)[1]: long_body(56)}, "return readStack(stack());")
    assert out["cls"] == ["heat-stack", "cal", "dated", "mid"]
    assert len(out["blocks"]) == 8 and all(b["filled"] == [True] * 7 for b in out["blocks"])


def test_a_team_with_nobody_in_it_has_no_stacked_grid(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): activity(WEEK, [], techs=[])},
                   "return screenEl.querySelectorAll('.heat-stack').length;")
    assert out == 0


# -- the stylesheet --------------------------------------------------------------------------------------------


def test_the_stacked_forms_show_below_1024px_in_place_of_the_grid_and_the_calendar_table():
    """The switch is the width alone (spec §5, §7): at 1024px and up today's three-part grid and calendar table, on a
    sideways iPad as on the PC; below it the stacked forms."""
    css = app_css()
    top, narrow = top_level(css), media_block(css, NARROW)
    assert values(top, ".heat-stack", "display") == ["none"]
    assert values(top, ".heat.split", "display") == ["flex"]
    assert values(narrow, ".heat-stack", "display") == ["block"]
    assert values(narrow, ".heat.split", "display") == ["none"]
    assert values(narrow, "table.heat.cal", "display") == ["none"]
    assert values(media_block(css, "(pointer: coarse)"), ".heat-stack", "display") == [], "never by input"


def test_a_stacked_grid_lays_its_head_and_cells_in_equal_columns():
    top = top_level(app_css())
    for part in (".hs-head", ".hs-cells"):
        assert values(top, part, "grid-template-columns") == ["repeat(var(--cols), minmax(0, 1fr))"], part
        assert values(top, part, "column-gap") == ["var(--heat-gap)"], part
    assert values(top, ".heat-stack", "--heat-gap") == ["var(--sp-2)"], "the grid's own gap, from one rule"
    assert values(top, ".heat-stack .hc", "min-width") == ["0"], "cells take their column's width"
    assert values(top, ".heat-stack.weeks .hc", "height") == ["var(--heat-stack-week-h)"]
    for rule in (".heat-stack.mid .hc", ".heat-stack.mid .hc.rest", ".heat-stack.mid .hc.pto"):
        assert values(top, rule, "font-size") == values(top, rule.replace("heat-stack", "heat"), "font-size") != [], rule


def test_phone_tiles_sit_two_to_a_row_with_the_lead_tile_across_both():
    """Below 640px the lead tile rows keep two tiles to a row, the first across both (§8b, mockup 07), in place of
    the old one tile per row."""
    phone = media_block(app_css(), PHONE)
    for tiles in (".panel-row.cols-4-lead", ".panel-row.cols-5-lead"):
        assert values(phone, tiles, "grid-template-columns") == ["repeat(2, minmax(0, 1fr))"], tiles
        assert values(phone, tiles + " > :first-child", "grid-column") == ["1 / -1"], tiles
