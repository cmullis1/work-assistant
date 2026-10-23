"""Storage for what the sync collects for 1:1 Prep and Trends: Talk call legs and calls, good and bad satisfaction
ratings, each changed ticket's state (its assignee and when it was solved), each tech's Zendesk id, each tech's daily
aging list, and where each kind's collection stands.

Every record is stored by its Zendesk id, so a later read of the same record replaces it and repeating a read never
doubles a count. Each upsert commits and returns how many rows it added, changed or removed (a row read again
unchanged is not counted), as `store.insert_actions` returns its new rows. Stored instants are UTC text like
2026-10-19T14:05:00Z (`store.iso`), which compares as a string; days are ISO dates. Nothing here reads Zendesk.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime

from work_assistant.activity.store import iso, parse_iso
from work_assistant.oneonone.count import Call, Leg
from work_assistant.oneonone.model import PrepItem

KINDS = ("legs", "calls", "ratings", "tickets")
SCORES = ("good", "bad")   # the ratings kept; a stored rating whose score is now anything else is deleted

_STATE_FIELDS = ("position", "through", "covered_from", "history_position", "error")
_STATE_TIMES = ("through", "covered_from")
_CHUNK = 500   # ids per IN (...) list, as `store.subjects` reads them


@dataclass(frozen=True)
class State:
    """Where one kind's collection stands (`collect_state`). Every field is None for a kind never read."""
    kind: str
    position: str | None            # the export position after the last page whose rows were stored
    through: datetime | None        # the instant the collection is current to
    covered_from: datetime | None   # where the stretch it has read begins
    history_position: str | None    # the history read-back's export position
    error: str | None               # the plain reason the last read failed; None after one that worked


def _time(text: str | None) -> datetime | None:
    return None if text is None else parse_iso(text)


def state(conn: sqlite3.Connection, kind: str) -> State:
    row = conn.execute("SELECT * FROM collect_state WHERE kind = ?", (kind,)).fetchone()
    if row is None:
        return State(kind, None, None, None, None, None)
    return State(kind=kind, position=row["position"], through=_time(row["through"]),
                 covered_from=_time(row["covered_from"]), history_position=row["history_position"],
                 error=row["error"])


def save_state(conn: sqlite3.Connection, kind: str, **fields) -> None:
    """Set the given fields of `kind`'s state (any of position, through, covered_from, history_position and error;
    None clears one) and leave the others as they are. A kind saved for the first time starts with every field None."""
    unknown = set(fields) - set(_STATE_FIELDS)
    if unknown:
        raise TypeError(f"save_state() got unexpected fields: {', '.join(sorted(unknown))}")
    names = [name for name in _STATE_FIELDS if name in fields]
    values = [iso(fields[name]) if name in _STATE_TIMES and fields[name] is not None else fields[name]
              for name in names]
    columns = ", ".join(("kind", *names))
    marks = ", ".join("?" * (len(names) + 1))
    sets = ", ".join(f"{name} = excluded.{name}" for name in names)
    on_conflict = f"DO UPDATE SET {sets}" if names else "DO NOTHING"
    conn.execute(f"INSERT INTO collect_state ({columns}) VALUES ({marks}) ON CONFLICT(kind) {on_conflict}",
                 (kind, *values))
    conn.commit()


def _upsert_sql(table: str, key: str, columns: tuple[str, ...]) -> str:
    """An insert of one row by `key` that, when the key is stored, replaces its other columns only if one differs (so a
    row read again unchanged makes no change)."""
    names = ", ".join((key, *columns))
    marks = ", ".join("?" * (len(columns) + 1))
    sets = ", ".join(f"{column} = excluded.{column}" for column in columns)
    stored = ", ".join(columns)
    read = ", ".join(f"excluded.{column}" for column in columns)
    return (f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {sets}"
            f" WHERE ({stored}) IS NOT ({read})")


_LEGS = _upsert_sql("talk_legs", "id", ("call_id", "agent_id", "type", "status", "created", "talk"))
_CALLS = _upsert_sql("talk_calls", "id", ("direction", "ticket_id"))
_TICKETS = _upsert_sql("ticket_states", "ticket_id", ("assignee_id", "status", "priority", "tags", "solved_at"))
_AGENTS = (
    "INSERT INTO tech_agents (email, agent_id) VALUES (?, ?)"
    " ON CONFLICT(email) DO UPDATE SET agent_id = excluded.agent_id WHERE agent_id IS NOT excluded.agent_id"
)
# A rating retention already purged keeps no reason or comment when a later read returns it again.
_RATINGS = (
    "INSERT INTO sat_ratings (id, ticket_id, assignee_id, score, created, reason, comment) VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET ticket_id = excluded.ticket_id, assignee_id = excluded.assignee_id,"
    " score = excluded.score, created = excluded.created,"
    " reason = CASE WHEN purged = 1 THEN NULL ELSE excluded.reason END,"
    " comment = CASE WHEN purged = 1 THEN NULL ELSE excluded.comment END"
    " WHERE (ticket_id, assignee_id, score, created) IS NOT"
    " (excluded.ticket_id, excluded.assignee_id, excluded.score, excluded.created)"
    " OR (purged = 0 AND (reason, comment) IS NOT (excluded.reason, excluded.comment))"
)


def _changes(conn: sqlite3.Connection, sql: str, rows: list[tuple]) -> int:
    before = conn.total_changes
    conn.executemany(sql, rows)
    conn.commit()
    return conn.total_changes - before


def upsert_legs(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Store Talk call legs, as the `legs` export (`ZendeskClient.talk_export`) yields them."""
    legs = [Leg.from_api(row) for row in rows]
    return _changes(conn, _LEGS, [(leg.id, leg.call_id, leg.agent_id, leg.type, leg.status, iso(leg.created), leg.talk)
                                  for leg in legs])


def upsert_calls(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Store Talk calls, as the `calls` export yields them."""
    calls = [Call.from_api(row) for row in rows]
    return _changes(conn, _CALLS, [(call.id, call.direction, call.ticket_id) for call in calls])


def upsert_ratings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Store satisfaction ratings as Zendesk returns them: a good or bad one is added or replaced, and a stored one
    whose score is now anything else (a customer can take a rating back) is deleted. Rows are applied in order."""
    before = conn.total_changes
    for row in rows:
        if row.get("score") in SCORES:
            conn.execute(_RATINGS, (row["id"], row.get("ticket_id"), row.get("assignee_id"), row["score"],
                                    iso(parse_iso(row["created_at"])), row.get("reason"), row.get("comment")))
        else:
            conn.execute("DELETE FROM sat_ratings WHERE id = ?", (row["id"],))
    conn.commit()
    return conn.total_changes - before


def upsert_ticket_states(conn: sqlite3.Connection, tickets: list[dict], metric_sets: list[dict]) -> int:
    """Store each ticket's assignee, status, priority and tags, and its solved time from its metric set (none when the
    set has none, or the page carries no set for it: an unsolved or reopened ticket)."""
    solved = {m["ticket_id"]: m.get("solved_at") for m in metric_sets}
    return _changes(conn, _TICKETS, [
        (t["id"], t.get("assignee_id"), t.get("status"), t.get("priority"), json.dumps(list(t.get("tags") or [])),
         iso(parse_iso(solved[t["id"]])) if solved.get(t["id"]) else None)
        for t in tickets
    ])


def set_agents(conn: sqlite3.Connection, by_email: dict[str, int]) -> None:
    """Store each tech's Zendesk user id; techs not named keep theirs."""
    _changes(conn, _AGENTS, list(by_email.items()))


def agents(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["email"]: r["agent_id"] for r in conn.execute("SELECT email, agent_id FROM tech_agents")}


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i : i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def talk_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> tuple[list[Leg], dict[int, Call]]:
    """Every stored agent leg created from `start` up to, not including, `end`, oldest first, and every stored call
    those legs name, by id (a leg whose call isn't stored yet has none). Customer legs are left out: nothing counts
    them (`oneonone.count.tally`), and they are about half the legs."""
    legs: list[Leg] = []
    calls: dict[int, Call] = {}
    for r in conn.execute("SELECT l.*, c.direction, c.ticket_id FROM talk_legs l LEFT JOIN talk_calls c"
                          " ON c.id = l.call_id WHERE l.created >= ? AND l.created < ? AND l.type = 'agent'"
                          " ORDER BY l.created, l.id", (iso(start), iso(end))):
        legs.append(Leg(id=r["id"], call_id=r["call_id"], agent_id=r["agent_id"], type=r["type"], status=r["status"],
                        created=parse_iso(r["created"]), talk=r["talk"]))
        if r["direction"] is not None:   # a stored call always has one
            calls[r["call_id"]] = Call(id=r["call_id"], direction=r["direction"], ticket_id=r["ticket_id"])
    return legs, calls


def ratings_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[tuple[datetime, dict]]:
    """Every stored rating made from `start` up to, not including, `end`, oldest first, with when it was made: each
    as the rating Zendesk returns (`make._read_ratings`), with the reason and comment it was stored with (None once
    retention erased them)."""
    rows = conn.execute("SELECT * FROM sat_ratings WHERE created >= ? AND created < ? ORDER BY created, id",
                        (iso(start), iso(end)))
    return [(parse_iso(r["created"]), {"id": r["id"], "ticket_id": r["ticket_id"], "assignee_id": r["assignee_id"],
                                       "score": r["score"], "created_at": r["created"], "reason": r["reason"],
                                       "comment": r["comment"]})
            for r in rows]


def solved_between(conn: sqlite3.Connection, start: datetime, end: datetime,
                   agent_id: int | None = None) -> list[tuple[datetime, int]]:
    """When and which stored tickets were solved from `start` up to, not including, `end`, oldest first: every
    agent's, or only those assigned to `agent_id`."""
    sql = "SELECT solved_at, ticket_id FROM ticket_states WHERE solved_at >= ? AND solved_at < ?"
    params: list = [iso(start), iso(end)]
    if agent_id is not None:
        sql += " AND assignee_id = ?"
        params.append(agent_id)
    return [(parse_iso(r["solved_at"]), r["ticket_id"])
            for r in conn.execute(sql + " ORDER BY solved_at, ticket_id", params)]


def solved_by(conn: sqlite3.Connection, start: datetime, end: datetime,
              agent_ids: list[int]) -> dict[int, list[tuple[datetime, int]]]:
    """`solved_between` for each of `agent_ids` from one read: when and which stored tickets assigned to each were
    solved from `start` up to, not including, `end`, oldest first (an agent with none has an empty list)."""
    found: dict[int, list[tuple[datetime, int]]] = {agent_id: [] for agent_id in agent_ids}
    for chunk in _chunks(sorted(found)):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute("SELECT solved_at, ticket_id, assignee_id FROM ticket_states WHERE solved_at >= ?"
                            f" AND solved_at < ? AND assignee_id IN ({marks}) ORDER BY solved_at, ticket_id",
                            [iso(start), iso(end), *chunk])
        for r in rows:
            found[r["assignee_id"]].append((parse_iso(r["solved_at"]), r["ticket_id"]))
    return found


def cut(rows: list, start: datetime, end: datetime, when=lambda row: row[0]) -> list:
    """The rows of one read (oldest first by `when`: a leg's `created`, a rating's or a solve's time, the default)
    from `start` up to, not including, `end`: what a read of that stretch alone gives, so many windows inside a span
    are cut from one read of it."""
    return rows[bisect_left(rows, start, key=when):bisect_left(rows, end, key=when)]


def ticket_facts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    """The priority and tags (a list) of each requested ticket that is stored; tickets never read are left out."""
    found: dict[int, dict] = {}
    for chunk in _chunks(sorted({int(i) for i in ids})):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT ticket_id, priority, tags FROM ticket_states WHERE ticket_id IN ({marks})", chunk)
        for r in rows:
            found[r["ticket_id"]] = {"priority": r["priority"], "tags": json.loads(r["tags"])}
    return found


def record_aging(conn: sqlite3.Connection, email: str, day: date, items: list[PrepItem], *, now: datetime) -> None:
    """Store the tech's aging list for their local `day` (aging PrepItems: ticket, created time, status and last
    reply; none for a day without aging tickets), recorded at `now`. A day recorded again is replaced."""
    when = day.isoformat()
    with conn:
        conn.execute("INSERT INTO aging_lists (email, day, recorded_at) VALUES (?, ?, ?)"
                     " ON CONFLICT(email, day) DO UPDATE SET recorded_at = excluded.recorded_at",
                     (email, when, iso(now)))
        conn.execute("DELETE FROM aging_items WHERE email = ? AND day = ?", (email, when))
        conn.executemany(
            "INSERT INTO aging_items (email, day, ticket_id, created, status, last_reply) VALUES (?, ?, ?, ?, ?, ?)",
            [(email, when, item.ticket_id, item.at, item.status, item.last_reply) for item in items])


def aging_on(conn: sqlite3.Connection, email: str, day: date) -> tuple[date, list[PrepItem]] | None:
    """The tech's latest aging list recorded on or before `day`, with the day it was recorded for, oldest ticket
    first; None when none was recorded by then."""
    row = conn.execute("SELECT day FROM aging_lists WHERE email = ? AND day <= ? ORDER BY day DESC LIMIT 1",
                       (email, day.isoformat())).fetchone()
    if row is None:
        return None
    rows = conn.execute("SELECT * FROM aging_items WHERE email = ? AND day = ? ORDER BY created, ticket_id",
                        (email, row["day"]))
    return date.fromisoformat(row["day"]), [PrepItem("aging", r["ticket_id"], r["created"], None, r["status"],
                                                     last_reply=r["last_reply"]) for r in rows]


def aging_size(conn: sqlite3.Connection, email: str, day: date) -> tuple[date, int] | None:
    """The day the tech's latest aging list on or before `day` (the one `aging_on` gives) was recorded for, and how
    many tickets it holds, without reading them; None when none was recorded by then."""
    row = conn.execute("SELECT l.day, COUNT(i.ticket_id) FROM aging_lists l LEFT JOIN aging_items i"
                       " ON i.email = l.email AND i.day = l.day WHERE l.email = ?"
                       " AND l.day = (SELECT MAX(day) FROM aging_lists WHERE email = ? AND day <= ?) GROUP BY l.day",
                       (email, email, day.isoformat())).fetchone()
    return None if row is None else (date.fromisoformat(row[0]), row[1])


def aging_recorded(conn: sqlite3.Connection, email: str, day: date) -> tuple[date, datetime] | None:
    """The day the tech's latest aging list on or before `day` (the one `aging_on` gives) was recorded for, and the
    moment it was recorded; None when none was recorded by then."""
    row = conn.execute("SELECT day, recorded_at FROM aging_lists WHERE email = ? AND day <= ? ORDER BY day DESC"
                       " LIMIT 1", (email, day.isoformat())).fetchone()
    return None if row is None else (date.fromisoformat(row["day"]), parse_iso(row["recorded_at"]))


def aging_counts(conn: sqlite3.Connection, email: str, first: date, last: date) -> dict[date, int]:
    """How many tickets were on the tech's aging list recorded for each of their days from `first` through `last`; a
    day with no list recorded is left out, and one recorded with no aging ticket is 0."""
    rows = conn.execute(
        "SELECT l.day, COUNT(i.ticket_id) FROM aging_lists l"
        " LEFT JOIN aging_items i ON i.email = l.email AND i.day = l.day"
        " WHERE l.email = ? AND l.day >= ? AND l.day <= ? GROUP BY l.day",
        (email, first.isoformat(), last.isoformat()))
    return {date.fromisoformat(day): n for day, n in rows}


def first_aging_day(conn: sqlite3.Connection, email: str | None = None) -> date | None:
    """The first day any tech's aging list was recorded for, or the tech `email`'s; None before any was."""
    if email is None:
        first = conn.execute("SELECT MIN(day) FROM aging_lists").fetchone()[0]
    else:
        first = conn.execute("SELECT MIN(day) FROM aging_lists WHERE email = ?", (email,)).fetchone()[0]
    return None if first is None else date.fromisoformat(first)


def uncovered(conn: sqlite3.Connection, kinds: tuple[str, ...], start: datetime, end: datetime) -> bool:
    """Whether any of `kinds` hasn't been collected over the whole of [start, end]: True unless every kind's covered
    stretch begins on or before `start` and is current to `end` or later."""
    for kind in kinds:
        current = state(conn, kind)
        if current.covered_from is None or current.through is None:
            return True
        if current.covered_from > start or current.through < end:
            return True
    return False


def purge_ratings(conn: sqlite3.Connection, cutoff: datetime) -> int:
    """Erase the reason and comment of every rating made before `cutoff` and mark it purged; its score and times stay.
    Returns how many ratings it changed."""
    changed = conn.execute(
        "UPDATE sat_ratings SET reason = NULL, comment = NULL, purged = 1 WHERE purged = 0 AND created < ?",
        (iso(cutoff),),
    ).rowcount
    conn.commit()
    return changed
