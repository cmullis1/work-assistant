"""The model comparison report (`web.compare`, spec §5.3): a model test's reviews against the shown reviews
they are paired with. Synthetic data and fakes only; no Anthropic or Zendesk.

Review Focus 4 is `test_comparison_survives_rereview_and_purge`.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from factories import add_run
from test_pipeline import FakeBatches, client_for, make_contact
from test_queries import LEE, SAM, add_review, all_met, missing, version  # noqa: F401  (fixture)
from work_assistant import pipeline, rubric
from work_assistant.retention import purge_expired
from work_assistant.settings import set_setting
from work_assistant.web import compare
from work_assistant.web.app import create_app

UTC = timezone.utc
FABLE, SONNET = "claude-fable-5-1", "claude-sonnet-5-5"
WINDOW = (datetime(2026, 10, 4, 5, tzinfo=UTC), datetime(2026, 10, 11, 5, tzinfo=UTC))
ERASED = "Erased after the retention period."


def add_shown(conn, version, ticket_id, outcomes, *, cost=None, resolved="yes", dropped="no", **kwargs) -> int:
    """A call's shown review (see `test_queries.add_review`) with its cost and judgments."""
    review_id = add_review(conn, version, ticket_id, outcomes, **kwargs)
    conn.execute("UPDATE reviews SET cost_usd = ?, resolved_on_call = ?, call_dropped = ? WHERE review_id = ?",
                 (cost, resolved, dropped, review_id))
    conn.commit()
    return review_id


def add_test_run(conn, *, model=FABLE, effort="high", created_at="2026-10-13T00:00:00Z", actual_usd=0.5,
             window=("2026-10-04T05:00:00Z", "2026-10-11T05:00:00Z"), status="complete") -> int:
    run_id = add_run(conn, kind="test", status=status, actual_usd=actual_usd, created_at=created_at, window=window)
    conn.execute("UPDATE runs SET model = ?, effort = ? WHERE run_id = ?", (model, effort, run_id))
    conn.commit()
    return run_id


def add_tested(conn, test_run_id, shown_id, outcomes, *, status="ok", counted=True, cost=0.1, resolved="yes",
               dropped="no", version_id=None, model=FABLE) -> int:
    """The test's review of the call whose shown review is `shown_id`, paired with it as `start_test` does."""
    shown = conn.execute("SELECT ticket_id, rubric_version_id FROM reviews WHERE review_id = ?",
                         (shown_id,)).fetchone()
    conn.execute("INSERT INTO run_contacts (run_id, ticket_id, cost_usd, compared_review_id) VALUES (?, ?, ?, ?)",
                 (test_run_id, shown["ticket_id"], cost or 0, shown_id))
    review_id = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, error, resolved_on_call, counted, "
        "not_counted_reason, is_current, cost_usd, created_at, compared_review_id, model, effort, call_dropped) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '2026-10-13T00:00:00Z', ?, ?, 'high', ?)",
        (test_run_id, shown["ticket_id"], version_id or shown["rubric_version_id"], status,
         None if status == "ok" else "refused: policy", resolved if status == "ok" else None, int(counted),
         None if counted else "Wrong number", cost, shown_id, model, dropped if status == "ok" else None)).lastrowid
    conn.executemany(
        "INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by) "
        "VALUES (?, ?, ?, 'test evidence', 'model')", [(review_id, cid, out) for cid, out in outcomes.items()])
    conn.commit()
    return review_id


def api_for(tmp_path):
    app = create_app(tmp_path / "work-assistant.db", run_starter=lambda spec: 1, log_dir=tmp_path / "logs",
                     startup_work=False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def pct(part, whole):
    return round(part / whole * 100, 1)


# -- the brief's tests ----------------------------------------------------------------------------


def test_report_main_numbers_use_original_outcomes(conn, version):
    shown = add_shown(conn, version, 1, all_met(version), overrides={"sc02": "missed", "sc06": "missed"})
    run_id = add_test_run(conn)
    add_tested(conn, run_id, shown, all_met(version))

    result = compare.report(conn, run_id)
    summary = result["summary"]
    assert summary["calls"] == 1
    assert summary["check_agreement_pct"] == 100.0       # against the original outcomes, not the owner's
    assert summary["avg_score_diff"] == 0.0
    assert summary["verdict_changes"] == []
    [call] = result["calls"]
    assert (call["shown_score"], call["shown_verdict"], call["test_score"], call["test_verdict"]) == (
        100.0, "good", 100.0, "good")
    assert call["differing_checks"] == 0
    assert all(c["agreement_pct"] == 100.0 and c["differing"] == 0 for c in result["checks"])


def test_override_line(conn, version):
    overruled = add_shown(conn, version, 1, all_met(version), overrides={"sc02": "missed", "sc06": "missed"})
    plain = add_shown(conn, version, 2, all_met(version), agent=LEE, call="2026-10-06T15:00:00Z")
    run_id = add_test_run(conn)
    add_tested(conn, run_id, overruled, missing(version, "sc02"))   # matches the owner on sc02 only
    add_tested(conn, run_id, plain, all_met(version))

    result = compare.report(conn, run_id)
    assert result["overrides"] == {"checks": 2, "original_matched": 0, "test_matched": 1}
    checks = len(all_met(version))
    assert result["summary"]["check_agreement_pct"] == pct(2 * checks - 1, 2 * checks)


def test_verdict_changes_and_not_counted(conn, version):
    problem = add_shown(conn, version, 1, missing(version, "sc06"))
    good = add_shown(conn, version, 2, all_met(version), call="2026-10-06T15:00:00Z")
    same = add_shown(conn, version, 3, all_met(version), call="2026-10-07T15:00:00Z")
    run_id = add_test_run(conn)
    add_tested(conn, run_id, problem, all_met(version))
    add_tested(conn, run_id, good, all_met(version), counted=False)
    add_tested(conn, run_id, same, all_met(version))

    result = compare.report(conn, run_id)
    assert sorted(result["summary"]["verdict_changes"], key=lambda c: c["from"]) == [
        {"from": "good", "to": "not_counted", "count": 1},
        {"from": "problem", "to": "good", "count": 1},
    ]
    assert result["judgments"]["counted"] == {"same": 2, "different": 1, "not_recorded": 0}
    rows = {c["ticket_id"]: c for c in result["calls"]}
    assert (rows[2]["test_verdict"], rows[2]["test_score"]) == ("not_counted", None)
    assert (rows[1]["shown_verdict"], rows[1]["test_verdict"]) == ("problem", "good")
    # Calls 1 and 3 have a score on both sides (+5 and 0); call 2's test is Not counted, so it has none.
    assert result["summary"]["avg_score_diff"] == 2.5
    assert result["calls"][0]["ticket_id"] == 1   # the largest score difference first


def test_dropped_call_flips_call_length_reported(conn, version, tmp_path):
    set_setting(conn, "retention_days", 36500)
    logs = tmp_path / "logs"
    calls = [make_contact(101, duration_seconds=720), make_contact(102, duration_seconds=720,
                                                                   call_started_at="2026-10-06T10:00:00Z")]
    pipeline.start_run(conn, "manual", WINDOW, fetcher=lambda w, log: calls, anthropic_client=client_for(
        FakeBatches()), images_loader=lambda c, log: [], log_dir=logs)
    fake = FakeBatches()
    fake.dropped["t101"] = "yes"   # the test model judges call 101 dropped; the shown review did not
    run_id = pipeline.start_test(conn, WINDOW, model_id=SONNET, effort="high", anthropic_client=client_for(fake),
                                 images_loader=lambda c, log: [], log_dir=logs)

    result = compare.report(conn, run_id)
    by_check = {c["check_id"]: c for c in result["checks"]}
    assert by_check["pr-call-length"] == {"check_id": "pr-call-length", "label": "10-minute rule",
                                          "agreement_pct": 50.0, "differing": 1}
    assert result["checks"][0]["check_id"] == "pr-call-length"   # least agreement first
    # No check is forced to be identical: every other check agrees because the outcomes really match.
    assert all(c["agreement_pct"] == 100.0 for c in result["checks"][1:])
    assert result["judgments"]["call_dropped"] == {"same": 1, "different": 1, "not_recorded": 0}
    rows = {c["ticket_id"]: c for c in result["calls"]}
    assert (rows[101]["differing_checks"], rows[102]["differing_checks"]) == (1, 0)

    detail = compare.call_detail(conn, run_id, 101)
    first = detail["checks"][0]
    assert (first["check_id"], first["original"], first["owner"], first["test"], first["differs"]) == (
        "pr-call-length", "missed", None, "na", True)
    assert first["test_evidence"] == pipeline.DROPPED_CALL_EVIDENCE
    assert not any(c["differs"] for c in detail["checks"][1:])


def test_rubric_mismatch_flag(conn, version):
    under_old = add_shown(conn, version, 1, all_met(version))
    under_same = add_shown(conn, version, 2, all_met(version), call="2026-10-06T15:00:00Z")
    newer = rubric.save_version(
        conn, [dataclasses.replace(c, enabled=False) if c.check_id == "sc06" else c for c in version.checks],
        version.reference, version.params, "sc06 off")
    run_id = add_test_run(conn)
    add_tested(conn, run_id, under_old, {k: v for k, v in missing(version, "sc02").items() if k != "sc06"},
               version_id=newer.version_id)
    add_tested(conn, run_id, under_same, all_met(version))

    result = compare.report(conn, run_id)
    assert result["summary"]["rubric_mismatch_calls"] == 1
    rows = {c["ticket_id"]: c for c in result["calls"]}
    assert (rows[1]["rubric_mismatch"], rows[2]["rubric_mismatch"]) == (True, False)
    by_check = {c["check_id"]: c for c in result["checks"]}
    assert by_check["sc06"]["agreement_pct"] == 100.0 and by_check["sc06"]["differing"] == 0  # call 2 only
    assert by_check["sc02"]["differing"] == 1
    checks = len(all_met(version))
    assert result["summary"]["check_agreement_pct"] == pct(2 * checks - 2, 2 * checks - 1)

    detail = compare.call_detail(conn, run_id, 1)
    sc06 = next(c for c in detail["checks"] if c["check_id"] == "sc06")
    assert (sc06["original"], sc06["test"], sc06["differs"]) == ("met", None, False)


def test_comparison_survives_rereview_and_purge(conn, version, tmp_path):
    set_setting(conn, "retention_days", 36500)
    logs = tmp_path / "logs"
    calls = [make_contact(101, duration_seconds=720), make_contact(102, call_started_at="2026-10-06T10:00:00Z")]
    fetcher = lambda w, log: calls  # noqa: E731
    pipeline.start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
                       images_loader=lambda c, log: [], log_dir=logs)
    paired = {r["ticket_id"]: r["review_id"] for r in conn.execute("SELECT * FROM reviews WHERE is_current = 1")}
    conn.execute("UPDATE check_results SET override_outcome = 'met' WHERE review_id = ? AND check_id = "
                 "'pr-call-length'", (paired[101],))
    conn.commit()
    fake = FakeBatches()
    fake.dropped["t101"] = "yes"
    run_id = pipeline.start_test(conn, WINDOW, model_id=SONNET, effort="high", anthropic_client=client_for(fake),
                                 images_loader=lambda c, log: [], log_dir=logs)
    before = compare.report(conn, run_id)
    detail_before = compare.call_detail(conn, run_id, 101)
    assert before["overrides"] == {"checks": 1, "original_matched": 0, "test_matched": 0}
    assert before["summary"]["calls"] == 2 and before["summary"]["check_agreement_pct"] < 100.0

    # A re-review replaces both shown reviews, one of them with a Not counted result.
    rereview_fake = FakeBatches()
    rereview_fake.not_counted["t102"] = "Wrong number"
    pipeline.rereview(conn, WINDOW, fetcher=fetcher, anthropic_client=client_for(rereview_fake),
                      images_loader=lambda c, log: [], log_dir=logs)
    current = {r["ticket_id"]: r["review_id"] for r in conn.execute("SELECT * FROM reviews WHERE is_current = 1")}
    assert current[101] != paired[101] and current[102] != paired[102]
    assert compare.report(conn, run_id) == before
    assert compare.call_detail(conn, run_id, 101) == detail_before

    # Retention erases the evidence; the numbers and outcomes stay.
    assert purge_expired(conn, datetime(2027, 6, 1, tzinfo=UTC), 30, logs) == 2
    assert compare.report(conn, run_id) == before
    detail = compare.call_detail(conn, run_id, 101)
    assert detail["purged"] is True
    assert {c["original_evidence"] for c in detail["checks"]} == {ERASED}
    assert {c["test_evidence"] for c in detail["checks"]} == {ERASED}
    strip = ("original_evidence", "test_evidence")
    assert ([{k: v for k, v in c.items() if k not in strip} for c in detail["checks"]]
            == [{k: v for k, v in c.items() if k not in strip} for c in detail_before["checks"]])
    call_length = next(c for c in detail["checks"] if c["check_id"] == "pr-call-length")
    assert (call_length["original"], call_length["owner"], call_length["test"]) == ("missed", "met", "na")


def test_cost_per_side(conn, version):
    first = add_shown(conn, version, 1, all_met(version), cost=0.06)
    second = add_shown(conn, version, 2, all_met(version), cost=None, call="2026-10-06T15:00:00Z")
    run_id = add_test_run(conn)
    add_tested(conn, run_id, first, all_met(version), cost=0.15)
    add_tested(conn, run_id, second, all_met(version), cost=0.25)

    summary = compare.report(conn, run_id)["summary"]
    assert summary["cost_total"] == {"shown": 0.06, "test": 0.4}
    assert summary["cost_per_call"] == {"shown": 0.06, "test": 0.2}
    assert summary["cost_not_recorded"] == {"shown": 1, "test": 0}
    rows = {c["ticket_id"]: c for c in compare.report(conn, run_id)["calls"]}
    assert (rows[1]["shown_cost"], rows[1]["test_cost"], rows[2]["shown_cost"]) == (0.06, 0.15, None)


# -- more -----------------------------------------------------------------------------------------


def test_test_errors_are_listed_and_left_out(conn, version):
    ok = add_shown(conn, version, 1, all_met(version), cost=0.05)
    errored = add_shown(conn, version, 2, missing(version, "sc06"), cost=0.05, call="2026-10-06T15:00:00Z",
                        overrides={"sc02": "missed"})
    run_id = add_test_run(conn)
    add_tested(conn, run_id, ok, missing(version, "sc06"), cost=0.1)
    add_tested(conn, run_id, errored, {"pr-call-length": "met"}, status="error", cost=0.02)

    result = compare.report(conn, run_id)
    summary = result["summary"]
    assert (summary["calls"], summary["test_errors"]) == (1, 1)
    assert summary["verdict_changes"] == [{"from": "good", "to": "problem", "count": 1}]
    assert result["overrides"] == {"checks": 0, "original_matched": 0, "test_matched": 0}
    assert summary["cost_total"] == {"shown": 0.05, "test": 0.1}
    assert result["judgments"]["resolved_on_call"] == {"same": 1, "different": 0, "not_recorded": 0}
    assert [c["ticket_id"] for c in result["calls"]] == [1, 2]   # the error has no score difference: last
    error_row = result["calls"][1]
    assert error_row["test_error"] == "The model declined to review this call."
    assert (error_row["test_score"], error_row["test_verdict"], error_row["differing_checks"]) == (None, None, None)
    assert result["calls"][0]["test_error"] is None

    detail = compare.call_detail(conn, run_id, 2)
    assert detail["test"]["error"] == "The model declined to review this call."
    assert not any(c["differs"] for c in detail["checks"])


def test_summary_models_and_judgments_not_recorded(conn, version):
    older = add_shown(conn, version, 1, all_met(version), dropped=None)
    sonnet = add_shown(conn, version, 2, all_met(version), call="2026-10-06T15:00:00Z", model=SONNET)
    conn.execute("UPDATE reviews SET effort = 'medium' WHERE review_id = ?", (sonnet,))
    conn.commit()
    run_id = add_test_run(conn, model=FABLE, effort="max")
    add_tested(conn, run_id, older, all_met(version), resolved="no")
    add_tested(conn, run_id, sonnet, all_met(version))

    result = compare.report(conn, run_id)
    summary = result["summary"]
    assert (summary["test_model"], summary["test_effort"]) == ("Claude Fable 5.1", "max")
    assert sorted(summary["shown_models"], key=lambda m: m["model_label"]) == [
        {"model_label": "Claude Opus 5.5", "effort": None, "calls": 1},
        {"model_label": "Claude Sonnet 5.5", "effort": "medium", "calls": 1},
    ]
    assert result["judgments"]["call_dropped"] == {"same": 1, "different": 0, "not_recorded": 1}
    assert result["judgments"]["resolved_on_call"] == {"same": 1, "different": 1, "not_recorded": 0}
    rows = {c["ticket_id"]: c for c in result["calls"]}
    assert (rows[1]["agent"], rows[1]["date"]) == (SAM[1], "2026-10-05")


def test_call_detail_orders_and_shows_three_outcomes(conn, version):
    shown = add_shown(conn, version, 1, missing(version, "sc15"), overrides={"sc15": "met"})
    run_id = add_test_run(conn)
    add_tested(conn, run_id, shown, missing(version, "sc02"))

    detail = compare.call_detail(conn, run_id, 1)
    assert [c["check_id"] for c in detail["checks"][:2]] == ["sc02", "sc15"]   # differing first, rubric order
    sc15 = detail["checks"][1]
    assert sc15 == {"check_id": "sc15", "label": sc15["label"], "original": "missed", "owner": "met", "test": "met",
                    "original_evidence": "evidence", "test_evidence": "test evidence", "differs": True}
    assert sc15["label"].startswith("#15 ")
    rest = [c["check_id"] for c in detail["checks"][2:]]
    order = [c.check_id for c in sorted((c for c in version.checks if c.enabled),
                                        key=lambda c: (0, c.number) if c.kind == "scorecard" else (1, 0, c.summary))]
    assert rest == [c for c in order if c not in ("sc02", "sc15")]
    assert (detail["ticket_id"], detail["agent"], detail["date"]) == (1, SAM[1], "2026-10-05")
    assert detail["shown"]["model_label"] == "Claude Opus 5.5" and detail["test"]["model_label"] == "Claude Fable 5.1"
    assert compare.call_detail(conn, run_id, 99) is None


def test_test_list_newest_first(conn, version):
    add_shown(conn, version, 1, all_met(version))
    older = add_test_run(conn, model=SONNET, effort="low", created_at="2026-10-13T00:00:00Z", actual_usd=0.25)
    conn.execute("UPDATE runs SET contact_count = 3 WHERE run_id = ?", (older,))
    newer = add_test_run(conn, created_at="2026-10-14T00:00:00Z", status="awaiting_confirmation", actual_usd=None)

    assert compare.test_list(conn) == [
        {"run_id": newer, "created_at": "2026-10-14T00:00:00Z", "model_label": "Claude Fable 5.1", "effort": "high",
         "window": {"first_day": "2026-10-04", "last_day": "2026-10-10"}, "calls": 0,
         "status": "awaiting_confirmation", "cost_usd": None},
        {"run_id": older, "created_at": "2026-10-13T00:00:00Z", "model_label": "Claude Sonnet 5.5", "effort": "low",
         "window": {"first_day": "2026-10-04", "last_day": "2026-10-10"}, "calls": 3, "status": "complete",
         "cost_usd": 0.25},
    ]


def test_report_routes(conn, version, tmp_path):
    shown = add_shown(conn, version, 1, all_met(version))
    normal_run = conn.execute("SELECT run_id FROM reviews WHERE review_id = ?", (shown,)).fetchone()[0]
    run_id = add_test_run(conn)
    add_tested(conn, run_id, shown, all_met(version))
    api = api_for(tmp_path)

    listed = api.get("/api/tests")
    assert listed.status_code == 200 and [t["run_id"] for t in listed.json()["tests"]] == [run_id]
    assert api.get(f"/api/tests/{run_id}").json() == compare.report(conn, run_id)
    assert api.get(f"/api/tests/{run_id}/calls/1").json() == compare.call_detail(conn, run_id, 1)
    for path in (f"/api/tests/{run_id + 1}", f"/api/tests/{normal_run}", f"/api/tests/{run_id}/calls/2",
                 f"/api/tests/{normal_run}/calls/1", f"/api/tests/{run_id + 1}/calls/1"):
        response = api.get(path)
        assert response.status_code == 404, path
        assert response.json()["detail"]


# -- task 7 follow-ups ----------------------------------------------------------------------------


def test_override_line_counts_only_checks_on_both_sides(conn, version):
    """An overruled check the test has no outcome for (a newer rubric version without it) is left out of the
    override line, as it is left out of agreement."""
    shown = add_shown(conn, version, 1, all_met(version), overrides={"sc06": "missed", "sc02": "missed"})
    newer = rubric.save_version(
        conn, [dataclasses.replace(c, enabled=False) if c.check_id == "sc06" else c for c in version.checks],
        version.reference, version.params, "sc06 off")
    run_id = add_test_run(conn)
    add_tested(conn, run_id, shown, {k: v for k, v in missing(version, "sc02").items() if k != "sc06"},
               version_id=newer.version_id)

    assert compare.report(conn, run_id)["overrides"] == {"checks": 1, "original_matched": 0, "test_matched": 1}


def test_test_run_total_includes_errored_calls(conn, version):
    """`test_run_total_usd` is everything the test run cost (its `actual_usd`), errored calls included; the
    per-side figures stay over the compared calls."""
    ok = add_shown(conn, version, 1, all_met(version), cost=0.05)
    errored = add_shown(conn, version, 2, all_met(version), cost=0.05, call="2026-10-06T15:00:00Z")
    run_id = add_test_run(conn, actual_usd=0.37)
    add_tested(conn, run_id, ok, all_met(version), cost=0.1)
    add_tested(conn, run_id, errored, {}, status="error", cost=0.02)

    summary = compare.report(conn, run_id)["summary"]
    assert summary["test_run_total_usd"] == 0.37
    assert summary["cost_total"] == {"shown": 0.05, "test": 0.1}
    assert summary["cost_per_call"] == {"shown": 0.05, "test": 0.1}

    unpaid = add_test_run(conn, actual_usd=None, created_at="2026-10-14T00:00:00Z")
    assert compare.report(conn, unpaid)["summary"]["test_run_total_usd"] is None
