"""Sorting the Tickets list: the server orders the whole filtered list (so paging and the export follow it),
and the page's Sort choice sits beside Missed check without becoming a filter."""
from __future__ import annotations

from urllib.parse import urlencode

from test_queries import all_met, missing, version  # noqa: F401  (fixture)
from test_web_review_again import BUTTONS_JS, calls, client, day, page_for  # noqa: F401  (fixture)
from test_web_test_view import run_page
from test_web_tickets import add_call, make_call


def ids(response):
    return [r["ticket_id"] for r in response.json()["rows"]]


def test_server_sorts_the_whole_list(calls, tmp_path):
    """101 (problem, 95) at 10:00, 102 (good, 100) at 10:01, 103 (review failed, no score) at 10:02."""
    api = client(tmp_path)
    assert ids(api.get("/api/tickets")) == [103, 102, 101]                    # newest first, the default
    assert ids(api.get("/api/tickets?sort=oldest")) == [101, 102, 103]
    assert ids(api.get("/api/tickets?sort=score_low")) == [101, 102, 103]     # no score goes last
    assert ids(api.get("/api/tickets?sort=score_high")) == [102, 101, 103]


def test_agent_sort_is_by_name_then_newest(conn, version, tmp_path):
    zed, amy = ("zed@example.com", "Zed Young"), ("amy@example.com", "Amy Bell")
    add_call(conn, version, make_call(201, agent=zed), all_met(version))
    add_call(conn, version, make_call(202, agent=amy, start=day(1)), all_met(version))
    add_call(conn, version, make_call(203, agent=zed, start=day(2)), all_met(version))
    assert ids(client(tmp_path).get("/api/tickets?sort=agent")) == [202, 203, 201]


def test_bad_sort_is_refused(calls, tmp_path):
    response = client(tmp_path).get("/api/tickets?sort=sideways")
    assert response.status_code == 400
    assert "sort must be one of" in response.json()["detail"]


def test_export_follows_the_sort(calls, tmp_path, monkeypatch):
    from work_assistant.web import export

    seen = []
    monkeypatch.setattr(export, "write_xlsx", lambda rows, buffer, subdomain: seen.append([r["ticket_id"] for r in rows]))
    client(tmp_path).get("/api/export.xlsx?sort=oldest")
    assert seen == [[101, 102, 103]]


def test_sort_sits_beside_missed_check_and_is_not_a_filter(calls, tmp_path):
    hash_query = {"agent": "sam@example.com", "sort": "score_low"}   # the order the page asks for them
    responses = page_for(tmp_path, hash_query)
    out = run_page("#/tickets?" + urlencode(hash_query), responses, BUTTONS_JS + """
run(async function () {
  var missed = document.getElementById("tk-missed"), sort = document.getElementById("tk-sort");
  var missedLabel = missed.parentNode, sortLabel = sort.parentNode;
  var row = missedLabel.parentNode;
  var siblings = Array.prototype.slice.call(row.children);
  var sub = all(screenEl, function (e) { return e.classList.contains("sub"); })[0].textContent;
  var exportLink = all(screenEl, function (e) { return e.classList.contains("export"); })[0].getAttribute("href");
  var review = buttonIn(screenEl, "Review th");
  if (review) { fire(review, "click"); await settle(); }
  var posts = requests.filter(function (r) { return r.method === "POST"; }).map(function (r) { return r.body; });
  return { nextToMissed: siblings.indexOf(sortLabel) === siblings.indexOf(missedLabel) + 1,
           sortValue: sort.value, sub: sub, exportLink: exportLink, posts: posts };
});
""")
    assert out["nextToMissed"] and out["sortValue"] == "score_low"
    assert out["sub"].startswith("Lowest score first.")
    assert "sort=score_low" in out["exportLink"]
    for body in out["posts"]:
        assert "sort" not in body["filters"], "Review these N again sends filters only"
