"""Timed process facts and the outcomes the code (not the AI) decides from timestamps."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from work_assistant.models import FirstContact, Outcome, RubricVersion, TimedFacts, TimedParams


def _parse(ts: str) -> datetime:
    """Parse an ISO UTC timestamp, accepting a trailing 'Z'."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ceil_tenths(delta: timedelta, unit: timedelta) -> float:
    """delta / unit rounded UP to 1 decimal place, using exact integer arithmetic.

    Rounding up (never down) keeps the displayed figure from contradicting a strict
    "no more than the limit" comparison made on the unrounded value.
    """
    tenth = unit / 10
    return -((-delta) // tenth) / 10


def _ceil_minutes(delta: timedelta) -> int:
    """delta in whole minutes, rounded UP (as `_ceil_tenths`, so the figure never contradicts the limit)."""
    return -((-delta) // timedelta(minutes=1))


def format_minutes(minutes: int) -> str:
    """A duration in minutes as the owner reads it: "30 min", "1 h", "1 h 30 min", "24 h"."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def is_exempt(priority: str | None, tags: Iterable[str], params: TimedParams) -> bool:
    """Whether a call with this priority and these tags is exempt from the call-length limit."""
    exempt_priorities = {p.lower() for p in params.exempt_priorities}
    if priority is not None and priority.lower() in exempt_priorities:
        return True
    exempt_tags = set(params.exempt_tags)
    return any(tag in exempt_tags for tag in tags)


def _is_exempt(contact: FirstContact, version: RubricVersion) -> bool:
    return is_exempt(contact.priority, contact.tags, version.params)


def timed_facts(contact: FirstContact, version: RubricVersion) -> TimedFacts:
    """Compute call length and summary-email timing, and decide the timed checks in code.

    A timed check is decided only if it is present and enabled in the version. The call-length
    check is left undecided for exempt calls so the reviewer can judge it from the definition, and
    the email-deadline check for a merged call with no summary email on its own ticket. For such a call
    the gap to the merged-into ticket's summary email is measured for the reviewer instead.
    """
    params = version.params
    call_start = _parse(contact.call_started_at)
    call_end = call_start + timedelta(seconds=contact.call_duration_seconds)
    call_delta = call_end - call_start
    exact_minutes = contact.call_duration_seconds / 60
    call_minutes = _ceil_tenths(call_delta, timedelta(minutes=1))
    exempt = _is_exempt(contact, version)

    deadline = timedelta(minutes=params.email_deadline_minutes)
    email_sent = contact.summary_email_at is not None
    email_delta: timedelta | None = None
    minutes_to_email: int | None = None
    if contact.summary_email_at is not None:
        email_delta = _parse(contact.summary_email_at) - call_end
        minutes_to_email = _ceil_minutes(email_delta)
    merged_email_minutes: int | None = None
    if contact.merged_into is not None:
        merged_email = next((c for c in contact.merged_into.comments if c.kind == "summary_email"), None)
        if merged_email is not None:
            merged_email_minutes = _ceil_minutes(_parse(merged_email.created_at) - call_end)

    decided: dict[str, Outcome] = {}
    evidence: dict[str, str] = {}

    for check in version.checks:
        if not check.enabled:
            continue
        if check.timed == "call_length" and not exempt:
            met = exact_minutes <= params.call_limit_minutes
            decided[check.check_id] = "met" if met else "missed"
            evidence[check.check_id] = (
                f"Call lasted {call_minutes} min (limit {params.call_limit_minutes})."
            )
        elif check.timed == "email_deadline":
            if email_delta is None and contact.merged_into is not None:
                # After a merge the email is normally on the merged-into ticket, and whether the
                # merge was correct is the reviewer's call (spec §7.2 rule 7), so the reviewer judges it.
                continue
            if email_delta is None:
                decided[check.check_id] = "missed"
                evidence[check.check_id] = "No summary email found."
            else:
                met = email_delta <= deadline
                decided[check.check_id] = "met" if met else "missed"
                evidence[check.check_id] = (
                    f"Summary email sent {format_minutes(minutes_to_email)} after the call ended "
                    f"(limit {format_minutes(params.email_deadline_minutes)})."
                )

    return TimedFacts(
        call_minutes=call_minutes,
        exempt=exempt,
        email_sent=email_sent,
        minutes_to_email=minutes_to_email,
        merged_email_minutes=merged_email_minutes,
        decided=decided,
        evidence=evidence,
    )
