import itertools
from datetime import date, datetime, timezone

from work_assistant.oneonone.count import Call, CallCounts, CallItem, Leg, tally
from work_assistant.oneonone.cycle import period_for

TZ = "America/Chicago"
PERIOD = period_for(date(2026, 9, 24))   # 9/10 – 9/23
RUBEN, DESMOND, OTHER = 101, 102, 999

INBOUND, OUTBOUND = 1, 2
CALLS = {
    INBOUND: Call.from_api({"id": INBOUND, "direction": "inbound", "ticket_id": 482084}),
    OUTBOUND: Call.from_api({"id": OUTBOUND, "direction": "outbound", "ticket_id": 482090}),
}
ZEROS = CallCounts(0, 0, 0, 0, ())

_ids = itertools.count(1)


def leg(agent_id=RUBEN, *, call_id=INBOUND, type="agent", status="completed", talk=300,
        created="2026-09-15T20:01:12Z", id=None):
    return Leg.from_api({
        "id": id if id is not None else next(_ids), "call_id": call_id, "agent_id": agent_id,
        "user_id": 555, "type": type, "completion_status": status, "created_at": created,
        "talk_time": talk,
    })


def run(legs, agent_ids=(RUBEN,), calls=CALLS, period=PERIOD):
    return tally(legs, calls, agent_ids, period, TZ)


def test_from_api_reads_talk_rows():
    row = {"id": 7, "call_id": 3, "agent_id": RUBEN, "user_id": 9, "type": "agent",
           "completion_status": "agent_missed", "created_at": "2026-09-15T20:01:12Z", "talk_time": None}
    assert Leg.from_api(row) == Leg(id=7, call_id=3, agent_id=RUBEN, type="agent", status="agent_missed",
                                    created=datetime(2026, 9, 15, 20, 1, 12, tzinfo=timezone.utc), talk=0)
    assert Call.from_api({"id": 3, "direction": "inbound", "ticket_id": None, "duration": 40}) == Call(
        id=3, direction="inbound", ticket_id=None)


def test_answered_needs_completed_inbound_agent_leg_with_talk():
    result = run([
        leg(talk=420),
        leg(talk=0),                       # completed with no talk: not taken (#482084)
        leg(call_id=OUTBOUND, talk=420),   # outbound call
        leg(type="supervisor", talk=420),
        leg(type="customer", agent_id=0, talk=420),
    ])
    assert result.techs[RUBEN].calls == 1
    assert result.techs[RUBEN].talk_seconds == 420
    assert result.queue == 1


def test_queue_counts_answered_legs_from_every_agent():
    result = run([leg(RUBEN), leg(DESMOND), leg(OTHER), leg(OTHER, status="agent_declined", talk=0)],
                 agent_ids=(RUBEN, DESMOND))
    assert result.queue == 3
    assert result.techs[RUBEN].calls == 1
    assert result.techs[DESMOND].calls == 1
    assert OTHER not in result.techs


def test_declined_and_missed_count_unreachable_does_not():
    result = run([
        leg(status="agent_declined", talk=0),
        leg(status="agent_missed", talk=0),
        leg(status="agent_unreachable", talk=0),
        leg(status="agent_unreachable", talk=0),
        leg(status="agent_unreachable", talk=0),
    ])
    counts = result.techs[RUBEN]
    assert counts.declined_missed == 2
    assert counts.unreachable == 3
    assert counts.calls == 0
    assert result.queue == 0


def test_talk_seconds_sum_answered_legs_only():
    result = run([leg(talk=7583), leg(talk=4283), leg(status="agent_declined", talk=0),
                  leg(status="agent_missed", talk=30)])   # talk on a leg not taken is not call time
    assert result.techs[RUBEN].talk_seconds == 11866
    assert result.techs[RUBEN].calls == 2


def test_legs_counted_by_local_day():
    result = run([
        leg(created="2026-09-24T04:30:00Z", talk=100),   # 9/23 23:30 CDT: last day, counts
        leg(created="2026-09-10T04:30:00Z", talk=200),   # 9/9 23:30 CDT: before the period
        leg(created="2026-09-10T05:30:00Z", talk=400),   # 9/10 00:30 CDT: first day, counts
        leg(created="2026-09-24T05:30:00Z", talk=800),   # 9/24 00:30 CDT: the 1:1 day itself
    ])
    assert result.techs[RUBEN].calls == 2
    assert result.techs[RUBEN].talk_seconds == 500
    assert result.queue == 2


def test_daylight_saving_day_is_one_local_day():
    period = period_for(date(2026, 11, 3))   # 10/20 – 11/2
    assert (period.start, period.end) == (date(2026, 10, 20), date(2026, 11, 2))
    result = run([
        leg(created="2026-11-01T05:30:00Z", talk=100),   # 00:30 CDT, Nov 1
        leg(created="2026-11-02T05:30:00Z", talk=200),   # 23:30 CST, Nov 1
        leg(created="2026-11-03T06:30:00Z", talk=400),   # 00:30 CST, Nov 3: outside
    ], period=period)
    assert result.techs[RUBEN].calls == 2
    assert result.techs[RUBEN].talk_seconds == 300


def test_tech_with_no_legs_gets_zeros():
    result = run([leg(RUBEN)], agent_ids=(RUBEN, DESMOND))
    assert result.techs[DESMOND] == ZEROS
    assert run([], agent_ids=(DESMOND,)).techs == {DESMOND: ZEROS}


def test_leg_with_unknown_call_is_unmatched():
    result = run([
        leg(call_id=77, talk=420),
        leg(call_id=77, type="customer", agent_id=None, talk=420),        # not an agent leg
        leg(call_id=77, talk=420, created="2026-09-01T15:00:00Z"),         # outside the period
    ])
    assert result.unmatched == 1
    assert result.queue == 0
    assert result.techs[RUBEN] == ZEROS


def test_items_are_oldest_first_with_ticket_ids():
    calls = {
        10: Call.from_api({"id": 10, "direction": "inbound", "ticket_id": 5001}),
        11: Call.from_api({"id": 11, "direction": "inbound", "ticket_id": None}),
        12: Call.from_api({"id": 12, "direction": "inbound", "ticket_id": 5003}),
    }
    legs = [
        leg(id=4, call_id=12, status="agent_unreachable", talk=0, created="2026-09-18T16:00:00Z"),
        leg(id=1, call_id=10, talk=610, created="2026-09-15T14:00:00Z"),
        leg(id=3, call_id=12, status="agent_missed", talk=0, created="2026-09-17T16:00:00Z"),
        leg(id=2, call_id=11, status="agent_declined", talk=0, created="2026-09-16T15:00:00Z"),
        leg(id=5, call_id=10, talk=0, created="2026-09-19T15:00:00Z"),            # zero talk: no item
        leg(DESMOND, id=6, call_id=10, talk=90, created="2026-09-14T15:00:00Z"),  # another tech
    ]
    items = run(legs, calls=calls).techs[RUBEN].items
    assert items == (
        CallItem(leg_id=1, ticket_id=5001, at=datetime(2026, 9, 15, 14, tzinfo=timezone.utc), talk=610,
                 outcome="answered"),
        CallItem(leg_id=2, ticket_id=None, at=datetime(2026, 9, 16, 15, tzinfo=timezone.utc), talk=0,
                 outcome="declined"),
        CallItem(leg_id=3, ticket_id=5003, at=datetime(2026, 9, 17, 16, tzinfo=timezone.utc), talk=0,
                 outcome="missed"),
        CallItem(leg_id=4, ticket_id=5003, at=datetime(2026, 9, 18, 16, tzinfo=timezone.utc), talk=0,
                 outcome="unreachable"),
    )
