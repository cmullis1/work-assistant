"""Filling the history of techs added after the stored activity began (`sync(fill=...)`, `techs_to_fill`), and
`work-assistant activity fill-techs`. The sync itself is tested in test_activity_sync."""
from datetime import date, datetime, timedelta

import httpx
import pytest
from test_activity_sync import NOW, PAT, PAT_ID, SAM, SAM_ID, UTC, FakeClient, event, page, run, stamp, ts

from work_assistant import cli
from work_assistant.activity import store
from work_assistant.activity import sync as activity_sync
from work_assistant.activity.model import ShiftVersion
from work_assistant.settings import set_setting
from work_assistant.zendesk.client import ZendeskClient

LATER = NOW + timedelta(days=5)
COVERED = NOW - timedelta(days=90)     # where the first (90-day) sync began: 2026-07-21 11:00 UTC
FIRST_DAY = date(2026, 7, 22)          # the first day whose midnight (UTC) the stored activity covers
SAM_ADDED = (NOW + timedelta(days=1)).date()


@pytest.fixture(autouse=True)
def team(conn):
    set_setting(conn, "team", [PAT])
    set_setting(conn, "retention_days", 90)
    set_setting(conn, "timezone", "UTC")


def with_sam_added_late(conn, tmp_path):
    """A complete 90-day sync for PAT at NOW, then SAM is added the next day: SAM's shift starts that day."""
    store.add_shift_version(conn, ShiftVersion(PAT, COVERED.date(), frozenset({1, 2, 3, 4, 5}), 480, 1020, True))
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    set_setting(conn, "team", [PAT, SAM])
    store.apply_team_change(conn, [SAM], [], today=SAM_ADDED)


def year_pages():
    """SAM's and PAT's older events, then one of SAM's after the add (the forward read's)."""
    return [
        page([event(10, 40, NOW - timedelta(days=95), author_id=SAM_ID),
              event(11, 41, NOW - timedelta(days=60), author_id=SAM_ID),
              event(12, 42, NOW - timedelta(days=50), author_id=PAT_ID)], NOW - timedelta(days=40)),
        page([event(13, 43, NOW - timedelta(days=10), author_id=SAM_ID)], NOW - timedelta(days=2)),
        page([event(14, 44, NOW + timedelta(days=2), author_id=SAM_ID)], LATER + timedelta(hours=1),
             end_of_stream=True),
    ]


def both():
    return {PAT: PAT_ID, SAM: SAM_ID}


def stored(conn):
    return {r[0] for r in conn.execute("SELECT event_id FROM activity_actions")}


def fill_row(conn):
    return conn.execute("SELECT * FROM activity_sync WHERE fill_techs IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()


# -- which techs need a fill ------------------------------------------------------------------------------


def test_a_tech_whose_shift_starts_after_the_history_needs_a_fill(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    assert activity_sync.techs_to_fill(conn) == [(SAM, SAM_ADDED)]


def test_no_tech_needs_a_fill_before_any_sync_or_when_shifts_start_with_the_history(conn, tmp_path):
    set_setting(conn, "team", [PAT, SAM])
    store.apply_team_change(conn, [SAM], [], today=NOW.date())
    assert activity_sync.techs_to_fill(conn) == [], "no stored history yet"
    store.add_shift_version(conn, ShiftVersion(PAT, COVERED.date(), frozenset({1}), 480, 1020, True))
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    assert activity_sync.techs_to_fill(conn) == [], "SAM's first shift is at the window start, before the first sync"


def test_a_tech_with_no_shift_or_off_the_team_needs_no_fill(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    set_setting(conn, "team", [PAT])
    assert activity_sync.techs_to_fill(conn) == []


# -- the fill ---------------------------------------------------------------------------------------------


def test_the_fill_reads_back_to_the_history_start_for_the_named_techs_only(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    client = FakeClient(year_pages(), users=both())
    lines: list[str] = []
    result = run(conn, client, tmp_path, now=LATER, kind="backfill", fill=[SAM], lines=lines)
    assert result["status"] == "complete"
    assert client.starts == [ts(COVERED), ts(NOW)], "the fill from the history start, then on from the cursor"
    assert stored(conn) == {11, 13, 14}, "10 is before the history, 12 is PAT's"
    assert (SAM_ID, COVERED, LATER) in client.login_calls
    assert not [call for call in client.login_calls if call[0] == PAT_ID and call[1] == COVERED]
    row = fill_row(conn)
    assert row["fill_techs"] == SAM and row["fill_from"] == stamp(COVERED)
    assert store.synced_from(conn) == COVERED, "the history start is everyone's, so it doesn't move"
    assert any("Filled 1 tech's activity back to 2026-07-21" in line for line in lines)


def test_the_fill_starts_the_techs_shift_with_the_history(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    store.add_shift_version(conn, ShiftVersion(SAM, SAM_ADDED, frozenset({2, 3}), 540, 1080, True))
    run(conn, FakeClient(year_pages(), users=both()), tmp_path, now=LATER, kind="backfill", fill=[SAM])
    versions = store.shift_versions(conn, SAM)
    assert [(v.effective_from, v.work_days, v.start_min, v.end_min, v.active) for v in versions] == [
        (FIRST_DAY, frozenset({2, 3}), 540, 1080, True),
        (SAM_ADDED, frozenset({2, 3}), 540, 1080, True),
    ], "the tech's own hours, from the first day the history covers"
    assert activity_sync.techs_to_fill(conn) == []


def test_a_shift_already_starting_with_the_history_is_left_alone(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    run(conn, FakeClient(year_pages(), users=both()), tmp_path, now=LATER, kind="backfill", fill=[PAT])
    assert [v.effective_from for v in store.shift_versions(conn, PAT)] == [COVERED.date()]


def test_a_failed_fill_resumes_where_it_stopped(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    result = run(conn, FakeClient(year_pages(), users=both(), fail_on_page=2), tmp_path, now=LATER,
                 kind="backfill", fill=[SAM])
    assert result["status"] == "failed"
    row = fill_row(conn)
    assert row["fill_cursor_ts"] == ts(NOW - timedelta(days=40)) and row["fill_from"] is None
    assert [v.effective_from for v in store.shift_versions(conn, SAM)] == [SAM_ADDED], "not until it is all read"

    again = LATER + timedelta(minutes=1)
    resumed = FakeClient(year_pages(), users=both())
    assert run(conn, resumed, tmp_path, now=again, kind="backfill", fill=[SAM])["status"] == "complete"
    assert resumed.starts[0] == ts(NOW - timedelta(days=40))
    assert stored(conn) == {11, 13, 14}
    assert fill_row(conn)["fill_from"] == stamp(COVERED)


def test_a_fill_for_other_techs_starts_over(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    run(conn, FakeClient(year_pages(), users=both(), fail_on_page=2), tmp_path, now=LATER, kind="backfill",
        fill=[SAM])
    client = FakeClient(year_pages(), users=both())
    run(conn, client, tmp_path, now=LATER + timedelta(minutes=1), kind="backfill", fill=[SAM, PAT])
    assert client.starts[0] == ts(COVERED)


def test_a_named_tech_not_on_the_team_or_not_in_zendesk_is_skipped(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    lines: list[str] = []
    client = FakeClient(year_pages(), users={PAT: PAT_ID})
    result = run(conn, client, tmp_path, now=LATER, kind="backfill", fill=[SAM, "kim@example.com"], lines=lines)
    assert result["status"] == "complete"
    assert client.starts == [ts(NOW)], "only the forward read"
    assert "Not filled, not on the team: kim@example.com" in lines
    assert "Not filled, not found in Zendesk: sam@example.com" in lines
    assert fill_row(conn) is None


def test_no_fill_before_any_history_is_stored(conn, tmp_path):
    set_setting(conn, "team", [PAT, SAM])
    lines: list[str] = []
    client = FakeClient([page([], NOW, end_of_stream=True)], users=both())
    run(conn, client, tmp_path, kind="backfill", fill=[SAM], lines=lines)
    assert "No history to fill yet: the first sync reads every tech's history." in lines
    assert fill_row(conn) is None


def test_the_fill_never_writes(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    methods = []
    base = NOW - timedelta(days=30)

    def handler(request):
        methods.append(request.method)
        assert request.method == "GET", f"{request.method} {request.url.path}"
        path = request.url.path
        if path == "/api/v2/users/search.json":
            return httpx.Response(200, json={"users": [{"id": PAT_ID, "email": PAT}, {"id": SAM_ID, "email": SAM}]})
        if path == "/api/v2/incremental/ticket_events.json":
            return httpx.Response(200, json=page([event(1, 5, base, author_id=SAM_ID)], LATER, end_of_stream=True))
        if path == "/api/v2/audit_logs.json":
            return httpx.Response(200, json={"audit_logs": []})
        if path == "/api/v2/tickets/show_many.json":
            return httpx.Response(200, json={"tickets": [{"id": 5, "subject": "Hello"}]})
        if path == "/api/v2/search.json":
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected request {path}")

    client = ZendeskClient("acme", "owner@example.com", "tok", transport=httpx.MockTransport(handler))
    assert run(conn, client, tmp_path, now=LATER, kind="backfill", fill=[SAM])["status"] == "complete"
    assert methods and set(methods) == {"GET"}
    assert stored(conn) == {1}


# -- work-assistant activity fill-techs -------------------------------------------------------------------


@pytest.fixture
def db_path(conn, tmp_path):
    with_sam_added_late(conn, tmp_path)
    return tmp_path / "work-assistant.db"


@pytest.fixture
def services(tmp_path):
    return cli.Services(anthropic_client=None, log_dir=tmp_path / "logs", now=lambda: LATER)


def use_client(monkeypatch, client):
    monkeypatch.setattr(cli, "_activity_client", lambda conn: client)
    return client


def test_fill_techs_lists_them_and_changes_nothing_without_a_yes(conn, db_path, services, monkeypatch, capsys):
    client = use_client(monkeypatch, FakeClient(year_pages(), users=both()))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["activity", "fill-techs"], db_path=db_path, services=services) == 0
    out = capsys.readouterr().out
    assert "The stored history starts 2026-07-22. These techs' history starts later:" in out
    assert "  sam@example.com: from 2026-10-20" in out
    assert "Nothing was changed." in out
    assert client.starts == [] and fill_row(conn) is None


def test_fill_techs_fills_on_a_yes_and_says_what_comes_next(conn, db_path, services, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(year_pages(), users=both()))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.main(["activity", "fill-techs"], db_path=db_path, services=services) == 0
    out = capsys.readouterr().out
    assert "Filled 1 tech's activity back to 2026-07-21" in out
    assert "1:1 schedule" in out and "Time off" in out
    assert fill_row(conn)["fill_from"] == stamp(COVERED)


def test_fill_techs_yes_skips_the_question(conn, db_path, services, monkeypatch):
    use_client(monkeypatch, FakeClient(year_pages(), users=both()))

    def no_questions(prompt):
        raise AssertionError("--yes asks nothing")

    monkeypatch.setattr("builtins.input", no_questions)
    assert cli.main(["activity", "fill-techs", "--yes"], db_path=db_path, services=services) == 0
    assert fill_row(conn)["fill_from"] == stamp(COVERED)


def test_fill_techs_leaves_out_a_skipped_tech(conn, db_path, services, monkeypatch, capsys):
    client = use_client(monkeypatch, FakeClient(year_pages(), users=both()))
    assert cli.main(["activity", "fill-techs", "--yes", "--skip", "Sam@Example.com"], db_path=db_path,
                    services=services) == 0
    assert "Every tech's history already starts 2026-07-22, apart from those skipped. Nothing to fill." in         capsys.readouterr().out
    assert client.starts == [] and fill_row(conn) is None


def test_fill_techs_with_nothing_to_fill(conn, db_path, services, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(year_pages(), users=both()))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.main(["activity", "fill-techs"], db_path=db_path, services=services) == 0
    capsys.readouterr()
    assert cli.main(["activity", "fill-techs"], db_path=db_path, services=services) == 0
    assert "Every tech's history already starts 2026-07-22. Nothing to fill." in capsys.readouterr().out


def test_fill_techs_reports_a_failed_fill(conn, db_path, services, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(year_pages(), users=both(), fail_on_page=2))
    assert cli.main(["activity", "fill-techs", "--yes"], db_path=db_path, services=services) == 1
    out = capsys.readouterr().out
    assert "Run the same command again: it carries on from where it stopped." in out


# -- one sync at a time -----------------------------------------------------------------------------------


def running_fill(conn, started):
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, pid, fill_techs)"
                 " VALUES ('backfill', 'running', ?, 4242, ?)", (stamp(started), SAM))
    conn.commit()


def test_a_long_fill_still_blocks_a_sync_while_its_process_lives(conn, tmp_path):
    running_fill(conn, NOW - timedelta(hours=10))
    with pytest.raises(activity_sync.SyncBusy):
        run(conn, FakeClient([]), tmp_path, alive=lambda pid: True)


def test_a_fill_running_past_two_days_is_dead_even_with_a_live_process(conn, tmp_path):
    running_fill(conn, NOW - timedelta(hours=49))
    run(conn, FakeClient([]), tmp_path, alive=lambda pid: True)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM activity_sync ORDER BY id")]
    assert statuses == ["failed", "complete"]


def test_a_fill_whose_process_is_gone_is_dead_at_once(conn, tmp_path):
    running_fill(conn, NOW - timedelta(hours=1))
    run(conn, FakeClient([]), tmp_path, alive=lambda pid: False)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM activity_sync ORDER BY id")]
    assert statuses == ["failed", "complete"]
