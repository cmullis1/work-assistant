"""Pull the team's activity from Zendesk into the local database. Read-only: only GET requests are made.

A sync reads the department-wide ticket event export from where the last sync stopped (or from the
start of the retention period when there is none), keeps what the team did, then adds sign-ins,
ticket subjects and a snapshot of each tech's open tickets. Then it stores each tech's Zendesk id and collects what
1:1 Prep and Trends count (`activity.collect`): the department's call legs, calls, satisfaction ratings and changed
tickets, and each tech's daily aging list. A failed collection read is kept on its kind and never fails the sync,
unless Zendesk rejected the credentials.

A backfill first fills in older activity: when the retention period now reaches back before the stored activity
begins and every tech was found in Zendesk, it reads that stretch (its own position is
`activity_sync.history_cursor_ts`, so a failed fill resumes too) and then marks it read (`history_from`).

A tech fill (`fill`, `activity fill-techs`) is for techs added after the stored activity began: the sync kept nothing
of theirs from before they joined the team, and their shift starts the day they were added. It reads the export from
the start of the stored activity to now for those techs only, with their sign-ins, so they have the same history as
everyone else (its own position is `activity_sync.fill_cursor_ts`, so a failed fill resumes too), then starts each
one's first shift on the first day that history covers and marks the fill done (`fill_from`).

Every step is safe to repeat: actions and sign-ins are inserted idempotently, so overlapping or repeated
syncs never double a count. The saved export position (`activity_sync.cursor_ts`) advances only after a
page's rows are committed, so a sync that dies partway resumes from its last fully stored page.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from work_assistant import regions, retention, settings, timeutil
from work_assistant.activity import collect, collected, store
from work_assistant.activity.classify import classify_event
from work_assistant.activity.model import SignIn
from work_assistant.procutil import pid_alive
from work_assistant.runlog import LOG_DIR
from work_assistant.zendesk.client import ZendeskError

# A process id can be reused after a crash, so a sync "running" for longer than this is treated as dead. A tech fill
# reads up to a year of the export, which Zendesk's rate limit can stretch past that, so it has longer.
STALE_AFTER = timedelta(hours=6)
FILL_STALE_AFTER = timedelta(hours=48)


def stale_after(row: sqlite3.Row) -> timedelta:
    """How long the `activity_sync` row's sync may run before it counts as dead (it needs `fill_techs`)."""
    return FILL_STALE_AFTER if row["fill_techs"] is not None else STALE_AFTER


class SyncBusy(Exception):
    """Another sync is running in a live process."""


def describe_error(exc: BaseException) -> str:
    """Text safe to store and print: a Zendesk error's message names only a status and a path; any other
    error is shown by its type alone, since its message could quote customer content."""
    return str(exc) if isinstance(exc, ZendeskError) else type(exc).__name__


def _mark_dead_syncs(conn: sqlite3.Connection, now: datetime, alive: Callable[[int], bool]) -> None:
    """Fail every `running` sync whose process is gone ("interrupted"); raise SyncBusy if one is really running."""
    live = None
    for row in conn.execute(
            "SELECT id, pid, started_at, fill_techs FROM activity_sync WHERE status = 'running'").fetchall():
        dead = (
            row["pid"] is None
            or row["pid"] == os.getpid()  # this process is starting a sync, so an older row of its own is dead
            or store.parse_iso(row["started_at"]) < now - stale_after(row)
            or not alive(row["pid"])
        )
        if dead:
            conn.execute(
                "UPDATE activity_sync SET status = 'failed', error = 'interrupted', finished_at = ? WHERE id = ?",
                (store.iso(now), row["id"]),
            )
        else:
            live = row
    conn.commit()
    if live is not None:
        raise SyncBusy(f"A team activity sync is already running (started {live['started_at']}).")


def _resume_point(conn: sqlite3.Connection, window_start: int) -> int:
    """Where to read the export from: the newest saved position (even a failed sync's), else the window start."""
    row = conn.execute(
        "SELECT cursor_ts FROM activity_sync WHERE cursor_ts IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return max(row["cursor_ts"], window_start) if row else window_start


# The UPDATE that saves an export position, by its column: the forward read's or the history fill's.
_SAVE_POSITION = {
    column: f"UPDATE activity_sync SET {column} = COALESCE(?, {column}), pages = ? WHERE id = ?"
    for column in ("cursor_ts", "history_cursor_ts", "fill_cursor_ts")
}


def _read_events(conn, client, sync_id: int, team: dict[int, str], start: int, keep_from: int, until: int,
                 progress: dict, column: str = "cursor_ts") -> None:
    """Store the team's actions in [keep_from, until) page by page, saving the export position (in `column`)
    after each page's rows commit."""
    for page in client.ticket_events(start):
        batch = []
        for event in page.get("ticket_events", []):
            # Filter by the event's own time: a page can carry older, late-arriving events.
            if keep_from <= event["timestamp"] < until:
                batch.extend(classify_event(event, team))
        progress["actions"] += store.insert_actions(conn, batch)
        progress["pages"] += 1
        end = page.get("end_time")
        conn.execute(_SAVE_POSITION[column], (None if end is None else min(end, until), progress["pages"], sync_id))
        conn.commit()
        if end is not None and end >= until:
            return  # reached the end of the range


def _read_signins(conn, client, team: dict[int, str], start: datetime, now: datetime) -> None:
    for user_id, email in team.items():
        rows = client.logins(user_id, start, now)
        store.insert_signins(conn, [SignIn(email, store.parse_iso(r["created_at"])) for r in rows])


def _history_start(conn: sqlite3.Connection, window_start: datetime, covered: datetime) -> int:
    """Where a history fill reads from: the newest fill's saved position when that fill never finished, read from
    at least as far back as this one needs (a retention increase since starts over) and stopped inside the gap or
    at its end (every page read, but the sign-ins or the final mark failed: the retry reads one page and goes on to
    the sign-ins); else the window start."""
    first = int(window_start.timestamp())
    row = conn.execute(
        "SELECT history_cursor_ts, history_from, window_start FROM activity_sync"
        " WHERE history_cursor_ts IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if (
        row is not None
        and row["history_from"] is None
        and row["window_start"] <= store.iso(window_start)
        and first <= row["history_cursor_ts"] <= int(covered.timestamp())
    ):
        return row["history_cursor_ts"]
    return first


def _fill_history(conn, client, sync_id: int, team: dict[int, str], window_start: datetime, covered: datetime,
                  progress: dict, log: Callable[[str], None]) -> None:
    """Read the stretch the retention period covers before the stored activity begins, then mark it read. From
    then on the stored activity counts as reaching back to `window_start`, even if the rest of the sync fails."""
    start = _history_start(conn, window_start, covered)
    log(f"Reading older team activity from {store.iso(datetime.fromtimestamp(start, timezone.utc))}"
        f" to {store.iso(covered)}.")
    _read_events(conn, client, sync_id, team, start, int(window_start.timestamp()), int(covered.timestamp()),
                 progress, column="history_cursor_ts")
    _read_signins(conn, client, team, window_start, covered)
    conn.execute("UPDATE activity_sync SET history_from = ? WHERE id = ?", (store.iso(window_start), sync_id))
    conn.commit()
    log(f"Older team activity read back to {store.iso(window_start)}.")


def first_covered_day(conn: sqlite3.Connection, covered: datetime, email: str | None = None) -> date:
    """The first local day whose midnight the stored activity covers (`rules.day_facts` leaves earlier days
    unsynced, and a sync that began reading mid-day leaves that day out): on the app's clock, or with `email` on that
    tech's, each day on the zone it is judged in (`regions.day_zones`)."""
    app_zone = settings.get_timezone(conn)
    zone_on = regions.day_zones(conn, email) if email is not None else lambda day: app_zone
    day = covered.astimezone(timezone.utc).date() - timedelta(days=1)   # early on every clock: none is a day off UTC
    while timeutil.day_window(day, zone_on(day))[0] < covered:
        day += timedelta(days=1)
    return day


def techs_to_fill(conn: sqlite3.Connection) -> list[tuple[str, date]]:
    """The techs on the team whose first shift starts after the first day the stored activity covers on their clock,
    each with that start: added after the activity began, so the sync holds nothing of theirs from before. Empty
    before any sync has completed (the first sync reads everyone's history)."""
    covered = store.synced_from(conn)
    if covered is None:
        return []
    late = []
    for email in settings.team_emails(conn):
        versions = store.shift_versions(conn, email)
        if versions and versions[0].effective_from > first_covered_day(conn, covered, email):
            late.append((email, versions[0].effective_from))
    return late


def _fill_start(conn: sqlite3.Connection, key: str, covered: datetime, now: datetime) -> int:
    """Where a tech fill reads from: the newest unfinished fill's saved position when it was for the same techs from
    the same start and stopped inside the range; else the start of the stored activity."""
    first = int(covered.timestamp())
    row = conn.execute(
        "SELECT fill_cursor_ts, fill_from, window_start FROM activity_sync"
        " WHERE fill_techs = ? AND fill_cursor_ts IS NOT NULL ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if row is not None and row["fill_from"] is None and first <= row["fill_cursor_ts"] <= int(now.timestamp()):
        return row["fill_cursor_ts"]
    return first


def _fill_techs(conn, client, sync_id: int, team: dict[int, str], fill: list[str], now: datetime, progress: dict,
                log: Callable[[str], None]) -> None:
    """Read the named techs' activity and sign-ins from the start of the stored activity to `now`, start each one's
    first shift on the first day it covers, and mark the fill done. A tech not on the team, or not found in
    Zendesk, is skipped with a line saying so."""
    covered = store.synced_from(conn)
    if covered is None:
        log("No history to fill yet: the first sync reads every tech's history.")
        return
    on_team = set(settings.team_emails(conn))
    found = set(team.values())
    wanted = sorted({email.strip().lower() for email in fill})
    for email in wanted:
        if email not in on_team:
            log(f"Not filled, not on the team: {email}")
        elif email not in found:
            log(f"Not filled, not found in Zendesk: {email}")
    techs = {user_id: email for user_id, email in team.items() if email in wanted}
    if not techs:
        return
    key = ",".join(sorted(techs.values()))
    conn.execute("UPDATE activity_sync SET fill_techs = ? WHERE id = ?", (key, sync_id))
    conn.commit()
    start = _fill_start(conn, key, covered, now)
    log(f"Reading {_count(len(techs))} activity from {store.iso(datetime.fromtimestamp(start, timezone.utc))}"
        f" to {store.iso(now)}.")
    _read_events(conn, client, sync_id, techs, start, int(covered.timestamp()), int(now.timestamp()), progress,
                 column="fill_cursor_ts")
    _read_signins(conn, client, techs, covered, now)
    firsts = set()
    for email in techs.values():
        first = first_covered_day(conn, covered, email)
        firsts.add(first)
        versions = store.shift_versions(conn, email)
        if versions and versions[0].effective_from > first:
            store.add_shift_version(conn, replace(versions[0], effective_from=first))
    conn.execute("UPDATE activity_sync SET fill_from = ? WHERE id = ?", (store.iso(covered), sync_id))
    conn.commit()
    log(f"Filled {_count(len(techs))} activity back to {store.iso(covered)}, and started their shifts on"
        f" {' or '.join(day.isoformat() for day in sorted(firsts))}.")


def _count(techs: int) -> str:
    return "1 tech's" if techs == 1 else f"{techs} techs'"


def _read_subjects(conn, client, window_start: datetime) -> None:
    """Fetch subjects for tickets with actions that have none stored yet (or whose subject retention erased
    but which have acted again since). A ticket Zendesk won't return is stored with no subject so it is not asked for again."""
    ids = [
        r["ticket_id"]
        for r in conn.execute(
            "SELECT DISTINCT a.ticket_id FROM activity_actions a"
            " LEFT JOIN activity_tickets t ON t.ticket_id = a.ticket_id"
            " WHERE t.ticket_id IS NULL OR (t.purged = 1 AND a.at >= ?) ORDER BY a.ticket_id",
            (store.iso(window_start),),
        )
    ]
    if not ids:
        return
    found = client.tickets_many(ids)
    conn.executemany(
        "INSERT OR REPLACE INTO activity_tickets (ticket_id, subject, purged) VALUES (?, ?, 0)",
        [(i, (found.get(i) or {}).get("subject")) for i in ids],
    )
    conn.commit()


def _snapshot_open(conn, client, team: dict[int, str], now: datetime) -> None:
    """Replace each tech's open tickets with what Zendesk says now, each with its status and created time (none when
    the search result has none)."""
    captured = store.iso(now)
    for user_id, email in team.items():
        tickets = client.open_assigned(user_id)
        conn.execute("DELETE FROM activity_open WHERE email = ?", (email,))
        conn.executemany(
            "INSERT OR REPLACE INTO activity_open (email, captured_at, ticket_id, status, created)"
            " VALUES (?, ?, ?, ?, ?)",
            [(email, captured, t["id"], t.get("status"),
              store.iso(store.parse_iso(t["created_at"])) if t.get("created_at") else None) for t in tickets],
        )
        # The search result carries each ticket's subject, so the day view can show it without another request.
        # A purged row keeps its erased subject; a result without one adds nothing and changes nothing.
        conn.executemany(
            "INSERT INTO activity_tickets (ticket_id, subject, purged) VALUES (?, ?, 0)"
            " ON CONFLICT(ticket_id) DO UPDATE SET subject = excluded.subject WHERE activity_tickets.purged = 0",
            [(t["id"], t["subject"]) for t in tickets if t.get("subject")],
        )
        conn.commit()


def sync(conn: sqlite3.Connection, client, *, kind: str, now: datetime,
         log: Callable[[str], None], logs_dir: Path = LOG_DIR, alive: Callable[[int], bool] = pid_alive,
         fill: list[str] | None = None, reach_back: datetime | None = None) -> dict:
    """Bring the stored activity up to `now`. Returns status, synced_through, pages, actions, error and collected:
    each collected kind's plain failure reason, None for one that read (`collect.collect`; empty when the sync didn't
    reach the collection).

    `reach_back`, with a backfill, is an instant to read the older stretch back to when it is earlier than the
    retention window (`activity sync --from`): whole-month periods need the days just before a year. Retention is
    unchanged, so its purge still erases those days' content; their counts stay.

    A backfill whose retention window starts before the stored activity first reads that older stretch; its pages
    and actions count in the totals. The stretch is left unread while any tech is not found in Zendesk, since the
    coverage moves back for everyone at once; the forward read goes on as usual. With `fill`, those techs' history
    is read next (`_fill_techs`), before the forward read. A failure is recorded on the sync's
    row and returned (status "failed"); the stored position stays at the last fully stored page. The collection runs
    after the open snapshot and its failures leave the status alone, apart from Zendesk rejecting the credentials,
    which fails the sync. A backfill then reads the collection back (`collect.read_back`) to the earlier of the
    history's start and the stored activity's, after the forward read so a kind read for the first time has its
    place; the hourly sync never does. Raises SyncBusy if another sync is running.
    """
    now = now.astimezone(timezone.utc)
    _mark_dead_syncs(conn, now, alive)
    retention_days = int(settings.get_setting(conn, "retention_days"))
    window_start = now - timedelta(days=retention_days)
    sync_id = conn.execute(
        "INSERT INTO activity_sync (kind, status, started_at, pid, window_start) VALUES (?, 'running', ?, ?, ?)",
        (kind, store.iso(now), os.getpid(), store.iso(window_start)),
    ).lastrowid
    conn.commit()
    progress = {"pages": 0, "actions": 0}
    kinds: dict[str, str | None] = {}
    try:
        emails = settings.team_emails(conn)
        found = client.users_by_email(emails) if emails else {}
        team: dict[int, str] = {}
        for email in emails:
            if email in found:
                team[found[email]["id"]] = email
            else:
                log(f"Not found in Zendesk: {email}")
        if team:
            covered = store.synced_from(conn)
            history_start = window_start if reach_back is None else min(window_start, reach_back)
            if kind == "backfill" and covered is not None and history_start < covered:
                if all(email in found for email in emails):
                    _fill_history(conn, client, sync_id, team, history_start, covered, progress, log)
                else:
                    log("Older team activity not read: not every tech was found in Zendesk.")
            if fill:
                _fill_techs(conn, client, sync_id, team, fill, now, progress, log)
            start = _resume_point(conn, int(window_start.timestamp()))
            log(f"Reading team activity ({kind}) from {store.iso(datetime.fromtimestamp(start, timezone.utc))}.")
            _read_events(conn, client, sync_id, team, start, int(window_start.timestamp()), int(now.timestamp()),
                         progress)
            previous = store.synced_through(conn)
            _read_signins(conn, client, team, max(previous, window_start) if previous else window_start, now)
            _read_subjects(conn, client, window_start)
            _snapshot_open(conn, client, team, now)
            collected.set_agents(conn, {email: user_id for user_id, email in team.items()})
            kinds = collect.collect(conn, client, team, now=now, log=log)
            collect.record_aging(conn, client, team, now=now, log=log)
            if kind == "backfill":
                collect.read_back(conn, client, start=min(history_start, store.synced_from(conn) or history_start),
                                  now=now, log=log)
        else:
            log("No team members found in Zendesk; nothing to sync.")
        retention.purge_expired(conn, now, retention_days, logs_dir)
        conn.execute(
            "UPDATE activity_sync SET status = 'complete', finished_at = ?, synced_through = ?, pages = ? WHERE id = ?",
            (store.iso(datetime.now(timezone.utc)), store.iso(now), progress["pages"], sync_id),
        )
        conn.commit()
        error = None
    except Exception as exc:
        conn.rollback()
        error = describe_error(exc)
        conn.execute(
            "UPDATE activity_sync SET status = 'failed', finished_at = ?, error = ?, pages = ? WHERE id = ?",
            (store.iso(datetime.now(timezone.utc)), error, progress["pages"], sync_id),
        )
        conn.commit()
        log(f"Team activity sync failed: {error}")
    return {
        "status": "failed" if error else "complete",
        "synced_through": store.synced_through(conn),
        "pages": progress["pages"],
        "actions": progress["actions"],
        "error": error,
        "collected": kinds,
    }
