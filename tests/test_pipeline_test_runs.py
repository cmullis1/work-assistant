"""Model test runs: stored calls scored by another model into reviews that are never current.

Fakes only: no Anthropic, no Zendesk. Review Focus 2 (a test never holds tickets or blocks a normal run)
is `test_test_run_does_not_hold_or_block_normal_runs`.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
import pytest

from factories import add_run
from test_pipeline import (  # noqa: F401  (fixtures)
    OPUS,
    SONNET,
    WINDOW,
    FakeBatches,
    Killed,
    chicago,
    chicago_day,
    checks_of,
    client_for,
    current_reviews,
    fetching,
    go,
    logs,
    make_contact,
    run_row,
    scheduled_run,
    services_for,
    version,
)
from work_assistant import cli, pipeline
from work_assistant.db import SCHEMA, connect, init_db
from work_assistant.pipeline import confirm_run, contact_snapshot, rereview, resume_runs
from work_assistant.retention import evidence_placeholder, purge_expired
from work_assistant.settings import set_setting

UTC = timezone.utc
FABLE = "claude-fable-5-1"


def model_test(conn, logs, fake, *, model_id=SONNET, effort="high", limit=None, confirm=False, images=None):
    return pipeline.start_test(
        conn, WINDOW, model_id=model_id, effort=effort, limit=limit, anthropic_client=client_for(fake),
        images_loader=images or (lambda contact, log: []), confirm=confirm, log_dir=logs, secrets=())


def reviewed(conn, logs, *ticket_ids, **contact_args):
    """Normal-run reviews of these tickets (calls on consecutive October days, so newest is the last)."""
    contacts = [make_contact(t, call_started_at=f"2026-10-{5 + i:02d}T10:00:00Z", **contact_args)
                for i, t in enumerate(ticket_ids)]
    go(conn, logs, FakeBatches(), contacts)
    return contacts


def reviews_of(conn, run_id):
    return conn.execute("SELECT * FROM reviews WHERE run_id = ? ORDER BY ticket_id", (run_id,)).fetchall()


def current_ids(conn):
    return {r["ticket_id"]: r["review_id"] for r in current_reviews(conn)}


def snapshot_of_shown(conn):
    """Every review and check result, and every contact, exactly as stored."""
    return (
        [tuple(r) for r in conn.execute("SELECT * FROM reviews ORDER BY review_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM check_results ORDER BY review_id, check_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM contacts ORDER BY ticket_id")],
    )


def purge_contact(conn, ticket_id):
    conn.execute("UPDATE contacts SET snapshot = '{}', purged_at = '2026-10-12T00:00:00Z' WHERE ticket_id = ?",
                 (ticket_id,))
    conn.commit()


def add_unreviewed_contact(conn, contact):
    conn.execute(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', ?, ?, ?, ?, 1, ?, '2026-10-12T00:00:00Z')",
        (contact.ticket_id, contact.call_started_at, contact.agent_email, contact.agent_name, contact.priority,
         contact_snapshot(contact)))
    conn.commit()


def errored_review(conn, logs, ticket_id, call_started_at):
    """A call whose only (so current) review is a review error."""
    fake = FakeBatches()
    fake.invalid[f"t{ticket_id}"] = 2
    go(conn, logs, fake, [make_contact(ticket_id, call_started_at=call_started_at)])


@pytest.fixture
def mixed(conn, version, logs):
    """In the window: 101-103 reviewed, 104 reviewed then purged, 105 never reviewed, 106 a review error.
    107 is reviewed but called after the window."""
    contacts = reviewed(conn, logs, 101, 102, 103, 104)
    purge_contact(conn, 104)
    add_unreviewed_contact(conn, make_contact(105, call_started_at="2026-10-09T10:00:00Z"))
    errored_review(conn, logs, 106, "2026-10-10T10:00:00Z")
    go(conn, logs, FakeBatches(), [make_contact(107, call_started_at="2026-10-12T10:00:00Z")])
    return contacts


# -- the brief's tests ----------------------------------------------------------------------------


def test_test_candidates_use_snapshots_only(conn, mixed, logs, tmp_path, capsys):
    shown = current_ids(conn)
    pairs = pipeline.test_candidates(conn, WINDOW, None)
    assert [(c.ticket_id, review_id) for c, review_id in pairs] == [(103, shown[103]), (102, shown[102]),
                                                                    (101, shown[101])]  # newest call first
    assert [c for c, _ in pairs] == list(reversed(mixed[:3]))  # rebuilt from the stored snapshots
    assert [c.ticket_id for c, _ in pipeline.test_candidates(conn, WINDOW, 2)] == [103, 102]
    with pytest.raises(ValueError, match="at least 1"):
        pipeline.test_candidates(conn, WINDOW, 0)

    estimate = pipeline.estimate_test(conn, WINDOW, model_id=SONNET, effort="high", limit=None)
    assert (estimate.contacts, estimate.excluded_purged, estimate.excluded_errored,
            estimate.excluded_unreviewed) == (3, 1, 1, 1)
    assert estimate.estimate_usd > 0
    assert pipeline.estimate_test(conn, WINDOW, model_id=SONNET, effort="high", limit=2).contacts == 2

    loaded = []
    fake = FakeBatches()
    run_id = model_test(conn, logs, fake, limit=2, images=lambda contact, log: loaded.append(contact.ticket_id) or [])
    run = run_row(conn, run_id)
    assert (run["kind"], run["status"], run["contact_count"], run["excluded_purged"]) == ("test", "complete", 2, 1)
    assert sorted(loaded) == [102, 103]  # images come from Zendesk; the call itself from the stored record
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t102", "t103"]

    def no_fetch(window, log):
        raise AssertionError("a model test never fetches calls from Zendesk")

    services = services_for(FakeBatches(), logs)
    services.fetcher = no_fetch
    code = cli.main(["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", FABLE,
                     "--effort", "low", "--limit", "1"], db_path=tmp_path / "work-assistant.db", services=services)
    out = capsys.readouterr().out
    assert code == 0
    assert "Claude Fable 5.1" in out and "effort low" in out
    assert "1 call will be tested · 3 skipped (1 with no review, 1 erased by retention, 1 with an errored review)" in out
    assert "never changes the results the app shows" in out
    newest = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert (newest["kind"], newest["model"], newest["effort"], newest["contact_count"]) == ("test", FABLE, "low", 1)


def test_start_test_stores_noncurrent_paired_reviews(conn, version, logs):
    reviewed(conn, logs, 101, 102)
    shown = current_ids(conn)
    conn.execute("UPDATE check_results SET override_outcome = 'missed', override_note = 'owner' "
                 "WHERE review_id = ? AND check_id = 'sc06'", (shown[101],))
    conn.commit()
    before = snapshot_of_shown(conn)

    fake = FakeBatches()
    run_id = model_test(conn, logs, fake)
    run = run_row(conn, run_id)
    assert (run["kind"], run["status"], run["model"], run["effort"]) == ("test", "complete", SONNET, "high")
    assert {r["params"]["model"] for r in fake.creates[0]} == {SONNET}

    tests = reviews_of(conn, run_id)
    assert [r["ticket_id"] for r in tests] == [101, 102]
    assert all(r["is_current"] == 0 for r in tests)
    assert [r["compared_review_id"] for r in tests] == [shown[101], shown[102]]
    assert all((r["model"], r["effort"], r["status"]) == (SONNET, "high", "ok") for r in tests)
    test_checks = checks_of(conn, tests[0]["review_id"])
    assert len(test_checks) == 23 and not any(r["needs_recheck"] for r in test_checks.values())
    assert all(r["override_outcome"] is None for r in test_checks.values())

    reviews, results, contacts = snapshot_of_shown(conn)
    test_ids = {r["review_id"] for r in tests}
    assert [r for r in reviews if r[0] not in test_ids] == before[0]  # shown reviews untouched
    assert [r for r in results if r[0] not in test_ids] == before[1]  # their checks and overrides too
    assert contacts == before[2]                                      # the stored calls too
    assert current_ids(conn) == shown


def test_test_run_budget_guard(conn, version, logs):
    reviewed(conn, logs, 101, 102)
    shown = current_ids(conn)
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = model_test(conn, logs, fake)
    run = run_row(conn, paused)
    assert run["status"] == "awaiting_confirmation" and run["estimate_usd"] > 0.01
    assert fake.creates == [] and reviews_of(conn, paused) == []

    # Meanwhile a re-review replaces both shown reviews, and the Model setting changes.
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101, call_started_at="2026-10-05T10:00:00Z"),
                                             make_contact(102, call_started_at="2026-10-06T10:00:00Z")]),
             anthropic_client=client_for(FakeBatches()), images_loader=lambda c, log: [], confirm=True, log_dir=logs)
    assert set(current_ids(conn).values()).isdisjoint(shown.values())
    assert run_row(conn, paused)["status"] == "awaiting_confirmation"  # a test is never "superseded"
    set_setting(conn, "model", OPUS)

    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert run_row(conn, paused)["status"] == "complete"
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t102"]
    assert {r["params"]["model"] for r in fake.creates[0]} == {SONNET}
    tests = reviews_of(conn, paused)
    # Paired with the review shown when the test chose the calls, even though 101 was re-reviewed since.
    assert [(r["ticket_id"], r["compared_review_id"], r["is_current"]) for r in tests] == [
        (101, shown[101], 0), (102, shown[102], 0)]

    over = model_test(conn, logs, FakeBatches(), confirm=True)  # --yes goes ahead over the limit
    assert run_row(conn, over)["status"] == "complete"


@pytest.mark.parametrize("status", pipeline.HOLDING_STATUSES)
def test_test_run_does_not_hold_or_block_normal_runs(conn, version, logs, status):
    reviewed(conn, logs, 101)
    set_setting(conn, "budget_limit_usd", 0.01)
    test_run = model_test(conn, logs, FakeBatches())
    conn.execute("UPDATE runs SET status = ?, pid = NULL WHERE run_id = ?", (status, test_run))
    conn.commit()
    set_setting(conn, "budget_limit_usd", 40)
    assert pipeline._held_tickets(conn, 0, pipeline.HOLDING_STATUSES) == {}

    # A normal run of the week goes ahead as usual: 102 is new, 101 already shown.
    fake = FakeBatches()
    normal = go(conn, logs, fake, [make_contact(101), make_contact(102)], kind="scheduled")
    assert run_row(conn, normal)["status"] == "complete"
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]
    text = (logs / f"run-{normal}.log").read_text(encoding="utf-8")
    assert "held by run" not in text

    # A re-review takes the test's call, whatever state the test is in.
    week = fetching([make_contact(101), make_contact(102)])
    assert pipeline.estimate_window(conn, WINDOW, fetcher=week, rereview=True).contacts == 2
    fake = FakeBatches()
    again = rereview(conn, WINDOW, fetcher=week, anthropic_client=client_for(fake),
                     images_loader=lambda c, log: [], confirm=True, log_dir=logs)
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t102"]
    assert {r["ticket_id"]: r["run_id"] for r in current_reviews(conn)} == {101: again, 102: again}
    # Nothing touched the test run (a dead process's `submitting` is always settled as unknown).
    assert run_row(conn, test_run)["status"] == ("submission_unknown" if status == "submitting" else status)
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE run_id = ?", (test_run,)).fetchone()[0] == 0


def test_paused_rereview_confirms_over_an_in_flight_test(conn, version, logs):
    reviewed(conn, logs, 101)
    set_setting(conn, "budget_limit_usd", 0.01)
    paused = rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]), anthropic_client=client_for(FakeBatches()),
                      images_loader=lambda c, log: [], log_dir=logs)
    assert run_row(conn, paused)["status"] == "awaiting_confirmation"

    # A model test ignores the paused re-review's hold and leaves its batch in flight.
    flight = FakeBatches()
    flight.retrieve_error = Killed()
    with pytest.raises(Killed):
        model_test(conn, logs, flight, confirm=True)
    test_run = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    assert run_row(conn, test_run)["status"] == "submitted"

    fake = FakeBatches()
    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]  # not held back by the test
    assert current_reviews(conn)[0]["run_id"] == paused

    flight.retrieve_error = None
    assert resume_runs(conn, anthropic_client=client_for(flight), log_dir=logs) == [test_run]
    [test_review] = reviews_of(conn, test_run)
    assert test_review["is_current"] == 0
    assert current_reviews(conn)[0]["run_id"] == paused  # collecting the test demoted nothing


def test_scheduled_coverage_ignores_test_runs(conn, chicago, logs, tmp_path, capsys):
    scheduled_run(conn, chicago_day(16))
    for day in (17, 18):
        window = tuple(m.strftime("%Y-%m-%dT%H:%M:%SZ") for m in chicago_day(day))
        add_run(conn, kind="test", status="complete", window=window)
    paused_test = add_run(conn, kind="test", status="awaiting_confirmation")
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=datetime(2026, 10, 19, 11, tzinfo=UTC))
    assert cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert seen == [chicago_day(17), chicago_day(18)]  # the test runs covered nothing
    assert f"Run {paused_test}" not in capsys.readouterr().out  # a test keeps no calls from a run


def test_retention_purges_test_review_evidence(conn, version, logs, tmp_path):
    reviewed(conn, logs, 101, 102)
    fake = FakeBatches()
    fake.invalid["t102"] = 2  # the test's review of 102 is a review error
    run_id = model_test(conn, logs, fake)
    tests = reviews_of(conn, run_id)
    assert [r["status"] for r in tests] == ["ok", "error"]
    outcomes = {r["review_id"]: {c: row["outcome"] for c, row in checks_of(conn, r["review_id"]).items()}
                for r in tests}

    assert purge_expired(conn, datetime(2027, 6, 1, tzinfo=UTC), 90, tmp_path / "logs") == 2
    placeholder = evidence_placeholder(90)
    after = reviews_of(conn, run_id)
    assert [(r["review_id"], r["compared_review_id"], r["is_current"]) for r in after] == [
        (r["review_id"], r["compared_review_id"], 0) for r in tests]
    for review in after:
        checks = checks_of(conn, review["review_id"])
        assert {row["evidence"] for row in checks.values()} == {placeholder}
        assert {c: row["outcome"] for c, row in checks.items()} == outcomes[review["review_id"]]
    assert after[1]["error"] == placeholder


OLD_KIND_SCHEMA = (
    SCHEMA.replace("'rereview','test'", "'rereview'")
    .replace("    compared_review_id INTEGER REFERENCES reviews,\n", "")
    .replace("    compared_review_id INTEGER REFERENCES reviews\n", "")
)


def test_kind_migration_keeps_rows(tmp_path: Path):
    assert "'test'" not in OLD_KIND_SCHEMA and "compared_review_id INTEGER" not in OLD_KIND_SCHEMA
    c = connect(tmp_path / "old.db")
    c.executescript(OLD_KIND_SCHEMA)
    c.execute("INSERT INTO rubric_versions (version_id, created_at, note, snapshot) VALUES (1, 'x', 'v1', '{}')")
    c.execute("INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, attribution_ok, "
              "snapshot, fetched_at) VALUES (9, 'voice', '2026-10-05T10:00:00Z', 'a@x.com', 'A', 1, '{}', 'x')")
    rows = [
        (1, "scheduled", "2026-10-04T05:00:00Z", "2026-10-11T05:00:00Z", 1, "complete", "b1", 1.5, 1.25, 4, None,
         "2026-10-12T11:00:00Z", "2026-10-12T12:00:00Z", 0, 11, None, None),
        (2, "manual", "w0", "w1", 1, "failed", None, None, None, None, "ZendeskError", "c", "f", 0, None,
         OPUS, "medium"),
        (5, "rereview", "w0", "w1", 1, "awaiting_confirmation", None, 2.0, None, 1, None, "c", None, 3, 4242,
         SONNET, "high"),
    ]
    c.executemany("INSERT INTO runs VALUES (" + ", ".join("?" * 17) + ")", rows)
    c.execute("INSERT INTO run_contacts (run_id, ticket_id) VALUES (5, 9)")
    c.execute("INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at) "
              "VALUES (5, 9, 1, 'ok', 1, 'x')")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at) "
                  "VALUES ('test', 'a', 'b', 1, 'fetching', 'c')")

    init_db(c)
    assert [tuple(r) for r in c.execute("SELECT * FROM runs ORDER BY run_id")] == rows
    assert c.execute("SELECT run_id FROM reviews").fetchone()[0] == 5
    assert c.execute("SELECT run_id FROM run_contacts").fetchone()[0] == 5
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    new_id = c.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at) "
                       "VALUES ('test', 'a', 'b', 1, 'fetching', 'c')").lastrowid
    assert new_id == 6
    for table in ("reviews", "run_contacts"):
        assert "compared_review_id" in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    init_db(c)  # idempotent once migrated
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
    c.close()


# -- more -----------------------------------------------------------------------------------------


def test_test_ignores_runs_whose_submission_is_unknown(conn, version, logs):
    reviewed(conn, logs, 101)
    unknown = add_run(conn, kind="rereview", status="submission_unknown")
    conn.execute("INSERT INTO run_contacts (run_id, ticket_id) VALUES (?, 101)", (unknown,))
    conn.commit()
    fake = FakeBatches()
    run_id = model_test(conn, logs, fake)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]
    assert run_row(conn, run_id)["status"] == "complete"


def test_two_tests_over_the_same_calls(conn, version, logs):
    reviewed(conn, logs, 101)
    shown = current_ids(conn)
    first = model_test(conn, logs, FakeBatches(), model_id=SONNET)
    second = model_test(conn, logs, FakeBatches(), model_id=FABLE, effort="low")
    for run_id, model in ((first, SONNET), (second, FABLE)):
        [review] = reviews_of(conn, run_id)
        assert (review["model"], review["compared_review_id"], review["is_current"]) == (model, shown[101], 0)
    assert current_ids(conn) == shown


def test_test_error_review_never_blocks_a_normal_run(conn, version, logs):
    """A review error stored by a test is not "already reviewed": normal runs retry only their own errors."""
    add_unreviewed_contact(conn, make_contact(101))
    test_run = add_run(conn, kind="test")
    conn.execute("INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, error, is_current, created_at) "
                 "VALUES (?, 101, ?, 'error', 'invalid output', 0, 'x')", (test_run, version.version_id))
    conn.commit()
    assert pipeline._already_reviewed(conn, 101, version.version_id) is False
    fake = FakeBatches()
    go(conn, logs, fake, [make_contact(101)])
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]


def test_unknown_model_or_effort_rejected(conn, version, logs, tmp_path):
    with pytest.raises(ValueError, match="model"):
        pipeline.estimate_test(conn, WINDOW, model_id="claude-nope", effort="high", limit=None)
    with pytest.raises(ValueError, match="effort"):
        model_test(conn, logs, FakeBatches(), effort="extreme")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    services = services_for(FakeBatches(), logs)
    with pytest.raises(SystemExit, match="Claude Sonnet 5.5"):
        cli.main(["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", "sonnet",
                  "--effort", "high"], db_path=tmp_path / "work-assistant.db", services=services)
    with pytest.raises(SystemExit, match="low, medium, high, xhigh, max"):
        cli.main(["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", SONNET,
                  "--effort", "extreme"], db_path=tmp_path / "work-assistant.db", services=services)
    with pytest.raises(SystemExit, match="--from"):
        cli.main(["test-model", "--from", "2026-10-10", "--to", "2026-10-04", "--model", SONNET,
                  "--effort", "high"], db_path=tmp_path / "work-assistant.db", services=services)


def test_cli_test_model_pauses_then_confirms(conn, version, logs, tmp_path, capsys):
    reviewed(conn, logs, 101)
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    services = services_for(fake, logs)
    db_path = tmp_path / "work-assistant.db"
    args = ["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", SONNET, "--effort", "high"]
    assert cli.main(args, db_path=db_path, services=services) == 0
    run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    out = capsys.readouterr().out
    assert "awaiting_confirmation" in out and f"work-assistant confirm {run_id}" in out
    assert fake.creates == []
    assert cli.main(["confirm", str(run_id)], db_path=db_path, services=services) == 0
    assert run_row(conn, run_id)["status"] == "complete"
    assert cli.main([*args, "--yes"], db_path=db_path, services=services) == 0
    assert run_row(conn, run_id + 1)["status"] == "complete"


def test_interrupted_test_fetch_is_failed_and_cleared_runs_release(conn, version, logs):
    fetching_test = add_run(conn, kind="test", status="fetching", pid=None)
    assert pipeline.fail_interrupted_fetching(conn, datetime(2026, 10, 12, 12, tzinfo=UTC), lambda pid: False) == [
        fetching_test]
    unknown = add_run(conn, kind="test", status="submission_unknown")
    pipeline.clear_run(conn, unknown, log_dir=logs)
    assert run_row(conn, unknown)["status"] == "failed"


# -- follow-ups from Task 5's review (Task 6) ------------------------------------------------------


def test_cli_report_wording_for_a_test(conn, version, logs, capsys):
    unknown = add_run(conn, kind="test", status="submission_unknown", error=pipeline.SUBMISSION_UNRECORDED)
    conn.execute("UPDATE runs SET excluded_purged = 2 WHERE run_id = ?", (unknown,))
    conn.commit()
    assert cli._report(conn, unknown) == 1
    out = capsys.readouterr().out
    assert "held" not in out
    assert "A model test holds no calls" in out and f"work-assistant clear-run {unknown}" in out
    assert "2 purged calls left out: they can't be tested because their content was erased" in out

    normal = add_run(conn, kind="rereview", status="submission_unknown", error=pipeline.SUBMISSION_UNRECORDED)
    conn.execute("UPDATE runs SET excluded_purged = 1 WHERE run_id = ?", (normal,))
    conn.commit()
    cli._report(conn, normal)
    out = capsys.readouterr().out
    assert "Its calls stay held" in out and "cannot be reviewed again" in out


def test_submission_unknown_log_for_a_test_says_nothing_is_held(conn, version, logs):
    reviewed(conn, logs, 101)
    fake = FakeBatches()
    fake.create_error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    run_id = model_test(conn, logs, fake)
    assert run_row(conn, run_id)["status"] == "submission_unknown"
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "held" not in text and "a model test holds no calls" in text


def test_interrupted_test_points_to_test_a_model(conn, version, logs, tmp_path, capsys):
    fetching_test = add_run(conn, kind="test", status="fetching", pid=None)
    fetching_run = add_run(conn, kind="scheduled", status="fetching", pid=None,
                           window=("2026-09-27T05:00:00Z", "2026-10-04T05:00:00Z"))
    services = services_for(FakeBatches(), logs, [], now=datetime(2026, 10, 12, 12, tzinfo=UTC))
    services.alive = lambda pid: False
    cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services)
    lines = capsys.readouterr().out.splitlines()
    [test_line] = [line for line in lines if f"{fetching_test} was interrupted" in line]
    assert test_line.startswith(f"Model test {fetching_test}")
    assert "Settings > Test a model" in test_line and "work-assistant test-model" in test_line
    assert "Run now" not in test_line and "week" not in test_line
    [run_line] = [line for line in lines if f"Run {fetching_run} was interrupted" in line]
    assert "its days are reviewed again" in run_line


def test_estimate_test_prices_at_the_tests_model(conn, version, logs):
    reviewed(conn, logs, 101, 102, 103)
    # The same learned output size for both models, so only the prices differ.
    run_id = conn.execute("SELECT MIN(run_id) FROM runs").fetchone()[0]
    for model_id in (OPUS, FABLE):
        conn.executemany(
            "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at, model, effort, "
            "output_tokens) VALUES (?, 101, ?, 'ok', 0, 'x', ?, 'high', 4000)",
            [(run_id, version.version_id, model_id)] * 10)
    conn.commit()
    estimates = {}
    for setting in (OPUS, SONNET, FABLE):
        set_setting(conn, "model", setting)
        estimates[setting] = {m: pipeline.estimate_test(conn, WINDOW, model_id=m, effort="high", limit=None)
                              .estimate_usd for m in (OPUS, FABLE)}
    assert estimates[OPUS] == estimates[SONNET] == estimates[FABLE]   # whatever the Model setting is
    opus, fable = estimates[OPUS][OPUS], estimates[OPUS][FABLE]
    assert opus > 0
    assert fable / opus == pytest.approx(2.5, rel=0.05)


# -- final review ---------------------------------------------------------------------------------


def test_cancel_a_model_test_waiting_for_your_ok(conn, version, logs, tmp_path, capsys):
    """A paused model test can be cancelled like a run: nothing is sent, and the CLI calls it a model test."""
    reviewed(conn, logs, 101)
    shown = snapshot_of_shown(conn)
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    services = services_for(fake, logs)
    db_path = tmp_path / "work-assistant.db"
    args = ["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", SONNET, "--effort", "high"]
    assert cli.main(args, db_path=db_path, services=services) == 0
    run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    assert f"work-assistant cancel-run {run_id}" in capsys.readouterr().out

    assert cli.main(["cancel-run", str(run_id)], db_path=db_path, services=services) == 0
    assert capsys.readouterr().out == f"Model test {run_id} cancelled. Nothing was sent or charged.\n"
    run = run_row(conn, run_id)
    assert (run["status"], run["error"]) == ("cancelled", pipeline.CANCELLED_BY_OWNER)
    assert fake.creates == [] and reviews_of(conn, run_id) == []
    assert snapshot_of_shown(conn) == shown


def test_cli_test_model_prints_its_estimate_before_submitting(conn, mixed, logs, tmp_path, capsys):
    """Before anything is sent, test-model prints the panel's line: calls tested, skipped by reason, estimate."""
    printed = []

    class Watching(FakeBatches):
        def create(self, requests):
            printed.append(capsys.readouterr().out)   # what was printed before the batch was created
            return super().create(requests)

    fake = Watching()
    code = cli.main(["test-model", "--from", "2026-10-04", "--to", "2026-10-10", "--model", SONNET,
                     "--effort", "high"], db_path=tmp_path / "work-assistant.db", services=services_for(fake, logs))
    assert code == 0 and len(fake.creates) == 1
    estimate = pipeline.estimate_test(conn, WINDOW, model_id=SONNET, effort="high", limit=None).estimate_usd
    line = (f"3 calls will be tested · 3 skipped (1 with no review, 1 erased by retention, 1 with an errored "
            f"review) · estimate ${estimate:.2f}")
    assert line in printed[0].splitlines()
