"""Review again (T-045): the estimate and start endpoints, the run starter's ticket file, and the two buttons on
the Tickets screen (whole page in node on the fake DOM). Fakes only: nothing calls Zendesk or Anthropic, and no
run is ever started for real."""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from test_queries import SAM, all_met, missing, version  # noqa: F401  (fixture)
from test_web_test_view import answer, run_page
from test_web_tickets import add_call, make_call
from work_assistant import credentials
from work_assistant.settings import set_setting
from work_assistant.web import app as webapp
from work_assistant.web.app import create_app
from work_assistant.zendesk.client import ZendeskNotFound

BASE = "http://127.0.0.1:8765"
SAME_ORIGIN = {"origin": BASE}
ESTIMATE = "/api/review-again/estimate"
START = "/api/review-again"


class FakeZendesk:
    """A read-only Zendesk stand-in that knows no tickets, so every call is reviewed from its stored data.
    It records the tickets asked for."""

    def __init__(self):
        self.asked: list[int] = []
        self.closed = False

    def users_by_email(self, emails):
        return {}

    def ticket_fields(self):
        return {}

    def ticket(self, ticket_id):
        self.asked.append(ticket_id)
        raise ZendeskNotFound("HTTP 404")

    def close(self):
        self.closed = True


def client(tmp_path, started=None, zendesk=None):
    started = started if started is not None else []
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: started.append(spec) or 4242,
                     zendesk_factory=lambda: zendesk or FakeZendesk(), log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(app, base_url=BASE)


def day(n: int) -> str:
    """A call start on Oct 5, n minutes after 15:00 UTC (10:00 in Chicago)."""
    return f"2026-10-05T{15 + n // 60:02d}:{n % 60:02d}:00Z"


@pytest.fixture
def calls(conn, version):
    """Call 101 a problem, 102 good, 103 a review that failed."""
    add_call(conn, version, make_call(101), missing(version, "sc06"))
    add_call(conn, version, make_call(102, start=day(1)), all_met(version))
    add_call(conn, version, make_call(103, start=day(2)), {"pr-call-length": "met"}, status="error",
             error="request errored: overloaded_error")
    return conn


# -- the endpoints ----------------------------------------------------------------------------------------


def test_estimate_for_one_call(calls, tmp_path):
    zendesk = FakeZendesk()
    response = client(tmp_path, zendesk=zendesk).post(ESTIMATE, json={"ticket_id": 103}, headers=SAME_ORIGIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["calls"], body["contacts"], body["excluded_purged"], body["excluded_held"]) == (1, 1, 0, 0)
    assert body["estimate_usd"] > 0 and body["budget_limit_usd"] == 40.0
    assert zendesk.asked == [103] and zendesk.closed, "only that call is read, and the client is closed"


def test_start_for_one_call(calls, tmp_path):
    started = []
    response = client(tmp_path, started).post(START, json={"ticket_id": 101}, headers=SAME_ORIGIN)
    assert response.status_code == 202
    assert response.json() == {"started": True, "pid": 4242, "calls": 1}
    assert started == [{"command": "rereview-tickets", "ticket_ids": [101]}]


def test_filters_resolve_to_every_matching_call(calls, tmp_path):
    started = []
    api = client(tmp_path, started)
    response = api.post(START, json={"filters": {"verdict": "review_failed"}}, headers=SAME_ORIGIN)
    assert response.status_code == 202 and started[-1]["ticket_ids"] == [103]
    api.post(START, json={"filters": {"verdict": None, "q": "", "agent": SAM[0].upper()}}, headers=SAME_ORIGIN)
    assert started[-1]["ticket_ids"] == [101, 102, 103], "the same calls the list shows"


def test_filters_reach_beyond_the_200_shown(conn, version, tmp_path):
    for n in range(205):
        add_call(conn, version, make_call(1000 + n, start=day(n)), all_met(version))
    started = []
    api = client(tmp_path, started)
    assert len(api.get("/api/tickets").json()["rows"]) == 205
    estimate = api.post(ESTIMATE, json={"filters": {}}, headers=SAME_ORIGIN).json()
    assert (estimate["calls"], estimate["contacts"]) == (205, 205)
    assert api.post(START, json={"filters": {}}, headers=SAME_ORIGIN).status_code == 202
    assert started[-1]["ticket_ids"] == sorted(1000 + n for n in range(205))


@pytest.mark.parametrize("body", [
    ["101"], {}, {"ticket_id": 101, "filters": {}}, {"ticket_id": "101"}, {"ticket_id": True}, {"ticket_id": 0},
    {"filters": "verdict=problem"}, {"filters": {"color": "red"}}, {"filters": {"verdict": "awful"}},
    {"filters": {"start": "2026-13-01"}}, {"filters": {"q": 5}},
])
def test_requests_are_validated(calls, tmp_path, body):
    started = []
    api = client(tmp_path, started)
    for path in (ESTIMATE, START):
        response = api.post(path, json=body, headers=SAME_ORIGIN)
        assert response.status_code == 400, (path, body, response.text)
        assert isinstance(response.json()["detail"], str)
    assert started == []


def test_unknown_call_and_empty_filters(calls, tmp_path):
    api = client(tmp_path)
    response = api.post(START, json={"ticket_id": 4040}, headers=SAME_ORIGIN)
    assert response.status_code == 404 and "4040" in response.json()["detail"]
    response = api.post(ESTIMATE, json={"filters": {"q": "nothing like this"}}, headers=SAME_ORIGIN)
    assert response.status_code == 400 and response.json()["detail"] == "No calls match these filters."


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999"])
def test_foreign_origin_rejected(calls, tmp_path, origin):
    started = []
    api = client(tmp_path, started)
    for path in (ESTIMATE, START):
        assert api.post(path, json={"ticket_id": 101}, headers={"origin": origin}).status_code == 403
    assert started == []


def test_demo_refuses(calls, tmp_path):
    from work_assistant.cli import _demo_unavailable

    app = create_app(tmp_path / "work-assistant.db", run_starter=_demo_unavailable, zendesk_factory=_demo_unavailable,
                     log_dir=tmp_path / "logs", startup_work=False, demo=True)
    api = TestClient(app, base_url=BASE)
    for path in (ESTIMATE, START):
        response = api.post(path, json={"ticket_id": 101}, headers=SAME_ORIGIN)
        assert response.status_code == 503 and "Not available in the demo" in response.json()["detail"]


# -- the run starter ------------------------------------------------------------------------------------


def test_cli_args_for_review_again(tmp_path):
    args = webapp.cli_args({"command": "rereview-tickets", "ticket_ids": [5, 7], "tickets_file": "logs/x.txt"},
                           tmp_path / "x.db")
    assert args[5:] == ["run", "--rereview", "--tickets-file", "logs/x.txt"]
    assert "--yes" not in args, "the run goes through the budget guard"


@pytest.fixture
def ready(conn, version):
    set_setting(conn, "zendesk_subdomain", "acme")
    set_setting(conn, "zendesk_email", "owner@example.com")
    credentials.set_secret("zendesk_token", "zd-test")
    credentials.set_secret("anthropic_api_key", "sk-test")


def test_starter_passes_the_ids_in_a_file(ready, tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(webapp.subprocess, "Popen",
                        lambda args, **kwargs: launched.append(args) or SimpleNamespace(pid=77))
    logs = tmp_path / "logs"
    starter = webapp.subprocess_run_starter(tmp_path / "work-assistant.db", logs)
    ids = list(range(100000, 106000))     # far past what a Windows command line holds
    assert starter({"command": "rereview-tickets", "ticket_ids": ids}) == 77
    [args] = launched
    assert args[0] == sys.executable and "--tickets-file" in args
    path = args[args.index("--tickets-file") + 1]
    assert path.startswith(str(logs)) and len(" ".join(args)) < 2000
    with open(path, encoding="utf-8") as f:
        assert [int(line) for line in f.read().split()] == ids


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation flags")
def test_starter_opens_no_console_window(ready, tmp_path, monkeypatch):
    """The venv's python.exe is a redirector that starts the real Python as a child. Started detached (no
    console), that child got a fresh, visible console window. CREATE_NO_WINDOW gives the run a hidden
    console instead, which the child inherits; the new process group keeps the run independent of the app."""
    flags = []
    monkeypatch.setattr(webapp.subprocess, "Popen",
                        lambda args, **kwargs: flags.append(kwargs["creationflags"]) or SimpleNamespace(pid=78))
    webapp.subprocess_run_starter(tmp_path / "work-assistant.db", tmp_path / "logs")({"command": "confirm", "run_id": 3})
    [value] = flags
    assert value & subprocess.CREATE_NO_WINDOW
    assert value & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not value & subprocess.DETACHED_PROCESS


def test_starter_removes_the_file_when_the_launch_fails(ready, tmp_path, monkeypatch):
    def fail(args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(webapp.subprocess, "Popen", fail)
    logs = tmp_path / "logs"
    with pytest.raises(OSError):
        webapp.subprocess_run_starter(tmp_path / "work-assistant.db", logs)({"command": "rereview-tickets",
                                                                         "ticket_ids": [1, 2]})
    assert not list(logs.glob("review-again-*"))


# -- the buttons ----------------------------------------------------------------------------------------

# The Tickets screen, driven in node: the list button and the detail button, each with its estimate box.
BUTTONS_JS = """
function texts(el) { return el ? [el.textContent] : []; }
function estimateBoxes() {
  return all(screenEl, function (e) { return e.classList.contains("estimate"); });
}
function shownBox(box) {
  for (var node = box; node && node.tagName; node = node.parentNode) { if (node.hidden) { return false; } }
  return true;
}
function startButtons() { return all(screenEl, function (e) { return e.tagName === "BUTTON" && e.textContent.indexOf("Start") === 0; }); }
"""


def page_for(tmp_path, filters=None, ticket=None, estimate=None):
    """The real endpoint answers for the Tickets screen with `filters`, plus stand-ins for Review again."""
    api = client(tmp_path)
    query = "?" + urlencode(filters) if filters else ""
    paths = ["/api/settings", "/api/status", "/api/tickets" + query]
    if ticket is not None:
        paths.append(f"/api/tickets/{ticket}")
    responses = {"GET " + p: answer(api.get(p).json(), api.get(p).status_code) for p in paths}
    responses["POST " + ESTIMATE] = answer(estimate or {"estimate_usd": 0.42, "contacts": 3, "calls": 3,
                                                        "excluded_purged": 0, "excluded_held": 0,
                                                        "budget_limit_usd": 5.0})
    responses["POST " + START] = answer({"started": True, "pid": 1, "calls": 3}, 202)
    return responses


def test_list_button_counts_the_filtered_calls_and_estimates_before_start(calls, tmp_path):
    responses = page_for(tmp_path, {"agent": SAM[0]})
    out = run_page("#/tickets?" + urlencode({"agent": SAM[0]}), responses, BUTTONS_JS + """
run(async function () {
  var out = {};
  var button = buttonIn(screenEl, "Review these");
  out.label = button.textContent;
  out.startsBefore = startButtons().length;
  fire(button, "click");
  await settle();
  out.requests = requests.filter(function (r) { return r.method === "POST"; });
  out.box = estimateBoxes().filter(shownBox).map(function (b) { return b.textContent; });
  var start = startButtons()[0];
  out.start = start.textContent;
  fire(start, "click");
  await settle();
  out.after = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  out.messages = all(screenEl, function (e) { return e.classList.contains("message"); })
    .map(function (m) { return m.textContent; }).filter(Boolean);
  return out;
});
""")
    assert out["label"] == "Review these 3 again"
    assert out["startsBefore"] == 0, "nothing can start before the estimate is shown"
    assert out["requests"] == [{"method": "POST", "url": ESTIMATE, "body": {"filters": {
        "start": None, "end": None, "agent": SAM[0], "verdict": None, "missed_check": None, "priority": None,
        "q": None}}}]
    [box] = out["box"]
    assert "3 calls to review again, estimated $0.42" in box
    assert out["start"] == "Start review again"
    assert out["after"][-1] == [START, {"filters": {"start": None, "end": None, "agent": SAM[0], "verdict": None,
                                                    "missed_check": None, "priority": None, "q": None}}]
    assert any(m.startswith("Review again started.") for m in out["messages"])


def test_list_button_says_one_call_and_hides_when_the_list_is_empty(calls, tmp_path):
    one = run_page("#/tickets?verdict=review_failed", page_for(tmp_path, {"verdict": "review_failed"}), BUTTONS_JS + """
run(async function () { var b = buttonIn(screenEl, "Review"); return b ? b.textContent : null; });
""")
    assert one == "Review this call again"
    none = run_page("#/tickets?q=nothing", page_for(tmp_path, {"q": "nothing"}), BUTTONS_JS + """
run(async function () {
  return all(screenEl, function (e) { return e.tagName === "BUTTON" && e.textContent.indexOf("Review") === 0; }).length;
});
""")
    assert none == 0


def test_buttons_sit_at_the_right_of_their_headers(calls, tmp_path):
    """Owner's request: the list button at the right of the list's title line, and Review again under
    the score on the right of the call header."""
    responses = page_for(tmp_path, ticket=101)
    out = run_page("#/tickets?ticket=101", responses, BUTTONS_JS + """
run(async function () {
  function holder(button, cls) {
    for (var node = button; node && node.tagName; node = node.parentNode) {
      if (node.classList && node.classList.contains(cls)) { return node; }
    }
    return null;
  }
  var list = buttonIn(screenEl, "Review these");
  var detail = all(screenEl, function (e) { return e.classList.contains("detail-box"); })[0];
  var again = buttonIn(detail, "Review again");
  var side = holder(again, "dethead-side");
  return {
    listRow: !!holder(list, "head-row"),
    listRowText: holder(list, "head-row").children[0].children[0].textContent,
    subUnderTitle: holder(list, "head-row").children[0].children[1].textContent,
    detailSide: !!side,
    scoreFirst: side.children[0].classList.contains("score")
  };
});
""")
    assert out["listRow"] and out["listRowText"].lower().endswith("calls")
    assert out["subUnderTitle"].startswith("Newest first."), "the description sits right under the title"
    assert out["detailSide"] and out["scoreFirst"]


def test_detail_button_estimates_before_start_and_over_limit_says_it_waits(calls, tmp_path):
    estimate = {"estimate_usd": 6.5, "contacts": 1, "calls": 1, "excluded_purged": 0, "excluded_held": 0,
                "budget_limit_usd": 5.0}
    responses = page_for(tmp_path, ticket=103, estimate=estimate)
    out = run_page("#/tickets?ticket=103", responses, BUTTONS_JS + """
run(async function () {
  var out = {};
  var detail = all(screenEl, function (e) { return e.classList.contains("detail-box"); })[0];
  var button = buttonIn(detail, "Review again");
  out.label = button.textContent;
  out.startsBefore = startButtons().length;
  fire(button, "click");
  await settle();
  out.estimate = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  out.box = all(detail, function (e) { return e.classList.contains("estimate"); }).filter(shownBox)
    .map(function (b) { return b.textContent; });
  var start = startButtons()[0];
  out.start = start.textContent;
  fire(start, "click");
  await settle();
  out.started = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return [r.url, r.body]; });
  return out;
});
""")
    assert out["label"] == "Review again"
    assert out["startsBefore"] == 0
    assert out["estimate"] == [[ESTIMATE, {"ticket_id": 103}]]
    [box] = out["box"]
    assert "1 call to review again, estimated $6.50" in box and "over your $5.00 budget limit" in box
    assert out["start"] == "Start review again (will wait for your OK)"
    assert out["started"][-1] == [START, {"ticket_id": 103}]


def test_estimate_counts_left_out_calls(calls, tmp_path):
    estimate = {"estimate_usd": 0.2, "contacts": 1, "calls": 3, "excluded_purged": 1, "excluded_held": 1,
                "budget_limit_usd": 5.0}
    out = run_page("#/tickets", page_for(tmp_path, estimate=estimate), BUTTONS_JS + """
run(async function () {
  fire(buttonIn(screenEl, "Review these"), "click");
  await settle();
  return estimateBoxes().filter(shownBox).map(function (b) { return b.textContent; });
});
""")
    [box] = out
    assert "1 call older than the retention period can't be reviewed again: its content was erased." in box
    assert "1 call is held by a run still in progress, so it is left out." in box


def test_buttons_use_text_and_classes_only():
    from test_web_tickets import app_js
    from test_web_overview import js_function

    js = app_js()
    for name in ("reviewAgainButton", "showReviewAgainEstimate"):
        source = js_function(js, name)
        assert "innerHTML" not in source and "style" not in source, name
    assert json.dumps("Review again") in js
