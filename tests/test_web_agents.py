"""The Agents screen endpoints, coaching notes, and page assets. Synthetic data only."""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_queries import LEE, SAM, add_review, all_met, missing, version  # noqa: F401  (fixture)
from test_web_overview import js_function
from work_assistant import rubric
from work_assistant.retention import purge_expired
from work_assistant.settings import set_setting
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app

STATIC = Path(webapp.__file__).parent / "static"
BASE = "http://127.0.0.1:8765"
SAME_ORIGIN = {"origin": BASE}
KIM = ("kim@example.com", "Kim Agent")


def client(tmp_path):
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 4242,
                     zendesk_factory=lambda: None, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def seeded(conn, version):
    """Sam and Lee in the weeks of Sep 27 and Oct 4 (America/Chicago); Oct 4 is the latest."""
    add_review(conn, version, 1, missing(version, "sc06"), agent=SAM, call="2026-10-05T15:00:00Z")
    add_review(conn, version, 2, all_met(version), agent=SAM, call="2026-10-06T15:00:00Z")
    add_review(conn, version, 3, all_met(version), agent=LEE, call="2026-10-06T16:00:00Z")
    add_review(conn, version, 4, all_met(version), agent=SAM, call="2026-09-28T15:00:00Z")
    return conn


# -- the brief's tests ----------------------------------------------------------------------------------


def test_agent_detail_shape(seeded, tmp_path):
    response = client(tmp_path).get(f"/api/agents/{SAM[0]}?weeks=8")
    assert response.status_code == 200
    body = response.json()
    assert (body["email"], body["name"], body["active"]) == (SAM[0], SAM[1], True)
    assert set(body) >= {"tiles", "trend", "checks", "problems", "notes"}
    assert body["period"]["label"] == "Aug 16 – Oct 10, 2026" and body["has_data_in_period"] is True   # weeks=8
    assert body["tiles"]["contacts"] == 3           # the period's calls: Sep 28, Oct 5 and Oct 6
    assert body["trend"]["unit"] == "week" and len(body["trend"]["steps"]) == 8
    assert body["trend"]["steps"][-1] == {
        "start": "2026-10-04", "end": "2026-10-10", "in_period": True, "so_far": False, "partial": False, "calls": 2,
        "agent": 97.5, "team": 98.3, "rubric_changed": False, "model_changed": False}
    sc06 = {c["check_id"]: c for c in body["checks"]}["sc06"]
    assert set(sc06) == {"check_id", "label", "agent_rate", "team_rate", "gap", "flagged", "applied_count"}
    assert sc06["applied_count"] == 3
    assert [p["ticket_id"] for p in body["problems"]] == [1]
    assert body["notes"] == []


def test_add_and_delete_note(seeded, tmp_path):
    api = client(tmp_path)
    response = api.post(f"/api/agents/{SAM[0]}/notes", json={"body": "  Summarize the issue back.\nRecheck #6.  "},
                        headers=SAME_ORIGIN)
    assert response.status_code == 201
    note = response.json()
    assert set(note) == {"note_id", "created_at", "body"}
    assert note["body"] == "Summarize the issue back.\nRecheck #6."   # outer whitespace trimmed, lines kept
    assert datetime.fromisoformat(note["created_at"].replace("Z", "+00:00")).tzinfo is not None
    later = api.post(f"/api/agents/{SAM[0]}/notes", json={"body": "Second"}, headers=SAME_ORIGIN).json()

    notes = api.get(f"/api/agents/{SAM[0]}").json()["notes"]
    assert [n["note_id"] for n in notes] == [later["note_id"], note["note_id"]]   # newest first
    assert api.get(f"/api/agents/{LEE[0]}").json()["notes"] == []                 # each agent's own

    assert api.delete(f"/api/notes/{note['note_id']}", headers=SAME_ORIGIN).status_code == 200
    assert [n["note_id"] for n in api.get(f"/api/agents/{SAM[0]}").json()["notes"]] == [later["note_id"]]
    gone = api.delete(f"/api/notes/{note['note_id']}", headers=SAME_ORIGIN)
    assert gone.status_code == 404


def test_unknown_agent_404(seeded, tmp_path):
    api = client(tmp_path)
    response = api.get("/api/agents/nobody@example.com")
    assert response.status_code == 404
    assert "nobody@example.com" in response.json()["detail"]
    assert api.post("/api/agents/nobody@example.com/notes", json={"body": "x"},
                    headers=SAME_ORIGIN).status_code == 404


# -- the agent list ---------------------------------------------------------------------------------------


def test_agents_list_includes_removed_and_new_team_members(seeded, tmp_path):
    set_setting(seeded, "team", [SAM[0], KIM[0]])   # Lee removed; Kim added, no calls yet
    body = client(tmp_path).get("/api/agents").json()
    assert body["agents"] == [
        {"email": KIM[0], "name": None, "active": True, "has_history": False},
        {"email": LEE[0], "name": LEE[1], "active": False, "has_history": True},
        {"email": SAM[0], "name": SAM[1], "active": True, "has_history": True},
    ]
    assert body["default"] == SAM[0]   # the first active agent with history


def test_agents_list_name_is_the_newest_and_email_is_normalized(conn, version, tmp_path):
    add_review(conn, version, 1, all_met(version), agent=(" SAM@Example.com ", "Old Name"),
               call="2026-09-28T15:00:00Z")
    add_review(conn, version, 2, all_met(version), agent=SAM, call="2026-10-05T15:00:00Z")
    body = client(tmp_path).get("/api/agents").json()
    sam = [a for a in body["agents"] if a["email"] == SAM[0]]
    assert sam == [{"email": SAM[0], "name": SAM[1], "active": True, "has_history": True}]


def test_agents_list_empty_database(tmp_path):
    body = client(tmp_path).get("/api/agents").json()
    assert body == {"agents": [], "default": None}


def test_agents_list_default_falls_back_to_any_agent(seeded, tmp_path):
    set_setting(seeded, "team", [])
    assert client(tmp_path).get("/api/agents").json()["default"] == LEE[0]   # first by name


# -- agent detail -----------------------------------------------------------------------------------------


def test_removed_agent_detail_is_inactive(seeded, tmp_path):
    set_setting(seeded, "team", [SAM[0]])
    body = client(tmp_path).get(f"/api/agents/{LEE[0]}").json()
    assert body["active"] is False and body["name"] == LEE[1]


def test_team_member_without_calls_has_an_empty_page(seeded, tmp_path):
    set_setting(seeded, "team", [SAM[0], KIM[0]])
    body = client(tmp_path).get(f"/api/agents/{KIM[0]}").json()
    assert body["name"] is None and body["active"] is True and body["has_data_in_period"] is False
    assert body["tiles"]["avg_percent"] is None and body["problems"] == []
    assert body["tiles"]["contacts"] is None and body["tiles"]["contacts_rank"] is None
    assert all(c["agent_rate"] is None and c["gap"] is None for c in body["checks"])


def test_agent_path_is_case_insensitive(seeded, tmp_path):
    body = client(tmp_path).get("/api/agents/SAM%40EXAMPLE.COM").json()
    assert body["email"] == SAM[0]


def test_weeks_parameter(seeded, tmp_path):
    api = client(tmp_path)
    body = api.get(f"/api/agents/{SAM[0]}?weeks=4").json()      # 28 days, so the trend steps by day
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-09-13", "2026-10-10")
    steps = body["trend"]["steps"]
    assert body["trend"]["unit"] == "day" and len(steps) == 28 and steps[0]["start"] == "2026-09-13"
    body = api.get(f"/api/agents/{SAM[0]}?weeks=1").json()
    assert body["period"]["start"] == "2026-10-04" and body["tiles"]["avg_percent"] == 97.5   # Oct 4 week only


@pytest.mark.parametrize("weeks", ["0", "53", "abc", "-2", "8.5"])
def test_weeks_parameter_rejected(seeded, tmp_path, weeks):
    response = client(tmp_path).get(f"/api/agents/{SAM[0]}?weeks={weeks}")
    assert response.status_code == 400
    assert "weeks" in response.json()["detail"]


def test_week_parameter_moves_the_period(seeded, tmp_path):
    body = client(tmp_path).get(f"/api/agents/{SAM[0]}?week=2026-09-30").json()   # 8 weeks to Oct 3
    assert (body["period"]["start"], body["period"]["end"]) == ("2026-08-09", "2026-10-03")
    assert body["tiles"]["contacts"] == 1
    assert client(tmp_path).get(f"/api/agents/{SAM[0]}?week=yesterday").status_code == 400


def test_trend_marks_rubric_changes(seeded, version, tmp_path):
    changed = tuple(dataclasses.replace(c, summary=c.summary + " (reworded)") if c.check_id == "sc06" else c
                    for c in version.checks)
    v2 = rubric.save_version(seeded, changed, version.reference, version.params, "reworded #6")
    add_review(seeded, v2, 5, all_met(v2), agent=LEE, call="2026-10-12T15:00:00Z")
    trend = client(tmp_path).get(f"/api/agents/{SAM[0]}").json()["trend"]["steps"]
    assert trend[-1]["start"] == "2026-10-11" and trend[-1]["rubric_changed"] is True
    assert [t["rubric_changed"] for t in trend[:-1]] == [False] * 7


def test_review_failed_call_is_not_a_problem(conn, version, tmp_path):
    """T-045: a review that did not finish is Review failed, not one of the agent's problem calls."""
    add_review(conn, version, 1, {"pr-call-length": "missed"}, status="error", agent=SAM)
    body = client(tmp_path).get(f"/api/agents/{SAM[0]}").json()
    assert body["problems"] == [] and body["tiles"]["problem_rate"] == 0.0
    assert body["tiles"]["contacts"] == 1 and body["tiles"]["avg_percent"] is None


# -- coaching notes ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{"body": ""}, {"body": "   \n "}, {"body": 5}, {}, ["note"],
                                     {"body": "x" * 10001}])
def test_note_body_validated(seeded, tmp_path, payload):
    response = client(tmp_path).post(f"/api/agents/{SAM[0]}/notes", json=payload, headers=SAME_ORIGIN)
    assert response.status_code == 400
    assert response.json()["detail"]


def test_note_stored_under_normalized_email(seeded, tmp_path):
    api = client(tmp_path)
    api.post("/api/agents/Sam%40Example.com/notes", json={"body": "hello"}, headers=SAME_ORIGIN)
    row = seeded.execute("SELECT agent_email, body FROM coaching_notes").fetchone()
    assert (row["agent_email"], row["body"]) == (SAM[0], "hello")


def test_note_for_team_member_without_calls(seeded, tmp_path):
    set_setting(seeded, "team", [SAM[0], KIM[0]])
    api = client(tmp_path)
    assert api.post(f"/api/agents/{KIM[0]}/notes", json={"body": "Welcome call"},
                    headers=SAME_ORIGIN).status_code == 201
    # Removed later, with no calls: the note keeps the agent on the list so it stays reachable.
    set_setting(seeded, "team", [SAM[0]])
    agents = {a["email"]: a for a in api.get("/api/agents").json()["agents"]}
    assert agents[KIM[0]]["active"] is False
    assert [n["body"] for n in api.get(f"/api/agents/{KIM[0]}").json()["notes"]] == ["Welcome call"]


def test_note_markup_is_stored_as_plain_text(seeded, tmp_path):
    api = client(tmp_path)
    text = "<img src=x onerror=alert(1)> & <b>bold</b>"
    note = api.post(f"/api/agents/{SAM[0]}/notes", json={"body": text}, headers=SAME_ORIGIN).json()
    assert note["body"] == text


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "null"])
def test_note_changes_from_a_foreign_origin_rejected(seeded, tmp_path, origin):
    api = client(tmp_path)
    assert api.post(f"/api/agents/{SAM[0]}/notes", json={"body": "x"},
                    headers={"origin": origin}).status_code == 403
    note = api.post(f"/api/agents/{SAM[0]}/notes", json={"body": "x"}, headers=SAME_ORIGIN).json()
    assert api.delete(f"/api/notes/{note['note_id']}", headers={"origin": origin}).status_code == 403
    assert len(api.get(f"/api/agents/{SAM[0]}").json()["notes"]) == 1


def test_notes_survive_the_retention_purge(seeded, tmp_path):
    api = client(tmp_path)
    api.post(f"/api/agents/{SAM[0]}/notes", json={"body": "Keep me"}, headers=SAME_ORIGIN)
    purged = purge_expired(seeded, datetime(2027, 6, 1, tzinfo=timezone.utc), 90, tmp_path / "logs")
    assert purged == 4
    assert [n["body"] for n in api.get(f"/api/agents/{SAM[0]}").json()["notes"]] == ["Keep me"]


# -- the page over a period (Task 5, T-043) ---------------------------------------------------------------

AGENT_PAGE_JS = """
run(async function () {
  var tiles = all(screenEl, function (e) { return e.className === "tile" || e.className === "tile lead"; })
    .map(function (t) { return t.children.map(function (c) { return c.textContent; }); });
  var subs = panels().map(function (p) {
    var sub = p.children.filter(function (c) { return c.className === "sub"; })[0];
    return [titleOf(p), sub ? sub.textContent : null];
  });
  var placeholders = all(screenEl, function (e) { return (e.className || "").indexOf("placeholder") === 0; })
    .map(function (e) { return e.textContent; });
  var tickets = links(screenEl).filter(function (l) {
    return (l.href || "").indexOf("#/tickets?") === 0 && l.href.indexOf("ticket=") < 0;
  });
  var head = all(screenEl, function (e) { return e.className === "period-head"; })[0];
  return { tiles: tiles, panels: subs, placeholders: placeholders, tickets: tickets, head: head ? head.textContent : null };
});
"""


def agent_page_out(tmp_path, email, query=""):
    """The Agents page for `email` in node, given what the endpoint answers for `query`."""
    from urllib.parse import quote

    from test_compare import api_for
    from test_web_test_view import page_responses, run_page

    api = api_for(tmp_path)
    responses = page_responses(api, "/api/agents")
    got = api.get(f"/api/agents/{email}{query}")
    responses["GET /api/agents/" + quote(email, safe="")] = {"status": got.status_code, "body": got.json()}
    return run_page("#/agents/" + quote(email, safe=""), responses, AGENT_PAGE_JS)


def test_agent_page_follows_the_period(seeded, tmp_path):
    """The period is named once, above the tiles, and the Tickets links carry its days, the agent and the
    Problem verdict, not the trend's first step."""
    out = agent_page_out(tmp_path, SAM[0])
    assert out["head"] == "Last 8 weeks (Aug 16 – Oct 10) · 3 calls by Sam Agent"
    assert [t[:3] for t in out["tiles"]] == [
        ["Avg score", "98.3", "Team: 98.8"],
        ["Problem rate", "33.3%", "▲ 8.3 pts above team (25%)"],
        ["Tickets", "3", "Most on the team"],
        ["Biggest gap vs team", "#6 Triage: Verify understanding", "▼ 8.3 pts below team"]]
    titles = dict(out["panels"])
    assert titles["Score vs team average"] == "By week, Aug 16 – Oct 10, out of 100"
    assert "Pass rate by check" in titles and "Problem calls (1)" in titles
    days = "start=2026-08-16&end=2026-10-10&agent=sam%40example.com"
    assert {"text": "See all problem calls", "href": "#/tickets?verdict=problem&" + days} in out["tickets"]
    assert {"text": "#6 Triage: Verify understanding", "href": "#/tickets?missed_check=sc06&" + days} in out["tickets"]


def test_agent_page_without_calls_in_the_period(seeded, tmp_path):
    """Review Focus 2: Lee had no calls on Mon Sep 28, Sam had one. The page says so, never 0, and the
    team's figures still show."""
    out = agent_page_out(tmp_path, LEE[0], "?period=custom&start=2026-09-28&end=2026-09-28")
    assert out["head"] == "Mon, Sep 28, 2026 · 0 calls by Lee Agent"
    assert [t[:3] for t in out["tiles"]] == [
        ["Avg score", "-", "Team: 100"],
        ["Problem rate", "-", "No calls in this period"],
        ["Tickets", "-", "No calls in this period"],
        ["Biggest gap vs team", "-", "Not behind the team on any check"]]
    titles = dict(out["panels"])
    assert titles["Score vs team average"] == "By day, Sep 15 – Sep 28, out of 100 · shaded: Mon, Sep 28, 2026"
    assert "Problem calls (0)" in titles
    assert "No problem calls in this period." in out["placeholders"]


# -- page assets ----------------------------------------------------------------------------------------


def test_agents_page_wired_up():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "renderAgents" in js and "/api/agents" in js and "/api/notes/" in js
    assert "(not on team)" in js
    assert '"Problem calls ("' in js
    assert "judged on " in js
    assert "Review failed" in js
    assert "window.confirm(" in js
    for param in ("ticket=", "verdict", "missed_check", "start", "end", "agent"):
        assert param in js, param
    assert "innerHTML" not in js and "innerHTML" not in (STATIC / "charts.js").read_text(encoding="utf-8")


def test_agents_page_styles():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    coach = re.search(r"\.coach-body\s*\{[^}]*\}", css)
    assert coach and "white-space: pre-wrap" in coach.group(0)
    for selector in (".passbar .track", ".passbar .refmark", ".passbar .fillbar.crit", "textarea"):
        assert selector in css, selector


COACHING_HEAD = "Coaching targets: 10+ points behind the team"
EVERYTHING_HEAD = "Everything else, sorted by gap"


def test_agents_panel_order():
    """Spec 5.2: agent pills and the period, a lead tile row, the chart beside coaching notes, the full
    pass-rate table, then the agent's problem calls."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    page = js_function(js, "agentPage")
    helpers = ["agentFilters", "agentTiles", "agentChart", "notesPanel", "checksPanel", "agentProblemsPanel"]
    calls = [page.index(name + "(") for name in helpers]
    assert calls == sorted(calls), "agentPage builds the panels out of spec order"

    # The rows are laid out in agentPage.
    assert re.search(r'"panel-row cols-4-lead"[^;]*agentTiles\(', page), "the tiles are one lead row"
    assert re.search(r'"panel-row cols-2-1[^"]*"[^;]*chart\.panel[^;]*notesPanel\(', page), \
        "the chart and coaching notes share one cols-2-1 row"
    for full_width in ("checksPanel(", "agentProblemsPanel("):
        assert not re.search(r'"panel-row[^"]*"[^;]*' + re.escape(full_width), page), full_width + " is full width"
    assert '"grid2"' not in page and '"tiles"' not in js_function(js, "agentTiles")

    # The agents are pill links, one marked current; the period stays a select.
    filters = js_function(js, "agentFilters")
    assert '"ag-agent"' not in filters and '"pills"' in filters
    assert re.search(r'"pill on"\s*:\s*"pill"', filters) and '"aria-current"' in filters
    assert 'periodControl("ag", ' in filters and "compareControl" not in filters

    # The labels read in spec order across the helpers, in the order agentPage calls them.
    source = "".join(js_function(js, name) for name in helpers)
    position = 0
    for label in ('"pills"', 'periodControl(', '"Avg score"', '"Problem rate"', '"Tickets"', '"Biggest gap vs team"',
                  '"Score vs team average"', '"Your coaching notes"', '"Pass rate by check"', COACHING_HEAD,
                  EVERYTHING_HEAD, '"Problem calls ("'):
        found = source.find(label, position)
        assert found >= 0, label + " missing or out of order"
        position = found + len(label)

    # The agent's Avg score is the lead number.
    assert re.search(r'tile\("Avg score",[\s\S]*?Review errors are left out\.",\s*true\)',
                     js_function(js, "agentTiles"))


def test_checks_grouping_labels():
    """Every check stays listed, in two group-head groups: the coaching targets, then everything else."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    checks = js_function(js, "checksPanel")
    coaching = checks.find('"' + COACHING_HEAD + '"')
    everything = checks.find('"' + EVERYTHING_HEAD + '"')
    assert 0 <= coaching < everything
    assert '"group-head"' in checks
    assert '"No checks are 10+ points behind the team."' in checks
    assert "checkGroups(detail.checks)" in checks
    # The coaching-target rows keep their highlight, and each row keeps its link, bar, rates, gap and count.
    for kept in ('"flagrow"', '"checklink"', "Charts.passBar(", "pct(c.agent_rate)", "pct(c.team_rate)",
                 "gapText(c.gap)", '"judged on "'):
        assert kept in checks, kept

    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".atable tr.group-head td" in css
    assert ".atable.nowrap td" not in css   # the overview's old table class has no user left


def run_check_groups(checks):
    """Run app.js's checkGroups on `checks` in node and return its {targets, rest} as check ids."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    script = (js_function(js, "checkGroups") + "\nvar g = checkGroups(" + json.dumps(checks) + ");\n" +
              "process.stdout.write(JSON.stringify({targets: g.targets.map(function (c) { return c.check_id; }), " +
              "rest: g.rest.map(function (c) { return c.check_id; })}));")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_check_groups_sort_by_gap_most_behind_first():
    def check(check_id, gap, flagged=False):
        return {"check_id": check_id, "gap": gap, "flagged": flagged}

    checks = [check("a", -4.0), check("b", -12.5, True), check("c", None), check("d", 3.0), check("e", -30.0, True),
              check("f", -10.0, True), check("g", -6.0), check("h", None), check("i", 0.0)]
    groups = run_check_groups(checks)
    assert groups == {"targets": ["e", "b", "f"], "rest": ["g", "a", "i", "d", "c", "h"]}


def test_check_groups_with_nothing_flagged():
    groups = run_check_groups([{"check_id": "a", "gap": 2.0, "flagged": False},
                               {"check_id": "b", "gap": -9.9, "flagged": False}])
    assert groups == {"targets": [], "rest": ["b", "a"]}
