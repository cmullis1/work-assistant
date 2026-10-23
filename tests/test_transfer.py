"""Moving the owner's Team activity history from the PC copy to the hosted copy (hosted Team activity plan, Task 4):
`transfer.export_activity` / `import_activity`, their commands (`work-assistant export-activity --out FILE` on the PC,
`work-assistant import-activity FILE --config PATH` on the VM), and the daily task skipping its sync once the PC's
"Sync Team activity" setting is off.

The source is the invented demo database (Call QA and Team activity both), built under tmp_path; nothing here opens
the owner's real database.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_server_config import HOSTED, write_config
from work_assistant import backups, cli, demo, people, regions, settings, transfer
from work_assistant.activity import report
from work_assistant.db import connect, init_db

CALL_QA_TABLES = ("contacts", "reviews", "check_results", "coaching_notes", "runs", "rubric_versions",
                  "run_contacts")
ADMIN = "owner@example.com"


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("demo") / "pc.db"
    demo.build_demo_db(path, now=demo.as_of(date(2026, 10, 17)))
    return path


@pytest.fixture
def pc(demo_db, tmp_path):
    """A copy of the demo database as the PC copy, with a time off note and a sync the PC is running."""
    path = tmp_path / "pc" / "work-assistant.db"
    path.parent.mkdir()
    source = connect(demo_db)
    conn = connect(path)
    source.backup(conn)
    source.close()
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', "
                 "'2026-10-17T11:00:00Z', 4242)")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def vm(tmp_path):
    """The hosted copy's database: set up, with a little history of its own that the import replaces."""
    path = tmp_path / "vm" / "hosted.db"
    conn = connect(path)
    init_db(conn)
    settings.set_settings(conn, {"team": ["stray@example.com"], "retention_days": 30,
                                 "zendesk_subdomain": "acme", "zendesk_email": ADMIN})
    conn.execute("INSERT INTO time_off (email, first, last, note) VALUES ('stray@example.com', '2026-09-01', "
                 "'2026-09-02', 'from before')")
    conn.execute("INSERT INTO activity_signins (email, at) VALUES ('stray@example.com', '2026-09-01T12:00:00Z')")
    conn.commit()
    yield conn
    conn.close()


def tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if not r[0].startswith("sqlite_")}
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))


def stored_settings(conn: sqlite3.Connection, keys=transfer.SETTINGS) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({', '.join('?' * len(keys))})", keys)}


def exported(pc, tmp_path) -> tuple[Path, dict]:
    path = tmp_path / "activity-export.db"
    return path, transfer.export_activity(pc, path)


# -- the export ----------------------------------------------------------------------------------------------


def test_export_contains_only_team_activity_data(pc, tmp_path):
    path, counts = exported(pc, tmp_path)
    assert tables(path) == set(transfer.FILE_TABLES)
    assert not tables(path) & set(CALL_QA_TABLES), "no reviews, contacts, runs or rubric tables"
    assert pc.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] > 0, "the source does have Call QA data"
    out = sqlite3.connect(path)
    try:
        keys = {r[0] for r in out.execute("SELECT key FROM settings")}
        assert keys <= set(transfer.SETTINGS) and "team" in keys and "retention_days" in keys
        assert not keys & {"zendesk_subdomain", "zendesk_email", "budget_limit_usd", "model", "effort"}, \
            "nothing of Call QA's, and nothing naming a login"
        names = dict(out.execute("SELECT email, name FROM tech_names"))
        for table in transfer.TABLES:
            assert out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == counts[table] \
                == pc.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], table
    finally:
        out.close()
    assert counts["activity_actions"] > 0 and counts["shift_versions"] > 0 and counts["time_off"] > 0
    team = settings.team_emails(pc)
    assert set(names) == set(team), "each tech's name travels: on the PC it comes from Call QA's calls"
    assert names == {t["email"]: t["name"] for t in report.techs(pc)}
    assert counts["tech_names"] == len(team) and counts["settings"] == len(keys)


def test_export_refuses_a_file_that_exists(pc, tmp_path):
    path = tmp_path / "already.db"
    path.write_bytes(b"something")
    with pytest.raises(FileExistsError, match="already exists"):
        transfer.export_activity(pc, path)
    assert path.read_bytes() == b"something"


def test_export_into_a_missing_folder_says_so_plainly(pc, tmp_path, capsys):
    """R9: an --out whose folder doesn't exist gets a sentence, not sqlite3's traceback, and leaves nothing."""
    out = tmp_path / "no such folder" / "moving.db"
    with pytest.raises(FileNotFoundError) as refused:
        transfer.export_activity(pc, out)
    assert str(refused.value) == (f"The folder {out.parent} doesn't exist. Create it first, or choose another place "
                                  "for the file.")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--db", str(tmp_path / "pc" / "work-assistant.db"), "export-activity", "--out", str(out)])
    assert stopped.value.code == str(refused.value)
    assert not out.parent.exists()
    capsys.readouterr()


def test_export_command_counts_in_plain_words(pc, tmp_path, capsys):
    """R9: "time off entries", never "entrys"; one of anything is singular."""
    pc.executemany("INSERT INTO time_off (email, first, last, note) VALUES ('x@example.com', ?, ?, '')",
                   [("2026-10-01", "2026-10-01"), ("2026-10-02", "2026-10-02")])
    pc.commit()
    assert cli.main(["--db", str(tmp_path / "pc" / "work-assistant.db"), "export-activity", "--out",
                     str(tmp_path / "moving.db")]) == 0
    said = capsys.readouterr().out
    entries = pc.execute("SELECT COUNT(*) FROM time_off").fetchone()[0]
    assert f"{entries} time off {'entry' if entries == 1 else 'entries'}," in said and "entrys" not in said
    assert cli._plural(1, "time off entry", "time off entries") == "1 time off entry"
    assert cli._plural(2, "time off entry", "time off entries") == "2 time off entries"
    assert cli._plural(2, "tech name") == "2 tech names"


# -- the import ----------------------------------------------------------------------------------------------


def test_import_round_trip_counts_match(pc, vm, tmp_path):
    path, out_counts = exported(pc, tmp_path)
    in_counts = transfer.import_activity(vm, path, admin_email=ADMIN)
    assert in_counts == out_counts
    for table in transfer.TABLES:
        if table == "activity_sync":
            continue
        assert rows(vm, table) == rows(pc, table), table
    assert stored_settings(vm) == stored_settings(pc)
    assert settings.get_setting(vm, "zendesk_subdomain") == "acme", "the VM's own settings stay"
    assert [(t["email"], t["name"]) for t in report.techs(vm)] == [(t["email"], t["name"]) for t in report.techs(pc)], \
        "the VM lists the same techs by the same names, with no calls to take them from"
    for table in CALL_QA_TABLES:
        if table != "rubric_versions":
            assert vm.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_import_marks_a_sync_the_pc_was_running_as_interrupted(pc, vm, tmp_path):
    """The PC's process ids mean nothing on the VM: a sync running on the PC can only be interrupted there."""
    path, _ = exported(pc, tmp_path)
    transfer.import_activity(vm, path, admin_email=ADMIN)
    assert vm.execute("SELECT COUNT(*) FROM activity_sync WHERE status = 'running'").fetchone()[0] == 0
    marked = vm.execute("SELECT status, error FROM activity_sync WHERE pid = 4242").fetchall()
    assert [tuple(r) for r in marked] == [("failed", "interrupted")]
    pc_syncs = [tuple(r) for r in pc.execute("SELECT id, kind, cursor_ts, synced_through FROM activity_sync")]
    assert sorted(tuple(r) for r in vm.execute("SELECT id, kind, cursor_ts, synced_through FROM activity_sync")) \
        == sorted(pc_syncs), "every sync, with where it got to, travels"


def test_import_backs_up_destination_first(pc, vm, tmp_path):
    path, _ = exported(pc, tmp_path)
    vm_file = tmp_path / "vm" / "hosted.db"
    transfer.import_activity(vm, path, admin_email=ADMIN)
    [backup] = list(vm_file.parent.glob("hosted.db.before-import-*.db"))
    assert backup.name.startswith("hosted.db.before-import-") and backup.suffix == ".db"
    old = connect(backup)
    try:
        assert settings.get_setting(old, "team") == ["stray@example.com"]
        assert [r["note"] for r in old.execute("SELECT note FROM time_off")] == ["from before"]
        assert old.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0] == 0
    finally:
        old.close()
    assert "stray@example.com" not in settings.get_setting(vm, "team"), "and the database itself was replaced"


def test_copy_beside_names_the_copy_by_its_label_and_the_utc_time(vm, tmp_path):
    """`backups.copy_beside`, the import's backup and the Teams Shifts import's: the whole database, beside it."""
    copy = backups.copy_beside(vm, "before-shifts-import")
    assert copy.parent == tmp_path / "vm"
    assert re.fullmatch(r"hosted\.db\.before-shifts-import-\d{8}T\d{6}Z\.db", copy.name), copy.name
    old = connect(copy)
    try:
        assert [r["note"] for r in old.execute("SELECT note FROM time_off")] == ["from before"]
    finally:
        old.close()


def test_copy_beside_never_overwrites_a_copy_made_the_same_second(vm, tmp_path, monkeypatch):
    """Two copies with one label in the same second (the name is to the second): each later one takes a counter, so
    no copy is ever overwritten, and each holds the database as it was when it was made."""
    moment = datetime(2026, 10, 21, 17, 30, 5, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(backups, "datetime", Frozen)
    copies = []
    for note in ("first", "second", "third"):
        vm.execute("UPDATE time_off SET note = ?", (note,))
        vm.commit()
        copies.append(backups.copy_beside(vm, "before-shifts-import"))
    stamp = "hosted.db.before-shifts-import-20261021T173005Z"
    assert [copy.name for copy in copies] == [f"{stamp}.db", f"{stamp}-2.db", f"{stamp}-3.db"]
    for copy, note in zip(copies, ("first", "second", "third")):
        old = connect(copy)
        try:
            assert [r["note"] for r in old.execute("SELECT note FROM time_off")] == [note], copy.name
        finally:
            old.close()


def test_import_assigns_roster_to_admin(pc, vm, tmp_path):
    people.add_person(vm, "Owner@Example.com", "tl")   # the admin was added as a TL by mistake
    people.add_person(vm, "lee@example.com", "tl")
    people.set_assignments(vm, "lee@example.com", ["stray@example.com"])
    path, _ = exported(pc, tmp_path)
    transfer.import_activity(vm, path, admin_email="Owner@Example.com")
    everyone = {p["email"]: p for p in people.everyone(vm)}
    assert everyone[ADMIN]["role"] == "admin"
    assert everyone[ADMIN]["techs"] == sorted(settings.team_emails(pc)), "every roster tech, assigned to the admin"
    assert everyone["lee@example.com"]["techs"] == ["stray@example.com"], "other people are left as they were"


def test_regions_stay_on_the_hosted_copy(pc, vm, tmp_path):
    """Regions design: the hosted copy owns regions. The export carries none and the import writes none, so a region
    set on the hosted copy stays through a later import."""
    first, second = settings.team_emails(pc)[:2]
    regions.set_regions(vm, first, [(date(2026, 6, 1), "APAC")])
    regions.set_regions(pc, second, [(date(2026, 6, 1), "EMEA")])
    path, _ = exported(pc, tmp_path)
    assert "tech_regions" not in tables(path)
    transfer.import_activity(vm, path, admin_email=ADMIN)
    assert regions.regions_of(vm, [first, second]) == {first: "APAC", second: "NASA"}


@pytest.mark.parametrize("dropped", ["activity_actions", "time_off", "settings", "tech_names"])
def test_import_refuses_incomplete_file(pc, vm, tmp_path, dropped):
    path, _ = exported(pc, tmp_path)
    out = sqlite3.connect(path)
    out.execute(f"DROP TABLE {dropped}")
    out.commit()
    out.close()
    before = {table: rows(vm, table) for table in (*transfer.TABLES, "settings")}
    with pytest.raises(ValueError, match=f"has no {dropped} table"):
        transfer.import_activity(vm, path, admin_email=ADMIN)
    assert {table: rows(vm, table) for table in (*transfer.TABLES, "settings")} == before, "nothing changed"
    assert not list((tmp_path / "vm").glob("*.before-import-*")), "a refused file isn't even backed up for"
    assert people.everyone(vm) == []


def test_import_refuses_a_missing_or_foreign_file(vm, tmp_path):
    with pytest.raises(ValueError, match="doesn't exist"):
        transfer.import_activity(vm, tmp_path / "nowhere.db", admin_email=ADMIN)
    assert not (tmp_path / "nowhere.db").exists(), "nothing is created in its place"
    other = tmp_path / "notes.txt"
    other.write_text("not a database at all, just some words to fill a page " * 20, encoding="utf-8")
    with pytest.raises(ValueError, match="isn't a Team activity export"):
        transfer.import_activity(vm, other, admin_email=ADMIN)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(ValueError, match="has no activity_actions, activity_signins"):
        transfer.import_activity(vm, empty, admin_email=ADMIN)


# -- 1:1 schedules and the history fill (hosted Team design §5) ---------------------------------------------------

PREP_TABLES = ("prep_periods", "prep_rows", "prep_items")   # what an export from before preps were counted held
HISTORY_COLUMNS = "SELECT id, window_start, cursor_ts, history_cursor_ts, history_from FROM activity_sync"


def test_export_import_round_trip_schedule_and_history_columns(pc, vm, tmp_path):
    """Each tech's 1:1 schedule and where the PC's history fill got to travel."""
    pc.execute("UPDATE activity_sync SET history_cursor_ts = 1760000000, history_from = '2025-10-17T17:00:00Z' "
               "WHERE status = 'complete'")
    pc.commit()
    path, out_counts = exported(pc, tmp_path)
    assert transfer.import_activity(vm, path, admin_email=ADMIN) == out_counts
    schedule = settings.get_setting(pc, "oneonone_schedule")
    assert schedule and settings.get_setting(vm, "oneonone_schedule") == schedule
    assert [tuple(r) for r in vm.execute(HISTORY_COLUMNS)] == [tuple(r) for r in pc.execute(HISTORY_COLUMNS)]
    assert vm.execute("SELECT history_cursor_ts, history_from FROM activity_sync WHERE status = 'complete'") \
        .fetchone()[:] == (1760000000, "2025-10-17T17:00:00Z")


def test_transfer_without_prep_tables(pc, vm, tmp_path):
    """Nothing stores a prep any more, so the export carries no prep tables; a file from an older PC copy that still
    carries them is imported all the same, and its preps are left out."""
    path, counts = exported(pc, tmp_path)
    assert not tables(path) & {*PREP_TABLES, "prep_jobs"} and not set(counts) & {*PREP_TABLES, "prep_jobs"}
    out = sqlite3.connect(path)
    out.execute('CREATE TABLE prep_periods (day TEXT PRIMARY KEY, start TEXT, "end" TEXT)')
    out.execute("INSERT INTO prep_periods VALUES ('2026-10-13', '2026-09-29', '2026-10-12')")
    out.execute("CREATE TABLE prep_rows (day TEXT, email TEXT, calls INTEGER)")
    out.execute("INSERT INTO prep_rows VALUES ('2026-10-13', 'avery.stone@example.com', 7)")
    out.execute("CREATE TABLE prep_items (day TEXT, email TEXT, kind TEXT)")
    out.commit()
    out.close()
    assert transfer.import_activity(vm, path, admin_email=ADMIN) == counts
    assert not tables(tmp_path / "vm" / "hosted.db") & {*PREP_TABLES, "prep_jobs"}
    assert rows(vm, "activity_actions") == rows(pc, "activity_actions")


def history_pc(tmp_path):
    """A PC copy that synced 90 days of Pat's activity at NOW, was moved to a 365-day retention, and whose backfill
    read part of the older stretch before it failed; a refresh is running on it when it exports."""
    from test_activity_sync import NOW, PAT, FakeClient, history_pages, page
    from work_assistant.activity import sync as activity_sync

    path = tmp_path / "pc" / "work-assistant.db"
    path.parent.mkdir()
    conn = connect(path)
    init_db(conn)
    settings.set_settings(conn, {"team": [PAT], "retention_days": 90})
    quiet = {"log": lambda line: None, "logs_dir": tmp_path / "logs"}
    activity_sync.sync(conn, FakeClient([page([], NOW, end_of_stream=True)]), kind="refresh", now=NOW, **quiet)
    settings.set_setting(conn, "retention_days", 365)
    later = NOW + timedelta(hours=1)
    failed = activity_sync.sync(conn, FakeClient(history_pages(), fail_on_page=2), kind="backfill", now=later, **quiet)
    assert failed["status"] == "failed"
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, 4242)",
                 (later.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.commit()
    return conn


def test_first_sync_after_import_neither_restarts_nor_blocks(tmp_path, vm):
    """Review Focus 5: the VM's first backfill after the import goes on from where the PC's unfinished history fill
    stopped and from the PC's forward position, not from the start of the window, and the refresh the PC was running
    doesn't hold it up, even with that process id alive on the VM."""
    from test_activity_sync import NOW, FakeClient, history_pages, ts
    from work_assistant.activity import store
    from work_assistant.activity import sync as activity_sync

    pc_conn = history_pc(tmp_path)
    path, _ = exported(pc_conn, tmp_path)
    pc_conn.close()
    transfer.import_activity(vm, path, admin_email=ADMIN)
    client = FakeClient(history_pages())
    first = NOW + timedelta(hours=2)
    result = activity_sync.sync(vm, client, kind="backfill", now=first, log=lambda line: None,
                                logs_dir=tmp_path / "logs", alive=lambda pid: True)
    assert result["status"] == "complete"
    assert client.starts == [ts(NOW - timedelta(days=200)), ts(NOW)], \
        "the history fill resumes at the PC's position, and the forward read at the PC's cursor"
    assert store.synced_from(vm) == first - timedelta(days=365)


def test_coverage_start_matches_after_import(tmp_path, vm):
    """Review Focus 5: "Team activity starts …" names the same day on the VM as on the PC, for a completed history
    fill, and the VM's first daily sync leaves it there."""
    from test_activity_sync import NOW, FakeClient, history_pages, page
    from work_assistant.activity import store
    from work_assistant.activity import sync as activity_sync

    pc_conn = history_pc(tmp_path)
    quiet = {"log": lambda line: None, "logs_dir": tmp_path / "logs", "alive": lambda pid: False}
    done = activity_sync.sync(pc_conn, FakeClient(history_pages()), kind="backfill", now=NOW + timedelta(hours=2),
                              **quiet)
    assert done["status"] == "complete"
    covered = store.synced_from(pc_conn)
    assert covered == NOW + timedelta(hours=2) - timedelta(days=365), "the fill moved the PC's start back"
    path, _ = exported(pc_conn, tmp_path)
    pc_conn.close()
    transfer.import_activity(vm, path, admin_email=ADMIN)
    assert store.synced_from(vm) == covered
    client = FakeClient([page([], NOW + timedelta(days=1), end_of_stream=True)])
    assert activity_sync.sync(vm, client, kind="daily", now=NOW + timedelta(days=1), **quiet)["status"] == "complete"
    assert store.synced_from(vm) == covered


# -- the commands --------------------------------------------------------------------------------------------


def test_export_and_import_commands(pc, tmp_path, capsys):
    pc_file = tmp_path / "pc" / "work-assistant.db"
    out = tmp_path / "moving.db"
    assert cli.main(["--db", str(pc_file), "export-activity", "--out", str(out)]) == 0
    said = capsys.readouterr().out
    assert f"Exported Team activity to {out}" in said and "delete it once it's imported" in said
    with pytest.raises(SystemExit) as refused:
        cli.main(["--db", str(pc_file), "export-activity", "--out", str(out)])
    assert "already exists" in str(refused.value.code)

    config = write_config(tmp_path, HOSTED)   # its database is data/hosted.db under tmp_path
    assert cli.main(["import-activity", str(out), "--config", str(config)]) == 0
    said = capsys.readouterr().out
    hosted = tmp_path / "data" / "hosted.db"
    [backup] = list(hosted.parent.glob("hosted.db.before-import-*.db"))
    assert f"Imported Team activity from {out}" in said and str(backup) in said
    assert f"assigned to {ADMIN}" in said
    conn = connect(hosted)
    try:
        assert rows(conn, "activity_actions") == rows(pc, "activity_actions")
        assert people.role(conn, ADMIN) == "admin"
    finally:
        conn.close()


def test_import_command_needs_a_config_and_refuses_a_bad_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as usage:
        cli.main(["import-activity", str(tmp_path / "x.db")])
    assert usage.value.code == 2
    config = write_config(tmp_path, HOSTED)
    with pytest.raises(SystemExit) as refused:
        cli.main(["import-activity", str(tmp_path / "x.db"), "--config", str(config)])
    assert "doesn't exist" in str(refused.value.code)
    capsys.readouterr()


# -- the daily task once the PC stops syncing ------------------------------------------------------------------


def test_daily_task_skips_activity_sync_when_disabled(tmp_path, monkeypatch, capsys):
    conn = connect(tmp_path / "work-assistant.db")
    init_db(conn)
    settings.set_setting(conn, "activity_sync_enabled", False)
    conn.close()
    synced = []
    monkeypatch.setattr(cli, "cmd_activity_sync", lambda *args, **kwargs: synced.append(args[1].kind) or 0)
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: (synced.append("review") or ([], [])))
    services = cli.Services(anthropic_client=None, log_dir=tmp_path / "logs")
    assert cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert synced == ["review"], "the review runs; the Team activity sync doesn't"
    assert "Team activity sync is off in Settings" in (tmp_path / "logs" / "scheduled.log").read_text(encoding="utf-8")

    conn = connect(tmp_path / "work-assistant.db")
    settings.set_setting(conn, "activity_sync_enabled", True)
    conn.close()
    synced.clear()
    assert cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert synced == ["review", "daily"], "on again, it syncs"


def test_pc_sync_off_stops_the_daily_sync_and_its_prep_still_counts(tmp_path, monkeypatch):
    """Hosted Team design §5: once the PC's Team activity sync is off, the daily task reads no activity; nothing is
    made, so its 1:1 Prep simply counts from what it has stored. On again, the daily sync runs as before."""
    from fastapi.testclient import TestClient

    from work_assistant.web.app import create_app

    db_file = tmp_path / "work-assistant.db"
    conn = connect(db_file)
    init_db(conn)
    settings.set_settings(conn, {"timezone": "America/Chicago", "team": ["a@example.com"],
                                 "oneonone_schedule": {"a@example.com": {"date": "2026-10-20", "weeks": 2}},
                                 "activity_sync_enabled": False})
    conn.close()
    order = []
    monkeypatch.setattr(cli, "_catch_up", lambda *a, **k: (order.append("review") or ([], [])))
    monkeypatch.setattr(cli, "cmd_activity_sync", lambda conn, args, services=None: order.append("sync") or 0)
    services = cli.Services(anthropic_client=None, log_dir=tmp_path / "logs")
    assert cli.main(["run", "--scheduled"], db_path=db_file, services=services) == 0
    assert order == ["review"], "no sync"
    assert "Team activity sync is off in Settings, so the daily task doesn't read Zendesk activity." in \
        (tmp_path / "logs" / "scheduled.log").read_text(encoding="utf-8")

    started = []
    app = create_app(db_file, run_starter=lambda spec: started.append(spec) or 7, log_dir=tmp_path / "logs",
                     startup_work=False)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert [row["state"] for row in client.get("/api/prep").json()["rows"]] == ["counted"]
    assert client.post("/api/prep/make", json={"email": "a@example.com", "day": "2026-10-06"}).status_code in (404, 405)
    assert started == []

    conn = connect(db_file)
    settings.set_setting(conn, "activity_sync_enabled", True)
    conn.close()
    order.clear()
    assert cli.main(["run", "--scheduled"], db_path=db_file, services=services) == 0
    assert order == ["review", "sync"]


def test_settings_validate_the_cut_over_switches():
    for key, label in (("show_team_activity", "Show Team activity"), ("activity_sync_enabled", "Sync Team activity"),
                       ("show_oneonone", "Show 1:1 Prep")):
        assert settings.validate_changes({key: False}) == ({key: False}, {})
        for bad in ("no", 0, 1, None):
            assert settings.validate_changes({key: bad}) == ({}, {key: f"{label} must be on or off"})
