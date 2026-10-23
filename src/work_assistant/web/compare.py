"""The model comparison report (spec §5.3): a model test's reviews set against the shown reviews they were
paired with. Read-only (SELECTs only), no network.

What is compared with what:
- Each test review is paired with the call's shown review as it was when the test chose the call
  (`reviews.compared_review_id`). The pairing holds after that review is replaced by a re-review, so a
  report never changes once its test is done.
- The main numbers (check agreement, score difference, verdict changes, the checks they disagree on, the
  judgments) compare the test's outcomes with the shown review's *original* outcomes, before any owner
  override. Scores and verdicts on both sides come from `scoring.evaluate` on those original outcomes, each
  under its own review's rubric version, with only that version's enabled checks.
- The override line is separate: every check of the compared calls that the owner overruled on the shown
  review and that the test has an outcome for too, and how often the test matched the owner. The original
  reviewer matches 0 by definition.
- A check present on only one side (a different rubric version) is left out of agreement; its call is
  counted in `rubric_mismatch_calls` and flagged `rubric_mismatch`.
- A call whose test review is a review error is listed with its error and left out of every number.
- Retention erases evidence and keeps outcomes, so the numbers outlive a purge and the evidence reads
  "Erased after the retention period."

Scores are percentages rounded to one decimal place; agreement is a percentage too. A rate or average
over nothing is None, never 0.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any

from work_assistant import rubric, scoring, settings, timeutil
from work_assistant.models import RubricVersion
from work_assistant.names import stored as typed_names
from work_assistant.pipeline import TEST_KIND
from work_assistant.reviewer.catalog import DEFAULT_MODEL
from work_assistant.web.queries import CheckLabel, check_label, model_label, plain_error

PURGED_EVIDENCE = "Erased after the retention period."
JUDGMENTS = ("counted", "resolved_on_call", "call_dropped")


@dataclass
class _Side:
    """One review of a compared call, scored on its original outcomes."""
    review: sqlite3.Row
    version: RubricVersion
    outcomes: dict[str, str]          # original outcome of each enabled check the review has a result for
    evidence: dict[str, str]
    overrides: dict[str, str]         # the owner's override of each such check that has one
    evaluation: scoring.Evaluation

    @property
    def ok(self) -> bool:
        return self.review["status"] == "ok"

    @property
    def counted(self) -> bool:
        return bool(self.review["counted"])

    @property
    def verdict(self) -> str | None:
        if not self.ok:
            return None
        return self.evaluation.verdict if self.counted else "not_counted"

    @property
    def score(self) -> float | None:
        return self.evaluation.percent if self.ok and self.counted else None


@dataclass
class _Pair:
    contact: sqlite3.Row
    shown: _Side
    test: _Side

    @property
    def ticket_id(self) -> int:
        return self.contact["ticket_id"]

    @property
    def purged(self) -> bool:
        return self.contact["purged_at"] is not None

    @property
    def compared(self) -> bool:
        """In the numbers: the test review finished (the shown review always did; the test chose it so)."""
        return self.test.ok

    @property
    def common(self) -> list[str]:
        """The checks both sides have an outcome for, in the shown review's rubric order."""
        return [c for c in self.shown.outcomes if c in self.test.outcomes]

    @property
    def differing(self) -> list[str]:
        return [c for c in self.common if self.shown.outcomes[c] != self.test.outcomes[c]]

    @property
    def rubric_mismatch(self) -> bool:
        """Scored under different rubric versions, or with checks that only one side has."""
        if self.shown.version.version_id != self.test.version.version_id:
            return True
        return self.test.ok and set(self.shown.outcomes) != set(self.test.outcomes)

    @property
    def score_diff(self) -> float | None:
        """Test minus shown, when both have a score."""
        if self.shown.score is None or self.test.score is None:
            return None
        return round(self.test.score - self.shown.score, 1)


class _Context:
    """What every pair needs: rubric versions, check labels, the problem threshold, the time zone, the techs' typed
    names."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.tz = settings.get_timezone(conn)
        self.typed = typed_names(conn)
        self.threshold = int(settings.get_setting(conn, "problem_threshold"))
        self.versions: dict[int, RubricVersion] = {}
        self.labels: dict[str, CheckLabel] = {}
        for version_id, _, _ in sorted(rubric.list_versions(conn)):  # oldest first: the newest label wins
            for check in self.version(version_id).checks:
                self.labels[check.check_id] = check_label(check)

    def version(self, version_id: int) -> RubricVersion:
        if version_id not in self.versions:
            self.versions[version_id] = rubric.get_version(self.conn, version_id)
        return self.versions[version_id]

    def agent(self, contact: sqlite3.Row) -> str:
        """The tech's name for a call: the one typed when they were added (names.py), else the call's."""
        return self.typed.get(contact["agent_email"].strip().lower()) or contact["agent_name"]

    def label(self, check_id: str) -> str:
        return self.labels[check_id].label if check_id in self.labels else check_id

    def order(self, check_id: str) -> tuple:
        return self.labels[check_id].order if check_id in self.labels else (2, 0, check_id)

    def side(self, review_id: int, contact: sqlite3.Row) -> _Side:
        review = self.conn.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        version = self.version(review["rubric_version_id"])
        stored = self.conn.execute("SELECT * FROM check_results WHERE review_id = ? ORDER BY rowid",
                                   (review_id,)).fetchall()
        kept = scoring.enabled_results(version, stored)
        order = {c.check_id: i for i, c in enumerate(version.checks)}
        kept.sort(key=lambda r: order[r["check_id"]])
        original = [{"check_id": r["check_id"], "outcome": r["outcome"], "override_outcome": None} for r in kept]
        evaluation = scoring.evaluate(version, original, bool(contact["attribution_ok"]), review["status"] == "ok",
                                      self.threshold)
        return _Side(
            review=review, version=version,
            outcomes={r["check_id"]: r["outcome"] for r in kept},
            evidence={r["check_id"]: r["evidence"] for r in kept},
            overrides={r["check_id"]: r["override_outcome"] for r in kept if r["override_outcome"] is not None},
            evaluation=evaluation,
        )


def _test_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id = ? AND kind = ?", (run_id, TEST_KIND)).fetchone()


def is_test(conn: sqlite3.Connection, run_id: int) -> bool:
    """Whether run `run_id` exists and is a model test."""
    return _test_run(conn, run_id) is not None


def _pairs(ctx: _Context, run_id: int, ticket_id: int | None = None) -> list[_Pair]:
    """The test's reviewed calls, each with its test review and the shown review it is paired with.

    A call the test has not reviewed (yet) is not in the report.
    """
    rows = ctx.conn.execute(
        "SELECT t.review_id, t.ticket_id, COALESCE(t.compared_review_id, rc.compared_review_id) AS shown_id "
        "FROM reviews t JOIN run_contacts rc ON rc.run_id = t.run_id AND rc.ticket_id = t.ticket_id "
        "WHERE t.run_id = ? AND (? IS NULL OR t.ticket_id = ?) ORDER BY t.review_id",
        (run_id, ticket_id, ticket_id),
    ).fetchall()
    latest = {r["ticket_id"]: r for r in rows if r["shown_id"] is not None}   # one review per call
    pairs = []
    for row in latest.values():
        contact = ctx.conn.execute("SELECT * FROM contacts WHERE ticket_id = ?", (row["ticket_id"],)).fetchone()
        pairs.append(_Pair(contact, ctx.side(row["shown_id"], contact), ctx.side(row["review_id"], contact)))
    return pairs


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _costs(values: list[float | None]) -> tuple[float | None, float | None, int]:
    """(total, per call, not recorded) over the recorded costs."""
    recorded = [v for v in values if v is not None]
    if not recorded:
        return None, None, len(values)
    total = sum(recorded)
    return round(total, 4), round(total / len(recorded), 4), len(values) - len(recorded)


# -- the list of tests --------------------------------------------------------------------------------


def test_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every model test, newest first: `run_id`, `created_at`, `model_label`, `effort`, `window` (its first
    and last local day), `calls` (the calls it chose), `status` and `cost_usd` (what it has cost so far,
    None before anything was paid)."""
    tz = settings.get_timezone(conn)
    runs = conn.execute("SELECT * FROM runs WHERE kind = ? ORDER BY created_at DESC, run_id DESC",
                        (TEST_KIND,)).fetchall()
    return [{
        "run_id": run["run_id"], "created_at": run["created_at"],
        "model_label": model_label(run["model"] or DEFAULT_MODEL), "effort": run["effort"],
        "window": timeutil.window_days(run["window_start"], run["window_end"], tz), "calls": run["contact_count"] or 0, "status": run["status"],
        "cost_usd": run["actual_usd"],
    } for run in runs]


# -- the report -----------------------------------------------------------------------------------------


def report(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """The comparison report of model test `run_id`, or None when there is no such test. Keys:

    - `summary`: `test_model` (label) and `test_effort`; `shown_models` (each shown model and effort with its
      number of calls); `calls` compared and `test_errors` (calls whose test review is an error, listed but
      left out of every number); `check_agreement_pct`; `avg_score_diff` (test minus shown, over calls with a
      score on both sides); `verdict_changes` (`{from, to, count}`, most common first, with `not_counted` as
      a verdict); `cost_total`, `cost_per_call` and `cost_not_recorded`, each `{shown, test}` over the
      compared calls (a review with no recorded cost is left out of its side's total and average);
      `test_run_total_usd`, everything the test run has cost (its `actual_usd`, errored calls included; None
      before anything was paid); `rubric_mismatch_calls`.
    - `overrides`: `{checks, original_matched (always 0), test_matched}` over the compared calls' checks
      the owner overruled on the shown review that both sides have an outcome for.
    - `checks`: `{check_id, label, agreement_pct, differing}` for every check compared, least agreement
      first, then rubric order.
    - `judgments`: `{same, different, not_recorded}` for `counted`, `resolved_on_call` and `call_dropped`.
    - `calls`: one row per call, the largest score difference first (calls without one last, newest
      first): `ticket_id`, `agent`, `date`, `shown_score`, `test_score`, `shown_verdict`, `test_verdict`,
      `differing_checks`, `shown_cost`, `test_cost`, `rubric_mismatch` and `test_error` (None unless the
      test review is an error; then the test's score, verdict and differing checks are None).
    """
    run = _test_run(conn, run_id)
    if run is None:
        return None
    ctx = _Context(conn)
    pairs = _pairs(ctx, run_id)
    compared = [p for p in pairs if p.compared]

    common = sum(len(p.common) for p in compared)
    differing = sum(len(p.differing) for p in compared)
    diffs = [p.score_diff for p in compared if p.score_diff is not None]
    changes = Counter((p.shown.verdict, p.test.verdict) for p in compared if p.shown.verdict != p.test.verdict)
    shown_cost = _costs([p.shown.review["cost_usd"] for p in compared])
    test_cost = _costs([p.test.review["cost_usd"] for p in compared])
    models = Counter((p.shown.review["model"] or DEFAULT_MODEL, p.shown.review["effort"]) for p in compared)

    summary = {
        "test_model": model_label(run["model"] or DEFAULT_MODEL),
        "test_effort": run["effort"],
        "shown_models": [{"model_label": model_label(m), "effort": e, "calls": n}
                         for (m, e), n in sorted(models.items(), key=lambda i: (-i[1], i[0][0], i[0][1] or ""))],
        "calls": len(compared),
        "test_errors": len(pairs) - len(compared),
        "check_agreement_pct": _pct(common - differing, common),
        "avg_score_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
        "verdict_changes": [{"from": f, "to": t, "count": n}
                            for (f, t), n in sorted(changes.items(), key=lambda i: (-i[1], i[0]))],
        "cost_total": {"shown": shown_cost[0], "test": test_cost[0]},
        "cost_per_call": {"shown": shown_cost[1], "test": test_cost[1]},
        "cost_not_recorded": {"shown": shown_cost[2], "test": test_cost[2]},
        "test_run_total_usd": run["actual_usd"],
        "rubric_mismatch_calls": sum(p.rubric_mismatch for p in compared),
    }

    overruled = [(p, check_id) for p in compared for check_id in p.common if check_id in p.shown.overrides]
    overrides = {
        "checks": len(overruled),
        "original_matched": 0,   # an override is the owner disagreeing with the original outcome
        "test_matched": sum(p.test.outcomes.get(c) == p.shown.overrides[c] for p, c in overruled),
    }

    per_check: dict[str, list[int]] = {}
    for pair in compared:
        changed = set(pair.differing)
        for check_id in pair.common:
            counts = per_check.setdefault(check_id, [0, 0])
            counts[0] += 1
            counts[1] += check_id in changed
    checks = sorted(
        ({"check_id": c, "label": ctx.label(c), "agreement_pct": _pct(n - d, n), "differing": d}
         for c, (n, d) in per_check.items()),
        key=lambda row: (row["agreement_pct"], ctx.order(row["check_id"])))

    judgments = {}
    for key in JUDGMENTS:
        counts = Counter()
        for pair in compared:
            shown, test = pair.shown.review[key], pair.test.review[key]
            counts["not_recorded" if shown is None or test is None else "same" if shown == test else "different"] += 1
        judgments[key] = {k: counts[k] for k in ("same", "different", "not_recorded")}

    rows = sorted(pairs, key=lambda p: (p.contact["call_started_at"], p.ticket_id), reverse=True)
    rows.sort(key=lambda p: (p.score_diff is None, -abs(p.score_diff or 0)))
    return {
        "summary": summary,
        "overrides": overrides,
        "checks": checks,
        "judgments": judgments,
        "calls": [_call_row(ctx, p) for p in rows],
    }


def _test_error(pair: _Pair) -> str | None:
    if pair.test.ok:
        return None
    return PURGED_EVIDENCE if pair.purged else plain_error(pair.test.review["error"], pair.test.review["model"])


def _call_row(ctx: _Context, pair: _Pair) -> dict[str, Any]:
    compared = pair.compared
    return {
        "ticket_id": pair.ticket_id,
        "agent": ctx.agent(pair.contact),
        "date": timeutil.local_date(pair.contact["call_started_at"], ctx.tz).isoformat(),
        "shown_score": pair.shown.score,
        "test_score": pair.test.score,
        "shown_verdict": pair.shown.verdict,
        "test_verdict": pair.test.verdict,
        "differing_checks": len(pair.differing) if compared else None,
        "shown_cost": pair.shown.review["cost_usd"],
        "test_cost": pair.test.review["cost_usd"],
        "rubric_mismatch": pair.rubric_mismatch,
        "test_error": _test_error(pair),
    }


# -- one call side by side ------------------------------------------------------------------------------


def _side_json(side: _Side, purged: bool) -> dict[str, Any]:
    review = side.review
    reason = review["not_counted_reason"]
    return {
        "review_id": review["review_id"],
        "model_label": model_label(review["model"] or DEFAULT_MODEL),
        "effort": review["effort"],
        "rubric_version_id": side.version.version_id,
        "score": side.score,
        "verdict": side.verdict,
        "counted": side.counted,
        "not_counted_reason": None if side.counted or reason is None else PURGED_EVIDENCE if purged else reason,
        "resolved_on_call": review["resolved_on_call"],
        "call_dropped": review["call_dropped"],
        "cost": review["cost_usd"],
    }


def call_detail(conn: sqlite3.Connection, run_id: int, ticket_id: int) -> dict[str, Any] | None:
    """One call of model test `run_id` side by side, or None when the test has no review of that call.

    Keys: `ticket_id`, `agent`, `date`, `purged`; `shown` and `test` (each side's review: model and effort,
    rubric version, score, verdict, Not counted and its reason, resolved on the call, dropped call, cost; the
    test's `error` too); `checks`, every check either side has, as `{check_id, label, original, owner, test,
    original_evidence, test_evidence, differs}`. `original` is the shown review's outcome before any
    override and `owner` the owner's override (None without one). `differs` compares `original` with
    `test`, for checks both sides have when the test review finished. Differing checks come first, then
    rubric order. Once the call is purged, evidence reads "Erased after the retention period."
    """
    if _test_run(conn, run_id) is None:
        return None
    ctx = _Context(conn)
    found = _pairs(ctx, run_id, ticket_id)
    if not found:
        return None
    pair = found[0]
    changed = set(pair.differing) if pair.compared else set()

    def evidence(side: _Side, check_id: str) -> str | None:
        if check_id not in side.outcomes:
            return None
        return PURGED_EVIDENCE if pair.purged else side.evidence[check_id]

    check_ids = list(dict.fromkeys([*pair.shown.outcomes, *pair.test.outcomes]))
    check_ids.sort(key=lambda c: (c not in changed, ctx.order(c)))
    return {
        "ticket_id": pair.ticket_id,
        "agent": ctx.agent(pair.contact),
        "date": timeutil.local_date(pair.contact["call_started_at"], ctx.tz).isoformat(),
        "purged": pair.purged,
        "shown": _side_json(pair.shown, pair.purged),
        "test": {**_side_json(pair.test, pair.purged), "error": _test_error(pair)},
        "checks": [{
            "check_id": c,
            "label": ctx.label(c),
            "original": pair.shown.outcomes.get(c),
            "owner": pair.shown.overrides.get(c),
            "test": pair.test.outcomes.get(c),
            "original_evidence": evidence(pair.shown, c),
            "test_evidence": evidence(pair.test, c),
            "differs": c in changed,
        } for c in check_ids],
    }
