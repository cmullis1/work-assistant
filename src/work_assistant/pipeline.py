"""Run orchestration: fetch, select, estimate, budget guard, submit, collect, retry once, and store.

A run's rubric version, reviewing model and effort are frozen when the run starts; every review it
stores references that version and records that model and effort, even if the rubric or the settings
change while the run is paused or its batch is in flight.

The contacts a run covers are recorded in `run_contacts` when they are selected, so a run paused
by the budget guard (`confirm_run`) or interrupted while its batch is in flight (`resume_runs`)
continues with exactly that set. Resuming only collects: it never creates a batch. A paused run can
instead be cancelled by the owner (`cancel_run`): no batch exists yet, and a cancelled run holds nothing.

Every batches.create is bracketed by explicit run states. The run is committed as `submitting`
just before the call, and becomes `submitted` in the same commit that records the batch id. If
the outcome of create is ambiguous (the process died during it, or a connection error, timeout or
5xx came back), the run becomes `submission_unknown`: its tickets stay held and nothing resubmits
them until the owner checks the Anthropic Console and runs `work-assistant clear-run`.

A model test (`start_test`, run kind `test`) scores calls that already have a shown review again,
with the model and effort the owner picks, from their stored records. Its reviews are never current:
each is stored with `is_current = 0` and paired (`compared_review_id`) with the call's shown review
when the test chose it, and storing one never touches any other review. A test holds no tickets, so
no other run waits for it, and it ignores other runs' holds, since it never changes what they store.

Log lines and `runs.error` carry only ticket ids, counts, costs, error class names, and the two
credential messages below. Never customer content.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types.messages.batch_create_params import Request

from work_assistant.costs import estimate_cost, output_tokens_for
from work_assistant.models import Comment, FirstContact, MergeInfo, RubricVersion, TimedFacts
from work_assistant.procutil import pid_alive
from work_assistant.retention import purge_expired
from work_assistant.reviewer.batch import ReviewOutcome, build_request, collect, submit, wait
from work_assistant.reviewer.catalog import DEFAULT_MODEL, FABLE_MODEL, MODELS
from work_assistant.reviewer.prompt import model_check_ids, system_blocks, user_content
from work_assistant.rubric import ensure_seeded, get_version
from work_assistant.runlog import LOG_DIR, LogSink, RunLog, log_path
from work_assistant.settings import EFFORTS, get_setting, get_timezone
from work_assistant.timeutil import date_range_window, local_date
from work_assistant.timing import timed_facts
from work_assistant.zendesk.client import ZendeskAuthError, ZendeskClient, ZendeskError

ANTHROPIC_KEY_REJECTED = (
    "Anthropic API key was rejected (it may have expired or been revoked). "
    "Create a new key in the Anthropic Console, then run: work-assistant setup"
)
ZENDESK_TOKEN_REJECTED = (
    "Zendesk token was rejected (it may have expired or been revoked). "
    "Create a new token in Zendesk Admin Center, then run: work-assistant setup"
)


def zendesk_token_rejected(config_path: Path | None = None) -> str:
    """What to do when Zendesk rejects the token. A copy started from a server config keeps its token where the config
    says, which `work-assistant set-secret` writes; `work-assistant setup` is the PC's."""
    if config_path is None:
        return ZENDESK_TOKEN_REJECTED
    return ("Zendesk token was rejected (it may have expired or been revoked). Create a new token in Zendesk Admin "
            f"Center, then run: work-assistant set-secret zendesk_token --config {config_path}")

SUBMISSION_UNRECORDED = (
    "Submission may have reached Anthropic without being recorded; check the Batches page in the "
    "Anthropic Console before re-running."
)
CLEARED_BY_OWNER = "cleared by owner"
CANCELLED_BY_OWNER = "Cancelled by you before anything was sent."
NOT_CANCELLABLE = "Only a run waiting for your OK can be cancelled."
# Fable 5.1 needs 30-day data retention, and Anthropic rejects its requests as invalid for an organization
# with zero data retention (spec §5.1), so a 400 on a Fable batch most likely means that.
FABLE_BATCH_REJECTED = (
    "Anthropic rejected the batch as an invalid request (BadRequestError). Claude Fable 5.1 needs 30-day data "
    "retention, which your Anthropic organization may not allow."
)

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
# Anthropic rejects a whole request when one image is over 10 MB once base64-encoded (4 bytes per 3).
MAX_IMAGE_BYTES = (10 * 1024 * 1024) // 4 * 3
MAX_ATTEMPTS = 2  # the first try plus one retry for retryable failures
# Runs whose tickets a normal run may not take. A test run never holds tickets, whatever its status.
HOLDING_STATUSES = ("submitting", "submitted", "awaiting_confirmation", "submission_unknown")
# Runs whose tickets not even a re-review may take: they may already be paid for at Anthropic.
UNCERTAIN_STATUSES = ("submitting", "submission_unknown")
TEST_KIND = "test"

Window = tuple[datetime, datetime]
# fetcher(window, log). It gathers each call's later comments over the fixed collection window
# (zendesk.normalize.COLLECTION_WINDOW_HOURS), so the rubric's summary-email deadline doesn't change the fetch.
Fetcher = Callable[[Window, LogSink], list[FirstContact]]
# ids_fetcher(ticket_ids, log): fresh contacts for chosen tickets (Review again), read by id with GETs only;
# a ticket that is missing or no longer qualifies is simply not returned (zendesk.fetch.fetch_tickets).
IdsFetcher = Callable[[Sequence[int], LogSink], list[FirstContact]]
NO_STORED_CALLS = "None of these calls is stored, so there is nothing to review again."
ImagesLoader = Callable[[FirstContact, LogSink], list[tuple[bytes, str]]]
Sleep = Callable[[float], None]


# -- contacts --------------------------------------------------------------------------------------


def contact_snapshot(contact: FirstContact) -> str:
    return json.dumps(dataclasses.asdict(contact), ensure_ascii=False, sort_keys=True)


def _comments_from_snapshot(items: list[dict]) -> tuple[Comment, ...]:
    return tuple(Comment(**{**c, "image_urls": tuple(c["image_urls"])}) for c in items)


def contact_from_snapshot(snapshot: str) -> FirstContact:
    """Rebuild the FirstContact stored by `contact_snapshot`.

    Snapshots stored before callbacks and merges were recorded load with neither.
    """
    data = json.loads(snapshot)
    merged = data.get("merged_into")
    merged_into = (
        MergeInfo(**{**merged, "comments": _comments_from_snapshot(merged["comments"])}) if merged else None
    )
    return FirstContact(**{
        **data,
        "tags": tuple(data["tags"]),
        "comments": _comments_from_snapshot(data["comments"]),
        "callback_minutes": data.get("callback_minutes"),
        "merged_into": merged_into,
    })


def image_urls(contact: FirstContact) -> list[str]:
    """Every image URL of the contact in the order the prompt renders comments (and expects images):
    the ticket's own comments, then the merged-into ticket's."""
    comments = contact.comments + (contact.merged_into.comments if contact.merged_into is not None else ())
    return [url for comment in comments for url in comment.image_urls]


def zendesk_images_loader(client: ZendeskClient) -> ImagesLoader:
    """Production images loader: downloads each image with the read-only Zendesk client.

    One failed download drops that image and logs the ticket id; rejected credentials and
    network failures propagate and fail the run.
    """

    def load(contact: FirstContact, log: LogSink) -> list[tuple[bytes, str]]:
        images: list[tuple[bytes, str]] = []
        for url in image_urls(contact):
            try:
                images.append(client.download(url))
            except ZendeskAuthError:
                raise
            except ZendeskError as exc:
                log.warn(f"ticket {contact.ticket_id}: dropped an image that failed to download ({type(exc).__name__})")
        return images

    return load


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(moment: datetime) -> str:
    """The same format as contacts.call_started_at, so window bounds compare as strings."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_current_review(conn: sqlite3.Connection, ticket_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM reviews WHERE ticket_id = ? AND is_current = 1", (ticket_id,)
    ).fetchone() is not None


def _upsert_contacts(conn: sqlite3.Connection, contacts: Iterable[FirstContact],
                     refresh: Collection[int] = ()) -> None:
    """Store freshly fetched contacts.

    A contact that already has a current review keeps the facts that review was made from (its
    snapshot, agent and attribution); only its fetch time moves. Its facts are rewritten only when
    its ticket id is in `refresh`, which a re-review passes for the tickets it scores again.
    """
    fetched_at = _now()
    store, keep = [], []
    for contact in contacts:
        if contact.ticket_id not in refresh and _has_current_review(conn, contact.ticket_id):
            keep.append(contact)
        else:
            store.append(contact)
    conn.executemany("UPDATE contacts SET fetched_at = ? WHERE ticket_id = ?",
                     [(fetched_at, c.ticket_id) for c in keep])
    conn.executemany(
        "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, priority, "
        "attribution_ok, snapshot, fetched_at) VALUES (?, 'voice', ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticket_id) DO UPDATE SET channel = excluded.channel, "
        "call_started_at = excluded.call_started_at, agent_email = excluded.agent_email, "
        "agent_name = excluded.agent_name, priority = excluded.priority, "
        "attribution_ok = excluded.attribution_ok, snapshot = excluded.snapshot, fetched_at = excluded.fetched_at, "
        "purged_at = NULL",  # content stored again, so the next purge erases it again
        [
            (c.ticket_id, c.call_started_at, c.agent_email, c.agent_name, c.priority, int(c.attribution_ok),
             contact_snapshot(c), fetched_at)
            for c in store
        ],
    )
    conn.commit()


def _already_reviewed(conn: sqlite3.Connection, ticket_id: int, version_id: int) -> bool:
    """Whether a normal run should skip this ticket. Only an explicit re-review scores it again.

    True when it has a current review under any rubric version (a rubric change never re-scores
    past results), or any review error under the run's version (current or kept as history):
    errors are retried only by a re-review. A model test's review errors don't count: a test
    never stands in for a review.
    """
    row = conn.execute(
        "SELECT 1 FROM reviews WHERE ticket_id = ? "
        "AND (is_current = 1 OR (status = 'error' AND rubric_version_id = ? "
        "AND run_id IN (SELECT run_id FROM runs WHERE kind != ?)))",
        (ticket_id, version_id, TEST_KIND),
    ).fetchone()
    return row is not None


def _reviewed_by_later_run(conn: sqlite3.Connection, ticket_id: int, run_id: int) -> bool:
    """The one "superseded" rule: the ticket's current review comes from a later run (any version).

    A result from this run could then only be stored as history, so paying for it is pointless.
    """
    row = conn.execute(
        "SELECT 1 FROM reviews WHERE ticket_id = ? AND is_current = 1 AND run_id > ?", (ticket_id, run_id)
    ).fetchone()
    return row is not None


def _held_tickets(conn: sqlite3.Connection, run_id: int, statuses: Sequence[str]) -> dict[int, int]:
    """Ticket id -> the other run holding it, among runs in one of `statuses`. Test runs hold nothing."""
    rows = conn.execute(
        "SELECT rc.ticket_id, rc.run_id FROM run_contacts rc JOIN runs r ON r.run_id = rc.run_id "
        f"WHERE rc.run_id != ? AND r.kind != ? AND r.status IN ({', '.join('?' for _ in statuses)}) "
        "ORDER BY rc.run_id",
        (run_id, TEST_KIND, *statuses),
    ).fetchall()
    return {r["ticket_id"]: r["run_id"] for r in rows}


def _rereview_candidates(conn: sqlite3.Connection, window: Window, fetched: list[FirstContact],
                         log: LogSink) -> tuple[list[FirstContact], list[FirstContact]]:
    """The contacts a re-review of the window may score, and the fresh ones among them.

    Returns (candidates, fresh). `fresh` is every freshly fetched contact except purged ones: their
    content has been erased and is never stored or reviewed again. `candidates` is `fresh` plus each
    stored contact in the window that Zendesk no longer returns (deleted), from its stored data,
    unless that was purged too.
    """
    purged = {r["ticket_id"] for r in conn.execute("SELECT ticket_id FROM contacts WHERE purged_at IS NOT NULL")}
    fresh = [c for c in fetched if c.ticket_id not in purged]
    fetched_ids = {c.ticket_id for c in fetched}
    gone = []
    for row in conn.execute(
        "SELECT ticket_id, snapshot, purged_at FROM contacts WHERE call_started_at >= ? AND call_started_at < ? "
        "ORDER BY ticket_id",
        (_iso(window[0]), _iso(window[1])),
    ):
        if row["ticket_id"] in fetched_ids:
            continue
        if row["purged_at"] is not None:
            log.info(f"ticket {row['ticket_id']}: no longer returned by Zendesk and its content was purged; skipped")
        else:
            log.info(f"ticket {row['ticket_id']}: no longer returned by Zendesk; using its stored data")
            gone.append(contact_from_snapshot(row["snapshot"]))
    return fresh + gone, fresh


def _purged_in_window(conn: sqlite3.Connection, window: Window) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE call_started_at >= ? AND call_started_at < ? "
        "AND purged_at IS NOT NULL", (_iso(window[0]), _iso(window[1])),
    ).fetchone()[0]


@dataclass(frozen=True)
class _TestPool:
    """The stored calls of a window a model test can score, and why the others can't be."""
    candidates: list[tuple[FirstContact, int]]   # (call, its shown review id), newest call first
    purged: int                                   # content erased after the retention period
    errored: int                                  # the shown review is a review error: nothing to compare
    unreviewed: int                               # no shown review


def _test_pool(conn: sqlite3.Connection, window: Window) -> _TestPool:
    """Every stored call in the window by call start (the window's bounds are local midnights, as for a
    normal run), sorted out from its stored record and shown review alone. Reads nothing from Zendesk."""
    rows = conn.execute(
        "SELECT c.ticket_id, c.snapshot, c.purged_at, r.review_id, r.status FROM contacts c "
        "LEFT JOIN reviews r ON r.ticket_id = c.ticket_id AND r.is_current = 1 "
        "WHERE c.call_started_at >= ? AND c.call_started_at < ? "
        "ORDER BY c.call_started_at DESC, c.ticket_id DESC",
        (_iso(window[0]), _iso(window[1])),
    ).fetchall()
    candidates: list[tuple[FirstContact, int]] = []
    purged = errored = unreviewed = 0
    for row in rows:
        if row["purged_at"] is not None:
            purged += 1
        elif row["review_id"] is None:
            unreviewed += 1
        elif row["status"] != "ok":
            errored += 1
        else:
            candidates.append((contact_from_snapshot(row["snapshot"]), row["review_id"]))
    return _TestPool(candidates, purged, errored, unreviewed)


def _check_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")


def test_candidates(conn: sqlite3.Connection, window: Window, limit: int | None) -> list[tuple[FirstContact, int]]:
    """The calls a model test of the window scores, each rebuilt from its stored snapshot and paired with
    the id of its shown (current) review: calls with a shown review that finished, not purged, newest
    call first, the first `limit` of them (all when None)."""
    _check_limit(limit)
    return _test_pool(conn, window).candidates[:limit]


def _run_contacts(conn: sqlite3.Connection, run_id: int, *, unreviewed: bool) -> list[FirstContact]:
    query = (
        "SELECT c.snapshot FROM run_contacts rc JOIN contacts c ON c.ticket_id = rc.ticket_id "
        "WHERE rc.run_id = ? AND c.purged_at IS NULL"
    )
    if unreviewed:
        query += " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.run_id = rc.run_id AND r.ticket_id = rc.ticket_id)"
    rows = conn.execute(query + " ORDER BY rc.ticket_id", (run_id,)).fetchall()
    return [contact_from_snapshot(r["snapshot"]) for r in rows]


# -- requests and the estimate ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Reviewer:
    """The model and effort a run reviews with. `effort` is None only for a run whose batch was built
    before effort was recorded on runs: its reviews record effort as not recorded."""
    model_id: str
    effort: str | None


@dataclass
class _Item:
    contact: FirstContact
    facts: TimedFacts
    check_ids: list[str]
    request: Request | None = None  # None when the code decides every enabled check
    chars: int = 0
    images: int = 0

    @property
    def custom_id(self) -> str:
        return f"t{self.contact.ticket_id}"


def _usable_images(contact: FirstContact, loaded: list[tuple[bytes, str]], log: LogSink) -> list[tuple[bytes, str]]:
    kept = []
    for data, content_type in loaded:
        media_type = (content_type or "").split(";")[0].strip().lower()
        if media_type not in ALLOWED_IMAGE_TYPES:
            log.warn(f"ticket {contact.ticket_id}: dropped an image with an unsupported type")
        elif len(data) > MAX_IMAGE_BYTES:
            log.warn(f"ticket {contact.ticket_id}: dropped an image over the 10 MB limit")
        else:
            kept.append((data, media_type))
    return kept


def _items(version: RubricVersion, contacts: Iterable[FirstContact]) -> list[_Item]:
    items = []
    for contact in contacts:
        facts = timed_facts(contact, version)
        items.append(_Item(contact=contact, facts=facts, check_ids=model_check_ids(version, facts)))
    return items


def _build_requests(items: list[_Item], system: list[dict], reviewer: _Reviewer,
                    images_loader: ImagesLoader, log: LogSink) -> None:
    """Load images and build the request for every item the model has checks to judge."""
    for item in items:
        if not item.check_ids:
            continue  # never send a schema with an empty enum
        images = _usable_images(item.contact, images_loader(item.contact, log), log)
        content = user_content(item.contact, item.facts, images)
        item.chars = sum(len(block["text"]) for block in content if block["type"] == "text")
        item.images = len(images)
        item.request = build_request(item.custom_id, system, content, item.check_ids, reviewer.effort,
                                     reviewer.model_id)


def _estimate(conn: sqlite3.Connection, system: list[dict], items: list[_Item], reviewer: _Reviewer) -> float:
    """Pre-submission estimate at the model's prices and its output tokens per call learned at the run's
    effort. A retry batch can add up to the cost of the retried tickets on top.

    Items with no checks for the model send no request, so they cost nothing.
    """
    sent = [i for i in items if i.check_ids]
    system_chars = sum(len(block["text"]) for block in system)
    return estimate_cost(system_chars, [i.chars for i in sent], [i.images for i in sent], reviewer.model_id,
                         output_tokens_for(conn, reviewer.model_id, reviewer.effort))


# -- storing reviews --------------------------------------------------------------------------------


def _store_review(conn: sqlite3.Connection, run_id: int, version: RubricVersion, reviewer: _Reviewer,
                  item: _Item, outcome: ReviewOutcome | None, log: LogSink) -> None:
    """Store one review, normally as the ticket's current one. Does not commit.

    `outcome` is None when the code decided every check; such a review still records the run's model
    and effort, but no output tokens. Overrides on the previous current review are not copied; those
    checks get needs_recheck=1 instead.

    The new review is stored as history (not current), leaving the previous current review in place,
    when that review comes from a later run (an older batch never demotes a newer result), or when
    the new review is an error (a failure never replaces a review or loses its recheck flags).

    A test run's review is never current and touches no other review: it is stored paired with the
    shown review recorded when the test chose the call (`run_contacts.compared_review_id`).
    """
    ticket_id = item.contact.ticket_id
    ok = outcome is None or outcome.ok
    taken = conn.execute(
        "SELECT rc.cost_usd, rc.compared_review_id, r.kind FROM run_contacts rc JOIN runs r ON r.run_id = rc.run_id "
        "WHERE rc.run_id = ? AND rc.ticket_id = ?", (run_id, ticket_id)
    ).fetchone()
    test = taken["kind"] == TEST_KIND
    previous = None if test else conn.execute(
        "SELECT review_id, run_id FROM reviews WHERE ticket_id = ? AND is_current = 1", (ticket_id,)
    ).fetchone()
    current = not test
    recheck: set[str] = set()
    if previous is not None and _reviewed_by_later_run(conn, ticket_id, run_id):
        current = False
        log.info(f"ticket {ticket_id}: run {previous['run_id']} already reviewed it; this result kept as history")
    elif previous is not None and not ok:
        current = False
        log.warn(f"ticket {ticket_id}: review error kept as history; the previous review stays current")
    elif previous is not None:
        recheck = {
            r["check_id"]
            for r in conn.execute(
                "SELECT check_id FROM check_results WHERE review_id = ? AND override_outcome IS NOT NULL",
                (previous["review_id"],),
            )
        }
        # Before the insert: the unique partial index allows one current review per ticket.
        conn.execute("UPDATE reviews SET is_current = 0 WHERE review_id = ?", (previous["review_id"],))

    # Only a successful model review can mark a call Not counted; code-only reviews and errors count.
    judged = outcome is not None and outcome.ok
    cur = conn.execute(
        "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, error, resolved_on_call, "
        "counted, not_counted_reason, is_current, cost_usd, created_at, model, effort, output_tokens, "
        "compared_review_id, call_dropped) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ticket_id, version.version_id, "ok" if ok else "error",
         None if ok else outcome.error, outcome.resolved_on_call if outcome is not None else None,
         int(outcome.counted) if judged else 1, outcome.not_counted_reason if judged else None,
         int(current), taken["cost_usd"], _now(), reviewer.model_id, reviewer.effort,
         outcome.output_tokens if outcome is not None else None, taken["compared_review_id"],
         outcome.call_dropped if judged else None),
    )
    review_id = cur.lastrowid

    model = outcome.outcomes if outcome is not None and outcome.ok else {}
    dropped = judged and outcome.call_dropped == "yes"
    rows = []
    for check in version.checks:
        check_id = check.check_id
        if dropped and check.timed == "call_length" and check_id in item.facts.decided:
            # A dropped call is exempt from the 10-minute rule; only its callback is checked.
            rows.append((check_id, "na", DROPPED_CALL_EVIDENCE, "code"))
        elif check_id in item.facts.decided:
            rows.append((check_id, item.facts.decided[check_id], item.facts.evidence.get(check_id, ""), "code"))
        elif check_id in model:
            rows.append((check_id, model[check_id][0], model[check_id][1], "model"))
    conn.executemany(
        "INSERT INTO check_results (review_id, check_id, outcome, evidence, decided_by, needs_recheck) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(review_id, cid, out, ev, by, int(cid in recheck)) for cid, out, ev, by in rows],
    )


def _add_cost(conn: sqlite3.Connection, run_id: int, ticket_id: int, batch_id: str, cost: float) -> None:
    """Count one batch's cost for one ticket, at most once per batch."""
    conn.execute(
        "UPDATE run_contacts SET cost_usd = cost_usd + ?, cost_batch_id = ? "
        "WHERE run_id = ? AND ticket_id = ? AND (cost_batch_id IS NULL OR cost_batch_id != ?)",
        (cost, batch_id, run_id, ticket_id, batch_id),
    )


def _update_actual(conn: sqlite3.Connection, run_id: int) -> float:
    total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM run_contacts WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    conn.execute("UPDATE runs SET actual_usd = ? WHERE run_id = ?", (total, run_id))
    return total


# -- running a batch --------------------------------------------------------------------------------


def _await_batch(conn: sqlite3.Connection, run_id: int, version: RubricVersion, reviewer: _Reviewer, client: Any,
                 batch_id: str, items: list[_Item], *, final: bool, log: LogSink, sleep: Sleep) -> list[_Item]:
    """Wait for a batch, store its results in one transaction, and return the items to retry."""
    wait(client, batch_id, sleep=sleep)
    results = collect(client, batch_id, {i.custom_id: i.check_ids for i in items}, reviewer.model_id)
    retry = []
    for item in items:
        outcome = results[item.custom_id]
        ticket_id = item.contact.ticket_id
        _add_cost(conn, run_id, ticket_id, batch_id, outcome.cost_usd)
        if not outcome.ok and outcome.retryable and not final:
            log.warn(f"ticket {ticket_id}: review failed; it will be retried once")
            retry.append(item)
            continue
        if not outcome.ok:
            log.warn(f"ticket {ticket_id}: review error stored")
        _store_review(conn, run_id, version, reviewer, item, outcome, log)
    _update_actual(conn, run_id)
    conn.commit()
    log.info(f"collected batch: {len(items) - len(retry)} reviews stored, {len(retry)} to retry")
    return retry


def _execute(conn: sqlite3.Connection, run_id: int, version: RubricVersion, reviewer: _Reviewer, items: list[_Item],
             client: Any, log: LogSink, sleep: Sleep, *, from_status: str, log_dir: Path) -> None:
    """Store code-only reviews, then submit, collect and retry. `from_status` is the run's status now
    (fetching or awaiting_confirmation); the first submission only proceeds from that status."""
    for item in items:
        if item.request is None:
            _store_review(conn, run_id, version, reviewer, item, None, log)
            log.info(f"ticket {item.contact.ticket_id}: every check decided by code; no request sent")
    conn.commit()

    pending = [i for i in items if i.request is not None]
    attempt = 1
    while pending:
        expected = (from_status,) if attempt == 1 else ("submitted",)
        batch_id = _submit_batch(conn, run_id, client, [i.request for i in pending], log, expected)
        log.info(f"submitted batch with {len(pending)} requests (attempt {attempt})")
        pending = _await_batch(conn, run_id, version, reviewer, client, batch_id, pending,
                               final=attempt >= MAX_ATTEMPTS, log=log, sleep=sleep)
        attempt += 1
    _finish(conn, run_id, log, log_dir)


class SubmissionUnknown(Exception):
    """batches.create may or may not have created a batch; the run is now `submission_unknown`."""


class SubmissionTransitionLost(Exception):
    """The run was no longer in the state this submission started from (another process moved it),
    so create was not called and the run is left to that process."""


def _failure_message(exc: Exception, model: str | None = None) -> str:
    """The safe runs.error text for an exception from a run reviewing with `model`: a credential message,
    the Fable retention hint for a rejected Fable batch, or the class name only."""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ANTHROPIC_KEY_REJECTED
    if isinstance(exc, ZendeskAuthError):
        return ZENDESK_TOKEN_REJECTED
    if isinstance(exc, anthropic.BadRequestError) and model == FABLE_MODEL:
        return FABLE_BATCH_REJECTED
    return type(exc).__name__  # messages can carry URLs or customer content


def _run_model(conn: sqlite3.Connection, run_id: int) -> str | None:
    row = conn.execute("SELECT model FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return row["model"] if row is not None else None


def _definitely_not_created(exc: Exception) -> bool:
    """Only a 4xx response (bad key, permission, bad request, rate limit) proves no batch was created.

    Anything else from create (connection error, timeout, 5xx, or any other exception) leaves it open.
    """
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code < 500


def _submit_batch(conn: sqlite3.Connection, run_id: int, client: Any, requests: list[Request],
                  log: LogSink, expected: Sequence[str]) -> str:
    """Create one batch between explicit `submitting` and `submitted` states; return its id.

    The move to `submitting` only happens from one of the `expected` statuses; if another process
    got there first, create is not called. The same update records this process as the run's driver.
    """
    cur = conn.execute(
        f"UPDATE runs SET status = 'submitting', batch_id = NULL, pid = ? WHERE run_id = ? "
        f"AND status IN ({', '.join('?' for _ in expected)})",
        (os.getpid(), run_id, *expected),
    )
    conn.commit()
    if cur.rowcount == 0:
        log.warn(f"run {run_id}: not submitted; its status changed in another process first")
        raise SubmissionTransitionLost()
    try:
        batch_id = submit(client, requests)
    except Exception as exc:
        if _definitely_not_created(exc):
            conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE run_id = ? "
                "AND status = 'submitting'",
                (_failure_message(exc, _run_model(conn, run_id)), _now(), run_id),
            )
            conn.commit()
            raise
        _mark_unknown(conn, run_id, log, type(exc).__name__)
        raise SubmissionUnknown() from None
    _record_submitted(conn, run_id, batch_id, log)
    return batch_id


def _record_submitted(conn: sqlite3.Connection, run_id: int, batch_id: str, log: LogSink) -> None:
    """Record an accepted batch: `submitting` becomes `submitted` with its id, in one commit."""
    cur = conn.execute(
        "UPDATE runs SET status = 'submitted', batch_id = ?, error = NULL WHERE run_id = ? AND status = 'submitting'",
        (batch_id, run_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        # Another process judged this submission lost and marked it unknown meanwhile. That status is
        # the owner's to clear; keep the batch id for them and stop here.
        conn.execute("UPDATE runs SET batch_id = ? WHERE run_id = ?", (batch_id, run_id))
        conn.commit()
        log.warn(f"run {run_id}: batch {batch_id} was accepted after the run was marked submission_unknown")
        raise SubmissionUnknown()


def _mark_unknown(conn: sqlite3.Connection, run_id: int, log: LogSink, cause: str) -> None:
    conn.execute(
        "UPDATE runs SET status = 'submission_unknown', error = ? WHERE run_id = ? AND status = 'submitting'",
        (SUBMISSION_UNRECORDED, run_id),
    )
    conn.commit()
    kind = conn.execute("SELECT kind FROM runs WHERE run_id = ?", (run_id,)).fetchone()["kind"]
    holding = "a model test holds no calls" if kind == TEST_KIND else "its tickets stay held"
    log.warn(f"run {run_id}: submission outcome unknown ({cause}); {holding}. "
             f"Check the Anthropic Console, then run: work-assistant clear-run {run_id}")


def driven_elsewhere(pid: int | None) -> bool:
    """Whether another live process is driving a run recorded with this process id.

    Such a run is that process's to finish: it may be creating the batch or polling it right now.
    """
    return pid is not None and pid != os.getpid() and pid_alive(pid)


def _settle_interrupted_submissions(conn: sqlite3.Connection, log_dir: Path,
                                    secrets: Sequence[str | None]) -> list[int]:
    """Move every run left in `submitting` by a process that died during create to `submission_unknown`.

    A run whose driving process is still alive is left alone: its create may be in progress.
    """
    run_ids = [r["run_id"] for r in conn.execute("SELECT run_id, pid FROM runs WHERE status = 'submitting'")
               if not driven_elsewhere(r["pid"])]
    for run_id in run_ids:
        _mark_unknown(conn, run_id, RunLog(log_path(log_dir, run_id), secrets), "interrupted during create")
    return run_ids


def clear_run(conn: sqlite3.Connection, run_id: int, *, log_dir: Path = LOG_DIR,
              secrets: Sequence[str | None] = ()) -> None:
    """The owner's release of a `submission_unknown` run, after checking the Anthropic Console.

    The run becomes `failed` ("cleared by owner") and its tickets are free for later runs.
    """
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"Run {run_id} does not exist")
    if row["status"] != "submission_unknown":
        raise ValueError(f"Run {run_id} is not in submission_unknown (status: {row['status']})")
    conn.execute(
        "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE run_id = ? "
        "AND status = 'submission_unknown'",
        (CLEARED_BY_OWNER, _now(), run_id),
    )
    conn.commit()
    RunLog(log_path(log_dir, run_id), secrets).info(f"run {run_id} {CLEARED_BY_OWNER}; its tickets are released")


def cancel_run(conn: sqlite3.Connection, run_id: int, *, log_dir: Path = LOG_DIR,
               secrets: Sequence[str | None] = ()) -> None:
    """The owner's cancel of a run (or model test) the budget guard paused, instead of confirming it.

    Only `awaiting_confirmation` can be cancelled: no batch exists yet, so nothing was sent or charged.
    The run becomes `cancelled`, which holds no tickets, so later runs can take its calls. Any other
    status raises ValueError, and so does a confirm that already moved the run on.
    """
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"Run {run_id} does not exist")
    cur = conn.execute(
        "UPDATE runs SET status = 'cancelled', error = ?, finished_at = ? WHERE run_id = ? "
        "AND status = 'awaiting_confirmation'",
        (CANCELLED_BY_OWNER, _now(), run_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"{NOT_CANCELLABLE} Run {run_id} is {row['status']}.")
    RunLog(log_path(log_dir, run_id), secrets).info(f"run {run_id} cancelled: {CANCELLED_BY_OWNER}")


def _finish(conn: sqlite3.Connection, run_id: int, log: LogSink, log_dir: Path) -> None:
    total = _update_actual(conn, run_id)
    conn.execute(
        "UPDATE runs SET status = 'complete', error = NULL, finished_at = ? WHERE run_id = ?", (_now(), run_id)
    )
    conn.commit()
    reviews = conn.execute("SELECT COUNT(*) FROM reviews WHERE run_id = ?", (run_id,)).fetchone()[0]
    log.info(f"run {run_id} complete: {reviews} reviews, actual ${total:.4f}")
    _cancel_superseded(conn, run_id, log)
    _purge_expired(conn, log, log_dir)


def _purge_expired(conn: sqlite3.Connection, log: LogSink, log_dir: Path) -> None:
    """Erase customer content past the retention period at the end of a completed run.

    The run is already complete, so a purge failure is logged and left for the next run or
    `work-assistant purge`; it never fails the run.
    """
    try:
        days = int(get_setting(conn, "retention_days"))
        purged = purge_expired(conn, datetime.now(timezone.utc), days, log_dir)
    except Exception as exc:
        conn.rollback()
        log.warn(f"purge failed: {type(exc).__name__}")
        return
    if purged:
        log.info(f"purged customer content of {purged} calls older than {days} days")


def _cancel_superseded(conn: sqlite3.Connection, run_id: int, log: LogSink) -> None:
    """Cancel every paused run whose tickets all now have a current review from a later run (any
    rubric version). Confirming it would only pay for results that would be kept as history.
    Only `awaiting_confirmation` runs are ever touched, and never a model test: its results are
    never shown, so a newer shown review takes nothing from it."""
    paused = conn.execute(
        "SELECT run_id FROM runs WHERE status = 'awaiting_confirmation' AND run_id != ? AND kind != ?",
        (run_id, TEST_KIND),
    ).fetchall()
    for run in paused:
        tickets = [r["ticket_id"] for r in conn.execute(
            "SELECT ticket_id FROM run_contacts WHERE run_id = ?", (run["run_id"],))]
        if tickets and all(_reviewed_by_later_run(conn, t, run["run_id"]) for t in tickets):
            conn.execute(
                "UPDATE runs SET status = 'cancelled', error = ?, finished_at = ? WHERE run_id = ? "
                "AND status = 'awaiting_confirmation'",
                (f"superseded by run {run_id}", _now(), run["run_id"]),
            )
            log.info(f"run {run['run_id']} cancelled: superseded by run {run_id}")
    conn.commit()


def _guarded(conn: sqlite3.Connection, run_id: int, log: LogSink, body: Callable[[], None]) -> None:
    """Run `body`; on an exception record a safe error message instead of raising.

    A run whose batch is already submitted (and recorded) stays `submitted`, so `resume_runs` can
    still collect the results that are being paid for. A `submission_unknown` run is left as it is.
    Any other run becomes `failed`.
    """
    try:
        body()
    except (SubmissionUnknown, SubmissionTransitionLost):
        pass  # already recorded, or the run belongs to another process
    except Exception as exc:
        _record_failure(conn, run_id, log, exc)


def _record_failure(conn: sqlite3.Connection, run_id: int, log: LogSink, exc: Exception) -> None:
    conn.rollback()
    message = _failure_message(exc, _run_model(conn, run_id))
    run = conn.execute("SELECT status, batch_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run["status"] == "submission_unknown":
        log.warn(f"run {run_id}: {message}; the run stays submission_unknown until cleared by the owner")
    elif run["status"] == "submitting":
        # Something failed between `submitting` and recording the batch id (for example the database
        # write after an accepted create): the batch may exist, so the tickets must stay held.
        _mark_unknown(conn, run_id, log, message)
        return
    elif run["status"] == "submitted" and run["batch_id"] is not None:
        conn.execute("UPDATE runs SET error = ? WHERE run_id = ? AND status = 'submitted'", (message, run_id))
        log.warn(f"run {run_id} interrupted while its batch is in flight: {message}. "
                 "Collect it with: work-assistant resume")
    else:
        conn.execute(
            "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE run_id = ? "
            "AND status != 'submission_unknown'",
            (message, _now(), run_id),
        )
        log.warn(f"run {run_id} failed: {message}")
    conn.commit()


# -- public entry points ----------------------------------------------------------------------------


def _select(conn: sqlite3.Connection, run_id: int, version: RubricVersion, contacts: list[FirstContact], *,
            select_all: bool, log: LogSink) -> list[FirstContact]:
    """The contacts a run takes, by ticket id: not yet reviewed (unless `select_all`), and not held.

    A ticket held by another run is that run's to finish; taking it here could pay for it twice.
    A re-review may override in-flight and paused runs (the newer result wins), but never a run
    whose submission may already be paid for at Anthropic.
    """
    selected = sorted(
        (c for c in contacts if select_all or not _already_reviewed(conn, c.ticket_id, version.version_id)),
        key=lambda c: c.ticket_id,
    )
    return _exclude_held(conn, run_id, selected, UNCERTAIN_STATUSES if select_all else HOLDING_STATUSES, log)


def _exclude_held(conn: sqlite3.Connection, run_id: int, selected: list[FirstContact], statuses: Sequence[str],
                  log: LogSink) -> list[FirstContact]:
    held = _held_tickets(conn, run_id, statuses)
    excluded = [c for c in selected if c.ticket_id in held]
    if excluded:
        holders = ", ".join(str(r) for r in sorted({held[c.ticket_id] for c in excluded}))
        log.info(f"{len(excluded)} tickets excluded: held by run(s) {holders} ({' or '.join(statuses)})")
    return [c for c in selected if c.ticket_id not in held]


@dataclass
class _Selection:
    """What a run's `choose` step found and selected, in the order the run's `limit` cuts it."""
    found: int                               # contacts considered
    contacts: list[FirstContact]
    excluded_purged: int = 0                 # purged calls left out (re-reviews and tests count them)
    compared: dict[int, int] = field(default_factory=dict)  # test runs: ticket id -> its shown review id


def _start(conn: sqlite3.Connection, kind: str, window: Window, *, reviewer: _Reviewer,
           choose: Callable[[int, RubricVersion, LogSink], _Selection], anthropic_client: Any,
           images_loader: ImagesLoader, confirm: bool, limit: int | None, log_dir: Path,
           secrets: Sequence[str | None], sleep: Sleep,
           store_selected: Callable[[list[FirstContact]], None] = lambda selected: None) -> int:
    """Create a run reviewing with `reviewer`, take the contacts `choose(run_id, version, log)` selects
    (the first `limit` of them), and review them.

    `store_selected` stores contact data once the run's selection is known, before it is recorded.
    """
    _check_limit(limit)
    _settle_interrupted_submissions(conn, log_dir, secrets)
    version = ensure_seeded(conn)  # frozen for the whole run, like the model and effort
    cur = conn.execute(
        "INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at, pid, model, "
        "effort) VALUES (?, ?, ?, ?, 'fetching', ?, ?, ?, ?)",
        (kind, _iso(window[0]), _iso(window[1]), version.version_id, _now(), os.getpid(), reviewer.model_id,
         reviewer.effort),
    )
    conn.commit()
    run_id = cur.lastrowid
    log = RunLog(log_path(log_dir, run_id), secrets)
    log.info(f"run {run_id} started: kind {kind}, rubric version {version.version_id}, "
             f"model {reviewer.model_id}, effort {reviewer.effort}")

    def body() -> None:
        chosen = choose(run_id, version, log)
        selected = chosen.contacts[:limit]
        store_selected(selected)
        conn.executemany(
            "INSERT INTO run_contacts (run_id, ticket_id, compared_review_id) VALUES (?, ?, ?)",
            [(run_id, c.ticket_id, chosen.compared.get(c.ticket_id)) for c in selected],
        )
        conn.execute("UPDATE runs SET contact_count = ?, excluded_purged = ? WHERE run_id = ?",
                     (len(selected), chosen.excluded_purged, run_id))
        conn.commit()
        log.info(f"{chosen.found} contacts found, {len(selected)} selected for review")
        if chosen.excluded_purged:
            log.info(f"{chosen.excluded_purged} purged calls excluded: their content has been erased")
        if selected:
            log.info("selected tickets: " + ", ".join(str(c.ticket_id) for c in selected))

        system = system_blocks(version)
        items = _items(version, selected)
        _build_requests(items, system, reviewer, images_loader, log)
        estimate = _estimate(conn, system, items, reviewer)
        conn.execute("UPDATE runs SET estimate_usd = ? WHERE run_id = ?", (estimate, run_id))
        conn.commit()
        requests = sum(1 for i in items if i.request is not None)
        log.info(f"estimate ${estimate:.2f} for {requests} requests")

        budget = float(get_setting(conn, "budget_limit_usd"))
        if estimate > budget and not confirm:
            conn.execute("UPDATE runs SET status = 'awaiting_confirmation' WHERE run_id = ?", (run_id,))
            conn.commit()
            log.info(f"estimate is over the ${budget:.2f} limit; waiting for confirmation")
            return
        _execute(conn, run_id, version, reviewer, items, anthropic_client, log, sleep, from_status="fetching",
                 log_dir=log_dir)

    _guarded(conn, run_id, log, body)
    return run_id


def start_run(conn: sqlite3.Connection, kind: str, window: Window, *, fetcher: Fetcher, anthropic_client: Any,
              images_loader: ImagesLoader, confirm: bool = False, limit: int | None = None,
              log_dir: Path = LOG_DIR, secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> int:
    """Fetch the window and review every contact not yet reviewed.

    Skips tickets with a current review under any rubric version (only a re-review scores a ticket
    again, so a rubric change never re-scores past weeks), tickets with a review error under the
    current version, and tickets held by another run (submitting, submitted, awaiting confirmation,
    or submission unknown). Contacts with a current review keep their stored facts. `limit`
    reviews only the first N of the rest (by ticket id). Returns the run id; the outcome is in the
    run's status: complete, awaiting_confirmation, failed, submitted (interrupted, resumable), or
    submission_unknown (the owner must check the Anthropic Console and run clear-run).
    """

    def choose(run_id: int, version: RubricVersion, log: LogSink) -> _Selection:
        contacts = fetcher(window, log)
        _upsert_contacts(conn, contacts)
        return _Selection(len(contacts), _select(conn, run_id, version, contacts, select_all=False, log=log))

    return _start(conn, kind, window, reviewer=_current_reviewer(conn), choose=choose,
                  anthropic_client=anthropic_client, images_loader=images_loader, confirm=confirm, limit=limit,
                  log_dir=log_dir, secrets=secrets, sleep=sleep)


def _current_reviewer(conn: sqlite3.Connection) -> _Reviewer:
    """The Model and Effort settings, which a normal run or re-review keeps from its start."""
    return _Reviewer(get_setting(conn, "model"), get_setting(conn, "effort"))


def rereview(conn: sqlite3.Connection, window: Window, *, fetcher: Fetcher, anthropic_client: Any,
             images_loader: ImagesLoader, confirm: bool = False, limit: int | None = None,
             log_dir: Path = LOG_DIR, secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> int:
    """Review every contact in the window again under the current rubric version, from fresh data.

    The window is fetched from Zendesk like a normal run, since a rubric change can need facts that
    older snapshots lack; the stored data of every ticket re-reviewed is refreshed. A stored ticket
    Zendesk no longer returns is re-reviewed from its stored data. Purged contacts are excluded
    (and counted), and their content is not stored again. Tickets held by a run whose submission
    outcome is unknown are excluded. The new reviews become current; the old ones (and their
    overrides) are kept, and every overridden check is flagged needs_recheck on the new review.
    Goes through the budget guard.
    """
    fresh: list[FirstContact] = []

    def choose(run_id: int, version: RubricVersion, log: LogSink) -> _Selection:
        fetched = fetcher(window, log)
        candidates, fresh[:] = _rereview_candidates(conn, window, fetched, log)
        selected = _select(conn, run_id, version, candidates, select_all=True, log=log)
        return _Selection(len(candidates), selected, excluded_purged=_purged_in_window(conn, window))

    def store_selected(selected: list[FirstContact]) -> None:
        _upsert_contacts(conn, fresh, refresh={c.ticket_id for c in selected})

    return _start(conn, "rereview", window, reviewer=_current_reviewer(conn), choose=choose,
                  anthropic_client=anthropic_client, images_loader=images_loader, confirm=confirm, limit=limit,
                  log_dir=log_dir, secrets=secrets, sleep=sleep, store_selected=store_selected)


@dataclass
class _TicketChoice:
    """What Review again of chosen tickets takes: `selection` (fresh contacts, or stored data for a call
    Zendesk no longer returns), the `fresh` ones to store, and how many were left out as held."""
    selection: _Selection
    fresh: list[FirstContact]
    held: int


def _stored_tickets(conn: sqlite3.Connection, ticket_ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    """The chosen tickets that are stored, by id, with their call start and purge time. Raises ValueError
    when none is."""
    wanted = {int(t) for t in ticket_ids}
    stored = {r["ticket_id"]: r for r in conn.execute("SELECT ticket_id, call_started_at, purged_at FROM contacts")
              if r["ticket_id"] in wanted}
    if not stored:
        raise ValueError(NO_STORED_CALLS)
    return stored


def _ticket_choice(conn: sqlite3.Connection, run_id: int, stored: dict[int, sqlite3.Row],
                   fetcher_for_ids: IdsFetcher, log: LogSink) -> _TicketChoice:
    """Sort the chosen stored tickets out, then read the rest fresh from Zendesk.

    Purged calls are left out and counted: their content was erased and is never stored or reviewed again.
    Calls held by another run in progress (`HOLDING_STATUSES`: submitting, submitted, waiting for the OK,
    or submission unknown) are left out and counted, so no call is paid for twice. Neither is read from
    Zendesk. A call Zendesk no longer returns is reviewed from its stored data, as a range re-review does.
    """
    purged = sorted(t for t, row in stored.items() if row["purged_at"] is not None)
    live = sorted(t for t, row in stored.items() if row["purged_at"] is None)
    held_by = _held_tickets(conn, run_id, HOLDING_STATUSES)
    held = [t for t in live if t in held_by]
    if held:
        holders = ", ".join(str(r) for r in sorted({held_by[t] for t in held}))
        log.info(f"{len(held)} tickets excluded: held by run(s) {holders} ({' or '.join(HOLDING_STATUSES)})")
    wanted = [t for t in live if t not in held_by]
    fresh = [c for c in fetcher_for_ids(wanted, log) if c.ticket_id in set(wanted)] if wanted else []
    returned = {c.ticket_id for c in fresh}
    gone = []
    for ticket_id in wanted:
        if ticket_id not in returned:
            log.info(f"ticket {ticket_id}: no longer returned by Zendesk; using its stored data")
            snapshot = conn.execute("SELECT snapshot FROM contacts WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]
            gone.append(contact_from_snapshot(snapshot))
    contacts = sorted(fresh + gone, key=lambda c: c.ticket_id)
    return _TicketChoice(_Selection(len(stored), contacts, excluded_purged=len(purged)), fresh, len(held))


def _tickets_window(conn: sqlite3.Connection, stored: dict[int, sqlite3.Row]) -> Window:
    """The local days the chosen calls fall on, from the first one's midnight to the midnight after the last
    one's: the run's window for Run history. Purged calls are left out unless every chosen call is purged."""
    tz = get_timezone(conn)
    rows = [r for r in stored.values() if r["purged_at"] is None] or list(stored.values())
    days = [local_date(r["call_started_at"], tz) for r in rows]
    return date_range_window(min(days), max(days), tz)


def rereview_tickets(conn: sqlite3.Connection, ticket_ids: Iterable[int], *, fetcher_for_ids: IdsFetcher,
                     anthropic_client: Any, images_loader: ImagesLoader, confirm: bool = False,
                     log_dir: Path = LOG_DIR, secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> int:
    """Review again the chosen calls (the call detail's Review again, or the Tickets list's Review these N
    again) under the current rubric version, model and effort, from fresh data.

    Like `rereview`, but for these tickets instead of a window: only they are read from Zendesk (by id, GETs
    only), and the stored data of every ticket reviewed again is refreshed. Purged calls and calls held by a
    run in progress are left out and counted (`_ticket_choice`). The run's kind is `rereview` and its window
    spans the chosen calls' local days. The new reviews become current; the old ones (and their overrides)
    are kept, and every overridden check is flagged needs_recheck on the new review. Goes through the budget
    guard. Raises ValueError, before any run is created, when none of the tickets is stored.
    """
    stored = _stored_tickets(conn, ticket_ids)
    window = _tickets_window(conn, stored)
    fresh: list[FirstContact] = []

    def choose(run_id: int, version: RubricVersion, log: LogSink) -> _Selection:
        choice = _ticket_choice(conn, run_id, stored, fetcher_for_ids, log)
        fresh[:] = choice.fresh
        return choice.selection

    def store_selected(selected: list[FirstContact]) -> None:
        _upsert_contacts(conn, fresh, refresh={c.ticket_id for c in selected})

    return _start(conn, "rereview", window, reviewer=_current_reviewer(conn), choose=choose,
                  anthropic_client=anthropic_client, images_loader=images_loader, confirm=confirm, limit=None,
                  log_dir=log_dir, secrets=secrets, sleep=sleep, store_selected=store_selected)


def _test_reviewer(model_id: str, effort: str) -> _Reviewer:
    """The model and effort a model test reviews with, checked against the catalog and the effort levels."""
    if model_id not in MODELS:
        raise ValueError(f"Unknown model {model_id!r}; choose one of: {', '.join(MODELS)}")
    if effort not in EFFORTS:
        raise ValueError(f"Unknown effort {effort!r}; choose one of: {', '.join(EFFORTS)}")
    return _Reviewer(model_id, effort)


def start_test(conn: sqlite3.Connection, window: Window, *, model_id: str, effort: str, limit: int | None = None,
               anthropic_client: Any, images_loader: ImagesLoader, confirm: bool = False,
               log_dir: Path = LOG_DIR, secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> int:
    """A model test: score the window's calls that have a shown review again with `model_id` at `effort`.

    The calls are `test_candidates` (newest first, the first `limit`), scored under the current rubric
    version from their stored records: text, timed facts and merge status are what the shown review saw,
    and nothing is fetched from Zendesk but the images (`images_loader`, read-only GETs). Purged calls
    are counted in `excluded_purged`. The run (kind `test`) goes through the same budget guard and
    statuses as `start_run`, and `confirm_run` and `resume_runs` finish it on its own model and effort.
    Each review is stored with `is_current = 0`, paired with the shown review the test chose it
    with; no shown review, override or stored call changes. Returns the run id.
    """
    reviewer = _test_reviewer(model_id, effort)

    def choose(run_id: int, version: RubricVersion, log: LogSink) -> _Selection:
        pool = _test_pool(conn, window)
        if pool.unreviewed or pool.errored:
            log.info(f"{pool.unreviewed} calls without a shown review and {pool.errored} whose shown review is "
                     "a review error skipped: nothing to compare with")
        return _Selection(len(pool.candidates), [c for c, _ in pool.candidates], excluded_purged=pool.purged,
                          compared={c.ticket_id: review_id for c, review_id in pool.candidates})

    return _start(conn, TEST_KIND, window, reviewer=reviewer, choose=choose, anthropic_client=anthropic_client,
                  images_loader=images_loader, confirm=confirm, limit=limit, log_dir=log_dir, secrets=secrets,
                  sleep=sleep)


def confirm_run(conn: sqlite3.Connection, run_id: int, *, anthropic_client: Any, images_loader: ImagesLoader,
                log_dir: Path = LOG_DIR, secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> None:
    """Go ahead with a run the budget guard paused, for the contacts it selected.

    Skipped: tickets whose current review now comes from a later run (any rubric version), and
    tickets another run holds with a batch in flight or possibly in flight (submitting, submitted,
    submission unknown). If two confirms race, only the one that moves the run to `submitting`
    creates a batch. A model test skips nothing: it never changes what another run stores, and its
    reviews stay paired with the shown reviews it chose, even ones replaced since.
    """
    _settle_interrupted_submissions(conn, log_dir, secrets)
    row = conn.execute("SELECT status, kind, rubric_version_id, model, effort FROM runs WHERE run_id = ?",
                       (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"Run {run_id} does not exist")
    if row["status"] != "awaiting_confirmation":
        raise ValueError(f"Run {run_id} is not awaiting confirmation (status: {row['status']})")
    version = get_version(conn, row["rubric_version_id"])
    # The run's own model and effort, never the current settings. A run paused before they were
    # recorded goes ahead as it always did, on Opus at the current effort, which is then recorded.
    reviewer = _Reviewer(row["model"] or DEFAULT_MODEL, row["effort"] or get_setting(conn, "effort"))
    conn.execute("UPDATE runs SET model = ?, effort = ? WHERE run_id = ?",
                 (reviewer.model_id, reviewer.effort, run_id))
    conn.commit()
    log = RunLog(log_path(log_dir, run_id), secrets)
    log.info(f"run {run_id} confirmed: model {reviewer.model_id}, effort {reviewer.effort}")

    def body() -> None:
        kept = contacts = _run_contacts(conn, run_id, unreviewed=True)
        if row["kind"] != TEST_KIND:
            kept = [c for c in contacts if not _reviewed_by_later_run(conn, c.ticket_id, run_id)]
            if len(kept) != len(contacts):
                log.info(f"{len(contacts) - len(kept)} tickets skipped: already reviewed by a later run")
            kept = _exclude_held(conn, run_id, kept, (*UNCERTAIN_STATUSES, "submitted"), log)
        items = _items(version, kept)
        _build_requests(items, system_blocks(version), reviewer, images_loader, log)
        _execute(conn, run_id, version, reviewer, items, anthropic_client, log, sleep,
                 from_status="awaiting_confirmation", log_dir=log_dir)

    _guarded(conn, run_id, log, body)


def resume_runs(conn: sqlite3.Connection, *, anthropic_client: Any, log_dir: Path = LOG_DIR,
                secrets: Sequence[str | None] = (), sleep: Sleep = time.sleep) -> list[int]:
    """Collect every `submitted` run's batch and complete the run. Never creates a batch.

    Runs left in `submitting` (their process died during create) become `submission_unknown` first.
    A run another live process is still driving (it recorded its process id) is skipped, since that
    process is polling its batch. The resumer records itself as the driver of each run it takes.
    Retryable failures are stored as review errors here, since a retry would be a new submission.
    Returns the ids of the runs it worked on; a run that fails again for another reason stays
    `submitted`.
    """
    settled = _settle_interrupted_submissions(conn, log_dir, secrets)
    runs = conn.execute(
        "SELECT run_id, batch_id, rubric_version_id, pid, model, effort FROM runs WHERE status = 'submitted' "
        "ORDER BY run_id"
    ).fetchall()
    resumed = list(settled)
    for run in runs:
        run_id = run["run_id"]
        if driven_elsewhere(run["pid"]):
            RunLog(log_path(log_dir, run_id), secrets).info(
                f"run {run_id}: process {run['pid']} is still collecting it; left to that process")
            continue
        conn.execute("UPDATE runs SET pid = ? WHERE run_id = ? AND status = 'submitted'", (os.getpid(), run_id))
        conn.commit()
        resumed.append(run_id)
        version = get_version(conn, run["rubric_version_id"])
        # The model and effort its batch was built with. A run submitted before they were recorded
        # used Opus at an effort nobody kept, so its reviews record no effort.
        reviewer = _Reviewer(run["model"] or DEFAULT_MODEL, run["effort"])
        log = RunLog(log_path(log_dir, run_id), secrets)
        log.info(f"resuming run {run_id}: collecting its batch")

        def body(run_id: int = run_id, batch_id: str = run["batch_id"], version: RubricVersion = version,
                 reviewer: _Reviewer = reviewer, log: LogSink = log) -> None:
            items = _items(version, _run_contacts(conn, run_id, unreviewed=True))
            if items:
                _await_batch(conn, run_id, version, reviewer, anthropic_client, batch_id, items,
                             final=True, log=log, sleep=sleep)
            _finish(conn, run_id, log, log_dir)

        _guarded(conn, run_id, log, body)
    return sorted(resumed)


# -- estimate without running, and startup housekeeping ---------------------------------------------


class _QuietLog:
    """A LogSink that drops lines: an estimate creates no run, so it has no run log."""

    def info(self, msg: str) -> None:
        return None

    def warn(self, msg: str) -> None:
        return None


@dataclass(frozen=True)
class Estimate:
    """What a run would cost and cover. The `excluded_` counts are calls in the window it leaves out:
    purged ones (re-reviews and model tests), and for a model test those whose shown review is a review
    error and those without a shown review."""
    estimate_usd: float
    contacts: int
    excluded_purged: int
    excluded_errored: int = 0
    excluded_unreviewed: int = 0
    excluded_held: int = 0      # Review again: calls held by another run in progress


def _estimate_unsent(conn: sqlite3.Connection, version: RubricVersion, contacts: list[FirstContact],
                     reviewer: _Reviewer) -> float:
    """The estimate a run of these contacts would record, with images counted from their URLs rather
    than downloaded."""
    system = system_blocks(version)
    items = _items(version, contacts)
    for item in items:
        if not item.check_ids:
            continue
        images = len(image_urls(item.contact))
        content = user_content(item.contact, item.facts, [(b"", "image/png")] * images)
        item.chars = sum(len(block["text"]) for block in content if block["type"] == "text")
        item.images = images
    return _estimate(conn, system, items, reviewer)


def estimate_window(conn: sqlite3.Connection, window: Window, *, fetcher: Fetcher, rereview: bool) -> Estimate:
    """What a run (or re-review) of the window would select and cost now. Stores and submits nothing.

    Uses the same selection and estimate as a real run started now (on the current model and effort),
    except that images are counted from their URLs rather than downloaded. Purged calls are never
    re-reviewed; a re-review estimate counts them.
    """
    version = ensure_seeded(conn)
    log = _QuietLog()
    fetched = fetcher(window, log)
    candidates = _rereview_candidates(conn, window, fetched, log)[0] if rereview else fetched
    selected = _select(conn, 0, version, candidates, select_all=rereview, log=log)
    excluded = _purged_in_window(conn, window) if rereview else 0
    estimate = _estimate_unsent(conn, version, selected, _current_reviewer(conn))
    return Estimate(estimate_usd=estimate, contacts=len(selected), excluded_purged=excluded)


def estimate_tickets(conn: sqlite3.Connection, ticket_ids: Iterable[int], *, fetcher_for_ids: IdsFetcher) -> Estimate:
    """What `rereview_tickets` of these tickets would select and cost now. Stores and submits nothing.

    The same choice (fresh from Zendesk by id, purged and held calls left out and counted) and estimate as
    the run started now, with images counted from their URLs. Raises ValueError when none is stored.
    """
    version = ensure_seeded(conn)
    choice = _ticket_choice(conn, 0, _stored_tickets(conn, ticket_ids), fetcher_for_ids, _QuietLog())
    contacts = choice.selection.contacts
    return Estimate(estimate_usd=_estimate_unsent(conn, version, contacts, _current_reviewer(conn)),
                    contacts=len(contacts), excluded_purged=choice.selection.excluded_purged,
                    excluded_held=choice.held)


def estimate_test(conn: sqlite3.Connection, window: Window, *, model_id: str, effort: str,
                  limit: int | None) -> Estimate:
    """What `start_test` with these choices would test and cost now, from stored data alone: no Zendesk
    call, nothing stored or submitted. The same calls, prompt and estimate as the test (at the model's
    prices and its output tokens learned at that effort), with images counted from their URLs."""
    reviewer = _test_reviewer(model_id, effort)
    _check_limit(limit)
    version = ensure_seeded(conn)
    pool = _test_pool(conn, window)
    chosen = [c for c, _ in pool.candidates[:limit]]
    return Estimate(estimate_usd=_estimate_unsent(conn, version, chosen, reviewer), contacts=len(chosen),
                    excluded_purged=pool.purged, excluded_errored=pool.errored, excluded_unreviewed=pool.unreviewed)


INTERRUPTED = "interrupted"
DROPPED_CALL_EVIDENCE = "Dropped call: the 10-minute rule doesn't apply; the callback is checked instead."
FETCHING_DEAD_AFTER = timedelta(hours=2)


def fail_interrupted_fetching(conn: sqlite3.Connection, now: datetime,
                              alive: Callable[[int], bool]) -> list[int]:
    """Mark every `fetching` run whose process is gone as failed ("interrupted"); return their ids.

    A run counts as gone when it has no recorded process, its process is not running, or it has
    been fetching for more than two hours (a process id can be reused). Fetching runs hold no
    batch, so failing them never loses a paid result.
    """
    cutoff = _iso(now - FETCHING_DEAD_AFTER)
    failed = []
    for run in conn.execute("SELECT run_id, pid, created_at FROM runs WHERE status = 'fetching' ORDER BY run_id"):
        if run["pid"] is None or run["created_at"] < cutoff or not alive(run["pid"]):
            failed.append(run["run_id"])
    for run_id in failed:
        conn.execute(
            "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE run_id = ? AND status = 'fetching'",
            (INTERRUPTED, _iso(now), run_id),
        )
    conn.commit()
    return failed
