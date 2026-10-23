import sqlite3
from pathlib import Path

import pytest

from work_assistant.db import DB_PATH, connect, init_db

EXPECTED_TABLES = {
    "settings",
    "rubric_versions",
    "contacts",
    "runs",
    "run_contacts",
    "reviews",
    "check_results",
    "coaching_notes",
    "shift_versions",
    "time_off",
    "activity_actions",
    "activity_signins",
    "activity_tickets",
    "activity_open",
    "activity_sync",
    "talk_legs",
    "talk_calls",
    "sat_ratings",
    "ticket_states",
    "tech_agents",
    "aging_lists",
    "aging_items",
    "collect_state",
    "people",
    "assignments",
    "manager_regions",
    "tech_names",
    "tech_regions",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def test_init_creates_tables(conn):
    assert _tables(conn) == EXPECTED_TABLES


def test_init_is_idempotent(conn):
    before = len(_tables(conn))
    init_db(conn)
    assert len(_tables(conn)) == before


def test_connect_sets_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert DB_PATH == Path("data/work-assistant.db")


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO rubric_versions(created_at, note, snapshot) VALUES ('2026-01-01T00:00:00Z','n','{}')"
    )
    conn.execute(
        "INSERT INTO contacts(ticket_id, channel, call_started_at, agent_email, agent_name,"
        " attribution_ok, snapshot, fetched_at) VALUES (1,'voice','2026-01-01T00:00:00Z','a@x.com','A',1,'{}','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO runs(kind, window_start, window_end, rubric_version_id, status, created_at)"
        " VALUES ('manual','2026-01-01','2026-01-07',1,'fetching','2026-01-08T00:00:00Z')"
    )


def _review(conn: sqlite3.Connection, is_current: int) -> None:
    conn.execute(
        "INSERT INTO reviews(run_id, ticket_id, rubric_version_id, status, is_current, created_at)"
        " VALUES (1,1,1,'ok',?, '2026-01-08T00:00:00Z')",
        (is_current,),
    )


def test_only_one_current_review_per_ticket(conn):
    _seed(conn)
    _review(conn, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _review(conn, 1)


def test_non_current_reviews_may_repeat(conn):
    _seed(conn)
    _review(conn, 1)
    _review(conn, 0)
    _review(conn, 0)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reviews(run_id, ticket_id, rubric_version_id, status, is_current, created_at)"
            " VALUES (99,99,99,'ok',1,'x')"
        )


OLD_RUNS_TABLE = """CREATE TABLE runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK(kind IN ('scheduled','manual','rereview')),
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    rubric_version_id INTEGER NOT NULL REFERENCES rubric_versions,
    status            TEXT NOT NULL CHECK(status IN
        ('fetching','awaiting_confirmation','submitted','complete','failed','cancelled')),
    batch_id          TEXT,
    estimate_usd      REAL,
    actual_usd        REAL,
    contact_count     INTEGER,
    error             TEXT,
    created_at        TEXT NOT NULL,
    finished_at       TEXT
)"""


def test_init_migrates_old_runs_status_check(tmp_path: Path):
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    old_schema = SCHEMA.replace(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS runs ("):SCHEMA.index("-- The contacts")],
                                OLD_RUNS_TABLE + ";\n\n")
    c.executescript(old_schema)
    _seed(c)
    _review(c, 1)
    c.execute("UPDATE runs SET batch_id = 'b1', estimate_usd = 1.5 WHERE run_id = 1")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE runs SET status = 'submission_unknown' WHERE run_id = 1")

    init_db(c)
    run = c.execute("SELECT * FROM runs WHERE run_id = 1").fetchone()
    assert (run["status"], run["batch_id"], run["estimate_usd"]) == ("fetching", "b1", 1.5)
    for status in ("submitting", "submission_unknown"):
        c.execute("UPDATE runs SET status = ? WHERE run_id = 1", (status,))
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("SELECT run_id FROM reviews").fetchone()[0] == 1
    c.execute(
        "INSERT INTO runs(kind, window_start, window_end, rubric_version_id, status, created_at)"
        " VALUES ('manual','a','b',1,'fetching','c')"
    )
    assert c.execute("SELECT MAX(run_id) FROM runs").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO reviews(run_id, ticket_id, rubric_version_id, status, is_current, created_at)"
                  " VALUES (99,1,1,'ok',0,'x')")
    init_db(c)  # idempotent once migrated
    assert _tables(c) == EXPECTED_TABLES
    c.close()


def _review_columns(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(reviews)")}


def test_reviews_have_counted_columns(conn):
    columns = _review_columns(conn)
    assert columns["counted"]["notnull"] == 1 and columns["counted"]["dflt_value"] == "1"
    assert "not_counted_reason" in columns
    _seed(conn)
    _review(conn, 1)
    row = conn.execute("SELECT counted, not_counted_reason FROM reviews").fetchone()
    assert (row["counted"], row["not_counted_reason"]) == (1, None)


def test_init_upgrades_existing_reviews_with_counted_columns(tmp_path: Path):
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    old_schema = SCHEMA.replace("    counted           INTEGER NOT NULL DEFAULT 1,\n", "").replace(
        "    not_counted_reason TEXT,\n", "")
    assert "counted" not in old_schema
    c.executescript(old_schema)
    _seed(c)
    _review(c, 1)
    c.commit()
    assert "counted" not in _review_columns(c)

    init_db(c)
    columns = _review_columns(c)
    assert "counted" in columns and "not_counted_reason" in columns
    row = c.execute("SELECT counted, not_counted_reason FROM reviews").fetchone()
    assert (row["counted"], row["not_counted_reason"]) == (1, None)  # existing reviews count
    init_db(c)  # idempotent once upgraded
    c.close()


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def test_model_and_effort_columns(conn):
    runs, reviews = _columns(conn, "runs"), _columns(conn, "reviews")
    assert all(runs[c]["notnull"] == 0 for c in ("model", "effort"))
    assert all(reviews[c]["notnull"] == 0 for c in ("model", "effort", "output_tokens"))
    assert reviews["output_tokens"]["type"] == "INTEGER"


def test_migration_backfills_model(tmp_path: Path):
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    for table, column in (("runs", "model"), ("runs", "effort"), ("reviews", "model"), ("reviews", "effort"),
                          ("reviews", "output_tokens")):
        c.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    _seed(c)
    _review(c, 1)
    _review(c, 0)
    c.commit()

    init_db(c)
    assert {"model", "effort"} <= set(_columns(c, "runs"))
    run = c.execute("SELECT model, effort FROM runs").fetchone()
    assert (run["model"], run["effort"]) == (None, None)   # an older run: Opus at the effort of its time
    reviews = c.execute("SELECT model, effort, output_tokens FROM reviews").fetchall()
    assert [tuple(r) for r in reviews] == [("claude-opus-5-5", None, None)] * 2
    init_db(c)  # idempotent once migrated
    assert [tuple(r) for r in c.execute("SELECT model, effort, output_tokens FROM reviews")] == [
        ("claude-opus-5-5", None, None)] * 2
    c.close()


def test_migration_adds_call_dropped(tmp_path: Path):
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.execute("ALTER TABLE reviews DROP COLUMN call_dropped")
    _seed(c)
    _review(c, 1)
    c.commit()

    init_db(c)
    assert _columns(c, "reviews")["call_dropped"]["type"] == "TEXT"
    assert [r["call_dropped"] for r in c.execute("SELECT call_dropped FROM reviews")] == [None]  # not recorded
    init_db(c)  # idempotent once migrated
    assert "call_dropped" in _columns(c, "reviews")
    c.close()


def test_status_rebuild_keeps_model_and_effort(tmp_path: Path):
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    old_schema = SCHEMA.replace(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS runs ("):SCHEMA.index("-- The contacts")],
                                OLD_RUNS_TABLE + ";\n\n")
    c.executescript(old_schema)
    c.execute("ALTER TABLE runs ADD COLUMN model TEXT")
    c.execute("ALTER TABLE runs ADD COLUMN effort TEXT")
    _seed(c)
    c.execute("UPDATE runs SET model = 'claude-sonnet-5-5', effort = 'high'")
    c.commit()
    init_db(c)
    run = c.execute("SELECT model, effort FROM runs").fetchone()
    assert (run["model"], run["effort"]) == ("claude-sonnet-5-5", "high")
    c.close()


def test_migration_adds_the_activity_sync_window(tmp_path: Path):
    """An activity_sync table made before the window was recorded gets the column; its rows keep NULL."""
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.execute("ALTER TABLE activity_sync DROP COLUMN window_start")
    c.execute("INSERT INTO activity_sync (kind, status, started_at) VALUES ('daily', 'complete', '2026-10-17T11:00:00Z')")
    c.commit()
    init_db(c)
    assert _columns(c, "activity_sync")["window_start"]["notnull"] == 0
    assert c.execute("SELECT window_start FROM activity_sync").fetchone()[0] is None
    init_db(c)  # idempotent once upgraded
    c.close()


def test_migration_adds_the_activity_sync_history_columns(tmp_path: Path):
    """An activity_sync table made before the history fill gets both columns; its rows keep NULL."""
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.execute("ALTER TABLE activity_sync DROP COLUMN history_cursor_ts")
    c.execute("ALTER TABLE activity_sync DROP COLUMN history_from")
    c.execute("INSERT INTO activity_sync (kind, status, started_at) VALUES ('daily', 'complete', '2026-10-17T11:00:00Z')")
    c.commit()
    init_db(c)
    columns = _columns(c, "activity_sync")
    assert columns["history_cursor_ts"]["notnull"] == 0 and columns["history_from"]["notnull"] == 0
    assert tuple(c.execute("SELECT history_cursor_ts, history_from FROM activity_sync").fetchone()) == (None, None)
    init_db(c)  # idempotent once upgraded
    c.close()


def test_migration_adds_the_activity_open_created_time(tmp_path: Path):
    """An activity_open table made before created times were kept gets the column; its rows keep NULL."""
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.execute("ALTER TABLE activity_open DROP COLUMN created")
    c.execute("INSERT INTO activity_open (email, captured_at, ticket_id, status)"
              " VALUES ('a@x.com', '2026-10-21T11:00:00Z', 40, 'open')")
    c.commit()
    init_db(c)
    assert _columns(c, "activity_open")["created"]["notnull"] == 0
    assert c.execute("SELECT created FROM activity_open").fetchone()[0] is None
    init_db(c)  # idempotent once upgraded
    c.close()


def test_migration_adds_the_time_off_kind_and_old_entries_stay_pto(tmp_path: Path):
    """A time_off table made before kinds existed gets the column; every old entry is PTO, nothing else changes."""
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.execute("ALTER TABLE time_off DROP COLUMN kind")
    c.execute("INSERT INTO time_off (email, first, last, note) VALUES ('a@x.com', '2026-10-12', '2026-10-14', 'trip')")
    c.commit()
    init_db(c)
    assert _columns(c, "time_off")["kind"]["notnull"] == 1
    row = c.execute("SELECT * FROM time_off").fetchone()
    assert tuple(row) == (1, "a@x.com", "2026-10-12", "2026-10-14", "trip", "pto")
    init_db(c)  # idempotent once upgraded
    assert c.execute("SELECT COUNT(*) FROM time_off").fetchone()[0] == 1
    c.close()


# The stored 1:1 prep tables as the last release before preps were counted on demand had them (2026-10-21).
OLD_PREP_TABLES = """CREATE TABLE prep_periods (
  day TEXT PRIMARY KEY, start TEXT NOT NULL, "end" TEXT NOT NULL,
  made_at TEXT NOT NULL, made_as TEXT NOT NULL CHECK (made_as IN ('scheduled', 'manual', 'history')),
  queue INTEGER, failures TEXT NOT NULL DEFAULT '{}', details_purged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE prep_rows (
  day TEXT NOT NULL, email TEXT NOT NULL,
  calls INTEGER, declined_missed INTEGER, unreachable INTEGER, talk_seconds INTEGER, solved INTEGER,
  ratings_good INTEGER, ratings_bad INTEGER, aging INTEGER, pto INTEGER, sick INTEGER,
  failures TEXT NOT NULL DEFAULT '{}', made_at TEXT, made_as TEXT, queue INTEGER, PRIMARY KEY (day, email));
CREATE TABLE prep_items (
  day TEXT NOT NULL, email TEXT NOT NULL, kind TEXT NOT NULL,
  ticket_id INTEGER, at TEXT, talk_seconds INTEGER, status TEXT,
  priority TEXT, tags TEXT, score TEXT, reason TEXT, comment TEXT, last_reply TEXT);
CREATE INDEX prep_items_by_tech ON prep_items(day, email);
CREATE TABLE prep_jobs (
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
  pid INTEGER, started_at TEXT NOT NULL, finished_at TEXT, progress TEXT, error TEXT);"""


def test_init_db_drops_stored_preps(tmp_path: Path):
    """A database from before 1:1 preps were counted on demand: the stored preps and prep jobs are dropped (every
    period they hold is counted again from the collected data), and everything else stays as it was."""
    c = connect(tmp_path / "old.db")
    init_db(c)
    c.executescript(OLD_PREP_TABLES)
    c.execute('INSERT INTO prep_periods (day, start, "end", made_at, made_as, queue)'
              " VALUES ('2026-10-20', '2026-10-06', '2026-10-19', '2026-10-20T11:04:00Z', 'scheduled', 565)")
    c.execute("INSERT INTO prep_rows (day, email, calls) VALUES ('2026-10-20', 'a@x.com', 7)")
    c.execute("INSERT INTO prep_jobs (day, status, started_at) VALUES ('2026-10-20', 'complete', '2026-10-20T11:00Z')")
    c.execute("INSERT INTO time_off (email, first, last, note) VALUES ('a@x.com', '2026-10-12', '2026-10-14', 'trip')")
    c.execute("INSERT INTO settings (key, value) VALUES ('team', '[\"a@x.com\"]')")
    c.commit()
    assert {"prep_periods", "prep_rows", "prep_items", "prep_jobs"} <= _tables(c)

    init_db(c)
    assert _tables(c) == EXPECTED_TABLES
    assert "prep_items_by_tech" not in {r["name"] for r in c.execute("SELECT name FROM sqlite_master")}
    assert c.execute("SELECT note FROM time_off").fetchone()[0] == "trip"
    assert c.execute("SELECT value FROM settings WHERE key = 'team'").fetchone()[0] == '["a@x.com"]'
    init_db(c)   # idempotent once dropped
    assert _tables(c) == EXPECTED_TABLES
    c.close()


# The people table as the server has it before managers (2026-10-21): only admins and team leads. The first shape is
# the one it was made with; the second is after `_add_people_name` gave it the name column.
OLD_PEOPLE_TABLES = {
    "before names": """CREATE TABLE people (
    email    TEXT PRIMARY KEY,
    role     TEXT NOT NULL CHECK(role IN ('admin','tl')),
    added_at TEXT NOT NULL
)""",
    "with names": """CREATE TABLE people (
    email    TEXT PRIMARY KEY,
    role     TEXT NOT NULL CHECK(role IN ('admin','tl')),
    added_at TEXT NOT NULL,
    name     TEXT
)""",
}


@pytest.mark.parametrize("shape", sorted(OLD_PEOPLE_TABLES))
def test_old_people_table_is_rebuilt_for_managers(tmp_path: Path, shape: str):
    """The update on the server's database (Review Focus 3): the old CHECK refuses a manager, so the table is rebuilt;
    both people are still there with their roles, dates and names, their teams stay, and adding a manager works."""
    from work_assistant import people
    from work_assistant.db import SCHEMA

    c = connect(tmp_path / "old.db")
    c.executescript(SCHEMA)
    c.executescript("DROP TABLE people;\n" + OLD_PEOPLE_TABLES[shape] + ";")
    c.execute("INSERT INTO people (email, role, added_at) VALUES ('owner@x.com', 'admin', '2026-10-20T09:00:00Z')")
    c.execute("INSERT INTO people (email, role, added_at) VALUES ('lee@x.com', 'tl', '2026-10-20T09:05:00Z')")
    c.execute("INSERT INTO assignments (tl_email, tech_email) VALUES ('lee@x.com', 'avery@x.com')")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO people (email, role, added_at) VALUES ('m@x.com', 'manager', '2026-10-21T09:00:00Z')")
    c.rollback()
    if shape == "with names":
        c.execute("UPDATE people SET name = 'Lee Park' WHERE email = 'lee@x.com'")
        c.commit()

    init_db(c)

    lee_name = "Lee Park" if shape == "with names" else None
    assert [tuple(r) for r in c.execute("SELECT email, role, added_at, name FROM people ORDER BY email")] == [
        ("lee@x.com", "tl", "2026-10-20T09:05:00Z", lee_name),
        ("owner@x.com", "admin", "2026-10-20T09:00:00Z", None),
    ]
    assert [tuple(r) for r in c.execute("SELECT tl_email, tech_email FROM assignments")] == [("lee@x.com", "avery@x.com")]
    people.add_person(c, "m@x.com", "manager", "Pat Lane")
    assert tuple(c.execute("SELECT role, name FROM people WHERE email = 'm@x.com'").fetchone()) == ("manager", "Pat Lane")
    assert "'manager'" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'people'").fetchone()[0]
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO people (email, role, added_at) VALUES ('x@x.com', 'boss', '2026-10-21T09:00:00Z')")
    c.rollback()

    init_db(c)  # idempotent once rebuilt: everyone stays
    assert c.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
    assert _tables(c) == EXPECTED_TABLES
    c.close()
