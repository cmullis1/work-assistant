"""A 1:1 prep's numbers and the lists behind them: one tech's row for one 1:1 day (`TechPrep`) and each item behind
its numbers (`PrepItem`). Counted from the collected data by `oneonone.live`; the aging lists the sync records
(`activity.collected`) are PrepItems too.

NULL (None) in a number means not counted, and `failures` maps a column key to its plain reason.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrepItem:
    kind: str                 # answered, declined, missed, unreachable, aging, pto, sick or rating
    ticket_id: int | None
    at: str | None            # ISO UTC for calls and ratings, the ISO created time for aging, an ISO date for pto/sick
    talk_seconds: int | None
    status: str | None        # aging tickets only
    priority: str | None = None        # answered calls: the ticket's priority
    tags: tuple[str, ...] = ()         # answered calls: the ticket's tags
    score: str | None = None           # ratings: good or bad
    reason: str | None = None          # ratings: Zendesk's reason, None when there is none
    comment: str | None = None         # ratings: the customer's comment, None when there is none
    last_reply: str | None = None      # aging tickets: ISO UTC of the tech's newest public comment, None if never


@dataclass(frozen=True)
class TechPrep:
    email: str
    calls: int | None
    declined_missed: int | None
    unreachable: int | None
    talk_seconds: int | None
    solved: int | None
    ratings_good: int | None
    ratings_bad: int | None
    aging: int | None
    pto: int | None
    sick: int | None
    failures: dict[str, str]
    items: tuple[PrepItem, ...]
    # The department-wide answered calls over this tech's own 14 days, kept when they were counted on another clock
    # than the app's (their region's); None for a tech on the app's clock, whose period's queue is over their days.
    queue: int | None = None
