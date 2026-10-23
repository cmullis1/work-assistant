"""The hourly collection inside `activity sync` (`activity.collect`; Collected calls design, "What is collected"): the
department's call legs, calls, satisfaction ratings and changed tickets read forward from each kind's saved position,
each tech's aging list recorded once a day on their clock, and who is who. A failed collection read keeps its reason
on its kind and never fails the sync or loses its activity; Zendesk rejecting the credentials still fails the sync.

The app's zone is Chicago (CDT, UTC-5); Manila is UTC+8."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from collect_fakes import CollectFake
from test_activity_sync import FakeClient, event, page, run, stamp, ts

from work_assistant import regions
from work_assistant.activity import collect, collected, store
from work_assistant.activity.model import Action
from work_assistant.activity.sync import describe_error
from work_assistant.oneonone.model import PrepItem
from work_assistant.settings import set_setting
from work_assistant.zendesk.client import TicketPage, ZendeskAuthError, ZendeskError

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)   # 07:00 in Chicago, 20:00 in Manila
LATER = NOW + timedelta(hours=1)
TECH, TECH_ID, OTHER_ID = "a@x.com", 11, 99
TEAM = {TECH_ID: TECH}
EVERY_KIND_READ = {kind: None for kind in collected.KINDS}


@pytest.fixture(autouse=True)
def team(conn):
    set_setting(conn, "team", [TECH])
    set_setting(conn, "retention_days", 90)
    set_setting(conn, "timezone", "America/Chicago")


def leg(id, call_id, *, at, updated=None, agent_id=TECH_ID):
    return {"id": id, "call_id": call_id, "agent_id": agent_id, "type": "agent", "completion_status": "completed",
            "created_at": stamp(at), "talk_time": 60, "updated_at": stamp(updated or at)}


def call(id, ticket_id, *, updated):
    return {"id": id, "direction": "inbound", "ticket_id": ticket_id, "updated_at": stamp(updated)}


def rating(id, score, *, at):
    return {"id": id, "ticket_id": 700 + id, "assignee_id": TECH_ID, "score": score, "created_at": stamp(at),
            "reason": None, "comment": ""}


def solved_page(ticket_id, solved_at, cursor, end_of_stream=True):
    return TicketPage([{"id": ticket_id, "assignee_id": TECH_ID, "status": "solved", "priority": "normal", "tags": []}],
                      [{"ticket_id": ticket_id, "solved_at": stamp(solved_at)}], cursor, end_of_stream)


def comment(author_id, at, public=True):
    return {"author_id": author_id, "public": public, "created_at": stamp(at)}


def collect_at(conn, client, now, lines=None):
    return collect.collect(conn, client, TEAM, now=now, log=(lines.append if lines is not None else lambda line: None))


# -- reading forward --------------------------------------------------------------------------------------------


def test_first_collect_starts_now(conn):
    """A kind never read starts at `now`: its stretch begins there and is current to it. Its first read asks from
    FIRST_READ_MARGIN before `now`, since Zendesk's incremental exports refuse a start time less than a minute old.
    Ratings ask for nothing yet, since no rating made after `now` can be read before it has passed, and are current
    to RATINGS_BEFORE_NOW before `now`, the end a ratings read reaches."""
    client = CollectFake(
        legs=[([leg(1, 101, at=NOW - timedelta(minutes=10), updated=NOW)], None)],
        calls=[([call(101, 9001, updated=NOW)], None)],
        ticket_export=[solved_page(9001, NOW - timedelta(minutes=3), "after-1")],
        ratings=[rating(1, "good", at=NOW - timedelta(minutes=30))])
    lines: list[str] = []
    assert collect_at(conn, client, NOW, lines) == EVERY_KIND_READ

    first = NOW - collect.FIRST_READ_MARGIN
    for kind in collected.KINDS:
        state = collected.state(conn, kind)
        through = NOW - collect.RATINGS_BEFORE_NOW if kind == "ratings" else NOW
        assert (state.covered_from, state.through, state.error) == (NOW, through, None), kind
    assert {kind: collected.state(conn, kind).position for kind in collected.KINDS} == {
        "legs": str(ts(NOW)), "calls": str(ts(NOW)), "ratings": str(ts(first)), "tickets": "after-1"}
    assert client.talk_reads == [("legs", first), ("calls", first)]
    assert client.ticket_reads == [{"start": first, "cursor": None}]
    assert client.rating_windows == []

    legs, calls = collected.talk_between(conn, NOW - timedelta(hours=1), NOW)
    assert [one.id for one in legs] == [1] and list(calls) == [101]
    assert collected.solved_between(conn, NOW - timedelta(hours=1), NOW) == [(NOW - timedelta(minutes=3), 9001)]
    assert lines == ["Reading call legs…", "1 call leg stored.", "Reading calls…", "1 call stored.",
                     "Reading satisfaction ratings…", "0 satisfaction ratings stored.",
                     "Reading changed tickets…", "1 changed ticket stored."]


def test_forward_read_resumes_from_position(conn):
    """A Talk kind's position is its next page's start, and after the last page the newest `updated_at` read; the
    tickets kind's is the export's after_cursor, and ratings' the end of the last window read."""
    turn = NOW + timedelta(minutes=20)
    newest = NOW + timedelta(minutes=50)
    first = CollectFake(
        legs=[([leg(1, 101, at=NOW + timedelta(minutes=5))], turn),
              ([leg(2, 102, at=NOW + timedelta(minutes=30), updated=newest)], None)],
        calls=[([call(101, 9001, updated=NOW + timedelta(minutes=40))], None)],
        ticket_export=[solved_page(9001, NOW, "after-1", end_of_stream=False), solved_page(9002, NOW, "after-2")])
    collect_at(conn, first, NOW)
    assert collected.state(conn, "legs").position == str(ts(newest))
    assert collected.state(conn, "tickets").position == "after-2"

    second = CollectFake()
    assert collect_at(conn, second, LATER) == EVERY_KIND_READ
    assert second.talk_reads == [("legs", newest), ("calls", NOW + timedelta(minutes=40))]
    assert second.ticket_reads == [{"start": None, "cursor": "after-2"}]
    assert second.rating_windows == [(NOW, LATER - collect.RATINGS_BEFORE_NOW)]
    for kind in collected.KINDS:
        state = collected.state(conn, kind)
        through = LATER - collect.RATINGS_BEFORE_NOW if kind == "ratings" else LATER
        assert (state.covered_from, state.through) == (NOW, through), kind
    assert collected.state(conn, "legs").position == str(ts(newest)), "an empty last page keeps the position"
    assert collected.state(conn, "tickets").position == "cursor-0"
    assert collected.state(conn, "ratings").position == str(ts(LATER - collect.RATINGS_BEFORE_NOW))


def test_a_failed_page_keeps_the_position_of_the_last_stored_page(conn):
    turn = NOW - timedelta(minutes=20)
    error = ZendeskError("Zendesk returned HTTP 500 for /api/v2/channels/voice/stats/incremental/legs.json")
    collect_at(conn, CollectFake(legs=[([leg(1, 101, at=NOW - timedelta(minutes=30))], turn), error]), NOW)
    legs = collected.state(conn, "legs")
    assert (legs.position, legs.through, legs.error) == (str(ts(turn)), None, describe_error(error))
    assert [one.id for one in collected.talk_between(conn, NOW - timedelta(hours=1), NOW)[0]] == [1]

    again = CollectFake()
    collect_at(conn, again, LATER)
    assert again.talk_reads[0] == ("legs", turn)
    legs = collected.state(conn, "legs")
    assert (legs.covered_from, legs.through, legs.error) == (NOW, LATER, None)


def test_rating_changed_within_lookback_follows(conn):
    """Each read looks RATINGS_LOOKBACK back from where the last one ended (never before the collection began), so a
    rating made good and later changed to bad is stored bad."""
    day1 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    made = day1 + timedelta(hours=6)
    client = CollectFake(ratings=[rating(1, "good", at=made)])
    collect_at(conn, client, day1)
    noon1 = day1 + timedelta(hours=12)
    collect_at(conn, client, noon1)
    assert [r["score"] for _, r in collected.ratings_between(conn, day1, noon1)] == ["good"]

    client.ratings = [rating(1, "bad", at=made)]
    day4 = day1 + timedelta(days=3, hours=12)
    collect_at(conn, client, day4)
    assert client.rating_windows[-1] == (day1, day4 - collect.RATINGS_BEFORE_NOW)
    assert [r["score"] for _, r in collected.ratings_between(conn, day1, day4)] == ["bad"]

    day12 = day1 + timedelta(days=11)
    collect_at(conn, client, day12)
    assert client.rating_windows[-1][0] == day1, "a lookback never reaches before the collection began"
    day13 = day12 + timedelta(days=1)
    collect_at(conn, client, day13)
    assert client.rating_windows[-1] == (day12 - collect.RATINGS_BEFORE_NOW - collect.RATINGS_LOOKBACK,
                                         day13 - collect.RATINGS_BEFORE_NOW)


# -- inside the sync ----------------------------------------------------------------------------------------------


def test_calls_failure_keeps_activity(conn, tmp_path):
    """A failed collection read is saved on its kind with its plain reason and leaves the kind's position and
    `through` as they were; the sync completes with its activity stored, and the next sync resumes the read."""
    first = FakeClient([page([event(1, 5, NOW - timedelta(hours=2), author_id=TECH_ID)], NOW - timedelta(hours=1),
                             end_of_stream=True)], users={TECH: TECH_ID},
                       legs=[([leg(1, 101, at=NOW - timedelta(hours=3))], None)])
    assert run(conn, first, tmp_path, now=NOW)["collected"] == EVERY_KIND_READ
    before = collected.state(conn, "legs")

    error = ZendeskError("Zendesk returned HTTP 500 for /api/v2/channels/voice/stats/incremental/legs.json")
    failing = FakeClient([page([event(2, 6, LATER - timedelta(minutes=30), author_id=TECH_ID)],
                               LATER - timedelta(minutes=10), end_of_stream=True)], users={TECH: TECH_ID}, legs=[error])
    lines: list[str] = []
    result = run(conn, failing, tmp_path, now=LATER, lines=lines)
    assert result["status"] == "complete" and result["error"] is None
    assert result["collected"] == {**EVERY_KIND_READ, "legs": describe_error(error)}
    assert result["actions"] == 1 and store.latest_sync(conn)["status"] == "complete"
    assert conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0] == 2
    legs = collected.state(conn, "legs")
    assert legs.error == describe_error(error)
    assert (legs.position, legs.through) == (before.position, NOW)
    assert collected.state(conn, "calls").through == LATER, "the other kinds still read"
    assert f"Call legs not read: {describe_error(error)}" in lines

    healthy = FakeClient([], users={TECH: TECH_ID})
    after = LATER + timedelta(hours=1)
    assert run(conn, healthy, tmp_path, now=after)["collected"] == EVERY_KIND_READ
    assert healthy.talk_reads[0] == ("legs", datetime.fromtimestamp(int(before.position), UTC))
    legs = collected.state(conn, "legs")
    assert (legs.error, legs.through) == (None, after)


AUTH = ZendeskAuthError("Zendesk rejected the credentials (HTTP 401) for /api/v2/satisfaction_ratings.json")


@pytest.mark.parametrize("reads", [
    {"calls": [AUTH]},
    {"ratings": AUTH},
    {"ticket_export": [AUTH]},
    {"comments": {6001: AUTH}},
], ids=["calls", "ratings", "tickets", "comments"])
def test_auth_error_fails_the_sync(conn, tmp_path, reads):
    client = FakeClient([], users={TECH: TECH_ID}, **reads,
                        open_assigned={TECH_ID: [{"id": 6001, "status": "open",
                                                  "created_at": stamp(LATER - timedelta(days=8))}]})
    collect_at(conn, CollectFake(), NOW)   # so the ratings window is not empty
    result = run(conn, client, tmp_path, now=LATER)
    assert result["status"] == "failed" and result["error"] == describe_error(AUTH)
    assert store.latest_sync(conn)["status"] == "failed"


def test_agents_stored(conn, tmp_path):
    run(conn, FakeClient([], users={TECH: TECH_ID}), tmp_path, now=NOW)
    assert collected.agents(conn) == {"a@x.com": 11}


def test_open_snapshot_keeps_each_tickets_created_time(conn, tmp_path):
    created = NOW - timedelta(days=3)
    run(conn, FakeClient([], users={TECH: TECH_ID}, open_assigned={TECH_ID: [
        {"id": 6001, "status": "open", "created_at": stamp(created)}, {"id": 6002, "status": "new"}]}),
        tmp_path, now=NOW)
    rows = conn.execute("SELECT ticket_id, created FROM activity_open ORDER BY ticket_id")
    assert [tuple(r) for r in rows] == [(6001, stamp(created)), (6002, None)]


# -- aging, once a day on the tech's clock ------------------------------------------------------------------------

CREATED = datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
REPLY = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


def open_rows(*ids):
    """Open tickets: 6001 and 6003 created long ago (aging), 6002 two days before 10/22 (not yet)."""
    created = {6001: CREATED, 6002: datetime(2026, 10, 20, 2, 0, tzinfo=UTC), 6003: CREATED + timedelta(hours=1)}
    return {TECH_ID: [{"id": i, "status": "open", "created_at": stamp(created[i])} for i in ids]}


def test_aging_recorded_once_per_local_day(conn, tmp_path):
    """An APAC tech's aging list is recorded by the first sync of each Manila day, whatever day it is in Chicago."""
    regions.set_regions(conn, TECH, [(date(2026, 10, 1), "APAC")])
    morning = datetime(2026, 10, 22, 0, 30, tzinfo=UTC)   # 08:30 on 10/22 in Manila; 19:30 on 10/21 in Chicago
    first = FakeClient([], users={TECH: TECH_ID}, open_assigned=open_rows(6001, 6002),
                       comments={6001: [comment(TECH_ID, REPLY)]})
    run(conn, first, tmp_path, now=morning)
    assert collected.aging_on(conn, TECH, date(2026, 10, 22)) == (date(2026, 10, 22), [
        PrepItem("aging", 6001, stamp(CREATED), None, "open", last_reply=stamp(REPLY))])
    assert collected.aging_on(conn, TECH, date(2026, 10, 21)) is None
    assert first.comment_reads == [6001]

    for now in (morning + timedelta(hours=1), datetime(2026, 10, 22, 15, 59, tzinfo=UTC)):   # still 10/22 in Manila
        again = FakeClient([], users={TECH: TECH_ID}, open_assigned=open_rows(6001, 6003))
        run(conn, again, tmp_path, now=now)
        assert again.comment_reads == []
    assert [i.ticket_id for i in collected.aging_on(conn, TECH, date(2026, 10, 22))[1]] == [6001]

    midnight = datetime(2026, 10, 22, 16, 30, tzinfo=UTC)   # 00:30 on 10/23 in Manila
    run(conn, FakeClient([], users={TECH: TECH_ID}, open_assigned=open_rows(6001, 6003)), tmp_path, now=midnight)
    day, items = collected.aging_on(conn, TECH, date(2026, 10, 23))
    assert day == date(2026, 10, 23) and [i.ticket_id for i in items] == [6001, 6003]
    assert conn.execute("SELECT COUNT(*) FROM aging_lists").fetchone()[0] == 2


def insert_open(conn, ticket_id, created):
    conn.execute("INSERT INTO activity_open (email, captured_at, ticket_id, status, created) VALUES (?, ?, ?, ?, ?)",
                 (TECH, stamp(NOW), ticket_id, "pending", stamp(created)))
    conn.commit()


def stored(ticket_id, at, kind="reply", email=TECH, event_id=None):
    return Action(email, ticket_id, at, kind, None, None, event_id or int(at.timestamp()), 1)


def test_aging_last_reply_uses_stored_reply_when_newer(conn):
    """The last reply is the later of the tech's newest public comment and their newest stored reply: the stored
    activity can hold a reply newer than the comments read, and the comments one older than it reaches."""
    for ticket_id in (6001, 6002, 6003):
        insert_open(conn, ticket_id, CREATED)
    insert_open(conn, 6004, NOW - timedelta(days=6))   # not aging yet
    newer = REPLY + timedelta(days=2)
    store.insert_actions(conn, [stored(6001, newer), stored(6001, newer + timedelta(hours=1), kind="note"),
                                stored(6001, newer + timedelta(hours=2), email="b@x.com"),
                                stored(6002, REPLY - timedelta(days=1))])
    client = CollectFake(comments={
        6001: [comment(TECH_ID, REPLY)],
        6002: [comment(TECH_ID, REPLY), comment(TECH_ID, REPLY + timedelta(days=5), public=False),
               comment(OTHER_ID, REPLY + timedelta(days=6))],
        6003: [comment(OTHER_ID, REPLY)]})
    lines: list[str] = []
    collect.record_aging(conn, client, TEAM, now=NOW, log=lines.append)

    day, items = collected.aging_on(conn, TECH, date(2026, 10, 22))
    assert day == date(2026, 10, 22)
    assert [(i.ticket_id, i.status, i.at, i.last_reply) for i in items] == [
        (6001, "pending", stamp(CREATED), stamp(newer)),
        (6002, "pending", stamp(CREATED), stamp(REPLY)),
        (6003, "pending", stamp(CREATED), None)]
    assert sorted(client.comment_reads) == [6001, 6002, 6003]
    assert lines == ["Recording aging tickets…", "1 aging list recorded."]


def test_a_failed_comment_read_leaves_the_day_unrecorded(conn):
    insert_open(conn, 6001, CREATED)
    error = ZendeskError("Zendesk returned HTTP 500 for /api/v2/tickets/6001/comments.json after 5 retries")
    lines: list[str] = []
    collect.record_aging(conn, CollectFake(comments={6001: error}), TEAM, now=NOW, log=lines.append)
    assert collected.aging_on(conn, TECH, date(2026, 10, 22)) is None
    assert lines == ["Recording aging tickets…", f"Aging tickets not recorded: {describe_error(error)}",
                     "0 aging lists recorded."]

    collect.record_aging(conn, CollectFake(comments={6001: [comment(TECH_ID, REPLY)]}), TEAM, now=LATER,
                         log=lambda line: None)
    assert collected.aging_on(conn, TECH, date(2026, 10, 22))[1][0].last_reply == stamp(REPLY)


def test_a_day_without_aging_tickets_is_recorded_empty(conn):
    insert_open(conn, 6004, NOW - timedelta(days=6))
    client = CollectFake()
    collect.record_aging(conn, client, TEAM, now=NOW, log=lambda line: None)
    assert collected.aging_on(conn, TECH, date(2026, 10, 22)) == (date(2026, 10, 22), [])
    assert client.comment_reads == []


# -- the history read-back (backfill only) ------------------------------------------------------------------------

OCT1 = datetime(2026, 10, 1, tzinfo=UTC)
OCT20 = datetime(2026, 10, 20, tzinfo=UTC)


def collected_from_oct20(conn):
    """Every kind collected from 10/20 up to NOW, as the hourly sync leaves it."""
    for kind in collected.KINDS:
        collected.save_state(conn, kind, covered_from=OCT20, through=NOW,
                             position="after-9" if kind == "tickets" else str(ts(NOW)))


def changed_page(ticket_id, updated, cursor, end_of_stream=False):
    """A ticket export page holding one ticket solved when it was last updated."""
    return TicketPage([{"id": ticket_id, "assignee_id": TECH_ID, "status": "solved", "priority": "normal", "tags": [],
                        "updated_at": stamp(updated)}],
                      [{"ticket_id": ticket_id, "solved_at": stamp(updated)}], cursor, end_of_stream)


def read_back(conn, client, start=OCT1, lines=None):
    collect.read_back(conn, client, start=start, now=NOW,
                      log=(lines.append if lines is not None else lambda line: None))


def test_read_back_covers_from_start(conn, monkeypatch):
    """Each kind is read from `start` up to where its collection began: Talk pages until one's next start is past
    it, ratings in windows ending there, and changed tickets until a page's tickets were updated after it. Its
    stretch then begins at `start`; its forward position and `through` stay as they were."""
    monkeypatch.setattr(collect, "PROGRESS_EVERY", 2)
    collected_from_oct20(conn)
    client = CollectFake(
        legs=[([leg(1, 101, at=OCT1 + timedelta(days=1)), leg(2, 102, at=OCT1 + timedelta(days=2))],
               OCT1 + timedelta(days=5)),
              ([leg(3, 103, at=OCT1 + timedelta(days=12))], OCT20 + timedelta(hours=1)),
              ([leg(4, 104, at=NOW)], None)],   # never asked for: the page before reached past 10/20
        calls=[([call(101, 9001, updated=OCT1 + timedelta(days=1))], None)],
        ticket_export=[changed_page(9001, OCT1 + timedelta(days=3), "c-1"),
                       changed_page(9002, OCT20 + timedelta(hours=2), "c-2"),
                       changed_page(9003, NOW, "c-3", end_of_stream=True)],   # never asked for
        ratings=[rating(1, "good", at=OCT1 + timedelta(days=4)), rating(2, "bad", at=OCT20 + timedelta(hours=3))])
    lines: list[str] = []
    read_back(conn, client, lines=lines)

    for kind in collected.KINDS:
        state = collected.state(conn, kind)
        assert (state.covered_from, state.history_position, state.through, state.error) == (OCT1, None, NOW, None)
        assert state.position == ("after-9" if kind == "tickets" else str(ts(NOW))), kind
    assert client.talk_reads == [("legs", OCT1), ("calls", OCT1)]
    assert client.rating_windows == [(OCT1, OCT20)]
    assert client.ticket_reads == [{"start": OCT1, "cursor": None}]

    legs, calls = collected.talk_between(conn, OCT1, NOW + timedelta(hours=1))
    assert [one.id for one in legs] == [1, 2, 3] and list(calls) == [101]
    assert [r["id"] for _, r in collected.ratings_between(conn, OCT1, NOW)] == [1]
    assert collected.solved_between(conn, OCT1, NOW) == [(OCT1 + timedelta(days=3), 9001),
                                                         (OCT20 + timedelta(hours=2), 9002)]
    assert lines == ["Reading older call legs…", "Reading older call legs… 2 so far", "3 older call legs stored.",
                     "Reading older calls…", "1 older call stored.",
                     "Reading older satisfaction ratings…", "1 older satisfaction rating stored.",
                     "Reading older changed tickets…", "Reading older changed tickets… 2 so far",
                     "2 older changed tickets stored."]


def test_read_back_reads_ratings_in_31_day_windows(conn):
    collected_from_oct20(conn)
    aug1 = datetime(2026, 8, 1, tzinfo=UTC)
    client = CollectFake(ratings=[rating(1, "good", at=aug1 + timedelta(days=40))])
    read_back(conn, client, start=aug1)
    assert client.rating_windows == [(aug1, aug1 + timedelta(days=31)),
                                     (aug1 + timedelta(days=31), aug1 + timedelta(days=62)),
                                     (aug1 + timedelta(days=62), OCT20)]
    assert [r["id"] for _, r in collected.ratings_between(conn, aug1, NOW)] == [1]
    assert collected.state(conn, "ratings").covered_from == aug1


def test_read_back_resumes_after_failure(conn):
    """A failed page leaves the kind's history position at the last stored page, with its plain reason, and the
    other kinds still read; the next read-back carries on from it, and asks nothing of the kinds already read."""
    collected_from_oct20(conn)
    turn = OCT1 + timedelta(days=6)
    error = ZendeskError("Zendesk returned HTTP 500 for /api/v2/channels/voice/stats/incremental/legs.json")
    lines: list[str] = []
    read_back(conn, CollectFake(legs=[([leg(1, 101, at=OCT1 + timedelta(days=1))], turn), error]), lines=lines)
    legs = collected.state(conn, "legs")
    assert (legs.history_position, legs.covered_from, legs.error) == (str(ts(turn)), OCT20, describe_error(error))
    assert f"Older call legs not read: {describe_error(error)}" in lines
    assert [collected.state(conn, kind).covered_from for kind in ("calls", "ratings", "tickets")] == [OCT1] * 3

    again = CollectFake(legs=[([leg(2, 102, at=turn + timedelta(days=1))], None)])
    read_back(conn, again)
    assert again.talk_reads == [("legs", turn)]
    assert (again.rating_windows, again.ticket_reads) == ([], [])
    legs = collected.state(conn, "legs")
    assert (legs.covered_from, legs.history_position, legs.error) == (OCT1, None, None)
    assert [one.id for one in collected.talk_between(conn, OCT1, NOW)[0]] == [1, 2]


def test_read_back_resumes_tickets_from_their_cursor(conn):
    collected_from_oct20(conn)
    error = ZendeskError("Zendesk returned HTTP 500 for /api/v2/incremental/tickets/cursor.json")
    read_back(conn, CollectFake(ticket_export=[changed_page(9001, OCT1 + timedelta(days=3), "c-1"), error]))
    assert collected.state(conn, "tickets").history_position == "c-1"

    again = CollectFake(ticket_export=[changed_page(9002, OCT1 + timedelta(days=4), "c-2", end_of_stream=True)])
    read_back(conn, again)
    assert again.ticket_reads == [{"start": None, "cursor": "c-1"}]
    tickets = collected.state(conn, "tickets")
    assert (tickets.covered_from, tickets.history_position, tickets.position) == (OCT1, None, "after-9")


def test_read_back_leaves_kinds_already_covered_or_never_read(conn):
    """A kind already read back to `start` asks nothing, nor one never read, nor tickets whose forward read has no
    cursor yet (its forward read would start back at `start` once its stretch did)."""
    collected.save_state(conn, "legs", covered_from=OCT1, through=NOW, position=str(ts(NOW)))
    collected.save_state(conn, "tickets", covered_from=OCT20)
    client = CollectFake()
    read_back(conn, client)
    assert (client.talk_reads, client.rating_windows, client.ticket_reads) == ([], [], [])
    assert collected.state(conn, "calls").covered_from is None
    assert collected.state(conn, "tickets").covered_from == OCT20


def test_read_back_ratings_end_before_now(conn):
    """A stretch that began this very sync: the last ratings window still ends RATINGS_BEFORE_NOW before `now`."""
    collected.save_state(conn, "ratings", covered_from=NOW, through=NOW, position=str(ts(NOW)))
    client = CollectFake()
    read_back(conn, client, start=NOW - timedelta(days=3))
    assert client.rating_windows == [(NOW - timedelta(days=3), NOW - collect.RATINGS_BEFORE_NOW)]
    assert collected.state(conn, "ratings").covered_from == NOW - timedelta(days=3)


def test_read_back_auth_error_is_raised(conn):
    collected_from_oct20(conn)
    with pytest.raises(ZendeskAuthError):
        read_back(conn, CollectFake(ratings=AUTH))


def test_backfill_runs_read_back(conn, tmp_path, monkeypatch):
    """Only a backfill reads the collection back, to the earlier of the history it reads and the stored activity's
    start; the hourly (daily) sync never does."""
    asked: list[dict] = []
    monkeypatch.setattr(collect, "read_back", lambda conn, client, **kwargs: asked.append(kwargs))
    run(conn, FakeClient([], users={TECH: TECH_ID}), tmp_path, now=NOW, kind="daily")
    assert asked == []
    run(conn, FakeClient([], users={TECH: TECH_ID}), tmp_path, now=LATER, kind="backfill")
    assert [(one["start"], one["now"]) for one in asked] == [(NOW - timedelta(days=90), LATER)]


def test_backfill_reads_the_collection_back_to_the_history(conn, tmp_path):
    run(conn, FakeClient([], users={TECH: TECH_ID}), tmp_path, now=NOW, kind="daily")
    assert collected.state(conn, "legs").covered_from == NOW
    assert run(conn, FakeClient([], users={TECH: TECH_ID}), tmp_path, now=LATER, kind="backfill")["status"] == "complete"
    for kind in collected.KINDS:
        assert collected.state(conn, kind).covered_from == NOW - timedelta(days=90), kind
