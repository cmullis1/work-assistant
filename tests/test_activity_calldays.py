"""Each tech's calls, ratings and aging day by day on their own clock (`activity.calldays.call_days`), from the collected
data: what Trends' Calls, Customers and Backlog measures add up (Collected calls design, "How the numbers are
counted").

MARA is APAC from Sep 1, so her days are Manila's (UTC+8): her Oct 19 runs 16:00Z Oct 18 to 16:00Z Oct 19, and her
Oct 20 runs 16:00Z Oct 19 to 16:00Z Oct 20. The app's own zone is Chicago."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from work_assistant import regions
from work_assistant.activity import collected
from work_assistant.activity.calldays import CallDay, call_days
from work_assistant.db import connect, init_db
from work_assistant.oneonone.model import PrepItem
from work_assistant.settings import set_setting

UTC = timezone.utc
MARA, CHI, NOBODY = "mara@example.com", "chi@example.com", "nobody@example.com"
MARA_ID, CHI_ID, OTHER_ID = 55, 66, 99
DAYS = [date(2026, 10, 19), date(2026, 10, 20)]
COVERED_FROM = datetime(2026, 9, 1, tzinfo=UTC)
THROUGH = datetime(2026, 10, 21, tzinfo=UTC)


def z(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC moment in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def leg(id, call_id, agent_id, at, status="completed", talk=120):
    return {"id": id, "call_id": call_id, "agent_id": agent_id, "type": "agent", "completion_status": status,
            "created_at": stamp(at), "talk_time": talk}


def call(id, direction="inbound"):
    return {"id": id, "direction": direction, "ticket_id": 5000 + id}


def rating(id, assignee_id, score, at):
    return {"id": id, "ticket_id": 7000 + id, "assignee_id": assignee_id, "score": score, "created_at": stamp(at),
            "reason": None, "comment": None}


def aging(n: int) -> list[PrepItem]:
    return [PrepItem("aging", 6000 + i, "2026-10-01T15:00:00Z", None, "open") for i in range(n)]


def make_db(tmp_path, covered_from=COVERED_FROM, through=THROUGH, kinds=collected.KINDS):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    set_setting(conn, "timezone", "America/Chicago")
    set_setting(conn, "team", [MARA])
    regions.set_regions(conn, MARA, [(date(2026, 9, 1), "APAC")])
    collected.set_agents(conn, {MARA: MARA_ID})
    for kind in kinds:
        collected.save_state(conn, kind, covered_from=covered_from, through=through)
    return conn


def store_talk(conn, legs, calls):
    collected.upsert_legs(conn, legs)
    collected.upsert_calls(conn, calls)


def days_of(conn, email=MARA, days=DAYS):
    return call_days(conn, days, {email: regions.day_zones(conn, email)})[email]


def test_call_days_on_manila_clock(tmp_path):
    """Each leg and rating falls on the Manila day that holds it: 17:00Z Oct 19 is Oct 19 in Chicago but Oct 20 in
    Manila. The queue is every agent's answered inbound legs on her Manila day; an outbound call, a leg on a day not
    asked for and another agent's rating count for nothing. Aging is the count on the list recorded for that very
    day, and None on a day with no list."""
    conn = make_db(tmp_path)
    store_talk(conn, [
        leg(1, 101, MARA_ID, z(10, 19, 15), talk=100),                     # Manila Oct 19, 23:00
        leg(2, 102, MARA_ID, z(10, 19, 17), talk=200),                     # Manila Oct 20, 01:00 (Chicago Oct 19)
        leg(3, 103, MARA_ID, z(10, 20, 2), status="agent_declined", talk=0),
        leg(4, 103, OTHER_ID, z(10, 20, 2, 1), talk=90),                  # took the call she declined
        leg(5, 104, MARA_ID, z(10, 20, 3), talk=60),                      # outbound: not counted
        leg(6, 105, OTHER_ID, z(10, 18, 15), talk=80),                    # Manila Oct 18: not asked for
        leg(7, 106, MARA_ID, z(10, 20, 17), status="agent_missed", talk=0),   # Manila Oct 21: not asked for
        leg(8, 107, MARA_ID, z(10, 20, 4), status="agent_unreachable", talk=0),  # counted apart, not here
    ], [call(101), call(102), call(103), call(104, "outbound"), call(105), call(106), call(107)])
    collected.upsert_ratings(conn, [rating(1, MARA_ID, "bad", z(10, 19, 10)),      # Manila Oct 19, 18:00
                                    rating(2, MARA_ID, "good", z(10, 19, 17, 30)),  # Manila Oct 20, 01:30
                                    rating(3, OTHER_ID, "good", z(10, 19, 10))])
    collected.record_aging(conn, MARA, date(2026, 10, 19), aging(2), now=z(10, 18, 16, 5))
    assert days_of(conn) == [
        CallDay(answered=1, declined_missed=0, talk=100, queue=1, good=0, bad=1, aging=2),
        CallDay(answered=1, declined_missed=1, talk=200, queue=2, good=1, bad=0, aging=None),
    ]


def test_a_list_recorded_empty_is_zero_aging_tickets(tmp_path):
    conn = make_db(tmp_path)
    collected.record_aging(conn, MARA, date(2026, 10, 20), [], now=z(10, 19, 16, 5))
    assert [d.aging for d in days_of(conn)] == [None, 0]


def test_a_day_the_calls_arent_collected_for_is_none(tmp_path):
    """The legs and calls must cover the whole Manila day: here from her Oct 20's midnight to the end of it, so Oct 19
    started before the collection and Oct 21 isn't over in it."""
    conn = make_db(tmp_path, covered_from=z(10, 19, 16), through=z(10, 20, 16))
    store_talk(conn, [leg(1, 101, MARA_ID, z(10, 19, 15)), leg(2, 102, MARA_ID, z(10, 19, 17)),
                      leg(3, 103, MARA_ID, z(10, 20, 17))], [call(101), call(102), call(103)])
    found = days_of(conn, days=[*DAYS, date(2026, 10, 21)])
    assert found[0] is None and found[2] is None
    assert found[1] == CallDay(answered=1, declined_missed=0, talk=120, queue=1, good=0, bad=0, aging=None)


def test_ratings_count_only_on_days_they_are_collected(tmp_path):
    conn = make_db(tmp_path, kinds=("legs", "calls", "tickets"))
    collected.upsert_ratings(conn, [rating(1, MARA_ID, "good", z(10, 19, 10))])
    assert [(d.good, d.bad) for d in days_of(conn)] == [(0, 0), (0, 0)]


def test_a_day_with_a_leg_missing_its_call_is_none(tmp_path):
    """A leg stored before its call record leaves its day uncounted until a later sync stores the call, as 1:1 Prep
    leaves its period's calls uncounted; any agent's leg does, since the queue is the department's."""
    conn = make_db(tmp_path)
    store_talk(conn, [leg(1, 101, MARA_ID, z(10, 19, 15)), leg(2, 102, OTHER_ID, z(10, 19, 17))], [call(101)])
    first, second = days_of(conn)
    assert first == CallDay(answered=1, declined_missed=0, talk=120, queue=1, good=0, bad=0, aging=None)
    assert second is None
    collected.upsert_calls(conn, [call(102)])
    assert days_of(conn)[1] == CallDay(answered=0, declined_missed=0, talk=0, queue=1, good=0, bad=0, aging=None)


def test_a_tech_with_no_zendesk_id_has_none_every_day(tmp_path):
    conn = make_db(tmp_path)
    collected.record_aging(conn, NOBODY, date(2026, 10, 19), aging(3), now=z(10, 19, 11))
    assert days_of(conn, NOBODY) == [None, None]
    assert days_of(conn, MARA, []) == []


def test_one_read_for_every_tech(tmp_path, monkeypatch):
    """Every tech's days are cut from one read of the department's legs, calls and ratings over the span all their
    days lie in: Mara's Manila Oct 19 starts it (16:00Z Oct 18) and Chicago's Oct 20 ends it (05:00Z Oct 21). Each
    tech's days are what reading them alone gives, and a tech with no Zendesk id adds nothing to the span."""
    conn = make_db(tmp_path, through=z(10, 22, 0))
    set_setting(conn, "team", [MARA, CHI, NOBODY])
    collected.set_agents(conn, {MARA: MARA_ID, CHI: CHI_ID})
    store_talk(conn, [leg(1, 101, MARA_ID, z(10, 19, 15), talk=100),          # Manila Oct 19; Chicago Oct 19
                      leg(2, 102, CHI_ID, z(10, 19, 17), talk=200),           # Manila Oct 20; Chicago Oct 19
                      leg(3, 103, CHI_ID, z(10, 21, 4), talk=300)],           # Chicago Oct 20, 23:00
               [call(101), call(102), call(103)])
    collected.upsert_ratings(conn, [rating(1, MARA_ID, "good", z(10, 19, 10)), rating(2, CHI_ID, "bad", z(10, 20, 20))])
    zones = {email: regions.day_zones(conn, email) for email in (MARA, CHI, NOBODY)}
    alone = {email: call_days(conn, DAYS, {email: zone_on})[email] for email, zone_on in zones.items()}
    assert alone[CHI] == [CallDay(answered=1, declined_missed=0, talk=200, queue=2, good=0, bad=0, aging=None),
                          CallDay(answered=1, declined_missed=0, talk=300, queue=1, good=0, bad=1, aging=None)]
    assert alone[NOBODY] == [None, None]

    reads = []
    for name in ("talk_between", "ratings_between"):
        def spy(conn, start, end, name=name, real=getattr(collected, name)):
            reads.append((name, start, end))
            return real(conn, start, end)
        monkeypatch.setattr(collected, name, spy)
    assert call_days(conn, DAYS, zones) == alone
    assert sorted(reads) == [("ratings_between", z(10, 18, 16), z(10, 21, 5)),
                             ("talk_between", z(10, 18, 16), z(10, 21, 5))]
