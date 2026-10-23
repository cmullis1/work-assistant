"""Storage for shift versions, time off, flag thresholds and the activity synced from Zendesk.

Dates are ISO text. Stored instants are UTC text like 2026-10-19T14:05:00Z, which compares as a string.
"""

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from work_assistant import settings
from work_assistant.activity.model import Action, ShiftVersion, SignIn, Thresholds, TimeOff


def _version(row: sqlite3.Row) -> ShiftVersion:
    days = frozenset(int(d) for d in row["work_days"].split(",") if d != "")
    return ShiftVersion(
        email=row["email"],
        effective_from=date.fromisoformat(row["effective_from"]),
        work_days=days,
        start_min=row["start_min"],
        end_min=row["end_min"],
        active=bool(row["active"]),
    )


def add_shift_version(conn: sqlite3.Connection, v: ShiftVersion) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO shift_versions"
        " (email, effective_from, work_days, start_min, end_min, active)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (v.email, v.effective_from.isoformat(), ",".join(str(d) for d in sorted(v.work_days)),
         v.start_min, v.end_min, 1 if v.active else 0),
    )
    conn.commit()


def shift_versions(conn: sqlite3.Connection, email: str) -> list[ShiftVersion]:
    rows = conn.execute(
        "SELECT * FROM shift_versions WHERE email = ? ORDER BY effective_from", (email,)
    ).fetchall()
    return [_version(r) for r in rows]


def shift_on(conn: sqlite3.Connection, email: str, day: date) -> ShiftVersion | None:
    row = conn.execute(
        "SELECT * FROM shift_versions WHERE email = ? AND effective_from <= ?"
        " ORDER BY effective_from DESC LIMIT 1",
        (email, day.isoformat()),
    ).fetchone()
    return _version(row) if row else None


def remove_shift_versions(conn: sqlite3.Connection, email: str, first: date, last: date) -> None:
    """Delete the tech's versions dated `first` through `last`."""
    conn.execute("DELETE FROM shift_versions WHERE email = ? AND effective_from BETWEEN ? AND ?",
                 (email, first.isoformat(), last.isoformat()))
    conn.commit()


def merge_repeats(conn: sqlite3.Connection, email: str) -> None:
    """Delete each of the tech's versions that repeats the one before it (the same days, hours and whether it is
    active): it changes no day, and a later edit of the earlier one would otherwise stop at it."""
    kept = None
    for version in shift_versions(conn, email):
        same = (version.work_days, version.start_min, version.end_min, version.active)
        if kept == same:
            conn.execute("DELETE FROM shift_versions WHERE email = ? AND effective_from = ?",
                         (email, version.effective_from.isoformat()))
        else:
            kept = same
    conn.commit()


def set_start(conn: sqlite3.Connection, email: str, day: date) -> None:
    """Make `day` the tech's first tracked day: the version in force on it (their first one when `day` is earlier)
    now starts on it, every version before it is deleted, so earlier days are untracked, and repeats are merged.
    The tech must have a version."""
    versions = shift_versions(conn, email)
    in_force = _version_on(versions, day) or versions[0]
    conn.execute("DELETE FROM shift_versions WHERE email = ? AND effective_from < ?", (email, day.isoformat()))
    add_shift_version(conn, replace(in_force, effective_from=day))
    merge_repeats(conn, email)


def add_time_off(conn: sqlite3.Connection, email: str, first: date, last: date, note: str,
                 kind: str = "pto") -> int:
    cur = conn.execute(
        "INSERT INTO time_off (email, first, last, note, kind) VALUES (?, ?, ?, ?, ?)",
        (email, first.isoformat(), last.isoformat(), note, kind),
    )
    conn.commit()
    return cur.lastrowid


def edit_time_off(conn: sqlite3.Connection, id: int, first: date, last: date, note: str, kind: str) -> bool:
    """Change an entry's days, note and kind. Returns False when there is no such entry."""
    cur = conn.execute("UPDATE time_off SET first = ?, last = ?, note = ?, kind = ? WHERE id = ?",
                       (first.isoformat(), last.isoformat(), note, kind, id))
    conn.commit()
    return cur.rowcount > 0


def remove_time_off(conn: sqlite3.Connection, id: int) -> None:
    conn.execute("DELETE FROM time_off WHERE id = ?", (id,))
    conn.commit()


def remove_time_off_day(conn: sqlite3.Connection, email: str, day: date) -> bool:
    """Take `day` out of every time-off entry of `email` that covers it, leaving the other days. A one-day entry
    is deleted, an entry that starts or ends on the day is shortened, and one that has the day in the middle is
    split: the original keeps the days before it and a new entry (same note and kind) gets the days after. The
    pieces that remain keep the entry's kind. Returns whether anything changed."""
    covering = [o for o in time_off(conn, email) if o.first <= day <= o.last]
    for entry in covering:
        if entry.first == entry.last:
            conn.execute("DELETE FROM time_off WHERE id = ?", (entry.id,))
        elif day == entry.first:
            conn.execute("UPDATE time_off SET first = ? WHERE id = ?",
                         ((day + timedelta(days=1)).isoformat(), entry.id))
        elif day == entry.last:
            conn.execute("UPDATE time_off SET last = ? WHERE id = ?",
                         ((day - timedelta(days=1)).isoformat(), entry.id))
        else:
            conn.execute("UPDATE time_off SET last = ? WHERE id = ?",
                         ((day - timedelta(days=1)).isoformat(), entry.id))
            conn.execute("INSERT INTO time_off (email, first, last, note, kind) VALUES (?, ?, ?, ?, ?)",
                         (email, (day + timedelta(days=1)).isoformat(), entry.last.isoformat(), entry.note,
                          entry.kind))
    conn.commit()
    return bool(covering)


def time_off(conn: sqlite3.Connection, email: str | None = None) -> list[TimeOff]:
    if email is None:
        rows = conn.execute("SELECT * FROM time_off ORDER BY first, id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM time_off WHERE email = ? ORDER BY first, id", (email,)
        ).fetchall()
    return [
        TimeOff(r["id"], r["email"], date.fromisoformat(r["first"]),
                date.fromisoformat(r["last"]), r["note"], r["kind"])
        for r in rows
    ]


def on_time_off(conn: sqlite3.Connection, email: str, day: date) -> bool:
    iso = day.isoformat()
    row = conn.execute(
        "SELECT 1 FROM time_off WHERE email = ? AND first <= ? AND last >= ? LIMIT 1",
        (email, iso, iso),
    ).fetchone()
    return row is not None


PLACEHOLDER_DAYS = frozenset({0, 1, 2, 3, 4})   # Sunday to Thursday
PLACEHOLDER_START, PLACEHOLDER_END = 8 * 60, 17 * 60


def apply_team_change(conn: sqlite3.Connection, added: list[str], removed: list[str], *, today: date) -> None:
    """Keep shift versions in step with a change to the team.

    A removed tech gets an inactive version effective tomorrow, with the hours in force then, so their past days
    stay as they were and later days are not theirs; every version dated after tomorrow (a shift change the
    owner set ahead) is kept with its hours but made inactive, so none brings them back when its day comes.
    A tech added with no versions gets the placeholder hours (Sunday to Thursday, 08:00 to 17:00) for the owner
    to edit: from the start of the retention period while no activity sync has completed (the first sync reads
    that far back), else from today, since a sync that already moved on holds nothing earlier and those days
    would read as a false "Sick day". A tech who returns is tracked again from today: with the hours in force
    today (unless their removal has not taken effect yet, when today's version is still active), and every
    version dated after today is made active again, which cancels a removal still ahead and restores the
    shift changes set ahead.
    """
    for email in removed:
        versions = shift_versions(conn, email)
        tomorrow = today + timedelta(days=1)
        in_force = _version_on(versions, tomorrow)
        if in_force is not None and in_force.active:
            add_shift_version(conn, ShiftVersion(email, tomorrow, in_force.work_days, in_force.start_min,
                                                 in_force.end_min, False))
        for later in versions:
            if later.effective_from > tomorrow and later.active:
                add_shift_version(conn, replace(later, active=False))
    for email in added:
        versions = shift_versions(conn, email)
        if not versions:
            first = today if synced_through(conn) is not None else today - timedelta(
                days=int(settings.get_setting(conn, "retention_days")))
            add_shift_version(conn, ShiftVersion(email, first, PLACEHOLDER_DAYS, PLACEHOLDER_START,
                                                 PLACEHOLDER_END, True))
        elif not versions[-1].active:
            current = _version_on(versions, today)
            if current is not None and not current.active:
                add_shift_version(conn, replace(current, effective_from=today, active=True))
            for later in versions:
                if later.effective_from > today and not later.active:
                    add_shift_version(conn, replace(later, active=True))


def _version_on(versions: list[ShiftVersion], day: date) -> ShiftVersion | None:
    """The version in force on `day` (`versions` are oldest first), like `shift_on`."""
    current = None
    for version in versions:
        if version.effective_from > day:
            break
        current = version
    return current


def thresholds(conn: sqlite3.Connection) -> Thresholds:
    return Thresholds(
        late_min=settings.get_setting(conn, "activity_late_minutes"),
        early_min=settings.get_setting(conn, "activity_early_minutes"),
        gap_min=settings.get_setting(conn, "activity_gap_minutes"),
    )


# -- synced activity: actions, sign-ins, ticket subjects and sync bookkeeping ----------------------

def iso(moment: datetime) -> str:
    """UTC text in a fixed-width form, so stored times compare correctly as strings."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def insert_actions(conn: sqlite3.Connection, actions: list[Action]) -> int:
    """Store actions, skipping any already stored (same event, child and tech). Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO activity_actions (event_id, child_id, email, ticket_id, at, kind, old, new)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(a.event_id, a.child_id, a.email, a.ticket_id, iso(a.at), a.kind, a.old, a.new) for a in actions],
    )
    conn.commit()
    return conn.total_changes - before


def insert_signins(conn: sqlite3.Connection, rows: list[SignIn]) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO activity_signins (email, at) VALUES (?, ?)",
        [(r.email, iso(r.at)) for r in rows],
    )
    conn.commit()
    return conn.total_changes - before


def actions_between(conn: sqlite3.Connection, email: str, start_utc: datetime, end_utc: datetime) -> list[Action]:
    """One tech's actions from `start_utc` up to, not including, `end_utc`, oldest first."""
    rows = conn.execute(
        "SELECT * FROM activity_actions WHERE email = ? AND at >= ? AND at < ?"
        " ORDER BY at, event_id, child_id",
        (email, iso(start_utc), iso(end_utc)),
    ).fetchall()
    return [
        Action(email=r["email"], ticket_id=r["ticket_id"], at=parse_iso(r["at"]), kind=r["kind"],
               old=r["old"], new=r["new"], event_id=r["event_id"], child_id=r["child_id"])
        for r in rows
    ]


def signins_between(conn: sqlite3.Connection, email: str, start_utc: datetime, end_utc: datetime) -> list[SignIn]:
    rows = conn.execute(
        "SELECT email, at FROM activity_signins WHERE email = ? AND at >= ? AND at < ? ORDER BY at",
        (email, iso(start_utc), iso(end_utc)),
    ).fetchall()
    return [SignIn(r["email"], parse_iso(r["at"])) for r in rows]


def subjects(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str | None]:
    """Each requested ticket's subject; None when Zendesk would not return it, retention erased it, or it is unknown."""
    found: dict[int, str | None] = {int(i): None for i in ids}
    wanted = list(found)
    for i in range(0, len(wanted), 500):
        chunk = wanted[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT ticket_id, subject FROM activity_tickets WHERE ticket_id IN ({marks})", chunk):
            found[r["ticket_id"]] = r["subject"]
    return found


def latest_sync(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM activity_sync ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def synced_through(conn: sqlite3.Connection) -> datetime | None:
    """The instant the newest complete sync was current to."""
    row = conn.execute(
        "SELECT synced_through FROM activity_sync WHERE status = 'complete' AND synced_through IS NOT NULL"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return parse_iso(row["synced_through"]) if row else None


def synced_from(conn: sqlite3.Connection) -> datetime | None:
    """The earliest instant the stored activity covers: where the first complete sync began reading (the start of
    its retention window). Later syncs carry on from where the one before stopped, so nothing earlier is stored.
    None before any sync has completed. A sync stored before the window was recorded counts back from its start
    by the retention period. A completed history fill (a backfill that read the older stretch, `sync`) moves the
    start back to where it began."""
    row = conn.execute(
        "SELECT window_start, started_at FROM activity_sync WHERE status = 'complete' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    if row["window_start"] is not None:
        start = parse_iso(row["window_start"])
    else:
        start = parse_iso(row["started_at"]) - timedelta(days=int(settings.get_setting(conn, "retention_days")))
    history_from = conn.execute("SELECT MIN(history_from) FROM activity_sync").fetchone()[0]
    if history_from is None:
        return start
    return min(start, parse_iso(history_from))


def purge_activity(conn: sqlite3.Connection, cutoff: datetime) -> int:
    """Erase customer content older than `cutoff` and return how many rows it changed.

    A ticket whose newest action is older than the cutoff loses its subject (and is marked purged);
    a `fields` action older than the cutoff loses its old and new values. Action rows are kept. A subject that
    belongs to no action and no open ticket is erased too.
    """
    limit = iso(cutoff)
    tickets = conn.execute(
        "UPDATE activity_tickets SET subject = NULL, purged = 1 WHERE purged = 0 AND ticket_id IN"
        " (SELECT ticket_id FROM activity_actions GROUP BY ticket_id HAVING MAX(at) < ?)",
        (limit,),
    ).rowcount
    # A subject stored from the open-ticket snapshot has no action to date it: keep it only while the ticket is open.
    tickets += conn.execute(
        "UPDATE activity_tickets SET subject = NULL, purged = 1 WHERE purged = 0"
        " AND ticket_id NOT IN (SELECT ticket_id FROM activity_actions)"
        " AND ticket_id NOT IN (SELECT ticket_id FROM activity_open)"
    ).rowcount
    fields =conn.execute(
        "UPDATE activity_actions SET old = NULL, new = NULL"
        " WHERE kind = 'fields' AND at < ? AND (old IS NOT NULL OR new IS NOT NULL)",
        (limit,),
    ).rowcount
    conn.commit()
    return tickets + fields
