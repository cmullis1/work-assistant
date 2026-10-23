"""The Team activity screen over a range of days (Team activity design §6.1-6.4 and §6.7, Task 10): the all-techs
heat grid in its densities, By day / By week, month names, the flag tables, one tech's calendar and day by day,
Refresh and the closing note. app.js runs in node on the fake DOM (tests/fake_dom.js) through the stand-in page of
test_web_test_view, with `GET /api/activity` answered from fixtures in the shape report.range_view returns."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest

from css_rules import STATIC, app_css, tokens, top_level, values
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_overview import node_prelude, run_node
from test_web_edition import signed_in_manager
from test_web_test_view import answer, page_responses, run_page

AVERY, BLAKE, CASEY = "avery@example.com", "blake@example.com", "casey@example.com"
SHIFT = {"start": "08:00", "end": "17:00"}
LABEL = "Last week (Oct 4 – Oct 10)"
NOTE = ("Built from Zendesk's own records, read-only: replies, internal notes, logged calls, ticket changes and "
        "sign-ins. Zendesk doesn't record opening or reading a ticket, calls not logged in Zendesk, remote sessions, "
        "or work in other tools, so a gap means \"nothing recorded\", not proof of idleness.")


# -- fixtures in the shape of the API ------------------------------------------------------------------------


def iso_days(first: str, count: int) -> list[str]:
    return [(date.fromisoformat(first) + timedelta(days=n)).isoformat() for n in range(count)]


def cell(day: str, state: str = "worked", tickets: int = 0, **extra) -> dict:
    base = {"day": day, "state": state, "tickets": tickets, "replies": 0, "notes": 0, "calls": 0, "solved": 0,
            "signin": None, "first": None, "last": None, "gap_min": 0, "gap_start": None, "flags": [],
            "shift": None if state == "untracked" else dict(SHIFT), "time_off": "pto" if state == "pto" else None}
    base.update(extra)
    return base


def flag(kind: str, minutes: int | None = None) -> dict:
    return {"kind": kind, "minutes": minutes}


def row(email: str, name: str, cells: list[dict]) -> dict:
    return {"email": email, "name": name, "cells": cells}


def activity(days: list[str], rows: list[dict], *, techs=None, picker=None, preset="last_week", label=LABEL,
             so_far=False, synced=None) -> dict:
    """The API's answer. `picker` (the Tech picker's list, Ruling 9 of Regions) defaults to the techs listed."""
    if techs is None:
        techs = [{"email": r["email"], "name": r["name"], "has_shift": True} for r in rows]
    if picker is None:
        picker = [{key: t[key] for key in ("email", "name", "region") if key in t} for t in techs]
    return {"period": {"start": days[0], "end": days[-1], "label": label, "so_far": so_far, "preset": preset},
            "today": "2026-10-17",
            "synced": synced or {"at": "2026-10-17T14:42:00Z", "status": "complete", "running": False},
            "techs": techs, "days": days, "rows": rows, "picker": picker}


def url(period=None, start=None, end=None, tech=None, region=None, team=None) -> str:
    """The request the page makes: the address's own parameters, in the order app.js writes them."""
    query = urlencode({k: v for k, v in (("period", period), ("start", start), ("end", end), ("tech", tech),
                                         ("region", region), ("team", team)) if v})
    return "/api/activity" + ("?" + query if query else "")


def custom(days: list[str], tech=None) -> tuple[str, str]:
    """(the page's hash, the API url) for a custom range."""
    return ("#/team" + "?" + urlencode({"period": "custom", "start": days[0], "end": days[-1]}),
            url("custom", days[0], days[-1], tech))


# The week of Oct 4 - 10: Sun-Sat, two techs, every kind of cell and every shade step.
WEEK = iso_days("2026-10-04", 7)


def week_body() -> dict:
    avery = row(AVERY, "Avery Cole", [
        cell(WEEK[0], "missed", flags=[flag("missed")]),
        cell(WEEK[1], tickets=12, replies=12, notes=2, calls=1, solved=3),
        cell(WEEK[2], tickets=8, replies=9, solved=1),
        cell(WEEK[3], tickets=4, replies=4, notes=1, calls=2),
        cell(WEEK[4], tickets=3, replies=3, flags=[flag("long_gap", 150)]),
        cell(WEEK[5], "rest"),
        cell(WEEK[6], "off", tickets=2, replies=2)])
    blake = row(BLAKE, "Blake Moreno", [
        cell(WEEK[0], "untracked"),
        cell(WEEK[1], "pto"),
        cell(WEEK[2], "zero", signin="08:05", flags=[flag("no_activity")]),
        cell(WEEK[3], tickets=5, replies=6, notes=1, solved=1),
        cell(WEEK[4], tickets=1, replies=1),
        cell(WEEK[5], "rest"),
        cell(WEEK[6], "rest")])
    return activity(WEEK, [avery, blake])


def long_body(count: int, first: str = "2026-08-23", missed_week=False) -> dict:
    """One tech working Mon-Fri for `count` days from a Sunday; a sick day and a flag in later weeks."""
    days = iso_days(first, count)
    cells = []
    for i, day in enumerate(days):
        weekday = (date.fromisoformat(day).weekday() + 1) % 7   # Sunday = 0
        if weekday in (0, 6):
            cells.append(cell(day, "rest"))
        elif missed_week and i == 8:
            cells.append(cell(day, "missed", flags=[flag("missed")]))
        elif i == 16:
            cells.append(cell(day, tickets=5, flags=[flag("long_gap", 130)]))
        else:
            cells.append(cell(day, tickets=5))
    return activity(days, [row(AVERY, "Avery Cole", cells)], preset="custom", label="Aug 23 – Oct 17, 2026")


# -- the page ----------------------------------------------------------------------------------------------

READ = """
// A cell's own text: one tech's calendar adds the date in its corner (.cdate, last), read on its own as `date`.
function desc(el) {
  var date = el.querySelectorAll(".cdate")[0];
  var text = date ? el.textContent.slice(0, el.textContent.length - date.textContent.length) : el.textContent;
  return { tag: el.tagName, cls: el.className, text: text, title: el.getAttribute("title"),
    href: el.getAttribute("href"), dots: el.querySelectorAll(".fdot").length, date: date ? date.textContent : null };
}
function byId(id) { return document.getElementById(id); }
function heat() { return screenEl.querySelectorAll(".heat")[0] || null; }
// The all-techs grid is three parts side by side (T-126); a line of it is that line's row in each part, read as one.
function line(t, i) {
  var rows = t.children.map(function (part) { return part.children[i]; });
  return { rows: rows, children: [].concat.apply([], rows.map(function (r) { return r.children; })),
    querySelectorAll: function (selector) {
      return [].concat.apply([], rows.map(function (r) { return r.querySelectorAll(selector); }));
    } };
}
function bodyRows(t) {
  if (!t.classList.contains("split")) { return t.querySelectorAll("tbody tr"); }
  return t.children[0].children.slice(1).map(function (_, i) { return line(t, i + 1); });
}
function hcs(el) { return el.querySelectorAll(".hc").map(desc); }
function heads(t) {
  var cells = t.classList.contains("split") ? line(t, 0).children : t.querySelectorAll("thead th");
  return cells.map(function (th) { return th.textContent; });
}
function tiles() {
  return screenEl.querySelectorAll(".tile").map(function (t) {
    var d = t.querySelectorAll(".d")[0];
    return [t.querySelectorAll(".k")[0].textContent, t.querySelectorAll(".v")[0].textContent, d ? d.textContent : ""];
  });
}
function head() { var el = screenEl.querySelectorAll(".period-head")[0]; return el ? el.childNodes[0].textContent : null; }
function titles() { return panels().map(titleOf); }
function choose(id, value) { var el = byId(id); el.value = value; fire(el, "change"); }
function getRequests(prefix) {
  return requests.filter(function (r) { return r.method === "GET" && r.url.indexOf(prefix) === 0; });
}
"""


def team(tmp_path, start_hash: str, bodies: dict[str, dict], scenario: str):
    """Load the page at `start_hash` with `GET <url>` answered by each body in `bodies`, then run `scenario`."""
    responses = page_responses(api_for(tmp_path))
    for path, body in bodies.items():
        responses["GET " + path] = answer(body)
    return run_page(start_hash, responses, READ + scenario)


def snapshot(tmp_path, start_hash: str, bodies: dict[str, dict], reader: str):
    """What `reader` (the body of a function returning JSON) sees once the page has settled."""
    return team(tmp_path, start_hash, bodies, "run(async function () {\n" + reader + "\n});")


# -- the all-techs grid ------------------------------------------------------------------------------------


def test_all_techs_week_grid_has_cell_per_day(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  var t = heat();
  return { cls: t.className, heads: heads(t), parts: t.children.map(function (c) { return c.className; }),
    rows: bodyRows(t).map(function (tr) {
      return { who: tr.querySelectorAll(".who")[0].textContent, cells: hcs(tr),
        nums: tr.querySelectorAll(".num").map(function (td) { return td.textContent; }) };
    }), head: head(), tiles: tiles(), titles: titles() };
""")
    assert out["head"] == LABEL + " · 2 techs · 8 scheduled shifts"
    assert out["titles"][0] == "Tickets touched per day"
    assert out["cls"].split() == ["heat", "split"]
    assert out["heads"] == ["", "Sun4", "Mon5", "Tue6", "Wed7", "Thu8", "Fri9", "Sat10", "Total", "Flags"]
    assert out["parts"] == ["heat-names", "heat-days", "heat-ends"]
    avery, blake = out["rows"]
    assert avery["who"] == "Avery Cole" and blake["who"] == "Blake Moreno"
    assert [c["cls"] for c in avery["cells"]] == [
        "hc miss", "hc h4", "hc h3", "hc h2", "hc h1", "hc rest", "hc off"]
    assert [c["text"] for c in avery["cells"]] == ["Sick", "12", "8", "4", "3", "Rest", "2"]
    assert [c["dots"] for c in avery["cells"]] == [0, 0, 0, 0, 1, 0, 0], "only the flagged working day has a dot"
    assert [c["cls"] for c in blake["cells"]] == ["hc rest", "hc pto", "hc zero", "hc h2", "hc h1", "hc rest", "hc rest"]
    assert [c["text"] for c in blake["cells"]] == ["-", "PTO", "0", "5", "1", "Rest", "Rest"]
    assert avery["nums"] == ["29", "2"] and blake["nums"] == ["6", "1"]
    assert out["tiles"] == [
        ["Tickets touched", "35", "5.5 per shift worked"],
        ["Shifts worked", "6 of 8", "2 with no activity"],
        ["Replies", "37", "4 internal notes"],
        ["Solved", "5", "3 calls logged"],
        ["Flags", "3", "1 sick day"]]


def test_tiles_without_a_sick_day(tmp_path, conn, version):
    body = activity(WEEK, [row(AVERY, "Avery Cole", [cell(d, tickets=4, flags=[flag("late_start", 95)])
                                                     for d in WEEK])])
    out = snapshot(tmp_path, "#/team", {url(): body}, "return tiles();")
    assert out[4] == ["Flags", "7", "No sick days"]


def test_this_week_future_cells_and_totals_exclude_future(tmp_path, conn, version):
    days = iso_days("2026-10-18", 7)
    # The API reports zeros for days to come; the page must not count whatever sits in them.
    cells = [cell(days[0], tickets=3, replies=3), cell(days[1], tickets=5, replies=5, flags=[flag("late_start", 100)])]
    cells += [cell(d, "future", tickets=9, replies=9, flags=[flag("missed")]) for d in days[2:]]
    body = activity(days, [row(AVERY, "Avery Cole", cells)], preset="this_week", label="This week (Oct 18 – Oct 24)",
                    so_far=True)
    out = snapshot(tmp_path, "#/team?period=this_week", {url("this_week"): body}, """
  var tr = bodyRows(heat())[0];
  return { cells: hcs(tr), nums: tr.querySelectorAll(".num").map(function (td) { return td.textContent; }),
    head: head(), tiles: tiles(), titles: titles() };
""")
    assert [c["cls"] for c in out["cells"]] == ["hc h1", "hc h2"] + ["hc future"] * 5
    assert [c["tag"] for c in out["cells"]] == ["A", "A"] + ["SPAN"] * 5, "days to come are not links"
    assert all(c["text"] == "" and c["title"] == "Still to come" for c in out["cells"][2:])
    assert out["nums"] == ["8", "1"]
    assert out["head"] == "This week (Oct 18 – Oct 24) · 1 tech · 2 scheduled shifts"
    assert out["tiles"][:2] == [["Tickets touched", "8", "4 per shift worked"], ["Shifts worked", "2 of 2", "0 with no activity"]]
    assert out["tiles"][4] == ["Flags", "1", "No sick days"]
    assert out["titles"] == ["Tickets touched per day", "Flags · 1"], "7 days or fewer: no Flags by tech table"


def test_unsynced_days_are_dashed_cells_that_say_so_and_count_nothing(tmp_path, conn, version):
    """Final review F1: a day no sync has reached is drawn like a day to come (dashed, not a link), says "Not synced
    yet", is in the legend, and nothing in it is counted."""
    cells = [cell(WEEK[0], "unsynced", tickets=9, flags=[flag("missed")]),
             cell(WEEK[1], "unsynced", signin="08:05"),
             cell(WEEK[2], tickets=3, replies=3), cell(WEEK[3], tickets=5, replies=5, flags=[flag("late_start", 100)]),
             cell(WEEK[4], tickets=2, replies=2), cell(WEEK[5], "rest"), cell(WEEK[6], "rest")]
    out = snapshot(tmp_path, "#/team", {url(): activity(WEEK, [row(AVERY, "Avery Cole", cells)])}, """
  var tr = bodyRows(heat())[0];
  var legend = screenEl.querySelectorAll(".heat-legend")[0];
  return { cells: hcs(tr), nums: tr.querySelectorAll(".num").map(function (td) { return td.textContent; }),
    head: head(), tiles: tiles(), legend: legend.children.map(function (item) { return item.textContent; }),
    swatch: legend.children[legend.children.length - 1].querySelectorAll(".hc")[0].className };
""")
    assert [c["cls"] for c in out["cells"][:2]] == ["hc future", "hc future"]
    assert [c["tag"] for c in out["cells"][:2]] == ["SPAN", "SPAN"], "not links"
    assert all(c["title"] == "Not synced yet" and c["text"] == "" for c in out["cells"][:2])
    assert out["nums"] == ["10", "1"]
    assert out["head"] == LABEL + " · 1 tech · 3 scheduled shifts"
    assert out["tiles"][1] == ["Shifts worked", "3 of 3", "0 with no activity"]
    assert out["tiles"][4] == ["Flags", "1", "No sick days"]
    assert out["legend"][-1] == "Not synced yet" and out["swatch"] == "hc future"
    assert "Still to come" not in out["legend"]


def test_a_week_no_sync_has_reached_is_a_dashed_week_cell(tmp_path, conn, version):
    body = long_body(56)
    for c in body["rows"][0]["cells"][:7]:
        c.update(state="unsynced", tickets=0, flags=[])
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_, {api_url: body}, "return hcs(bodyRows(heat())[0])[0];")
    assert out["cls"] == "hc future" and out["title"] == "Not synced yet" and out["tag"] == "SPAN"


def test_untracked_days_are_rest_cells_that_say_why(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, "return hcs(bodyRows(heat())[1])[0];")
    assert out["cls"] == "hc rest" and out["title"] == "Not on the team" and out["tag"] == "SPAN"


@pytest.mark.parametrize("count, classes", [
    (7, ["heat", "split"]), (14, ["heat", "split"]),
    (15, ["heat", "split", "mid"]), (30, ["heat", "split", "mid"]), (31, ["heat", "split", "mid"]),
    (32, ["heat", "split", "mid", "dates"]), (56, ["heat", "split", "mid", "dates"]),
    (63, ["heat", "split", "mid", "dates"]), (64, ["heat", "split", "mid", "dates"]),
    (91, ["heat", "split", "mid", "dates"])])
def test_density_classes_by_length(tmp_path, conn, version, count, classes):
    days = iso_days("2026-08-02", count)   # a Sunday
    cells = [cell(d, "missed", flags=[flag("missed")]) if i == 1 else cell(d, "rest") if i == 2 else cell(d, tickets=5)
             for i, d in enumerate(days)]
    body = activity(days, [row(AVERY, "Avery Cole", cells)], preset="custom", label="A range")
    hash_, api_url = custom(days)
    out = snapshot(tmp_path, hash_ + "&view=day", {api_url: body}, """
  var t = heat();
  var tr = bodyRows(t)[0];
  return { cls: t.className.split(" "), cells: hcs(tr).slice(0, 3).map(function (c) { return c.text; }),
    heads: heads(t).slice(1, 9),
    wk: t.querySelectorAll(".wk").length,
    mo: t.querySelectorAll("[data-mo]").length };
""")
    assert out["cls"] == classes
    missed, rest = ("Sick", "Rest") if count <= 14 else ("✕", "Rest")
    assert out["cells"] == ["5", missed, rest]
    assert out["mo"] == count, "every day column names its month for the month labels"
    assert out["wk"] == 0, "no week gap: every day cell is the same width (owner, 2026-10-21)"
    shown = min(count, 8)
    if count <= 31:
        assert out["heads"][:shown] == ["Sun2", "Mon3", "Tue4", "Wed5", "Thu6", "Fri7", "Sat8", "Sun9"][:shown]
    else:
        assert out["heads"] == ["2", "3", "4", "5", "6", "7", "8", "9"], "past a month: every day's date, no shrinking"


def test_long_range_defaults_to_week_view(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    scenario = """
  var t = heat();
  var trs = bodyRows(t);
  return { cls: t.className.split(" "), heads: heads(t), cells: hcs(trs[0]), title: titles()[0],
    pills: screenEl.querySelectorAll(".pills.grid-mode button").map(function (b) { return [b.textContent, b.className]; }),
    nums: trs[0].querySelectorAll(".num").map(function (td) { return td.textContent; }) };
"""
    out = snapshot(tmp_path, hash_, {api_url: body}, scenario)
    assert out["cls"] == ["heat", "split", "weeks"]
    assert out["title"] == "Tickets touched per week"
    assert out["heads"][:3] == ["Week of", "Aug 23", "Aug 30"] and out["heads"][-2:] == ["Total", "Flags"]
    assert len(out["cells"]) == 8
    assert out["pills"] == [["By day", "pill"], ["By week", "pill on"]]
    first = out["cells"][0]
    assert first["cls"] == "hc h2" and first["text"] == "255/5 shifts"   # 25 tickets, then the shifts line
    assert first["href"] == "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": "2026-08-23",
                                                   "end": "2026-08-29"})
    assert out["cells"][2]["dots"] == 1, "the week with a flag has a dot"
    assert out["nums"][0] == "200"


def test_week_cells_mark_a_sick_day_and_shade_by_tickets_per_shift(tmp_path, conn, version):
    days = iso_days("2026-08-23", 28)
    cells = []
    for i, d in enumerate(days):
        week, dow = divmod(i, 7)
        if dow in (0, 6):
            cells.append(cell(d, "rest"))
        elif week == 0:
            cells.append(cell(d, tickets=2))                     # 10 tickets over 5 shifts: 2 per shift
        elif week == 1:
            cells.append(cell(d, "missed", flags=[flag("missed")]) if dow == 1 else cell(d, tickets=12))   # 48 / 4
        elif week == 2:
            cells.append(cell(d, tickets=8))                     # 8 per shift
        else:
            cells.append(cell(d, "zero", flags=[flag("no_activity")]))
    body = activity(days, [row(AVERY, "Avery Cole", cells)], preset="custom", label="Four weeks")
    hash_, api_url = custom(days)
    out = snapshot(tmp_path, hash_ + "&view=week", {api_url: body}, "return hcs(bodyRows(heat())[0]);")
    assert [c["cls"] for c in out] == ["hc h1", "hc h4", "hc h3", "hc zero"]
    assert [c["text"] for c in out] == ["105/5 shifts", "484/5 shifts", "405/5 shifts", "00/5 shifts"]
    assert [c["dots"] for c in out] == [0, 1, 0, 1]


def test_week_cells_for_a_dot_that_says_missed(tmp_path, conn, version):
    body = long_body(56, missed_week=True)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  return heat().querySelectorAll(".fdot").map(function (d) { return d.className; });
""")
    assert out == ["fdot fail", "fdot"], "a sick day is a failure-colored dot; a flag is the plain one"


def per_shift_week(extra_work: bool) -> list[dict]:
    """Mon-Thu worked at 4 tickets; with `extra_work`, Thursday is a marked sick day with 9 tickets and Friday PTO with 7."""
    cells = [cell(WEEK[0], "rest")]
    cells += [cell(WEEK[n], tickets=4) for n in range(1, 5)]
    cells += [cell(WEEK[5], "rest"), cell(WEEK[6], "rest")]
    if extra_work:
        cells[4] = cell(WEEK[4], "missed", tickets=9, replies=9, time_off="sick", flags=[flag("missed")])
        cells[5] = cell(WEEK[5], "pto", tickets=7, replies=7)
    return cells


def test_work_on_a_marked_sick_or_pto_day_is_not_in_the_all_techs_per_shift_figure(tmp_path, conn, version):
    plain = activity(WEEK, [row(AVERY, "Avery Cole", per_shift_week(False))])
    extra = activity(WEEK, [row(AVERY, "Avery Cole", per_shift_week(True))])
    with_work = snapshot(tmp_path, "#/team", {url(): extra}, "return tiles();")
    without = snapshot(tmp_path, "#/team", {url(): plain}, "return tiles();")
    assert without[0] == ["Tickets touched", "16", "4 per shift worked"]
    assert with_work[0] == ["Tickets touched", "28", "4 per shift worked"], \
        "all recorded work is touched, but only worked days make the per-shift figure"


def test_work_on_a_marked_sick_or_pto_day_does_not_shade_or_describe_the_week_cell(tmp_path, conn, version):
    days = iso_days("2026-08-23", 14)
    second = [cell(d, tickets=3) if i not in (0, 6) else cell(d, "rest") for i, d in enumerate(days[7:])]
    hash_, api_url = custom(days)
    results = []
    for extra_work in (False, True):
        first = [cell(days[0], "rest")] + [cell(days[n], tickets=8) for n in range(1, 5)]
        if extra_work:
            first += [cell(days[5], "missed", tickets=40, time_off="sick", flags=[flag("missed")]),
                      cell(days[6], "pto", tickets=30)]
        else:
            first += [cell(days[5], "rest"), cell(days[6], "rest")]
        body = activity(days, [row(AVERY, "Avery Cole", first + second)], preset="custom", label="Two weeks")
        results.append(snapshot(tmp_path, hash_ + "&view=week", {api_url: body}, "return hcs(bodyRows(heat())[0])[0];"))
    plain, marked = results
    assert plain["cls"] == "hc h3" and plain["title"] == "32 tickets · 4 of 4 shifts worked · 8 per shift · 0 flags"
    assert marked["cls"] == "hc h3", "the shade follows tickets on worked days only"
    assert marked["title"] == "102 tickets · 4 of 5 shifts worked · 8 per shift · 1 flag", (
        "every ticket touched is still counted, but the per-shift figure is worked days only")


@pytest.mark.parametrize("count, default", [(8, "day"), (42, "day"), (43, "week"), (56, "week")])
def test_view_default_follows_the_length_and_the_address_wins(tmp_path, conn, version, count, default):
    body = long_body(count)
    hash_, api_url = custom(iso_days("2026-08-23", count))
    other = "week" if default == "day" else "day"
    out = snapshot(tmp_path, hash_, {api_url: body}, "return heat().className.indexOf('weeks') >= 0;")
    assert out == (default == "week")
    out = snapshot(tmp_path, hash_ + "&view=" + other, {api_url: body},
                   "return heat().className.indexOf('weeks') >= 0;")
    assert out == (other == "week")


def test_by_day_and_by_week_pills_write_the_view_into_the_address(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  var pills = screenEl.querySelectorAll(".pills.grid-mode button");
  fire(pills[0], "click");
  var afterDay = location.hash;
  fire(pills[1], "click");
  return [afterDay, location.hash];
""")
    assert out[0] == hash_ + "&view=day" and out[1] == hash_ + "&view=week"


def test_a_week_has_no_by_day_pills(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, "return screenEl.querySelectorAll('.pills').length;")
    assert out == 0


def test_a_new_period_forgets_the_view(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_ + "&view=day", {api_url: body}, """
  choose("ta-period", "last_month");
  return location.hash;
""")
    assert out == "#/team?period=last_month"


def test_changing_the_tech_keeps_the_period_and_the_view(tmp_path, conn, version):
    body = long_body(56)
    body["techs"] = [{"email": AVERY, "name": "Avery Cole", "has_shift": True},
                     {"email": BLAKE, "name": "Blake Moreno", "has_shift": True}]
    body["picker"] = [{"email": AVERY, "name": "Avery Cole"}, {"email": BLAKE, "name": "Blake Moreno"}]
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_ + "&view=day", {api_url: body}, """
  var options = byId("ta-tech").querySelectorAll("option").map(function (o) { return [o.value, o.textContent]; });
  choose("ta-tech", "blake@example.com");
  return [options, location.hash];
""")
    assert out[0] == [["", "All techs"], [AVERY, "Avery Cole"], [BLAKE, "Blake Moreno"]]
    assert out[1] == hash_ + "&tech=blake%40example.com&view=day"


# -- the Tech picker by region (Regions design) -------------------------------------------------------------

DREW = "drew@example.com"
NAMES = {AVERY: "Avery Cole", BLAKE: "Blake Moreno", CASEY: "Casey Nguyen", DREW: "Drew Patel"}   # by name, as listed
REGIONS = {AVERY: "NASA", BLAKE: "APAC", CASEY: "EMEA", DREW: "NASA"}
REGION_IN, TECH_IN = "\u00a0" * 3, "\u00a0" * 6   # a region's indent under All techs, and a tech's under its region


def closed(lines: list[list[str]], picked: str) -> list[list[str]]:
    """The picker's `lines` ([value, text]) as the closed picker holds them: the picked line (the first with the value
    `picked`) shows without its indent, so the closed picker reads "APAC", not "   APAC"; the others keep theirs."""
    first = next(i for i, (value, _) in enumerate(lines) if value == picked)
    return [[value, text.lstrip("\u00a0") if i == first else text] for i, (value, text) in enumerate(lines)]


OPTIONS = """
function options() {
  return byId("ta-tech").querySelectorAll("option").map(function (o) { return [o.value, o.textContent]; });
}
function rowNames() {
  return bodyRows(heat()).map(function (tr) { return tr.querySelectorAll(".who")[0].textContent; });
}
"""


def regional(rowed: list[str], listed: list[str] | None = None, regions: dict | None = None) -> dict:
    """A week's answer with a row for each of `rowed` and the techs `listed` (default `rowed`: the API narrowed to a
    region), each with their region from `regions` (default REGIONS); its picker is every tech, as the API's is."""
    regions = regions or REGIONS
    rows = [row(e, NAMES[e], [cell(d, tickets=2) for d in WEEK]) for e in rowed]
    techs = [{"email": e, "name": NAMES[e], "region": regions[e], "has_shift": True} for e in listed or rowed]
    return activity(WEEK, rows, techs=techs, picker=[{"email": e, "name": NAMES[e], "region": regions[e]}
                                                     for e in NAMES])


def test_a_viewer_across_regions_picks_a_region_or_a_tech_from_the_grouped_picker(tmp_path, conn, version):
    """Regions design: for a viewer whose techs span regions, the Tech picker is All techs, then each region in order
    (NASA, EMEA, APAC) slightly indented, each followed by its techs further in. Picking a region asks for that
    region's techs and writes region= into the address, keeping the period; picking a tech drops it, and All techs
    drops both. The lines are the answer's picker (Ruling 9): a region's answer lists only that region's techs, and
    every line is still offered."""
    bodies = {url("last_week"): regional(list(NAMES)), url("last_week", region="NASA"): regional([AVERY, DREW]),
              url("last_week", tech=BLAKE): regional([BLAKE], list(NAMES))}
    out = snapshot(tmp_path, "#/team?period=last_week", bodies, OPTIONS + """
  var out = { options: options() };
  choose("ta-tech", "region:NASA");
  out.region = location.hash;
  await go(location.hash);
  out.nasa = { selected: byId("ta-tech").value, options: options(), rows: rowNames() };
  choose("ta-tech", "blake@example.com");
  out.tech = location.hash;
  await go(location.hash);
  out.blake = byId("ta-tech").value;
  choose("ta-tech", "region:NASA");
  await go(location.hash);
  choose("ta-tech", "");
  out.all = location.hash;
  out.urls = getRequests("/api/activity").map(function (r) { return r.url; });
  return out;
""")
    assert out["options"] == [
        ["", "All techs"],
        ["region:NASA", REGION_IN + "NASA"], [AVERY, TECH_IN + "Avery Cole"], [DREW, TECH_IN + "Drew Patel"],
        ["region:EMEA", REGION_IN + "EMEA"], [CASEY, TECH_IN + "Casey Nguyen"],
        ["region:APAC", REGION_IN + "APAC"], [BLAKE, TECH_IN + "Blake Moreno"],
    ]
    assert out["region"] == "#/team?period=last_week&region=NASA"
    assert out["nasa"] == {"selected": "region:NASA", "options": closed(out["options"], "region:NASA"),
                           "rows": ["Avery Cole", "Drew Patel"]}, "the closed picker shows the region unindented"
    assert out["tech"] == "#/team?period=last_week&tech=blake%40example.com"
    assert out["blake"] == BLAKE
    assert out["all"] == "#/team?period=last_week"
    assert out["urls"] == [url("last_week"), url("last_week", region="NASA"), url("last_week", tech=BLAKE),
                           url("last_week", region="NASA")]


def test_a_viewer_in_one_region_keeps_the_plain_picker(tmp_path, conn, version):
    """Regions design: a viewer whose techs are all in one region keeps today's plain list, whichever region it is."""
    apac = {email: "APAC" for email in NAMES}
    out = snapshot(tmp_path, "#/team", {url(): regional(list(NAMES), regions=apac)}, OPTIONS + "return options();")
    assert out == [["", "All techs"]] + [[email, name] for email, name in NAMES.items()]


# -- a manager's teams (Managers design) ---------------------------------------------------------------------------

ALEX, JAMIE, PAOLO, LEIGH = ("team.lead@example.com", "jamie.rivera@example.com", "paolo.ramos@example.com",
                             "leigh.west@example.com")
STONE, PIKE, SANTOS, REYES, HALE, OKAFOR = ("avery.stone@example.com", "jordan.pike@example.com",
                                            "mara.santos@example.com", "morgan.reyes@example.com",
                                            "quinn.hale@example.com", "sam.okafor@example.com")
# The manager's techs, by name as the API lists them: (name, region, tickets on each day worked).
CREW = {STONE: ("Avery Stone", "NASA", 4), PIKE: ("Jordan Pike", "NASA", 2), SANTOS: ("Mara Santos", "APAC", 6),
        REYES: ("Morgan Reyes", "NASA", 3), HALE: ("Quinn Hale", "APAC", 1), OKAFOR: ("Sam Okafor", "NASA", 5)}
# The manager's people (GET /api/people), as the API orders them (the admin first, then by email): the admin, Alex
# Kim, leads Avery, Morgan and Mara; Jamie Rivera leads Jordan and Sam; Leigh West has no techs yet; Paolo Ramos leads
# Mara too, so she is on two teams. Quinn is on no team.
TEAM_BOOK = {"people": [{"email": ALEX, "role": "admin", "name": "Alex Kim", "techs": [STONE, REYES, SANTOS]},
                        {"email": JAMIE, "role": "tl", "name": "Jamie Rivera", "techs": [PIKE, OKAFOR]},
                        {"email": LEIGH, "role": "tl", "name": "Leigh West", "techs": []},
                        {"email": PAOLO, "role": "tl", "name": "Paolo Ramos", "techs": [SANTOS]}],
             "roster": []}
R_IN, T_IN = REGION_IN, TECH_IN
MANAGER_LINES = [
    ["", "All techs"],
    ["region:NASA", R_IN + "NASA"],
    ["team:" + ALEX, T_IN + "Alex Kim's team"], [STONE, T_IN + R_IN + "Avery Stone"], [REYES, T_IN + R_IN + "Morgan Reyes"],
    ["team:" + JAMIE, T_IN + "Jamie Rivera's team"], [PIKE, T_IN + R_IN + "Jordan Pike"],
    [OKAFOR, T_IN + R_IN + "Sam Okafor"],
    ["region:APAC", R_IN + "APAC"],
    ["team:" + ALEX, T_IN + "Alex Kim's team"], [SANTOS, T_IN + R_IN + "Mara Santos"],
    ["team:" + PAOLO, T_IN + "Paolo Ramos's team"], [SANTOS, T_IN + R_IN + "Mara Santos"],
    [HALE, T_IN + "Quinn Hale (no team lead)"],
]
TEAMS_SUB = "Each team lead's techs in the regions you see, side by side · click a team to show only it"


def crew(rowed: list[str] | None = None, days: list[str] = WEEK, sick=(STONE,), flagged=(PIKE, SANTOS)) -> dict:
    """The answer for the manager's techs over `days`: a row for each of `rowed` (default every tech in CREW), each
    working Monday to Friday at their CREW tickets a day, except a sick Monday for those in `sick` and a long gap on
    Tuesday for those in `flagged`. Its picker is every tech in CREW, as the API's is whatever it was asked for."""
    rowed = list(CREW) if rowed is None else rowed
    rows = []
    for email in rowed:
        name, _, tickets = CREW[email]
        cells = []
        for day in days:
            weekday = (date.fromisoformat(day).weekday() + 1) % 7   # Sunday = 0
            if weekday in (0, 6):
                cells.append(cell(day, "rest"))
            elif weekday == 1 and email in sick:
                cells.append(cell(day, "missed", flags=[flag("missed")]))
            elif weekday == 2 and email in flagged:
                cells.append(cell(day, tickets=tickets, flags=[flag("long_gap", 130)]))
            else:
                cells.append(cell(day, tickets=tickets))
        rows.append(row(email, name, cells))
    techs = [{"email": e, "name": CREW[e][0], "region": CREW[e][1], "has_shift": True} for e in rowed]
    return activity(days, rows, techs=techs,
                    picker=[{"email": e, "name": name, "region": region} for e, (name, region, _) in CREW.items()])


def manager_page(tmp_path, start_hash: str, bodies: dict[str, dict], scenario: str, people: dict = TEAM_BOOK):
    """The hosted page signed in as a manager whose people are `people`, at `start_hash`, each of `bodies` answering
    its GET; `scenario` is the body of a function returning JSON."""
    responses = signed_in_manager(page_responses(api_for(tmp_path)), people)
    for path, body in bodies.items():
        responses["GET " + path] = answer(body)
    return run_page(start_hash, responses, READ + OPTIONS + "run(async function () {\n" + scenario + "\n});")


TEAMS = """
function teamsPanel() {
  return panels().filter(function (p) { return (titleOf(p) || "").indexOf("Teams") === 0; })[0] || null;
}
"""


def test_a_managers_picker_lists_each_region_its_teams_and_their_techs(tmp_path, conn, version):
    """Managers design: a manager's Tech picker is All techs, then each region, then each team lead's team in it
    ("Jamie Rivera's team"), then that team's techs, every line pickable; a tech on two teams is under each, a tech on
    no team is under their region as "Name (no team lead)", and a lead with none of the techs listed has no line.
    Picking a team writes team= into the address and drops tech and region, the read asks for that team, and the
    picker shows it picked; picking a tech drops the team. The people are read once, at start."""
    bodies = {url("last_week", region="APAC"): crew([SANTOS, HALE]), url("last_week", team=JAMIE): crew([PIKE, OKAFOR])}
    out = manager_page(tmp_path, "#/team?period=last_week&region=APAC", bodies, """
  var out = { options: options(), selected: byId("ta-tech").value };
  choose("ta-tech", "team:jamie.rivera@example.com");
  out.team = location.hash;
  await go(location.hash);
  out.picked = { selected: byId("ta-tech").value, options: options(), rows: rowNames() };
  choose("ta-tech", "avery.stone@example.com");
  out.tech = location.hash;
  out.urls = getRequests("/api/activity").map(function (r) { return r.url; });
  out.people = getRequests("/api/people").length;
  return out;
""")
    assert out["options"] == closed(MANAGER_LINES, "region:APAC")
    assert out["selected"] == "region:APAC"
    assert out["team"] == "#/team?period=last_week&team=jamie.rivera%40example.com"
    assert out["picked"] == {"selected": "team:" + JAMIE, "options": closed(MANAGER_LINES, "team:" + JAMIE),
                             "rows": ["Jordan Pike", "Sam Okafor"]}
    assert out["tech"] == "#/team?period=last_week&tech=avery.stone%40example.com"
    assert out["urls"] == [url("last_week", region="APAC"), url("last_week", team=JAMIE)]
    assert out["people"] == 1


def test_a_manager_in_one_region_still_picks_its_teams(tmp_path, conn, version):
    """A manager whose techs are all in one region still gets that region and its teams: a team is how they narrow a
    screen, on Trends too, where there is no Teams panel."""
    nasa = [STONE, PIKE, REYES, OKAFOR]
    body = crew(nasa)
    body["picker"] = [t for t in body["picker"] if t["email"] in nasa]
    out = manager_page(tmp_path, "#/team?period=last_week", {url("last_week"): body}, "return options();")
    assert out == closed(MANAGER_LINES[:8], "")


def test_the_closed_picker_shows_the_picked_line_without_its_indent(tmp_path, conn, version):
    """Regions handover's known issue (2026-10-22): the closed picker showed a picked region's or tech's indent
    ("   APAC"). The open list keeps every line's indent (while the picker has the focus); the closed picker shows the
    picked line without it: "APAC", "Jamie Rivera's team", "Avery Stone"."""
    bodies = {url("last_week", region="APAC"): crew([SANTOS, HALE]), url("last_week", team=JAMIE): crew([PIKE, OKAFOR]),
              url("last_week", tech=STONE): crew([STONE])}
    out = manager_page(tmp_path, "#/team?period=last_week&region=APAC", bodies, """
  function texts() { return byId("ta-tech").children.map(function (o) { return o.textContent; }); }
  function shown() {
    var select = byId("ta-tech");
    return select.children.filter(function (o) { return o.value === select.value; })[0].textContent;
  }
  var out = [];
  for (var hash of ["#/team?period=last_week&region=APAC", "#/team?period=last_week&team=jamie.rivera%40example.com",
                    "#/team?period=last_week&tech=avery.stone%40example.com"]) {
    await go(hash);
    var line = { shown: shown(), shut: texts() };
    fire(byId("ta-tech"), "focus");
    line.open = texts();
    fire(byId("ta-tech"), "blur");
    line.again = texts();
    out.push(line);
  }
  return out;
""")
    indented = [text for _, text in MANAGER_LINES]
    for line, picked, shown in zip(out, ["region:APAC", "team:" + JAMIE, STONE],
                                   ["APAC", "Jamie Rivera's team", "Avery Stone"]):
        shut = [text for _, text in closed(MANAGER_LINES, picked)]
        assert line == {"shown": shown, "shut": shut, "open": indented, "again": shut}, picked


SIGNED_IN = {"admin": ALEX, "tl": JAMIE}


@pytest.mark.parametrize("role", ["admin", "tl"])
def test_a_leads_and_the_admins_picker_and_grid_have_no_teams(tmp_path, conn, version, role):
    """Managers design: a team lead and the admin see what they saw before: the Tech picker grouped by region alone,
    the grid by name, and no Teams panel; their people aren't read for it."""
    responses = page_responses(api_for(tmp_path))
    responses["GET /api/settings"]["body"]["edition"] = "hosted"
    responses["GET /api/me"] = answer({"email": SIGNED_IN[role], "role": role, "edition": "hosted"})
    responses["GET /api/people"] = answer(TEAM_BOOK)
    responses["GET " + url("last_week")] = answer(crew())
    out = run_page("#/team?period=last_week", responses, READ + OPTIONS + TEAMS + """
run(async function () {
  return { options: options(), rows: rowNames(), teams: teamsPanel() !== null,
    reads: requests.map(function (r) { return r.url; }) };
});
""")
    assert out["options"] == [
        ["", "All techs"],
        ["region:NASA", R_IN + "NASA"], [STONE, T_IN + "Avery Stone"], [PIKE, T_IN + "Jordan Pike"],
        [REYES, T_IN + "Morgan Reyes"], [OKAFOR, T_IN + "Sam Okafor"],
        ["region:APAC", R_IN + "APAC"], [SANTOS, T_IN + "Mara Santos"], [HALE, T_IN + "Quinn Hale"]]
    assert out["rows"] == ["Avery Stone", "Jordan Pike", "Mara Santos", "Morgan Reyes", "Quinn Hale", "Sam Okafor"]
    assert out["teams"] is False
    assert "/api/me" in out["reads"] and "/api/people" not in out["reads"]


def test_a_manager_sees_their_teams_side_by_side(tmp_path, conn, version):
    """Managers design: over more than one day, a manager's Activity has the Teams panel between the tiles and the
    grid: a row per team lead with any tech shown (Team lead, Region, Techs, Tickets touched, Per shift worked, Shifts
    worked, Sick days, Flags), by region, then lead. A tech on two teams counts in both rows (Mara: Alex's and
    Paolo's); there is no total row. Clicking a row, or the lead's name, shows only that team, keeping the period."""
    out = manager_page(tmp_path, "#/team?period=last_week", {url("last_week"): crew()}, TEAMS + """
  var panel = teamsPanel();
  var trs = panel.querySelectorAll("tbody tr");
  var out = { titles: titles(), sub: panel.querySelectorAll(".sub")[0].textContent,
    table: panel.querySelectorAll("table")[0].className, wrap: panel.querySelectorAll("table")[0].parentNode.className,
    heads: panel.querySelectorAll("thead th").map(function (th) { return [th.textContent, th.className]; }),
    rows: trs.map(function (tr) { return tr.children.map(function (td) { return td.textContent; }); }),
    marks: trs.map(function (tr) {
      return tr.children.slice(6).map(function (td) { return td.children[0].className; });
    }),
    links: trs.map(function (tr) { return links(tr.children[0]); }), cls: trs.map(function (tr) { return tr.className; }) };
  fire(trs[1], "click");
  out.clicked = location.hash;
  return out;
""")
    assert out["titles"] == ["Teams · 3", "Tickets touched per day", "Flags · 3"]
    assert out["sub"] == TEAMS_SUB
    assert (out["table"], out["wrap"]) == ("atable", "scroll")
    assert out["heads"] == [["Team lead", ""], ["Region", ""], ["Techs", "num"], ["Tickets touched", "num"],
                            ["Per shift worked", "num"], ["Shifts worked", "num"], ["Sick days", "num"],
                            ["Flags", "num"]]
    assert out["rows"] == [["Alex Kim", "NASA, APAC", "3", "61", "4.4", "14 of 15", "1", "2"],
                           ["Jamie Rivera", "NASA", "2", "35", "3.5", "10 of 10", "0", "1"],
                           ["Paolo Ramos", "APAC", "1", "30", "6", "5 of 5", "0", "1"]]
    assert out["marks"] == [["chip fail", "chip bad"], ["muted", "chip bad"], ["muted", "chip bad"]]
    assert out["links"] == [[{"text": name, "href": "#/team?" + urlencode({"period": "last_week", "team": lead})}]
                            for name, lead in (("Alex Kim", ALEX), ("Jamie Rivera", JAMIE), ("Paolo Ramos", PAOLO))]
    assert out["cls"] == ["rowlink"] * 3
    assert out["clicked"] == "#/team?period=last_week&team=jamie.rivera%40example.com"


def test_the_teams_panel_needs_more_than_one_team_and_more_than_one_day(tmp_path, conn, version):
    """With one team shown the Teams panel isn't drawn: a team picked, or techs that are all on one team. Nor over one
    day, which has no grid of days."""
    one_day = [WEEK[1]]
    jamie_only = {"people": [TEAM_BOOK["people"][1]], "roster": []}
    picked = manager_page(tmp_path, "#/team?period=last_week&team=jamie.rivera%40example.com",
                          {url("last_week", team=JAMIE): crew([PIKE, OKAFOR])}, TEAMS + "return [titles(), teamsPanel() !== null];")
    alone = manager_page(tmp_path, "#/team?period=last_week", {url("last_week"): crew()}, TEAMS + "return teamsPanel() !== null;",
                         people=jamie_only)
    day = manager_page(tmp_path, f"#/team?period=custom&start={one_day[0]}&end={one_day[0]}",
                       {url("custom", one_day[0], one_day[0]): crew(days=one_day)},
                       TEAMS + "return [titles(), teamsPanel() !== null];")
    assert picked == [["Tickets touched per day", "Flags · 1"], False]
    assert alone is False
    assert day == [["Everyone"], False]


def test_a_managers_grid_lists_techs_by_region_then_team_lead_then_name(tmp_path, conn, version):
    """Managers design: a manager's grid lists the techs by region (NASA, EMEA, APAC), then team lead, then name; a tech
    on two teams goes with the first of their leads by name, and a tech on no team last in their region."""
    out = manager_page(tmp_path, "#/team?period=last_week", {url("last_week"): crew()}, "return rowNames();")
    assert out == ["Avery Stone", "Morgan Reyes", "Jordan Pike", "Sam Okafor", "Mara Santos", "Quinn Hale"]


def test_a_manager_is_told_a_team_lead_sets_a_missing_shift(tmp_path, conn, version):
    """A manager can't set shifts (each team lead does), so the line above the grid for a tech with no shift says who
    hasn't set it, with no link to Settings."""
    one = week_body()
    one["techs"].append({"email": CASEY, "name": "Casey Nguyen", "has_shift": False})
    several = week_body()
    several["techs"] += [{"email": CASEY, "name": "Casey Nguyen", "has_shift": False},
                         {"email": DREW, "name": "Drew Patel", "has_shift": False}]
    shown = """
  var notes = panelTitled("Tickets touched per day").querySelectorAll(".placeholder");
  return notes.map(function (n) { return [n.textContent, links(n).length]; });
"""
    assert manager_page(tmp_path, "#/team", {url(): one}, shown) == [
        ["Casey Nguyen's team lead hasn't set their shift yet.", 0]]
    assert manager_page(tmp_path, "#/team", {url(): several}, shown) == [
        ["The team leads of Casey Nguyen and Drew Patel haven't set their shifts yet.", 0]]


def test_this_week_is_the_default_so_the_address_leaves_it_out(tmp_path, conn, version):
    """T-143: Activity's period defaults to This week (so far); Last week is written into the address."""
    body = week_body()
    out = snapshot(tmp_path, "#/team?period=last_week", {url("last_week"): body}, """
  choose("ta-period", "this_week");
  var hashes = [location.hash];
  choose("ta-period", "last_week");
  hashes.push(location.hash);
  return hashes;
""")
    assert out == ["#/team", "#/team?period=last_week"]


def test_earlier_and_later_step_the_range(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  fire(byId("ta-earlier"), "click");
  return location.hash;
""")
    assert out == "#/team?period=custom&start=2026-09-27&end=2026-10-03"


# -- flags -------------------------------------------------------------------------------------------------


def flagged_days(count: int) -> dict:
    days = iso_days("2026-10-04", count)
    avery = [cell(d, tickets=5) for d in days]
    avery[1] = cell(days[1], "missed", flags=[flag("missed")])
    avery[2] = cell(days[2], tickets=5, signin="08:01", first="09:45", last="16:00", gap_min=150,
                    flags=[flag("late_start", 105), flag("long_gap", 150)])
    blake = [cell(d, tickets=3) for d in days]
    blake[3] = cell(days[3], "zero", signin="08:04", flags=[flag("no_activity")])
    blake[4] = cell(days[4], tickets=3, first="08:10", last="14:20", flags=[flag("early_finish", 160)])
    return activity(days, [row(AVERY, "Avery Cole", avery), row(BLAKE, "Blake Moreno", blake)], preset="custom",
                    label="A range")


def test_flags_by_tech_only_over_seven_days(tmp_path, conn, version):
    scenario = """
  var panel = panelTitled("Flags by tech");
  var every = panels().filter(function (p) { return /^(Every flag|Flags ·)/.test(titleOf(p)); })[0];
  return { titles: titles(), by: panel ? { heads: heads(panel), rows: bodyRows(panel).map(function (tr) {
      return tr.children.map(function (c) { return c.textContent; }); }),
    chips: bodyRows(panel)[0].querySelectorAll(".chip").map(function (c) { return c.className; }) } : null,
    every: titleOf(every) };
"""
    seven = snapshot(tmp_path, custom(iso_days("2026-10-04", 7))[0], {custom(iso_days("2026-10-04", 7))[1]: flagged_days(7)},
                     scenario)
    assert "Flags by tech" not in seven["titles"] and seven["by"] is None
    assert seven["every"] == "Flags · 5"
    eight_days = iso_days("2026-10-04", 8)
    eight = snapshot(tmp_path, custom(eight_days)[0], {custom(eight_days)[1]: flagged_days(8)}, scenario)
    assert eight["titles"].index("Flags by tech") < eight["titles"].index("Every flag · 5")
    assert eight["by"]["heads"] == ["Tech", "Sick day", "Signed in, no activity", "Late start", "Early finish",
                                    "Long gap", "Total"]
    assert eight["by"]["rows"] == [["Avery Cole", "1", "0", "1", "0", "1", "3"],
                                   ["Blake Moreno", "0", "1", "0", "1", "0", "2"]]
    assert eight["by"]["chips"] == ["chip fail", "chip bad", "chip bad"]


def test_every_flag_lists_each_flag_with_what_was_recorded(tmp_path, conn, version):
    days = iso_days("2026-10-04", 8)
    hash_, api_url = custom(days)
    out = snapshot(tmp_path, hash_, {api_url: flagged_days(8)}, """
  var panel = panels().filter(function (p) { return /^Every flag/.test(titleOf(p)); })[0];
  return { wrap: panel.querySelectorAll("table")[0].parentNode.className, heads: heads(panel), rows: bodyRows(panel).map(function (tr) {
    return { cls: tr.className, cells: tr.children.map(function (c) { return c.textContent; }),
      chip: tr.querySelectorAll(".chip")[0].className }; }) };
""")
    assert out["wrap"] == "scroll list-scroll", "a long list of flags scrolls inside its panel"
    assert out["heads"] == ["Day", "Tech", "Flag", "What was recorded", ""]
    assert [r["cls"] for r in out["rows"]] == ["rowlink failrow", "rowlink flagrow", "rowlink flagrow",
                                               "rowlink flagrow", "rowlink flagrow"]
    assert [r["cells"][:3] for r in out["rows"]] == [
        ["Mon Oct 5", "Avery Cole", "Sick day"],
        ["Tue Oct 6", "Avery Cole", "Late start · 1 h 45 min"],
        ["Tue Oct 6", "Avery Cole", "Long gap · 2 h 30 min"],
        ["Wed Oct 7", "Blake Moreno", "Signed in, no activity"],
        ["Thu Oct 8", "Blake Moreno", "Early finish · 2 h 40 min"]]
    assert out["rows"][1]["cells"][3] == "Signed in 8:01 AM · active 9:45 AM – 4:00 PM"
    assert out["rows"][0]["cells"][3] == "No sign-in"
    assert [r["chip"] for r in out["rows"]][:2] == ["chip fail", "chip bad"]


def test_every_flag_says_so_when_nothing_is_flagged(tmp_path, conn, version):
    body = activity(WEEK, [row(AVERY, "Avery Cole", [cell(d, tickets=4) for d in WEEK])])
    out = snapshot(tmp_path, "#/team", {url(): body}, """
  var panel = panelTitled("Flags · 0");
  return panel.querySelectorAll(".placeholder")[0].textContent;
""")
    assert out == "Nothing flagged in this period."


def test_every_flag_row_has_mark_day_off(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(week_body())
    responses["POST /api/time-off"] = answer({"id": 1}, 201)
    out = run_page("#/team", responses, READ + """
run(async function () {
  var panel = panelTitled("Flags · 3");
  var rows = bodyRows(panel.querySelectorAll("table")[0]);
  var labels = rows.map(function (tr) { return [tr.children[2].textContent, tr.querySelectorAll("button").map(function (b) { return b.textContent; })]; });
  fire(buttonIn(rows[2], "Mark as day off"), "click");
  var choice = rows[2].querySelectorAll("button").map(function (b) { return b.textContent; });
  later();
  fire(buttonIn(rows[2], "PTO"), "click");
  await settle();
  var post = requests.filter(function (r) { return r.method === "POST"; });
  return { labels: labels, choice: choice, posts: post.map(function (r) { return [r.url, r.body]; }),
    gets: getRequests("/api/activity").length };
});
""")
    assert out["labels"] == [["Sick day", ["Mark as day off"]], ["Signed in, no activity", ["Mark as day off"]],
                             ["Long gap · 2 h 30 min", ["Mark as day off"]]]
    assert out["choice"] == ["Sick", "PTO", "Cancel"]
    assert out["posts"] == [["/api/time-off", {"email": AVERY, "first": WEEK[4], "last": WEEK[4], "note": "",
                                               "kind": "pto"}]]
    assert out["gets"] == 2, "the screen is read again after the change"


def test_every_flag_row_marks_a_sick_day_and_undo_shows_on_a_marked_one(tmp_path, conn, version):
    marked = cell(WEEK[1], "missed", time_off="sick", flags=[flag("missed")])
    plain = cell(WEEK[2], "missed", flags=[flag("missed")])
    body = activity(WEEK, [row(AVERY, "Avery Cole", [marked, plain])])
    body["days"] = [WEEK[1], WEEK[2]]
    body["period"].update(start=WEEK[1], end=WEEK[2])
    hash_, api_url = custom(body["days"])
    responses = page_responses(api_for(tmp_path))
    responses["GET " + api_url] = answer(body)
    responses["POST /api/time-off"] = answer({"id": 1}, 201)
    out = run_page(hash_, responses, READ + """
run(async function () {
  var rows = bodyRows(panelTitled("Flags · 2").querySelectorAll("table")[0]);
  var labels = rows.map(function (tr) { return tr.querySelectorAll("button").map(function (b) { return b.textContent; }); });
  fire(buttonIn(rows[1], "Mark as day off"), "click");
  later();
  fire(buttonIn(rows[1], "Sick"), "click");
  await settle();
  return { labels: labels, posts: requests.filter(function (r) { return r.method === "POST"; })
    .map(function (r) { return r.body; }) };
});
""")
    assert out["labels"] == [["Undo day off"], ["Mark as day off"]]
    assert out["posts"] == [{"email": AVERY, "first": WEEK[2], "last": WEEK[2], "note": "", "kind": "sick"}]


def test_a_manager_has_no_mark_day_off_in_every_flag(tmp_path, conn, version):
    """Managers design: time off is each team lead's (the server refuses a manager's), so a manager's Every flag draws
    no Mark as day off or Undo day off, nor the column they sit in."""
    responses = signed_in_manager(page_responses(api_for(tmp_path)))
    responses["GET " + url()] = answer(week_body())
    out = run_page("#/team", responses, READ + """
run(async function () {
  var panel = panelTitled("Flags · 3");
  var table = panel.querySelectorAll("table")[0];
  return { heads: heads(table), buttons: panel.querySelectorAll("button").length,
    cells: bodyRows(table).map(function (tr) { return tr.children.length; }),
    reads: requests.map(function (r) { return r.url; }) };
});
""")
    assert "/api/me" in out.pop("reads"), "signed in as the manager"
    assert out == {"heads": ["Day", "Tech", "Flag", "What was recorded"], "buttons": 0, "cells": [4, 4, 4]}


# -- links -------------------------------------------------------------------------------------------------


def day_hash(email: str, first: str, last: str | None = None) -> str:
    return "#/team?" + urlencode({"tech": email, "period": "custom", "start": first, "end": last or first})


def test_cell_links_to_tech_day(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  var rows = bodyRows(heat());
  return rows.map(function (tr) {
    return { name: desc(tr.querySelectorAll(".who a")[0]),
      cells: hcs(tr).map(function (c) { return [c.tag, c.href]; }) };
  });
""")
    avery, blake = out
    assert avery["name"]["href"] == day_hash(AVERY, WEEK[0], WEEK[-1]), "a name opens the same range for that tech"
    assert blake["name"]["href"] == day_hash(BLAKE, WEEK[0], WEEK[-1])
    assert avery["cells"] == [
        ["A", day_hash(AVERY, WEEK[0])], ["A", day_hash(AVERY, WEEK[1])], ["A", day_hash(AVERY, WEEK[2])],
        ["A", day_hash(AVERY, WEEK[3])], ["A", day_hash(AVERY, WEEK[4])], ["SPAN", None],
        ["A", day_hash(AVERY, WEEK[6])]]
    assert [tag for tag, _ in blake["cells"]] == ["SPAN", "SPAN", "A", "A", "A", "SPAN", "SPAN"], \
        "untracked, time off and rest days are not links; a zero day is"


def test_the_address_names_the_tech_and_range_the_page_asks_for(tmp_path, conn, version):
    c = cell(WEEK[1], tickets=4)
    out = day_page(tmp_path, AVERY, c, day_body(c), "run(async function () { return titles(); });")
    assert out == ["The day", "What Avery did, in order", "Recent days", "Open tickets assigned to Avery now"]


# -- one tech over a range ---------------------------------------------------------------------------------


def tech_body() -> dict:
    days = iso_days("2026-10-07", 14)   # Wed Oct 7 - Tue Oct 20
    cells = [
        cell(days[0], tickets=6, replies=7, notes=2, calls=2, solved=1, signin="08:04", first="08:10", last="16:35",
             gap_min=150, gap_start="11:00", flags=[flag("long_gap", 150)]),
        cell(days[1], "zero", signin="08:05", flags=[flag("no_activity")]),
        cell(days[2], "rest"),
        cell(days[3], "off", tickets=2, replies=2, first="10:05", last="11:40"),
        cell(days[4], "missed", flags=[flag("missed")]),
        cell(days[5], "pto"),
        cell(days[6], tickets=10, replies=11, notes=2, calls=1, solved=2, signin="08:00", first="08:07", last="16:32",
             gap_min=75, gap_start="12:00"),
        cell(days[7], tickets=5, replies=6, notes=1, signin="08:11", first="08:20", last="16:40"),
        cell(days[8], tickets=4, replies=4, notes=1, solved=1, first="08:30", last="15:00"),
        cell(days[9], "rest"), cell(days[10], "rest")]
    cells += [cell(d, "future") for d in days[11:]]
    return activity(days, [row(AVERY, "Avery Cole", cells)], preset="custom", label="Oct 7 – Oct 20, 2026")


def test_tech_range_shows_calendar_and_day_by_day(tmp_path, conn, version):
    days = iso_days("2026-10-07", 14)
    hash_ = "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": days[0], "end": days[-1]})
    api_url = url("custom", days[0], days[-1], AVERY)
    out = snapshot(tmp_path, hash_, {api_url: tech_body()}, """
  var cal = heat();
  var table = panelTitled("Day by day").querySelectorAll("table")[0];
  return { head: head(), tiles: tiles(), titles: titles(), cal: cal.className.split(" "), calHeads: heads(cal),
    weeks: bodyRows(cal).map(function (tr) {
      return { label: tr.querySelectorAll("td.wklabel")[0].textContent, tds: tr.children.length,
        cells: hcs(tr).map(function (c) { return c.text; }),
        dates: hcs(tr).map(function (c) { return c.date; }),
        nums: tr.querySelectorAll(".num").map(function (td) { return td.textContent; }) };
    }),
    blanks: bodyRows(cal).map(function (tr) { return tr.children.filter(function (td) {
      return td.tagName === "TD" && td.children.length === 0 && td.textContent === ""; }).length; }),
    table: table.className, wrap: table.parentNode.className, tableHeads: heads(table),
    days: bodyRows(table).map(function (tr) { return { cls: tr.className,
      cells: tr.children.map(function (td) { return td.textContent; }),
      link: tr.querySelectorAll("a")[0].getAttribute("href") }; }) };
""")
    assert out["head"] == "Avery Cole · Oct 7 – Oct 20, 2026"
    assert out["titles"] == ["Tickets touched per day", "Day by day"], "no flag tables for one tech"
    assert out["wrap"] == "scroll list-scroll", "a long Day by day list scrolls inside its panel"
    assert out["tiles"] == [
        ["Tickets per shift", "6.3", "25 tickets over 4 shifts"],
        ["Shifts worked", "4 of 6", "2 with no activity"],
        ["Replies per shift", "7", "1.5 notes per shift"],
        ["Solved", "4", "3 calls logged"],
        ["Flags", "3", "1 sick day"]]
    assert out["cal"] == ["heat", "cal"]
    assert out["calHeads"] == ["", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Total", "Flags"]
    assert [w["label"] for w in out["weeks"]] == ["Week of Oct 4", "Week of Oct 11", "Week of Oct 18"]
    assert [w["tds"] for w in out["weeks"]] == [10, 10, 10]
    assert [len(w["cells"]) for w in out["weeks"]] == [4, 7, 3]
    assert out["blanks"] == [3, 0, 4], "a week that starts mid-week is padded to Sunday-Saturday"
    assert out["weeks"][0]["cells"] == ["6", "0", "Rest", "2"]
    assert out["weeks"][1]["cells"] == ["Sick", "PTO", "10", "5", "4", "Rest", "Rest"]
    assert [w["dates"] for w in out["weeks"]] == [["7", "8", "9", "10"], ["11", "12", "13", "14", "15", "16", "17"],
                                                  ["18", "19", "20"]], "each day's date in its corner"
    assert [w["nums"] for w in out["weeks"]] == [["8", "2"], ["19", "1"], ["0", "0"]]
    assert out["table"].split() == ["atable", "one-line"]
    assert out["tableHeads"] == ["Day", "Shift", "Signed in", "First – last action", "Longest gap", "Tickets",
                                 "Replies", "Notes", "Calls", "Solved", "Flags", "Day off"]
    shown = out["days"]
    assert [d["cells"][0] for d in shown] == ["Wed Oct 7", "Thu Oct 8", "Sat Oct 10", "Sun Oct 11", "Mon Oct 12",
                                              "Tue Oct 13", "Wed Oct 14", "Thu Oct 15"], \
        "days neither scheduled nor worked are left out"
    assert shown[0]["cells"] == ["Wed Oct 7", "8 AM – 5 PM", "8:04 AM", "8:10 AM – 4:35 PM", "2 h 30 min", "6", "7",
                                 "2", "2", "1", "Long gap · 2 h 30 min", "Mark as day off"]
    assert shown[1]["cells"][1:5] == ["8 AM – 5 PM", "8:05 AM", "-", "-"]
    assert shown[2]["cells"][1] == "Rest day" and shown[2]["cells"][3] == "10:05 AM – 11:40 AM"
    assert shown[4]["cells"][1] == "Time off"
    assert [d["cls"].split() for d in shown] == [
        ["rowlink", "flagrow"], ["rowlink", "flagrow"], ["rowlink", "restrow"], ["rowlink", "failrow"],
        ["rowlink", "restrow"], ["rowlink"], ["rowlink"], ["rowlink"]]
    assert shown[0]["link"] == day_hash(AVERY, days[0])


# -- day-off buttons in the one tech's Day by day table (Task 15) ---------------------------------------------

DBD = iso_days("2026-10-05", 10)
DBD_STATES = ["worked", "zero", "missed", "pto", "off", "rest", "untracked", "future", "unsynced", "worked"]
BUBBLE = """
function bubble(el, type) {
  var event = { target: el, stopped: false, preventDefault: function () {}, stopPropagation: function () { this.stopped = true; } };
  for (var node = el; node && !event.stopped; node = node.parentNode) {
    (node.listeners && node.listeners[type] || []).forEach(function (f) { f(event); });
  }
}
function dayRows() {
  var table = panelTitled("Day by day").querySelectorAll("table")[0];
  return { table: table, rows: bodyRows(table) };
}
"""


def day_by_day_page(tmp_path, scenario, extra=None, marked=(), manager=False):
    """The Day by day table of DBD; the days at the positions in `marked` are marked sick days. `manager`: the hosted
    page, signed in as a manager (test_web_edition)."""
    cells = [cell(d, s, tickets=3 if s in ("worked", "off") else 0, flags=[flag("missed")] if s == "missed" else [],
                  **({"time_off": "sick"} if i in marked else {}))
             for i, (d, s) in enumerate(zip(DBD, DBD_STATES))]
    hash_ = "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": DBD[0], "end": DBD[-1]})
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url("custom", DBD[0], DBD[-1], AVERY)] = answer(activity(DBD, [row(AVERY, "Avery Cole", cells)], preset="custom",
                                                  label="Oct 5 – Oct 14, 2026"))
    responses.update(extra or {})
    if manager:
        signed_in_manager(responses)
    return run_page(hash_, responses, READ + BUBBLE + scenario)


def test_day_by_day_mark_day_off_on_scheduled_rows(tmp_path, conn, version):
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  var last = t.table.querySelectorAll("thead th").pop();
  var buttons = t.rows.map(function (tr) { return tr.querySelectorAll("button.small").map(function (b) { return b.textContent; }); });
  bubble(buttonIn(t.rows[2], "Mark as day off"), "click");
  var choice = t.rows[2].querySelectorAll("button").map(function (b) { return b.textContent; });
  later();
  bubble(buttonIn(t.rows[2], "Sick"), "click");
  await settle();
  var posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  return { head: [last.textContent, last.querySelectorAll(".sr-only").length], days: t.rows.map(function (tr) { return tr.children[0].textContent; }),
    buttons: buttons, choice: choice, posts: posts, gets: getRequests("/api/activity").length };
});
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out["head"] == ["Day off", 1]
    assert out["days"] == ["Mon Oct 5", "Tue Oct 6", "Wed Oct 7", "Thu Oct 8", "Fri Oct 9", "Wed Oct 14"]
    assert out["buttons"] == [["Mark as day off"], ["Mark as day off"], ["Mark as day off"], ["Undo day off"], [],
                              ["Mark as day off"]]
    assert out["choice"] == ["Sick", "PTO", "Cancel"]
    assert out["posts"] == [["/api/time-off", {"email": AVERY, "first": DBD[2], "last": DBD[2], "note": "",
                                               "kind": "sick"}]]
    assert out["gets"] == 2, "the range is read again after the change"


def test_day_by_day_undo_on_a_marked_sick_row(tmp_path, conn, version):
    path = "/api/time-off/day?email=avery%40example.com&day=" + DBD[2]
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  var buttons = t.rows.map(function (tr) { return tr.querySelectorAll("button.small").map(function (b) { return b.textContent; }); });
  bubble(buttonIn(t.rows[2], "Undo day off"), "click");
  await settle();
  return { buttons: buttons, deletes: requests.filter(function (r) { return r.method === "DELETE"; }).map(function (r) { return r.url; }),
    posts: requests.filter(function (r) { return r.method === "POST"; }).length };
});
""", {"DELETE " + path: answer({}, 204)}, marked=(2,))
    assert out["buttons"][2] == ["Undo day off"] and out["buttons"][1] == ["Mark as day off"]
    assert out["deletes"] == [path] and out["posts"] == 0


def test_cancelling_the_choice_marks_nothing_and_sends_nothing(tmp_path, conn, version):
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  var tr = t.rows[0];
  var names = function () { return tr.querySelectorAll("button").map(function (b) { return b.textContent; }); };
  var before = names();
  bubble(buttonIn(tr, "Mark as day off"), "click");
  var during = names();
  bubble(buttonIn(tr, "Cancel"), "click");
  await settle();
  return { before: before, during: during, after: names(), requests: requests.filter(function (r) { return r.method !== "GET"; }).length,
    gets: getRequests("/api/activity").length };
});
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out["before"] == ["Mark as day off"] and out["during"] == ["Sick", "PTO", "Cancel"]
    assert out["after"] == ["Mark as day off"], "Cancel puts the button back"
    assert out["requests"] == 0 and out["gets"] == 1, "nothing is sent and the screen is not read again"


def test_day_by_day_undo_on_pto_row(tmp_path, conn, version):
    path = "/api/time-off/day?email=avery%40example.com&day=" + DBD[3]
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  bubble(buttonIn(t.rows[3], "Undo day off"), "click");
  await settle();
  return { deletes: requests.filter(function (r) { return r.method === "DELETE"; }).map(function (r) { return r.url; }),
    posts: requests.filter(function (r) { return r.method === "POST"; }).length, gets: getRequests("/api/activity").length };
});
""", {"DELETE " + path: answer({}, 204)})
    assert out == {"deletes": [path], "posts": 0, "gets": 2}


def test_a_manager_has_no_day_off_column_in_day_by_day(tmp_path, conn, version):
    """Managers design: a manager marks no day off, so Day by day has no Day off column, marked days included."""
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  return { heads: t.table.querySelectorAll("thead th").length, cells: t.rows.map(function (tr) { return tr.children.length; }),
    buttons: t.table.querySelectorAll("button").length };
});
""", marked=(2,), manager=True)
    assert out == {"heads": 11, "cells": [11] * 6, "buttons": 0}


def test_day_by_day_no_button_on_rest_off_untracked_future_unsynced(tmp_path, conn, version):
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  return t.rows.map(function (tr) { var end = tr.children[tr.children.length - 1];
    return [tr.children[0].textContent, end.children.length, end.textContent]; });
});
""")
    by_day = {r[0]: r for r in out}
    assert by_day["Fri Oct 9"][1:] == [0, ""], "a rest day that was worked (off) has an empty cell"
    assert [r[0] for r in out] == ["Mon Oct 5", "Tue Oct 6", "Wed Oct 7", "Thu Oct 8", "Fri Oct 9", "Wed Oct 14"], \
        "rest, untracked, future and unsynced days are not listed, so they carry no button"


def test_day_by_day_button_click_does_not_open_the_day(tmp_path, conn, version):
    out = day_by_day_page(tmp_path, """
run(async function () {
  var t = dayRows();
  var before = location.hash;
  var button = buttonIn(t.rows[0], "Mark as day off");
  var found = button !== null;
  if (found) { bubble(button, "click"); }
  await settle();
  var afterButton = location.hash;
  bubble(t.rows[0].children[1], "click");
  return { found: found, same: before === afterButton, opens: location.hash !== before };
});
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out == {"found": True, "same": True, "opens": True}, "the button leaves the address alone; the rest of the row opens the day"


def test_day_by_day_error_inline(tmp_path, conn, version):
    out = day_by_day_page(tmp_path, """
run(async function () {
  var alerts = 0;
  globalThis.alert = function () { alerts++; };
  var t = dayRows();
  bubble(buttonIn(t.rows[0], "Mark as day off"), "click");
  later();
  var button = buttonIn(t.rows[0], "PTO");
  bubble(button, "click");
  var inFlight = button.disabled;
  await settle();
  var end = t.rows[0].children[t.rows[0].children.length - 1];
  var message = end.querySelectorAll("span.message")[0];
  return { inFlight: inFlight, text: message.textContent, cls: message.className, enabled: !button.disabled, alerts: alerts };
});
""", {"POST /api/time-off": answer({"detail": "Choose a tech."}, 400)})
    assert out == {"inFlight": True, "text": "Choose a tech.", "cls": "message error", "enabled": True, "alerts": 0}


def test_a_tech_calendar_with_every_week_whole(tmp_path, conn, version):
    body = long_body(56)
    api_url = custom(iso_days("2026-08-23", 56), AVERY)[1]
    hash_ = "#/team?" + urlencode({"tech": AVERY, "period": "custom", "start": "2026-08-23", "end": "2026-10-17"})
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  var rows = bodyRows(heat());
  return { n: rows.length, first: rows[0].querySelectorAll("td.wklabel")[0].textContent,
    last: rows[rows.length - 1].querySelectorAll("td.wklabel")[0].textContent,
    texts: hcs(rows[0]).map(function (c) { return c.text; }),
    dates: rows.slice(1, 2).concat(rows.slice(5, 6)).map(function (r) { return hcs(r).map(function (c) { return c.date; }); }) };
""")
    assert out["n"] == 8 and out["first"] == "Week of Aug 23" and out["last"] == "Week of Oct 11"
    assert out["texts"] == ["Rest", "5", "5", "5", "5", "5", "Rest"]
    assert out["dates"] == [["30", "31", "Sep 1", "2", "3", "4", "5"], ["27", "28", "29", "30", "Oct 1", "2", "3"]], \
        "the 1st of a month names it"


# -- who has no row ----------------------------------------------------------------------------------------


def test_a_tech_with_no_shift_is_named_above_the_grid(tmp_path, conn, version):
    body = week_body()
    body["techs"].append({"email": CASEY, "name": "Casey Nguyen", "has_shift": False})
    out = snapshot(tmp_path, "#/team", {url(): body}, """
  var panel = panelTitled("Tickets touched per day");
  var note = panel.querySelectorAll(".placeholder")[0];
  var table = panel.querySelectorAll(".heat.split")[0];
  var index = function (el) { return panel.children.indexOf(el); };
  return { text: note.textContent, link: links(note), rows: bodyRows(table).length,
    above: index(note) < index(table.parentNode) };
""")
    assert out["text"] == "Set Casey Nguyen's shift in Settings to see their days."
    assert out["link"] == [{"text": "Settings", "href": "#/settings"}]
    assert out["rows"] == 2 and out["above"] is True


def test_several_techs_with_no_shift_share_one_line(tmp_path, conn, version):
    body = week_body()
    body["techs"] += [{"email": CASEY, "name": "Casey Nguyen", "has_shift": False},
                      {"email": "drew@example.com", "name": "Drew Patel", "has_shift": False}]
    out = snapshot(tmp_path, "#/team", {url(): body}, """
  var notes = screenEl.querySelectorAll(".placeholder");
  return notes.map(function (n) { return n.textContent; });
""")
    assert out == ["Set shifts in Settings for Casey Nguyen and Drew Patel to see their days."]


def test_a_team_with_nobody_in_it_has_no_grid(tmp_path, conn, version):
    body = activity(WEEK, [], techs=[])
    out = snapshot(tmp_path, "#/team", {url(): body}, """
  return { grids: screenEl.querySelectorAll("table").length + screenEl.querySelectorAll(".heat").length, text: screenEl.querySelectorAll(".placeholder")[0].textContent };
""")
    assert out["grids"] == 0 and out["text"] == "No techs are on the team yet. Add them in Settings."


# -- Refresh -----------------------------------------------------------------------------------------------


def running_body(running: bool) -> dict:
    body = week_body()
    body["synced"] = {"at": "2026-10-17T14:42:00Z", "status": "running" if running else "complete", "running": running}
    return body


def test_refresh_disabled_while_running(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): running_body(True)}, """
  var button = byId("ta-refresh");
  var msg = button.parentNode.querySelectorAll("span.message")[0];
  return { text: button.textContent, disabled: !!button.disabled, cls: button.className, message: msg.textContent };
""")
    assert out == {"text": "Refresh", "disabled": True, "cls": "small", "message": "Updating…"}


def test_refresh_shows_when_the_data_was_last_updated(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): running_body(False)}, """
  var button = byId("ta-refresh");
  return { disabled: !!button.disabled, message: button.parentNode.querySelectorAll("span.message")[0].textContent };
""")
    assert out["disabled"] is False
    assert re.fullmatch(r"Updated (\d{1,2}:\d{2}\s[AP]M|\w{3} \d{1,2}, \d{1,2}:\d{2}\s[AP]M)", out["message"]), out


def test_refresh_without_a_sync_yet(tmp_path, conn, version):
    body = running_body(False)
    body["synced"] = {"at": None, "status": None, "running": False}
    out = snapshot(tmp_path, "#/team", {url(): body}, "return byId('ta-refresh').parentNode.querySelectorAll('span.message')[0].textContent;")
    assert out == "Not updated yet"


POLL = """
var timers = [];
var realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = function (f, ms) { if (ms === 5000) { timers.push(f); return 0; } return realSetTimeout(f, ms); };
function status() {
  var button = byId("ta-refresh");
  return { disabled: !!button.disabled, message: button.parentNode.querySelectorAll("span.message")[0].textContent };
}
"""


def test_a_running_sync_is_polled_every_five_seconds_until_it_finishes(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(running_body(True))
    out = run_page("#/team", responses, READ + POLL + """
run(async function () {
  var out = { first: status(), timers: timers.length, gets: getRequests("/api/activity").length };
  timers.shift()();                       // five seconds pass: still running
  await settle();
  out.still = { timers: timers.length, gets: getRequests("/api/activity").length, status: status() };
  RESPONSES["GET /api/activity"] = { status: 200, body: %s };
  timers.shift()();                       // and again: finished
  await settle();
  out.done = { timers: timers.length, gets: getRequests("/api/activity").length };
  out.status = status();
  return out;
});
""" % json.dumps(running_body(False)))
    assert out["first"] == {"disabled": True, "message": "Updating…"} and out["timers"] == 1 and out["gets"] == 1
    assert out["still"]["timers"] == 1 and out["still"]["gets"] == 2
    assert out["still"]["status"] == {"disabled": True, "message": "Updating…"}
    assert out["done"] == {"timers": 0, "gets": 3}, "once it is done the screen is redrawn and polling stops"
    assert out["status"]["disabled"] is False and out["status"]["message"].startswith("Updated ")


def test_leaving_the_screen_stops_the_polling(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(running_body(True))
    out = run_page("#/team", responses, READ + POLL + """
run(async function () {
  await go("#/settings");
  var before = getRequests("/api/activity").length;
  timers.shift()();
  await settle();
  return { before: before, after: getRequests("/api/activity").length, timers: timers.length };
});
""")
    assert out == {"before": 1, "after": 1, "timers": 0}, "a poll that fires after the route changed does nothing"


def test_clicking_refresh_starts_a_sync_and_polls(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(running_body(False))
    responses["POST /api/activity/sync"] = answer({"started": True}, 202)
    out = run_page("#/team", responses, READ + POLL + """
run(async function () {
  fire(byId("ta-refresh"), "click");
  await settle();
  var posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  return { posts: posts, status: status(), timers: timers.length };
});
""")
    assert out["posts"] == [["/api/activity/sync", {"kind": "refresh"}]]
    assert out["status"] == {"disabled": True, "message": "Updating…"} and out["timers"] == 1


def test_a_sync_already_running_is_waited_for(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(running_body(False))
    responses["POST /api/activity/sync"] = answer({"detail": "A team activity sync is already running."}, 409)
    out = run_page("#/team", responses, READ + POLL + """
run(async function () {
  fire(byId("ta-refresh"), "click");
  await settle();
  return { status: status(), timers: timers.length };
});
""")
    assert out == {"status": {"disabled": True, "message": "Updating…"}, "timers": 1}


def test_a_refused_refresh_says_why_and_can_be_retried(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url()] = answer(running_body(False))
    responses["POST /api/activity/sync"] = answer({"detail": "Zendesk is not set up."}, 400)
    out = run_page("#/team", responses, READ + POLL + """
run(async function () {
  fire(byId("ta-refresh"), "click");
  await settle();
  return { status: status(), timers: timers.length };
});
""")
    assert out == {"status": {"disabled": False, "message": "Zendesk is not set up."}, "timers": 0}


# -- month names -------------------------------------------------------------------------------------------


LAYOUT = """
// Oct 25 - Nov 7: Oct's seven columns are 100px each, Nov's seven 20px each; the panel's line is 1000px wide.
globalThis.layout = function (el) {
  var mo = el.getAttribute("data-mo");
  if (mo !== null) {
    var i = el.parentNode.children.filter(function (c) { return c.getAttribute("data-mo") !== null; }).indexOf(el);
    var left = i < 7 ? 200 + i * 100 : 900 + (i - 7) * 20;
    return { rect: { left: left, right: left + (i < 7 ? 100 : 20), width: i < 7 ? 100 : 20 } };
  }
  if (el.classList.contains("month-tag")) { return { rect: { left: 0, right: 60, width: 60 } }; }
  if (el.classList.contains("pills")) { return { rect: { left: 0, right: 1000, width: 1000 } }; }
  if (el.classList.contains("pill")) { return { rect: { left: 0, right: PILL_RIGHT, width: PILL_RIGHT } }; }
  return {};
};
"""


@pytest.mark.parametrize("pill_right, october, november", [
    (150, "550px", "970px"),     # centered over their columns
    (700, "746px", "970px")])    # October's centre would hide behind the pills: kept 16px clear of them
def test_month_names_sit_over_their_columns_on_the_by_day_line(tmp_path, conn, version, pill_right, october, november):
    days = iso_days("2026-10-25", 14)
    cells = [cell(d, tickets=5) for d in days]
    body = activity(days, [row(AVERY, "Avery Cole", cells)], preset="custom", label="Oct 25 – Nov 7, 2026")
    hash_, api_url = custom(days)
    responses = page_responses(api_for(tmp_path))
    responses["GET " + api_url] = answer(body)
    out = run_page(hash_, responses, READ + "var PILL_RIGHT = %d;" % pill_right + "\n" + LAYOUT + """
run(async function () {
  var bar = screenEl.querySelectorAll(".pills.grid-mode.has-months")[0];
  var tags = bar.querySelectorAll(".months .month-tag");
  return { tags: tags.map(function (t) { return [t.textContent, t.style.left]; }),
    sub: screenEl.querySelectorAll(".sub.has-months").length };
});
""")
    assert out["sub"] == 0, "with a By day line the names go there, not on the description"
    assert out["tags"] == [["October", october], ["Nov", november]],         "the full name for a span over 140px, the short one under it"


def test_month_names_go_on_the_description_line_for_a_week(tmp_path, conn, version):
    days = iso_days("2026-10-28", 7)
    body = activity(days, [row(AVERY, "Avery Cole", [cell(d, tickets=5) for d in days])], preset="custom",
                    label="Oct 28 – Nov 3, 2026")
    hash_, api_url = custom(days)
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  var sub = screenEl.querySelectorAll(".sub.has-months")[0];
  return { months: sub.querySelectorAll(".months").length, pills: screenEl.querySelectorAll(".pills").length,
    text: sub.children[0].textContent };
""")
    assert out["months"] == 1 and out["pills"] == 0
    assert out["text"].startswith("Each tech's shifts from Settings")


# -- the rest of the screen --------------------------------------------------------------------------------


def test_footer_note_text(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  var last = screenEl.children[screenEl.children.length - 1];
  return [last.tagName, last.className, last.textContent];
""")
    assert out == ["P", "note", NOTE]


def test_the_footer_ends_the_one_day_placeholder_too(tmp_path, conn, version):
    body = activity([WEEK[1]], [row(AVERY, "Avery Cole", [cell(WEEK[1], tickets=4)])], preset="custom", label="Mon Oct 5")
    out = snapshot(tmp_path, "#/team?period=custom&start=%s&end=%s" % (WEEK[1], WEEK[1]),
                   {url("custom", WEEK[1], WEEK[1]): body}, "return screenEl.children[screenEl.children.length - 1].textContent;")
    assert out == NOTE


def test_legend_lists_each_kind_of_cell(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  var legend = screenEl.querySelectorAll(".heat-legend")[0];
  return legend.children.map(function (item) { return item.textContent; });
""")
    assert out == ["6Tickets touched", "6With a flag", "0Signed in, did nothing", "✕Sick day",
                   "3Worked a rest day", "Rest day", "Time off"]


def test_the_week_legend_explains_the_shade(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  return screenEl.querySelectorAll(".heat-legend")[0].children.map(function (item) { return item.textContent; });
""")
    assert out[0].startswith("38Tickets that week") and "shade" in out[0]
    assert out[-1] == "Click a week to open it"


def test_an_unknown_tech_or_a_bad_range_shows_the_error_and_a_way_back(tmp_path, conn, version):
    responses = page_responses(api_for(tmp_path))
    responses["GET " + url(tech="nobody@example.com")] = answer({"detail": "That tech isn't on the team."}, 400)
    out = run_page("#/team?tech=nobody%40example.com", responses, READ + """
run(async function () {
  return { error: screenEl.querySelectorAll(".message.error")[0].textContent, links: links(screenEl) };
});
""")
    assert out["error"] == "That tech isn't on the team."
    assert out["links"] == [{"text": "Show this week", "href": "#/team"}]


def test_a_tech_who_left_still_shows_in_the_picker_while_chosen(tmp_path, conn, version):
    body = activity(WEEK, [row(AVERY, "Avery Cole", [cell(d, tickets=4) for d in WEEK])], techs=[])
    out = snapshot(tmp_path, "#/team?tech=" + AVERY.replace("@", "%40"), {url(tech=AVERY): body}, """
  var select = byId("ta-tech");
  return [select.value, select.querySelectorAll("option").map(function (o) { return o.textContent; })];
""")
    assert out == [AVERY, ["All techs", "Avery Cole"]]


def test_nothing_in_the_screen_says_agent(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, "return screenEl.textContent;")
    assert not re.search(r"\bagents?\b", out, re.IGNORECASE)


# -- the stylesheet ----------------------------------------------------------------------------------------


def section() -> tuple[str, str]:
    """(the Team activity section of app.css, the rest of it), comments removed."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    start = css.index("/* -- team activity")
    end = css.index("/* From 1120px")
    strip = lambda text: re.sub(r"/\*.*?\*/", "", text, flags=re.S)   # noqa: E731
    return strip(css[start:end]), strip(css[:start] + css[end:])


def subjects(css: str) -> list[tuple[str, frozenset]]:
    """(tag, classes) of the last compound of every selector in `css`: the element a rule styles."""
    found = []
    for selectors, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for selector in selectors.split(","):
            compound = re.split(r"[\s>+~]+", selector.strip())[-1]
            tag = re.match(r"[a-zA-Z][\w-]*", compound)
            found.append((tag.group(0) if tag else "", frozenset(re.findall(r"\.([A-Za-z][\w-]*)", compound))))
    return found


# Names the new section deliberately shares with existing parts: it styles those parts' own elements.
SHARED = {"chip", "atable", "pill", "pills", "num", "tile", "note", "muted", "panel"}


def collisions(new: str, old: str) -> list[str]:
    """New class names that a rule outside the section would also style on the same element."""
    mine = {c for _, classes in subjects(new) for c in classes} - SHARED
    owned = mine | SHARED
    clash = set()
    for tag, classes in subjects(old):
        if tag in {"td", "tr", "th", "button", "input", "select", "label", "li"}:
            continue   # an element of mine never has these tags and the new class together
        if classes & mine and classes <= owned:
            clash |= classes & mine
    return sorted(clash)


def test_new_classes_do_not_collide():
    new, old = section()
    assert {"heat", "hc", "month-tag", "has-months", "heat-legend"} <= {c for _, cl in subjects(new) for c in cl},         "the Team activity section exists"
    assert collisions(new, old) == []


def test_the_collision_check_catches_the_mlabel_mistake():
    new = ".month-tag { position: absolute; } .mlabel { color: red; }"
    old = "@media (max-width: 640px) { .mlabel { display: none; } } .atable .num { color: red; }"
    assert collisions(new, old) == ["mlabel"]
    assert collisions(".heat td.num { color: red; }", ".atable .num { color: red; } .d.bad { color: red; }") == []


def test_the_team_activity_tokens_and_rules_exist():
    css = app_css()
    for token in ("heat-1", "heat-2", "heat-3", "heat-4", "heat-cell-h", "heat-col-min", "heat-name-w", "heat-cell-pad"):
        assert token in tokens(css), token
    new, _ = section()
    for needle in (".heat", ".hc", ".hc.h1", ".hc.h4", ".hc.zero", ".hc.miss", ".hc.off", ".hc.rest", ".hc.pto",
                   ".hc.future", ".heat.split", ".heat.mid", ".heat.weeks", ".heat-names", ".heat-days",
                   ".heat-ends", ".heat-row", ".heat-head",
                   ".heat-legend", ".grid-mode", ".has-months", ".month-tag", ".chip.bad", ".chip.fail",
                   "tr.failrow", "tr.restrow", ".fdot", ".shifts"):
        assert needle in new, needle
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", new), "the section uses tokens only"


def test_a_cell_never_wraps_its_number_and_longer_ranges_scroll_instead_of_shrinking():
    """Past a month the By day grid keeps --heat-day-min-mid columns and scrolls sideways inside its panel; there
    is no compact or colour-only grid any more (owner, 2026-10-21)."""
    css = top_level(app_css())
    assert values(css, ".hc", "white-space") == ["nowrap"]
    assert "heat-day-min-compact" not in tokens(css) and "heat-day-min-tiny" not in tokens(css)
    assert ".heat.compact" not in app_css() and ".heat.tiny" not in app_css()


def test_the_day_columns_share_the_middle_part_equally_and_scroll_below_a_minimum():
    """The names part and the Total and Flags part take only their own width; the day columns share what is left
    equally, each never narrower than its density's token, so a panel too narrow for them scrolls the days part
    sideways instead of squeezing the cells (T-078; three parts since T-126)."""
    css = top_level(app_css())
    for token in ("heat-day-min-mid", "heat-tot-w", "heat-flag-w"):
        assert token in tokens(css), token
    assert values(css, ".heat-names", "width") == ["var(--heat-name-w)"]
    assert values(css, ".heat-ends > .heat-row", "grid-template-columns") == ["var(--heat-tot-w) var(--heat-flag-w)"]
    assert values(css, ".heat-days > .heat-row", "grid-template-columns") == [
        "repeat(var(--days), minmax(var(--heat-day-min), 1fr))"]
    assert values(css, ".heat.split", "--heat-day-min") == ["var(--heat-col-min)"]
    assert values(css, ".heat.split.mid", "--heat-day-min") == ["var(--heat-day-min-mid)"]
    assert values(css, ".heat-days", "overflow-x") == ["auto"]
    assert values(css, ".heat-days", "min-width") == ["0"]


def test_the_by_week_grid_fills_the_width_like_by_day_and_scrolls_below_its_minimum():
    """Week columns share the middle part like day columns; --heat-week-min is their minimum (--days counts the
    week columns), so a narrow panel scrolls (T-079), and a week cell's height is its own token."""
    css = top_level(app_css())
    for token in ("heat-week-min", "heat-week-cell-h"):
        assert token in tokens(css), token
    assert values(css, ".heat.split.weeks", "--heat-day-min") == ["var(--heat-week-min)"]
    assert values(css, ".heat.split.weeks", "--heat-cell-h") == ["var(--heat-week-cell-h)"]


def test_one_property_sets_the_heat_gap_for_every_part_of_the_grid():
    """Every space around and between heat cells reads --heat-gap: the calendar table's border-spacing and the
    three-part grid's padding and gaps (T-079, T-126)."""
    css = top_level(app_css())
    assert values(css, ".heat", "border-spacing") == ["var(--heat-gap)"]
    assert values(css, ".heat", "--heat-gap") == ["var(--sp-2)"]
    assert values(css, ".heat.split", "padding-inline") == ["var(--heat-gap)"]
    assert values(css, ".heat-days", "padding-inline") == ["var(--heat-gap)"]
    assert values(css, ".heat-row", "column-gap") == ["var(--heat-gap)"]
    for part in (".heat-names", ".heat-days", ".heat-ends"):
        assert values(css, part, "row-gap") == ["var(--heat-gap)"], part
        assert values(css, part, "padding-block") == ["var(--heat-gap)"], part


def test_a_by_week_grid_is_in_three_parts_and_tells_its_stylesheet_how_many_weeks(tmp_path, conn, version):
    body = long_body(56)
    hash_, api_url = custom(iso_days("2026-08-23", 56))
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  var t = heat();
  return { cls: t.className.split(" "), style: t.getAttribute("style"),
    parts: t.children.map(function (c) { return c.className; }), weeks: heads(t).length - 3 };
""")
    assert out["cls"] == ["heat", "split", "weeks"]
    assert out["style"] == "--days: %d" % out["weeks"] and out["weeks"] == 8
    assert out["parts"] == ["heat-names", "heat-days", "heat-ends"]


def test_a_day_grid_tells_its_stylesheet_how_many_days_it_has(tmp_path, conn, version):
    out = snapshot(tmp_path, "#/team", {url(): week_body()},
                   "return screenEl.querySelector('.heat.split').getAttribute('style');")
    assert out == "--days: 7"


def test_the_app_bar_reaches_the_side_panel_over_the_page_padding():
    """No page-colored strip between the side panel and the app bar (Eyes, T-061)."""
    css = top_level(app_css())
    assert values(css, ".shell .appbar", "clip-path") == ["inset(0 -100vmax -1px calc(-1 * var(--pad-page-x)))"]


def test_app_js_declares_no_function_twice():
    """A second declaration of a name silently replaces the first for the whole page (Team activity's
    clockLabel once replaced the Tickets timeline's)."""
    names = re.findall(r"^  function (\w+)", (STATIC / "app.js").read_text(encoding="utf-8"), re.M)
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_a_tech_with_a_shift_but_no_row_is_not_named_as_missing_a_shift(tmp_path, conn, version):
    body = week_body()
    body["techs"].append({"email": CASEY, "name": "Casey Nguyen", "has_shift": True})
    out = snapshot(tmp_path, "#/team", {url(): body}, "return screenEl.querySelectorAll('.placeholder').length;")
    assert out == 0, "only a tech with no shift version at all is named"


# -- the day chart -----------------------------------------------------------------------------------------

CHART_PAGE_JS = """
globalThis.layout = function () { return { clientWidth: 1000, scrollWidth: 1000 }; };
var el = document.createElement("div");
function tag(e, name) { return e.tagName.toLowerCase() === name; }
function cls(e) { return e.className.split(/\\s+/); }
function find(name, klass) {
  return el.findAll(function (e) { return tag(e, name) && cls(e).indexOf(klass) >= 0; });
}
function titleOf(e) { return e.findAll(function (x) { return tag(x, "desc"); }).map(function (t) { return t.textContent; }); }
"""

DAY_DATA = """{ shift: { start: "08:00", end: "17:00" }, signin: "08:11", gap: { start: "09:30", minutes: 245 },
  actions: [{ at: "08:30", kind: "reply" }, { at: "09:00", kind: "note" }, { at: "09:30", kind: "call" },
            { at: "13:35", kind: "status" }] }"""


def test_day_chart_draws_shift_actions_signin_and_gap():
    out = run_node(node_prelude() + CHART_PAGE_JS + "Charts.dayChart(el, " + DAY_DATA + ");" + """
var svg = el.children[0];
var bars = find("rect", "bar");
process.stdout.write(JSON.stringify({
  cls: svg.className, width: svg.getAttribute("width"),
  band: find("rect", "period-band").length, shiftLabel: find("text", "shiftlabel").map(function (t) { return t.textContent; }),
  bars: bars.map(function (b) { return b.className; }), barTitles: bars.map(titleOf),
  hits: find("rect", "day-hit").map(titleOf),
  marker: find("line", "marker").length, gap: find("rect", "gapband").length,
  gapLabel: find("text", "gaplabel").map(function (t) { return t.textContent; }),
  ticks: find("text", "tick").map(function (t) { return t.textContent; }),
  grid: find("line", "grid").length, base: find("line", "base").length
}));
""")
    assert out["cls"] == "chart" and out["width"] == "1000"
    assert out["band"] == 1 and out["shiftLabel"] == ["Shift 8 AM – 5 PM"]
    assert out["bars"] == ["bar act", "bar note", "bar call", "bar act"]
    assert out["barTitles"] == [[], [], [], []], "the bars are thin; a wider hover target over each minute carries it"
    assert out["hits"] == [["8:30 AM"], ["9:00 AM"], ["9:30 AM"], ["1:35 PM"]], "by default a moment's card is its time"
    assert out["marker"] == 1
    assert out["gap"] == 1 and out["gapLabel"] == ["4 h 05 m with nothing recorded"]
    assert out["ticks"] == ["6 AM", "8 AM", "10 AM", "Noon", "2 PM", "4 PM", "6 PM", "signed in 8:11 AM"]
    assert out["grid"] == 14, "a line each hour from 6 AM to 7 PM"
    assert out["base"] == 1


def test_day_chart_text_uses_tick_class():
    out = run_node(node_prelude() + CHART_PAGE_JS + "Charts.dayChart(el, " + DAY_DATA + ");" + """
var texts = el.findAll(function (e) { return tag(e, "text"); });
process.stdout.write(JSON.stringify(texts.map(function (t) { return [t.textContent, t.className]; })));
""")
    classes = dict(out)
    assert classes["signed in 8:11 AM"] == "tick" and classes["Noon"] == "tick"
    assert classes["Shift 8 AM – 5 PM"] == "shiftlabel" and classes["4 h 05 m with nothing recorded"] == "gaplabel"
    assert set(classes.values()) == {"tick", "shiftlabel", "gaplabel"}


def test_day_chart_widens_the_axis_so_no_mark_is_clipped():
    out = run_node(node_prelude() + CHART_PAGE_JS + """
Charts.dayChart(el, { shift: null, signin: null, gap: null,
  actions: [{ at: "05:15", kind: "reply" }, { at: "20:30", kind: "reply" }] });
process.stdout.write(JSON.stringify({ ticks: find("text", "tick").map(function (t) { return t.textContent; }),
  xs: find("rect", "bar").map(function (b) { return Number(b.getAttribute("x")); }),
  band: find("rect", "period-band").length, marker: find("line", "marker").length }));
""")
    assert out["ticks"] == ["6 AM", "8 AM", "10 AM", "Noon", "2 PM", "4 PM", "6 PM", "8 PM"]
    assert min(out["xs"]) >= 0 and max(out["xs"]) + 4 <= 1000
    assert out["band"] == 0 and out["marker"] == 0, "no shift and no sign-in draw neither"


# -- the day views -----------------------------------------------------------------------------------------

DAY = WEEK[1]   # Mon Oct 5


def day_url(email: str, day: str) -> str:
    return "/api/activity/day?" + urlencode({"tech": email, "day": day})


def action(at, kind, ticket=486001, subject="Printer jam", event=1, old=None, new=None) -> dict:
    return {"at": at, "kind": kind, "old": old, "new": new, "ticket_id": ticket, "subject": subject, "event_id": event}


def day_body(c: dict, actions=None, recent=None, tickets=None) -> dict:
    return {"cell": c, "actions": actions or [], "recent": recent or [c],
            "open": {"as_of": "2026-10-17T14:42:00Z", "tickets": tickets or []}}


def day_page(tmp_path, email, c, body, scenario, extra=None, subdomain="acme", manager=False):
    """Load the day view of `email` on DAY and run `scenario`; `extra` adds more answers. `manager`: the hosted page,
    signed in as a manager (test_web_edition)."""
    hash_ = "#/team?" + urlencode({"tech": email, "period": "custom", "start": DAY, "end": DAY})
    responses = page_responses(api_for(tmp_path))
    responses["GET /api/settings"]["body"]["zendesk_subdomain"] = subdomain
    responses["GET " + url("custom", DAY, DAY, email)] = answer(
        activity([DAY], [row(email, "Avery Cole", [c])], preset="custom", label="Mon Oct 5"))
    responses["GET " + day_url(email, DAY)] = answer(body)
    responses.update(extra or {})
    if manager:
        signed_in_manager(responses)
    return run_page(hash_, responses, READ + scenario)


FULL_DAY = cell(DAY, tickets=4, replies=3, notes=1, calls=1, solved=1, signin="08:11", first="08:30", last="16:50",
                gap_min=245, gap_start="09:30", flags=[flag("late_start", 11), flag("long_gap", 245)])
FULL_ACTIONS = [
    action("08:30", "reply", 486001, "Printer jam", 11),
    action("08:30", "status", 486001, "Printer jam", 11, "open", "pending"),
    action("09:00", "note", 486002, "Refund", 12),
    action("13:35", "assignee", 486003, None, 13, None, AVERY),
    action("13:40", "status", 486001, "Printer jam", 14, "pending", "solved"),
    action("13:40", "reply", 486001, "Printer jam", 14),
    action("16:50", "call", 486004, "Lost login", 15),
]
HEADS_TEXT = """
function texts(el) { return el.children.map(function (td) { return td.textContent; }); }
"""


def test_same_save_joined_into_one_row(tmp_path, conn, version):
    mine = [{"ticket_id": 486003, "subject": None, "status": "open", "last_acted": None}]
    out = day_page(tmp_path, AVERY, FULL_DAY, day_body(FULL_DAY, FULL_ACTIONS, tickets=mine), HEADS_TEXT + """
run(async function () {
  var panel = panelTitled("What Avery did, in order");
  var rows = panel.querySelectorAll("tbody tr").map(function (tr) {
    var cell = tr.children[2];
    var a = cell ? cell.querySelectorAll("a")[0] : null;
    return { cls: tr.className, cells: texts(tr), href: a ? a.getAttribute("href") : null,
      subject: cell ? cell.querySelectorAll("span.muted").map(function (m) { return m.textContent; }) : null };
  });
  return { heads: panel.querySelectorAll("thead th").map(function (th) { return th.textContent; }), rows: rows,
    table: panel.querySelectorAll("table")[0].className };
});
""")
    assert out["heads"] == ["Time", "Action", "Ticket"] and out["table"] == "atable"
    assert [r["cells"] for r in out["rows"]] == [
        ["8:30 AM", "Public reply · Set Pending", "#486001 Printer jam"],
        ["9:00 AM", "Internal note", "#486002 Refund"],
        ["4 h 05 m", "Nothing recorded"],
        ["1:35 PM", "Took the ticket", "#486003 -"],
        ["1:40 PM", "Public reply · Solved", "#486001 Printer jam"],
        ["4:50 PM", "Call logged", "#486004 Lost login"]]
    assert out["rows"][2]["cls"] == "flagrow"
    assert out["rows"][0]["href"] == "https://acme.zendesk.com/agent/tickets/486001"
    assert out["rows"][0]["subject"] == ["Printer jam"] and out["rows"][3]["subject"] == ["-"]


def test_a_moment_on_the_day_chart_says_what_was_done_and_opens_the_ticket(tmp_path, conn, version):
    """The owner, 2026-10-22: a moment on the day chart has a hovercard saying what the tech did then and on which
    ticket (every save at that minute), and clicking it opens the ticket in Zendesk; with no subdomain it opens
    nothing."""
    scenario = """
run(async function () {
  var opened = [];
  window.open = function (url) { opened.push(url); };
  var hits = screenEl.querySelectorAll("rect.day-hit");
  var cards = hits.map(function (r) { return r.querySelectorAll("desc")[0].textContent; });
  fire(hits[0], "click");
  return { cards: cards, opened: opened, links: hits.map(function (r) { return r.classList.contains("link"); }) };
});
"""
    out = day_page(tmp_path, AVERY, FULL_DAY, day_body(FULL_DAY, FULL_ACTIONS), scenario)
    opens = "\nClick to open the ticket in Zendesk."
    assert out["cards"] == ["8:30 AM\nPublic reply · Set Pending · #486001 Printer jam" + opens,
                            "9:00 AM\nInternal note · #486002 Refund" + opens,
                            "1:35 PM\nTook the ticket · #486003" + opens,
                            "1:40 PM\nPublic reply · Solved · #486001 Printer jam" + opens,
                            "4:50 PM\nCall logged · #486004 Lost login" + opens]
    assert out["opened"] == ["https://acme.zendesk.com/agent/tickets/486001"]
    assert all(out["links"])
    bare = day_page(tmp_path, AVERY, FULL_DAY, day_body(FULL_DAY, FULL_ACTIONS), scenario, subdomain="")
    assert bare["opened"] == [] and not any(bare["links"])


def test_recent_days_step_a_week_earlier_and_later(tmp_path, conn, version):
    """The owner, 2026-10-22: Recent days steps a week back or forward with Earlier and Later, read from the grid's
    answer for the tech, while the day shown stays the page's."""
    week = iso_days("2026-09-29", 7)               # Tue Sep 29 to Mon Oct 5 (DAY)
    before = iso_days("2026-09-22", 7)
    recent = [cell(d, tickets=n + 1) for n, d in enumerate(week)]
    earlier = activity(before, [row(AVERY, "Avery Cole", [cell(d, tickets=20 + n) for n, d in enumerate(before)])],
                       preset="custom")
    out = day_page(tmp_path, AVERY, recent[-1], day_body(recent[-1], recent=recent), """
run(async function () {
  function shown() {
    var p = panelTitled("Recent days");
    return { span: p.querySelectorAll(".recent-span")[0].textContent,
      days: p.querySelectorAll("tbody tr").map(function (tr) { return tr.children[0].textContent; }) };
  }
  var first = shown();
  fire(byId("rd-earlier"), "click");
  await settle();
  return { first: first, earlier: shown(), head: head(), asked: getRequests("/api/activity?").map(function (r) { return r.url; }) };
});
""", extra={"GET " + url("custom", before[0], before[-1], AVERY): answer(earlier)})
    assert out["first"]["span"] == "Sep 29 – Oct 5" and out["first"]["days"][-1] == "Mon Oct 5"
    assert out["earlier"]["span"] == "Sep 22 – Sep 28"
    assert out["earlier"]["days"] == ["Tue Sep 22", "Wed Sep 23", "Thu Sep 24", "Fri Sep 25", "Sat Sep 26",
                                      "Sun Sep 27", "Mon Sep 28"]
    assert url("custom", before[0], before[-1], AVERY) in out["asked"]
    assert out["head"].startswith("Avery Cole · Mon Oct 5"), "the day shown stays the page's"


def test_open_tickets_pick_out_aging_ones(tmp_path, conn, version):
    """The owner, 2026-10-22: an aging open ticket (1:1 Prep's rule) is tinted with its age beside its status, and
    the subtitle counts open and aging tickets."""
    tickets = [{"ticket_id": 21, "subject": "Login loop", "status": "open", "last_acted": "2026-10-05T10:00",
                "age_days": 12, "aging": True},
               {"ticket_id": 22, "subject": "Refund", "status": "pending", "last_acted": None, "age_days": 3,
                "aging": False}]
    out = day_page(tmp_path, AVERY, FULL_DAY, day_body(FULL_DAY, FULL_ACTIONS, tickets=tickets), """
run(async function () {
  var p = panelTitled("Open tickets assigned to Avery now");
  return { sub: p.querySelectorAll(".sub")[0].textContent,
    rows: p.querySelectorAll("tbody tr").map(function (tr) {
      return [tr.className, tr.children[1].textContent]; }) };
});
""")
    assert out["sub"].endswith(" · 2 open · 1 aging")
    assert out["rows"] == [["flagrow aging", "OpenAging · 12 days"], ["", "Pending"]]


def test_the_words_for_each_kind_of_change(tmp_path, conn, version):
    kinds = [action("08:30", "assignee", 1, None, 21, None, "5"), action("08:31", "group", 2, None, 22),
             action("08:32", "priority", 3, None, 23, "low", "urgent"), action("08:33", "fields", 4, None, 24),
             action("08:34", "created", 5, None, 25), action("08:35", "merge", 6, None, 26),
             action("08:36", "status", 7, None, 27, "new", "open")]
    c = cell(DAY, tickets=7, first="08:30", last="08:36")
    out = day_page(tmp_path, AVERY, c, day_body(c, kinds), """
run(async function () {
  return panelTitled("What Avery did, in order").querySelectorAll("tbody tr").map(function (tr) {
    return tr.children[1].textContent;
  });
});
""")
    assert out == ["Changed the assignee", "Moved group", "Priority urgent", "Edited fields", "Created the ticket", "Merged",
                   "Set Open"], "each part starts with a capital"


def test_an_assignee_change_says_took_the_ticket_only_when_the_record_names_this_tech(tmp_path, conn, version):
    kinds = [action("08:30", "assignee", 1, None, 21, None, AVERY),
             action("08:31", "assignee", 2, None, 22, None, BLAKE),
             action("08:32", "assignee", 3, None, 23, None, "777"),
             # the tech took it, then solved it: it is no longer open, and still reads "took the ticket"
             action("09:00", "assignee", 4, None, 24, None, AVERY),
             action("09:30", "status", 4, None, 25, "open", "solved")]
    c = cell(DAY, tickets=4, first="08:30", last="09:30")
    out = day_page(tmp_path, AVERY, c, day_body(c, kinds, tickets=[]), """
run(async function () {
  return panelTitled("What Avery did, in order").querySelectorAll("tbody tr").map(function (tr) {
    return tr.children[1].textContent;
  });
});
""")
    assert out == ["Took the ticket", "Changed the assignee", "Changed the assignee", "Took the ticket", "Solved"]


@pytest.mark.parametrize("state, text", [
    ("untracked", "Not on the team on this day."),
    ("future", "Still to come."),
    ("unsynced", "Not synced yet."),
])
def test_an_empty_day_off_the_team_or_still_to_come_says_so(tmp_path, conn, version, state, text):
    c = cell(DAY, state)
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  return panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0].textContent;
});
""")
    assert out == text


def test_tiles_chart_and_side_panels_of_a_tech_day(tmp_path, conn, version):
    recent = [cell("2026-10-03", "rest"), cell("2026-10-04", "missed", flags=[flag("missed")]), FULL_DAY]
    tickets = [{"ticket_id": 486009, "subject": "Router", "status": "pending", "last_acted": "2026-10-02T15:12"},
               {"ticket_id": 486010, "subject": None, "status": "open", "last_acted": None}]
    out = day_page(tmp_path, AVERY, FULL_DAY, day_body(FULL_DAY, FULL_ACTIONS, recent, tickets), """
function rowsOf(p) {
  return p.querySelectorAll("tbody tr").map(function (tr) { return tr.children.map(function (td) { return td.textContent; }); });
}
run(async function () {
  var open = panelTitled("Open tickets assigned to Avery now");
  var chartPanel = panelTitled("The day");
  var recent = panelTitled("Recent days");
  return {
    head: head(), tiles: tiles(), titles: titles(),
    vtext: screenEl.querySelectorAll(".tile .v.v-text").map(function (v) { return v.textContent; }),
    warn: screenEl.querySelectorAll(".tile .d .warn").map(function (w) { return w.textContent; }),
    legend: chartPanel.querySelectorAll(".chart-legend")[0].textContent,
    swatches: chartPanel.querySelectorAll(".chart-legend .sw").map(function (s) { return s.className; }),
    svgs: chartPanel.querySelectorAll("svg").length,
    chips: chartPanel.querySelectorAll(".timeline-chips .chip").map(function (c) { return c.textContent; }),
    recent: rowsOf(recent), recentLinks: links(recent), recentBold: recent.querySelectorAll("b").map(function (b) { return b.textContent; }),
    open: rowsOf(open), openHeads: open.querySelectorAll("thead th").map(function (th) { return th.textContent; }),
    layout: screenEl.querySelectorAll(".panel-row.cols-2-1").length, stack: screenEl.querySelectorAll(".stack").length
  };
});
""")
    assert out["head"] == "Avery Cole · Mon Oct 5 · 8 AM – 5 PM"
    assert out["tiles"] == [
        ["Tickets touched", "4", "1 call logged"],
        ["Signed in", "8:11 AM", "on time"],
        ["First – last action", "8:30 AM – 4:50 PM", "8 h 20 m span"],
        ["Replies", "3", "1 internal note"],
        ["Solved", "1", ""]]
    assert out["vtext"] == ["8:11 AM", "8:30 AM – 4:50 PM"] and out["warn"] == []
    assert out["titles"] == ["The day", "What Avery did, in order", "Recent days", "Open tickets assigned to Avery now"]
    assert out["swatches"] == ["sw act", "sw note", "sw call", "sw shift"]
    assert out["legend"] == "Reply or ticket changeInternal noteLogged callShift"
    assert out["svgs"] == 1 and out["chips"] == ["Late start · 11 min", "Long gap · 4 h 5 min"]
    assert out["recent"] == [["Sun Oct 4", "0", "0", "0", "1"], ["Mon Oct 5", "4", "3", "1", "2"]], "rest days are left out"
    assert out["recentBold"] == ["Mon Oct 5"] and len(out["recentLinks"]) == 1
    assert out["recentLinks"][0]["href"] == day_hash(AVERY, "2026-10-04")
    assert out["openHeads"] == ["Ticket", "Status", "Avery last acted"]
    assert out["open"] == [["#486009 Router", "Pending", "Oct 2, 3:12 PM"], ["#486010 -", "Open", "-"]]
    assert out["layout"] == 1 and out["stack"] == 1


def test_a_late_sign_in_is_a_warning_and_the_gap_is_drawn(tmp_path, conn, version):
    c = cell(DAY, tickets=1, replies=1, signin="08:45", first="09:00", last="09:00", gap_min=150, gap_start="09:00",
             flags=[flag("late_start", 45), flag("long_gap", 150)])
    out = day_page(tmp_path, AVERY, c, day_body(c, [action("09:00", "reply")]), """
run(async function () {
  var warn = screenEl.querySelectorAll(".tile .d .warn").map(function (w) { return w.textContent; });
  return { warn: warn, gap: screenEl.querySelectorAll("rect.gapband").length };
});
""")
    assert out == {"warn": ["45 m after shift start"], "gap": 1}


@pytest.mark.parametrize("state", ["off", "pto"])
def test_a_worked_rest_day_or_time_off_draws_no_shift(tmp_path, conn, version, state):
    """Final review F2: a day the tech was not due to work has no shift band and no "after shift start", even if
    the cell carries the hours in force."""
    c = cell(DAY, state, tickets=1, replies=1, signin="10:30", first="10:40", last="10:40")
    out = day_page(tmp_path, AVERY, c, day_body(c, [action("10:40", "reply")]), """
run(async function () {
  var chartPanel = panelTitled("The day");
  return { band: chartPanel.querySelectorAll("rect.period-band").length,
    label: chartPanel.querySelectorAll("text.shiftlabel").length, tiles: tiles(),
    warn: screenEl.querySelectorAll(".tile .d .warn").map(function (w) { return w.textContent; }) };
});
""")
    assert out["band"] == 0 and out["label"] == 0
    assert out["tiles"][1] == ["Signed in", "10:30 AM", ""] and out["warn"] == []


def test_a_manager_has_no_mark_day_off_on_the_day(tmp_path, conn, version):
    """Managers design: neither the day's heading nor its empty placeholder offers a manager Mark as day off."""
    c = cell(DAY, "missed", flags=[flag("missed")])
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  var box = panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0];
  return { buttons: screenEl.querySelectorAll("button").map(function (b) { return b.textContent; })
      .filter(function (text) { return /day off/.test(text); }),
    dayOff: screenEl.querySelectorAll(".day-off").length, text: box.textContent, actions: box.querySelectorAll(".actions").length };
});
""", manager=True)
    assert out == {"buttons": [], "dayOff": 0, "text": "Scheduled 8 AM – 5 PM, with no sign-in and no activity.",
                   "actions": 0}


def test_empty_day_placeholder_and_mark_day_off_posts(tmp_path, conn, version):
    c = cell(DAY, "missed", flags=[flag("missed")])
    extra = {"POST /api/time-off": answer({"id": 1}, 201)}
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  var panel = panelTitled("What Avery did, in order");
  var box = panel.querySelectorAll("div.placeholder")[0];
  var text = box.textContent;
  var buttons = box.querySelectorAll("button.small").map(function (b) { return b.textContent; });
  fire(buttonIn(box, "Mark as day off"), "click");
  var choice = box.querySelectorAll("button").map(function (b) { return b.textContent; });
  later();
  fire(buttonIn(box, "Sick"), "click");
  await settle();
  var posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  return { text: text, buttons: buttons, choice: choice, tables: panel.querySelectorAll("table").length, posts: posts,
    gets: getRequests("/api/activity/day").length };
});
""", extra)
    assert out["text"] == "Scheduled 8 AM – 5 PM, with no sign-in and no activity.Mark as day off"
    assert out["buttons"] == ["Mark as day off"] and out["tables"] == 0
    assert out["choice"] == ["Sick", "PTO", "Cancel"]
    assert out["posts"] == [["/api/time-off", {"email": AVERY, "first": DAY, "last": DAY, "note": "", "kind": "sick"}]]
    assert out["gets"] == 2, "the day is read again after the change"


def test_placeholder_day_off_error_shows_a_message_next_to_the_button(tmp_path, conn, version):
    c = cell(DAY, "missed", flags=[flag("missed")])
    extra = {"POST /api/time-off": answer({"detail": "Choose a tech."}, 400)}
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  var box = panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0];
  fire(buttonIn(box, "Mark as day off"), "click");
  later();
  var button = buttonIn(box, "Sick");
  fire(button, "click");
  await settle();
  var message = box.querySelectorAll(".message")[0];
  return { text: message.textContent, cls: message.className, enabled: !button.disabled };
});
""", extra)
    assert out == {"text": "Choose a tech.", "cls": "message error", "enabled": True}


def test_signed_in_but_idle_day_has_no_day_off_button_in_the_placeholder(tmp_path, conn, version):
    c = cell(DAY, "zero", signin="08:04", flags=[flag("no_activity")])
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  var box = panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0];
  return [box.textContent, box.querySelectorAll("button").length];
});
""")
    assert out == ["Signed in at 8:04 AM, then nothing recorded for the rest of the shift: no replies, notes, calls "
                   "or ticket changes.", 0]


def heading_controls(tmp_path, c, scenario_tail="", extra=None):
    """The day-off controls in the heading of the day view of `c`, and anything `scenario_tail` returns."""
    return day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  var heading = screenEl.querySelectorAll(".period-head")[0];
  var buttons = heading.querySelectorAll("button.small");
  var result = { head: head(), buttons: buttons.map(function (b) { return b.textContent; }),
    messages: heading.querySelectorAll("span.message").length };
""" + scenario_tail + """
  return result;
});
""", extra)


def test_day_view_mark_day_off_button_on_scheduled_day(tmp_path, conn, version):
    c = cell(DAY, tickets=4, replies=3, first="08:30", last="16:50", gap_min=245, gap_start="09:30",
             flags=[flag("long_gap", 245)])
    out = heading_controls(tmp_path, c, """
  fire(buttons[0], "click");
  result.choice = heading.querySelectorAll("button.small").map(function (b) { return b.textContent; });
  later();
  fire(buttonIn(heading, "PTO"), "click");
  await settle();
  result.posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  result.gets = getRequests("/api/activity/day").length;
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out["head"] == "Avery Cole · Mon Oct 5 · 8 AM – 5 PM"
    assert out["buttons"] == ["Mark as day off"] and out["messages"] == 1
    assert out["choice"] == ["Sick", "PTO", "Cancel"]
    assert out["posts"] == [["/api/time-off", {"email": AVERY, "first": DAY, "last": DAY, "note": "", "kind": "pto"}]]
    assert out["gets"] == 2, "the day is read again after the change"


@pytest.mark.parametrize("state", ["worked", "zero", "missed"])
def test_day_view_mark_day_off_button_on_every_scheduled_state(tmp_path, conn, version, state):
    out = heading_controls(tmp_path, cell(DAY, state))
    assert out["buttons"] == ["Mark as day off"]


def test_day_view_undo_day_off_on_pto_day(tmp_path, conn, version):
    c = cell(DAY, "pto")
    out = heading_controls(tmp_path, c, """
  fire(buttons[0], "click");
  await settle();
  result.deletes = requests.filter(function (r) { return r.method === "DELETE"; }).map(function (r) { return r.url; });
  result.posts = requests.filter(function (r) { return r.method === "POST"; }).length;
  result.gets = getRequests("/api/activity/day").length;
""", {"DELETE /api/time-off/day?email=avery%40example.com&day=" + DAY: answer({}, 204)})
    assert out["buttons"] == ["Undo day off"]
    assert out["deletes"] == ["/api/time-off/day?email=avery%40example.com&day=" + DAY] and out["posts"] == 0
    assert out["gets"] == 2, "the day is read again after the change"


def test_day_view_cancel_sends_nothing_and_restores_the_button(tmp_path, conn, version):
    out = heading_controls(tmp_path, cell(DAY, tickets=2), """
  fire(buttons[0], "click");
  fire(buttonIn(heading, "Cancel"), "click");
  await settle();
  result.after = heading.querySelectorAll("button.small").map(function (b) { return b.textContent; });
  result.writes = requests.filter(function (r) { return r.method !== "GET"; }).length;
  result.gets = getRequests("/api/activity/day").length;
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out["after"] == ["Mark as day off"] and out["writes"] == 0 and out["gets"] == 1


def test_day_view_undo_day_off_on_a_marked_sick_day(tmp_path, conn, version):
    c = cell(DAY, "missed", time_off="sick", flags=[flag("missed")])
    out = heading_controls(tmp_path, c, """
  fire(buttons[0], "click");
  await settle();
  result.deletes = requests.filter(function (r) { return r.method === "DELETE"; }).map(function (r) { return r.url; });
""", {"DELETE /api/time-off/day?email=avery%40example.com&day=" + DAY: answer({}, 204)})
    assert out["buttons"] == ["Undo day off"]
    assert out["deletes"] == ["/api/time-off/day?email=avery%40example.com&day=" + DAY]


def test_a_sick_day_says_whether_it_was_marked(tmp_path, conn, version):
    scenario = """
run(async function () {
  return { chips: panelTitled("The day").querySelectorAll(".timeline-chips .chip").map(function (c) { return [c.className, c.textContent]; }),
    empty: panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0].textContent,
    buttons: screenEl.querySelectorAll(".placeholder button").length };
});
"""
    marked = cell(DAY, "missed", time_off="sick", flags=[flag("missed")])
    automatic = cell(DAY, "missed", flags=[flag("missed")])
    got_marked = day_page(tmp_path, AVERY, marked, day_body(marked), scenario)
    got_automatic = day_page(tmp_path, AVERY, automatic, day_body(automatic), scenario)
    assert got_marked["chips"] == [["chip fail", "Sick day · marked"]]
    assert got_automatic["chips"] == [["chip fail", "Sick day · no sign-in or activity"]]
    assert got_marked["empty"] == "Marked as a sick day. No activity recorded."
    assert got_marked["buttons"] == 0, "the Undo button is in the heading; the placeholder offers no Mark button"


def test_work_recorded_on_a_marked_sick_day_still_lists_in_the_day_view(tmp_path, conn, version):
    c = cell(DAY, "missed", tickets=1, replies=1, signin="09:00", first="09:05", last="09:05", time_off="sick",
             flags=[flag("missed")])
    out = day_page(tmp_path, AVERY, c, day_body(c, [action("09:05", "reply")]), """
run(async function () {
  return { rows: panelTitled("What Avery did, in order").querySelectorAll("tbody tr").map(function (tr) { return tr.children[1].textContent; }),
    chips: panelTitled("The day").querySelectorAll(".timeline-chips .chip").map(function (x) { return x.textContent; }) };
});
""")
    assert out == {"rows": ["Public reply"], "chips": ["Sick day · marked"]}


@pytest.mark.parametrize("state", ["rest", "off", "untracked", "future", "unsynced"])
def test_no_day_off_button_on_rest_untracked_future_unsynced(tmp_path, conn, version, state):
    out = heading_controls(tmp_path, cell(DAY, state))
    assert out["buttons"] == [] and out["messages"] == 0
    assert out["head"].startswith("Avery Cole · Mon Oct 5")


def test_day_off_error_shown_inline(tmp_path, conn, version):
    c = cell(DAY, tickets=2)
    out = heading_controls(tmp_path, c, """
  var alerts = 0;
  globalThis.alert = function () { alerts++; };
  fire(buttons[0], "click");
  later();
  var sick = buttonIn(heading, "Sick");
  fire(sick, "click");
  await settle();
  var message = heading.querySelectorAll("span.message")[0];
  result.message = [message.textContent, message.className, !sick.disabled];
  result.alerts = alerts;
""", {"POST /api/time-off": answer({"detail": "Choose a tech."}, 400)})
    assert out["message"] == ["Choose a tech.", "message error", True]
    assert out["alerts"] == 0


def test_day_off_choice_ignores_a_click_right_after_it_opens(tmp_path, conn, version):
    """A double-click on Mark as day off lands the second click on PTO; it must not post until the owner chooses."""
    out = heading_controls(tmp_path, cell(DAY, tickets=2), """
  fire(buttons[0], "click");
  fire(buttonIn(heading, "PTO"), "click");
  await settle();
  result.early = requests.filter(function (r) { return r.method === "POST"; }).length;
  result.stillOpen = heading.querySelectorAll("button.small").map(function (b) { return b.textContent; });
  later(500);
  fire(buttonIn(heading, "PTO"), "click");
  await settle();
  result.posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return r.body; });
""", {"POST /api/time-off": answer({"id": 1}, 201)})
    assert out["early"] == 0 and out["stillOpen"] == ["Sick", "PTO", "Cancel"]
    assert out["posts"] == [{"email": AVERY, "first": DAY, "last": DAY, "note": "", "kind": "pto"}]


def test_a_rest_day_inside_a_sick_entry_reads_as_a_rest_day(tmp_path, conn, version):
    c = cell(DAY, "rest", time_off="sick")
    out = day_page(tmp_path, AVERY, c, day_body(c), """
run(async function () {
  return panelTitled("What Avery did, in order").querySelectorAll("div.placeholder")[0].textContent;
});
""")
    assert out == "Rest day. Nothing recorded."


def test_all_techs_one_day_rows_link(tmp_path, conn, version):
    avery = cell(DAY, tickets=6, replies=7, notes=2, calls=1, solved=2, signin="08:04", first="08:10", last="16:35",
                 gap_min=150, gap_start="11:00", flags=[flag("long_gap", 150)])
    blake = cell(DAY, "missed", flags=[flag("missed")])
    casey = cell(DAY, "rest")
    body = activity([DAY], [row(AVERY, "Avery Cole", [avery]), row(BLAKE, "Blake Moreno", [blake]),
                            row(CASEY, "Casey Nguyen", [casey])], preset="custom", label="Mon Oct 5")
    hash_, api_url = custom([DAY])
    out = snapshot(tmp_path, hash_, {api_url: body}, """
  var panel = panelTitled("Everyone");
  var table = panel.querySelectorAll("table")[0];
  var trs = bodyRows(table);
  var result = { head: head(), tiles: tiles(), titles: titles(), cls: table.className, heads: heads(table),
    rows: trs.map(function (tr) { return { cls: tr.className, cells: tr.children.map(function (td) { return td.textContent; }),
      href: links(tr)[0].href }; }) };
  (trs[1].listeners.click || []).forEach(function (f) {
    f({ target: trs[1], preventDefault: function () {} });
  });
  result.hash = location.hash;
  return result;
""")
    assert out["head"] == "Mon Oct 5 · 2 techs scheduled"
    assert out["titles"] == ["Everyone"] and out["cls"] == "atable one-line"
    assert out["tiles"] == [["Tickets touched", "6", "by 1 of 2 scheduled techs"], ["Replies", "7", "2 internal notes"],
                            ["Solved", "2", "1 call logged"], ["Flags", "2", "1 sick day"]]
    assert out["heads"] == ["Tech", "Shift", "Signed in", "First – last action", "Longest gap", "Tickets", "Replies",
                            "Notes", "Calls", "Solved", "Flags"]
    assert [r["cells"][:4] for r in out["rows"]] == [
        ["Avery Cole", "8 AM – 5 PM", "8:04 AM", "8:10 AM – 4:35 PM"],
        ["Blake Moreno", "8 AM – 5 PM", "-", "-"],
        ["Casey Nguyen", "Rest day", "-", "-"]]
    assert [r["cls"] for r in out["rows"]] == ["rowlink flagrow", "rowlink failrow", "rowlink"]
    assert [r["href"] for r in out["rows"]] == [day_hash(AVERY, DAY), day_hash(BLAKE, DAY), day_hash(CASEY, DAY)]
    assert out["hash"] == day_hash(BLAKE, DAY), "a click on the line opens that tech's day"


def test_time_off_list_scrolls_inside_its_panel():
    """Settings' Time off list scrolls past --list-max like the other long lists; its add row stays below it."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function timeOffPanel()"):source.index("function renderTimeOff()")]
    assert re.search(r'tableBox\(h\("table", \{ class: "atable time-off" \}[\s\S]*\{ list: true \}\)', body)


def test_the_tech_names_and_totals_stay_put_while_only_the_days_scroll():
    """The grid is three parts side by side: the names, the days (the only part that scrolls sideways) and Total
    and Flags, so no day ever passes under a pinned column and nothing is sticky (owner, 2026-10-21, T-126; it
    replaced T-113's sticky columns, which Eyes' layout scan saw covering the days scrolled under them)."""
    css = app_css()
    assert "sticky" not in "".join(body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css) if ".heat" in sel)
    top = top_level(css)
    assert values(top, ".heat.split", "display") == ["flex"]
    assert values(top, ".heat-names", "flex") == ["none"] and values(top, ".heat-ends", "flex") == ["none"]
    assert values(top, ".heat-days", "flex") == ["1 1 0"]
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function placeMonthTags("):source.index("function watchMonthTags(")]
    assert 'grid.querySelector(".heat-days")' in body, "month names follow the days part as it scrolls"
    assert "x - width / 2 < edge - box.left + MONTH_CLEAR_PX" in body, "a month name never covers the By day pills"


def test_every_part_takes_the_same_row_heights_whatever_is_in_a_row():
    """A line lines up across the three parts because one rule gives every part the same fixed rows: the head
    line from its number of text lines, every other line from the cell height (T-126). A name too long for its
    column ends in an ellipsis instead of growing its line."""
    css = top_level(app_css())
    for part in (".heat-names", ".heat-days", ".heat-ends"):
        assert values(css, part, "grid-template-rows") == ["var(--heat-head-h)"], part
        assert values(css, part, "grid-auto-rows") == ["var(--heat-row-h)"], part
    assert values(css, ".heat.split", "--heat-head-h") == [
        "calc(var(--heat-head-lines) * var(--fs-sm) * var(--lh-base) + var(--sp-2))"]
    assert values(css, ".heat.split", "--heat-row-h") == ["calc(var(--heat-cell-h) + 2 * var(--heat-cell-pad))"]
    assert values(css, ".heat.split", "--heat-head-lines") == ["2"]
    assert values(css, ".heat.split.dates", "--heat-head-lines") == ["1"]
    assert values(css, ".heat.split.weeks", "--heat-head-lines") == ["1"]
    assert values(css, ".heat-head > *", "font-size") == ["var(--fs-sm)"]
    assert values(css, ".heat-head > *", "white-space") == ["nowrap"]
    assert values(css, ".heat-names .who", "text-overflow") == ["ellipsis"]
    assert values(css, ".heat-ends .num", "white-space") == ["nowrap"]


def test_the_grid_reads_as_one_table_to_a_screen_reader(tmp_path, conn, version):
    """Each line's row in the names part is the table's row and owns the same line's cells in the days part and
    in Total and Flags (aria-owns); those two rows are only containers (role none)."""
    out = snapshot(tmp_path, "#/team", {url(): week_body()}, """
  var t = heat();
  var parts = t.children;
  return { role: t.getAttribute("role"), label: t.getAttribute("aria-label"),
    partRoles: parts.map(function (p) { return p.getAttribute("role"); }),
    lines: parts[0].children.map(function (row, i) {
      var owned = row.getAttribute("aria-owns").split(" ");
      return { role: row.getAttribute("role"),
        owned: owned.map(function (id) { var el = byId(id); return el ? [el.getAttribute("role"), parts.indexOf(el.parentNode)] : null; }),
        own: row.children.map(function (c) { return c.getAttribute("role"); }),
        cells: [].concat.apply([], owned.map(function (id) { return byId(id).children.map(function (c) { return c.getAttribute("role"); }); })) };
    }) };
""")
    assert out["role"] == "table" and out["label"] == "Tickets touched per day"
    assert out["partRoles"] == ["none", "none", "none"]
    head, *techs = out["lines"]
    assert len(techs) == 2
    for line in out["lines"]:
        assert line["role"] == "row" and line["owned"] == [["none", 1], ["none", 2]]
    assert head["own"] == ["columnheader"] and head["cells"] == ["columnheader"] * 9
    for line in techs:
        assert line["own"] == ["rowheader"] and line["cells"] == ["cell"] * 9


def test_pto_days_stand_out_from_rest_days_at_every_density():
    """PTO is violet (--unsure) with its own tint and edge, and keeps its PTO label in the denser grid (owner,
    2026-10-21); rest days stay plain."""
    css = top_level(app_css())
    assert values(css, ".hc.pto", "background") == ["var(--pto-tint)"]
    assert values(css, ".hc.pto", "border") == ["1px dashed var(--pto-edge)"]
    assert values(css, ".hc.pto", "color") == ["var(--unsure)"]
    assert values(css, ".heat.mid .hc.pto", "font-size") == ["var(--fs-xs)"]
    assert values(css, ".heat.mid .hc.rest", "font-size") == ["0"]
    for token in ("pto-tint", "pto-edge"):
        assert token in tokens(css), token
    assert not re.search(r"\.heat t[dh]\.wk", app_css()), "no Sunday gap that would narrow a day cell"


def test_the_hosted_copy_has_no_refresh_and_says_when_the_next_update_comes(tmp_path, conn, version):
    """The owner, 2026-10-22: the hosted copy syncs every hour, so Refresh gives way to a line saying when the data
    was updated and when the next update comes; while a sync runs it says so and the page waits for it."""
    responses = page_responses(api_for(tmp_path))
    responses["GET /api/settings"]["body"]["edition"] = "hosted"
    responses["GET /api/me"] = answer({"email": ALEX, "role": "admin", "edition": "hosted"})
    body = running_body(False)
    body["synced"]["next"] = "2026-10-17T17:00:00Z"
    responses["GET " + url()] = answer(body)
    out = run_page("#/team", responses, READ + OPTIONS + """
run(async function () {
  var line = all(screenEl, function (e) { return e.tagName === "SPAN" && e.classList.contains("message"); })[0];
  return { refresh: byId("ta-refresh") !== null, line: line.textContent };
});
""")
    assert out["refresh"] is False
    assert re.fullmatch(r"Updated .+ · next update (\w{3} )?\d{1,2}:\d{2}\s[AP]M", out["line"]), out
