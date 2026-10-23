"""Read-side statistics: every number the app's screens show. Read-only (SELECTs only), no network.

Every function follows the read pattern for results (.claude-project/conventions.md):
- only current reviews (`is_current = 1`);
- each review is scored against its own rubric version, keeping only the check results whose check is
  enabled in that version, with overrides applied (`scoring.evaluate`);
- Not counted reviews (`counted = 0`) are left out of scores, verdict counts, trends, agent statistics,
  most-missed checks and problem lists. Only `contact_rows` lists them, marked `counted: False`.
- review errors (status other than ok) have no real score (only code-decided checks, worth no points):
  they are left out of score averages, trend points and check pass rates, but still count as contacts.
  Their verdict is Review failed (`review_failed`), never Problem, Needs review or Good.

Weeks run Sunday to Saturday in the configured time zone and are named by their Sunday (ISO date).
Scores, pass rates and problem rates are percentages rounded to one decimal place; agreement rates are
fractions from 0 to 1. A rate or average over nothing is None (no data), never 0.

Check labels come from the newest rubric version that contains the check, so a retired check keeps
its last label. Agents removed from the team setting keep their history everywhere.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from work_assistant import periods, rubric, scoring, settings, timeutil
from work_assistant.models import CheckDef
from work_assistant.names import stored as typed_names
from work_assistant.reviewer.catalog import DEFAULT_MODEL, FABLE_MODEL, MODELS
from work_assistant.timing import is_exempt

TREND_WEEKS = 8
LATEST_PROBLEMS = 10
FLAG_GAP = -10.0          # a check this many points (or more) below the team is a coaching target
SCOPE_CHECK_ID = "pr-scope"
VERDICT_FILTERS = ("good", "needs_review", "problem", "review_failed", "not_counted")


@dataclass(frozen=True)
class CheckLabel:
    label: str            # "#6 Triage: Verify understanding", or a process rule's name
    short: str            # "#6", or a process rule's name
    order: tuple          # display order: scorecard by number, then process rules


def check_label(check: CheckDef) -> CheckLabel:
    """How a check is named and ordered on every screen."""
    if check.kind == "scorecard" and check.number is not None:
        return CheckLabel(f"#{check.number} {check.summary}", f"#{check.number}", (0, check.number, check.summary))
    return CheckLabel(check.summary, check.summary, (1, 0, check.summary))


@dataclass
class _Review:
    ticket_id: int
    agent_email: str
    agent_name: str
    priority: str | None
    call_started_at: str
    day: date
    week: date
    version_id: int
    counted: bool
    ok: bool                                    # the review finished (status ok), so its score is real
    results: list[sqlite3.Row]                  # enabled checks of the review's version only
    evaluation: scoring.Evaluation | None       # None when Not counted
    missed: list[str]                           # missed check ids in the version's order; [] when Not counted
    needs_recheck: bool
    snapshot: str
    error: str | None                           # the stored review error, None after the retention purge
    not_counted_reason: str | None
    model: str                                  # the model that produced the review

    @property
    def verdict(self) -> str:
        return self.evaluation.verdict if self.evaluation is not None else "not_counted"

    @property
    def percent(self) -> float | None:
        return self.evaluation.percent if self.evaluation is not None else None

    def outcome(self, check_id: str) -> str | None:
        return self.evaluation.outcomes.get(check_id) if self.evaluation is not None else None


class _Data:
    """Every current review, evaluated once, plus the labels and version facts the statistics need."""

    def __init__(self, conn: sqlite3.Connection):
        self.tz = settings.get_timezone(conn)
        threshold = int(settings.get_setting(conn, "problem_threshold"))
        versions = {vid: rubric.get_version(conn, vid) for vid, _, _ in rubric.list_versions(conn)}
        self.labels: dict[str, CheckLabel] = {}
        self.call_length_ids: dict[int, set[str]] = {}
        for vid in sorted(versions):  # oldest first, so the newest version containing a check wins
            for check in versions[vid].checks:
                self.labels[check.check_id] = check_label(check)
            self.call_length_ids[vid] = {c.check_id for c in versions[vid].checks if c.timed == "call_length"}

        results: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in conn.execute(
            "SELECT cr.* FROM check_results cr JOIN reviews r ON r.review_id = cr.review_id "
            "WHERE r.is_current = 1 ORDER BY cr.rowid"
        ):
            results[row["review_id"]].append(row)

        typed = typed_names(conn)   # a tech's typed name comes before the one on the call (names.py)
        self.reviews: list[_Review] = []
        for row in conn.execute(
            "SELECT r.review_id, r.ticket_id, r.rubric_version_id, r.status, r.error, r.model, r.counted, "
            "r.not_counted_reason, c.call_started_at, c.agent_email, c.agent_name, c.priority, c.attribution_ok, "
            "c.snapshot, c.purged_at "
            "FROM reviews r JOIN contacts c ON c.ticket_id = r.ticket_id WHERE r.is_current = 1"
        ):
            version = versions[row["rubric_version_id"]]
            kept = scoring.enabled_results(version, results[row["review_id"]])
            counted = bool(row["counted"])
            evaluation = (scoring.evaluate(version, kept, bool(row["attribution_ok"]), row["status"] == "ok",
                                           threshold) if counted else None)
            missed = ([c.check_id for c in version.checks if evaluation.outcomes.get(c.check_id) == "missed"]
                      if evaluation is not None else [])
            email = row["agent_email"].strip().lower()
            self.reviews.append(_Review(
                ticket_id=row["ticket_id"], agent_email=email,
                agent_name=typed.get(email) or row["agent_name"], priority=row["priority"],
                call_started_at=row["call_started_at"],
                day=timeutil.local_date(row["call_started_at"], self.tz),
                week=timeutil.week_start_local(row["call_started_at"], self.tz),
                version_id=version.version_id, counted=counted, ok=row["status"] == "ok", results=kept,
                evaluation=evaluation,
                missed=missed, needs_recheck=any(r["needs_recheck"] for r in kept), snapshot=row["snapshot"],
                error=_review_error(row), not_counted_reason=None if counted else row["not_counted_reason"],
                model=row["model"] or DEFAULT_MODEL,
            ))
        self.reviews.sort(key=lambda r: (r.call_started_at, r.ticket_id), reverse=True)  # newest first

        # Team-wide, by local call day: the rubric versions counted reviews used, and how many current
        # reviews each model made. The change markers sum these days into any step.
        versions: dict[date, set[int]] = defaultdict(set)
        models: dict[date, Counter[str]] = defaultdict(Counter)
        for review in self.reviews:
            if review.counted:
                versions[review.day].add(review.version_id)
            models[review.day][review.model] += 1
        self.versions_by_day = dict(versions)
        self.models_by_day = dict(models)
        self.version_days = sorted(versions)
        self.model_days = sorted(models)

    def counted(self, agent: str | None = None) -> list[_Review]:
        email = _email(agent)
        return [r for r in self.reviews if r.counted and (email is None or r.agent_email == email)]

    def rubric_changed(self, start: date, end: date, unit: str) -> bool:
        """True when the step's counted reviews used a rubric version that the most recent earlier step
        (of the same unit) with counted reviews did not use. The first step with data is never marked."""
        now = _union(self.versions_by_day, self.version_days, start, end)
        before = _previous_step(self.version_days, start, unit)
        if not now or before is None:
            return False
        return bool(now - _union(self.versions_by_day, self.version_days, *before))

    def model_changed(self, start: date, end: date, unit: str) -> bool:
        """True when the step's most common reviewing model differs from that of the most recent earlier
        step (of the same unit) with current reviews. Steps without reviews are skipped; the first step is
        never marked."""
        now = _most_common_model(self.models_by_day, self.model_days, start, end)
        before = _previous_step(self.model_days, start, unit)
        if now is None or before is None:
            return False
        return now != _most_common_model(self.models_by_day, self.model_days, *before)

    def label(self, check_id: str) -> str:
        return self.labels[check_id].label

    def order(self, check_id: str) -> tuple:
        return self.labels[check_id].order

    def row(self, review: _Review) -> dict[str, Any]:
        evaluation = review.evaluation
        scored = evaluation is not None and review.ok   # a review error has no real score, never a 0
        return {
            "ticket_id": review.ticket_id,
            "date": review.day.isoformat(),
            "agent_email": review.agent_email,
            "agent_name": review.agent_name,
            "priority": review.priority,
            "earned": evaluation.earned if scored else None,
            "total": evaluation.total if evaluation else None,
            "percent": evaluation.percent if scored else None,
            "verdict": review.verdict,
            "counted": review.counted,
            "missed": [self.labels[c].short for c in review.missed],
            "missed_ids": list(review.missed),
            "needs_recheck": review.needs_recheck,
            "review_error": not review.ok,      # the review did not finish: its score is not real
            "error": review.error,
            "not_counted_reason": review.not_counted_reason,
        }

    def check_options(self) -> list[dict[str, str]]:
        """Every check any rubric version has had, in display order, with its newest label."""
        return [{"check_id": c, "label": self.labels[c].label} for c in sorted(self.labels, key=self.order)]


REFUSED_MESSAGE = "The model declined to review this call."
FABLE_RETENTION_MESSAGE = (
    "Claude Fable 5.1 needs 30-day data retention, which your Anthropic organization doesn't allow.")


def _review_error(row: sqlite3.Row) -> str | None:
    """The error shown for a review that did not finish. Left out once the call's content is purged:
    an invalid-output message can quote the reviewer's output, which may quote the customer."""
    if row["status"] == "ok" or row["purged_at"] is not None:
        return None
    return plain_error(row["error"], row["model"])


def plain_error(error: str | None, model: str | None) -> str | None:
    """A stored review error in the owner's words, where it has a plain meaning; otherwise as stored.

    Keyed off the stored text (see `reviewer.batch`), so reviews stored before these messages show
    them too. Fable 5.1 rejects every request from an organization with zero data retention as an
    invalid request (spec §5.1).
    """
    if error is not None and error.startswith("refused"):
        return REFUSED_MESSAGE
    if error == "request errored: invalid_request_error" and model == FABLE_MODEL:
        return FABLE_RETENTION_MESSAGE
    return error


def model_label(model_id: str) -> str:
    """The model's name for the owner; a model no longer in the catalog shows its id."""
    model = MODELS.get(model_id)
    return model.label if model is not None else model_id


def _email(agent: str | None) -> str | None:
    return agent.strip().lower() if agent else None


def _avg(reviews: Iterable[_Review]) -> float | None:
    """Mean score of the reviews that have one: review errors are left out."""
    percents = [r.percent for r in reviews if r.ok]
    return round(sum(percents) / len(percents), 1) if percents else None


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _problem_rate(reviews: list[_Review]) -> float | None:
    return _pct(sum(r.verdict == "problem" for r in reviews), len(reviews))


_MODEL_ORDER = {model_id: i for i, model_id in enumerate(MODELS)}


def _days_between(days: list[date], start: date, end: date) -> list[date]:
    """The days of the sorted `days` from `start` to `end`, both included."""
    return days[bisect_left(days, start):bisect_right(days, end)]


def _union(by_day: dict[date, set[int]], days: list[date], start: date, end: date) -> set[int]:
    return set().union(*(by_day[d] for d in _days_between(days, start, end)))


def _most_common_model(by_day: dict[date, Counter[str]], days: list[date], start: date, end: date) -> str | None:
    """The model that made the most reviews from `start` to `end`, ties to the first in catalog order; None
    without reviews."""
    counts: Counter[str] = Counter()
    for day in _days_between(days, start, end):
        counts.update(by_day[day])
    if not counts:
        return None
    return min(counts, key=lambda m: (-counts[m], _MODEL_ORDER.get(m, len(_MODEL_ORDER)), m))


def _step_start(day: date, unit: str) -> date:
    """The first day of the trend step of `unit` (see `periods.TREND_UNITS`) that holds `day`."""
    if unit == "day":
        return day
    if unit == "week":
        return timeutil.week_start(day)
    if unit == "month":
        return day.replace(day=1)
    raise ValueError(f"Unknown trend unit: {unit}")


def _previous_step(days: list[date], start: date, unit: str) -> tuple[date, date] | None:
    """The most recent step of `unit` before `start` that has data (one of the sorted `days`), from its first
    day to its last day with data; None when no day before `start` has data."""
    i = bisect_left(days, start)
    if i == 0:
        return None
    last = days[i - 1]
    return _step_start(last, unit), last


def _search_text(review: _Review) -> str:
    parts = [str(review.ticket_id), review.agent_name, review.agent_email]
    try:
        snapshot = json.loads(review.snapshot)
    except (TypeError, ValueError):
        snapshot = {}
    if isinstance(snapshot, dict):
        parts.append(str(snapshot.get("subject") or ""))
        parts.extend(str(c.get("text") or "") for c in snapshot.get("comments") or [] if isinstance(c, dict))
    return "\n".join(parts).lower()


# -- Tickets ------------------------------------------------------------------------------------------------


def contact_rows(conn: sqlite3.Connection, *, start: date | None, end: date | None, agent: str | None,
                 verdict: str | None, missed_check: str | None, priority: str | None,
                 q: str | None) -> list[dict[str, Any]]:
    """The Tickets list, newest call first.

    Dates are local call days, both ends included. `verdict` is good, needs_review, problem,
    review_failed (the review did not finish) or not_counted; None lists every current review, Not counted ones included (marked `counted: False`,
    verdict `not_counted`, no score, no misses). `missed_check` is a check id whose effective outcome
    is missed. `q` matches the ticket number, the agent's name or email, the subject, or comment text.

    Each row's `earned` and `percent` are None for a review error (no real score) and for Not counted;
    `error` is the review error's text (None once the call is purged) and `not_counted_reason` the
    reviewer's reason for a Not counted call.
    """
    return _filtered_rows(_Data(conn), start=start, end=end, agent=agent, verdict=verdict,
                          missed_check=missed_check, priority=priority, q=q)


def _filtered_rows(data: _Data, *, start: date | None, end: date | None, agent: str | None, verdict: str | None,
                   missed_check: str | None, priority: str | None, q: str | None) -> list[dict[str, Any]]:
    if verdict is not None and verdict not in VERDICT_FILTERS:
        raise ValueError(f"Unknown verdict filter: {verdict}")
    email = _email(agent)
    needle = q.strip().lstrip("#").lower() if q and q.strip().lstrip("#") else None
    rows = []
    for review in data.reviews:
        if start is not None and review.day < start:
            continue
        if end is not None and review.day > end:
            continue
        if email is not None and review.agent_email != email:
            continue
        if verdict is not None and review.verdict != verdict:
            continue
        if missed_check is not None and missed_check not in review.missed:
            continue
        if priority is not None and (review.priority or "").lower() != priority.strip().lower():
            continue
        if needle is not None and needle not in _search_text(review):
            continue
        rows.append(data.row(review))
    return rows


PRIORITY_ORDER = ("urgent", "high", "normal", "low")


TICKET_SORTS = ("newest", "oldest", "score_low", "score_high", "agent")


def _sorted_rows(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """`rows` (newest first) in the order `sort` names. Calls without a score (a failed review, Not counted)
    go last when sorting by score; equal keys keep newest first."""
    if sort == "oldest":
        return rows[::-1]
    if sort in ("score_low", "score_high"):
        scored = [r for r in rows if r["percent"] is not None]
        scored.sort(key=lambda r: r["percent"], reverse=sort == "score_high")
        return scored + [r for r in rows if r["percent"] is None]
    if sort == "agent":
        return sorted(rows, key=lambda r: (r["agent_name"] or r["agent_email"] or "").lower())
    return rows


def tickets(conn: sqlite3.Connection, *, start: date | None, end: date | None, agent: str | None,
            verdict: str | None, missed_check: str | None, priority: str | None,
            q: str | None, sort: str = "newest") -> dict[str, Any]:
    """The Tickets screen's list and its filter choices.

    `rows` are `contact_rows` with one difference: without a verdict filter, Not counted calls are left
    out (they are not part of any score); the `not_counted` filter lists them. They come in `sort` order
    (one of `TICKET_SORTS`, newest first by default). `total` is the number of rows. The choices cover
    every current review whatever the filters: `agent_options` (`{email, name}` by name), `check_options`
    (`{check_id, label}` in display order) and `priorities` (urgent, high, normal, low first, then any
    others alphabetically).
    """
    data = _Data(conn)
    rows = _filtered_rows(data, start=start, end=end, agent=agent, verdict=verdict, missed_check=missed_check,
                          priority=priority, q=q)
    if verdict is None:
        rows = [r for r in rows if r["counted"]]
    rows = _sorted_rows(rows, sort)
    names: dict[str, str] = {}
    for review in data.reviews:  # newest first, so the first name seen is the agent's newest
        names.setdefault(review.agent_email, review.agent_name)
    found = {r.priority.lower() for r in data.reviews if r.priority}
    return {
        "rows": rows,
        "total": len(rows),
        "agent_options": [{"email": e, "name": n}
                          for e, n in sorted(names.items(), key=lambda item: (item[1].lower(), item[0]))],
        "check_options": data.check_options(),
        "priorities": [p for p in PRIORITY_ORDER if p in found] + sorted(found - set(PRIORITY_ORDER)),
    }


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def zendesk_url(subdomain: str | None, ticket_id: int) -> str | None:
    """The agent-interface link to a ticket, or None until the Zendesk subdomain is set."""
    return f"https://{subdomain}.zendesk.com/agent/tickets/{int(ticket_id)}" if subdomain else None


def _comments(items: Any) -> list[dict[str, Any]]:
    """Stored comments as plain data, oldest first. The text is untrusted customer content: the page
    renders it as text, never as markup."""
    comments = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        comments.append({
            "comment_id": c.get("comment_id"),
            "created_at": c.get("created_at"),
            "author_name": c.get("author_name"),
            "author_role": c.get("author_role"),
            "public": bool(c.get("public")),
            "kind": c.get("kind") or "other",
            "text": str(c.get("text") or ""),
            "images": len(c.get("image_urls") or []),
        })
    comments.sort(key=lambda c: (c["created_at"] or "", c["comment_id"] or 0))
    return comments


def _timeline(contact: sqlite3.Row, snapshot: dict, version: Any, subdomain: str | None,
              purged: bool) -> dict[str, Any]:
    """The process-rule timeline: every time is ISO UTC, or None when unknown or not applicable.

    The call-length limit is shown only for a call that is not exempt (under the review's own rubric
    version) when that version checks call length, and the summary-email deadline only when it checks
    the email deadline. A purged call keeps only its start time.
    """
    params = version.params
    enabled = {c.timed for c in version.checks if c.enabled and c.timed}
    start = _parse_iso(contact["call_started_at"])
    timeline: dict[str, Any] = {
        "call_start": contact["call_started_at"], "call_end": None, "triage_macro_at": None,
        "call_limit_at": None, "call_limit_minutes": params.call_limit_minutes, "exempt": None,
        "summary_email_at": None, "email_deadline_at": None, "email_deadline_minutes": params.email_deadline_minutes,
        "callback_at": None, "merged_into": None,
    }
    if purged or start is None:
        return timeline
    duration = snapshot.get("call_duration_seconds")
    end = start + timedelta(seconds=duration) if isinstance(duration, (int, float)) else None
    tags = snapshot.get("tags") if isinstance(snapshot.get("tags"), list) else []
    exempt = is_exempt(contact["priority"], tags, params)
    macros = sorted(c["created_at"] for c in _comments(snapshot.get("comments"))
                    if c["kind"] == "triage_macro" and c["created_at"])
    callback = snapshot.get("callback_minutes")
    merged = snapshot.get("merged_into")
    timeline.update({
        "call_end": _iso_z(end) if end else None,
        "triage_macro_at": macros[0] if macros else None,
        "exempt": exempt,
        "call_limit_at": (_iso_z(start + timedelta(minutes=params.call_limit_minutes))
                          if "call_length" in enabled and not exempt else None),
        "summary_email_at": snapshot.get("summary_email_at"),
        "email_deadline_at": (_iso_z(end + timedelta(minutes=params.email_deadline_minutes))
                              if end and "email_deadline" in enabled else None),
        "callback_at": (_iso_z(end + timedelta(minutes=callback))
                        if end and isinstance(callback, (int, float)) else None),
        "merged_into": ({"ticket_id": merged["ticket_id"], "subject": merged.get("subject"),
                         "zendesk_url": zendesk_url(subdomain, merged["ticket_id"])}
                        if isinstance(merged, dict) and isinstance(merged.get("ticket_id"), int) else None),
    })
    return timeline


def ticket_detail(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    """One call's detail panel, or None when the ticket has no current review.

    Follows the read pattern: the current review, scored against its own rubric version with only that
    version's enabled checks, overrides applied. Keys:
    - `contact`: ticket_id, subject, agent, priority, channel, call start, local `date`, attribution_ok,
      `purged` and `purged_at`.
    - `review`: review_id, version_id, status, `review_error`, `error` (None once purged), counted,
      not_counted_reason, resolved_on_call, verdict (not_counted when Not counted), and earned, total,
      percent (earned and percent None for a review error or a Not counted call: never a 0 score);
      `model_label` (the reviewing model's name) and `effort` (None for reviews stored before effort was
      recorded).
    - `timeline` (see `_timeline`).
    - `checks`: every enabled check of the version, scorecard by number then process rules, each with
      the stored outcome, evidence (None once purged), decided_by (code or model), the owner's override
      and note, needs_recheck, the effective outcome and, for judged scorecard checks, `earned_points`
      (None for a check without an effective outcome, so a review error never shows 0 / N).
      `outcome` is None for a check without a stored result (a review error stores only measured ones).
    - `comments` and `merged_comments` (the merged-into ticket's), oldest first; empty once purged.
    - `zendesk_url` (None until the Zendesk subdomain is set) and `retention_days`.
    """
    review = conn.execute(
        "SELECT * FROM reviews WHERE ticket_id = ? AND is_current = 1", (ticket_id,)).fetchone()
    contact = conn.execute("SELECT * FROM contacts WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if review is None or contact is None:
        return None
    version = rubric.get_version(conn, review["rubric_version_id"])
    model_id = review["model"] or DEFAULT_MODEL
    threshold = int(settings.get_setting(conn, "problem_threshold"))
    subdomain = settings.get_setting(conn, "zendesk_subdomain")
    tz = settings.get_timezone(conn)
    purged = contact["purged_at"] is not None
    try:
        snapshot = json.loads(contact["snapshot"])
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}

    stored = conn.execute("SELECT * FROM check_results WHERE review_id = ? ORDER BY rowid",
                          (review["review_id"],)).fetchall()
    kept = scoring.enabled_results(version, stored)
    by_id = {r["check_id"]: r for r in kept}
    counted = bool(review["counted"])
    ok = review["status"] == "ok"
    evaluation = scoring.evaluate(version, kept, bool(contact["attribution_ok"]), ok, threshold)
    scored = counted and ok

    enabled = [c for c in version.checks if c.enabled]
    enabled.sort(key=lambda c: (0, c.number or 0) if c.kind == "scorecard" else (1, 0))
    checks = []
    for check in enabled:
        result = by_id.get(check.check_id)
        effective = evaluation.outcomes.get(check.check_id)
        checks.append({
            "check_id": check.check_id, "kind": check.kind, "number": check.number, "summary": check.summary,
            "points": check.points, "auto_fail": check.auto_fail,
            "outcome": result["outcome"] if result else None,
            "evidence": None if purged or result is None else result["evidence"],
            "decided_by": result["decided_by"] if result else None,
            "override_outcome": result["override_outcome"] if result else None,
            "override_note": result["override_note"] if result else None,
            "override_at": result["override_at"] if result else None,
            "needs_recheck": bool(result["needs_recheck"]) if result else False,
            "effective": effective,
            # None for a process rule, and for a check never judged (a review error): never a 0 / N.
            "earned_points": ((check.points if effective in ("met", "na", "unsure") else 0)
                              if check.kind == "scorecard" and effective is not None else None),
        })

    merged = None if purged else snapshot.get("merged_into")
    return {
        "contact": {
            "ticket_id": contact["ticket_id"], "subject": None if purged else snapshot.get("subject"),
            "agent_email": contact["agent_email"].strip().lower(),
            "agent_name": typed_names(conn).get(contact["agent_email"].strip().lower()) or contact["agent_name"],
            "priority": contact["priority"], "channel": contact["channel"],
            "call_started_at": contact["call_started_at"],
            "date": timeutil.local_date(contact["call_started_at"], tz).isoformat(),
            "attribution_ok": bool(contact["attribution_ok"]),
            "purged": purged, "purged_at": contact["purged_at"],
        },
        "review": {
            "review_id": review["review_id"], "version_id": version.version_id, "status": review["status"],
            "review_error": not ok, "error": None if ok or purged else plain_error(review["error"], review["model"]),
            "counted": counted, "not_counted_reason": None if counted else review["not_counted_reason"],
            "resolved_on_call": review["resolved_on_call"],
            "verdict": evaluation.verdict if counted else "not_counted",
            "earned": evaluation.earned if scored else None,
            "total": evaluation.total,
            "percent": evaluation.percent if scored else None,
            "created_at": review["created_at"],
            "model_label": model_label(model_id), "effort": review["effort"],
        },
        "timeline": _timeline(contact, snapshot, version, subdomain, purged),
        "checks": checks,
        "comments": [] if purged else _comments(snapshot.get("comments")),
        "merged_comments": _comments(merged.get("comments")) if isinstance(merged, dict) else [],
        "zendesk_url": zendesk_url(subdomain, contact["ticket_id"]),
        "retention_days": int(settings.get_setting(conn, "retention_days")),
    }


# -- Team overview --------------------------------------------------------------------------------------------


def _within(reviews: list[_Review], period: periods.Period | None) -> list[_Review]:
    """The reviews whose local call day is in `period` (none for an empty period, or for no period)."""
    if period is None:
        return []
    return [r for r in reviews if period.start <= r.day <= period.end]


def _figures(count: int, calls: int, compare_count: int, compare_calls: int) -> dict[str, Any]:
    """A count with its share of the calls, the same for the comparison, and the changes: in share
    (percentage points, so periods of different sizes compare fairly) and in count. A side without calls
    has no figures (None, never 0), and neither has a change that needs it."""
    count_or_none = count if calls else None
    compare_or_none = compare_count if compare_calls else None
    share, compare_share = _pct(count, calls), _pct(compare_count, compare_calls)
    return {
        "count": count_or_none,
        "share_pct": share,
        "compare_count": compare_or_none,
        "compare_share_pct": compare_share,
        "change_pts": round(share - compare_share, 1) if share is not None and compare_share is not None else None,
        "count_change": (count_or_none - compare_or_none
                         if count_or_none is not None and compare_or_none is not None else None),
    }


def _tiles(data: _Data, now: list[_Review], then: list[_Review]) -> dict[str, Any]:
    """The headline tiles for the period's reviews (`now`) against the comparison's (`then`)."""
    def call_length(review: _Review) -> bool:
        return any(c in data.call_length_ids[review.version_id] for c in review.missed)

    counts = {
        "problems": lambda r: r.verdict == "problem",
        "call_length_misses": call_length,
        "scope_misses": lambda r: SCOPE_CHECK_ID in r.missed,
    }
    avg, compare_avg = _avg(now), _avg(then)
    return {
        "calls": {"count": len(now) or None, "compare_count": len(then) or None},
        "avg_score": {"avg_percent": avg, "compare_avg_percent": compare_avg,
                      "change": round(avg - compare_avg, 1) if avg is not None and compare_avg is not None else None},
        **{key: _figures(sum(map(test, now)), len(now), sum(map(test, then)), len(then))
           for key, test in counts.items()},
    }


def _miss_counts(reviews: Iterable[_Review]) -> Counter[str]:
    return Counter(check_id for r in reviews for check_id in r.missed)


def _partial(step: periods.Step, period: periods.Period) -> bool:
    """True for a step in the period that runs past either end of it (a custom range's first or last week
    or month): it counts only the period's days."""
    return step.in_period and (step.start < period.start or step.end > period.end)


def _by_step(reviews: Iterable[_Review], steps: list[periods.Step], period: periods.Period) -> list[list[_Review]]:
    """The reviews in each trend step (by local call day), in the steps' order. A step in the period holds
    only the period's reviews, so the in-period steps add up to the period's figures; a context step is
    whole. Reviews outside every step are left out."""
    starts = [s.start for s in steps]
    buckets: list[list[_Review]] = [[] for _ in steps]
    for review in reviews:
        i = bisect_right(starts, review.day) - 1
        if i < 0 or review.day > steps[i].end:
            continue
        if steps[i].in_period and not period.start <= review.day <= period.end:
            continue
        buckets[i].append(review)
    return buckets


def overview(conn: sqlite3.Connection, period: periods.Period, compare: periods.Period | None,
             agent: str | None = None, *, last_reviewed: date | None) -> dict[str, Any]:
    """The Team overview for `period`, compared with `compare` (None when the owner compares with nothing).

    Every figure counts the current, counted reviews whose local call day is in the period; `agent` (an
    email) narrows every figure to that agent. `last_reviewed` is the last day reviewed (as for
    `periods.resolve`), which marks the trend's unfinished last step.

    Keys:
    - `period` and `comparison`: `Period.as_dict()`, `comparison` None for no comparison; `agent` (the
      normalized email, or None).
    - `has_data_in_period` and `has_data_in_comparison`: whether each has any call (False for no
      comparison). Without calls a side's figures are None, never 0.
    - `tiles`: `calls` `{count, compare_count}`; `avg_score` `{avg_percent, compare_avg_percent, change}`;
      and the counts `problems`, `call_length_misses` and `scope_misses`, each `{count, share_pct,
      compare_count, compare_share_pct, change_pts, count_change}`, the share being of the side's calls and
      `change_pts` the change in share, in percentage points.
    - `trend`: `{unit, steps}`, the steps from `periods.trend_steps` (oldest first) with `start`, `end`,
      `in_period`, `so_far`, `partial`, `calls`, `avg_percent` and `problem_share_pct` (both None for a step
      without calls), and `rubric_changed` and `model_changed`, marked team-wide whatever the agent. A step
      in the period counts only the period's calls, so the in-period steps add up to the tiles; `partial`
      marks one that runs past the period's start or end. Context steps are whole. The change markers look
      at the whole step.
    - `most_missed`: every check missed in the period, most missed first, each `{check_id, label}` with the
      count figures of the tiles.
    - `agents`: each agent with calls in the period (by name): `email`, `name` (on their newest call),
      `contacts`, `avg_percent`, `problems`, `problem_rate`, `top_miss`, and `trend`, their average score at
      each trend step.
    - `problems`: the period's problem calls, newest first, at most 10 (`tiles.problems.count` has them all).
    - The filter choices, team-wide whatever the period and agent: `agent_options` (`{email, name}` of
      agents with counted reviews, by name), and `has_data` (any counted review at all).
    """
    data = _Data(conn)
    team_reviews = data.counted()
    names: dict[str, str] = {}
    for review in team_reviews:  # newest first, so the first name seen is the agent's newest
        names.setdefault(review.agent_email, review.agent_name)
    filters = {
        "agent_options": [{"email": e, "name": n}
                          for e, n in sorted(names.items(), key=lambda item: (item[1].lower(), item[0]))],
        "has_data": bool(team_reviews),
    }
    reviews = data.counted(agent)
    now, then = _within(reviews, period), _within(reviews, compare)

    unit = periods.trend_unit(period)
    steps = periods.trend_steps(period, unit, last_reviewed=last_reviewed)
    trend = {"unit": unit, "steps": [{
        "start": step.start.isoformat(),
        "end": step.end.isoformat(),
        "in_period": step.in_period,
        "so_far": step.so_far,
        "partial": _partial(step, period),
        "calls": len(bucket),
        "avg_percent": _avg(bucket),
        "problem_share_pct": _problem_rate(bucket),
        "rubric_changed": data.rubric_changed(step.start, step.end, unit),
        "model_changed": data.model_changed(step.start, step.end, unit),
    } for step, bucket in zip(steps, _by_step(reviews, steps, period))]}

    now_counts, then_counts = _miss_counts(now), _miss_counts(then)
    most_missed = [
        {"check_id": c, "label": data.label(c), **_figures(n, len(now), then_counts[c], len(then))}
        for c, n in sorted(now_counts.items(), key=lambda item: (-item[1], data.order(item[0])))
    ]

    by_agent: dict[str, list[_Review]] = defaultdict(list)
    for review in reviews:
        by_agent[review.agent_email].append(review)
    agents = []
    for email, own in by_agent.items():
        own_now = _within(own, period)
        if not own_now:
            continue
        misses = _miss_counts(own_now)
        top = min(misses, key=lambda c: (-misses[c], data.order(c))) if misses else None
        agents.append({
            "email": email,
            "name": own[0].agent_name,  # the name on their newest call
            "contacts": len(own_now),
            "avg_percent": _avg(own_now),
            "problems": sum(r.verdict == "problem" for r in own_now),
            "problem_rate": _problem_rate(own_now),
            "trend": [_avg(bucket) for bucket in _by_step(own, steps, period)],
            "top_miss": data.label(top) if top else None,
        })
    agents.sort(key=lambda a: (a["name"].lower(), a["email"]))

    return {
        "period": period.as_dict(),
        "comparison": compare.as_dict() if compare is not None else None,
        "agent": _email(agent),
        "has_data_in_period": bool(now),
        "has_data_in_comparison": bool(then),
        "tiles": _tiles(data, now, then),
        "trend": trend,
        "most_missed": most_missed,
        "agents": agents,
        "problems": [data.row(r) for r in now if r.verdict == "problem"][:LATEST_PROBLEMS],
        **filters,
    }


# -- Agents ---------------------------------------------------------------------------------------------------


def _pass_counts(reviews: Iterable[_Review]) -> tuple[Counter[str], Counter[str]]:
    """Per check: how many reviews it applied to (any outcome but N/A), and how many of those it was not
    missed. Review errors are left out."""
    applied: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    for review in reviews:
        if not review.ok:
            continue
        for check_id, outcome in review.evaluation.outcomes.items():
            if outcome == "na":
                continue
            applied[check_id] += 1
            passed[check_id] += outcome != "missed"
    return applied, passed


def agent_detail(conn: sqlite3.Connection, email: str, period: periods.Period, *,
                 last_reviewed: date | None) -> dict[str, Any]:
    """One agent's page for `period`: tiles against the team, the score trend, pass rate by check, problems
    and notes.

    Every figure counts the current, counted reviews whose local call day is in the period, the agent's
    against the whole team's over the same days. `last_reviewed` is the last day reviewed (as for
    `periods.resolve`), which marks the trend's unfinished last step. Without calls of the agent's in the
    period (`has_data_in_period` False) their figures are None, never 0; the team's still show.

    Keys:
    - `email`, `name` (on their newest call; None before any), `active` (on the team now); `period`
      (`Period.as_dict()`), `has_data_in_period`.
    - `tiles`: `avg_percent` and `team_avg_percent`, `problem_rate` and `team_problem_rate`, `contacts` (the
      agent's calls) and `contacts_rank` (1 for the most calls among the agents with calls in the period),
      and `biggest_gap` `{check_id, label, gap}`, the check the agent is furthest behind the team on.
    - `trend`: `{unit, steps}`, the steps from `periods.trend_steps` (oldest first) with `start`, `end`,
      `in_period`, `so_far`, `partial`, `calls` (the agent's), `agent` and `team` (the average scores, None
      without calls), and `rubric_changed` and `model_changed` (team-wide). As on the Team overview, a step
      in the period counts only the period's calls and context steps are whole.
    - `checks`: each check in display order with `agent_rate`, `team_rate`, `gap` and `applied_count` (the
      agent's calls it applied to). A pass rate is the share of the calls a check applied to (not N/A) where
      it was not missed; rows `FLAG_GAP` points or more below the team are `flagged`.
    - `problems`: the agent's problem calls in the period, all of them, newest first.
    - `notes`: the owner's coaching notes, newest first, whatever the period.
    """
    data = _Data(conn)
    email = email.strip().lower()
    team = data.counted()
    own = [r for r in team if r.agent_email == email]
    team_now, own_now = _within(team, period), _within(own, period)

    team_applied, team_passed = _pass_counts(team_now)
    own_applied, own_passed = _pass_counts(own_now)
    checks = []
    for check_id in sorted(team_applied.keys() | {c for r in team_now for c in r.evaluation.outcomes if r.ok},
                           key=data.order):
        agent_rate = _pct(own_passed[check_id], own_applied[check_id])
        team_rate = _pct(team_passed[check_id], team_applied[check_id])
        gap = round(agent_rate - team_rate, 1) if agent_rate is not None and team_rate is not None else None
        checks.append({
            "check_id": check_id, "label": data.label(check_id), "agent_rate": agent_rate, "team_rate": team_rate,
            "gap": gap, "flagged": gap is not None and gap <= FLAG_GAP, "applied_count": own_applied[check_id],
        })
    behind = [c for c in checks if c["gap"] is not None and c["gap"] < 0]
    biggest = min(behind, key=lambda c: c["gap"]) if behind else None

    per_agent = Counter(r.agent_email for r in team_now)
    contacts = per_agent.get(email)

    unit = periods.trend_unit(period)
    steps = periods.trend_steps(period, unit, last_reviewed=last_reviewed)
    trend = {"unit": unit, "steps": [{
        "start": step.start.isoformat(),
        "end": step.end.isoformat(),
        "in_period": step.in_period,
        "so_far": step.so_far,
        "partial": _partial(step, period),
        "calls": len(mine),
        "agent": _avg(mine),
        "team": _avg(everyone),
        "rubric_changed": data.rubric_changed(step.start, step.end, unit),
        "model_changed": data.model_changed(step.start, step.end, unit),
    } for step, mine, everyone in zip(steps, _by_step(own, steps, period), _by_step(team, steps, period))]}

    name_row = conn.execute(
        "SELECT agent_name FROM contacts WHERE lower(trim(agent_email)) = ? ORDER BY call_started_at DESC LIMIT 1",
        (email,)).fetchone()
    notes = conn.execute(
        "SELECT note_id, created_at, body FROM coaching_notes WHERE lower(trim(agent_email)) = ? "
        "ORDER BY created_at DESC, note_id DESC", (email,)).fetchall()

    return {
        "email": email,
        "name": typed_names(conn).get(email) or (name_row["agent_name"] if name_row else None),
        "active": email in settings.team_emails(conn),
        "period": period.as_dict(),
        "has_data_in_period": bool(own_now),
        "tiles": {
            "avg_percent": _avg(own_now),
            "team_avg_percent": _avg(team_now),
            "problem_rate": _problem_rate(own_now),
            "team_problem_rate": _problem_rate(team_now),
            "contacts": contacts,
            "contacts_rank": 1 + sum(n > contacts for n in per_agent.values()) if contacts else None,
            "biggest_gap": ({"check_id": biggest["check_id"], "label": biggest["label"], "gap": biggest["gap"]}
                            if biggest else None),
        },
        "trend": trend,
        "checks": checks,
        "problems": [data.row(r) for r in own_now if r.verdict == "problem"],
        "notes": [dict(n) for n in notes],
    }


def agent_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every agent the Agents screen can show: anyone with calls on record, on the team setting, or with
    coaching notes. Agents removed from the team keep their place (`active: False`).

    Each is `{email, name, active, has_history}`: the normalized email, the name typed when they were added
    (names.py), else the name on their newest call (None before their first call), whether they are on the team now,
    and whether any call of theirs is stored. Sorted by name (agents without one by email).
    """
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT lower(trim(agent_email)) AS email, agent_name FROM contacts ORDER BY call_started_at DESC"
    ):
        names.setdefault(row["email"], row["agent_name"])
    typed = typed_names(conn)
    team = settings.team_emails(conn)
    noted = {row[0] for row in conn.execute("SELECT DISTINCT lower(trim(agent_email)) FROM coaching_notes")}
    emails = set(names) | set(team) | noted
    agents = [{"email": e, "name": typed.get(e) or names.get(e), "active": e in team, "has_history": e in names}
              for e in emails]
    agents.sort(key=lambda a: ((a["name"] or a["email"]).lower(), a["email"]))
    return agents


# -- Reviewer agreement ---------------------------------------------------------------------------------------


def agreement(conn: sqlite3.Connection, start: date | None, end: date | None) -> dict[str, Any]:
    """How often the owner agreed with the reviewer: 1 − overridden / total, over the enabled checks of
    counted, current reviews whose local call day is in [start, end] (None leaves that end open).

    An override counts as a disagreement only when it differs from the reviewer's outcome. Rates are
    fractions; `overall` is None when there are no checks.
    """
    data = _Data(conn)
    total: Counter[str] = Counter()
    overridden: Counter[str] = Counter()
    for review in data.counted():
        if (start is not None and review.day < start) or (end is not None and review.day > end):
            continue
        for row in review.results:
            total[row["check_id"]] += 1
            changed = row["override_outcome"] is not None and row["override_outcome"] != row["outcome"]
            overridden[row["check_id"]] += changed
    checks = sum(total.values())
    return {
        "overall": 1 - sum(overridden.values()) / checks if checks else None,
        "by_check": {c: 1 - overridden[c] / n for c, n in sorted(total.items(), key=lambda i: data.order(i[0]))},
    }
