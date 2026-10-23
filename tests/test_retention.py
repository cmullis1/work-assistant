import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from test_pipeline import WINDOW, FakeBatches, client_for, make_contact

from work_assistant import cli, pipeline
from work_assistant.pipeline import contact_snapshot
from work_assistant.retention import purge_expired
from work_assistant.rubric import ensure_seeded
from work_assistant.scoring import score
from work_assistant.settings import set_setting

NOW = datetime(2027, 1, 20, 12, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_contact(conn, ticket_id, age_days, now=NOW):
    """A stored contact with one current review of two checks (one overridden), `age_days` old at `now`."""
    started = iso(now - timedelta(days=age_days))
    conn.execute(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', ?, 'sam@example.com', 'Sam Agent', 'normal', 1, ?, ?)",
        (ticket_id, started, contact_snapshot(make_contact(ticket_id, call_started_at=started)), started),
    )
    if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at) "
            "VALUES ('manual', ?, ?, 1, 'complete', ?)", (started, started, started),
        )
    review_id = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at) "
        "VALUES (1, ?, 1, 'ok', 1, ?)", (ticket_id, started),
    ).lastrowid
    conn.executemany(
        "INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by, override_outcome, "
        "override_note) VALUES (?, ?, ?, ?, 'model', ?, ?)",
        [(review_id, "sc01", "met", "Agent said: Jane Smith at 555-0100", None, None),
         (review_id, "sc06", "missed", "never asked for the location ID", "met", "heard it on the call")],
    )
    conn.commit()
    return review_id


def outcomes(conn, review_id):
    return [tuple(r) for r in conn.execute(
        "SELECT check_id, outcome, decided_by, override_outcome, override_note FROM check_results "
        "WHERE review_id = ? ORDER BY check_id", (review_id,))]


def effective_outcomes(conn, review_id):
    return {c: override or outcome for c, outcome, _, override, _ in outcomes(conn, review_id)}


def evidence_set(conn):
    return {r[0] for r in conn.execute("SELECT evidence FROM check_results")}


@pytest.fixture
def version(conn):
    return ensure_seeded(conn)


@pytest.fixture
def logs(tmp_path):
    path = tmp_path / "logs"
    path.mkdir()
    return path


# -- purge_expired -------------------------------------------------------------------------------


def test_purges_old_keeps_scores(conn, version, logs):
    old = add_contact(conn, 1, 91)
    new = add_contact(conn, 2, 10)
    old_before = outcomes(conn, old)
    new_before = [tuple(r) for r in conn.execute("SELECT * FROM check_results WHERE review_id = ?", (new,))]
    score_before = score(version, effective_outcomes(conn, old))

    assert purge_expired(conn, NOW, 90, logs) == 1

    row = conn.execute("SELECT * FROM contacts WHERE ticket_id = 1").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] == iso(NOW)
    assert row["agent_email"] == "sam@example.com" and row["priority"] == "normal"
    assert {r[0] for r in conn.execute("SELECT evidence FROM check_results WHERE review_id = ?", (old,))} \
        == {"Removed after 90 days"}
    assert outcomes(conn, old) == old_before  # outcomes, deciders, overrides and notes unchanged
    assert score(version, effective_outcomes(conn, old)) == score_before

    fresh = conn.execute("SELECT * FROM contacts WHERE ticket_id = 2").fetchone()
    assert fresh["purged_at"] is None and json.loads(fresh["snapshot"])["ticket_id"] == 2
    assert [tuple(r) for r in conn.execute("SELECT * FROM check_results WHERE review_id = ?", (new,))] == new_before


def test_purge_erases_evidence_of_every_review_of_the_ticket(conn, version, logs):
    add_contact(conn, 1, 100)
    conn.execute("UPDATE reviews SET is_current = 0")
    second = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at) "
        "VALUES (1, 1, 1, 'ok', 1, 'x')").lastrowid
    conn.execute("INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by) "
                 "VALUES (?, 'sc01', 'met', 'quote', 'model')", (second,))
    conn.commit()
    purge_expired(conn, NOW, 90, logs)
    assert evidence_set(conn) == {"Removed after 90 days"}


def test_purge_erases_not_counted_reason(conn, version, logs):
    old = add_contact(conn, 1, 91)
    new = add_contact(conn, 2, 10)
    conn.execute("UPDATE reviews SET counted = 0, not_counted_reason = '[00:09] Customer: \"it works now\"'")
    conn.commit()
    purge_expired(conn, NOW, 90, logs)
    rows = {r["review_id"]: r for r in conn.execute("SELECT review_id, counted, not_counted_reason FROM reviews")}
    assert (rows[old]["counted"], rows[old]["not_counted_reason"]) == (0, "Removed after 90 days")
    assert rows[new]["not_counted_reason"] == '[00:09] Customer: "it works now"'


def test_purge_erases_review_error_text(conn, version, logs):
    old = add_contact(conn, 1, 91)
    new = add_contact(conn, 2, 10)
    conn.execute("UPDATE reviews SET status = 'error', error = 'parse failed near: Customer: Jane at 555-0100'")
    conn.commit()
    purge_expired(conn, NOW, 90, logs)
    rows = {r["review_id"]: r["error"] for r in conn.execute("SELECT review_id, error FROM reviews")}
    assert rows[old] == "Removed after 90 days"
    assert rows[new] == "parse failed near: Customer: Jane at 555-0100"


def test_purge_leaves_reviews_without_an_error(conn, version, logs):
    add_contact(conn, 1, 91)
    purge_expired(conn, NOW, 90, logs)
    assert conn.execute("SELECT error FROM reviews").fetchone()[0] is None


def test_purge_leaves_counted_reviews_without_a_reason(conn, version, logs):
    add_contact(conn, 1, 91)
    purge_expired(conn, NOW, 90, logs)
    assert conn.execute("SELECT not_counted_reason FROM reviews").fetchone()[0] is None


def test_purge_idempotent(conn, version, logs):
    add_contact(conn, 1, 91)
    assert purge_expired(conn, NOW, 90, logs) == 1
    assert purge_expired(conn, NOW, 90, logs) == 0


def test_changed_retention(conn, version, logs):
    add_contact(conn, 1, 91)
    add_contact(conn, 2, 10)
    assert purge_expired(conn, NOW, 90, logs) == 1
    assert purge_expired(conn, NOW, 5, logs) == 1
    row = conn.execute("SELECT snapshot, purged_at FROM contacts WHERE ticket_id = 2").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] is not None
    assert evidence_set(conn) == {"Removed after 90 days", "Removed after 5 days"}


def test_old_logs_deleted(conn, version, logs):
    old, fresh, other = logs / "run-1.log", logs / "run-2.log", logs / "notes.txt"
    for path in (old, fresh, other):
        path.write_text("x", encoding="utf-8")
    stale = (NOW - timedelta(days=100)).timestamp()
    recent = (NOW - timedelta(days=1)).timestamp()
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    os.utime(fresh, (recent, recent))
    purge_expired(conn, NOW, 90, logs)
    assert not old.exists() and fresh.exists() and other.exists()  # only run-*.log files are touched


def test_missing_log_dir_is_fine(conn, version, tmp_path):
    add_contact(conn, 1, 91)
    assert purge_expired(conn, NOW, 90, tmp_path / "nope") == 1


def test_vacuum_runs_only_when_something_was_purged(conn, version, logs):
    add_contact(conn, 1, 91)
    statements = []
    conn.set_trace_callback(statements.append)
    purge_expired(conn, NOW, 90, logs)
    purge_expired(conn, NOW, 90, logs)
    assert statements.count("VACUUM") == 1


# -- pipeline and CLI wiring ---------------------------------------------------------------------


def start(conn, logs, contacts):
    return pipeline.start_run(conn, "manual", WINDOW, fetcher=lambda w, log: list(contacts),
                              anthropic_client=client_for(FakeBatches()), images_loader=lambda c, log: [],
                              log_dir=logs)


def status_of(conn, run_id):
    return conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]


def test_rereview_skips_purged(conn, version, logs):
    set_setting(conn, "retention_days", 36500)  # keeps the run-end purge out of the way
    fetched = [make_contact(101), make_contact(102, call_started_at="2026-10-09T10:00:00Z")]
    start(conn, logs, fetched)
    assert purge_expired(conn, datetime(2027, 1, 5, tzinfo=timezone.utc), 90, logs) == 1  # only 101 (Oct 5)

    fake = FakeBatches()
    run_id = pipeline.rereview(conn, WINDOW, fetcher=lambda w, log: list(fetched),
                               anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run["status"] == "complete" and run["excluded_purged"] == 1 and run["contact_count"] == 1
    # Zendesk still returns 101, but its erased content is not stored again.
    row = conn.execute("SELECT snapshot, purged_at FROM contacts WHERE ticket_id = 101").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] is not None


def test_rereview_skips_purged_call_no_longer_in_zendesk(conn, version, logs):
    set_setting(conn, "retention_days", 36500)
    later = make_contact(102, call_started_at="2026-10-09T10:00:00Z")
    start(conn, logs, [make_contact(101), later])
    assert purge_expired(conn, datetime(2027, 1, 5, tzinfo=timezone.utc), 90, logs) == 1  # only 101 (Oct 5)

    fake = FakeBatches()
    run_id = pipeline.rereview(conn, WINDOW, fetcher=lambda w, log: [later],  # 101 was deleted in Zendesk
                               anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run["excluded_purged"] == 1 and run["contact_count"] == 1
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ticket 101: no longer returned by Zendesk and its content was purged; skipped" in text


def test_rereview_with_nothing_purged_reports_zero(conn, version, logs):
    set_setting(conn, "retention_days", 36500)
    start(conn, logs, [make_contact(101)])
    run_id = pipeline.rereview(conn, WINDOW, fetcher=lambda w, log: [make_contact(101)],
                               anthropic_client=client_for(FakeBatches()), images_loader=lambda c, log: [], log_dir=logs)
    assert conn.execute("SELECT excluded_purged FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] == 0


def test_run_end_purges_expired_calls(conn, version, logs):
    set_setting(conn, "retention_days", 1)
    run_id = start(conn, logs, [make_contact(101)])
    assert status_of(conn, run_id) == "complete"
    row = conn.execute("SELECT snapshot, purged_at FROM contacts WHERE ticket_id = 101").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE ticket_id = 101 AND status = 'ok'").fetchone()[0] == 1
    assert evidence_set(conn) == {"Removed after 1 days"}


def test_failed_purge_does_not_fail_the_run(conn, version, logs, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pipeline, "purge_expired", boom)
    run_id = start(conn, logs, [make_contact(101)])
    assert status_of(conn, run_id) == "complete"
    assert "purge failed: RuntimeError" in (logs / f"run-{run_id}.log").read_text(encoding="utf-8")


def test_refetched_purged_call_is_erased_again(conn, version, logs):
    set_setting(conn, "retention_days", 1)
    start(conn, logs, [make_contact(101)])
    conn.execute("UPDATE reviews SET is_current = 0")  # no current review, so the next run reviews it again
    conn.commit()
    start(conn, logs, [make_contact(101)])  # the fetch stores the content again; the run-end purge erases it
    row = conn.execute("SELECT snapshot, purged_at FROM contacts WHERE ticket_id = 101").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] is not None


def test_refetched_purged_reviewed_call_is_not_stored_again(conn, version, logs):
    set_setting(conn, "retention_days", 1)
    start(conn, logs, [make_contact(101)])
    set_setting(conn, "retention_days", 36500)  # nothing would erase it a second time
    start(conn, logs, [make_contact(101)])
    row = conn.execute("SELECT snapshot, purged_at FROM contacts WHERE ticket_id = 101").fetchone()
    assert row["snapshot"] == "{}" and row["purged_at"] is not None


def test_cli_purge_prints_count(conn, version, logs, tmp_path, capsys):
    add_contact(conn, 1, 91, now=datetime.now(timezone.utc))
    add_contact(conn, 2, 10, now=datetime.now(timezone.utc))
    services = cli.Services(anthropic_client=None, log_dir=logs)
    assert cli.main(["purge"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert "Purged 1 call" in capsys.readouterr().out
    assert conn.execute("SELECT purged_at FROM contacts WHERE ticket_id = 2").fetchone()[0] is None


# -- activity content (Team activity) ------------------------------------------------------------


def add_activity(conn, ticket_id, age_days, kind="fields", old="a", new="b", subject="Fun Zone - POS frozen",
                 event_id=None):
    from work_assistant.activity import store
    from work_assistant.activity.model import Action

    at = NOW - timedelta(days=age_days)
    store.insert_actions(conn, [Action("sam@example.com", ticket_id, at, kind, old, new,
                                       event_id or ticket_id * 10, 1)])
    conn.execute("INSERT OR REPLACE INTO activity_tickets (ticket_id, subject, purged) VALUES (?, ?, 0)",
                 (ticket_id, subject))
    conn.commit()


def test_purge_keeps_rows_erases_subjects(conn, logs):
    from work_assistant.activity import store

    add_activity(conn, 1, 100)                              # old ticket: its subject and field values go
    add_activity(conn, 2, 10, subject="Recent ticket")      # recent: untouched
    add_activity(conn, 3, 100, kind="status", old="open", new="solved")  # status values are not customer content
    before = conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0]

    purge_expired(conn, NOW, 90, logs)

    assert conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0] == before
    assert store.subjects(conn, [1, 2, 3]) == {1: None, 2: "Recent ticket", 3: None}
    assert conn.execute("SELECT purged FROM activity_tickets WHERE ticket_id = 1").fetchone()[0] == 1
    assert conn.execute("SELECT purged FROM activity_tickets WHERE ticket_id = 2").fetchone()[0] == 0
    fields = conn.execute("SELECT old, new FROM activity_actions WHERE kind = 'fields' AND ticket_id = 1").fetchone()
    assert (fields["old"], fields["new"]) == (None, None)
    recent = conn.execute("SELECT old, new FROM activity_actions WHERE ticket_id = 2").fetchone()
    assert (recent["old"], recent["new"]) == ("a", "b")
    status = conn.execute("SELECT old, new FROM activity_actions WHERE ticket_id = 3").fetchone()
    assert (status["old"], status["new"]) == ("open", "solved")


def test_ticket_with_a_recent_action_keeps_its_subject(conn, logs):
    add_activity(conn, 1, 100)
    add_activity(conn, 1, 5, kind="note", old=None, new=None, event_id=999)  # newest action is recent
    purge_expired(conn, NOW, 90, logs)
    assert conn.execute("SELECT subject FROM activity_tickets WHERE ticket_id = 1").fetchone()[0] \
        == "Fun Zone - POS frozen"
    # The old field values are still erased: they are older than the cutoff themselves.
    assert conn.execute("SELECT old FROM activity_actions WHERE kind = 'fields'").fetchone()[0] is None


def test_activity_purge_vacuums_once_and_is_idempotent(conn, logs):
    add_activity(conn, 1, 100)
    statements = []
    conn.set_trace_callback(statements.append)
    purge_expired(conn, NOW, 90, logs)
    purge_expired(conn, NOW, 90, logs)
    assert statements.count("VACUUM") == 1


# -- satisfaction ratings collected for 1:1 Prep and Trends -------------------------------------------------------


def add_rating(conn, rating_id, age_days, score="bad", reason="Issue not resolved",
               comment="Still can't print receipts."):
    from work_assistant.activity import collected

    collected.upsert_ratings(conn, [{"id": rating_id, "ticket_id": 500 + rating_id, "assignee_id": 11, "score": score,
                                     "created_at": iso(NOW - timedelta(days=age_days)), "reason": reason,
                                     "comment": comment}])


def test_retention_purges_rating_comments(conn, logs):
    """Collected calls design, Retention: a rating's comment and reason are customer-written, so once it is older than
    the retention period they are erased, and its score and times are kept so no count changes. The database is
    VACUUMed once, as for the other erased text."""
    add_rating(conn, 1, 100)
    add_rating(conn, 2, 10, score="good", reason="Issue resolved", comment="Thanks!")
    statements = []
    conn.set_trace_callback(statements.append)

    purge_expired(conn, NOW, 90, logs)
    purge_expired(conn, NOW, 90, logs)

    assert statements.count("VACUUM") == 1
    rows = conn.execute("SELECT id, score, created, reason, comment, purged FROM sat_ratings ORDER BY id")
    assert [tuple(r) for r in rows] == [
        (1, "bad", iso(NOW - timedelta(days=100)), None, None, 1),
        (2, "good", iso(NOW - timedelta(days=10)), "Issue resolved", "Thanks!", 0)]
