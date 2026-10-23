"""Scores and verdicts from per-check outcomes. Pure functions, no I/O."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from work_assistant.models import Outcome, RubricVersion
from work_assistant.rubric import scorecard_total

Verdict = Literal["review_failed", "problem", "needs_review", "good"]

_EARNING: frozenset[str] = frozenset({"met", "na", "unsure"})


def effective(outcome: str, override: str | None) -> Outcome:
    """The outcome that counts: the owner's override when set, otherwise the reviewer's outcome."""
    return (override if override is not None else outcome)  # type: ignore[return-value]


def score(version: RubricVersion, outcomes: dict[str, Outcome]) -> tuple[int, int, float]:
    """Return (earned, total, percent) for one contact against one rubric version.

    Only enabled scorecard checks count. A check earns its points when its outcome is met,
    na, or unsure. A check with no entry in `outcomes` (for example after a review error)
    earns nothing and is not treated as a miss. If any enabled auto-fail scorecard check is
    missed, earned is 0. Total is the version's scorecard total; percent is rounded to one
    decimal place.
    """
    earned = 0
    for check in version.checks:
        if check.kind != "scorecard" or not check.enabled:
            continue
        outcome = outcomes.get(check.check_id)
        if outcome == "missed" and check.auto_fail:
            earned = 0
            break
        if outcome in _EARNING:
            earned += check.points
    total = scorecard_total(version)
    percent = round(earned / total * 100, 1) if total else 0.0
    return earned, total, percent


def verdict(
    outcomes: dict[str, Outcome],
    attribution_ok: bool,
    review_ok: bool,
    problem_threshold: int,
) -> Verdict:
    """Decide the contact's verdict.

    `review_failed` when the review did not finish (a review error, including one that produced no
    outcomes): its measured checks alone say nothing about the call, so no other verdict applies.
    Otherwise `problem` when misses (across scorecard and process checks) reach the threshold.
    Otherwise `needs_review` for any unsure outcome or uncertain attribution. Otherwise `good`.
    """
    if not review_ok:
        return "review_failed"
    values = list(outcomes.values())
    if sum(1 for o in values if o == "missed") >= problem_threshold:
        return "problem"
    if "unsure" in values or not attribution_ok:
        return "needs_review"
    return "good"


@dataclass(frozen=True)
class Evaluation:
    """One counted review scored against its own rubric version."""
    outcomes: dict[str, Outcome]  # effective outcomes of the version's enabled checks
    earned: int
    total: int
    percent: float
    verdict: Verdict


def enabled_results(version: RubricVersion, rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The stored check results whose check is enabled in `version` (the review's own version)."""
    enabled = {c.check_id for c in version.checks if c.enabled}
    return [r for r in rows if r["check_id"] in enabled]


def evaluate(version: RubricVersion, rows: Iterable[Mapping[str, Any]], attribution_ok: bool, review_ok: bool,
             problem_threshold: int) -> Evaluation:
    """Score and classify one review from its stored check results (`check_id`, `outcome`,
    `override_outcome`): only checks enabled in the review's version count, and overrides apply."""
    outcomes = {r["check_id"]: effective(r["outcome"], r["override_outcome"]) for r in enabled_results(version, rows)}
    earned, total, percent = score(version, outcomes)
    return Evaluation(outcomes, earned, total, percent,
                      verdict(outcomes, attribution_ok, review_ok, problem_threshold))
