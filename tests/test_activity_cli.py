"""`work-assistant activity sync` and the daily task's sync (the sync itself is tested in test_activity_sync)."""
import argparse
from datetime import timedelta
from pathlib import Path

import pytest
from test_activity_collect import call, leg, rating
from test_activity_sync import NOW, PAT, FakeClient, stamp, three_pages

from work_assistant import cli, pipeline
from work_assistant.activity import collect, store
from work_assistant.settings import set_setting
from work_assistant.zendesk.client import TicketPage, ZendeskAuthError, ZendeskError


REAL_BUILD_SERVICES = cli.build_services  # the autouse fixture below replaces it for each test


@pytest.fixture
def logs(tmp_path):
    return tmp_path / "logs"


@pytest.fixture
def services(logs):
    return cli.Services(anthropic_client=None, log_dir=logs, now=lambda: NOW)


@pytest.fixture
def db_path(conn, tmp_path):
    set_setting(conn, "team", [PAT])
    return tmp_path / "work-assistant.db"


@pytest.fixture(autouse=True)
def no_anthropic_key(monkeypatch):
    """Activity must work without an Anthropic key: building the full services would exit."""
    def refuse(*args, **kwargs):
        raise AssertionError("activity must not build the review services")

    monkeypatch.setattr(cli, "build_services", refuse)


def use_client(monkeypatch, client):
    monkeypatch.setattr(cli, "_activity_client", lambda conn: client)
    return client


def scheduled_log(logs):
    return (logs / "scheduled.log").read_text(encoding="utf-8")


# -- work-assistant activity sync ----------------------------------------------------------------------


def test_activity_sync_runs_without_an_anthropic_key(conn, db_path, services, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(three_pages()))
    assert cli.main(["activity", "sync"], db_path=db_path, services=services) == 0
    assert conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0] == 3
    assert store.latest_sync(conn)["kind"] == "refresh"
    out = capsys.readouterr().out
    assert "3 pages" in out and "3 new actions" in out


def test_activity_sync_without_services_does_not_need_the_review_services(conn, db_path, monkeypatch):
    use_client(monkeypatch, FakeClient(three_pages()))
    assert cli.main(["activity", "sync", "--kind", "backfill"], db_path=db_path) == 0
    assert store.latest_sync(conn)["kind"] == "backfill"


def test_activity_sync_from_reads_back_to_that_local_day(conn, db_path, services, monkeypatch):
    seen = {}

    def fake_sync(conn, client, **kwargs):
        seen.update(kwargs)
        return {"status": "complete", "synced_through": NOW, "pages": 0, "actions": 0, "error": None}

    use_client(monkeypatch, FakeClient([]))
    monkeypatch.setattr(cli.activity_sync, "sync", fake_sync)
    set_setting(conn, "timezone", "America/Chicago")
    assert cli.main(["activity", "sync", "--kind", "backfill", "--from", "2025-10-01"], db_path=db_path,
                    services=services) == 0
    assert seen["reach_back"].isoformat() == "2025-10-01T05:00:00+00:00", "midnight in Chicago"


def test_activity_sync_from_needs_a_backfill(conn, db_path, services, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["activity", "sync", "--from", "2025-10-01"], db_path=db_path, services=services)
    assert str(stopped.value) == "--from needs --kind backfill."


def test_activity_sync_closes_its_client(conn, db_path, services, monkeypatch):
    client = use_client(monkeypatch, FakeClient([]))
    assert cli.main(["activity", "sync"], db_path=db_path, services=services) == 0
    assert client.closed


def test_activity_sync_says_when_zendesk_is_not_set_up(conn, db_path, services, capsys):
    assert cli.main(["activity", "sync"], db_path=db_path, services=services) == 1
    assert "Zendesk is not set up yet. Run: work-assistant setup" in capsys.readouterr().out
    assert store.latest_sync(conn) is None


def test_activity_sync_builds_the_client_from_settings_and_the_keyring(conn, monkeypatch):
    from work_assistant import credentials

    assert cli._activity_client(conn) is None
    set_setting(conn, "zendesk_subdomain", "acme")
    set_setting(conn, "zendesk_email", "owner@example.com")
    assert cli._activity_client(conn) is None, "the token is still missing"
    credentials.set_secret("zendesk_token", "tok")
    client = cli._activity_client(conn)
    try:
        assert client._base_url == "https://acme.zendesk.com"
    finally:
        client.close()


def test_activity_sync_reports_a_failed_sync(conn, db_path, services, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(three_pages(), fail_on_page=2))
    assert cli.main(["activity", "sync"], db_path=db_path, services=services) == 1
    out = capsys.readouterr().out
    assert "Team activity sync failed" in out and "429" in out
    assert store.latest_sync(conn)["status"] == "failed"


def test_activity_sync_refuses_while_another_is_running(conn, db_path, services, monkeypatch, capsys):
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, 4242)",
                 (store.iso(NOW - timedelta(minutes=5)),))
    conn.commit()
    services.alive = lambda pid: True
    client = use_client(monkeypatch, FakeClient(three_pages()))
    assert cli.main(["activity", "sync"], db_path=db_path, services=services) == 1
    assert "already running" in capsys.readouterr().out
    assert client.starts == []


def test_count_collected_prints_counts(conn, db_path, services, monkeypatch, capsys):
    """Counts, read-only, the department's call legs, calls, ratings and changed tickets in the last N days, each
    record once however often the export returns it, and stores nothing."""
    start = NOW - timedelta(days=30)
    end = NOW - collect.RATINGS_BEFORE_NOW
    day = timedelta(days=1)
    client = use_client(monkeypatch, FakeClient(
        [],
        legs=[([leg(1, 101, at=start + day), leg(2, 102, at=start + 2 * day)], start + 3 * day),
              ([leg(2, 102, at=start + 2 * day), leg(3, 103, at=NOW - day), leg(4, 104, at=NOW + day)], None)],
        calls=[([call(101, 9001, updated=start + day), call(102, 9002, updated=start + 2 * day)], None)],
        ticket_export=[TicketPage([{"id": 9001, "updated_at": stamp(start + day)},
                                   {"id": 9002, "updated_at": stamp(start + 2 * day)}], [], "c-1", False),
                       TicketPage([{"id": 9001, "updated_at": stamp(NOW - day)},
                                   {"id": 9003, "updated_at": stamp(NOW + day)}], [], "c-2", False),
                       TicketPage([{"id": 9004, "updated_at": stamp(NOW + day)}], [], "c-3", True)],
        ratings=[rating(1, "good", at=start + day), rating(2, "bad", at=start - day)]))
    assert cli.main(["activity", "count-collected", "--days", "30"], db_path=db_path, services=services) == 0
    assert capsys.readouterr().out == "3 call legs, 2 calls, 1 ratings, 2 changed tickets in the last 30 days.\n"
    assert client.talk_reads == [("legs", start), ("calls", start)]
    assert client.rating_windows == [(start, end)]
    assert client.ticket_reads == [{"start": start, "cursor": None}]
    assert client.closed
    for table in ("talk_legs", "talk_calls", "sat_ratings", "ticket_states", "collect_state", "activity_sync"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.parametrize("error, line", [
    (ZendeskError("Zendesk returned HTTP 429 for /api/v2/channels/voice/stats/incremental/legs"),
     "Counting failed: Zendesk returned HTTP 429 for /api/v2/channels/voice/stats/incremental/legs\n"),
    (RuntimeError("a customer's words"), "Counting failed: RuntimeError\n"),
    (ZendeskAuthError("Zendesk rejected the credentials (HTTP 401) for /api/v2/satisfaction_ratings"),
     pipeline.ZENDESK_TOKEN_REJECTED + "\n"),
], ids=["zendesk", "other", "credentials"])
def test_count_collected_prints_a_plain_reason(conn, db_path, services, monkeypatch, capsys, error, line):
    """A failed read prints one line, as `activity sync` reports a failure (`describe_error`: a Zendesk error's status
    and path, any other by its type alone), and the command exits 1; rejected credentials print the message the other
    commands give."""
    def fail(client, *, start, end):
        raise error

    client = use_client(monkeypatch, FakeClient([]))
    monkeypatch.setattr(collect, "count_volumes", fail)
    assert cli.main(["activity", "count-collected"], db_path=db_path, services=services) == 1
    assert capsys.readouterr().out == line
    assert client.closed


def test_count_collected_on_the_server_says_how_to_save_a_new_token(conn, services, monkeypatch, capsys):
    """T-169: run with the server config (`--config`), rejected credentials name the server's way to save a token,
    `set-secret` with that config, never the PC's `setup`."""
    def fail(client, *, start, end):
        raise ZendeskAuthError("Zendesk rejected the credentials (HTTP 401) for /api/v2/satisfaction_ratings")

    config = Path("/etc/work-assistant/config.toml")
    use_client(monkeypatch, FakeClient([]))
    monkeypatch.setattr(collect, "count_volumes", fail)
    args = argparse.Namespace(days=30, config=config)
    assert cli.cmd_activity_count_collected(conn, args, services) == 1
    out = capsys.readouterr().out
    assert out == pipeline.zendesk_token_rejected(config) + "\n"
    assert out.endswith(f"then run: work-assistant set-secret zendesk_token --config {config}\n")
    assert "work-assistant setup" not in out


def test_count_collected_defaults_to_30_days():
    assert cli.build_parser().parse_args(["activity", "count-collected"]).days == 30


def test_cli_has_no_prep_make_or_make_due(capsys):
    """Nothing makes a 1:1 prep any more (Collected calls design, "What goes away"): `prep make|due|reset` and the
    sync's `--make-due-preps` are unknown to the CLI."""
    parser = cli.build_parser()
    for argv in (["prep", "make", "--tech", "a@example.com"], ["prep", "due"], ["prep", "reset"],
                 ["activity", "sync", "--kind", "daily", "--make-due-preps"]):
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(argv)
        assert refused.value.code == 2, argv
    capsys.readouterr()


def test_activity_command_needs_sync_and_a_known_kind(capsys):
    parser = cli.build_parser()
    assert parser.parse_args(["activity", "sync"]).kind == "refresh"
    for argv in (["activity"], ["activity", "sync", "--kind", "weekly"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    capsys.readouterr()


# -- the daily task ------------------------------------------------------------------------------


def record_syncs(monkeypatch, outcome=0):
    calls = []

    def fake(conn, args, services=None):
        calls.append(args.kind)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cli, "cmd_activity_sync", fake)
    return calls


def run_scheduled(db_path, services):
    return cli.main(["run", "--scheduled"], db_path=db_path, services=services)


def test_scheduled_run_syncs_after_the_review(conn, db_path, services, monkeypatch):
    order = []
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: (order.append("review") or ([], [])))
    monkeypatch.setattr(cli, "cmd_activity_sync",
                        lambda conn, args, services=None: order.append(args.kind) or 0)
    assert run_scheduled(db_path, services) == 0
    assert order == ["review", "daily"]


def test_scheduled_run_syncs_even_when_review_fails(conn, db_path, services, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("review blew up")

    monkeypatch.setattr(cli, "_catch_up", boom)
    calls = record_syncs(monkeypatch)
    with pytest.raises(RuntimeError, match="review blew up"):
        run_scheduled(db_path, services)
    assert calls == ["daily"]


def test_scheduled_run_syncs_even_when_the_review_exits(conn, db_path, services, monkeypatch):
    def stop(*args, **kwargs):
        raise SystemExit("not set up")

    monkeypatch.setattr(cli, "_catch_up", stop)
    calls = record_syncs(monkeypatch)
    with pytest.raises(SystemExit):
        run_scheduled(db_path, services)
    assert calls == ["daily"]


def test_a_failing_sync_does_not_change_the_runs_result(conn, db_path, services, logs, monkeypatch):
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: ([7], [3]))
    monkeypatch.setattr(cli, "_report_other_open_runs", lambda *a, **k: None)
    record_syncs(monkeypatch, outcome=ZendeskError("Zendesk returned HTTP 500 for /api/v2/x.json after 5 retries"))
    assert run_scheduled(db_path, services) == 3, "the review's exit code stands"
    assert "Team activity sync failed: Zendesk returned HTTP 500" in scheduled_log(logs)


def test_a_sync_that_exits_non_zero_does_not_change_the_runs_result(conn, db_path, services, monkeypatch):
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: ([], []))
    record_syncs(monkeypatch, outcome=1)
    assert run_scheduled(db_path, services) == 0


def test_a_sync_error_shows_only_its_type_unless_it_is_a_zendesk_error(conn, db_path, services, logs, monkeypatch):
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: ([], []))
    record_syncs(monkeypatch, outcome=KeyError("ticket subject: Secret"))
    assert run_scheduled(db_path, services) == 0
    log = scheduled_log(logs)
    assert "Team activity sync failed: KeyError" in log and "Secret" not in log


def test_the_daily_sync_really_runs_in_the_scheduled_log(conn, db_path, services, logs, monkeypatch):
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: ([], []))
    use_client(monkeypatch, FakeClient(three_pages()))
    assert run_scheduled(db_path, services) == 0
    assert store.latest_sync(conn)["kind"] == "daily"
    assert "Team activity synced" in scheduled_log(logs)


def test_scheduled_run_syncs_when_there_is_no_anthropic_key(conn, db_path, logs, monkeypatch):
    """With no key saved build_services exits before the review starts; the sync still runs once."""
    monkeypatch.setattr(cli, "build_services", REAL_BUILD_SERVICES)
    monkeypatch.setattr(cli, "LOG_DIR", logs)
    calls = record_syncs(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", "--scheduled"], db_path=db_path)
    assert stopped.value.code == 1, "the run still fails, as before"
    assert "No Anthropic API key" in scheduled_log(logs)
    assert calls == ["daily"]


def test_other_runs_do_not_sync_when_there_is_no_anthropic_key(conn, db_path, monkeypatch):
    monkeypatch.setattr(cli, "build_services", REAL_BUILD_SERVICES)
    calls = record_syncs(monkeypatch)
    with pytest.raises(SystemExit, match="No Anthropic API key"):
        cli.main(["run", "--last-week"], db_path=db_path)
    assert calls == []


def test_scheduled_run_syncs_even_when_collecting_interrupted_runs_raises(conn, db_path, services, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("batch lookup failed")

    monkeypatch.setattr(pipeline, "resume_runs", boom)
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: pytest.fail("the review must not start"))
    calls = record_syncs(monkeypatch)
    with pytest.raises(RuntimeError, match="batch lookup failed"):
        run_scheduled(db_path, services)
    assert calls == ["daily"]


def test_scheduled_run_syncs_once_when_the_review_completes(conn, db_path, services, monkeypatch):
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: ([], []))
    calls = record_syncs(monkeypatch)
    assert run_scheduled(db_path, services) == 0
    assert calls == ["daily"]
