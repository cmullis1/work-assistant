"""Moving Team activity from the owner's PC copy to the hosted copy (hosted Team activity design, plan Task 4; hosted
Team design §5): `export_activity` writes the PC's Team activity history into a new SQLite file, and `import_activity`
puts that file's contents in place of the hosted copy's.

The file holds only what the Team screens show: the activity tables (`activity_sync` with where each sync and history
fill got to, so the hosted copy's first sync carries on from there), the shifts and time off, the settings those
screens follow (`SETTINGS`, each tech's 1:1 schedule included), and each tech's display name (`tech_names`; on the PC
the names come from Call QA's reviewed calls, which never travel). No 1:1 prep travels: each is counted from the
collected data when asked, and the hosted copy collects its own; a file from an older copy that still holds the
stored preps' tables is imported without them. Nothing from Call QA (calls, reviews, runs, the rubric) and nothing
secret is in it: no setting copied names a credential. It does hold ticket subjects, so it is deleted once imported.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from work_assistant import backups, people, settings
from work_assistant.activity import report

TABLES = ("activity_actions", "activity_signins", "activity_tickets", "activity_open", "activity_sync",
          "shift_versions", "time_off")
SETTINGS = ("team", "timezone", "retention_days", "activity_late_minutes", "activity_early_minutes",
            "activity_gap_minutes", "oneonone_schedule")
NAMES = "tech_names"
# Every table the file must hold: the activity tables, then the settings and the names.
FILE_TABLES = (*TABLES, "settings", NAMES)


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    """The table's column names, each quoted for a statement (so a column named like a keyword reads as a name)."""
    return [f'"{row[1]}"' for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def export_activity(src_conn: sqlite3.Connection, out_path: Path) -> dict[str, int]:
    """Write the Team activity history of `src_conn` into a new SQLite file at `out_path` (FileExistsError if
    something is already there, FileNotFoundError if its folder isn't). Returns how many rows each table of the file
    holds."""
    out_path = Path(out_path)
    if out_path.exists():
        raise FileExistsError(f"{out_path} already exists. Choose a new file name, or delete that file first.")
    if not out_path.parent.is_dir():
        raise FileNotFoundError(f"The folder {out_path.parent} doesn't exist. Create it first, or choose another "
                                "place for the file.")
    counts: dict[str, int] = {}
    out = sqlite3.connect(out_path)
    src_conn.commit()
    src_conn.execute("BEGIN")   # one read transaction: every table as of the same moment, even while a sync writes
    try:
        for table in TABLES:
            create = src_conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                                      (table,)).fetchone()[0]
            out.execute(create)
            names = _columns(src_conn, table)
            columns, marks = ", ".join(names), ", ".join("?" * len(names))
            rows = [tuple(row) for row in src_conn.execute(f"SELECT {columns} FROM {table}")]
            out.executemany(f"INSERT INTO {table} ({columns}) VALUES ({marks})", rows)
            counts[table] = len(rows)
        out.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        stored = [tuple(row) for row in src_conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({', '.join('?' * len(SETTINGS))})", SETTINGS)]
        out.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", stored)
        counts["settings"] = len(stored)
        out.execute(f"CREATE TABLE {NAMES} (email TEXT PRIMARY KEY, name TEXT NOT NULL)")
        named = [(t["email"], t["name"]) for t in report.techs(src_conn) if t["name"] != t["email"]]
        out.executemany(f"INSERT INTO {NAMES} (email, name) VALUES (?, ?)", named)
        counts[NAMES] = len(named)
        out.commit()
    except BaseException:
        out.close()
        out_path.unlink(missing_ok=True)
        raise
    finally:
        src_conn.rollback()   # it only read
    out.close()
    return counts


def import_activity(dst_conn: sqlite3.Connection, in_path: Path, *, admin_email: str) -> dict[str, int]:
    """Replace the Team activity history of `dst_conn` with the file at `in_path` (made by `export_activity`), and
    assign every roster tech to `admin_email`, who is made the admin; nobody else's team changes. ValueError, with
    nothing changed, for a file that isn't a complete export. Before anything changes the whole database is copied
    beside it (`<db>.before-import-<timestamp>.db`); the tables and settings are then replaced in one transaction.
    Returns how many rows each table received."""
    in_path = Path(in_path)
    if not in_path.is_file():
        raise ValueError(f"{in_path} doesn't exist.")
    not_a_database = ValueError(f"{in_path} isn't a Team activity export (not a database file). Nothing was imported.")
    dst_conn.commit()
    try:
        dst_conn.execute("ATTACH DATABASE ? AS imported", (str(in_path),))
    except sqlite3.DatabaseError:
        raise not_a_database from None
    try:
        try:
            found = {row[0] for row in dst_conn.execute(
                "SELECT name FROM imported.sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError:
            raise not_a_database from None
        missing = [table for table in FILE_TABLES if table not in found]
        if missing:
            raise ValueError(f"{in_path} isn't a complete Team activity export: it has no "
                             f"{', '.join(missing)} table{'s' if len(missing) > 1 else ''}. Nothing was imported.")
        backups.copy_beside(dst_conn, "before-import")
        counts: dict[str, int] = {}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with dst_conn:
            for table in (*TABLES, NAMES):
                columns = ", ".join(_columns(dst_conn, table, "imported"))
                dst_conn.execute(f"DELETE FROM main.{table}")
                counts[table] = dst_conn.execute(
                    f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM imported.{table}").rowcount
            # A sync the PC was running when it exported is the PC's process: here it can only be interrupted.
            dst_conn.execute("UPDATE main.activity_sync SET status = 'failed', error = 'interrupted', "
                             "finished_at = ? WHERE status = 'running'", (now,))
            marks = ", ".join("?" * len(SETTINGS))
            dst_conn.execute(f"DELETE FROM main.settings WHERE key IN ({marks})", SETTINGS)
            counts["settings"] = dst_conn.execute(
                f"INSERT INTO main.settings (key, value) SELECT key, value FROM imported.settings "
                f"WHERE key IN ({marks})", SETTINGS).rowcount
    finally:
        dst_conn.execute("DETACH DATABASE imported")
    people.ensure_admin(dst_conn, admin_email)
    people.set_assignments(dst_conn, admin_email, settings.team_emails(dst_conn))
    return counts
