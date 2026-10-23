"""The 1:1 prep screen (1:1 prep schedules design §5–§7, Task 6; Collected calls design): each tech's latest prep in
one table, counted when the page asks (nothing to make), with when the data was last synced, its pills and its "No 1:1
date set" rows, Copy row and Copy all, and a tech's detail below it: the heading, the past periods to pick from, and the
four lists behind the picked period's numbers. The address keeps the tech and the period. app.js runs in node on the
fake DOM (tests/fake_dom.js) through the stand-in page of test_web_test_view, with `/api/prep` and `/api/prep/tech`
answered from fixtures in the shapes oneonone.report returns (`latest_view`, `tech_view`)."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlencode

from css_rules import STATIC, app_css, media_block, rules, top_level, values
from test_compare import api_for
from test_queries import LEE, all_met, missing, version  # noqa: F401  (fixture)
from test_web_edition import signed_in_manager
from test_web_test_view import answer, page_responses, run_page
from work_assistant.oneonone.cycle import period_for
from work_assistant.oneonone.live import BEFORE_ACTIVITY, NO_AGING, UNSYNCED
from work_assistant.oneonone.report import COLUMNS, NO_ROW, UNREACHABLE, copy_row

AVERY, BLAKE, CASEY, DREW = "avery@example.com", "blake@example.com", "casey@example.com", "drew@example.com"
NAMES = {AVERY: "Avery Cole", BLAKE: "Blake Moreno", CASEY: "Casey Nguyen", DREW: "Drew Park"}
ZONE = "America/Chicago"
SEARCH_FAILED = "Zendesk could not be reached for /api/v2/search/count.json after 5 retries"
RATINGS_FAILED = "Zendesk could not be reached for /api/v2/satisfaction_ratings.json after 5 retries"
TALK_FAILED = "Zendesk could not be reached for /api/v2/channels/voice/stats after 5 retries"
RINGS = UNREACHABLE.format(n=3, rings="rings")
HISTORY_AGING = NO_AGING.format(day="Oct 20")   # an older period's aging: before recording began
HEADERS = ["Tech", "1:1 day", "Time frame", "Total calls", "% of calls", "Declined / missed", "% D/M",
           "Total call time", "Avg. call time", "Solved", "Satisfaction", "Aging", "Attendance", "Next 1:1", "Copy all"]
PAST_HEADERS = ["Date", "Data time frame", "Total calls", "% of calls", "Declined / missed", "% D/M",
                "Total call time", "Avg. call time", "Solved", "Satisfaction", "Aging", "Attendance"]
HINT = ("Each tech's prep covers the 14 days before their own 1:1, counted from the data as of the last update. % of "
        "calls is out of the department's answered inbound calls in that tech's two weeks. Copy row pastes into the "
        "tech's sheet tab.")
CLICK_NOTE = "Click a tech for their past periods and the calls, tickets and days behind each number."
PICK_NOTE = "Click a period to see what's behind its numbers"


def cells(**by_key) -> dict:
    """A row's cells in report.COLUMNS order: each key -> (text, value[, reason])."""
    out = {}
    for key in COLUMNS:
        text, value, *reason = by_key[key]
        out[key] = {"text": text, "value": value, "reason": reason[0] if reason else None}
    return out


AVERY_CELLS = cells(calls=("36", 36), share=("7.36%", 7.36), declined_missed=("1", 1, RINGS),
                    dm_share=("2.78%", 2.78), talk=("5:16:50", 19010), avg=("08:48", 528), solved=("43", 43),
                    satisfaction=(None, None), aging=("3", 3), attendance=("2", 2))
AVERY_HISTORY = cells(calls=("34", 34), share=("7.64%", 7.64), declined_missed=("2", 2), dm_share=("5.88%", 5.88),
                      talk=("4:01:00", 14460), avg=("07:05", 425), solved=("47", 47), satisfaction=("100%", 100),
                      aging=(None, None, HISTORY_AGING),
                      attendance=(None, None, BEFORE_ACTIVITY.format(day="Jul 21")))
BLAKE_CELLS = cells(calls=("19", 19), share=("3.89%", 3.89), declined_missed=("1", 1), dm_share=("5.26%", 5.26),
                    talk=("2:21:19", 8479), avg=("07:26", 446), solved=("39", 39), satisfaction=("100%", 100),
                    aging=(None, None, SEARCH_FAILED), attendance=("0", 0))
CASEY_HISTORY = cells(calls=("12", 12), share=("2.61%", 2.61), declined_missed=("0", 0), dm_share=("0.00%", 0.0),
                      talk=("1:30:00", 5400), avg=("07:30", 450), solved=("30", 30), satisfaction=(None, None),
                      aging=("1", 1), attendance=("0", 0))


def counted_row(email, day: date, row_cells, *, label, frame, today, next_day, next_label, soon, unreachable) -> dict:
    return {"email": email, "name": NAMES[email], "state": "counted", "day": day.isoformat(), "label": label,
            "frame": frame, "today": today, "next_day": next_day, "next_label": next_label, "soon": soon,
            "cells": row_cells, "copy": copy_row(period_for(day), row_cells), "unreachable": unreachable}


AVERY_ROW = counted_row(AVERY, date(2026, 10, 20), AVERY_CELLS, label="Tue Oct 20", frame="10/6 – 10/19", today=True,
                     next_day="2026-11-03", next_label="Tue Nov 3", soon=None, unreachable=3)
BLAKE_ROW = counted_row(BLAKE, date(2026, 10, 7), BLAKE_CELLS, label="Wed Oct 7", frame="9/23 – 10/6", today=False,
                     next_day="2026-10-21", next_label="Wed Oct 21", soon="Tomorrow", unreachable=0)
CASEY_ROW = counted_row(CASEY, date(2026, 10, 8), CASEY_HISTORY, label="Thu Oct 8", frame="9/24 – 10/7", today=False,
                        next_day="2026-10-22", next_label="Thu Oct 22", soon="In 2 days", unreachable=0)
DREW_ROW = {"email": DREW, "name": NAMES[DREW], "state": "no_schedule", "day": None, "label": None, "frame": None,
            "today": False, "next_day": None, "next_label": None, "soon": None, "cells": None, "copy": None,
            "unreachable": None}
ROWS = [AVERY_ROW, BLAKE_ROW, CASEY_ROW, DREW_ROW]
SYNCED = "2026-10-20T17:00:00Z"   # noon on Oct 20 in Chicago, the zone node runs in here (READ)
UPDATED = "Updated Oct 20, 12:00 PM"
LATEST_HEAD = "Each tech's latest 1:1 prep · 4 techs"


def latest(*, rows=None, synced=SYNCED) -> dict:
    """`/api/prep`'s answer (latest_view's shape)."""
    return {"synced": synced, "rows": ROWS if rows is None else rows}


AVERY_BEHIND = {
    "label": "1:1 10/20 (10/6 – 10/19)",
    "answered": [{"ticket_id": 483101, "day": "10/7", "talk": "5:12", "priority": "normal", "over": False},
                 {"ticket_id": 483102, "day": "10/8", "talk": "11:03", "priority": "high", "over": True},
                 {"ticket_id": None, "day": "10/9", "talk": "14:40", "priority": None, "over": True},
                 {"ticket_id": 483104, "day": "10/12", "talk": "12:10", "priority": "urgent", "over": False}],
    "over_count": 2, "limit": 10,
    "ratings": [{"ticket_id": 483300, "day": "10/9", "score": "good", "reason": None,
                 "comment": "Fixed it on the first call."},
                {"ticket_id": 483301, "day": "10/14", "score": "bad", "reason": "The issue took too long to resolve",
                 "comment": None}],
    "aging": [{"ticket_id": 481000, "age_days": 19, "status": "hold", "last_reply": "10/16", "reply_days_ago": 4},
              {"ticket_id": 481001, "age_days": 9, "status": "open", "last_reply": "10/20", "reply_days_ago": 0},
              {"ticket_id": 481002, "age_days": 1, "status": "new", "last_reply": None, "reply_days_ago": None}],
    "aging_reason": None,
    "days_out": [{"day": "10/13", "kind": "pto"}, {"day": "10/15", "kind": "sick"}],
}
HISTORY_BEHIND = {
    "label": "1:1 10/6 (9/22 – 10/5)",
    "answered": [{"ticket_id": 482001, "day": "9/23", "talk": "10:30", "priority": "low", "over": True},
                 {"ticket_id": 482002, "day": "9/30", "talk": "4:00", "priority": "normal", "over": False}],
    "over_count": 1, "limit": 10,
    "ratings": [], "aging": None, "aging_reason": HISTORY_AGING, "days_out": [],
}
AVERY_PAST = [{"day": "2026-10-20", "sheet_day": "10/20/2026", "frame": "10/6 - 10/19", "cells": AVERY_CELLS,
               "unreachable": 3},
              {"day": "2026-10-06", "sheet_day": "10/6/2026", "frame": "9/22 - 10/5", "cells": AVERY_HISTORY,
               "unreachable": 0}]


def avery_view(day: str = "2026-10-20", *, behind=None) -> dict:
    """`/api/prep/tech` for Avery (tech_view's shape), the period of `day` selected."""
    if behind is None:
        behind = AVERY_BEHIND if day == "2026-10-20" else HISTORY_BEHIND
    return {"email": AVERY, "name": NAMES[AVERY], "next_day": "2026-11-03", "next_label": "Tue Nov 3",
            "selected": day, "past": AVERY_PAST, "behind": behind}


def trend(key, label, better, unit, values, team, change) -> dict:
    """One of `/api/prep/trends`' measures (trends_view's shape)."""
    return {"key": key, "label": label, "better": better, "unit": unit, "values": values, "team": team,
            "latest": values[-1], "change": change}


TREND_DAYS = ["2026-09-08", "2026-09-22", "2026-10-06", "2026-10-20"]
AVERY_TRENDS = {"days": TREND_DAYS, "measures": [
    trend("calls", "Total calls", "higher", "count", [30, 32, 34, 36], [28.5, 30, 31.25, 33], 4.0),
    trend("share", "% of calls", "higher", "percent", [7.1, 7.0, 7.64, 7.36], [6.2, 6.5, 6.25, 6.4], 0.11),
    trend("declined_missed", "Declined / missed", "lower", "count", [0, 1, 1, 2], [1, 1.5, 1, 1.25], 1.33),
    trend("avg", "Avg. call time", None, "seconds", [528, 528, 527, 528], [480, 490, 470, 485], 0.33),
    trend("solved", "Solved tickets", "higher", "count", [40, 45, 47, 43], [38, 41.5, 40, 39], -1.0),
    trend("satisfaction", "Satisfaction", "higher", "percent", [100, None, 80, 75], [90, 95, None, 88], -15.0),
    trend("aging", "Aging tickets", "lower", "count", [2, 2, 3, None], [2.5, 3, 2, 2.75], None),
    trend("attendance", "Attendance", None, "count", [None, 0, 1, 2], [None, 0.5, 0.25, 1], 1.5),
]}
YEAR_DAYS = ["2026-08-11", "2026-08-25"] + TREND_DAYS
AVERY_TRENDS_26 = {"days": YEAR_DAYS, "measures": [
    {**m, "values": [m["values"][0]] * 2 + m["values"], "team": [m["team"][0]] * 2 + m["team"]}
    for m in AVERY_TRENDS["measures"]]}


def trends_url(email: str, n: int = 8) -> str:
    return "/api/prep/trends?" + urlencode({"email": email, "n": n})


def tech_url(email: str, day: str | None = None) -> str:
    return "/api/prep/tech?" + urlencode({"email": email, **({"day": day} if day else {})})


def address(email: str | None = None, day: str | None = None) -> str:
    query = urlencode({**({"tech": email} if email else {}), **({"day": day} if day else {})})
    return "#/oneonone" + ("?" + query if query else "")


READ = """
process.env.TZ = "America/Chicago";   // so "Updated …" reads the same on every machine
var copied = [];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: {
  writeText: function (text) { copied.push(text); return Promise.resolve(); } } } });
// Every timer of a second or more the page sets is held here, not run, so a test can see whether the page would read
// anything again later (it never polls: a prep is counted when it is asked for).
var timers = [];
var realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = function (f, ms) { if (ms >= 1000) { timers.push(f); return 0; } return realSetTimeout(f, ms); };
async function runTimers() {
  var held = timers;
  timers = [];
  held.forEach(function (f) { f(); });
  await settle();
}
function byId(id) { return document.getElementById(id); }
function text(el) { return el ? el.textContent : null; }
function heads() { return screenEl.querySelectorAll("h3.period-head").map(text); }
function headers(table) { return table.querySelectorAll("thead th").map(function (th) { return th.textContent; }); }
function isPill(c) { return c.classList && c.classList.contains("pill"); }
function cellOf(td) {
  var pill = td.childNodes.filter(isPill)[0], faint = td.querySelectorAll("span.muted")[0];
  return { text: td.childNodes.filter(function (c) { return !isPill(c); }).map(function (c) { return c.textContent; })
      .join("").trim(),
    title: td.getAttribute("title"), cls: td.className, colspan: td.getAttribute("colspan"),
    pill: pill ? [pill.textContent, pill.className] : null, faint: faint ? faint.textContent : null,
    links: links(td) };
}
function mainRows() {
  return byId("pp-latest").querySelectorAll("tbody tr").map(function (tr) {
    return { cls: tr.className, cells: tr.querySelectorAll("td").map(cellOf) };
  });
}
function buttonWith(label, el) {
  return (el || screenEl).querySelectorAll("button").filter(function (b) { return b.textContent === label; });
}
function nameButton(name) {
  return byId("pp-latest").querySelectorAll("tbody button").filter(function (b) { return b.textContent === name; })[0];
}
function tableRows(table) {
  return table.querySelectorAll("tbody tr").map(function (tr) {
    return tr.querySelectorAll("td").map(function (td) { return td.textContent; });
  });
}
function pastRows() { var t = byId("pp-past"); return t ? t.querySelectorAll("tbody tr") : []; }
function behindLabel() {
  return text(all(screenEl, function (e) { return e.tagName === "H4" && !e.closest("section"); })[0] || null);
}
function behindPanels() {
  var row = byId("pp-behind");
  return row ? row.children : [];
}
function behindPanel(prefix) {
  return behindPanels().filter(function (p) { return (titleOf(p) || "").indexOf(prefix) === 0; })[0] || null;
}
function placeholderIn(el) { return text(el.querySelectorAll(".placeholder")[0]); }
function toastText() { var t = document.querySelectorAll(".toast")[0]; return t ? { text: t.textContent, cls: t.className } : null; }
// The reads under `prefix`; the trends' own reads only when asked for by their prefix (trendGets), so the page's and the
// tech detail's reads are counted as they were before Trends.
function getRequests(prefix) {
  var trends = prefix.indexOf("/api/prep/trends") === 0;
  return requests.filter(function (r) {
    return r.method === "GET" && r.url.indexOf(prefix) === 0 && (trends || r.url.indexOf("/api/prep/trends") !== 0);
  }).map(function (r) { return r.url; });
}
function trendGets() { return getRequests("/api/prep/trends"); }
function posts() {
  return requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
}
function updated() { var el = byId("pp-updated"); return el ? [el.textContent, el.className, el.getAttribute("role")] : null; }
function loading() {
  return screenEl.querySelectorAll(".message").map(text).filter(function (t) { return /^Loading/.test(t); });
}
// The browser's hashchange played with every request waiting, so what is read next is what the page drew from the
// answers kept, without waiting for one (T-143); letGo() then lets the background reads answer.
async function routeHeld() {
  waitingRequests = [];
  (windowListeners.hashchange || []).forEach(function (f) { f(); });
  await settle();
}
async function letGo() {
  var waiting = waitingRequests;
  waitingRequests = null;
  waiting.forEach(function (request) { request.answer(); });
  await settle();
}
// A click that changes the address once what it opens has been read: the page sets location.hash, and the browser's
// hashchange is played here, as go() does.
async function pick(el) {
  fire(el, "click");
  await settle();
  (windowListeners.hashchange || []).forEach(function (f) { f(); });
  await settle();
}
"""


def settings_with_zone(responses: dict) -> None:
    body = responses["GET /api/settings"]["body"]
    body.update(zendesk_subdomain="example", timezone_effective=ZONE)


def prep_page(tmp_path, start_hash: str, bodies: dict[str, dict], scenario: str, *, schedule: dict | None = None,
              manager: bool = False, before: str = ""):
    """The page at `start_hash` with `bodies` answered (a path, or "METHOD path" for a full answer). Every tech's
    default Trends (Last 8) answer AVERY_TRENDS unless `bodies` says otherwise. `schedule` is the oneonone_schedule
    setting the page reads. `manager`: the hosted page, signed in as a manager (test_web_edition). `before` is JS run
    before the page draws (a stand-in the page reads as it draws)."""
    responses = page_responses(api_for(tmp_path))
    settings_with_zone(responses)
    if manager:
        signed_in_manager(responses)
    if schedule is not None:
        responses["GET /api/settings"]["body"]["oneonone_schedule"] = schedule
    for email in NAMES:
        responses["GET " + trends_url(email)] = answer(AVERY_TRENDS)
    for key, body in bodies.items():
        if " " in key:
            responses[key] = body
        else:
            responses["GET " + key] = answer(body)
    return run_page(start_hash, responses, READ + before + "run(async function () {\n" + scenario + "\n});")


# -- the side panel ---------------------------------------------------------------------------------------


def test_side_panel_has_one_on_one_prep_section(tmp_path, conn, version):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nav = re.search(r'<nav class="sidenav" aria-label="Sections">(.*?)</nav>', html, re.S).group(1)
    items = [(" ".join(attrs.split()), text.strip()) for _, attrs, text in re.findall(r"<(a|span)\b([^>]*)>([^<]*)", nav)]
    assert items[7:11] == [
        ('class="sub" data-tab="trends" href="#/team/trends" hidden', "Trends"),
        ('class="sub" data-tab="oneonone" href="#/oneonone" hidden', "1:1 Prep"),
        ('class="gap"', ""),
        ('data-tab="settings" href="#/settings"', "Settings"),
    ]
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest()}, """
  var link = document.querySelectorAll(".sidenav a").filter(function (a) { return a.textContent === "1:1 Prep"; })[0];
  var team = document.querySelectorAll(".sidenav a").filter(function (a) { return a.textContent === "Team"; })[0];
  var crumb = titleEl.querySelectorAll(".crumb")[0];
  var out = { lit: settingsTab(), cls: link.className, current: link.getAttribute("aria-current"),
    teamOpen: team.classList.contains("open"), title: titleEl.textContent, crumb: crumb ? crumb.textContent : null,
    docTitle: document.title, gets: getRequests("/api/prep") };
  await go("#/team/trends");
  out.after = { cls: link.className, lit: settingsTab() };
  return out;
""")
    assert out["lit"] == ["oneonone"] and set(out["cls"].split()) == {"sub", "on"}
    assert out["current"] == "page" and out["teamOpen"]
    assert (out["title"], out["crumb"], out["docTitle"]) == (
        "Team › 1:1 Prep", "Team ›", "Team › 1:1 Prep · Work Assistant")
    assert out["gets"] == ["/api/prep"]
    assert out["after"] == {"cls": "sub", "lit": ["trends"]}


# -- the main table ---------------------------------------------------------------------------------------


def test_latest_table_columns_pills_and_states(tmp_path, conn, version):
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest()}, """
  var table = byId("pp-latest");
  return { heads: heads(), headers: headers(table), rows: mainRows(),
    inPanel: !!table.closest("section.panel"), inScroll: !!table.closest("div.scroll"),
    hint: text(table.closest("section.panel").querySelectorAll("p.note")[0]),
    notes: screenEl.querySelectorAll("p.note").map(text), filters: screenEl.querySelectorAll(".filters").length,
    selects: screenEl.querySelectorAll("select").length,
    pressed: ["Avery Cole", "Blake Moreno", "Casey Nguyen", "Drew Park"].map(function (n) {
      var b = nameButton(n); return b ? [b.className, b.getAttribute("aria-pressed")] : null; }),
    copyButtons: buttonWith("Copy row").length };
""")
    assert out["heads"] == [LATEST_HEAD]
    assert out["headers"] == HEADERS
    assert out["inPanel"] and out["inScroll"]
    assert out["hint"] == HINT and out["notes"][-1] == CLICK_NOTE
    assert out["filters"] == 0 and out["selects"] == 0, "no shared 1:1 day dropdown"
    assert out["pressed"] == [["rank-name", "false"]] * 4
    avery, blake, casey, drew = out["rows"]

    texts = [c["text"] for c in avery["cells"]]
    assert texts == ["Avery Cole", "Tue Oct 20", "10/6 – 10/19", "36", "7.36%", "1", "2.78%", "5:16:50", "08:48",
                     "43", "blank", "3", "2", "Tue Nov 3", "Copy row"]
    assert avery["cells"][1]["pill"] == ["Today", "pill cell-pill today"]
    assert avery["cells"][13]["pill"] is None
    assert avery["cells"][2]["cls"] == "nowrap muted"
    assert avery["cells"][5]["title"] == RINGS, "the unreachable rings left out, on the Declined / missed number"
    assert (avery["cells"][10]["faint"], avery["cells"][10]["title"]) == ("blank", "No ratings this period")
    assert all(c["cls"] == "num nowrap fig" for c in avery["cells"][3:13]), "each a figure when shown as a card"

    texts = [c["text"] for c in blake["cells"]]
    assert texts == ["Blake Moreno", "Wed Oct 7", "9/23 – 10/6", "19", "3.89%", "1", "5.26%", "2:21:19", "07:26",
                     "39", "100%", "-", "0", "Wed Oct 21", "Copy row"]
    assert blake["cells"][1]["pill"] is None
    assert blake["cells"][13]["pill"] == ["Tomorrow", "pill cell-pill soon"]
    assert blake["cells"][11]["title"] == SEARCH_FAILED
    assert blake["cells"][5]["title"] is None

    texts = [c["text"] for c in casey["cells"]]
    assert texts == ["Casey Nguyen", "Thu Oct 8", "9/24 – 10/7", "12", "2.61%", "0", "0.00%", "1:30:00", "07:30",
                     "30", "blank", "1", "0", "Thu Oct 22", "Copy row"]
    assert casey["cells"][13]["pill"] == ["In 2 days", "pill cell-pill soon"]

    assert [(c["text"], c["colspan"]) for c in drew["cells"]] == [
        ("Drew Park", None), ("No 1:1 date set", "13"), ("", None)]
    assert drew["cells"][1]["links"] == [{"text": "No 1:1 date set", "href": "#/settings"}]
    assert out["copyButtons"] == 3, "every counted prep has a row to copy; no 1:1 date set has none"


def test_the_page_says_when_the_data_was_last_synced(tmp_path, conn, version):
    """Collected calls design: a prep is as current as the latest sync, so the page says when that was, beside its
    heading, as Activity does ("Updated …", with the day when it wasn't today), whether or not a tech is open."""
    script = "return { heads: heads(), updated: updated(), beside: !!byId('pp-updated').closest('.head-line') };"
    shut = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest()}, script)
    opened = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, script)
    assert shut == {"heads": [LATEST_HEAD], "updated": [UPDATED, "message", "status"], "beside": True}
    assert opened["updated"] == shut["updated"] and opened["heads"][0] == LATEST_HEAD


def test_before_any_sync_and_one_tech(tmp_path, conn, version):
    """Before any sync the page says it hasn't been updated, as Activity does; one tech reads "1 tech"."""
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest(rows=[DREW_ROW], synced=None)},
                    "return { heads: heads(), updated: updated() };")
    assert out == {"heads": ["Each tech's latest 1:1 prep · 1 tech"],
                   "updated": ["Not updated yet", "message", "status"]}


def test_copy_row_uses_each_rows_own_day(tmp_path, conn, version):
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest()}, """
  var buttons = buttonWith("Copy row");
  fire(buttons[0], "click");
  await settle();
  var out = { first: { copied: copied.slice(), toast: toastText() }, cls: buttons[0].className };
  fire(buttons[1], "click");
  await settle();
  out.second = { copied: copied.slice(), toast: toastText(), toasts: document.querySelectorAll(".toast").length };
  var all = byId("pp-latest").querySelectorAll("thead button")[0];
  fire(all, "click");
  await settle();
  out.all = { label: all.textContent, cls: all.className, copied: copied[2], toast: toastText() };
  return out;
""")
    assert out["first"]["copied"] == [AVERY_ROW["copy"]]
    assert out["first"]["copied"][0] == "10/20/2026\t10/6 - 10/19\t36\t7.36%\t1\t2.78%\t5:16:50\t08:48\t43\t\t3\t2"
    assert out["first"]["toast"] == {"text": "Copied Avery Cole's row. Paste it into their tab.", "cls": "toast show"}
    assert out["cls"] == "small"
    assert out["second"]["copied"][1] == BLAKE_ROW["copy"]
    assert out["second"]["copied"][1].startswith("10/7/2026\t9/23 - 10/6\t19\t"), "Blake's own 1:1 day and frame"
    assert out["second"]["toast"]["text"] == "Copied Blake Moreno's row. Paste it into their tab."
    assert out["second"]["toasts"] == 1, "one toast, reused"
    assert (out["all"]["label"], out["all"]["cls"]) == ("Copy all", "small")
    assert out["all"]["copied"] == "\n".join(r["copy"] for r in (AVERY_ROW, BLAKE_ROW, CASEY_ROW)), \
        "the techs with a prep, in order"
    assert out["all"]["toast"] == {"text": "Copied 3 rows, one per tech", "cls": "toast show"}


# -- the tech detail ----------------------------------------------------------------------------------------


def test_tech_detail_heading_has_nothing_to_make(tmp_path, conn, version):
    """Collected calls design: a prep is counted whenever it is asked for, so the detail's heading has no Make prep
    again, the page sends nothing and sets no timer to read it again."""
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, """
  var line = screenEl.querySelectorAll(".head-line")[1];
  return { gets: getRequests("/api/prep"), heads: heads(), make: !!byId("pp-make"), state: !!byId("pp-state"),
    detailLine: line.children.map(function (c) { return c.tagName; }),
    buttons: screenEl.querySelectorAll("button").map(text).filter(function (t) { return /prep/i.test(t); }),
    selected: mainRows().map(function (r) { return r.cls; }), pressed: nameButton("Avery Cole").getAttribute("aria-pressed"),
    loading: loading(), posts: posts(), banner: screenEl.querySelectorAll(".banner").length };
""")
    assert out["gets"] == ["/api/prep", tech_url(AVERY)]
    assert out["heads"] == [LATEST_HEAD, "Avery Cole · past periods · next 1:1 Tue Nov 3"]
    assert out["make"] is False and out["state"] is False and out["buttons"] == []
    assert out["detailLine"] == ["H3"], "the detail's heading stands alone"
    assert out["selected"] == ["rowlink sel", "rowlink", "rowlink", "rowlink"] and out["pressed"] == "true"
    assert out["loading"] == [] and out["posts"] == [] and out["banner"] == 0


def test_no_polling_after_load(tmp_path, conn, version):
    """Nothing is being made, so nothing is waited for: every timer the page set, run, reads nothing more."""
    for start, bodies in (("#/oneonone", {"/api/prep": latest()}),
                          (address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()})):
        out = prep_page(tmp_path, start, bodies, """
  var before = getRequests("/api/prep");
  await runTimers();
  await runTimers();
  return { before: before, after: getRequests("/api/prep"), heads: heads() };
""")
        assert out["after"] == out["before"], start
        assert out["heads"][0] == LATEST_HEAD


def test_clicking_a_name_opens_and_closes_the_tech(tmp_path, conn, version):
    """The tech's detail is read before the address changes, so the screen is drawn whole: no loading line and no
    second read of the table."""
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest(), tech_url(AVERY): avery_view()}, """
  fire(nameButton("Avery Cole"), "click");
  await settle();
  var out = { hash: location.hash };
  await routeHeld();
  out.drawn = { loading: loading(), heads: heads() };
  await letGo();
  await pick(nameButton("Avery Cole"));
  out.closed = { hash: location.hash, heads: heads(), past: !!byId("pp-past"), note: screenEl.querySelectorAll("p.note").map(text).pop() };
  out.gets = getRequests("/api/prep");
  return out;
""")
    assert out["hash"] == address(AVERY)
    assert out["drawn"] == {"loading": [], "heads": [
        LATEST_HEAD,
        "Avery Cole · past periods · next 1:1 Tue Nov 3"]}
    assert out["closed"] == {"hash": "#/oneonone", "heads": [
        LATEST_HEAD], "past": False,
        "note": CLICK_NOTE}
    assert out["gets"] == ["/api/prep", tech_url(AVERY), "/api/prep", tech_url(AVERY), "/api/prep"], \
        "each drawing from the kept answers reads them again in the background"


# What the screen shows while it waits (T-168): the shared loading look (`.stale` on the screen, the loading bar), the
# address, and the headings drawn.
LOOK_JS = """
function look() {
  return { stale: screenEl.classList.contains("stale"), bar: !loadingBarEl.hidden, hash: location.hash, heads: heads() };
}
async function hashchange() {
  (windowListeners.hashchange || []).forEach(function (f) { f(); });
  await settle();
}
function waitingUrls() { return waitingRequests.map(function (r) { return r.url; }); }
"""
AVERY_HEAD = "Avery Cole · past periods · next 1:1 Tue Nov 3"


def blake_view() -> dict:
    """`/api/prep/tech` for Blake: Avery's periods under Blake's name."""
    return {**avery_view(), "email": BLAKE, "name": NAMES[BLAKE]}


def test_clicking_a_tech_shows_the_loading_look_until_their_detail_lands(tmp_path, conn, version):
    """Clicking a tech, their row or their name, shows the app's shared loading look at once: the table stays on show,
    faded under the loading bar, while their detail and Trends are read; it goes when they land and the detail is
    drawn (T-168)."""
    for target in ("row", "name"):
        out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest(), tech_url(AVERY): avery_view()}, LOOK_JS + """
  waitingRequests = [];
  fire(%s, "click");
  await settle();
  var out = { waiting: look(), reads: waitingUrls() };
  await letGo();
  out.landed = look();
  await hashchange();
  out.drawn = look();
  return out;
""" % ('byId("pp-latest").querySelectorAll("tbody tr")[0]' if target == "row" else 'nameButton("Avery Cole")'))
        assert out["waiting"] == {"stale": True, "bar": True, "hash": "#/oneonone", "heads": [LATEST_HEAD]}, target
        assert out["reads"] == [tech_url(AVERY), trends_url(AVERY)], target
        assert out["landed"]["hash"] == address(AVERY), "the address changes once both have landed"
        assert out["drawn"] == {"stale": False, "bar": False, "hash": address(AVERY), "heads": [LATEST_HEAD, AVERY_HEAD]}


def test_the_page_stays_usable_while_a_tech_loads(tmp_path, conn, version):
    """Another tech clicked while the first one's detail is read is the one opened, whichever answer lands last."""
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest(), tech_url(AVERY): avery_view(),
                                             tech_url(BLAKE): blake_view()}, LOOK_JS + """
  waitingRequests = [];
  fire(nameButton("Avery Cole"), "click");
  await settle();
  fire(nameButton("Blake Moreno"), "click");
  await settle();
  var out = { waiting: look() };
  var held = waitingRequests;
  waitingRequests = null;
  held.filter(function (r) { return r.url.indexOf("blake") >= 0; }).forEach(function (r) { r.answer(); });
  await settle();
  out.blake = location.hash;
  held.filter(function (r) { return r.url.indexOf("avery") >= 0; }).forEach(function (r) { r.answer(); });
  await settle();
  out.after = location.hash;
  await hashchange();
  out.drawn = look();
  return out;
""")
    assert out["waiting"] == {"stale": True, "bar": True, "hash": "#/oneonone", "heads": [LATEST_HEAD]}
    assert out["blake"] == out["after"] == address(BLAKE), "Avery's late answer opens nothing"
    assert out["drawn"] == {"stale": False, "bar": False, "hash": address(BLAKE),
                            "heads": [LATEST_HEAD, "Blake Moreno · past periods · next 1:1 Tue Nov 3"]}


def test_changing_period_or_range_shows_the_loading_look(tmp_path, conn, version):
    """Picking another period in a tech's detail, or another Compare range, shows the loading look while it is read
    and drops it when it lands; a range changed and changed back before it lands leaves the screen as it is, not
    faded (T-168)."""
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(), tech_url(AVERY, "2026-10-06"): avery_view("2026-10-06"),
        trends_url(AVERY, 26): AVERY_TRENDS_26}, LOOK_JS + TRENDS_JS + """
  waitingRequests = [];
  fire(pastRows()[1], "click");
  await settle();
  var out = { period: look(), periodReads: waitingUrls() };
  await letGo();
  await hashchange();
  out.periodDrawn = look();
  waitingRequests = [];
  compare().value = "26";
  fire(compare(), "change");
  await settle();
  out.range = look();
  compare().value = "8";
  fire(compare(), "change");
  await settle();
  out.back = look();
  await letGo();
  out.landed = look();
  return out;
""")
    assert out["period"] == {"stale": True, "bar": True, "hash": address(AVERY), "heads": [LATEST_HEAD, AVERY_HEAD]}
    assert out["periodReads"] == [tech_url(AVERY, "2026-10-06")]
    assert out["periodDrawn"] == {"stale": False, "bar": False, "hash": address(AVERY, "2026-10-06"),
                                  "heads": [LATEST_HEAD, AVERY_HEAD]}
    assert out["range"]["stale"] is True and out["range"]["bar"] is True
    assert out["back"] == out["landed"] == out["periodDrawn"], "back to the range shown: not faded, nothing opened"


def test_past_period_click_switches_behind(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(),
        tech_url(AVERY, "2026-10-06"): avery_view("2026-10-06"), tech_url(AVERY, "2026-10-20"): avery_view()}, """
  var table = byId("pp-past");
  var panel = table.closest("section.panel");
  var out = { sub: text(panel.querySelectorAll(".sub")[0]), title: titleOf(panel), headers: headers(table),
    rows: tableRows(table), cls: pastRows().map(function (tr) { return tr.className; }),
    pressed: pastRows().map(function (tr) { return tr.querySelectorAll("button.rank-name")[0].getAttribute("aria-pressed"); }),
    titles: pastRows().map(function (tr) { return tr.querySelectorAll("td")[4].getAttribute("title"); }),
    attendance: pastRows()[1].querySelectorAll("td")[11].getAttribute("title"),
    behind: behindLabel(), answered: titleOf(behindPanel("Answered")) };
  fire(pastRows()[1], "click");
  await settle();
  out.hash = location.hash;
  await routeHeld();
  out.drawn = { loading: loading(), behind: behindLabel(), cls: pastRows().map(function (tr) { return tr.className; }) };
  await letGo();
  out.after = { answered: titleOf(behindPanel("Answered")), aging: placeholderIn(behindPanel("Aging")) };
  await pick(pastRows()[0].querySelectorAll("button.rank-name")[0]);
  out.back = { hash: location.hash, behind: behindLabel() };
  out.gets = getRequests("/api/prep");
  return out;
""")
    assert out["sub"] == PICK_NOTE and out["title"] is None
    assert out["headers"] == PAST_HEADERS
    assert out["rows"] == [
        ["10/20/2026", "10/6 - 10/19", "36", "7.36%", "1", "2.78%", "5:16:50", "08:48", "43", "blank", "3", "2"],
        ["10/6/2026", "9/22 - 10/5", "34", "7.64%", "2", "5.88%", "4:01:00", "07:05", "47", "100%", "-", "-"],
    ]
    assert out["cls"] == ["rowlink sel", "rowlink"] and out["pressed"] == ["true", "false"]
    assert out["titles"] == [RINGS, None]
    assert out["attendance"] == "Team activity starts Jul 21"
    assert out["behind"] == "Behind the numbers · 1:1 10/20 (10/6 – 10/19)"
    assert out["answered"] == "Answered calls · 4"
    assert out["hash"] == address(AVERY, "2026-10-06")
    assert out["drawn"] == {"loading": [], "behind": "Behind the numbers · 1:1 10/6 (9/22 – 10/5)",
                            "cls": ["rowlink", "rowlink sel"]}, "drawn at once from the period already read"
    assert out["after"] == {"answered": "Answered calls · 2", "aging": HISTORY_AGING}
    assert out["back"] == {"hash": address(AVERY, "2026-10-20"),
                           "behind": "Behind the numbers · 1:1 10/20 (10/6 – 10/19)"}
    assert out["gets"] == ["/api/prep", tech_url(AVERY), tech_url(AVERY, "2026-10-06"), "/api/prep",
                           tech_url(AVERY, "2026-10-06"), tech_url(AVERY, "2026-10-20"), "/api/prep",
                           tech_url(AVERY, "2026-10-20")], "each period is read once, then again in the background"


def test_tech_with_no_date_and_no_preps_links_to_settings(tmp_path, conn, version):
    """Spec §3: a tech with no schedule entry has "No 1:1 date set", with a link to Settings. Opening one with no stored
    prep says that too, not that there's no prep for the period."""
    out = prep_page(tmp_path, address(DREW), {
        "/api/prep": latest(), "GET " + tech_url(DREW): answer({"detail": NO_ROW}, 400)}, """
  var box = screenEl.querySelectorAll(".placeholder").pop();
  return { head: heads()[1], placeholder: text(box), links: links(box), role: box.getAttribute("role"),
    errors: screenEl.querySelectorAll(".error").length, past: !!byId("pp-past"), trends: !!byId("pp-trends") };
""")
    assert out == {"head": "Drew Park · past periods", "placeholder": "No 1:1 date set",
                   "links": [{"text": "No 1:1 date set", "href": "#/settings"}], "role": "status", "errors": 0,
                   "past": False, "trends": False}


def test_a_manager_reads_no_1_1_date_set_as_plain_text(tmp_path, conn, version):
    """Managers design: a manager sets no 1:1 dates (each team lead does, and the server refuses them), so a tech with
    none reads "No 1:1 date set" as plain text, in the table and in the tech's detail, not the link to Settings a lead
    gets."""
    out = prep_page(tmp_path, address(DREW), {
        "/api/prep": latest(), "GET " + tech_url(DREW): answer({"detail": NO_ROW}, 400)}, """
  var drew = mainRows()[3];
  var box = screenEl.querySelectorAll(".placeholder").pop();
  return { cell: [drew.cells[1].text, drew.cells[1].colspan, drew.cells[1].links], placeholder: text(box),
    links: links(box), role: box.getAttribute("role"), reads: requests.map(function (r) { return r.url; }) };
""", manager=True)
    assert "/api/me" in out.pop("reads"), "signed in as the manager"
    assert out == {"cell": ["No 1:1 date set", "13", []], "placeholder": "No 1:1 date set", "links": [],
                   "role": "status"}


def test_detail_that_cannot_be_read_says_why(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY, "2026-09-01"), {
        "/api/prep": latest(), "GET " + tech_url(AVERY, "2026-09-01"): answer({"detail": NO_ROW}, 400)}, """
  return { heads: heads(), error: text(screenEl.querySelectorAll(".message.error")[0]), past: !!byId("pp-past") };
""")
    assert out == {"heads": [LATEST_HEAD, "Avery Cole · past periods · next 1:1 Tue Nov 3"], "error": NO_ROW,
                   "past": False}


# -- behind the numbers -------------------------------------------------------------------------------------


def test_answered_calls_highlight_and_count_line(tmp_path, conn, version):
    twelve = {**HISTORY_BEHIND, "limit": 12}
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(),
        tech_url(AVERY, "2026-10-06"): avery_view("2026-10-06", behind=twelve),
        tech_url(AVERY, "2026-10-20"): avery_view(behind={**AVERY_BEHIND, "over_count": 0,
                                                          "answered": AVERY_BEHIND["answered"][:1]})}, """
  function read() {
    var p = behindPanel("Answered");
    var table = p.querySelectorAll("table")[0];
    var key = p.querySelectorAll(".flag-key")[0];
    return { title: titleOf(p), key: key ? [key.textContent, key.className] : null,
      table: table ? [table.className, headers(table), tableRows(table),
        table.querySelectorAll("tbody tr").map(function (tr) { return [tr.className, tr.getAttribute("title")]; }),
        table.querySelectorAll("th").map(function (th) { return th.className; }),
        table.querySelectorAll("tbody tr")[0].querySelectorAll("td").map(function (td) { return td.className; }),
        !!table.closest("div.list-scroll"), links(table).map(function (l) { return l.href; })] : null };
  }
  var out = { two: read() };
  await pick(pastRows()[1]);
  out.one = read();
  await pick(pastRows()[0]);
  out.none = read();
  return out;
""")
    two = out["two"]
    assert two["title"] == "Answered calls · 4"
    assert two["key"] == ["2 non-urgent calls over 10 min, highlighted", "flag-key"]
    cls, heads_, rows, trs, th_cls, td_cls, scrolls, hrefs = two["table"]
    assert cls == "atable tight" and heads_ == ["Day", "Ticket", "Talk", "Priority"]
    assert rows == [["10/7", "#483101", "5:12", "Normal"], ["10/8", "#483102", "11:03", "High"],
                    ["10/9", "-", "14:40", "-"], ["10/12", "#483104", "12:10", "Urgent"]]
    assert trs == [["", None], ["flagrow over-limit", "Non-urgent call over 10 minutes"],
                   ["flagrow over-limit", "Non-urgent call over 10 minutes"], ["", None]]
    assert th_cls == ["", "", "num", ""] and td_cls == ["", "", "num fig", ""], "Talk is right-aligned (a card's figure)"
    assert scrolls
    assert hrefs == ["https://example.zendesk.com/agent/tickets/483101",
                     "https://example.zendesk.com/agent/tickets/483102",
                     "https://example.zendesk.com/agent/tickets/483104"]
    one = out["one"]
    assert one["key"] == ["1 non-urgent call over 12 min, highlighted", "flag-key"]
    assert one["table"][3] == [["flagrow over-limit", "Non-urgent call over 12 minutes"], ["", None]], "the rule as it is now"
    assert out["none"]["key"] is None and out["none"]["title"] == "Answered calls · 1"


def test_ratings_panel_reason_comment(tmp_path, conn, version):
    failed = {**AVERY_HISTORY, "satisfaction": {"text": None, "value": None, "reason": RATINGS_FAILED}}
    view = {**avery_view("2026-10-06"), "past": [AVERY_PAST[0], {**AVERY_PAST[1], "cells": failed}]}
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(), tech_url(AVERY, "2026-10-06"): view}, """
  function read() {
    var p = behindPanel("Satisfaction");
    return { title: titleOf(p), scrolls: p.querySelectorAll("div.list-scroll").length,
      placeholder: placeholderIn(p),
      items: p.querySelectorAll(".rating-item").map(function (item) {
        var chip = item.querySelectorAll(".chip")[0], say = item.querySelectorAll(".rating-say")[0];
        return { day: text(item.querySelectorAll(".rating-line .rating-day")[0]), links: links(item),
          chip: [chip.textContent, chip.className],
          notes: item.querySelectorAll(".rating-note").map(text), say: say ? say.textContent : null };
      }) };
  }
  var out = { two: read() };
  await pick(pastRows()[1]);
  out.failed = read();
  RESPONSES["GET /api/prep/tech?email=avery%%40example.com&day=2026-10-06"] = { status: 200, body: %s };
  await go("#/settings");
  await go("#/oneonone?tech=avery%%40example.com&day=2026-10-06");
  out.empty = read();
  return out;
""" % json.dumps(avery_view("2026-10-06")))
    two = out["two"]
    assert two["title"] == "Satisfaction ratings · 2" and two["scrolls"] == 1 and two["placeholder"] is None
    assert two["items"] == [
        {"day": "10/9", "links": [{"text": "#483300", "href": "https://example.zendesk.com/agent/tickets/483300"}],
         "chip": ["▲ Good", "chip good"], "notes": [], "say": "“Fixed it on the first call.”"},
        {"day": "10/14", "links": [{"text": "#483301", "href": "https://example.zendesk.com/agent/tickets/483301"}],
         "chip": ["▼ Bad", "chip bad"], "notes": ["Reason: The issue took too long to resolve", "No comment"],
         "say": None},
    ]
    assert out["failed"] == {"title": "Satisfaction ratings", "scrolls": 0, "placeholder": RATINGS_FAILED, "items": []}
    assert out["empty"] == {"title": "Satisfaction ratings · 0", "scrolls": 0, "placeholder": "No ratings this period",
                            "items": []}


def test_aging_last_reply_and_history_note(tmp_path, conn, version):
    failed = {**AVERY_BEHIND, "aging": None, "aging_reason": SEARCH_FAILED}
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(),
        tech_url(AVERY, "2026-10-06"): avery_view("2026-10-06"),
        tech_url(AVERY, "2026-10-20"): avery_view(behind=failed)}, """
  function read() {
    var p = behindPanel("Aging");
    var table = p.querySelectorAll("table")[0];
    return { title: titleOf(p), placeholder: placeholderIn(p),
      table: table ? [headers(table), tableRows(table),
        table.querySelectorAll("tbody tr").map(function (tr) { return tr.querySelectorAll("td")[3].className; }),
        !!table.closest("div.list-scroll"), links(table).map(function (l) { return l.text; })] : null };
  }
  var out = { list: read() };
  await pick(pastRows()[1]);
  out.history = read();
  await pick(pastRows()[0]);
  out.failed = read();
  return out;
""")
    assert out["list"]["title"] == "Aging tickets · 3" and out["list"]["placeholder"] is None
    heads_, rows, reply_cls, scrolls, links_ = out["list"]["table"]
    assert heads_ == ["Ticket", "Age", "Status", "Last reply"]
    assert rows == [["#481000", "19 days", "On-hold", "10/16 · 4d ago"], ["#481001", "9 days", "Open", "10/20 · today"],
                    ["#481002", "1 day", "New", "Never"]]
    assert reply_cls == ["nowrap", "nowrap", "muted"] and scrolls and links_ == ["#481000", "#481001", "#481002"]
    assert out["history"] == {"title": "Aging tickets", "placeholder": HISTORY_AGING, "table": None}
    assert out["failed"] == {"title": "Aging tickets", "placeholder": SEARCH_FAILED, "table": None}


def test_no_declined_missed_panel(tmp_path, conn, version):
    """Four panels side by side: Answered calls, Satisfaction ratings, Aging tickets and Days out. Phase 8's
    Declined / missed panel is gone; its count stays in the tables and Copy row."""
    unsynced = {**AVERY_HISTORY, "attendance": {"text": None, "value": None, "reason": UNSYNCED}}
    view = {**avery_view("2026-10-06"), "past": [AVERY_PAST[0], {**AVERY_PAST[1], "cells": unsynced}]}
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(), tech_url(AVERY, "2026-10-06"): view}, """
  var row = byId("pp-behind");
  var days = behindPanel("Days out");
  var out = { cls: row.className, titles: behindPanels().map(titleOf), inPanel: !!row.closest("section"),
    days: tableRows(days.querySelectorAll("table")[0]), daysHeads: headers(days.querySelectorAll("table")[0]),
    declined: screenEl.findAll(function (e) {
      return e.tagName === "H4" && /Declined/.test(e.textContent) && !e.closest(".trend-card");   // Trends graphs it
    }).length };
  await pick(pastRows()[1]);
  out.history = { titles: behindPanels().map(titleOf), days: placeholderIn(behindPanel("Days out")) };
  return out;
""")
    assert out["cls"] == "panel-row cols-4 last-narrow stretch" and not out["inPanel"], "the four panels share one height"
    assert out["titles"] == ["Answered calls · 4", "Satisfaction ratings · 2", "Aging tickets · 3", "Days out · 2"]
    assert out["daysHeads"] == ["Day", "From Team activity"]
    assert out["days"] == [["10/13", "PTO"], ["10/15", "Sick day"]]
    assert out["declined"] == 0
    assert out["history"] == {"titles": ["Answered calls · 2", "Satisfaction ratings · 0", "Aging tickets",
                                         "Days out"], "days": UNSYNCED}


def test_days_out_empty(tmp_path, conn, version):
    empty = {**AVERY_BEHIND, "days_out": []}
    out = prep_page(tmp_path, address(AVERY), {
        "/api/prep": latest(), tech_url(AVERY): avery_view(behind=empty)}, """
  return { days: [titleOf(behindPanel("Days out")), placeholderIn(behindPanel("Days out"))] };
""")
    assert out["days"] == ["Days out · 0", "No PTO or sick days"]


# -- trends -------------------------------------------------------------------------------------------------

TRENDS_JS = """
function trendsBox() { return byId("pp-trends"); }
function compare() { return byId("pp-compare"); }
function changeText() { var t = trendsBox().querySelectorAll(".trend-head .trend-note")[0]; return text(t); }
function nodeOf(c) { return c.tagName ? [c.tagName, c.className || ""] : c.textContent; }
async function choose(value) {
  compare().value = value;
  fire(compare(), "change");
  await settle();
  (windowListeners.hashchange || []).forEach(function (f) { f(); });
  await settle();
}
"""
SPANS_2 = ["Last 4 1:1s (2 months)", "Last 8 1:1s (4 months)", "Last 13 1:1s (6 months)", "Last 26 1:1s (1 year)"]


def test_trends_block_picker_changes_n_and_refetches(tmp_path, conn, version):
    """Compare picks how many 1:1s the charts cover. The choice is read before the address changes (the screen is
    drawn whole, the picked period kept), and Last 8, the default, stays out of the address."""
    out = prep_page(tmp_path, address(AVERY, "2026-10-06"), {
        "/api/prep": latest(), tech_url(AVERY, "2026-10-06"): avery_view("2026-10-06"),
        trends_url(AVERY, 26): AVERY_TRENDS_26}, TRENDS_JS + """
  function read() {
    return { options: compare().querySelectorAll("option").map(function (o) { return [o.getAttribute("value"), o.textContent]; }),
      value: compare().value, label: text(compare().closest("label.filt").querySelectorAll("b")[0]), note: changeText(),
      cards: trendsBox().querySelectorAll(".trend-card").length, behind: behindLabel(), hash: location.hash,
      loading: loading() };
  }
  var out = { first: read(), gets: trendGets() };
  await choose("26");
  out.year = read();
  out.yearGets = trendGets();
  await choose("8");
  out.back = read();
  out.backGets = trendGets();
  out.details = getRequests("/api/prep/tech");
  return out;
""", schedule={AVERY: {"date": "2026-10-20", "weeks": 2}})
    first = out["first"]
    assert first["options"] == [[str(n), label] for n, label in zip((4, 8, 13, 26), SPANS_2)]
    assert first["value"] == "8" and first["label"] == "Compare"
    assert first["note"] == "Change: the latest 1:1 against the average of the 3 before it"
    assert first["cards"] == 8 and first["behind"] == "Behind the numbers · 1:1 10/6 (9/22 – 10/5)"
    assert out["gets"] == [trends_url(AVERY, 8)]
    year = out["year"]
    assert year["hash"] == "#/oneonone?tech=avery%40example.com&day=2026-10-06&n=26"
    assert year["value"] == "26" and year["loading"] == []
    assert year["note"] == "Change: the latest 1:1 against the average of the 5 before it"
    assert year["behind"] == first["behind"], "the picked period stays"
    assert out["yearGets"] == [trends_url(AVERY, 8), trends_url(AVERY, 26), trends_url(AVERY, 26)], \
        "read before the address changes, then again in the background once drawn from it"
    back = out["back"]
    assert back["hash"] == address(AVERY, "2026-10-06") and back["value"] == "8"
    assert out["backGets"] == out["yearGets"] + [trends_url(AVERY, 8)], \
        "a range already read is drawn from the kept answer, which is read again in the background"
    assert out["details"] == [tech_url(AVERY, "2026-10-06")] * 3, "kept, and read again behind each drawing"


def test_compare_month_spans_follow_the_repeat(tmp_path, conn, version):
    """The month spans in brackets come from the tech's repeat; with no 1:1 date set they are left off."""
    script = TRENDS_JS + "return compare().querySelectorAll('option').map(text);"
    bodies = {"/api/prep": latest(), tech_url(AVERY): avery_view()}
    weekly = prep_page(tmp_path, address(AVERY), bodies, script, schedule={AVERY: {"date": "2026-10-20", "weeks": 1}})
    monthly = prep_page(tmp_path, address(AVERY), bodies, script, schedule={AVERY: {"date": "2026-10-20", "weeks": 4}})
    unset = prep_page(tmp_path, address(AVERY), bodies, script, schedule={})
    assert weekly == ["Last 4 1:1s (1 month)", "Last 8 1:1s (2 months)", "Last 13 1:1s (3 months)",
                      "Last 26 1:1s (6 months)"]
    assert monthly == ["Last 4 1:1s (4 months)", "Last 8 1:1s (7 months)", "Last 13 1:1s (1 year)",
                       "Last 26 1:1s (2 years)"]
    assert unset == ["Last 4 1:1s", "Last 8 1:1s", "Last 13 1:1s", "Last 26 1:1s"]


def test_trend_card_change_classes(tmp_path, conn, version):
    """Each card: its label, the latest value and its change against the average of the 1:1s before it, better or
    worse by the measure's direction, flat for a neutral one, "no change" when it rounds to zero, percents in pts."""
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, TRENDS_JS + """
  var grid = trendsBox().querySelectorAll(".trend-grid")[0];
  return { cls: grid.className, cards: grid.children.map(function (card) {
    var delta = card.querySelectorAll(".mdelta")[0];
    var svg = card.querySelectorAll("svg.chart")[0];
    return { cls: card.className, label: text(card.querySelectorAll("h4.panel-label")[0]),
      now: text(card.querySelectorAll(".trend-now")[0]), delta: delta ? [delta.textContent, delta.className] : null,
      svg: svg ? svg.getAttribute("aria-label") : null,
      dots: card.querySelectorAll("circle.dot").filter(function (d) { return !d.classList.contains("ref"); }).length };
  }) };
""")
    assert out["cls"] == "trend-grid"
    cards = out["cards"]
    assert all(c["cls"] == "panel trend-card" for c in cards)
    assert [(c["label"], c["now"], c["delta"]) for c in cards] == [
        ("Total calls", "36", ["▲ 4", "mdelta better"]),
        ("% of calls", "7.36%", ["▲ 0.11 pts", "mdelta better"]),
        ("Declined / missed", "2", ["▲ 1", "mdelta worse"]),
        ("Avg. call time", "8:48", ["no change", "mdelta flat"]),
        ("Solved tickets", "43", ["▼ 1", "mdelta worse"]),
        ("Satisfaction", "75%", ["▼ 15 pts", "mdelta worse"]),
        ("Aging tickets", "-", None),
        ("Attendance", "2", ["▲ 2", "mdelta flat"]),
    ]
    assert [c["svg"] for c in cards] == ["Avery's " + c["label"] + " by 1:1" for c in cards], "drawn on the page"
    assert [c["dots"] for c in cards] == [4, 4, 4, 4, 4, 3, 3, 3], "a dot per 1:1 with a value"


def test_trend_hover_reads_the_team_count_without_a_trailing_zero(tmp_path, conn, version):
    """A 1:1's hovercard: the 1:1, the days its prep covers, the tech's figure and the team's; a count's team figure is
    a mean to one decimal at most, so a whole one reads "33", not "33.0"."""
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, TRENDS_JS + """
  var cards = trendsBox().querySelectorAll(".trend-card");
  function hovers(card) {
    return card.querySelectorAll("g.hit").map(function (g) { return text(g.querySelectorAll("desc")[0]); });
  }
  return { calls: hovers(cards[0]), share: hovers(cards[1]) };
""")
    assert out["calls"] == ["1:1 Sep 8\nCovers Aug 25 – Sep 7\nAvery 30\nTeam 28.5",
                            "1:1 Sep 22\nCovers Sep 8 – Sep 21\nAvery 32\nTeam 30",
                            "1:1 Oct 6\nCovers Sep 22 – Oct 5\nAvery 34\nTeam 31.3",
                            "1:1 Oct 20\nCovers Oct 6 – Oct 19\nAvery 36\nTeam 33"]
    assert out["share"][1].endswith("\nAvery 7.00%\nTeam 6.50%"), "a percent keeps its measure's decimals"


def test_trends_legend_uses_shared_chart_legend(tmp_path, conn, version):
    """Below Behind the numbers, one heading row: Trends, Compare, the shared chart key (the tech's first name and
    Team average) and what the change is against."""
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, TRENDS_JS + """
  var head = trendsBox().querySelectorAll(".trend-head")[0];
  var legend = head.querySelectorAll(".chart-legend")[0];
  var order = screenEl.findAll(function (e) { return /^pp-(behind|trends)$/.test(e.getAttribute("id") || ""); })
    .map(function (e) { return e.getAttribute("id"); });
  return { head: head.children.map(nodeOf), legend: legend.childNodes.map(nodeOf),
    keys: legend.querySelectorAll("span").map(function (s) { return s.getAttribute("aria-hidden"); }),
    legends: trendsBox().querySelectorAll(".chart-legend").length, order: order, note: changeText(),
    fewer: text(trendsBox().querySelectorAll(".trend-head .trend-note")[1]) };
""")
    # The view has 4 of the 8 1:1s Compare asks for: the last note says so.
    assert out["head"] == [["H4", "panel-label"], ["LABEL", "filt"], ["DIV", "chart-legend"], ["SPAN", "trend-note"],
                           ["SPAN", "trend-note"]]
    assert out["legend"] == [["SPAN", "k1"], "Avery", ["SPAN", "k2"], "Team average"]
    assert out["keys"] == ["true", "true"]
    assert out["legends"] == 1, "one key for the eight charts"
    assert out["order"] == ["pp-behind", "pp-trends"]
    assert out["note"] == "Change: the latest 1:1 against the average of the 3 before it"
    assert out["fewer"] == "Only 4 1:1s with data so far (since Sep 8)"
    css = app_css()
    wide = top_level(css)
    assert values(wide, ".trend-grid", "grid-template-columns") == ["repeat(4, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 1499px)"), ".trend-grid", "grid-template-columns") == [
        "repeat(2, minmax(0, 1fr))"]
    for selector in (".trend-head", ".trend-grid", ".trend-now", ".trend-note"):
        assert rules(wide, selector), selector
        for body in rules(wide, selector):
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|\d+px", body), selector + " uses tokens only"


def test_trends_follow_the_tech(tmp_path, conn, version):
    """Opening another tech reads their Trends at the range picked; closing the tech drops the range from the
    address."""
    out = prep_page(tmp_path, address(AVERY) + "&n=13", {
        "/api/prep": latest(), tech_url(AVERY): avery_view(), trends_url(AVERY, 13): AVERY_TRENDS,
        tech_url(BLAKE): {**avery_view(), "email": BLAKE, "name": NAMES[BLAKE]}, trends_url(BLAKE, 13): AVERY_TRENDS},
        TRENDS_JS + """
  var out = { value: compare().value, gets: trendGets() };
  await pick(nameButton("Blake Moreno"));
  out.blake = { hash: location.hash, gets: trendGets(), legend: text(trendsBox().querySelectorAll(".chart-legend")[0]) };
  await pick(nameButton("Blake Moreno"));
  out.closed = location.hash;
  return out;
""")
    assert out["value"] == "13" and out["gets"] == [trends_url(AVERY, 13)]
    assert out["blake"] == {"hash": "#/oneonone?tech=blake%40example.com&n=13",
                            "gets": [trends_url(AVERY, 13), trends_url(BLAKE, 13), trends_url(BLAKE, 13)],
                            "legend": "BlakeTeam average"}
    assert out["closed"] == "#/oneonone"


def test_the_page_that_cannot_be_read_says_why(tmp_path, conn, version):
    out = prep_page(tmp_path, "#/oneonone", {"GET /api/prep": answer({"detail": "The app is busy."}, 500)}, """
  return text(screenEl.querySelectorAll(".message.error")[0]);
""")
    assert out == "The app is busy."


# -- the 1:1 schedule panel's note (Settings) ------------------------------------------------------------------

SCHEDULE_NOTE = ("Changing a date or the repeat takes effect at once. A tech added to the team needs a 1:1 date before "
                 "their preps start.")


def test_schedule_note_says_a_change_takes_effect_at_once(tmp_path, conn, version):
    """Collected calls design: with nothing made, a 1:1 schedule change shows at the next look at 1:1 Prep."""
    out = prep_page(tmp_path, "#/settings", {}, """
  var panel = panelTitled("1:1 schedule");
  return panel.querySelectorAll(".note.table-note").map(text);
""")
    assert out == [SCHEDULE_NOTE]


# -- the shared parts in app.css ------------------------------------------------------------------------------


def test_shared_parts_for_behind_the_numbers():
    """The scrolling list, the flag key line, the good chip, the rating item, the narrow fourth panel and the
    table pill are shared parts in app.css, built from tokens."""
    css = app_css()
    wide = top_level(css)
    assert values(wide, ".list-scroll", "max-height") == ["var(--list-max)"]
    assert values(wide, ".list-scroll", "overflow-y") == ["auto"]
    assert values(wide, ".list-scroll thead th", "position") == ["sticky"]
    assert values(wide, ".list-scroll thead th", "top") == ["0"]
    assert values(wide, ".flag-key", "color") == ["var(--attention)"]
    assert values(wide, ".chip.good", "color") == ["var(--accent)"]
    assert values(wide, ".chip.good", "background") == ["var(--accent-tint)"]
    assert rules(wide, ".rating-item") and rules(wide, ".rating-say") and rules(wide, ".rating-note")
    assert values(wide, ".panel-row.cols-4.last-narrow", "grid-template-columns") == [
        "repeat(3, minmax(0, 1fr)) minmax(0, .55fr)"], "three equal panels and Days out at about 0.55"
    assert values(media_block(css, "(max-width: 1499px)"), ".panel-row.cols-4.last-narrow",
                  "grid-template-columns") == ["repeat(2, minmax(0, 1fr))"]
    assert values(media_block(css, "(max-width: 1023px)"), ".panel-row.cols-4.last-narrow",
                  "grid-template-columns") == ["minmax(0, 1fr)"]
    assert values(wide, ".atable.tight td", "padding-top") == ["var(--sp-2)"]
    assert values(wide, ".pill.cell-pill.today", "background") == ["var(--accent)"]
    assert values(wide, ".pill.cell-pill.soon", "color") == ["var(--accent)"]
    for selector in (".list-scroll", ".flag-key", ".chip.good", ".rating-item", ".rating-line", ".rating-say",
                     ".rating-note", ".pill.cell-pill", ".head-line"):
        for body in rules(wide, selector):
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body), selector + " uses tokens only"


# -- quick polish (T-095) ------------------------------------------------------------------------------------


def test_clicking_anywhere_in_a_latest_row_opens_that_tech(tmp_path, conn, version):
    out = prep_page(tmp_path, "#/oneonone", {"/api/prep": latest(), tech_url(AVERY): avery_view()}, """
  var row = byId("pp-latest").querySelectorAll("tbody tr")[0];
  fire(row, "click");
  await settle();
  var out = { hash: location.hash };
  var copy = row.querySelectorAll("button.small")[0];
  fire(copy, "click");
  await settle();
  out.afterCopy = location.hash;
  return out;
""")
    assert out["hash"].startswith("#/oneonone?tech=")
    assert out["afterCopy"] == out["hash"], "Copy row keeps its own click"


def test_past_periods_scroll_inside_their_panel(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, """
  var box = byId("pp-past").parentNode;
  return { cls: box.className };
""")
    assert set(out["cls"].split()) == {"scroll", "list-scroll"}


# -- a tap on a 1:1 trend (iPad and phone design §10.2) -----------------------------------------------------

GROW_JS = TRENDS_JS + """
var columns = "300px 300px";   // the trend grid's computed columns
var computed = globalThis.getComputedStyle;
globalThis.getComputedStyle = function (el) { return Object.assign(computed(el), { gridTemplateColumns: columns }); };
function cards() { return trendsBox().querySelectorAll(".trend-card"); }
function settleCards() { cards().forEach(function (c) { c.scrollIntoView = function () {}; }); }
/* `type` at `el`, passed up to its card as a browser would. */
function dispatch(el, type, props) {
  var event = Object.assign({ type: type, target: el, preventDefault: function () {}, stopPropagation: function () {} },
    props || {});
  for (var node = el; node; node = node.parentNode) {
    (node.listeners[type] || []).forEach(function (f) { f(event); });
    if (node.classList.contains("trend-card")) {
      break;
    }
  }
}
function tap(el, pointerType) {
  dispatch(el, "pointerdown", { pointerType: pointerType });
  dispatch(el, "click");
}
function big() {
  return cards().map(function (c, i) { return c.classList.contains("big") ? i : -1; }).filter(function (i) { return i >= 0; });
}
function growButton(card) { return card.querySelectorAll("button.trend-grow")[0]; }
function labels() {
  return cards().map(function (c) { var b = growButton(c); return [b.textContent, b.getAttribute("aria-pressed")]; });
}
"""


def test_a_tap_on_a_trend_card_reads_it_and_a_mouse_click_enlarges_it_only_beside_others(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, GROW_JS + """
  settleCards();
  var chart = cards()[0].querySelectorAll("svg.chart")[0];
  var out = {};
  tap(chart, "touch"); out.touch = big();
  tap(chart, "mouse"); out.mouse = big();
  tap(chart, "mouse"); out.again = big();
  dispatch(cards()[1], "click"); out.noPointer = big();
  dispatch(cards()[1], "click");
  columns = "600px";
  tap(cards()[2], "mouse"); out.oneColumn = big();
  return out;
""")
    assert out["touch"] == [], "a tap on a touch screen never enlarges the card"
    assert out["mouse"] == [0], "a mouse click enlarges it, as before, when the cards sit side by side"
    assert out["again"] == [], "and clicking it again puts it back"
    assert out["noPointer"] == [1], "a click with no press before it is not a tap"
    assert out["oneColumn"] == [], "one card to a row: it is already full width, so nothing enlarges"


def test_a_mouse_click_still_shrinks_a_card_left_big_when_the_grid_went_to_one_column(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, GROW_JS + """
  settleCards();
  tap(cards()[0], "mouse");
  var grown = big();
  columns = "600px";
  tap(cards()[0], "mouse");
  return { grown: grown, shrunk: big() };
""")
    assert out == {"grown": [0], "shrunk": []}


# Each chart box's ResizeObserver kept and its animation frames held, so a test can give a chart a new width
# (`resizeChart`) and see the height it is drawn at again.
REDRAW_JS = """
var chartWatchers = [], frames = [], widths = new Map();
globalThis.requestAnimationFrame = function (f) { frames.push(f); };
globalThis.ResizeObserver = function (callback) {
  var watcher = { callback: callback, observe: function (el) { watcher.el = el; }, disconnect: function () {} };
  chartWatchers.push(watcher);
  return watcher;
};
globalThis.layout = function (el) { return { clientWidth: widths.get(el) || 0, scrollWidth: 0 }; };
function resizeChart(box, width) {
  widths.set(box, width);
  chartWatchers.filter(function (w) { return w.el === box; }).forEach(function (w) { w.callback(); });
  var held = frames;
  frames = [];
  held.forEach(function (f) { f(); });
}
"""


def test_a_big_trend_card_goes_back_when_the_grid_turns_to_one_column(tmp_path, conn, version):
    """iPad and phone design §10.2: with the trend grid one column, enlarging is off entirely. A card enlarged while
    the cards sat side by side goes back when the page turns narrow (an iPad turned upright), since there a tap reads
    its chart and the Enlarge button is hidden: its place, its button, and its chart's height, whether the chart is
    drawn again at its new width before the page's width change is heard or after."""
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, GROW_JS + """
  settleCards();
  var card = cards()[2], box = card.querySelectorAll(".chart-box")[0];
  function height() { return box.querySelectorAll("svg.chart")[0].getAttribute("height"); }
  function places() { return cards().map(function (c) { return [c.style.gridColumn || "", c.style.gridRow || ""]; }); }
  var out = { start: height() };
  tap(growButton(card), "touch");
  resizeChart(box, 900);
  out.grown = { big: big(), height: height(), place: places()[2] };
  columns = "600px";
  resizeChart(box, 700);
  out.redrawn = { big: big(), height: height() };
  mediaChange("(max-width: 1023px)", true);
  out.narrow = { big: big(), labels: labels(), places: places() };
  columns = "300px 300px";
  tap(growButton(cards()[1]), "touch");
  mediaChange("(max-width: 1023px)", false);
  out.widened = big();
  return out;
""", before=REDRAW_JS)
    assert out["grown"] == {"big": [2], "height": "380", "place": ["1 / span 2", "2 / span 2"]}
    assert out["redrawn"] == {"big": [2], "height": out["start"]}, "one column: drawn at the card's own height"
    assert out["narrow"] == {"big": [], "labels": [["Enlarge", "false"]] * 8, "places": [["", ""]] * 8}
    assert out["widened"] == [1], "turning wide again leaves the cards as they are"


def test_each_trend_card_has_an_enlarge_button(tmp_path, conn, version):
    out = prep_page(tmp_path, address(AVERY), {"/api/prep": latest(), tech_url(AVERY): avery_view()}, GROW_JS + """
  settleCards();
  var out = { start: labels(), buttons: cards().map(function (c) {
    var b = growButton(c);
    return [c.querySelectorAll("button.trend-grow").length, b.className, b.getAttribute("type"), b.parentNode.className];
  }) };
  tap(growButton(cards()[2]), "touch"); out.grown = big(); out.grownLabels = labels();
  tap(growButton(cards()[3]), "touch"); out.swapped = big(); out.swappedLabels = labels();
  tap(growButton(cards()[3]), "touch"); out.shrunk = big(); out.shrunkLabels = labels();
  return out;
""")
    enlarge, shrink = ["Enlarge", "false"], ["Shrink", "true"]
    assert out["buttons"] == [[1, "small trend-grow", "button", "trend-figs"]] * 8, "one each, beside its figures"
    assert out["start"] == [enlarge] * 8
    assert out["grown"] == [2], "on a touch screen the button enlarges the card"
    assert out["grownLabels"] == [enlarge] * 2 + [shrink] + [enlarge] * 5
    assert out["swapped"] == [3] and out["swappedLabels"] == [enlarge] * 3 + [shrink] + [enlarge] * 4, \
        "enlarging another puts the first back, and its button says so"
    assert out["shrunk"] == [] and out["shrunkLabels"] == [enlarge] * 8


def test_the_enlarge_button_shows_only_on_a_touch_screen_with_cards_side_by_side():
    css = app_css()
    touch_wide = media_block(css, "(pointer: coarse) and (min-width: 1024px)")
    assert any("display: inline-block" in body for body in rules(touch_wide, ".trend-grow"))
    assert any("display: none" in body for body in rules(top_level(css), ".trend-grow"))
    shown = [body for body in rules(css, ".trend-grow") if "display" in body and "display: none" not in body]
    assert len(shown) == 1, "shown nowhere else: a mouse clicks the card itself, and one card to a row has no use for it"
