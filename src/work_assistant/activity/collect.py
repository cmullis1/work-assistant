"""The hourly collection inside `activity sync` (Collected calls design, "What is collected"): the department's Talk
call legs and calls, satisfaction ratings and changed tickets, each read forward from where its last read stopped, and
each tech's aging list, recorded once a day on their clock. Everything lands in `activity.collected`'s tables. A
backfill also reads each kind back over the older history (`read_back`), and `count_volumes` counts, read-only, what
a span holds.

Each kind's place is its `collect_state` row (`collected.state`). A Talk kind's position is the export `start_time` to
read from, and the ratings kind's the end of the last window read, both as epoch seconds in text ("1729500000"); the
tickets kind's is the incremental ticket export's `after_cursor`. A position is saved only after a page's rows commit,
so a read that stops partway resumes from its last stored page. A kind never read starts at `now`: its stretch
(`covered_from`) begins there, and the days before it are the history read-back's. Its first read asks from
FIRST_READ_MARGIN before `now`, since Zendesk's incremental exports refuse a start time less than a minute old.
`through` moves to the end a read reached once it reaches the present: `now`, and RATINGS_BEFORE_NOW before it for
ratings.

The history read-back keeps its own place in the kind's `history_position` (the same kinds of value), and when it
reaches where the stretch began, the stretch begins at the read-back's start instead.

Each kind is read on its own: a failure is saved on the kind with its plain reason (`describe_error`) and leaves its
position and `through` as they were, so the sync keeps the activity it stored and the next sync resumes the read.
Zendesk rejecting the credentials (ZendeskAuthError) is raised, so the sync fails as it would on any other read. Every
read is a GET through the read-only client, and the progress lines are fixed text and counts.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from work_assistant import regions, settings
from work_assistant.activity import collected
# `sync` imports this module, so its names are looked up when called, never at import time.
from work_assistant.activity import sync as activity_sync
from work_assistant.activity.collected import State
from work_assistant.activity.store import iso, parse_iso
from work_assistant.oneonone.model import PrepItem
from work_assistant.zendesk.client import ZendeskAuthError

FIRST_READ_MARGIN = timedelta(minutes=2)    # Zendesk's incremental exports refuse a start time less than a minute old
RATINGS_LOOKBACK = timedelta(days=7)        # a rating can change after it is made, so each read looks back this far
RATINGS_BEFORE_NOW = timedelta(minutes=1)   # Zendesk refuses a ratings end_time that hasn't passed
RATINGS_WINDOW = timedelta(days=31)         # the history read-back and the volume count read ratings this far at a time
PROGRESS_EVERY = 1000                       # the history read-back's progress line each time this many more rows read
AGING_AFTER = timedelta(days=7)             # an unsolved ticket created longer ago than this is aging

# What each kind's progress lines call one row and several.
_NAMES = {"legs": ("call leg", "call legs"), "calls": ("call", "calls"),
          "ratings": ("satisfaction rating", "satisfaction ratings"), "tickets": ("changed ticket", "changed tickets")}
_TIMED = ("legs", "calls", "ratings")       # the kinds whose position is an instant; the tickets kind's is a cursor


def _epoch(moment: datetime) -> str:
    return str(int(moment.timestamp()))


def _instant(position: str) -> datetime:
    return datetime.fromtimestamp(int(position), timezone.utc)


def _from(current: State) -> datetime:
    """The instant a timed kind reads on from: its position, or where its stretch begins when it has none."""
    return _instant(current.position) if current.position else current.covered_from


def _talk_moved(rows: list[dict], next_start: datetime | None) -> datetime | None:
    """Where a Talk read goes on from after a page: the next page's start, or after the last page the newest
    `updated_at` it read (the export runs by `updated_at`, so the next read starts there and reads that row again,
    harmlessly); None for a last page with no rows, which keeps the place it had."""
    if next_start is not None:
        return next_start
    return max((parse_iso(row["updated_at"]) for row in rows if row.get("updated_at")), default=None)


def _updated_after(rows: list[dict], moment: datetime) -> bool:
    """Whether any of the rows was updated after `moment` (the incremental exports run by `updated_at`, so the read
    has passed it)."""
    return any(parse_iso(row["updated_at"]) > moment for row in rows if row.get("updated_at"))


def _read_talk(conn: sqlite3.Connection, client, current: State, now: datetime) -> int:
    """Store the Talk export's rows from the kind's position page by page, the position moving after each page
    (`_talk_moved`)."""
    upsert = collected.upsert_legs if current.kind == "legs" else collected.upsert_calls
    stored = 0
    for rows, next_start in client.talk_pages(current.kind, _from(current)):
        stored += upsert(conn, rows)
        moved = _talk_moved(rows, next_start)
        if moved is not None:
            collected.save_state(conn, current.kind, position=_epoch(moved))
    return stored


def _read_ratings(conn: sqlite3.Connection, client, current: State, now: datetime) -> int:
    """Store the good and bad ratings made from RATINGS_LOOKBACK before the last read's end (never before the stretch
    begins) to RATINGS_BEFORE_NOW before `now`, so a rating changed within the lookback follows its latest score. A
    window not yet open (the first read) asks Zendesk nothing."""
    start = max(_from(current) - RATINGS_LOOKBACK, current.covered_from)
    end = now - RATINGS_BEFORE_NOW
    if start >= end:
        return 0
    stored = collected.upsert_ratings(conn, list(client.satisfaction_ratings(start, end)))
    collected.save_state(conn, "ratings", position=_epoch(end))
    return stored


def _read_tickets(conn: sqlite3.Connection, client, current: State, now: datetime) -> int:
    """Store each changed ticket's state (its assignee and when it was solved) page by page, from the saved cursor, or
    before the first page has given one from where the stretch begins (never later than FIRST_READ_MARGIN before
    `now`)."""
    pages = (client.ticket_pages(cursor=current.position) if current.position
             else client.ticket_pages(start=min(current.covered_from, now - FIRST_READ_MARGIN)))
    stored = 0
    for page in pages:
        stored += collected.upsert_ticket_states(conn, page.tickets, page.metric_sets)
        if page.after_cursor:
            collected.save_state(conn, "tickets", position=page.after_cursor)
    return stored


_READS: dict[str, Callable[[sqlite3.Connection, object, State, datetime], int]] = {
    "legs": _read_talk, "calls": _read_talk, "ratings": _read_ratings, "tickets": _read_tickets}


def collect(conn: sqlite3.Connection, client, team: dict[int, str], *, now: datetime,
            log: Callable[[str], None]) -> dict[str, str | None]:
    """Read every kind (`collected.KINDS`) forward from its position to the present, department-wide (`team` names
    the techs the sync found, but every agent's rows are kept). Returns each kind's plain failure reason, None for a
    kind that read. Raises ZendeskAuthError."""
    errors: dict[str, str | None] = {}
    for kind in collected.KINDS:
        one, several = _NAMES[kind]
        log(f"Reading {several}…")
        current = collected.state(conn, kind)
        if current.covered_from is None:   # never read: its stretch begins now, and it reads from just before
            first = {"position": _epoch(now - FIRST_READ_MARGIN)} if kind in _TIMED else {}
            collected.save_state(conn, kind, covered_from=now, **first)
            current = collected.state(conn, kind)
        try:
            stored = _READS[kind](conn, client, current, now)
        except ZendeskAuthError:
            raise
        except Exception as exc:
            errors[kind] = activity_sync.describe_error(exc)
            collected.save_state(conn, kind, error=errors[kind])
            log(f"{several.capitalize()} not read: {errors[kind]}")
            continue
        collected.save_state(conn, kind, through=now - RATINGS_BEFORE_NOW if kind == "ratings" else now, error=None)
        errors[kind] = None
        log(f"{stored} {one if stored == 1 else several} stored.")
    return errors


# -- the history read-back (backfill only) and the volume count --------------------------------------------------


def _back_talk(conn: sqlite3.Connection, client, current: State, start: datetime, until: datetime,
               counted: Callable[[int], None]) -> int:
    """Store the Talk export's rows from the history position (`start` before the first page) page by page, the
    history position moving as the forward read's position does, until a page's next start reaches `until` or the
    export ends."""
    upsert = collected.upsert_legs if current.kind == "legs" else collected.upsert_calls
    begin = _instant(current.history_position) if current.history_position else start
    stored = 0
    for rows, next_start in client.talk_pages(current.kind, begin):
        stored += upsert(conn, rows)
        counted(len(rows))
        moved = _talk_moved(rows, next_start)
        if moved is not None:
            collected.save_state(conn, current.kind, history_position=_epoch(moved))
        if next_start is None or next_start >= until:
            break
    return stored


def _back_ratings(conn: sqlite3.Connection, client, current: State, start: datetime, until: datetime,
                  counted: Callable[[int], None]) -> int:
    """Store the good and bad ratings made from the history position (`start` before the first window) to `until`,
    RATINGS_WINDOW at a time, the history position moving to each window's end."""
    begin = _instant(current.history_position) if current.history_position else start
    stored = 0
    while begin < until:
        end = min(begin + RATINGS_WINDOW, until)
        rows = list(client.satisfaction_ratings(begin, end))
        stored += collected.upsert_ratings(conn, rows)
        counted(len(rows))
        collected.save_state(conn, "ratings", history_position=_epoch(end))
        begin = end
    return stored


def _back_tickets(conn: sqlite3.Connection, client, current: State, start: datetime, until: datetime,
                  counted: Callable[[int], None]) -> int:
    """Store each changed ticket's state page by page from the history position (the export from `start` before the
    first page has given a cursor), the history position moving to each page's cursor, until a page's tickets were
    updated after `until` or the export ends."""
    pages = (client.ticket_pages(cursor=current.history_position) if current.history_position
             else client.ticket_pages(start=start))
    stored = 0
    for page in pages:
        stored += collected.upsert_ticket_states(conn, page.tickets, page.metric_sets)
        counted(len(page.tickets))
        if page.after_cursor:
            collected.save_state(conn, "tickets", history_position=page.after_cursor)
        if _updated_after(page.tickets, until):
            break
    return stored


_BACK = {"legs": _back_talk, "calls": _back_talk, "ratings": _back_ratings, "tickets": _back_tickets}


def _progress(log: Callable[[str], None], several: str) -> Callable[[int], None]:
    """Counts the rows a read-back reads and logs a progress line each time the count passes a PROGRESS_EVERY."""
    read = 0

    def counted(rows: int) -> None:
        nonlocal read
        before, read = read, read + rows
        if read // PROGRESS_EVERY > before // PROGRESS_EVERY:
            log(f"Reading older {several}… {read} so far")

    return counted


def read_back(conn: sqlite3.Connection, client, *, start: datetime, now: datetime,
              log: Callable[[str], None]) -> None:
    """The history read-back (a backfill's, never the hourly sync's): read each kind whose stretch begins after
    `start` back from `start` to where its stretch begins, from where the last read-back stopped, then begin its
    stretch at `start`. Ratings end RATINGS_BEFORE_NOW before `now` at the latest. A kind with no forward position yet
    (never read, or tickets whose first read stored no page) is left for a later read-back, so its forward read never
    starts back at `start`. A failure is saved on the kind as in `collect` and leaves its history position at its last
    stored page, for the next read-back to resume. Raises ZendeskAuthError."""
    for kind in collected.KINDS:
        current = collected.state(conn, kind)
        if current.covered_from is None or current.position is None or current.covered_from <= start:
            continue
        one, several = _NAMES[kind]
        log(f"Reading older {several}…")
        until = min(current.covered_from, now - RATINGS_BEFORE_NOW) if kind == "ratings" else current.covered_from
        try:
            stored = _BACK[kind](conn, client, current, start, until, _progress(log, several))
        except ZendeskAuthError:
            raise
        except Exception as exc:
            reason = activity_sync.describe_error(exc)
            collected.save_state(conn, kind, error=reason)
            log(f"Older {several} not read: {reason}")
            continue
        collected.save_state(conn, kind, covered_from=start, history_position=None, error=None)
        log(f"{stored} older {one if stored == 1 else several} stored.")


def _in_span(row: dict, end: datetime) -> bool:
    return not row.get("updated_at") or parse_iso(row["updated_at"]) <= end


def count_volumes(client, *, start: datetime, end: datetime) -> dict[str, int]:
    """How many call legs, calls, satisfaction ratings and changed tickets the department has from `start` to `end`:
    read (GET only) as the history read-back reads them, stored nowhere, each record counted once however often an
    export returns it, and only those updated (ratings: made) by `end`. `end` must have passed: Zendesk refuses a
    ratings end_time that hasn't."""
    counts: dict[str, int] = {}
    for kind in ("legs", "calls"):
        ids: set[int] = set()
        for rows, next_start in client.talk_pages(kind, start):
            ids.update(row["id"] for row in rows if _in_span(row, end))
            if next_start is None or next_start >= end:
                break
        counts[kind] = len(ids)
    ratings: set[int] = set()
    begin = start
    while begin < end:
        stop = min(begin + RATINGS_WINDOW, end)
        ratings.update(row["id"] for row in client.satisfaction_ratings(begin, stop))
        begin = stop
    counts["ratings"] = len(ratings)
    tickets: set[int] = set()
    for page in client.ticket_pages(start=start):
        tickets.update(ticket["id"] for ticket in page.tickets if _in_span(ticket, end))
        if _updated_after(page.tickets, end):
            break
    counts["tickets"] = len(tickets)
    return counts


def _local_day(conn: sqlite3.Connection, email: str, now: datetime) -> date:
    """The tech's day at `now`: `now` on the clock of their region in force today (`regions.today_for`)."""
    app_today = now.astimezone(ZoneInfo(settings.get_timezone(conn))).date()
    return regions.today_for(conn, email, now, app_today)


def _last_reply(conn: sqlite3.Connection, client, email: str, agent_id: int, ticket_id: int) -> str | None:
    """When the tech last replied publicly on the ticket: the later of their newest public comment Zendesk returns
    and their newest stored reply, or None when there is neither."""
    comments, _ = client.ticket_comments(ticket_id)
    replies = [parse_iso(c["created_at"]) for c in comments if c.get("public") and c.get("author_id") == agent_id]
    newest = conn.execute("SELECT MAX(at) FROM activity_actions WHERE email = ? AND ticket_id = ? AND kind = 'reply'",
                          (email, ticket_id)).fetchone()[0]
    if newest is not None:
        replies.append(parse_iso(newest))
    return iso(max(replies)) if replies else None


def _aging(conn: sqlite3.Connection, client, email: str, agent_id: int, now: datetime) -> list[PrepItem]:
    """The tech's open tickets as the sync last captured them that were created more than AGING_AFTER before `now`,
    oldest first, each with its status and the tech's last reply."""
    rows = conn.execute("SELECT ticket_id, status, created FROM activity_open WHERE email = ? AND created IS NOT NULL",
                        (email,)).fetchall()
    old = sorted((parse_iso(r["created"]), r["ticket_id"], r["status"]) for r in rows
                 if now - parse_iso(r["created"]) > AGING_AFTER)
    return [PrepItem("aging", ticket_id, iso(created), None, status,
                     last_reply=_last_reply(conn, client, email, agent_id, ticket_id))
            for created, ticket_id, status in old]


def record_aging(conn: sqlite3.Connection, client, team: dict[int, str], *, now: datetime,
                 log: Callable[[str], None]) -> None:
    """Record each tech's aging list for their local day at `now`, once: a tech whose day already has one is left
    alone. A tech whose comments can't be read is logged and left unrecorded, so the next sync tries again. Raises
    ZendeskAuthError."""
    log("Recording aging tickets…")
    recorded = 0
    for agent_id, email in team.items():
        day = _local_day(conn, email, now)
        found = collected.aging_on(conn, email, day)
        if found is not None and found[0] == day:
            continue
        try:
            items = _aging(conn, client, email, agent_id, now)
        except ZendeskAuthError:
            raise
        except Exception as exc:
            log(f"Aging tickets not recorded: {activity_sync.describe_error(exc)}")
            continue
        collected.record_aging(conn, email, day, items, now=now)
        recorded += 1
    log(f"{recorded} aging {'list' if recorded == 1 else 'lists'} recorded.")
