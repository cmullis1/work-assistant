"""Each tech's 1:1 call figures, counted from Zendesk Talk call legs and calls (spec §4).

Pure, no I/O. A leg counts when its local day (in the team's time zone) of `created` lies inside the
period. An answered leg is an `agent` leg on an inbound call, completed, with talk time above zero;
the department-wide queue is every answered leg from any agent. Declined and missed agent legs on
inbound calls are counted per tech; unreachable legs are counted apart (a connection fault, not a
tech's choice). Agent legs whose call is missing from the calls export are only counted as unmatched.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from work_assistant.oneonone.cycle import PrepPeriod

ANSWERED = "answered"
DECLINED = "declined"
MISSED = "missed"
UNREACHABLE = "unreachable"

# completion_status of an agent leg on an inbound call that the tech did not take -> its outcome
_NOT_TAKEN = {"agent_declined": DECLINED, "agent_missed": MISSED, "agent_unreachable": UNREACHABLE}


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class Leg:
    id: int
    call_id: int
    agent_id: int | None
    type: str
    status: str
    created: datetime   # aware, UTC
    talk: int           # seconds

    @classmethod
    def from_api(cls, row: dict) -> Leg:
        return cls(
            id=row["id"],
            call_id=row["call_id"],
            agent_id=row.get("agent_id") or None,   # customer legs carry 0 or null
            type=row["type"],
            status=row["completion_status"],
            created=_utc(row["created_at"]),
            talk=row.get("talk_time") or 0,
        )


@dataclass(frozen=True)
class Call:
    id: int
    direction: str
    ticket_id: int | None

    @classmethod
    def from_api(cls, row: dict) -> Call:
        return cls(id=row["id"], direction=row["direction"], ticket_id=row.get("ticket_id"))


@dataclass(frozen=True)
class CallItem:
    leg_id: int
    ticket_id: int | None
    at: datetime
    talk: int
    outcome: str   # ANSWERED, DECLINED, MISSED or UNREACHABLE


@dataclass(frozen=True)
class CallCounts:
    calls: int
    declined_missed: int
    unreachable: int
    talk_seconds: int
    items: tuple[CallItem, ...]   # oldest first


@dataclass(frozen=True)
class CallTally:
    queue: int
    unmatched: int
    techs: dict[int, CallCounts]


def _outcome(leg: Leg) -> str | None:
    """What an agent leg on an inbound call records, or None when it records nothing."""
    if leg.status == "completed":
        return ANSWERED if leg.talk > 0 else None
    return _NOT_TAKEN.get(leg.status)


def _counts(items: list[CallItem]) -> CallCounts:
    ordered = tuple(sorted(items, key=lambda item: (item.at, item.leg_id)))
    answered = [item for item in ordered if item.outcome == ANSWERED]
    return CallCounts(
        calls=len(answered),
        declined_missed=sum(item.outcome in (DECLINED, MISSED) for item in ordered),
        unreachable=sum(item.outcome == UNREACHABLE for item in ordered),
        talk_seconds=sum(item.talk for item in answered),
        items=ordered,
    )


def tally(legs: Iterable[Leg], calls: Mapping[int, Call], agent_ids: Iterable[int],
          period: PrepPeriod, tz: str) -> CallTally:
    zone = ZoneInfo(tz)
    per_tech: dict[int, list[CallItem]] = {agent_id: [] for agent_id in agent_ids}
    queue = unmatched = 0
    for leg in legs:
        if leg.type != "agent" or not period.start <= leg.created.astimezone(zone).date() <= period.end:
            continue
        call = calls.get(leg.call_id)
        if call is None:
            unmatched += 1
            continue
        if call.direction != "inbound":
            continue
        outcome = _outcome(leg)
        if outcome is None:
            continue
        if outcome == ANSWERED:
            queue += 1
        if leg.agent_id in per_tech:
            per_tech[leg.agent_id].append(CallItem(leg.id, call.ticket_id, leg.created, leg.talk, outcome))
    return CallTally(queue=queue, unmatched=unmatched,
                     techs={agent_id: _counts(items) for agent_id, items in per_tech.items()})
