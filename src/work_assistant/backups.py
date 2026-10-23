"""Copies of the hosted copy's database (hosted Team activity design §5, "Backups"; `work-assistant backup-db`), taken
every night by `work-assistant-backup.timer` and before every update by `deploy/update.ps1`.

A nightly copy goes into `backups/` beside the database, named by its day: `work-assistant-YYYYMMDD.db`, the day in the
config's time zone. A second copy the same day replaces the first, so a day keeps its latest copy. Copies more than
`keep_days` days old are then deleted.

The copy taken before an update (`backup-db --before-update`) is kept apart, as `before-update-<UTC time>.db`: it is
the database exactly as the version being replaced left it, which `deploy/linux/rollback.sh --restore-db` puts back
(a nightly copy taken since already holds what the new version wrote). The next update's copy replaces it, and like
every backup it is kept for `keep_days` (14) days: the nightly deletion removes it once the UTC day it was taken is
more than `keep_days` days old, so the database can be rolled back for 14 days after an update.

Every copy is made with SQLite's online backup, so it is whole even while the app or a sync writes, and it includes
what is still only in the journal. It is written beside its final name and renamed into place, so a failed copy never
destroys the one it would replace. Nothing else in the folder is ever deleted.

A change that replaces part of the database first copies it beside itself with `copy_beside`, named by its label and
the UTC time (`<db name>.before-import-<UTC time>.db` before the Team activity import, `before-shifts-import` before
the Teams Shifts import), with a counter after the time for a second copy in the same second. Those copies are the
owner's to keep or delete: nothing here deletes or overwrites them.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FOLDER = "backups"
KEEP_DAYS = 14
BEFORE_UPDATE = "before-update-"
_NAME = re.compile(r"work-assistant-(\d{8})\.db")
_BEFORE_UPDATE_NAME = re.compile(r"before-update-(\d{8})T\d{6}Z\.db")


def name_for(day: date) -> str:
    return f"work-assistant-{day:%Y%m%d}.db"


def before_update_name(moment: datetime) -> str:
    return f"{BEFORE_UPDATE}{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.db"


def _day_of(path: Path) -> date | None:
    """The day a backup's file name gives: a nightly copy's day, or the UTC day a copy before an update was taken. None
    for any other file (or a name that isn't a real day)."""
    match = _NAME.fullmatch(path.name) or _BEFORE_UPDATE_NAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match[1], "%Y%m%d").date()
    except ValueError:
        return None


def _copy(db_path: Path, target: Path) -> None:
    """Copy the database at `db_path` to `target`, in place of any file there only once the copy is whole."""
    target.parent.mkdir(exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(db_path)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    partial.replace(target)


def copy_beside(conn: sqlite3.Connection, label: str) -> Path:
    """Copy the whole database of `conn` beside it, as `<db name>.<label>-<UTC time>.db`, and return the copy. The
    time is to the second, so a later copy in the same second takes a counter (`-2`, `-3`, ...): no copy is ever
    overwritten. The name is claimed by creating the file, so two copies at once can't both take it."""
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])   # main's file
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    count = 1
    while True:
        target = db_file.with_name(f"{db_file.name}.{label}-{stamp}{'' if count == 1 else f'-{count}'}.db")
        try:
            target.touch(exist_ok=False)   # an empty file, which SQLite takes as a new database
            break
        except FileExistsError:
            count += 1
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def back_up(db_path: Path, today: date, keep_days: int = KEEP_DAYS) -> tuple[Path, list[Path]]:
    """Copy the database at `db_path` (which must exist) to `<its folder>/backups/work-assistant-<today>.db`, in place of
    any copy already made today, then delete the backups dated more than `keep_days` days before `today`: nightly
    copies, and a copy taken before an update. Returns the copy and the deleted copies, in name order."""
    folder = Path(db_path).parent / FOLDER
    target = folder / name_for(today)
    _copy(db_path, target)
    oldest_kept = today - timedelta(days=keep_days)
    deleted = []
    for path in sorted(folder.iterdir()):
        day = _day_of(path)
        if day is not None and day < oldest_kept:
            path.unlink()
            deleted.append(path)
    return target, deleted


def back_up_before_update(db_path: Path, now: datetime) -> tuple[Path, list[Path]]:
    """Copy the database at `db_path` (which must exist) to `<its folder>/backups/before-update-<now, UTC>.db`, then
    delete the copy an earlier update kept (any other `before-update-` copy). The nightly copies are left as they are,
    and the nightly deletion (`back_up`) removes this copy once it is more than `keep_days` days old. Returns the copy
    and the deleted copies."""
    folder = Path(db_path).parent / FOLDER
    target = folder / before_update_name(now)
    _copy(db_path, target)
    deleted = []
    for path in sorted(folder.iterdir()):
        if path != target and _BEFORE_UPDATE_NAME.fullmatch(path.name):
            path.unlink()
            deleted.append(path)
    return target, deleted
