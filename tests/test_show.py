"""`work-assistant show`: the read-only terminal view used to check results against the owner's judgment."""
import sqlite3

import pytest

from work_assistant import cli
from work_assistant.rubric import current_version, ensure_seeded
from work_assistant.settings import set_setting


def seed_review(conn, version, outcomes, *, overrides=None, ticket_id=4242, status="ok", not_counted=None):
    """One contact and one review in run 1 (created on first use); `outcomes` maps check_id -> (outcome, evidence).

    `not_counted` is the reviewer's Not counted reason, or None for a counted call.
    """
    overrides = overrides or {}
    conn.execute(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', '2026-10-05T10:00:00Z', 'sam@example.com', "
        "'Sam Agent', 'high', 1, '{}', '2026-10-06T00:00:00Z')", (ticket_id,))
    if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, estimate_usd, actual_usd, "
            "contact_count, created_at) VALUES ('manual', '2026-10-04', '2026-10-11', ?, 'complete', 0.5, 0.25, 1, "
            "'2026-10-12T00:00:00Z')", (version.version_id,))
    conn.execute("INSERT INTO run_contacts (run_id, ticket_id) VALUES (1, ?)", (ticket_id,))
    review_id = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, resolved_on_call, is_current, created_at, "
        "counted, not_counted_reason) VALUES (1, ?, ?, ?, 'yes', 1, '2026-10-12T00:00:00Z', ?, ?)",
        (ticket_id, version.version_id, status, int(not_counted is None), not_counted)).lastrowid
    for check_id, (outcome, evidence) in outcomes.items():
        override, note = overrides.get(check_id, (None, None))
        conn.execute(
            "INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by, override_outcome, "
            "override_note) VALUES (?, ?, ?, ?, 'model', ?, ?)", (review_id, check_id, outcome, evidence, override, note))
    conn.commit()


@pytest.fixture
def version(conn):
    return ensure_seeded(conn)


def show(tmp_path, capsys, *argv):
    code = cli.main(["show", *argv], db_path=tmp_path / "work-assistant.db")
    return code, capsys.readouterr().out


def test_show_lists_outcomes(conn, version, tmp_path, capsys):
    seed_review(conn, version, {
        "sc01": ("missed", "Never greeted the caller by company name"),
        "sc02": ("unsure", "Transcript cuts off"),
        "sc03": ("met", "Verified the account"),
    })
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    assert "Run 1: manual" in out and "status complete" in out and "Actual: $0.25" in out
    assert "4242" in out and "Sam Agent" in out and "2026-10-05T10:00:00Z" in out and "priority high" in out
    assert f"✗ missed [sc01] {next(c.summary for c in version.checks if c.check_id == 'sc01')}: " \
           "Never greeted the caller by company name" in out
    assert "? unsure [sc02]" in out and "Transcript cuts off" in out
    assert "1 checks met" in out
    assert "Verified the account" not in out
    assert "verdict problem" in out and "resolved on call: yes" in out
    earned = sum(c.points for c in version.checks if c.check_id in ("sc02", "sc03"))  # unsure and met earn
    total = sum(c.points for c in version.checks if c.kind == "scorecard" and c.enabled)
    assert f"Score {earned}/{total} " in out


def test_show_uses_overrides_and_only_enabled_checks_of_the_reviews_version(conn, version, tmp_path, capsys):
    seed_review(conn, version, {
        "sc01": ("missed", "No greeting"),
        "sc02": ("met", "ok"),
        "gone": ("missed", "belongs to a check this version does not have"),
    }, overrides={"sc01": ("met", "Greeted on the recording")})
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    assert "(overridden → met: Greeted on the recording)" in out
    assert "gone" not in out
    assert "verdict good" in out  # the override counts, and the unknown check is ignored


def test_show_review_error_is_review_failed(conn, version, tmp_path, capsys):
    seed_review(conn, version, {}, status="error")
    _, out = show(tmp_path, capsys, "--run", "1")
    assert "verdict review_failed" in out
    assert "0 needs review, 0 problem, 1 review failed." in out


def test_show_rubric_lists_checks_and_references(conn, version, tmp_path, capsys):
    code, out = show(tmp_path, capsys, "--rubric")
    assert code == 0
    assert f"Rubric version {current_version(conn).version_id}" in out
    assert "[sc01] #1" in out and "[pr-scope]" in out and "Applies when:" in out
    assert "Priority definitions" in out and "Third-party barcode scanners" in out
    assert "Call length limit: 10 minutes" in out
    assert "Summary email deadline: 30 min after the call ends" in out


def test_show_rubric_before_first_run_shows_defaults_without_writing(tmp_path, capsys):
    code, out = show(tmp_path, capsys, "--rubric")
    assert code == 0
    assert "sc01" in out and "Third-party barcode scanners" in out and "No rubric is saved yet" in out
    assert not (tmp_path / "work-assistant.db").exists()


def test_show_unknown_run(conn, version, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["show", "--run", "99"], db_path=tmp_path / "work-assistant.db")
    assert "Run 99 does not exist" in str(exc.value)


def test_show_does_not_write(conn, version, tmp_path, capsys):
    seed_review(conn, version, {"sc01": ("met", "ok")})
    before = list(conn.iterdump())
    show(tmp_path, capsys, "--run", "1")
    show(tmp_path, capsys, "--rubric")
    assert list(conn.iterdump()) == before
    ro = cli._open_read_only(tmp_path / "work-assistant.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM runs")
    finally:
        ro.close()


def test_show_labels_not_counted_and_leaves_it_out_of_the_summary(conn, version, tmp_path, capsys):
    seed_review(conn, version, {"sc01": ("met", "ok")}, ticket_id=4242)
    seed_review(conn, version, {"sc01": ("missed", "No greeting")}, ticket_id=4243,
                not_counted='[00:12] Customer: "my problem seems to have resolved itself"')
    seed_review(conn, version, {"sc01": ("missed", "No greeting")}, ticket_id=4244)
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    block = out.split("Ticket 4243", 1)[1].split("Ticket 4244", 1)[0]
    assert 'NOT COUNTED: [00:12] Customer: "my problem seems to have resolved itself"' in block
    assert "verdict" not in block and "Score" not in block
    assert "✗ missed [sc01]" in block  # the stored review stays visible
    assert "Summary: 1 good, 0 needs review, 1 problem, 0 review failed. Not counted (left out of these counts): 1." in out


def test_show_summary_without_not_counted(conn, version, tmp_path, capsys):
    seed_review(conn, version, {"sc01": ("unsure", "garbled")})
    _, out = show(tmp_path, capsys, "--run", "1")
    assert "NOT COUNTED" not in out
    assert "Summary: 0 good, 1 needs review, 0 problem, 0 review failed. Not counted (left out of these counts): 0." in out


def test_show_names_the_tech_by_their_typed_name(conn, version, tmp_path, capsys):
    from work_assistant import names

    seed_review(conn, version, {"sc01": ("met", "ok")})
    names.store(conn, "Sam@Example.com", "Samuel Typed")
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    assert "Ticket 4242  Samuel Typed  call 2026-10-05T10:00:00Z" in out
    assert "Sam Agent" not in out


def test_show_reads_a_database_from_before_typed_names(tmp_path, capsys):
    # show opens the database read-only, so it can't add the tech_names table to an older one.
    import re

    from work_assistant.db import SCHEMA, connect

    schema = re.sub(r"CREATE TABLE IF NOT EXISTS tech_names \([^;]*\);", "", SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS tech_names" not in schema
    db = connect(tmp_path / "work-assistant.db")
    db.executescript(schema)
    version = ensure_seeded(db)
    seed_review(db, version, {"sc01": ("met", "ok")})
    db.close()
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    assert "Ticket 4242  Sam Agent  call" in out


def test_show_reads_a_database_from_before_the_counted_columns(tmp_path, capsys):
    from work_assistant.db import SCHEMA, connect

    db = connect(tmp_path / "work-assistant.db")
    db.executescript(SCHEMA.replace("    counted           INTEGER NOT NULL DEFAULT 1,\n", "")
                     .replace("    not_counted_reason TEXT,\n", ""))
    version = ensure_seeded(db)
    db.execute(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (1, 'voice', 'x', 'a@example.com', 'A', 'high', 1, '{}', 'x')")
    db.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at) "
               "VALUES ('manual', 'a', 'b', ?, 'complete', 'c')", (version.version_id,))
    db.execute("INSERT INTO run_contacts (run_id, ticket_id) VALUES (1, 1)")
    db.execute("INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at) "
               "VALUES (1, 1, ?, 'ok', 1, 'c')", (version.version_id,))
    db.commit()
    db.close()
    code, out = show(tmp_path, capsys, "--run", "1")
    assert code == 0
    assert "Summary: 1 good, 0 needs review, 0 problem, 0 review failed." in out
