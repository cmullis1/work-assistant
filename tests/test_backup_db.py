"""`work-assistant backup-db` (hosted Team activity design §5 "Backups", rulings R5 and R11): the hosted copy's nightly
database copy, into `backups/` beside the database, named by the day in the config's time zone, and the deletion of
copies older than `--keep-days`; and with `--before-update` the copy update.ps1 takes before it installs, kept apart for
rollback.sh --restore-db until the next update, and like every backup for 14 days at most.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_server_config import HOSTED, write_config
from work_assistant import cli
from work_assistant.db import connect, init_db

# 03:00 UTC on Oct 21 is still Oct 20 (22:00) in America/Chicago, the config's zone.
LATE_EVENING = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def hosted(tmp_path) -> Path:
    return write_config(tmp_path, HOSTED)


def database(tmp_path) -> sqlite3.Connection:
    """The config's database (data/hosted.db), set up, with a retention of 45 days saved. The caller closes it."""
    conn = connect(tmp_path / "data" / "hosted.db")
    init_db(conn)
    conn.execute("INSERT INTO settings (key, value) VALUES ('retention_days', '45')")
    conn.commit()
    return conn


def backup(config: Path, capsys, *extra: str, now: datetime = LATE_EVENING) -> tuple[int, str]:
    code = cli.main(["backup-db", "--config", str(config), *extra],
                    services=cli.Services(anthropic_client=None, now=lambda: now))
    return code, capsys.readouterr().out


def stored_retention(path: Path) -> str:
    copy = sqlite3.connect(path)
    try:
        return copy.execute("SELECT value FROM settings WHERE key = 'retention_days'").fetchone()[0]
    finally:
        copy.close()


def test_backup_copies_the_database_named_by_the_day_in_the_configs_zone(tmp_path, hosted, capsys):
    conn = database(tmp_path)   # still open: its last write is only in the journal (WAL) so far
    try:
        code, out = backup(hosted, capsys)
    finally:
        conn.close()
    target = tmp_path / "data" / "backups" / "work-assistant-20261020.db"
    assert code == 0
    assert sorted(p.name for p in target.parent.iterdir()) == ["work-assistant-20261020.db"]
    assert stored_retention(target) == "45", "the copy holds what the open database had written"
    assert f"Backed up the database to {target}." in out


def test_backup_deletes_copies_older_than_keep_days_and_nothing_else(tmp_path, hosted, capsys):
    database(tmp_path).close()
    folder = tmp_path / "data" / "backups"
    folder.mkdir()
    kept = ("work-assistant-20261006.db",   # 14 days before Oct 20: not older than 14 days
            "notes.txt", "other-20260101.db", "work-assistant-20261301.db", "work-assistant-20261005.db.partial")
    for name in ("work-assistant-20261005.db", "work-assistant-20260901.db", *kept):
        (folder / name).write_text("x", encoding="utf-8")
    code, out = backup(hosted, capsys)
    assert code == 0
    assert sorted(p.name for p in folder.iterdir()) == sorted((*kept, "work-assistant-20261020.db"))
    assert "Deleted 2 backups older than 14 days." in out

    code, out = backup(hosted, capsys, "--keep-days", "3")
    assert code == 0
    assert "work-assistant-20261006.db" not in {p.name for p in folder.iterdir()}
    assert "Deleted 1 backup older than 3 days." in out


def test_a_second_backup_the_same_day_replaces_the_first(tmp_path, hosted, capsys):
    conn = database(tmp_path)
    try:
        backup(hosted, capsys)
        conn.execute("UPDATE settings SET value = '60' WHERE key = 'retention_days'")
        conn.commit()
        code, _ = backup(hosted, capsys, now=LATE_EVENING.replace(hour=4))
    finally:
        conn.close()
    folder = tmp_path / "data" / "backups"
    assert code == 0
    assert sorted(p.name for p in folder.iterdir()) == ["work-assistant-20261020.db"]
    assert stored_retention(folder / "work-assistant-20261020.db") == "60"


def test_backup_without_a_database_says_so_and_creates_nothing(tmp_path, hosted, capsys):
    code, out = backup(hosted, capsys)
    assert code == 0
    assert "There is no database at" in out and "nothing to back up" in out
    assert not (tmp_path / "data").exists()


def test_backup_db_needs_a_config(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["backup-db"])
    assert refused.value.code == 2
    capsys.readouterr()


def test_the_copy_before_an_update_is_kept_apart_until_the_next_update(tmp_path, hosted, capsys):
    """Ruling R11 (1): the copy update.ps1 takes before it installs is the database as the version being replaced left
    it, so rollback.sh --restore-db puts that back. It has a name of its own (the UTC time), the nightly backups taken
    after it leave it alone for 14 days, and the next update's copy replaces it; the nightly copies are left as they
    are."""
    conn = database(tmp_path)
    try:
        code, out = backup(hosted, capsys, "--before-update")
        conn.execute("UPDATE settings SET value = '60' WHERE key = 'retention_days'")   # what the new version did
        conn.commit()
    finally:
        conn.close()
    folder = tmp_path / "data" / "backups"
    first = folder / "before-update-20261021T030000Z.db"
    assert code == 0
    assert sorted(p.name for p in folder.iterdir()) == [first.name], "no nightly copy is made or replaced"
    assert stored_retention(first) == "45"
    assert f"Backed up the database to {first}, the copy a rollback with --restore-db puts back." in out

    nightly = [f"work-assistant-202610{day:02d}.db" for day in range(1, 21)]
    for name in nightly:
        (folder / name).write_text("x", encoding="utf-8")
    code, out = backup(hosted, capsys, now=datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc))   # 14 days on
    assert code == 0
    assert first.is_file(), "the copy kept before an update is kept for 14 days"
    assert not any((folder / name).exists() for name in nightly), "every copy before Oct 21 is past 14 days by Nov 4"
    assert stored_retention(first) == "45"
    assert "Deleted 20 backups older than 14 days." in out and first.name not in out

    code, out = backup(hosted, capsys, "--before-update", now=datetime(2026, 11, 4, 18, 5, 9, tzinfo=timezone.utc))
    second = folder / "before-update-20261104T180509Z.db"
    assert code == 0
    assert sorted(p.name for p in folder.iterdir()) == [second.name, "work-assistant-20261104.db"]
    assert stored_retention(second) == "60"
    assert f"Deleted {first.name}, the copy the previous update kept." in out


def test_the_copy_before_an_update_is_deleted_after_14_days(tmp_path, hosted, capsys):
    """Hosted design §5, "Backups are kept for 14 days" (final review F4): the nightly backup deletes the copy taken
    before an update once the UTC day it was taken is more than --keep-days days old, and says that a rollback can no
    longer put the database back. Until then it stays, so the database can be rolled back for 14 days."""
    database(tmp_path).close()
    folder = tmp_path / "data" / "backups"
    copy = folder / "before-update-20261104T180509Z.db"
    code, _ = backup(hosted, capsys, "--before-update", now=datetime(2026, 11, 4, 18, 5, 9, tzinfo=timezone.utc))
    assert code == 0 and copy.is_file()

    code, out = backup(hosted, capsys, now=datetime(2026, 11, 18, 9, 30, tzinfo=timezone.utc))   # 14 days on
    assert code == 0
    assert copy.is_file(), "not older than 14 days yet"
    assert "Deleted" not in out

    code, out = backup(hosted, capsys, now=datetime(2026, 11, 19, 9, 30, tzinfo=timezone.utc))   # 15 days on
    assert code == 0
    assert sorted(p.name for p in folder.iterdir()) == ["work-assistant-20261118.db", "work-assistant-20261119.db"]
    assert (f"Deleted {copy.name}, the copy taken before an update more than 14 days ago: a rollback can no longer "
            "put the database back.") in out
    assert "backups older than" not in out, "the nightly count is of nightly copies only"

    copy.write_text("x", encoding="utf-8")   # --keep-days shortens it too
    code, out = backup(hosted, capsys, "--keep-days", "3", now=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc))
    assert code == 0 and not copy.exists()
    assert "more than 3 days ago" in out
