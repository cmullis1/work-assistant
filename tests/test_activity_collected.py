"""The collected calls, ratings, solves and aging (`activity.collected`): stored by Zendesk id, read back by time."""
from datetime import date, datetime, timedelta, timezone

from work_assistant.activity import collected
from work_assistant.oneonone.count import Call, Leg
from work_assistant.oneonone.model import PrepItem

UTC = timezone.utc
EMAIL = "avery@example.com"


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def leg(id, call_id, agent_id=11, talk=30, at=datetime(2026, 10, 20, 15, tzinfo=UTC), status="completed"):
    return {"id": id, "call_id": call_id, "agent_id": agent_id, "type": "agent", "completion_status": status,
            "created_at": stamp(at), "talk_time": talk, "updated_at": stamp(at + timedelta(minutes=10))}


def call(id, ticket_id, direction="inbound"):
    return {"id": id, "direction": direction, "ticket_id": ticket_id, "updated_at": "2026-10-20T16:00:00Z"}


def rating(id, score, at=datetime(2026, 10, 20, 15, tzinfo=UTC), comment="Thanks!", reason="Issue resolved"):
    return {"id": id, "ticket_id": 500 + id, "assignee_id": 11, "score": score, "created_at": stamp(at),
            "updated_at": stamp(at + timedelta(hours=1)), "reason": reason, "comment": comment}


def ticket(id, assignee_id=11, status="solved", priority="high", tags=("billing",)):
    return {"id": id, "assignee_id": assignee_id, "status": status, "priority": priority, "tags": list(tags)}


def aging(ticket_id, created, status="open", last_reply=None):
    return PrepItem("aging", ticket_id, created, None, status, last_reply=last_reply)


DAY_START = datetime(2026, 10, 20, tzinfo=UTC)
DAY_END = datetime(2026, 10, 21, tzinfo=UTC)


def test_upserts_never_double(conn):
    legs = [leg(1, 101), leg(2, 101, at=datetime(2026, 10, 20, 16, tzinfo=UTC)), leg(3, 102, agent_id=0)]
    assert collected.upsert_legs(conn, legs) == 3
    assert collected.upsert_calls(conn, [call(101, 9001), call(102, None, "outbound")]) == 2
    assert collected.upsert_legs(conn, legs) == 0          # the same rows again change nothing
    assert collected.upsert_calls(conn, [call(101, 9001)]) == 0

    found, calls = collected.talk_between(conn, DAY_START, DAY_END)
    assert [f.id for f in found] == [1, 3, 2]                # oldest first
    assert found[0] == Leg(id=1, call_id=101, agent_id=11, type="agent", status="completed",
                           created=datetime(2026, 10, 20, 15, tzinfo=UTC), talk=30)
    assert found[1].agent_id is None                         # a customer leg's 0 is no agent
    assert calls == {101: Call(101, "inbound", 9001), 102: Call(102, "outbound", None)}

    assert collected.upsert_legs(conn, [leg(1, 101, talk=90)]) == 1   # a later read replaces the stored row
    found, _ = collected.talk_between(conn, DAY_START, DAY_END)
    assert len(found) == 3 and found[0].talk == 90


def test_talk_between_is_start_inclusive_end_exclusive(conn):
    collected.upsert_legs(conn, [leg(1, 101, at=DAY_START), leg(2, 102, at=DAY_END)])
    collected.upsert_calls(conn, [call(101, 1), call(102, 2), call(103, 3)])
    found, calls = collected.talk_between(conn, DAY_START, DAY_END)
    assert [f.id for f in found] == [1]
    assert calls == {101: Call(101, "inbound", 1)}           # only the calls the legs name


def test_a_leg_whose_call_is_not_stored_has_no_call(conn):
    collected.upsert_legs(conn, [leg(1, 101)])
    found, calls = collected.talk_between(conn, DAY_START, DAY_END)
    assert [f.id for f in found] == [1] and calls == {}


def test_rating_turns_bad_and_leaves(conn):
    assert collected.upsert_ratings(conn, [rating(7, "good")]) == 1
    assert [(at, r["score"]) for at, r in collected.ratings_between(conn, DAY_START, DAY_END)] == [
        (datetime(2026, 10, 20, 15, tzinfo=UTC), "good")]

    collected.upsert_ratings(conn, [rating(7, "bad", comment="Still broken")])
    [(at, stored)] = collected.ratings_between(conn, DAY_START, DAY_END)
    assert stored == {"id": 7, "ticket_id": 507, "assignee_id": 11, "score": "bad",
                      "created_at": "2026-10-20T15:00:00Z", "reason": "Issue resolved", "comment": "Still broken"}

    assert collected.upsert_ratings(conn, [rating(7, "offered")]) == 1
    assert collected.ratings_between(conn, DAY_START, DAY_END) == []
    assert collected.upsert_ratings(conn, [rating(8, "offered")]) == 0   # never stored, nothing to remove


def test_ratings_between_oldest_first_in_range(conn):
    collected.upsert_ratings(conn, [rating(1, "good", at=datetime(2026, 10, 20, 18, tzinfo=UTC)),
                                    rating(2, "bad", at=datetime(2026, 10, 20, 9, tzinfo=UTC)),
                                    rating(3, "good", at=DAY_END)])
    assert [r["id"] for _, r in collected.ratings_between(conn, DAY_START, DAY_END)] == [2, 1]


def test_ticket_state_solved_at_from_metric_set(conn):
    assert collected.upsert_ticket_states(
        conn, [ticket(5), ticket(6, assignee_id=12)],
        [{"ticket_id": 5, "solved_at": "2026-10-20T15:00:00Z"}, {"ticket_id": 6, "solved_at": "2026-10-20T16:00:00Z"}],
    ) == 2
    three, four = datetime(2026, 10, 20, 15, tzinfo=UTC), datetime(2026, 10, 20, 16, tzinfo=UTC)
    assert collected.solved_between(conn, DAY_START, DAY_END, agent_id=11) == [(three, 5)]
    assert collected.solved_between(conn, DAY_START, DAY_END) == [(three, 5), (four, 6)]
    assert collected.ticket_facts(conn, [5, 6, 99]) == {5: {"priority": "high", "tags": ["billing"]},
                                                        6: {"priority": "high", "tags": ["billing"]}}

    collected.upsert_ticket_states(conn, [ticket(5, status="open")], [{"ticket_id": 5, "solved_at": None}])
    assert collected.solved_between(conn, DAY_START, DAY_END, agent_id=11) == []   # reopened


def test_solved_by_is_each_agents_solved_between(conn):
    """One read gives each asked agent's solves, as `solved_between` gives one agent's; an agent with none has []."""
    at = [datetime(2026, 10, 20, h, tzinfo=UTC) for h in (9, 15, 16)]
    collected.upsert_ticket_states(conn, [ticket(5), ticket(6, assignee_id=12), ticket(7), ticket(8, assignee_id=13)],
                                   [{"ticket_id": 5, "solved_at": stamp(at[2])},
                                    {"ticket_id": 6, "solved_at": stamp(at[1])},
                                    {"ticket_id": 7, "solved_at": stamp(at[0])},
                                    {"ticket_id": 8, "solved_at": stamp(at[0])}])
    found = collected.solved_by(conn, DAY_START, DAY_END, [11, 12, 14])
    assert found == {11: [(at[0], 7), (at[2], 5)], 12: [(at[1], 6)], 14: []}
    assert found[11] == collected.solved_between(conn, DAY_START, DAY_END, agent_id=11)
    assert collected.solved_by(conn, at[1], DAY_END, [11]) == {11: [(at[2], 5)]}


def test_cut_is_what_a_read_of_the_stretch_gives(conn):
    """Any stretch cut from a read of a longer one is what reading that stretch gives: from its start, up to and not
    including its end, oldest first."""
    times = [DAY_START, DAY_START + timedelta(hours=9), DAY_START + timedelta(hours=9), DAY_END]
    collected.upsert_legs(conn, [leg(n, 100 + n, at=moment) for n, moment in enumerate(times, 1)])
    collected.upsert_ratings(conn, [rating(n, "good", at=moment) for n, moment in enumerate(times, 1)])
    span = (DAY_START - timedelta(days=1), DAY_END + timedelta(days=1))
    legs, _ = collected.talk_between(conn, *span)
    ratings = collected.ratings_between(conn, *span)
    for start, end in [(DAY_START, DAY_END), (times[1], DAY_END), (DAY_START, times[1]), (DAY_END, span[1]),
                       (span[0], DAY_START)]:
        assert collected.cut(legs, start, end, lambda one: one.created) == collected.talk_between(conn, start, end)[0]
        assert collected.cut(ratings, start, end) == collected.ratings_between(conn, start, end)


def test_ticket_facts_without_priority_or_tags(conn):
    collected.upsert_ticket_states(conn, [{"id": 5, "assignee_id": None, "status": "new", "priority": None,
                                           "tags": []}], [])
    assert collected.ticket_facts(conn, [5]) == {5: {"priority": None, "tags": []}}
    assert collected.solved_between(conn, DAY_START, DAY_END) == []


def test_state_round_trip(conn):
    assert collected.state(conn, "legs") == collected.State("legs", None, None, None, None, None)
    through = datetime(2026, 10, 20, 12, tzinfo=UTC)
    collected.save_state(conn, "legs", position="1729500000", through=through)
    assert collected.state(conn, "legs") == collected.State("legs", "1729500000", through, None, None, None)

    collected.save_state(conn, "legs", error="Zendesk timed out.")   # only the given fields change
    assert collected.state(conn, "legs") == collected.State("legs", "1729500000", through, None, None,
                                                            "Zendesk timed out.")
    collected.save_state(conn, "legs", error=None, covered_from=datetime(2026, 10, 1, tzinfo=UTC))
    assert collected.state(conn, "legs").error is None
    assert collected.state(conn, "legs").covered_from == datetime(2026, 10, 1, tzinfo=UTC)
    assert collected.state(conn, "calls") == collected.State("calls", None, None, None, None, None)
    assert collected.KINDS == ("legs", "calls", "ratings", "tickets")


def test_uncovered(conn):
    covered_from, through = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 20, 12, tzinfo=UTC)
    start, end = datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC)
    collected.save_state(conn, "legs", covered_from=covered_from, through=through)
    assert collected.uncovered(conn, ("legs", "calls"), start, end) is True      # calls never read
    assert collected.uncovered(conn, ("legs",), start, end) is False
    collected.save_state(conn, "calls", covered_from=covered_from, through=through)
    assert collected.uncovered(conn, ("legs", "calls"), start, end) is False
    assert collected.uncovered(conn, ("legs", "calls"), covered_from, through) is False
    assert collected.uncovered(conn, ("legs",), start, datetime(2026, 10, 21, tzinfo=UTC)) is True   # past through
    assert collected.uncovered(conn, ("legs",), datetime(2026, 9, 30, tzinfo=UTC), end) is True      # before it began


def test_agents_round_trip(conn):
    assert collected.agents(conn) == {}
    collected.set_agents(conn, {EMAIL: 11, "blake@example.com": 12})
    collected.set_agents(conn, {EMAIL: 13})
    assert collected.agents(conn) == {EMAIL: 13, "blake@example.com": 12}


def test_aging_latest_on_or_before(conn):
    now = datetime(2026, 10, 18, 11, tzinfo=UTC)
    first = [aging(40, "2026-10-01T09:00:00Z", last_reply="2026-10-15T10:00:00Z"), aging(41, "2026-10-05T09:00:00Z")]
    later = [aging(41, "2026-10-05T09:00:00Z", status="pending")]
    assert collected.first_aging_day(conn) is None
    collected.record_aging(conn, EMAIL, date(2026, 10, 18), first, now=now)
    collected.record_aging(conn, EMAIL, date(2026, 10, 20), later, now=now + timedelta(days=2))
    collected.record_aging(conn, "blake@example.com", date(2026, 10, 19), [], now=now + timedelta(days=1))

    assert collected.aging_on(conn, EMAIL, date(2026, 10, 19)) == (date(2026, 10, 18), first)
    assert collected.aging_on(conn, EMAIL, date(2026, 10, 20)) == (date(2026, 10, 20), later)
    assert collected.aging_on(conn, EMAIL, date(2026, 10, 17)) is None
    assert collected.aging_on(conn, "blake@example.com", date(2026, 10, 25)) == (date(2026, 10, 19), [])
    assert collected.first_aging_day(conn) == date(2026, 10, 18)
    assert collected.first_aging_day(conn, "blake@example.com") == date(2026, 10, 19)
    assert collected.first_aging_day(conn, "casey@example.com") is None

    collected.record_aging(conn, EMAIL, date(2026, 10, 18), later, now=now)   # a day recorded again is replaced
    assert collected.aging_on(conn, EMAIL, date(2026, 10, 18)) == (date(2026, 10, 18), later)


def test_aging_size_is_the_size_of_the_list_aging_on_gives(conn):
    """How many tickets `aging_on`'s list holds, and the day it was recorded for, without reading them (T-168)."""
    now = datetime(2026, 10, 18, 11, tzinfo=UTC)
    collected.record_aging(conn, EMAIL, date(2026, 10, 18), [aging(40, "2026-10-01T09:00:00Z"),
                                                             aging(41, "2026-10-05T09:00:00Z")], now=now)
    collected.record_aging(conn, EMAIL, date(2026, 10, 20), [], now=now + timedelta(days=2))
    collected.record_aging(conn, "blake@example.com", date(2026, 10, 19), [aging(42, "2026-10-02T09:00:00Z")],
                           now=now)
    for email, day in ((EMAIL, date(2026, 10, 17)), (EMAIL, date(2026, 10, 18)), (EMAIL, date(2026, 10, 19)),
                       (EMAIL, date(2026, 10, 25)), ("blake@example.com", date(2026, 10, 25)),
                       ("casey@example.com", date(2026, 10, 25))):
        listed = collected.aging_on(conn, email, day)
        assert collected.aging_size(conn, email, day) == (None if listed is None else (listed[0], len(listed[1])))
    assert collected.aging_size(conn, EMAIL, date(2026, 10, 19)) == (date(2026, 10, 18), 2)
    assert collected.aging_size(conn, EMAIL, date(2026, 10, 20)) == (date(2026, 10, 20), 0)


def test_aging_recorded_says_when_the_list_was_taken(conn):
    """The day and moment of the list `aging_on` gives, so a 1:1 prep counts its tickets' ages from then."""
    now = datetime(2026, 10, 18, 11, 5, tzinfo=UTC)
    collected.record_aging(conn, EMAIL, date(2026, 10, 18), [aging(40, "2026-10-01T09:00:00Z")], now=now)
    collected.record_aging(conn, EMAIL, date(2026, 10, 20), [], now=now + timedelta(days=2, hours=1))
    assert collected.aging_recorded(conn, EMAIL, date(2026, 10, 19)) == (date(2026, 10, 18), now)
    assert collected.aging_recorded(conn, EMAIL, date(2026, 10, 25)) == (date(2026, 10, 20),
                                                                         now + timedelta(days=2, hours=1))
    assert collected.aging_recorded(conn, EMAIL, date(2026, 10, 17)) is None


def test_purge_ratings_keeps_scores(conn):
    old, new = datetime(2026, 9, 1, 15, tzinfo=UTC), datetime(2026, 10, 20, 15, tzinfo=UTC)
    collected.upsert_ratings(conn, [rating(1, "bad", at=old), rating(2, "good", at=new)])
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    assert collected.purge_ratings(conn, cutoff) == 1
    assert collected.purge_ratings(conn, cutoff) == 0

    everything = collected.ratings_between(conn, datetime(2026, 8, 1, tzinfo=UTC), DAY_END)
    assert [(at, r["id"], r["score"], r["reason"], r["comment"]) for at, r in everything] == [
        (old, 1, "bad", None, None), (new, 2, "good", "Issue resolved", "Thanks!")]
    assert [tuple(r) for r in conn.execute("SELECT id, purged FROM sat_ratings ORDER BY id")] == [(1, 1), (2, 0)]

    collected.upsert_ratings(conn, [rating(1, "good", at=old)])   # a later read of a purged rating stays purged
    [(_, purged)] = collected.ratings_between(conn, datetime(2026, 8, 1, tzinfo=UTC), cutoff)
    assert (purged["score"], purged["reason"], purged["comment"]) == ("good", None, None)
