"""SQLite connection and schema."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("data/work-assistant.db")

# Run statuses:
#   fetching               selecting contacts and building requests
#   awaiting_confirmation  paused by the budget guard
#   submitting             set just before batches.create; replaced by submitted in the same commit
#                          that records the batch id
#   submitted              batch id recorded; results not collected yet
#   submission_unknown     create may or may not have reached Anthropic; only the owner clears it
#   complete, failed, cancelled
RUN_STATUSES = (
    "fetching", "awaiting_confirmation", "submitting", "submitted", "submission_unknown",
    "complete", "failed", "cancelled",
)
# Run kinds. A `test` run scores stored calls with another model into reviews that are never current
# (`pipeline.start_test`): it holds no tickets and covers no week.
RUN_KINDS = ("scheduled", "manual", "rereview", "test")

RUNS_TABLE = """CREATE TABLE IF NOT EXISTS {name} (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK(kind IN ({kinds})),
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    rubric_version_id INTEGER NOT NULL REFERENCES rubric_versions,
    status            TEXT NOT NULL CHECK(status IN ({statuses})),
    batch_id          TEXT,
    estimate_usd      REAL,
    actual_usd        REAL,
    contact_count     INTEGER,
    error             TEXT,
    created_at        TEXT NOT NULL,
    finished_at       TEXT,
    excluded_purged   INTEGER NOT NULL DEFAULT 0,
    pid               INTEGER,
    model             TEXT,
    effort            TEXT
);"""


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def _runs_table(name: str) -> str:
    return RUNS_TABLE.format(name=name, kinds=_quoted(RUN_KINDS), statuses=_quoted(RUN_STATUSES))


# Who can use the hosted copy (people.py): the admin, the team leads and the managers, by the email they sign in with
# and the name typed when they were added (names.py; NULL for none). A template, as `runs` is, so an older table whose
# CHECK predates a role is made again from the same text (`_rebuild_people_if_stale`).
PEOPLE_TABLE = """CREATE TABLE IF NOT EXISTS {name} (
    email    TEXT PRIMARY KEY,
    role     TEXT NOT NULL CHECK(role IN ('admin','tl','manager')),
    added_at TEXT NOT NULL,
    name     TEXT
);"""


def _people_table(name: str) -> str:
    return PEOPLE_TABLE.format(name=name)


SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rubric_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL,
    snapshot   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    ticket_id       INTEGER PRIMARY KEY,
    channel         TEXT NOT NULL,
    call_started_at TEXT NOT NULL,
    agent_email     TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    priority        TEXT,
    attribution_ok  INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    purged_at       TEXT
);

{runs_table}

-- The contacts a run covers, fixed when the run selects them (so a paused or interrupted run
-- resumes with exactly that set), and the API cost each has incurred in this run so far.
-- cost_batch_id is the last batch whose cost is included, so collecting a batch twice never
-- counts its cost twice. compared_review_id (test runs only) is the call's shown review when the test
-- chose it, which the test's review is paired with even if that review is replaced meanwhile.
CREATE TABLE IF NOT EXISTS run_contacts (
    run_id        INTEGER NOT NULL REFERENCES runs,
    ticket_id     INTEGER NOT NULL REFERENCES contacts,
    cost_usd      REAL NOT NULL DEFAULT 0,
    cost_batch_id TEXT,
    compared_review_id INTEGER REFERENCES reviews,
    PRIMARY KEY (run_id, ticket_id)
);

-- A test run's reviews are never current (is_current = 0 always) and carry compared_review_id, the
-- shown review they are compared with; every other review has it NULL. call_dropped is the reviewer's
-- 'yes' / 'no' / 'unclear' on whether the call dropped; NULL when the model judged nothing (a review
-- error or a code-only review) or for reviews stored before it was recorded.
CREATE TABLE IF NOT EXISTS reviews (
    review_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES runs,
    ticket_id         INTEGER NOT NULL REFERENCES contacts,
    rubric_version_id INTEGER NOT NULL REFERENCES rubric_versions,
    status            TEXT NOT NULL CHECK(status IN ('ok','error')),
    error             TEXT,
    resolved_on_call  TEXT,
    counted           INTEGER NOT NULL DEFAULT 1,
    not_counted_reason TEXT,
    is_current        INTEGER NOT NULL,
    cost_usd          REAL,
    created_at        TEXT NOT NULL,
    compared_review_id INTEGER REFERENCES reviews,
    model             TEXT,
    effort            TEXT,
    output_tokens     INTEGER,
    call_dropped      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_review_per_ticket
    ON reviews(ticket_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS check_results (
    review_id        INTEGER NOT NULL REFERENCES reviews,
    check_id         TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    decided_by       TEXT NOT NULL CHECK(decided_by IN ('code','model')),
    override_outcome TEXT,
    override_note    TEXT,
    override_at      TEXT,
    needs_recheck    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (review_id, check_id)
);

CREATE TABLE IF NOT EXISTS coaching_notes (
    note_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_email TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shift_versions (
    email          TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    work_days      TEXT NOT NULL,
    start_min      INTEGER NOT NULL,
    end_min        INTEGER NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (email, effective_from)
);

CREATE TABLE IF NOT EXISTS time_off (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    first TEXT NOT NULL,
    last  TEXT NOT NULL,
    note  TEXT NOT NULL DEFAULT '',
    kind  TEXT NOT NULL DEFAULT 'pto'
);

-- Team activity, synced from Zendesk. One row per recorded action; `at` is UTC text in the form
-- 2026-10-19T14:05:00Z so it compares as a string. The action rows outlive retention: only
-- fields.old/new and the ticket subject are customer content, and those are erased.
CREATE TABLE IF NOT EXISTS activity_actions (
    event_id  INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    email     TEXT NOT NULL,
    ticket_id INTEGER NOT NULL,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    old       TEXT,
    new       TEXT,
    PRIMARY KEY (event_id, child_id, email)
);
CREATE INDEX IF NOT EXISTS activity_actions_by_tech ON activity_actions(email, at);
CREATE INDEX IF NOT EXISTS activity_actions_by_ticket ON activity_actions(ticket_id);

CREATE TABLE IF NOT EXISTS activity_signins (
    email TEXT NOT NULL,
    at    TEXT NOT NULL,
    PRIMARY KEY (email, at)
);

-- subject is NULL when Zendesk would not return the ticket, or once retention erased it (purged = 1).
CREATE TABLE IF NOT EXISTS activity_tickets (
    ticket_id INTEGER PRIMARY KEY,
    subject   TEXT,
    purged    INTEGER NOT NULL DEFAULT 0
);

-- The tickets assigned to a tech and not yet solved, as of the sync that captured them. created is the ticket's
-- created time (UTC text), NULL for a row captured before it was kept.
CREATE TABLE IF NOT EXISTS activity_open (
    email       TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    ticket_id   INTEGER NOT NULL,
    status      TEXT,
    created     TEXT,
    PRIMARY KEY (email, ticket_id)
);

-- One row per sync. cursor_ts is the Zendesk export position (epoch seconds) after the last page whose
-- rows were stored; a sync that stops partway resumes from the newest cursor_ts. synced_through is the
-- instant a complete sync was current to. history_cursor_ts is the same position for a backfill's history fill
-- (the older stretch before the first sync's window), and history_from is where a completed fill began reading.
-- fill_techs, fill_cursor_ts and fill_from are the same for a tech fill (`activity fill-techs`): the techs it reads
-- for (their emails, sorted, comma-separated), its export position, and where it began reading once complete.
CREATE TABLE IF NOT EXISTS activity_sync (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK(kind IN ('daily','refresh','backfill')),
    status         TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    cursor_ts      INTEGER,
    synced_through TEXT,
    pages          INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    pid            INTEGER,
    window_start   TEXT,
    history_cursor_ts INTEGER,
    history_from   TEXT,
    fill_techs     TEXT,
    fill_cursor_ts INTEGER,
    fill_from      TEXT
);

-- What the sync collects for 1:1 Prep and Trends (`activity.collected`), department-wide (every agent, not only the
-- team) and stored by Zendesk id, so a later read of the same record replaces it and repeating a read never doubles a
-- count. Times are UTC text like activity_actions.at.
-- Talk call legs and calls, as `oneonone.count` counts them: a leg's agent_id is NULL for a customer leg, status is
-- its completion status, talk its talk time in seconds; a call's ticket_id is NULL when it has none.
CREATE TABLE IF NOT EXISTS talk_legs (
    id       INTEGER PRIMARY KEY,
    call_id  INTEGER NOT NULL,
    agent_id INTEGER,
    type     TEXT NOT NULL,
    status   TEXT NOT NULL,
    created  TEXT NOT NULL,
    talk     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS talk_legs_by_created ON talk_legs(created);

CREATE TABLE IF NOT EXISTS talk_calls (
    id        INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,
    ticket_id INTEGER
);

-- Good and bad satisfaction ratings; a stored rating that a later read finds neither good nor bad is deleted.
-- Retention erases reason and comment (the customer's text) and marks the row purged; the score and times stay, so
-- counts never change.
CREATE TABLE IF NOT EXISTS sat_ratings (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER,
    assignee_id INTEGER,
    score       TEXT NOT NULL CHECK (score IN ('good','bad')),
    created     TEXT NOT NULL,
    reason      TEXT,
    comment     TEXT,
    purged      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sat_ratings_by_created ON sat_ratings(created);

-- Every ticket the incremental ticket export reported changed, as of its latest read: tags is a JSON list and
-- solved_at the solved time from its metric set, NULL while it is unsolved.
CREATE TABLE IF NOT EXISTS ticket_states (
    ticket_id   INTEGER PRIMARY KEY,
    assignee_id INTEGER,
    status      TEXT,
    priority    TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    solved_at   TEXT
);
CREATE INDEX IF NOT EXISTS ticket_states_by_solved ON ticket_states(solved_at);

-- Each tech's Zendesk user id, by email, as the latest sync found it.
CREATE TABLE IF NOT EXISTS tech_agents (
    email    TEXT PRIMARY KEY,
    agent_id INTEGER NOT NULL
);

-- Each tech's aging list, recorded once a day: day is the tech's local date (ISO) and recorded_at when it was
-- recorded. aging_items holds the list's tickets (none for a day the tech had no aging ticket): each with its created
-- time, status and last_reply, the tech's newest public comment on it (NULL when there is none).
CREATE TABLE IF NOT EXISTS aging_lists (
    email       TEXT NOT NULL,
    day         TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (email, day)
);
CREATE INDEX IF NOT EXISTS aging_lists_by_day ON aging_lists(day);

CREATE TABLE IF NOT EXISTS aging_items (
    email      TEXT NOT NULL,
    day        TEXT NOT NULL,
    ticket_id  INTEGER NOT NULL,
    created    TEXT NOT NULL,
    status     TEXT,
    last_reply TEXT,
    PRIMARY KEY (email, day, ticket_id)
);

-- Where each kind's collection stands. position is its export position (saved only after a page's rows commit) and
-- history_position the history read-back's; through is the instant the collection is current to and covered_from
-- where the stretch it has read begins, both UTC text; error is the plain reason the last read failed, NULL after one
-- that worked.
CREATE TABLE IF NOT EXISTS collect_state (
    kind             TEXT PRIMARY KEY CHECK (kind IN ('legs','calls','ratings','tickets')),
    position         TEXT,
    through          TEXT,
    covered_from     TEXT,
    history_position TEXT,
    error            TEXT
);

-- Who can use the hosted copy (people.py; `PEOPLE_TABLE`), which roster techs each team lead (and the admin) is
-- assigned, and the regions each manager picked to see. Only the hosted edition uses them; on the PC they stay empty.
{people_table}

CREATE TABLE IF NOT EXISTS assignments (
    tl_email   TEXT NOT NULL,
    tech_email TEXT NOT NULL,
    PRIMARY KEY (tl_email, tech_email)
);

CREATE TABLE IF NOT EXISTS manager_regions (
    email  TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('NASA','EMEA','APAC')),
    PRIMARY KEY (email, region)
);

-- Each tech's display name (names.py): the name typed when they were added by name, and on the hosted copy, which
-- has no reviewed calls to take names from, the names it received with the PC's Team activity (transfer.py). It
-- comes before the name on their calls wherever a tech is shown.
CREATE TABLE IF NOT EXISTS tech_names (
    email TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

-- Each tech's region (regions.py), dated as a shift is: `region` (NASA, EMEA or APAC) is in force from `since` (an ISO
-- date) until their next change. A tech with no rows is NASA on the app's own zone. The hosted copy's admin sets them;
-- the PC-to-server import never writes them and the export never carries them (transfer.py).
CREATE TABLE IF NOT EXISTS tech_regions (
    email  TEXT NOT NULL,
    since  TEXT NOT NULL,
    region TEXT NOT NULL,
    PRIMARY KEY (email, since)
);

"""
SCHEMA = SCHEMA_TEMPLATE.format(runs_table=_runs_table("runs"), people_table=_people_table("people"))


def connect(path: Path) -> sqlite3.Connection:
    """Open the database with Row access, foreign keys on, and WAL journaling."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if missing, and upgrade older schemas. Safe to call repeatedly."""
    conn.executescript(SCHEMA)
    _add_excluded_purged(conn)
    _add_run_pid(conn)
    _add_review_counted(conn)
    _add_model_and_effort(conn)
    _add_compared_review(conn)
    _add_call_dropped(conn)
    _add_activity_sync_window(conn)
    _add_activity_sync_history(conn)
    _add_activity_sync_fill(conn)
    _add_time_off_kind(conn)
    _add_people_name(conn)
    _add_activity_open_created(conn)
    _drop_stored_preps(conn)
    _rebuild_runs_if_stale(conn)
    _rebuild_people_if_stale(conn)
    conn.commit()


def _add_excluded_purged(conn: sqlite3.Connection) -> None:
    """Add runs.excluded_purged (purged calls a re-review left out) to databases created before it."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    if "excluded_purged" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN excluded_purged INTEGER NOT NULL DEFAULT 0")


def _add_run_pid(conn: sqlite3.Connection) -> None:
    """Add runs.pid (the process that created the run) to databases created before it.

    The app uses it at startup to tell a run still fetching in a live process from one whose
    process died; runs stored before the column existed have none.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    if "pid" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN pid INTEGER")


def _add_review_counted(conn: sqlite3.Connection) -> None:
    """Add reviews.counted and reviews.not_counted_reason (spec §7.3 Not counted) to older databases.

    Reviews stored before the columns existed were all counted, which the default records.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "counted" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
    if "not_counted_reason" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN not_counted_reason TEXT")


def _add_model_and_effort(conn: sqlite3.Connection) -> None:
    """Add the reviewing model and effort to runs and reviews, and reviews.output_tokens, to older databases.

    Every review stored before the model was recorded came from Claude Opus 5.5, so a review without
    one is recorded as Opus (new reviews always record theirs); its effort stays NULL, "not recorded".
    Older runs keep NULL: Opus at the effort setting of their time.
    """
    runs = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    for column in ("model", "effort"):
        if column not in runs:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
    reviews = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
    for column, kind in (("model", "TEXT"), ("effort", "TEXT"), ("output_tokens", "INTEGER")):
        if column not in reviews:
            conn.execute(f"ALTER TABLE reviews ADD COLUMN {column} {kind}")
    conn.execute("UPDATE reviews SET model = 'claude-opus-5-5' WHERE model IS NULL")


def _add_compared_review(conn: sqlite3.Connection) -> None:
    """Add the test-run pairing (reviews.compared_review_id, run_contacts.compared_review_id) to older databases."""
    for table in ("reviews", "run_contacts"):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "compared_review_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN compared_review_id INTEGER REFERENCES reviews")


def _add_call_dropped(conn: sqlite3.Connection) -> None:
    """Add reviews.call_dropped (the reviewer's dropped-call judgment) to older databases.

    Reviews stored before it was recorded keep NULL: "not recorded".
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "call_dropped" not in columns:
        conn.execute("ALTER TABLE reviews ADD COLUMN call_dropped TEXT")


def _add_activity_sync_window(conn: sqlite3.Connection) -> None:
    """Add activity_sync.window_start (the start of the retention window when the sync ran: where a first sync
    reads from) to older databases. Syncs stored before it have none; `activity.store.synced_from` then counts
    back from the sync's start by the retention period."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(activity_sync)")}
    if "window_start" not in columns:
        conn.execute("ALTER TABLE activity_sync ADD COLUMN window_start TEXT")


def _add_activity_sync_history(conn: sqlite3.Connection) -> None:
    """Add activity_sync.history_cursor_ts (the history fill's export position) and history_from (the start of the
    stretch a completed fill covered) to older databases. Syncs stored before them have none: no fill ran."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(activity_sync)")}
    if "history_cursor_ts" not in columns:
        conn.execute("ALTER TABLE activity_sync ADD COLUMN history_cursor_ts INTEGER")
    if "history_from" not in columns:
        conn.execute("ALTER TABLE activity_sync ADD COLUMN history_from TEXT")


def _add_activity_sync_fill(conn: sqlite3.Connection) -> None:
    """Add activity_sync.fill_techs, fill_cursor_ts and fill_from (a tech fill's techs, position and start) to older
    databases. Syncs stored before them have none: no tech fill ran."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(activity_sync)")}
    for column, kind in (("fill_techs", "TEXT"), ("fill_cursor_ts", "INTEGER"), ("fill_from", "TEXT")):
        if column not in columns:
            conn.execute(f"ALTER TABLE activity_sync ADD COLUMN {column} {kind}")


def _add_time_off_kind(conn: sqlite3.Connection) -> None:
    """Add time_off.kind (`pto` or `sick`) to older databases. Every entry made before it stays PTO."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(time_off)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE time_off ADD COLUMN kind TEXT NOT NULL DEFAULT 'pto'")


def _add_people_name(conn: sqlite3.Connection) -> None:
    """Add people.name (the name typed when a TL was added) to older databases. Everyone added before has none."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
    if "name" not in columns:
        conn.execute("ALTER TABLE people ADD COLUMN name TEXT")


def _add_activity_open_created(conn: sqlite3.Connection) -> None:
    """Add activity_open.created (each open ticket's created time) to older databases. Rows captured before it keep
    NULL."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(activity_open)")}
    if "created" not in columns:
        conn.execute("ALTER TABLE activity_open ADD COLUMN created TEXT")


def _drop_stored_preps(conn: sqlite3.Connection) -> None:
    """Drop the stored 1:1 preps and prep jobs of databases from before preps were counted on demand (Collected calls
    design, "What goes away"): every period they hold is counted again from the collected data, and nothing makes a
    prep any more. A table's index goes with it."""
    for table in ("prep_items", "prep_rows", "prep_periods", "prep_jobs"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _rebuild_runs_if_stale(conn: sqlite3.Connection) -> None:
    """Rebuild `runs` when its CHECK constraints predate the current status or kind lists.

    SQLite cannot alter a CHECK constraint, so this follows SQLite's documented table rebuild:
    foreign keys off, copy into a new table, drop the old one, rename, verify, foreign keys on.
    Every column (the earlier upgrades in `init_db` have already added any missing ones), every row
    and every run_id is kept, so references from reviews and run_contacts stay valid.
    """
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'").fetchone()[0]
    if all(f"'{value}'" in sql for value in (*RUN_STATUSES, *RUN_KINDS)):
        return
    columns = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(runs)"))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            "BEGIN;\n"
            + _runs_table("runs_new").replace("IF NOT EXISTS ", "")
            + f"\nINSERT INTO runs_new ({columns}) SELECT {columns} FROM runs;\n"
            "DROP TABLE runs;\n"
            "ALTER TABLE runs_new RENAME TO runs;\n"
            "COMMIT;"
        )
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError(f"foreign key check failed after migrating runs: {len(problems)} rows")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _rebuild_people_if_stale(conn: sqlite3.Connection) -> None:
    """Rebuild `people` when its CHECK predates the manager role (it allowed only admin and tl), as the server's
    database has it. SQLite cannot alter a CHECK constraint, so this follows `_rebuild_runs_if_stale`: copy into a new
    table, drop the old one, rename. Every column (`_add_people_name` has already added `name`) and every row is kept.
    Nothing references `people`, so foreign keys stay on."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'people'").fetchone()[0]
    if "'manager'" in sql:
        return
    columns = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(people)"))
    conn.commit()
    conn.executescript(
        "BEGIN;\n"
        + _people_table("people_new").replace("IF NOT EXISTS ", "")
        + f"\nINSERT INTO people_new ({columns}) SELECT {columns} FROM people;\n"
        "DROP TABLE people;\n"
        "ALTER TABLE people_new RENAME TO people;\n"
        "COMMIT;"
    )
