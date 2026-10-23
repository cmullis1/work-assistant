"""Dataclasses shared across the work_assistant modules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["met", "missed", "na", "unsure"]
Kind = Literal["scorecard", "process"]
Timed = Literal["call_length", "email_deadline"] | None


@dataclass(frozen=True)
class CheckDef:
    check_id: str        # permanent identity; never reused
    kind: Kind
    number: int | None   # display order (scorecard #1-16); not identity
    summary: str
    definition: str
    points: int          # 0 for process rules
    auto_fail: bool
    applies_when: str    # plain language, "Always" by default
    enabled: bool
    timed: Timed = None


@dataclass(frozen=True)
class TimedParams:
    call_limit_minutes: int = 10
    exempt_priorities: tuple[str, ...] = ("urgent",)
    exempt_tags: tuple[str, ...] = ()
    email_deadline_minutes: int = 30


@dataclass(frozen=True)
class RubricVersion:
    version_id: int
    created_at: str      # ISO UTC
    note: str
    checks: tuple[CheckDef, ...]      # checks absent from a version are retired as of it
    reference: dict[str, str]         # document title -> text; open list, owner can add/rename/remove
    params: TimedParams


# Seed documents only; the set is not fixed. Titles are what the reviewer and rules refer to.
DEFAULT_REFERENCE = {"Priority definitions": "priority_definitions.md", "Unsupported products": "unsupported_products.md",
                     "Not authorized": "not_authorized.md", "Redirect procedure": "redirect_procedure.md"}


@dataclass(frozen=True)
class Comment:
    comment_id: int
    created_at: str      # ISO UTC
    author_id: int
    author_name: str
    author_role: Literal["agent", "end-user", "system"]
    public: bool
    kind: Literal["voice", "transcript", "call_summary", "triage_macro", "summary_email", "other"]
    text: str            # plain text
    image_urls: tuple[str, ...]


@dataclass(frozen=True)
class MergeInfo:
    """The ticket a call's ticket was merged into (spec §7.2 rule 7)."""
    ticket_id: int
    subject: str
    location: str | None          # the target's "Location ID" field value
    created_at: str               # ISO UTC
    comments: tuple[Comment, ...] # from this call's start through call end + the collection window


@dataclass(frozen=True)
class FirstContact:
    ticket_id: int
    channel: str                  # "voice"
    subject: str
    priority: str | None
    tags: tuple[str, ...]
    fields: dict[str, str]        # custom field title -> display value (non-empty only)
    call_started_at: str          # ISO UTC
    call_duration_seconds: int
    agent_user_id: int
    agent_email: str
    agent_name: str
    attribution_ok: bool
    summary_email_at: str | None  # ISO UTC
    comments: tuple[Comment, ...] # agent's part only (see Task 6)
    # Minutes from call end to the start of the next outbound call on the ticket (spec §7.2 rule 6).
    callback_minutes: float | None = None
    merged_into: MergeInfo | None = None
    # Who Zendesk says answered the call. It differs from agent_user_id when the triage macro's author
    # stands in as the agent; either of the two may send the summary email. None in older snapshots.
    answered_by_user_id: int | None = None


@dataclass(frozen=True)
class TimedFacts:
    call_minutes: float
    exempt: bool
    email_sent: bool
    minutes_to_email: int | None          # whole minutes from call end, rounded up
    merged_email_minutes: int | None      # the same, for a summary email on the merged-into ticket
    decided: dict[str, Outcome]   # check_id -> outcome decided by code
    evidence: dict[str, str]      # check_id -> evidence text for decided outcomes
