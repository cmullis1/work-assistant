"""The app server: page shell, Settings API, runs, status banner, and startup work. Fakes only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factories import add_run
from work_assistant import cli, credentials, pipeline
from work_assistant.db import connect, init_db
from work_assistant.procutil import resume_lock
from work_assistant.rubric import ensure_seeded
from work_assistant.settings import get_setting, set_setting
from work_assistant.web import app as webapp
from work_assistant.web.app import Unavailable, create_app

STATIC = Path(webapp.__file__).parent / "static"
BASE = "http://127.0.0.1:8765"


class FakeZendesk:
    """Knows a fixed set of agent emails and has no tickets."""

    def __init__(self, known=(), end_users=()):
        self.known = set(known)
        self.end_users = set(end_users)
        self.lookups: list[list[str]] = []
        self.closed = False

    def users_by_email(self, emails):
        self.lookups.append(list(emails))
        found = {}
        for i, email in enumerate(emails):
            if email in self.known:
                found[email] = {"id": i + 1, "email": email, "role": "agent"}
            elif email in self.end_users:
                found[email] = {"id": i + 1, "email": email, "role": "end-user"}
        return found

    def ticket_fields(self):
        return {}

    def incremental_tickets(self, start):
        return iter(())

    def close(self):
        self.closed = True


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "work-assistant.db"
    conn = connect(path)
    init_db(conn)
    ensure_seeded(conn)
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = connect(db_path)
    yield conn
    conn.close()


def client_for(db_path, tmp_path, *, starter=None, zendesk=None, factory=None):
    if factory is None:
        def factory():
            return zendesk if zendesk is not None else FakeZendesk()
    app = create_app(db_path, run_starter=starter or (lambda spec: 4242), zendesk_factory=factory,
                     log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


# -- the brief's tests ----------------------------------------------------------------------------


def test_index_served(db_path, tmp_path):
    response = client_for(db_path, tmp_path).get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for label in ("Work Assistant", "Call QA", "Team overview", "Techs", "Tickets", "Rubric &amp; rules", "Team",
                  "Settings"):
        assert f">{label}<" in response.text
    assert client_for(db_path, tmp_path).get("/static/app.css").status_code == 200


def test_settings_never_returns_secrets(db_path, tmp_path, memory_keyring):
    credentials.set_secret("zendesk_token", "tok-XYZ")
    response = client_for(db_path, tmp_path).get("/api/settings")
    assert response.status_code == 200
    assert "tok-XYZ" not in response.text
    body = response.json()
    assert body["credentials"]["zendesk_token"] is True
    assert body["credentials"]["anthropic_api_key"] is False
    for key in ("team", "zendesk_subdomain", "zendesk_email", "timezone", "budget_limit_usd",
                "problem_threshold", "effort", "retention_days"):
        assert key in body


def test_put_unknown_setting_rejected(db_path, tmp_path, db):
    response = client_for(db_path, tmp_path).put("/api/settings", json={"budget_limit_usd": 10, "colour": "red"})
    assert response.status_code == 400
    assert response.json()["unknown_settings"] == ["colour"]
    assert get_setting(db, "budget_limit_usd") == 40.0  # nothing saved


def test_add_unknown_agent_rejected(db_path, tmp_path, db):
    zendesk = FakeZendesk(known={"a@x.com"})
    response = client_for(db_path, tmp_path, zendesk=zendesk).put(
        "/api/settings", json={"team": ["a@x.com", "typo@x.com"]})
    assert response.status_code == 400
    assert response.json()["unknown_agents"] == ["typo@x.com"]
    assert get_setting(db, "team") == []
    assert zendesk.closed


def test_remove_agent_no_lookup(db_path, tmp_path, db):
    set_setting(db, "team", ["a@x.com", "b@x.com"])

    def factory():
        raise AssertionError("removing an agent must not look anything up")

    response = client_for(db_path, tmp_path, factory=factory).put("/api/settings", json={"team": ["b@x.com"]})
    assert response.status_code == 200
    assert get_setting(db, "team") == ["b@x.com"]


def test_run_estimate_then_start(db_path, tmp_path, db):
    started = []
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 4242,
                        zendesk=FakeZendesk())
    body = {"from": "2026-10-04", "to": "2026-10-10", "rereview": False}
    estimate = client.post("/api/runs/estimate", json=body)
    assert estimate.status_code == 200
    assert estimate.json() == {"estimate_usd": 0.0, "contacts": 0, "excluded_purged": 0}
    assert started == []  # the estimate never starts anything
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    response = client.post("/api/runs", json=body)
    assert response.status_code == 202
    assert started == [{"command": "run", "from": "2026-10-04", "to": "2026-10-10", "rereview": False}]


def test_runs_total_spent(db_path, tmp_path, db):
    add_run(db, actual_usd=1.25)
    add_run(db, actual_usd=2.50)
    add_run(db, status="failed", error="ZendeskError")  # no cost
    body = client_for(db_path, tmp_path).get("/api/runs").json()
    assert body["total_spent_usd"] == 3.75
    assert len(body["runs"]) == 3
    assert body["runs"][0]["run_id"] == 3  # newest first


def test_serve_binds_localhost():
    config = cli.serve_config()
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 8765


def _strip_comments(text: str, suffix: str) -> str:
    if suffix == ".html":
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", text)


@pytest.mark.parametrize("name", ["app.js", "charts.js", "index.html"])
def test_static_has_no_inline_colors(name):
    path = STATIC / name
    text = _strip_comments(path.read_text(encoding="utf-8"), path.suffix)
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", text) == []
    assert 'style="' not in text and "style='" not in text
    assert "rgb(" not in text and "rgba(" not in text and "hsl(" not in text


def test_status_reports_failed_run(db_path, tmp_path, db):
    add_run(db, status="complete", actual_usd=1.0)
    run_id = add_run(db, status="failed", error="ZendeskError", finished_at="2026-10-13T11:05:00Z")
    body = client_for(db_path, tmp_path).get("/api/status").json()
    assert body["last_run"] == {"run_id": run_id, "status": "failed", "error": "ZendeskError",
                                "error_label": webapp.ZENDESK_UNREACHABLE,
                                "finished_at": "2026-10-13T11:05:00Z", "alert": True}
    assert body["awaiting_confirmation"] == []


# -- more -----------------------------------------------------------------------------------------


def test_app_css_defines_dark_tokens():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{([^}]*)\}", css)
    assert root, ":root block"
    tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", root.group(1)))
    expected = {"--page": "#0b1020", "--bar": "#11182d", "--card-top": "#18213b", "--card-bottom": "#121a30",
                "--grid": "#233056", "--inset": "#0f1730", "--ink": "#e8ecf6", "--ink2": "#9aa6c4",
                "--headline": "#ffffff", "--accent": "#5eead4", "--attention": "#fbbf24", "--unsure": "#c4b5fd",
                "--failure": "#fb7185"}
    for token, value in expected.items():
        assert tokens.get(token, "").lower() == value, token
    for token in ("--card-top", "--ink2", "--base", "--series-1", "--accent", "--attention", "--grid",
                  "--failure-tint", "--fs-base", "--fs-label", "--fs-tile", "--fs-lead", "--fs-detail-head",
                  "--radius-card", "--gap-panel", "--pad-page"):
        assert token in tokens, token
        assert css.count(token + ":") == 1, token  # one dark set, defined once
    for alias in ("--surface-1", "--muted", "--good", "--crit", "--ring"):
        assert alias not in tokens, alias + " duplicated a role token; use the role name"
    assert "color-scheme: dark" in root.group(1)
    assert "data-theme" not in css and "prefers-color-scheme" not in css and "#fcfcfb" not in css


def test_status_with_no_runs(db_path, tmp_path):
    body = client_for(db_path, tmp_path).get("/api/status").json()
    assert body == {"last_run": None, "awaiting_confirmation": [], "submission_unknown": []}


def test_status_lists_awaiting_and_unknown(db_path, tmp_path, db):
    """Each run the banner lists comes with its kind and model, so a model test can say so."""
    waiting = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    unknown = add_run(db, status="submission_unknown", error=pipeline.SUBMISSION_UNRECORDED)
    test = add_run(db, kind="test", status="awaiting_confirmation", estimate_usd=55.0)
    db.execute("UPDATE runs SET model = 'claude-sonnet-5-5' WHERE run_id = ?", (test,))
    db.commit()
    body = client_for(db_path, tmp_path).get("/api/status").json()
    assert body["awaiting_confirmation"] == [
        {"run_id": waiting, "kind": "manual", "model": "claude-opus-5-5", "model_label": "Claude Opus 5.5"},
        {"run_id": test, "kind": "test", "model": "claude-sonnet-5-5", "model_label": "Claude Sonnet 5.5"}]
    assert body["submission_unknown"] == [{"run_id": unknown, "kind": "manual", "model": "claude-opus-5-5",
                                           "model_label": "Claude Opus 5.5",
                                           "message": pipeline.SUBMISSION_UNRECORDED}]
    assert body["last_run"]["alert"] is False


def test_cleared_run_raises_no_alert(db_path, tmp_path, db):
    add_run(db, status="failed", error=pipeline.CLEARED_BY_OWNER)
    assert client_for(db_path, tmp_path).get("/api/status").json()["last_run"]["alert"] is False


def test_clear_unknown_run(db_path, tmp_path, db):
    run_id = add_run(db, status="submission_unknown", error=pipeline.SUBMISSION_UNRECORDED)
    client = client_for(db_path, tmp_path)
    assert client.post(f"/api/runs/{run_id}/clear").status_code == 200
    row = db.execute("SELECT status, error FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert (row["status"], row["error"]) == ("failed", pipeline.CLEARED_BY_OWNER)
    assert client.post(f"/api/runs/{run_id}/clear").status_code == 409
    assert client.post("/api/runs/999/clear").status_code == 404


def test_cancel_waiting_run(db_path, tmp_path, db):
    """Cancel answers with the run's new status; an unknown run is 404, and any run not waiting for the OK is
    409 with the plain reason, unchanged."""
    waiting = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    submitted = add_run(db, status="submitted")
    client = client_for(db_path, tmp_path)
    response = client.post(f"/api/runs/{waiting}/cancel")
    assert (response.status_code, response.json()) == (200, {"run_id": waiting, "status": "cancelled"})
    row = db.execute("SELECT status, error FROM runs WHERE run_id = ?", (waiting,)).fetchone()
    assert (row["status"], row["error"]) == ("cancelled", pipeline.CANCELLED_BY_OWNER)
    assert client.get("/api/status").json()["awaiting_confirmation"] == []

    again = client.post(f"/api/runs/{waiting}/cancel")
    assert again.status_code == 409
    assert again.json()["detail"] == f"Only a run waiting for your OK can be cancelled. Run {waiting} is cancelled."
    assert client.post(f"/api/runs/{submitted}/cancel").status_code == 409
    assert db.execute("SELECT status FROM runs WHERE run_id = ?", (submitted,)).fetchone()[0] == "submitted"
    missing = client.post("/api/runs/999/cancel")
    assert (missing.status_code, missing.json()["detail"]) == (404, "Run 999 does not exist.")


def test_confirm_starts_confirm_command(db_path, tmp_path, db):
    started = []
    waiting = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    done = add_run(db, status="complete")
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 1)
    assert client.post(f"/api/runs/{waiting}/confirm").status_code == 202
    assert started == [{"command": "confirm", "run_id": waiting}]
    assert client.post(f"/api/runs/{done}/confirm").status_code == 409
    assert client.post("/api/runs/999/confirm").status_code == 404
    assert len(started) == 1


def test_run_requests_validated(db_path, tmp_path):
    started = []
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 1)
    for body in ({"from": "2026-10-10", "to": "2026-10-04"}, {"from": "10/04/2026", "to": "2026-10-10"},
                 {"to": "2026-10-10"}, {"from": "2026-10-04", "to": "2026-10-10", "rereview": "yes"}):
        assert client.post("/api/runs", json=body).status_code == 400, body
        assert client.post("/api/runs/estimate", json=body).status_code == 400, body
    assert started == []


def test_rereview_flag_passed_to_starter(db_path, tmp_path):
    started = []
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 1)
    client.post("/api/runs", json={"from": "2026-10-04", "to": "2026-10-04", "rereview": True})
    assert started == [{"command": "run", "from": "2026-10-04", "to": "2026-10-04", "rereview": True}]


def test_estimate_unavailable_without_zendesk(db_path, tmp_path):
    def factory():
        raise Unavailable("Zendesk is not set up yet. Run `work-assistant setup`.")

    response = client_for(db_path, tmp_path, factory=factory).post(
        "/api/runs/estimate", json={"from": "2026-10-04", "to": "2026-10-10", "rereview": False})
    assert response.status_code == 503
    assert "work-assistant setup" in response.json()["detail"]


def test_start_run_unavailable_is_503(db_path, tmp_path):
    def starter(spec):
        raise Unavailable("Runs are turned off in the demo.")

    response = client_for(db_path, tmp_path, starter=starter).post(
        "/api/runs", json={"from": "2026-10-04", "to": "2026-10-10", "rereview": False})
    assert response.status_code == 503
    assert response.json()["detail"] == "Runs are turned off in the demo."


def test_team_normalized_and_only_new_emails_looked_up(db_path, tmp_path, db):
    set_setting(db, "team", ["a@x.com"])
    zendesk = FakeZendesk(known={"a@x.com", "b@x.com"})
    response = client_for(db_path, tmp_path, zendesk=zendesk).put(
        "/api/settings", json={"team": [" A@X.com", "B@x.com ", "b@x.com"]})
    assert response.status_code == 200
    assert get_setting(db, "team") == ["a@x.com", "b@x.com"]
    assert zendesk.lookups == [["b@x.com"]]


def test_end_user_is_not_an_agent(db_path, tmp_path, db):
    zendesk = FakeZendesk(end_users={"customer@x.com"})
    response = client_for(db_path, tmp_path, zendesk=zendesk).put(
        "/api/settings", json={"team": ["customer@x.com"]})
    assert response.status_code == 400
    assert response.json()["unknown_agents"] == ["customer@x.com"]


def test_add_agent_when_zendesk_unavailable_is_503(db_path, tmp_path, db):
    def factory():
        raise Unavailable("Zendesk is not set up yet. Run `work-assistant setup`.")

    response = client_for(db_path, tmp_path, factory=factory).put(
        "/api/settings", json={"team": ["a@x.com"], "budget_limit_usd": 12})
    assert response.status_code == 503
    assert get_setting(db, "budget_limit_usd") == 40.0


@pytest.mark.parametrize("change", [
    {"budget_limit_usd": 0}, {"budget_limit_usd": "ten"}, {"problem_threshold": 0},
    {"problem_threshold": 1.5}, {"effort": "extreme"}, {"retention_days": 0}, {"timezone": "Mars/Base"},
    {"zendesk_subdomain": "not a subdomain!"}, {"zendesk_email": "nobody"}, {"team": ["not-an-email"]},
    {"team": "a@x.com"}, {"budget_limit_usd": True},
])
def test_invalid_values_rejected(db_path, tmp_path, change):
    response = client_for(db_path, tmp_path).put("/api/settings", json=change)
    assert response.status_code == 400
    assert list(response.json()["invalid"]) == list(change)


def test_show_call_qa_round_trips_and_rejects_a_non_boolean(db_path, tmp_path, db):
    client = client_for(db_path, tmp_path)
    assert client.get("/api/settings").json()["show_call_qa"] is True
    saved = client.put("/api/settings", json={"show_call_qa": False})
    assert saved.status_code == 200 and saved.json()["show_call_qa"] is False
    assert get_setting(db, "show_call_qa") is False
    assert client.get("/api/settings").json()["show_call_qa"] is False
    assert client.put("/api/settings", json={"show_call_qa": True}).json()["show_call_qa"] is True
    bad = client.put("/api/settings", json={"show_call_qa": "no"})
    assert bad.status_code == 400
    assert bad.json()["invalid"] == {"show_call_qa": "Show Call QA must be on or off"}
    assert get_setting(db, "show_call_qa") is True


def test_anchor_setting_is_gone(db_path, tmp_path, db):
    """Per-tech schedules replace phase 8's shared 1:1 anchor (spec §3): the setting, its Settings field and the
    anchor's date helpers are gone, and the anchor is refused as an unknown setting."""
    from work_assistant import settings
    from work_assistant.oneonone import cycle

    assert "oneonone_anchor" not in settings.DEFAULTS
    client = client_for(db_path, tmp_path)
    assert "oneonone_anchor" not in client.get("/api/settings").json()
    response = client.put("/api/settings", json={"oneonone_anchor": "2026-11-03"})
    assert response.status_code == 400 and response.json()["unknown_settings"] == ["oneonone_anchor"]
    for name in ("is_prep_day", "prep_days", "history_days", "next_prep_day", "ANCHOR_HISTORY_PERIODS"):
        assert not hasattr(cycle, name), name
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "oneonone_anchor" not in js and "set-oneonone-anchor" not in js


def test_valid_values_saved(db_path, tmp_path, db):
    response = client_for(db_path, tmp_path).put("/api/settings", json={
        "budget_limit_usd": 25, "problem_threshold": 2, "effort": "high", "retention_days": 60,
        "timezone": "America/Chicago", "zendesk_subdomain": "https://Acme.zendesk.com/",
        "zendesk_email": " lead@acme.com "})
    assert response.status_code == 200
    assert get_setting(db, "budget_limit_usd") == 25.0
    assert get_setting(db, "problem_threshold") == 2
    assert get_setting(db, "effort") == "high"
    assert get_setting(db, "retention_days") == 60
    assert get_setting(db, "timezone") == "America/Chicago"
    assert get_setting(db, "zendesk_subdomain") == "acme"
    assert get_setting(db, "zendesk_email") == "lead@acme.com"
    assert response.json()["budget_limit_usd"] == 25.0
    # A blank time zone means the PC's zone again.
    client_for(db_path, tmp_path).put("/api/settings", json={"timezone": None})
    assert get_setting(db, "timezone") is None


def test_runs_include_window_days_and_log_tail(db_path, tmp_path, db):
    set_setting(db, "timezone", "America/Chicago")
    run_id = add_run(db, estimate_usd=1.5, actual_usd=1.25, contact_count=17)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"run-{run_id}.log").write_text("".join(f"line {i}\n" for i in range(60)), encoding="utf-8")
    run = client_for(db_path, tmp_path).get("/api/runs").json()["runs"][0]
    assert run["first_day"] == "2026-10-04" and run["last_day"] == "2026-10-10"
    assert run["log_tail"][0] == "line 10" and run["log_tail"][-1] == "line 59"
    assert len(run["log_tail"]) == 50
    assert (run["estimate_usd"], run["actual_usd"], run["contact_count"]) == (1.5, 1.25, 17)


def test_runs_limited_to_latest_fifty(db_path, tmp_path, db):
    for _ in range(55):
        add_run(db, actual_usd=1.0)
    body = client_for(db_path, tmp_path).get("/api/runs").json()
    assert len(body["runs"]) == 50
    assert body["total_spent_usd"] == 55.0


# -- the daily schedule (Task 20; daily since T-040) ----------------------------------------------------------------------

PLACEHOLDER = {"installed": False, "text": "Not installed yet"}
SCHEDULE_QUERY = """
TaskName:                             \\Work Assistant Daily Review
Next Run Time:                        10/26/2026 6:00:00 AM
Status:                               Ready
Last Run Time:                        10/19/2026 6:00:01 AM
Last Result:                          0
"""


def schedule_client(db_path, tmp_path, runner, demo=False):
    app = create_app(db_path, run_starter=lambda spec: 4242, zendesk_factory=FakeZendesk,
                     log_dir=tmp_path / "logs", startup_work=False, demo=demo, schedule_runner=runner)
    return TestClient(app, base_url=BASE)


def answering(code, stdout="", calls=None):
    def runner(args, **kwargs):
        if calls is not None:
            calls.append(list(args))
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")
    return runner


def test_settings_never_asks_task_scheduler(db_path, tmp_path):
    """Every page reads /api/settings (the demo badge, Rubric, Confirm), so it never waits on schtasks."""
    calls = []
    client = schedule_client(db_path, tmp_path, answering(0, SCHEDULE_QUERY, calls))
    assert "schedule" not in client.get("/api/settings").json()
    assert client.put("/api/settings", json={"problem_threshold": 3}).status_code == 200
    assert calls == []


def test_schedule_without_task_scheduler_is_placeholder(db_path, tmp_path):
    # The conftest guard stands in for a PC without schtasks.
    assert client_for(db_path, tmp_path).get("/api/schedule").json() == PLACEHOLDER


def test_schedule_installed(db_path, tmp_path):
    calls = []
    body = schedule_client(db_path, tmp_path, answering(0, SCHEDULE_QUERY, calls)).get("/api/schedule").json()
    assert body == {"installed": True, "text": "Installed", "next_run": "10/26/2026 6:00:00 AM",
                    "last_run": "10/19/2026 6:00:01 AM", "last_result": "Succeeded"}
    assert calls == [["schtasks", "/Query", "/TN", "Work Assistant Daily Review", "/V", "/FO", "LIST"]]


def test_schedule_not_installed(db_path, tmp_path):
    assert schedule_client(db_path, tmp_path, answering(1)).get("/api/schedule").json() == PLACEHOLDER


@pytest.mark.parametrize("error", [RuntimeError("boom"), subprocess.TimeoutExpired("schtasks", 5),
                                   FileNotFoundError(2, "No such file")])
def test_schedule_failure_gives_placeholder(db_path, tmp_path, error):
    def runner(args, **kwargs):
        raise error

    response = schedule_client(db_path, tmp_path, runner).get("/api/schedule")
    assert response.status_code == 200
    assert response.json() == PLACEHOLDER


def test_schedule_demo_never_asks_task_scheduler(db_path, tmp_path):
    calls = []
    body = schedule_client(db_path, tmp_path, answering(0, SCHEDULE_QUERY, calls), demo=True).get(
        "/api/schedule").json()
    assert body == PLACEHOLDER
    assert calls == []


PANEL_STUBS = """
  var swaps = [];
  var requests = [];
  function flat(list, out) {
    list.forEach(function (c) {
      if (c == null || c === false) { return; }
      if (Array.isArray(c)) { flat(c, out); } else { out.push(c); }
    });
    return out;
  }
  function h(tag, attrs) {
    var node = { tag: tag, attrs: attrs || {}, children: flat(Array.prototype.slice.call(arguments, 2), []) };
    node.replaceWith = function () { swaps.push([node, Array.prototype.slice.call(arguments)]); };
    return node;
  }
  function panel(title, sub) {
    return h("section", { class: "panel" }, h("h4", { text: title }), sub ? h("div", { text: sub }) : null,
      Array.prototype.slice.call(arguments, 2));
  }
  function expand(list) {
    var out = [];
    list.forEach(function (c) {
      var swap = swaps.filter(function (s) { return s[0] === c; })[0];
      out = out.concat(swap ? expand(swap[1]) : [c]);
    });
    return out;
  }
  function text(node) {
    if (typeof node === "string") { return node; }
    return (node.attrs.text || "") + node.children.map(text).join("");
  }
  function api(method, url) { requests.push(method + " " + url); return ANSWER; }
"""


def render_schedule_panel(answer):
    """Run app.js's schedulePanel in node with stand-ins for `h`, `panel` and `api`.

    `answer` is the JS for the promise `api` returns. Returns the panel's rows as text before and after
    that promise settles, and the requests it made.
    """
    from test_web_overview import js_function

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fallback = re.search(r"var SCHEDULE_FALLBACK = [^;]*;", js)
    assert fallback, "the placeholder the panel falls back to is one constant"
    # The panel reads through the page's kept answers (T-143), as a part of the Settings screen whose state is `view`.
    kept = "".join(js_function(js, name) + "\n" for name in ("readPart", "readKept", "getKept", "readJson", "keep"))
    script = (PANEL_STUBS.replace("ANSWER", answer) + fallback.group(0) + "\n" +
              "var view = {}, kept = {}, keptEpoch = 0;\n" + kept +
              js_function(js, "schedulePanel") + "\n" + js_function(js, "scheduleRows") + "\n" +
              js_function(js, "scheduleRow") + "\n" +
              "var p = schedulePanel();\n" +
              "var before = p.children.map(text);\n" +
              "setTimeout(function () { process.stdout.write(JSON.stringify(" +
              "{ before: before, after: expand(p.children).map(text), requests: requests })); }, 0);")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


COMMANDS_ROW = ("To turn it on, run work-assistant schedule install in a terminal. "
                "To turn it off, run work-assistant schedule remove.")
LOG_ROW = "Each scheduled run writes its results to logs/scheduled.log."


def test_schedule_panel_installed_shows_runs_and_commands():
    installed = json.dumps({"installed": True, "text": "Installed", "next_run": "10/26/2026 6:00:00 AM",
                            "last_run": None, "last_result": "Has not run yet"})
    rendered = render_schedule_panel(f"Promise.resolve({installed})")
    assert rendered["requests"] == ["GET /api/schedule"]
    assert rendered["before"][:2] == [
        "Schedule", "Every day at 06:00 it reviews the previous day's calls, and any days it missed. It runs only "
                    "while you are logged on to this PC."]
    assert "Schedule statusChecking…" in rendered["before"]
    rows = rendered["after"]
    assert rows.index("Schedule statusInstalled") < rows.index("Next run10/26/2026 6:00:00 AM") \
        < rows.index("Last runNot yet") < rows.index("Last resultHas not run yet") < rows.index(COMMANDS_ROW)
    assert LOG_ROW in rows and "Schedule statusChecking…" not in rows


def test_page_copy_speaks_of_the_daily_run():
    """The scheduled run is daily (T-040): no page copy points the owner at a weekly run or schedule."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for stale in ("weekly run", "Weekly schedule", "every Monday", "Every Monday"):
        assert stale not in js, stale
    assert "New techs are reviewed from the next daily run." in js
    assert "or wait for the daily run." in js


def test_schedule_panel_not_installed_shows_status_and_commands():
    rows = render_schedule_panel('Promise.resolve({ installed: false, text: "Not installed yet" })')["after"]
    assert "Schedule statusNot installed yet" in rows
    assert not any(row.startswith(("Next run", "Last run", "Last result")) for row in rows)
    assert COMMANDS_ROW in rows and LOG_ROW in rows
    assert "button" not in js_source("schedulePanel"), "the schedule is installed from a terminal, not the browser"


def test_schedule_panel_failed_request_shows_placeholder():
    rows = render_schedule_panel('Promise.reject(new Error("offline"))')["after"]
    assert "Schedule statusNot installed yet" in rows
    assert "Schedule statusChecking…" not in rows
    assert COMMANDS_ROW in rows


def test_schedule_panel_still_loading_says_checking():
    rows = render_schedule_panel("new Promise(function () {})")["after"]
    assert "Schedule statusChecking…" in rows and COMMANDS_ROW in rows


def js_source(name):
    from test_web_overview import js_function

    return js_function((STATIC / "app.js").read_text(encoding="utf-8"), name)


# -- startup work ---------------------------------------------------------------------------------

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def test_startup_fails_dead_fetching_runs(db_path, db):
    dead = add_run(db, status="fetching", created_at="2026-10-17T11:30:00Z", pid=111)
    stale = add_run(db, status="fetching", created_at="2026-10-17T09:30:00Z", pid=222)
    live = add_run(db, status="fetching", created_at="2026-10-17T11:30:00Z", pid=222)
    unknown_pid = add_run(db, status="fetching", created_at="2026-10-17T11:30:00Z", pid=None)
    other = add_run(db, status="submitted", created_at="2026-10-17T09:30:00Z", pid=111)
    failed = pipeline.fail_interrupted_fetching(db, NOW, alive=lambda pid: pid == 222)
    assert failed == [dead, stale, unknown_pid]
    rows = {r["run_id"]: r for r in db.execute("SELECT * FROM runs")}
    for run_id in (dead, stale, unknown_pid):
        assert (rows[run_id]["status"], rows[run_id]["error"]) == ("failed", "interrupted")
        assert rows[run_id]["finished_at"] == "2026-10-17T12:00:00Z"
    assert rows[live]["status"] == "fetching"
    assert rows[other]["status"] == "submitted"


def test_startup_work_skips_resume_without_credentials(db_path, db, tmp_path, monkeypatch):
    add_run(db, status="submitted")
    called = []
    monkeypatch.setattr(pipeline, "resume_runs", lambda *a, **k: called.append(1) or [])
    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=lambda: None, now=NOW)
    assert called == []
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "submitted"


def test_startup_work_does_not_crash_on_errors(db_path, db, tmp_path):
    add_run(db, status="submitted")

    def broken():
        raise RuntimeError("keyring unavailable")

    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=broken, now=NOW)  # no raise


def test_startup_work_purges_marks_and_resumes(db_path, db, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(webapp, "purge_expired", lambda conn, now, days, log_dir: seen.append(("purge", days)) or 0)
    monkeypatch.setattr(pipeline, "resume_runs",
                        lambda conn, **kw: seen.append(("resume", kw["anthropic_client"], kw["secrets"])) or [])
    run_id = add_run(db, status="fetching", created_at="2026-10-17T09:00:00Z", pid=None)
    add_run(db, status="submitted")
    webapp.startup_work(db_path, log_dir=tmp_path / "logs",
                        anthropic_factory=lambda: ("client", ("sk-secret",)), now=NOW)
    assert seen == [("purge", 90), ("resume", "client", ("sk-secret",))]
    assert db.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] == "failed"
    assert not list(db_path.parent.glob("*.lock"))  # lock released


def test_startup_work_skips_resume_when_another_process_holds_the_lock(db_path, db, tmp_path, monkeypatch):
    add_run(db, status="submitted")
    called = []
    monkeypatch.setattr(pipeline, "resume_runs", lambda *a, **k: called.append(1) or [])
    lock = db_path.with_name(db_path.stem + ".resume.lock")
    lock.write_text(str(os.getpid()), encoding="utf-8")  # a live process holds it
    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=lambda: ("c", ()), now=NOW)
    assert called == []
    assert lock.read_text(encoding="utf-8") == str(os.getpid())  # not taken over or removed


def test_resume_lock_takes_over_a_stale_lock(tmp_path):
    lock = tmp_path / "x.resume.lock"
    lock.write_text("123456789", encoding="utf-8")
    with resume_lock(lock, alive=lambda pid: False) as acquired:
        assert acquired
        assert lock.read_text(encoding="utf-8") == str(os.getpid())
    assert not lock.exists()


def test_resume_lock_held_by_live_process(tmp_path):
    lock = tmp_path / "x.resume.lock"
    lock.write_text("4321", encoding="utf-8")
    with resume_lock(lock, alive=lambda pid: pid == 4321) as acquired:
        assert not acquired
    assert lock.read_text(encoding="utf-8") == "4321"


def test_resume_lock_garbage_counts_as_stale(tmp_path):
    lock = tmp_path / "x.resume.lock"
    lock.write_text("not a pid", encoding="utf-8")
    with resume_lock(lock, alive=lambda pid: True) as acquired:
        assert acquired


def test_pid_alive_for_this_process():
    from work_assistant.procutil import pid_alive

    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(2 ** 31 - 2)


def test_startup_work_needs_no_credentials_when_nothing_is_pending(db_path, tmp_path):
    def factory():
        raise AssertionError("no interrupted run, so no Anthropic client is needed")

    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=factory, now=NOW)


def test_cli_args_for_run_and_confirm(tmp_path):
    db_file = tmp_path / "x.db"
    run = webapp.cli_args({"command": "run", "from": "2026-10-04", "to": "2026-10-10", "rereview": True}, db_file)
    # No --yes: the pipeline's budget guard re-checks the run against freshly fetched data.
    assert run[1:] == ["-m", "work_assistant.cli", "--db", str(db_file), "run", "--from", "2026-10-04",
                       "--to", "2026-10-10", "--rereview"]
    assert "--yes" not in webapp.cli_args(
        {"command": "run", "from": "2026-10-04", "to": "2026-10-10", "rereview": False}, db_file)
    confirm = webapp.cli_args({"command": "confirm", "run_id": 7}, db_file)
    assert confirm[-2:] == ["confirm", "7"]


def test_production_starter_refuses_without_credentials(db_path, tmp_path):
    starter = webapp.subprocess_run_starter(db_path, tmp_path / "logs")
    with pytest.raises(Unavailable):
        starter({"command": "run", "from": "2026-10-04", "to": "2026-10-10", "rereview": False})


def test_cli_db_option_selects_the_database(tmp_path, capsys):
    other = tmp_path / "other.db"
    assert cli.main(["--db", str(other), "show", "--rubric"]) == 0
    assert "default rubric" in capsys.readouterr().out.lower()


# -- same-origin API (DNS rebinding and cross-site requests) ---------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "127.0.0.1:9999", "127.0.0.1"])
def test_api_rejects_a_foreign_host(db_path, tmp_path, host):
    client = client_for(db_path, tmp_path)
    assert client.get("/api/settings", headers={"host": host}).status_code == 403
    assert client.get("/api/status", headers={"host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999", "null",
                                    "http://localhost.evil.example:8765"])
def test_changes_from_a_foreign_origin_rejected(db_path, tmp_path, db, origin):
    started = []
    waiting = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    unknown = add_run(db, status="submission_unknown", error=pipeline.SUBMISSION_UNRECORDED)
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 1)
    headers = {"origin": origin}
    assert client.post(f"/api/runs/{waiting}/confirm", headers=headers).status_code == 403
    assert client.post(f"/api/runs/{unknown}/clear", headers=headers).status_code == 403
    assert client.post(f"/api/runs/{waiting}/cancel", headers=headers).status_code == 403
    assert client.post("/api/runs", headers=headers,
                       json={"from": "2026-10-04", "to": "2026-10-10", "rereview": False}).status_code == 403
    assert client.put("/api/settings", headers=headers, json={"budget_limit_usd": 999}).status_code == 403
    assert started == []
    assert db.execute("SELECT status FROM runs WHERE run_id = ?", (unknown,)).fetchone()[0] == "submission_unknown"
    assert db.execute("SELECT status FROM runs WHERE run_id = ?", (waiting,)).fetchone()[0] == "awaiting_confirmation"
    assert get_setting(db, "budget_limit_usd") == 40.0


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765", None])
def test_same_origin_changes_work(db_path, tmp_path, db, origin):
    started = []
    waiting = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    client = client_for(db_path, tmp_path, starter=lambda spec: started.append(spec) or 1)
    headers = {"origin": origin} if origin else {}
    assert client.post(f"/api/runs/{waiting}/confirm", headers=headers).status_code == 202
    assert client.put("/api/settings", headers=headers, json={"budget_limit_usd": 30}).status_code == 200
    assert started == [{"command": "confirm", "run_id": waiting}]
    other = add_run(db, status="awaiting_confirmation", estimate_usd=55.0)
    assert client.post(f"/api/runs/{other}/cancel", headers=headers).status_code == 200


def test_localhost_host_accepted(db_path, tmp_path):
    client = client_for(db_path, tmp_path)
    assert client.get("/api/status", headers={"host": "localhost:8765"}).status_code == 200


def test_allowed_host_follows_the_configured_port(db_path, tmp_path):
    app = create_app(db_path, run_starter=lambda spec: 1, zendesk_factory=FakeZendesk, log_dir=tmp_path / "logs",
                     startup_work=False, port=9001)
    client = TestClient(app, base_url="http://127.0.0.1:9001")
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"host": "127.0.0.1:8765"}).status_code == 403
    assert client.post("/api/runs", headers={"origin": "http://127.0.0.1:8765"},
                       json={"from": "2026-10-04", "to": "2026-10-10"}).status_code == 403


def test_page_shell_is_not_api(db_path, tmp_path):
    client = client_for(db_path, tmp_path)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 200  # the shell carries no data


def test_startup_skips_a_run_another_live_process_is_collecting(db_path, db, tmp_path, monkeypatch):
    add_run(db, status="submitted", pid=424242)
    monkeypatch.setattr(pipeline, "pid_alive", lambda pid: pid == 424242)

    def factory():
        raise AssertionError("the only pending run belongs to a live process; no client is needed")

    webapp.startup_work(db_path, log_dir=tmp_path / "logs", anthropic_factory=factory, now=NOW)
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "submitted"


# -- plain-English run failures -------------------------------------------------------------------

ZENDESK_TEXT = "Zendesk couldn't be reached. Check the internet connection and try Run now again."
ANTHROPIC_TEXT = "Anthropic couldn't be reached. Try again later."


@pytest.mark.parametrize("error, label", [
    ("ZendeskError", ZENDESK_TEXT),
    ("ConnectError", ZENDESK_TEXT),
    ("ConnectTimeout", ZENDESK_TEXT),
    ("ReadTimeout", ZENDESK_TEXT),
    ("HTTPStatusError", ZENDESK_TEXT),
    ("TransportError", ZENDESK_TEXT),
    ("APIConnectionError", ANTHROPIC_TEXT),
    ("APITimeoutError", ANTHROPIC_TEXT),
    ("InternalServerError", ANTHROPIC_TEXT),
    ("interrupted", "The run was interrupted (the PC or app was closed while it was fetching). "
                    "Start it again with Run now."),
    (pipeline.ANTHROPIC_KEY_REJECTED, pipeline.ANTHROPIC_KEY_REJECTED),
    (pipeline.ZENDESK_TOKEN_REJECTED, pipeline.ZENDESK_TOKEN_REJECTED),
    ("KeyError", "The run failed (KeyError). See the run log in Settings."),
    (None, None),
])
def test_failure_label(error, label):
    assert webapp.failure_label(error) == label


def test_runs_carry_the_plain_label_and_the_raw_error(db_path, tmp_path, db):
    add_run(db, status="failed", error="ConnectTimeout")
    run = client_for(db_path, tmp_path).get("/api/runs").json()["runs"][0]
    assert run["error"] == "ConnectTimeout"
    assert run["error_label"] == ZENDESK_TEXT


def test_status_label_for_an_interrupted_run(db_path, tmp_path, db):
    add_run(db, status="failed", error=pipeline.INTERRUPTED)
    last = client_for(db_path, tmp_path).get("/api/status").json()["last_run"]
    assert last["alert"] is True and last["error_label"].startswith("The run was interrupted")


# -- lowering retention erases now ----------------------------------------------------------------


def _old_call(db, ticket_id: int, days_ago: int) -> None:
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', ?, 'sam@example.com', 'Sam', 'normal', 1, "
        "'{\"subject\": \"secret\"}', ?)", (ticket_id, started, started))
    db.commit()


def test_lowering_retention_erases_now(db_path, tmp_path, db):
    _old_call(db, 1, 45)
    _old_call(db, 2, 40)
    _old_call(db, 3, 5)
    response = client_for(db_path, tmp_path).put("/api/settings", json={"retention_days": 30})
    assert response.status_code == 200
    assert response.json()["retention_message"] == "2 older calls were erased."
    purged = {r["ticket_id"]: r["purged_at"] for r in db.execute("SELECT ticket_id, purged_at FROM contacts")}
    assert purged[1] is not None and purged[2] is not None and purged[3] is None
    assert get_setting(db, "retention_days") == 30


def test_lowering_retention_with_nothing_old(db_path, tmp_path, db):
    _old_call(db, 1, 5)
    response = client_for(db_path, tmp_path).put("/api/settings", json={"retention_days": 30})
    assert response.json()["retention_message"] == "No older calls needed erasing."


def test_raising_or_keeping_retention_erases_nothing(db_path, tmp_path, db, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "purge_expired", lambda *a: calls.append(a) or 0)
    api = client_for(db_path, tmp_path)
    assert api.put("/api/settings", json={"retention_days": 120}).json()["retention_message"] is None
    assert api.put("/api/settings", json={"retention_days": 120}).json()["retention_message"] is None
    assert api.put("/api/settings", json={"budget_limit_usd": 5}).json()["retention_message"] is None
    assert calls == []


def test_lowering_retention_purge_failure_is_reported_not_raised(db_path, tmp_path, db, monkeypatch):
    seen = []

    def broken(conn, now, days, log_dir):
        seen.append((days, log_dir))
        raise RuntimeError("database is locked")

    monkeypatch.setattr(webapp, "purge_expired", broken)
    response = client_for(db_path, tmp_path).put("/api/settings", json={"retention_days": 30})
    assert response.status_code == 200
    assert response.json()["retention_message"] == "Couldn't erase older calls now; will retry on the next run."
    assert seen == [(30, tmp_path / "logs")]
    assert get_setting(db, "retention_days") == 30   # the setting itself is saved


def test_settings_page_erases_now_wording():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "will be permanently erased now" in js
    assert "the next time a run finishes or the app starts" not in js


def test_page_polish_wording():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'typeof data.detail === "string"' in js and "Something went wrong with that request." in js
    assert "error_label" in js and "Recorded error: " in js
    assert "(will wait for your OK)" in js
    assert '"Nothing to compare yet"' in js and '"no data for "' not in js
    assert "The reviewer looked at this call again after you overrode this check. Your earlier " in js
    assert "override wasn't carried over. Check the new result, and override it again if you still disagree." in js
    assert "Enter the data retention as a whole number of days, 1 or more." in js
    assert "list.splice(at, 0, item.check)" in js


# -- the design pass (spec 5.5) ---------------------------------------------------------------------------

def test_settings_panel_order():
    """Team activity §9: Settings is app-wide. A cols-2-1 row on top: Team, Time off and the 1:1 schedule (T-092)
    on the left, Sections (the Show Call QA switch), Activity flags and Zendesk and credentials on the right. Under a
    "Call QA" heading, the existing cols-2 row: the left column stacks Run now, Test a model (phase 4), Run history
    and the schedule; the right column stacks Review settings and Reviewer agreement (its own panel). Each column's
    panels are listed in one place, so a later phase can append one."""
    from test_web_overview import js_function

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    render = js_function(js, "drawSettings") + js_function(js, "showCallQaGroup")
    assert '"grid2"' not in render, "Settings no longer uses the old two-up grids"

    columns = {}
    for name in ("team", "flags", "left", "right"):
        found = re.search(r"var " + name + r" = \[([^\]]*)\];", render)
        assert found, "each column's panels are listed in one place: " + name
        columns[name] = [p.strip() for p in found.group(1).split(",")]
    assert columns["team"] == ["teamPanel()", "timeOffPanel()", "prepSchedulePanel()"]
    assert columns["flags"] == ["sectionsPanel(note)", "flagsPanel()", "zendeskPanel()"], \
        "the hosted admin's Techs and Team leads are drawHostedAdminSettings' (test_web_people_ui)"
    assert columns["left"] == ["runNowPanel()", "testPanel()", "historyPanel()", "schedulePanel()"]
    assert columns["right"] == ["reviewPanel()", "agreementPanel()"]

    top = re.search(r'screen\.appendChild\(h\("div", \{ class: "panel-row cols-2-1" \},\s*'
                    r'h\("div", \{ class: "stack" \}, team\),\s*h\("div", \{ class: "stack" \}, flags\)\)\);', render)
    heading = re.search(r'h\("h3", \{ class: "group-head", text: "Call QA" \}\),', render)
    row = re.search(r'h\("div", \{ class: "panel-row cols-2" \},\s*'
                    r'h\("div", \{ class: "stack" \}, left\),\s*h\("div", \{ class: "stack" \}, right\)\)\s*\];', render)
    assert top and heading and row, "the cols-2-1 row, the Call QA heading, then the cols-2 row"
    assert render.count("screen.appendChild(") == 2 and top.start() < heading.start() < row.start(), \
        "the cols-2-1 row, then the Call QA group (heading, cols-2 row) that the switch adds or removes as one"
    assert render.index("var right") < row.start() < render.index("loadRuns()"), \
        "the panels are placed before the run history is filled"

    # The panels keep their current labels, and each column reads in spec order.
    for names, labels in (
            (("teamPanel", "timeOffPanel"), ('panel("Team"', 'panel("Time off"')),
            (("sectionsPanel", "flagsPanel", "zendeskPanel"),
             ('panel("Sections"', 'panel("Activity flags"', '"Zendesk and credentials"')),
            (("runNowPanel", "testPanel", "historyPanel", "schedulePanel"),
             ('"Run now for a date range"', '"Test a model"', '"Run history"', 'panel("Schedule"')),
            (("reviewPanel", "agreementPanel"), ('panel("Reviews"', '"Reviewer agreement"'))):
        source = "".join(js_function(js, name) for name in names)
        position = 0
        for label in labels:
            found = source.find(label, position)
            assert found >= 0, label + " missing or out of order"
            position = found + len(label)


def test_run_history_is_two_line_rows():
    """Run history fits its column with every column and action in view. Each run is its own row group (a
    tbody): line one holds the run, kind, window and status, with the Confirm… or Clear… action under the
    status; line two holds the calls, estimate, actual cost and start time; the existing log row (the one
    <details>) follows. A failed run is tinted, and its error shown, in the failure color."""
    from css_rules import app_css, has_element, media_block, top_level, values
    from test_web_overview import js_function

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    history = js_function(js, "historyPanel")
    assert "view.historyTable = h(" in history and has_element(history, "table", "atable", "two-line", "runs")
    for line, labels in (("line1", r'"Run",\s*"Kind",\s*"Window",\s*"Status"'),
                         ("line2", r'"Calls",\s*"Estimate",\s*"Actual",\s*"Started"')):
        assert re.search(r'h\("tr",\s*\{\s*class:\s*"' + line + r'"\s*\},\s*\[' + labels + r"\]", history), line
    load = js_function(js, "loadRuns")
    assert 'view.historyTable.querySelectorAll("tbody")' in load and "appendRun(view.historyTable, run)" in load, \
        "a reload replaces the runs' tbodies and keeps the header"

    run = js_function(js, "appendRun")
    assert has_element(run, "tbody", "row-group") and has_element(run, "tbody", "flagged"), \
        "each run is one row group, and a failed one is flagged"
    line1 = run.index('h("tr", { class: "line1" }')
    line2 = run.index('h("tr", { class: "line2" }')
    log = run.index('h("tr", { class: "logrow" }')
    assert line1 < line2 < log
    first = run[line1:line2]
    for cell in ("run.run_id", "kindLabel(run)", "run-window", "run-state", '"run-status "',
                 "run.status }), action)"):
        assert cell in first, cell + " belongs on line one"
    second = run[line2:log]
    for cell in ("run.contact_count", "money(run.estimate_usd)", "money(run.actual_usd)", "timeLabel(run.created_at)"):
        assert cell in second, cell + " belongs on line two"
    assert re.search(r'h\("td", \{ colspan: 3 \}', run[log:]), "the log spans the row under the run number"
    assert run.count('h("details"') == 1 and "table.appendChild(rows)" in run
    assert has_element(run[log:], "div", "flag", "failure"), "a failed run's error is in the failure color"

    css = app_css()
    wide = top_level(css)
    assert values(wide, ".atable.runs tr.line1 td.run-window", "white-space") == ["normal"], "the window may wrap"
    assert values(wide, ".atable.runs td.run-state button", "display") == ["block"], "the action sits under the status"
    assert values(wide, ".atable.runs tbody.flagged td", "background") == ["var(--failure-tint)"], \
        "a failed run is tinted across all its lines, in the failure color"
    assert values(wide, ".flag.failure", "color") == ["var(--failure)"]
    assert values(media_block(app_css(), "(hover: hover)"), ".atable tbody.row-group:not(.add-row):hover tr:not(.edit-row) td", "background") == ["var(--hover)"], \
        "a run's lines highlight together"
    narrow = media_block(css, "(max-width: 640px)")
    assert values(narrow, ".atable.runs .run-status", "white-space") == ["normal"], \
        "the table fits the narrow panel too"


# -- the reviewing model (Task 4, T-034) --------------------------------------------------------------

OPUS, SONNET, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"


def _measured_reviews(db, model, costs, effort="medium"):
    """Finished reviews by `model` at `effort`, each with its output tokens and cost recorded."""
    run_id = add_run(db)
    for i, cost in enumerate(costs):
        ticket_id = run_id * 1000 + i
        db.execute(
            "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, attribution_ok, "
            "snapshot, fetched_at) VALUES (?, 'voice', '2026-10-05T10:00:00Z', 'a@x.com', 'A', 1, '{}', 'x')",
            (ticket_id,))
        db.execute(
            "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at, model, "
            "effort, output_tokens, cost_usd) VALUES (?, ?, 1, 'ok', 0, 'x', ?, ?, 3000, ?)",
            (run_id, ticket_id, model, effort, cost))
    db.commit()


def test_settings_lists_models_with_approx_cost(db_path, tmp_path, db):
    api = client_for(db_path, tmp_path)
    body = api.get("/api/settings").json()
    assert body["model"] == OPUS
    assert body["models"] == [
        {"id": OPUS, "label": "Claude Opus 5.5", "approx_cost_per_call": 0.07, "measured": False},
        {"id": SONNET, "label": "Claude Sonnet 5.5", "approx_cost_per_call": 0.03, "measured": False},
        {"id": FABLE, "label": "Claude Fable 5.1", "approx_cost_per_call": 0.17, "measured": False},
    ]
    _measured_reviews(db, SONNET, [0.02] * 5 + [0.06] * 5)
    sonnet = api.get("/api/settings").json()["models"][1]
    assert sonnet == {"id": SONNET, "label": "Claude Sonnet 5.5", "approx_cost_per_call": 0.04, "measured": True}
    # Measured at the effort that is selected now: at max, Sonnet has nothing measured yet.
    set_setting(db, "effort", "max")
    assert api.get("/api/settings").json()["models"][1]["measured"] is False


def test_put_model_setting(db_path, tmp_path, db):
    api = client_for(db_path, tmp_path)
    response = api.put("/api/settings", json={"model": FABLE})
    assert response.status_code == 200
    assert response.json()["model"] == FABLE and get_setting(db, "model") == FABLE
    response = api.put("/api/settings", json={"model": "claude-haiku-5-5"})
    assert response.status_code == 400
    assert response.json()["invalid"] == {
        "model": "Model must be one of: Claude Opus 5.5, Claude Sonnet 5.5, Claude Fable 5.1."}
    assert get_setting(db, "model") == FABLE


SONNET_NOTE = "Sonnet's effort levels are calibrated differently; its own default is high."

REVIEW_PANEL_JS = """
function byId(root, id) {
  return root.findAll(function (e) { return e.getAttribute("id") === id; })[0];
}
function row(root, id) {
  return root.findAll(function (e) {
    return e.className === "form-row" && e.children.some(function (c) { return c.getAttribute("id") === id; });
  })[0];
}
function hints(r) {
  return r.children.filter(function (c) { return c.className === "hint"; })
    .map(function (c) { return { text: c.textContent, hidden: Boolean(c.hidden) }; });
}
function state(p) {
  return { model: hints(row(p, "set-model")), effort: hints(row(p, "set-effort")) };
}
var view = { settings: SETTINGS };
var p = reviewPanel();
var select = byId(p, "set-model");
var out = {
  options: select.children.map(function (o) {
    return [o.getAttribute("value"), o.textContent, o.getAttribute("selected")];
  }),
  first: state(p)
};
select.value = "claude-sonnet-5-5";
select.listeners.change.forEach(function (f) { f(); });
out.sonnet = state(p);
select.value = "claude-fable-5-1";
select.listeners.change.forEach(function (f) { f(); });
out.fable = state(p);
// Save: the inputs get the values the browser would report, then the Save button is clicked.
p.findAll(function (e) { return e.tagName === "INPUT"; }).forEach(function (e) {
  e.value = e.getAttribute("value") || "";
});
var saved = [];
function saveSettings(changes) {
  saved.push(changes);
  return Promise.resolve(Object.assign({}, SETTINGS, changes));
}
function setMessage() {}
p.findAll(function (e) { return e.tagName === "BUTTON" && e.textContent === "Save"; })[0]
  .listeners.click.forEach(function (f) { f(); });
setTimeout(function () {
  out.saved = saved.map(function (c) { return c.model; });
  out.afterSave = state(p);
  process.stdout.write(JSON.stringify(out));
}, 0);
"""


def run_review_panel(settings_body: dict) -> dict:
    """Build app.js's Reviews panel on the fake DOM for `settings_body`, then pick Sonnet and Fable."""
    from test_web_overview import js_function, js_var, node_prelude, run_node

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    source = "\n".join([js_var(js, v) for v in ("SONNET_MODEL", "SONNET_EFFORT_NOTE")] + [
        js_function(js, name) for name in ("h", "append", "panel", "timeZones", "costPerCallText", "reviewPanel")])
    return run_node(node_prelude() + source + f"\nvar SETTINGS = {json.dumps(settings_body)};\n" + REVIEW_PANEL_JS)


def test_review_panel_model_select_cost_and_sonnet_note(db_path, tmp_path, db):
    """The Model select lists the three models with the saved one selected, and the line under it gives the
    selected model's approximate cost per call. The Sonnet effort note shows only while Sonnet is selected."""
    _measured_reviews(db, FABLE, [0.2] * 10)
    body = client_for(db_path, tmp_path).get("/api/settings").json()
    shown = run_review_panel(body)
    assert shown["options"] == [[OPUS, "Claude Opus 5.5", "selected"], [SONNET, "Claude Sonnet 5.5", None],
                                [FABLE, "Claude Fable 5.1", None]]
    assert shown["first"]["model"] == [{"text": "about $0.07 per call (estimate)", "hidden": False}]
    assert {"text": SONNET_NOTE, "hidden": True} in shown["first"]["effort"]
    assert shown["sonnet"]["model"] == [{"text": "about $0.03 per call (estimate)", "hidden": False}]
    assert {"text": SONNET_NOTE, "hidden": False} in shown["sonnet"]["effort"]
    assert shown["fable"]["model"] == [{"text": "about $0.20 per call (measured)", "hidden": False}]
    assert {"text": SONNET_NOTE, "hidden": True} in shown["fable"]["effort"]
    assert shown["saved"] == [FABLE], "Save sends the selected model"
    assert shown["afterSave"]["model"] == [{"text": "about $0.20 per call (measured)", "hidden": False}]

    set_setting(db, "model", SONNET)
    body = client_for(db_path, tmp_path).get("/api/settings").json()
    shown = run_review_panel(body)
    assert shown["options"][1][2] == "selected"
    assert {"text": SONNET_NOTE, "hidden": False} in shown["first"]["effort"]



# -- versioned static addresses (stale app.js after an update) -----------------------------------------------------


def _version(name: str) -> str:
    import hashlib
    from work_assistant.web.app import STATIC_DIR
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


def test_index_asks_for_each_static_file_by_its_contents(db_path, tmp_path):
    response = client_for(db_path, tmp_path).get("/")
    assert response.headers["Cache-Control"] == "no-store"
    for name, attribute in (("app.css", "href"), ("charts.js", "src"), ("app.js", "src")):
        assert f'{attribute}="/static/{name}?v={_version(name)}"' in response.text, name
    assert 'src="/static/app.js"' not in response.text and 'href="/static/app.css"' not in response.text
    assert client_for(db_path, tmp_path).get(f"/static/app.js?v={_version('app.js')}").status_code == 200


def test_a_changed_file_gets_a_new_address(tmp_path):
    from work_assistant.web.app import versioned_page
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="/static/app.css">'
                                         '<script src="/static/app.js"></script>', encoding="utf-8")
    (tmp_path / "app.css").write_text("a{}", encoding="utf-8")
    (tmp_path / "app.js").write_text("var a = 1;", encoding="utf-8")
    before = versioned_page(tmp_path)
    (tmp_path / "app.js").write_text("var a = 2;", encoding="utf-8")
    after = versioned_page(tmp_path)
    css = re.search(r'app\.css\?v=(\w+)', before).group(1)
    assert re.search(r'app\.css\?v=(\w+)', after).group(1) == css, "an unchanged file keeps its address"
    assert re.search(r'app\.js\?v=(\w+)', before).group(1) != re.search(r'app\.js\?v=(\w+)', after).group(1)
