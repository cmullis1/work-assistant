"""Run orchestration against fake Anthropic and Zendesk collaborators. No network, no real credentials."""
import dataclasses
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, Usage
from anthropic.types.messages import MessageBatchIndividualResponse, MessageBatchSucceededResult

from work_assistant import cli, pipeline
from work_assistant.costs import actual_cost
from work_assistant.db import connect
from work_assistant.models import Comment, FirstContact
from work_assistant.pipeline import (
    ANTHROPIC_KEY_REJECTED,
    ZENDESK_TOKEN_REJECTED,
    confirm_run,
    contact_from_snapshot,
    rereview,
    resume_runs,
    start_run,
    zendesk_images_loader,
)
from work_assistant.rubric import current_version, ensure_seeded, save_version
from work_assistant.settings import set_setting
from work_assistant.zendesk.client import ZendeskAuthError, ZendeskClient

UTC = timezone.utc
WINDOW = (datetime(2026, 10, 4, 5, tzinfo=UTC), datetime(2026, 10, 11, 5, tzinfo=UTC))
MONDAY_OCT_19 = datetime(2026, 10, 19, 11, tzinfo=UTC)  # 06:00 in Chicago, when the weekly task runs
SUBJECT = "Fun Zone - POS frozen"
USAGE = dict(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
ONE_RESULT_COST = actual_cost(USAGE, OPUS)


# -- fakes ----------------------------------------------------------------------------------------


class Killed(BaseException):
    """Stands in for the process being killed (or the PC sleeping) while a batch is in flight."""


def make_contact(ticket_id, priority="normal", image_urls=(), call_started_at="2026-10-05T10:00:00Z",
                 duration_seconds=420):
    comments = (
        Comment(comment_id=ticket_id * 10 + 1, created_at="2026-10-05T10:07:30Z", author_id=0,
                author_name="System", author_role="system", public=False, kind="transcript",
                text="[00:03] Agent: Thank you for calling technical support, this is Sam.",
                image_urls=()),
        Comment(comment_id=ticket_id * 10 + 2, created_at="2026-10-05T10:09:00Z", author_id=7,
                author_name="Sam Agent", author_role="agent", public=False, kind="triage_macro",
                text="Symptoms: see screenshot", image_urls=tuple(image_urls)),
    )
    return FirstContact(
        ticket_id=ticket_id, channel="voice", subject=SUBJECT, priority=priority, tags=("pos",),
        fields={"Location ID": "L-9"}, call_started_at=call_started_at, call_duration_seconds=duration_seconds,
        agent_user_id=7, agent_email="sam@example.com", agent_name="Sam Agent", attribution_ok=True,
        summary_email_at="2026-10-05T12:25:00Z", comments=comments,
    )


def schema_check_ids(request):
    schema = request["params"]["output_config"]["format"]["schema"]
    return schema["properties"]["checks"]["items"]["properties"]["check_id"]["enum"]


def good_text(check_ids, not_counted_reason=None, call_dropped="no"):
    return json.dumps({
        "resolved_on_call": "yes",
        "call_dropped": call_dropped,
        "counted": "no" if not_counted_reason else "yes",
        "not_counted_reason": not_counted_reason or "",
        "checks": [{"check_id": cid, "outcome": "met", "evidence": "model evidence"} for cid in check_ids],
    })


def succeeded(custom_id, text):
    message = Message.model_construct(
        id="msg_1", type="message", role="assistant", model="claude-opus-5-5",
        content=[TextBlock.model_construct(type="text", text=text, citations=None)],
        stop_reason="end_turn", stop_details=None, stop_sequence=None, usage=Usage.model_construct(**USAGE),
    )
    return MessageBatchIndividualResponse.model_construct(
        custom_id=custom_id, result=MessageBatchSucceededResult.model_construct(type="succeeded", message=message))


class FakeBatches:
    """Answers every request in a batch from its own schema; can be told to fail or to stall."""

    def __init__(self):
        self.creates = []
        self.requests = {}
        self.invalid = {}          # custom_id -> number of leading attempts that return invalid JSON
        self.not_counted = {}      # custom_id -> the not_counted_reason the model reports
        self.dropped = {}          # custom_id -> the call_dropped answer the model reports
        self.served = Counter()
        self.on_create = None
        self.create_error = None
        self.retrieve_error = None
        self.die_after_create = None  # create number after which the process "dies" once accepted

    def create(self, requests):
        if self.create_error is not None:
            raise self.create_error
        if self.on_create is not None:
            self.on_create()
        self.creates.append(list(requests))
        batch_id = f"batch_{len(self.creates)}"
        self.requests[batch_id] = list(requests)
        if self.die_after_create == len(self.creates):
            raise Killed()  # Anthropic accepted the batch, but its id never reached the caller
        return NS(id=batch_id)

    def retrieve(self, batch_id):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return NS(processing_status="ended")

    def results(self, batch_id):
        for request in self.requests[batch_id]:
            custom_id = request["custom_id"]
            self.served[custom_id] += 1
            if self.served[custom_id] <= self.invalid.get(custom_id, 0):
                yield succeeded(custom_id, "not json")
            else:
                yield succeeded(custom_id, good_text(schema_check_ids(request), self.not_counted.get(custom_id),
                                                  self.dropped.get(custom_id, "no")))


class FakeClient:
    """Mimics anthropic.Anthropic's `with_options`, recording the options each batches call ran with."""

    def __init__(self, batches, options=None):
        self._batches = batches
        self.options = dict(options or {})
        self.messages = NS(batches=self)

    def with_options(self, **options):
        return FakeClient(self._batches, {**self.options, **options})

    def create(self, requests):
        self._batches.calls.append(("create", self.options))
        return self._batches.create(requests)

    def retrieve(self, batch_id):
        self._batches.calls.append(("retrieve", self.options))
        return self._batches.retrieve(batch_id)

    def results(self, batch_id):
        self._batches.calls.append(("results", self.options))
        return self._batches.results(batch_id)


def client_for(fake):
    if not hasattr(fake, "calls"):
        fake.calls = []
    return FakeClient(fake)


def api_response(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches"))


# -- helpers --------------------------------------------------------------------------------------


@pytest.fixture
def version(conn):
    # These tests use fixed October 2026 call dates; a long retention keeps the run-end purge
    # (Task 12a) from erasing them once the real clock passes 90 days. Retention has its own tests.
    set_setting(conn, "retention_days", 36500)
    return ensure_seeded(conn)


@pytest.fixture
def logs(tmp_path):
    return tmp_path / "logs"


def go(conn, logs, fake, contacts, kind="manual", images=None, **kwargs):
    return start_run(
        conn, kind, WINDOW,
        fetcher=lambda window, log: list(contacts),
        anthropic_client=client_for(fake),
        images_loader=images or (lambda contact, log: []),
        log_dir=logs,
        **kwargs,
    )


def fetching(contacts):
    """A fetcher that returns these contacts for any window."""
    return lambda window, log: list(contacts)


def run_row(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def current_reviews(conn):
    return conn.execute("SELECT * FROM reviews WHERE is_current = 1 ORDER BY ticket_id").fetchall()


def checks_of(conn, review_id):
    rows = conn.execute("SELECT * FROM check_results WHERE review_id = ?", (review_id,)).fetchall()
    return {r["check_id"]: r for r in rows}


# -- the brief's tests ----------------------------------------------------------------------------


def test_run_reviews_and_stores(conn, version, logs):
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102, priority="urgent")])

    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert run["actual_usd"] > 0
    assert run["actual_usd"] == pytest.approx(2 * ONE_RESULT_COST)
    assert run["contact_count"] == 2
    assert run["rubric_version_id"] == version.version_id
    assert run["finished_at"] is not None

    reviews = current_reviews(conn)
    assert [r["ticket_id"] for r in reviews] == [101, 102]
    assert all(r["status"] == "ok" and r["run_id"] == run_id for r in reviews)
    assert all(r["resolved_on_call"] == "yes" for r in reviews)
    assert all(r["counted"] == 1 and r["not_counted_reason"] is None for r in reviews)

    normal = checks_of(conn, reviews[0]["review_id"])
    assert len(normal) == 23
    assert Counter(r["decided_by"] for r in normal.values()) == {"model": 21, "code": 2}
    assert normal["pr-call-length"]["decided_by"] == "code"
    assert normal["pr-call-length"]["evidence"] == "Call lasted 7.0 min (limit 10)."
    assert normal["sc01"]["evidence"] == "model evidence"

    urgent = checks_of(conn, reviews[1]["review_id"])  # call length is left to the model when exempt
    assert len(urgent) == 23
    assert Counter(r["decided_by"] for r in urgent.values()) == {"model": 22, "code": 1}

    stored = conn.execute("SELECT * FROM contacts WHERE ticket_id = 101").fetchone()
    assert stored["channel"] == "voice"
    assert contact_from_snapshot(stored["snapshot"]) == make_contact(101)

    requests = fake.creates[0]
    assert sorted(r["custom_id"] for r in requests) == ["t101", "t102"]


def test_run_idempotent(conn, version, logs):
    contacts = [make_contact(101), make_contact(102)]
    go(conn, logs, FakeBatches(), contacts)
    fake = FakeBatches()
    second = go(conn, logs, fake, contacts)
    assert fake.creates == []
    run = run_row(conn, second)
    assert run["status"] == "complete" and run["contact_count"] == 0 and run["actual_usd"] == 0
    assert len(current_reviews(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2


def test_budget_guard_pauses(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    run = run_row(conn, run_id)
    assert run["status"] == "awaiting_confirmation"
    assert run["estimate_usd"] > 0.01
    assert run["contact_count"] == 2
    assert fake.creates == []
    assert current_reviews(conn) == []

    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert len(fake.creates) == 1 and len(fake.creates[0]) == 2
    assert len(current_reviews(conn)) == 2


def test_empty_week_completes(conn, version, logs):
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [])
    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert run["actual_usd"] == 0
    assert run["contact_count"] == 0
    assert fake.creates == []


def test_resume_does_not_resubmit(conn, version, logs):
    fake = FakeBatches()
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101), make_contact(102)])
    run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    run = run_row(conn, run_id)
    assert run["status"] == "submitted" and run["batch_id"] == "batch_1"

    fake.retrieve_error = None
    creates_before = len(fake.creates)
    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [run_id]
    assert len(fake.creates) == creates_before  # zero new create calls
    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert run["actual_usd"] == pytest.approx(2 * ONE_RESULT_COST)
    assert [r["ticket_id"] for r in current_reviews(conn)] == [101, 102]
    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == []


def test_version_frozen_during_run(conn, version, logs):
    fake = FakeBatches()
    fake.on_create = lambda: save_version(conn, version.checks, version.reference, version.params, "edited mid-run")
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert current_version(conn).version_id != version.version_id
    assert run_row(conn, run_id)["rubric_version_id"] == version.version_id
    reviews = conn.execute("SELECT rubric_version_id FROM reviews").fetchall()
    assert [r["rubric_version_id"] for r in reviews] == [version.version_id]


def test_retry_once(conn, version, logs):
    fake = FakeBatches()
    fake.invalid = {"t101": 1}
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert len(fake.creates) == 2
    assert [r["custom_id"] for r in fake.creates[1]] == ["t101"]
    reviews = current_reviews(conn)
    assert [r["status"] for r in reviews] == ["ok", "ok"]
    assert reviews[0]["cost_usd"] == pytest.approx(2 * ONE_RESULT_COST)  # both attempts are billed
    assert run_row(conn, run_id)["actual_usd"] == pytest.approx(3 * ONE_RESULT_COST)

    conn.execute("UPDATE reviews SET is_current = 0")
    conn.commit()
    fake = FakeBatches()
    fake.invalid = {"t101": 2}
    go(conn, logs, fake, [make_contact(101)])
    assert len(fake.creates) == 2
    review = current_reviews(conn)[0]
    assert review["status"] == "error"
    assert review["error"].startswith("invalid output:")
    checks = checks_of(conn, review["review_id"])
    assert set(checks) == {"pr-call-length", "pr-email-sent"}
    assert all(r["decided_by"] == "code" for r in checks.values())
    # An error review keeps the output tokens its last attempt used; estimates never count them.
    assert (review["model"], review["effort"], review["output_tokens"]) == (OPUS, "medium", 500)


def test_rereview_flags_overrides(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102)])
    old = current_reviews(conn)[0]
    conn.execute(
        "UPDATE check_results SET override_outcome = 'missed', override_note = 'disagree', "
        "override_at = '2026-10-12T00:00:00Z' WHERE review_id = ? AND check_id = 'sc06'",
        (old["review_id"],),
    )
    conn.commit()

    fake = FakeBatches()
    run_id = rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(102)]),
                      anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                      log_dir=logs)
    assert run_row(conn, run_id)["kind"] == "rereview"
    assert run_row(conn, run_id)["status"] == "complete"
    assert len(fake.creates) == 1 and len(fake.creates[0]) == 2

    new = current_reviews(conn)[0]
    assert new["ticket_id"] == 101 and new["review_id"] != old["review_id"] and new["run_id"] == run_id
    checks = checks_of(conn, new["review_id"])
    assert checks["sc06"]["needs_recheck"] == 1
    assert checks["sc06"]["override_outcome"] is None and checks["sc06"]["override_note"] is None
    assert sum(r["needs_recheck"] for r in checks.values()) == 1

    kept = conn.execute("SELECT * FROM reviews WHERE review_id = ?", (old["review_id"],)).fetchone()
    assert kept["is_current"] == 0
    assert checks_of(conn, old["review_id"])["sc06"]["override_outcome"] == "missed"


def test_expired_anthropic_key_message(conn, version, logs):
    fake = FakeBatches()
    fake.create_error = anthropic.AuthenticationError("invalid x-api-key", response=api_response(401), body=None)
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "failed"
    assert run["error"] == (
        "Anthropic API key was rejected (it may have expired or been revoked). "
        "Create a new key in the Anthropic Console, then run: work-assistant setup"
    )
    assert run["error"] == ANTHROPIC_KEY_REJECTED


def test_permission_denied_uses_key_message(conn, version, logs):
    fake = FakeBatches()
    fake.create_error = anthropic.PermissionDeniedError("denied", response=api_response(403), body=None)
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, run_id)["error"] == ANTHROPIC_KEY_REJECTED


def test_expired_zendesk_token_message(conn, version, logs):
    def fetcher(window, log):
        raise ZendeskAuthError("Zendesk rejected the credentials (HTTP 401) for /api/v2/users/search.json")

    fake = FakeBatches()
    run_id = start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(fake),
                       images_loader=lambda c, log: [], log_dir=logs)
    run = run_row(conn, run_id)
    assert run["status"] == "failed"
    assert run["error"] == (
        "Zendesk token was rejected (it may have expired or been revoked). "
        "Create a new token in Zendesk Admin Center, then run: work-assistant setup"
    )
    assert run["error"] == ZENDESK_TOKEN_REJECTED
    assert fake.creates == []


def test_scheduled_resumes_first(conn, version, logs, tmp_path):
    fake = FakeBatches()
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)], kind="scheduled")
    old_run = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    fake.retrieve_error = None

    seen = {}

    def fetcher(window, log):
        seen["old_status"] = run_row(conn, old_run)["status"]
        seen["runs"] = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return [make_contact(201)]

    # Monday Oct 12: the old run covered Oct 4-10, so this run reviews Sunday Oct 11.
    services = cli.Services(anthropic_client=client_for(fake), fetcher=fetcher, images_loader=lambda c, log: [],
                            log_dir=logs, now=lambda: datetime(2026, 10, 12, 11, tzinfo=UTC))
    assert cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert seen == {"old_status": "complete", "runs": 2}
    assert len(fake.creates) == 2  # the old batch was collected, never resubmitted
    new_run = run_row(conn, old_run + 1)
    assert new_run["kind"] == "scheduled" and new_run["status"] == "complete"
    assert [r["ticket_id"] for r in current_reviews(conn)] == [101, 201]


# -- rulings ---------------------------------------------------------------------------------------


def test_code_only_contact_sends_no_request(conn, version, logs):
    timed_only = tuple(dataclasses.replace(c, enabled=c.timed is not None) for c in version.checks)
    v2 = save_version(conn, timed_only, version.reference, version.params, "timed only")
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert fake.creates == []
    run = run_row(conn, run_id)
    assert run["status"] == "complete" and run["actual_usd"] == 0 and run["contact_count"] == 1
    review = current_reviews(conn)[0]
    assert review["status"] == "ok" and review["rubric_version_id"] == v2.version_id
    checks = checks_of(conn, review["review_id"])
    assert set(checks) == {"pr-call-length", "pr-email-sent"}
    assert all(r["decided_by"] == "code" for r in checks.values())
    # Recorded with the run's model and effort; no output tokens were measured.
    assert (review["model"], review["effort"], review["output_tokens"]) == (OPUS, "medium", None)


def test_images_filtered_by_type_and_logged(conn, version, logs):
    def images(contact, log):
        return [(b"png-bytes", "image/png"), (b"%PDF", "application/pdf"), (b"jpg", "IMAGE/JPEG; charset=binary")]

    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)], images=images)
    content = fake.creates[0][0]["params"]["messages"][0]["content"]
    media = [block["source"]["media_type"] for block in content if block["type"] == "image"]
    assert media == ["image/png", "image/jpeg"]
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ticket 101" in text and "dropped" in text
    assert "application/pdf" not in text  # the line names only the ticket id


def test_images_over_the_size_limit_are_dropped_and_logged(conn, version, logs):
    """Anthropic rejects a whole request whose image exceeds 10 MB once base64-encoded (ticket 484753 failed
    this way twice), so such an image is left out and the call is still reviewed."""
    from work_assistant.pipeline import MAX_IMAGE_BYTES

    fits, too_big = b"a" * MAX_IMAGE_BYTES, b"b" * (MAX_IMAGE_BYTES + 1)

    def images(contact, log):
        return [(fits, "image/png"), (too_big, "image/jpeg")]

    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)], images=images)
    content = fake.creates[0][0]["params"]["messages"][0]["content"]
    sent = [block["source"] for block in content if block["type"] == "image"]
    assert [source["media_type"] for source in sent] == ["image/png"]
    assert all(len(source["data"]) <= 10 * 1024 * 1024 for source in sent)
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ticket 101: dropped an image over the 10 MB limit" in text


def test_estimate_counts_contact_text_and_images(conn, version, logs, monkeypatch):
    seen = {}
    real = pipeline.estimate_cost

    def spy(system_chars, contact_chars, image_counts, model_id, output_tokens):
        seen.update(system=system_chars, chars=contact_chars, images=image_counts, model=model_id,
                    output=output_tokens)
        return real(system_chars, contact_chars, image_counts, model_id, output_tokens)

    monkeypatch.setattr(pipeline, "estimate_cost", spy)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)], images=lambda c, log: [(b"a", "image/png"), (b"b", "image/gif")])
    params = fake.creates[0][0]["params"]
    texts = [b["text"] for b in params["messages"][0]["content"] if b["type"] == "text"]
    assert seen["chars"] == [sum(len(t) for t in texts)]
    assert seen["images"] == [2]
    assert seen["system"] == sum(len(b["text"]) for b in params["system"])
    assert (seen["model"], seen["output"]) == (OPUS, 6000)
    assert run_row(conn, run_id)["estimate_usd"] == real(seen["system"], seen["chars"], seen["images"], OPUS, 6000)


def test_limit_reviews_first_n_by_ticket_id(conn, version, logs):
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(303), make_contact(101), make_contact(202)], limit=2)
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t202"]
    assert run_row(conn, run_id)["contact_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 3


def test_confirm_keeps_the_limited_selection(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(303), make_contact(101)], limit=1)
    assert run_row(conn, run_id)["status"] == "awaiting_confirmation"
    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]


def test_confirm_rejects_run_not_awaiting(conn, version, logs):
    run_id = go(conn, logs, FakeBatches(), [])
    with pytest.raises(ValueError, match="not awaiting confirmation"):
        confirm_run(conn, run_id, anthropic_client=client_for(FakeBatches()), images_loader=lambda c, log: [],
                    log_dir=logs)


def test_network_errors_fail_with_class_name(conn, version, logs):
    def fetcher(window, log):
        raise httpx.ConnectError("connection to https://acme.zendesk.com/api/v2/x?query=email:a@b.com failed")

    run_id = start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
                       images_loader=lambda c, log: [], log_dir=logs)
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and run["error"] == "ConnectError"
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ConnectError" in text and "zendesk.com" not in text and "a@b.com" not in text
    # An Anthropic connection error from create is ambiguous, not a failure: see
    # test_ambiguous_create_errors_give_submission_unknown.


def test_connection_lost_after_submit_stays_resumable(conn, version, logs):
    fake = FakeBatches()
    fake.retrieve_error = anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com"))
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "submitted" and run["batch_id"] == "batch_1"
    assert run["error"] == "APIConnectionError"

    fake.retrieve_error = None
    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [run_id]
    run = run_row(conn, run_id)
    assert run["status"] == "complete" and run["error"] is None
    assert len(fake.creates) == 1


def test_resume_stores_retryable_failures_as_errors(conn, version, logs):
    fake = FakeBatches()
    fake.invalid = {"t101": 1}
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    fake.retrieve_error = None
    run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs)
    assert len(fake.creates) == 1  # resume never creates a batch, not even a retry
    review = current_reviews(conn)[0]
    assert review["status"] == "error" and review["run_id"] == run_id


def test_log_has_no_customer_content(conn, version, logs):
    run_id = go(conn, logs, FakeBatches(), [make_contact(101)])
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "101" in text
    for content in (SUBJECT, "sam@example.com", "Sam Agent", "Thank you for calling", "model evidence"):
        assert content not in text


def test_log_redacts_secrets(conn, version, logs):
    def fetcher(window, log):
        log.info("leaked zd-secret-1 and sk-ant-2")
        return []

    run_id = start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
                       images_loader=lambda c, log: [], log_dir=logs, secrets=["zd-secret-1", "sk-ant-2"])
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "zd-secret-1" not in text and "sk-ant-2" not in text and "[REDACTED]" in text


def test_rereview_only_covers_contacts_in_window(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102, call_started_at="2026-10-12T10:00:00Z")])
    fake = FakeBatches()
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
             anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]


# -- production images loader ---------------------------------------------------------------------


def test_zendesk_images_loader_drops_failed_download(logs):
    def handler(request):
        if request.url.path.endswith("/missing.png"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"img", headers={"Content-Type": "image/png"})

    client = ZendeskClient("acme", "owner@example.com", "tok", transport=httpx.MockTransport(handler),
                           sleep=lambda s: None)
    contact = make_contact(101, image_urls=("https://acme.zendesk.com/a.png", "https://acme.zendesk.com/missing.png"))
    lines = []
    log = NS(info=lines.append, warn=lines.append)
    assert zendesk_images_loader(client)(contact, log) == [(b"img", "image/png")]
    assert len(lines) == 1 and "ticket 101" in lines[0] and "missing.png" not in lines[0]


def test_zendesk_images_loader_propagates_auth_errors():
    client = ZendeskClient("acme", "owner@example.com", "tok",
                           transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda s: None)
    contact = make_contact(101, image_urls=("https://acme.zendesk.com/a.png",))
    with pytest.raises(ZendeskAuthError):
        zendesk_images_loader(client)(contact, NS(info=print, warn=print))


# -- CLI ------------------------------------------------------------------------------------------

CHI = "America/Chicago"


def services_for(fake, logs, contacts=(), seen=None, secrets=(), now=None):
    def fetcher(window, log):
        if seen is not None:
            seen.append(window)
        return list(contacts)

    extra = {"now": lambda: now} if now is not None else {}
    return cli.Services(anthropic_client=client_for(fake), fetcher=fetcher, images_loader=lambda c, log: [],
                        log_dir=logs, secrets=tuple(secrets), **extra)


def test_cli_run_week_of_prints_summary_without_secrets(conn, version, logs, tmp_path, capsys):
    from datetime import date

    from work_assistant.timeutil import week_window

    set_setting(conn, "timezone", CHI)
    seen = []
    fake = FakeBatches()
    services = services_for(fake, logs, [make_contact(202), make_contact(101)], seen, secrets=("sk-ant-SECRET",))
    code = cli.main(["run", "--week-of", "2026-10-07", "--limit", "1"], db_path=tmp_path / "work-assistant.db",
                    services=services)
    out = capsys.readouterr().out
    assert code == 0
    assert seen == [week_window(date(2026, 10, 7), CHI)]
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]
    assert "Run 1: complete" in out and "Contacts: 1" in out and "Estimate: $" in out and "Actual: $" in out
    assert "sk-ant-SECRET" not in out
    assert run_row(conn, 1)["kind"] == "manual"


def test_cli_from_to_window_is_inclusive(conn, version, logs, tmp_path):
    set_setting(conn, "timezone", CHI)
    seen = []
    cli.main(["run", "--from", "2026-10-04", "--to", "2026-10-10"], db_path=tmp_path / "work-assistant.db",
             services=services_for(FakeBatches(), logs, [], seen))
    assert seen == [(datetime(2026, 10, 4, 5, tzinfo=UTC), datetime(2026, 10, 11, 5, tzinfo=UTC))]


def test_cli_from_after_to_is_rejected(conn, version, logs, tmp_path):
    with pytest.raises(SystemExit, match="--from"):
        cli.main(["run", "--from", "2026-10-10", "--to", "2026-10-04"], db_path=tmp_path / "work-assistant.db",
                 services=services_for(FakeBatches(), logs))


def test_cli_budget_pause_then_confirm(conn, version, logs, tmp_path, capsys):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    services = services_for(fake, logs, [make_contact(101)])
    db_path = tmp_path / "work-assistant.db"
    assert cli.main(["run", "--last-week"], db_path=db_path, services=services) == 0
    out = capsys.readouterr().out
    assert "awaiting_confirmation" in out and "work-assistant confirm 1" in out
    assert fake.creates == []

    assert cli.main(["confirm", "1"], db_path=db_path, services=services) == 0
    assert "Run 1: complete" in capsys.readouterr().out
    assert len(fake.creates) == 1


def test_cli_yes_skips_budget_pause(conn, version, logs, tmp_path):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    cli.main(["run", "--last-week", "--yes"], db_path=tmp_path / "work-assistant.db",
             services=services_for(fake, logs, [make_contact(101)]))
    assert run_row(conn, 1)["status"] == "complete"


def test_cli_resume(conn, version, logs, tmp_path, capsys):
    fake = FakeBatches()
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    fake.retrieve_error = None
    assert cli.main(["resume"], db_path=tmp_path / "work-assistant.db", services=services_for(fake, logs)) == 0
    assert "Run 1: complete" in capsys.readouterr().out
    assert len(fake.creates) == 1


def test_cli_rereview(conn, version, logs, tmp_path):
    set_setting(conn, "timezone", CHI)
    go(conn, logs, FakeBatches(), [make_contact(101)])
    fake = FakeBatches()
    seen = []
    cli.main(["run", "--week-of", "2026-10-05", "--rereview"], db_path=tmp_path / "work-assistant.db",
             services=services_for(fake, logs, [changed(101)], seen))
    assert run_row(conn, 2)["kind"] == "rereview"
    assert len(fake.creates) == 1
    assert len(seen) == 1  # the re-review fetched the window from Zendesk
    assert contact_from_snapshot(contact_row(conn, 101)["snapshot"]) == changed(101)


def test_cli_failed_run_returns_error_code(conn, version, logs, tmp_path, capsys):
    fake = FakeBatches()
    fake.create_error = anthropic.AuthenticationError("bad", response=api_response(401), body=None)
    code = cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db",
                    services=services_for(fake, logs, [make_contact(101)]))
    assert code == 1
    assert ANTHROPIC_KEY_REJECTED in capsys.readouterr().out


# -- the daily task catches up on missed days ---------------------------------------------------------


def chicago_week(start_day, end_day):
    """The UTC bounds of a Chicago week in October 2026 (CDT, UTC-5) from its Sunday to the next Sunday."""
    return (datetime(2026, 10, start_day, 5, tzinfo=UTC), datetime(2026, 10, end_day, 5, tzinfo=UTC))


def chicago_day(day):
    """The UTC bounds of a Chicago day in October 2026 (CDT, UTC-5), midnight to midnight."""
    start = datetime(2026, 10, day, 5, tzinfo=UTC)
    return start, start + timedelta(days=1)


def chicago_days(first, last):
    return [chicago_day(day) for day in range(first, last + 1)]


def scheduled_run(conn, window, status="complete"):
    from factories import add_run

    return add_run(conn, kind="scheduled", status=status,
                   window=tuple(m.strftime("%Y-%m-%dT%H:%M:%SZ") for m in window))


def runs_after(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE run_id > ? ORDER BY run_id", (run_id,)).fetchall()


def run_scheduled(services, tmp_path, *extra):
    return cli.main(["run", "--scheduled", *extra], db_path=tmp_path / "work-assistant.db", services=services)


@pytest.fixture
def chicago(conn, version):
    set_setting(conn, "timezone", "America/Chicago")


def test_scheduled_reviews_each_missed_day_oldest_first(conn, chicago, logs, tmp_path, capsys):
    last = scheduled_run(conn, chicago_day(15))  # reviewed Thursday Oct 15, then the PC was off for the weekend
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    assert seen == chicago_days(16, 18)
    new = runs_after(conn, last)
    assert [(r["kind"], r["status"]) for r in new] == [("scheduled", "complete")] * 3
    assert [(r["window_start"], r["window_end"]) for r in new] == [
        ("2026-10-16T05:00:00Z", "2026-10-17T05:00:00Z"), ("2026-10-17T05:00:00Z", "2026-10-18T05:00:00Z"),
        ("2026-10-18T05:00:00Z", "2026-10-19T05:00:00Z")]
    out = capsys.readouterr().out
    assert out.index("Reviewing Friday 2026-10-16.") < out.index("Reviewing Saturday 2026-10-17.") \
        < out.index("Reviewing Sunday 2026-10-18.")
    assert all(f"Run {r['run_id']}: complete." in out for r in new)


def test_old_weekly_task_arguments_run_the_daily_catch_up(conn, chicago, logs, tmp_path):
    """A task installed before the switch still runs `run --last-week --scheduled`: it catches up by day too."""
    last = scheduled_run(conn, chicago_day(16))
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    assert cli.main(["run", "--last-week", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert seen == chicago_days(17, 18)
    assert [r["kind"] for r in runs_after(conn, last)] == ["scheduled", "scheduled"]


def test_scheduled_without_coverage_reviews_yesterday(conn, chicago, logs, tmp_path):
    # Manual runs, failed and cancelled scheduled runs do not count as covered.
    from factories import add_run

    add_run(conn, kind="manual", window=("2026-10-18T05:00:00Z", "2026-10-19T05:00:00Z"))
    scheduled_run(conn, chicago_day(18), status="failed")
    scheduled_run(conn, chicago_week(11, 18), status="cancelled")
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    assert seen == [chicago_day(18)]


@pytest.mark.parametrize("status", ["awaiting_confirmation", "submitted", "submission_unknown"])
def test_scheduled_paused_and_held_runs_count_as_covered(conn, chicago, logs, tmp_path, status):
    scheduled_run(conn, chicago_day(17), status=status)
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    services.alive = lambda pid: True  # a submitted run's process is still collecting it
    run_scheduled(services, tmp_path)
    assert seen == [chicago_day(18)]


@pytest.mark.parametrize("status", ["complete", "awaiting_confirmation"])
def test_daily_catch_up_after_weekly_coverage(conn, chicago, logs, tmp_path, status):
    """Review Focus 3: the first daily run after a weekly one starts the day after the week's end. A weekly run
    still waiting for Confirm covers its week, so nothing is reviewed twice."""
    weekly = scheduled_run(conn, chicago_week(4, 11), status=status)  # Sunday Oct 4 to Saturday Oct 10
    seen = []
    db_path = tmp_path / "work-assistant.db"
    services = services_for(FakeBatches(), logs, [], seen, now=datetime(2026, 10, 14, 11, tzinfo=UTC))
    assert cli.main(["run", "--scheduled"], db_path=db_path, services=services) == 0
    assert seen == chicago_days(11, 13), "Sunday Oct 11 to Tuesday Oct 13, none inside the weekly window"
    assert run_row(conn, weekly)["status"] == status, "the weekly run is left as it was"

    # The next morning reviews just the one new day.
    seen.clear()
    services.now = lambda: datetime(2026, 10, 15, 11, tzinfo=UTC)
    assert cli.main(["run", "--scheduled"], db_path=db_path, services=services) == 0
    assert seen == [chicago_day(14)]


def test_daily_catch_up_skips_a_weekly_run_awaiting_confirmation_for_last_week(conn, chicago, logs, tmp_path):
    # The last weekly run (Monday Oct 19, for Oct 11-17) is waiting for Confirm; the first daily run is Tuesday.
    scheduled_run(conn, chicago_week(11, 18), status="awaiting_confirmation")
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=datetime(2026, 10, 20, 11, tzinfo=UTC))
    assert run_scheduled(services, tmp_path) == 0
    assert seen == chicago_days(18, 19)


def fetching_run(conn, window, pid, created_at):
    from factories import add_run

    return add_run(conn, kind="scheduled", status="fetching", pid=pid, created_at=created_at,
                   window=tuple(m.strftime("%Y-%m-%dT%H:%M:%SZ") for m in window))


def test_scheduled_fails_a_dead_fetch_and_reviews_its_day_again(conn, chicago, logs, tmp_path, capsys):
    scheduled_run(conn, chicago_day(16))
    # Saturday Oct 17 died mid-fetch; its process is gone.
    dead = fetching_run(conn, chicago_day(17), pid=4242, created_at="2026-10-19T10:55:00Z")
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    asked = []
    services.alive = lambda pid: asked.append(pid) or False
    assert run_scheduled(services, tmp_path) == 0
    assert asked == [4242]
    assert run_row(conn, dead)["status"] == "failed" and run_row(conn, dead)["error"] == pipeline.INTERRUPTED
    assert seen == chicago_days(17, 18)
    assert (f"Run {dead} was interrupted while fetching and is marked failed, so its days are reviewed again."
            in capsys.readouterr().out)


def test_a_dead_manual_fetch_is_not_promised_a_retry(conn, chicago, logs, tmp_path, capsys):
    from factories import add_run

    scheduled_run(conn, chicago_day(18))
    dead = add_run(conn, kind="manual", status="fetching", pid=4242, created_at="2026-10-19T10:55:00Z",
                   window=("2026-10-01T05:00:00Z", "2026-10-03T05:00:00Z"))
    services = services_for(FakeBatches(), logs, [], now=MONDAY_OCT_19)
    services.alive = lambda pid: False
    assert run_scheduled(services, tmp_path) == 0
    assert (f"Run {dead} was interrupted while fetching and is marked failed. Start it again to review its calls."
            in capsys.readouterr().out)


def test_scheduled_leaves_a_live_fetch_alone(conn, chicago, logs, tmp_path):
    scheduled_run(conn, chicago_day(16))
    live = fetching_run(conn, chicago_day(17), pid=4242, created_at="2026-10-19T10:55:00Z")
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    services.alive = lambda pid: True  # the app is fetching that day right now
    run_scheduled(services, tmp_path)
    assert run_row(conn, live)["status"] == "fetching"
    assert seen == [chicago_day(18)], "a live fetch covers its day"


def test_scheduled_with_yesterday_covered_starts_no_run(conn, chicago, logs, tmp_path, capsys):
    last = scheduled_run(conn, chicago_day(18))
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    assert seen == [] and runs_after(conn, last) == []
    assert "Every day up to 2026-10-18 is already reviewed; nothing new to review." in capsys.readouterr().out


def test_scheduled_paused_day_does_not_stop_later_days(conn, chicago, logs, tmp_path, capsys):
    set_setting(conn, "budget_limit_usd", 0.01)
    last = scheduled_run(conn, chicago_day(16))

    def fetcher(window, log):
        return [make_contact(101)] if window == chicago_day(17) else []

    fake = FakeBatches()
    services = cli.Services(anthropic_client=client_for(fake), fetcher=fetcher, images_loader=lambda c, log: [],
                            log_dir=logs, now=lambda: MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    paused, later = runs_after(conn, last)
    assert (paused["status"], later["status"]) == ("awaiting_confirmation", "complete")
    assert fake.creates == []  # the budget guard held the paused day
    assert f"work-assistant confirm {paused['run_id']}" in capsys.readouterr().out


def test_scheduled_yes_confirms_each_day(conn, chicago, logs, tmp_path):
    set_setting(conn, "budget_limit_usd", 0.01)
    last = scheduled_run(conn, chicago_day(16))
    seen = []

    def fetcher(window, log):
        seen.append(window)
        return [make_contact(100 + window[0].day)]

    fake = FakeBatches()
    services = cli.Services(anthropic_client=client_for(fake), fetcher=fetcher, images_loader=lambda c, log: [],
                            log_dir=logs, now=lambda: MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path, "--yes") == 0
    assert [r["status"] for r in runs_after(conn, last)] == ["complete", "complete"]
    assert [[r["custom_id"] for r in create] for create in fake.creates] == [["t117"], ["t118"]]


def test_failed_day_stops_catch_up(conn, chicago, logs, tmp_path, capsys):
    last = scheduled_run(conn, chicago_day(15))
    seen = []
    broken = {"on": True}

    def fetcher(window, log):
        seen.append(window)
        if broken["on"]:
            raise ZendeskAuthError("Zendesk rejected the credentials (HTTP 401)")
        return []

    services = cli.Services(anthropic_client=client_for(FakeBatches()), fetcher=fetcher,
                            images_loader=lambda c, log: [], log_dir=logs, now=lambda: MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 1
    assert seen == [chicago_day(16)], "a failed day stops the catch-up, so later days wait for the retry"
    out = capsys.readouterr().out
    assert ZENDESK_TOKEN_REJECTED in out
    assert "Stopped after a failed day: it and the 2 later days are tried again at the next daily run." in out

    broken["on"] = False
    seen.clear()
    assert run_scheduled(services, tmp_path) == 0
    assert seen == chicago_days(16, 18)
    assert [r["status"] for r in runs_after(conn, last)] == ["failed", "complete", "complete", "complete"]


def test_scheduled_catch_up_capped_by_retention(conn, chicago, logs, tmp_path, capsys):
    set_setting(conn, "retention_days", 30)
    scheduled_run(conn, (datetime(2026, 9, 6, 5, tzinfo=UTC), datetime(2026, 9, 13, 5, tzinfo=UTC)))
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    # 30 days before Oct 19 11:00Z is Sep 19 11:00Z, so Sep 13 to Sep 19 are too old; Sep 20 on are reviewed.
    assert seen[0] == (datetime(2026, 9, 20, 5, tzinfo=UTC), datetime(2026, 9, 21, 5, tzinfo=UTC))
    assert seen[-1] == chicago_day(18) and len(seen) == 29
    assert ("7 missed days were skipped: they started more than 30 days ago (the data retention period), "
            "so their calls are not reviewed.") in capsys.readouterr().out


def test_scheduled_takes_no_other_period(conn, chicago, logs, tmp_path, capsys):
    services = services_for(FakeBatches(), logs, [], now=MONDAY_OCT_19)
    for period in (["--week-of", "2026-10-04"], ["--from", "2026-10-01", "--to", "2026-10-02"]):
        with pytest.raises(SystemExit) as exit_:
            run_scheduled(services, tmp_path, *period)
        assert exit_.value.code == 1
        assert ("--scheduled picks its own days (each one since the last scheduled run), so it takes no "
                "--week-of or --from.") in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_manual_run_still_needs_a_period(conn, logs, tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["run"], db_path=tmp_path / "work-assistant.db", services=services_for(FakeBatches(), logs))
    assert exit_.value.code == 2
    assert "choose a period: --last-week, --week-of, or --from with --to" in capsys.readouterr().err


def scheduled_log(logs):
    return (logs / "scheduled.log").read_text(encoding="utf-8")


def test_scheduled_run_writes_its_output_to_scheduled_log(conn, chicago, logs, tmp_path, capsys):
    last = scheduled_run(conn, chicago_day(17))
    services = services_for(FakeBatches(), logs, [], now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 0
    log = scheduled_log(logs)
    assert log.splitlines()[0] == "=== 2026-10-19T11:00:00Z: daily scheduled run ==="
    assert "Reviewing Sunday 2026-10-18." in log
    assert f"Run {last + 1}: complete. Contacts: 0." in log
    assert f"Run {last + 1}: complete." in capsys.readouterr().out, "a terminal still sees the output"

    # A second run appends under its own header.
    run_scheduled(services, tmp_path)
    log = scheduled_log(logs)
    assert log.count("=== 2026-10-19T11:00:00Z: daily scheduled run ===") == 2
    assert "Every day up to 2026-10-18 is already reviewed; nothing new to review." in log


def test_scheduled_log_without_console(conn, chicago, logs, tmp_path, monkeypatch):
    """Under pythonw there is no console: sys.stdout and sys.stderr are None, and only the log is written."""
    scheduled_run(conn, chicago_day(17))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fake = FakeBatches()
    fake.create_error = anthropic.AuthenticationError("bad", response=api_response(401), body=None)
    services = services_for(fake, logs, [make_contact(101)], now=MONDAY_OCT_19)
    assert run_scheduled(services, tmp_path) == 1
    assert sys.stdout is None and sys.stderr is None, "the console streams are restored afterward"
    log = scheduled_log(logs)
    assert "Reviewing Sunday 2026-10-18." in log
    assert ANTHROPIC_KEY_REJECTED in log


def test_scheduled_log_records_exit_messages(conn, logs, tmp_path, capsys):
    services = services_for(FakeBatches(), logs, now=MONDAY_OCT_19)
    with pytest.raises(SystemExit) as exit_:
        run_scheduled(services, tmp_path, "--rereview")
    assert exit_.value.code == 1
    assert "--scheduled and --rereview cannot be combined." in scheduled_log(logs)
    assert "--scheduled and --rereview cannot be combined." in capsys.readouterr().err


def test_scheduled_log_records_unexpected_errors_without_their_message(conn, chicago, logs, tmp_path):
    def fetcher(window, log):
        raise KeyboardInterrupt  # not caught by the run's own guard, so it reaches the CLI

    services = cli.Services(anthropic_client=client_for(FakeBatches()), fetcher=fetcher,
                            images_loader=lambda c, log: [], log_dir=logs, now=lambda: MONDAY_OCT_19)
    with pytest.raises(KeyboardInterrupt):
        run_scheduled(services, tmp_path)
    log = scheduled_log(logs)
    assert "The scheduled run stopped with an unexpected error: KeyboardInterrupt" in log
    assert "in fetcher" in log  # where it happened, from the traceback's frames


def test_manual_run_writes_no_scheduled_log(conn, chicago, logs, tmp_path):
    services = services_for(FakeBatches(), logs, [], now=MONDAY_OCT_19)
    cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db", services=services)
    assert not (logs / "scheduled.log").exists()


def test_manual_last_week_ignores_missed_days(conn, chicago, logs, tmp_path):
    scheduled_run(conn, chicago_day(10))
    seen = []
    services = services_for(FakeBatches(), logs, [], seen, now=datetime(2026, 10, 26, 11, tzinfo=UTC))
    assert cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    assert seen == [chicago_week(18, 25)]
    assert run_row(conn, 2)["kind"] == "manual"


def test_build_services_uses_saved_credentials(conn, version, memory_keyring):
    from work_assistant import credentials

    set_setting(conn, "zendesk_subdomain", "acme")
    set_setting(conn, "zendesk_email", "owner@example.com")
    credentials.set_secret("zendesk_token", "zd-tok")
    credentials.set_secret("anthropic_api_key", "sk-ant-key")
    services = cli.build_services(conn, need_zendesk=True)
    try:
        assert isinstance(services.anthropic_client, anthropic.Anthropic)
        assert services.anthropic_client.api_key == "sk-ant-key"
        assert set(services.secrets) == {"zd-tok", "sk-ant-key"}
        assert callable(services.fetcher) and callable(services.images_loader)
    finally:
        services.close()


def test_build_services_requires_credentials(conn, version):
    with pytest.raises(SystemExit, match="work-assistant setup"):
        cli.build_services(conn, need_zendesk=False)


# -- fix round 1: overlapping runs, error reviews, unrecorded submissions, frozen deadline ---------


def test_new_run_skips_tickets_held_by_submitted_run(conn, version, logs):
    fake = FakeBatches()
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101), make_contact(102)])
    fake.retrieve_error = None

    second = go(conn, logs, fake, [make_contact(101), make_contact(102)])  # the owner runs the week again
    assert len(fake.creates) == 1  # nothing paid twice
    assert run_row(conn, second)["status"] == "complete" and run_row(conn, second)["contact_count"] == 0
    text = (logs / f"run-{second}.log").read_text(encoding="utf-8")
    assert "2 tickets excluded: held by run(s) 1" in text

    resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs)
    assert [(r["ticket_id"], r["run_id"]) for r in current_reviews(conn)] == [(101, 1), (102, 1)]


def test_paused_run_keeps_its_tickets_from_a_yes_run(conn, version, logs, tmp_path, capsys):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101), make_contact(102)], kind="scheduled")
    assert run_row(conn, paused)["status"] == "awaiting_confirmation"

    services = services_for(fake, logs, [make_contact(101), make_contact(102)])
    db_path = tmp_path / "work-assistant.db"
    assert cli.main(["run", "--last-week", "--yes"], db_path=db_path, services=services) == 0
    out = capsys.readouterr().out
    assert "Run 2: complete. Contacts: 0." in out
    assert f"work-assistant confirm {paused}" in out
    assert fake.creates == []

    assert cli.main(["confirm", str(paused)], db_path=db_path, services=services) == 0
    assert len(fake.creates) == 1 and len(fake.creates[0]) == 2  # each ticket paid for once


def test_paused_run_cancelled_when_superseded(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101)])
    newer = rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
                     anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                     confirm=True, log_dir=logs)
    run = run_row(conn, paused)
    assert run["status"] == "cancelled" and run["error"] == f"superseded by run {newer}"
    with pytest.raises(ValueError, match="not awaiting confirmation"):
        confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert len(fake.creates) == 1


def test_confirm_skips_tickets_reviewed_meanwhile(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(102)]),
             anthropic_client=client_for(fake), images_loader=lambda c, log: [], confirm=True,
             limit=1, log_dir=logs)
    assert run_row(conn, paused)["status"] == "awaiting_confirmation"  # 102 is still uncovered

    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[-1]] == ["t102"]
    assert run_row(conn, paused)["status"] == "complete"


def test_older_batch_never_demotes_newer_review(conn, version, logs):
    fake = FakeBatches()
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    fake.retrieve_error = None
    newer = rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
                     anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                     log_dir=logs)

    resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs)
    assert run_row(conn, 1)["status"] == "complete"
    reviews = conn.execute("SELECT run_id, is_current FROM reviews WHERE ticket_id = 101 ORDER BY run_id").fetchall()
    assert [(r["run_id"], r["is_current"]) for r in reviews] == [(1, 0), (newer, 1)]


def test_rereview_error_keeps_previous_review_current(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])
    old = current_reviews(conn)[0]
    conn.execute("UPDATE check_results SET override_outcome = 'missed' WHERE review_id = ? AND check_id = 'sc06'",
                 (old["review_id"],))
    conn.commit()

    fake = FakeBatches()
    fake.invalid = {"t101": 2}
    run_id = rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
                      anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                      log_dir=logs)
    assert current_reviews(conn)[0]["review_id"] == old["review_id"]
    assert checks_of(conn, old["review_id"])["sc06"]["override_outcome"] == "missed"
    error = conn.execute("SELECT * FROM reviews WHERE run_id = ?", (run_id,)).fetchone()
    assert error["status"] == "error" and error["is_current"] == 0
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ticket 101: review error kept as history" in text


UNRECORDED = (
    "Submission may have reached Anthropic without being recorded; check the Batches page in the "
    "Anthropic Console before re-running."
)


def test_crash_during_confirm_create_is_never_resubmitted(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    fake.die_after_create = 1
    with pytest.raises(Killed):
        confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert run_row(conn, run_id)["status"] == "submitting"

    with pytest.raises(ValueError, match="submission_unknown"):
        confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    run = run_row(conn, run_id)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED
    assert len(fake.creates) == 1


def test_crash_during_retry_create_becomes_unknown_on_resume(conn, version, logs):
    fake = FakeBatches()
    fake.invalid = {"t101": 1}
    fake.die_after_create = 2  # the retry batch is accepted, then the process dies
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101), make_contact(102)])
    run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    run = run_row(conn, run_id)
    assert run["status"] == "submitting" and run["batch_id"] is None

    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [run_id]
    run = run_row(conn, run_id)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED
    assert len(fake.creates) == 2
    assert [r["ticket_id"] for r in current_reviews(conn)] == [102]  # the first batch's result was kept


def test_submitted_state_records_batch_id_in_one_step(conn, version, logs):
    seen = []
    fake = FakeBatches()
    fake.on_create = lambda: seen.append(tuple(conn.execute(
        "SELECT status, batch_id FROM runs WHERE run_id = 1").fetchone()))
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    assert seen == [("submitting", None)]
    run = run_row(conn, 1)
    assert (run["status"], run["batch_id"], run["error"]) == ("submitted", "batch_1", None)


def test_fetcher_takes_window_and_log_only(conn, version, logs):
    """The fetch no longer depends on the deadline: later comments are gathered over a fixed collection
    window, so a run, a re-review and an estimate all call fetcher(window, log)."""
    save_version(conn, version.checks, version.reference,
                 dataclasses.replace(version.params, email_deadline_minutes=30), "30 minutes")
    seen = []

    def fetcher(window, log):
        seen.append(window)
        return []

    start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
              images_loader=lambda c, log: [], log_dir=logs)
    rereview(conn, WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
             images_loader=lambda c, log: [], log_dir=logs)
    pipeline.estimate_window(conn, WINDOW, fetcher=fetcher, rereview=False)
    assert seen == [WINDOW, WINDOW, WINDOW]


def test_email_deadline_comes_from_the_frozen_version(conn, version, logs):
    """A rubric edit saved while the run is fetching doesn't change the deadline the run's calls are judged by."""
    frozen = save_version(conn, version.checks, version.reference,
                          dataclasses.replace(version.params, email_deadline_minutes=30), "30 minutes")
    late = dataclasses.replace(make_contact(101), summary_email_at="2026-10-05T10:52:00Z")  # 45 min after the call

    def fetcher(window, log):
        save_version(conn, frozen.checks, frozen.reference,
                     dataclasses.replace(frozen.params, email_deadline_minutes=1440), "edited during fetch")
        return [late]

    start_run(conn, "manual", WINDOW, fetcher=fetcher, anthropic_client=client_for(FakeBatches()),
              images_loader=lambda c, log: [], log_dir=logs)
    [review] = current_reviews(conn)
    assert review["rubric_version_id"] == frozen.version_id
    email = checks_of(conn, review["review_id"])["pr-email-sent"]
    assert email["outcome"] == "missed"
    assert email["evidence"].startswith("Summary email sent 45 min after the call ended (limit 30 min)")


def test_cli_fetcher_passes_window_through(conn, version, monkeypatch):
    from work_assistant import credentials
    from work_assistant.zendesk import fetch

    set_setting(conn, "zendesk_subdomain", "acme")
    set_setting(conn, "zendesk_email", "owner@example.com")
    set_setting(conn, "team", ["sam@example.com"])
    credentials.set_secret("zendesk_token", "zd-tok")
    credentials.set_secret("anthropic_api_key", "sk-ant-key")
    calls = []

    def fake_fetch(client, window, team, log):
        calls.append((team, window))
        return []

    monkeypatch.setattr(fetch, "fetch_contacts", fake_fetch)
    services = cli.build_services(conn, need_zendesk=True)
    try:
        assert services.fetcher(WINDOW, NS(info=print, warn=print)) == []
    finally:
        services.close()
    assert calls == [(["sam@example.com"], WINDOW)]


# -- fix round 2: explicit submission states -------------------------------------------------------


def silent_log():
    return NS(info=lambda m: None, warn=lambda m: None)


def unknown_run(conn, logs, contacts):
    """A run whose create raised a connection error: its outcome at Anthropic is unknown."""
    fake = FakeBatches()
    fake.create_error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    run_id = go(conn, logs, fake, contacts)
    assert run_row(conn, run_id)["status"] == "submission_unknown"
    return run_id


def test_start_crash_in_create_then_resume_marks_unknown_and_holds_tickets(conn, version, logs):
    fake = FakeBatches()
    fake.die_after_create = 1
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert run_row(conn, 1)["status"] == "submitting"

    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [1]
    run = run_row(conn, 1)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED

    again = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert run_row(conn, again)["contact_count"] == 0
    assert len(fake.creates) == 1
    assert run_row(conn, 1)["status"] == "submission_unknown"


def test_start_settles_an_interrupted_submission_first(conn, version, logs):
    fake = FakeBatches()
    fake.die_after_create = 1
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    fake.die_after_create = None
    go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, 1)["status"] == "submission_unknown"
    assert len(fake.creates) == 1


def test_scheduled_after_unknown_run_does_not_resubmit(conn, version, logs, tmp_path, capsys):
    fake = FakeBatches()
    fake.die_after_create = 1
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)], kind="scheduled")
    # The held run covered Oct 4-10, so Monday Oct 12's run reviews Sunday Oct 11.
    services = services_for(fake, logs, [make_contact(101), make_contact(202)],
                            now=datetime(2026, 10, 12, 11, tzinfo=UTC))
    assert cli.main(["run", "--scheduled"], db_path=tmp_path / "work-assistant.db", services=services) == 0
    out = capsys.readouterr().out
    assert run_row(conn, 1)["status"] == "submission_unknown"
    assert "work-assistant clear-run 1" in out
    assert [r["custom_id"] for r in fake.creates[1]] == ["t202"]  # 101 was not paid for again
    assert len(fake.creates) == 2


@pytest.mark.parametrize("error", [
    anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
    anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com")),
    anthropic.InternalServerError("boom", response=api_response(500), body=None),
    anthropic.APIStatusError("unavailable", response=api_response(503), body=None),
], ids=["connection", "timeout", "500", "503"])
def test_ambiguous_create_errors_give_submission_unknown(conn, version, logs, error):
    fake = FakeBatches()
    fake.create_error = error
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED and run["batch_id"] is None
    assert run["finished_at"] is None


@pytest.mark.parametrize("error, message", [
    (anthropic.AuthenticationError("bad key", response=api_response(401), body=None), ANTHROPIC_KEY_REJECTED),
    (anthropic.PermissionDeniedError("denied", response=api_response(403), body=None), ANTHROPIC_KEY_REJECTED),
    (anthropic.BadRequestError("bad", response=api_response(400), body=None), "BadRequestError"),
    (anthropic.RateLimitError("slow down", response=api_response(429), body=None), "RateLimitError"),
], ids=["401", "403", "400", "429"])
def test_clear_4xx_create_errors_fail(conn, version, logs, error, message):
    fake = FakeBatches()
    fake.create_error = error
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and run["error"] == message
    assert go(conn, logs, FakeBatches(), [make_contact(101)]) and run_row(conn, run_id + 1)["contact_count"] == 1


def test_clear_run_frees_tickets(conn, version, logs, tmp_path, capsys, monkeypatch):
    run_id = unknown_run(conn, logs, [make_contact(101)])
    db_path = tmp_path / "work-assistant.db"
    services = services_for(FakeBatches(), logs)

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["clear-run", str(run_id)], db_path=db_path, services=services) == 1
    assert run_row(conn, run_id)["status"] == "submission_unknown"
    assert "Batches page in the Anthropic Console" in capsys.readouterr().out

    assert cli.main(["clear-run", str(run_id), "--yes"], db_path=db_path, services=services) == 0
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and run["error"] == "cleared by owner"

    fake = FakeBatches()
    go(conn, logs, fake, [make_contact(101)])
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]

    with pytest.raises(SystemExit, match="only a run in submission_unknown"):
        cli.main(["clear-run", str(run_id), "--yes"], db_path=db_path, services=services)


def test_unknown_run_untouched_by_cancel_and_failure(conn, version, logs):
    run_id = unknown_run(conn, logs, [make_contact(101)])
    conn.execute("INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at) "
                 "VALUES ('manual', 'a', 'b', ?, 'complete', 'c')", (version.version_id,))
    conn.execute("INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at) "
                 "VALUES (?, 101, ?, 'ok', 1, 'c')", (run_id + 1, version.version_id))
    conn.commit()

    pipeline._cancel_superseded(conn, run_id + 1, silent_log())
    pipeline._record_failure(conn, run_id, silent_log(), RuntimeError("SomeError"))
    pipeline._guarded(conn, run_id, silent_log(), lambda: (_ for _ in ()).throw(RuntimeError("x")))
    run = run_row(conn, run_id)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED


def test_rereview_excludes_tickets_of_unknown_runs(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102)])
    conn.execute("UPDATE reviews SET is_current = 0 WHERE ticket_id = 101")
    conn.commit()
    unknown = unknown_run(conn, logs, [make_contact(101)])
    fake = FakeBatches()
    run_id = rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(102)]),
                      anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                      log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert f"1 tickets excluded: held by run(s) {unknown}" in text


def test_paused_v1_run_cancelled_after_v2_rereview(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101)])
    save_version(conn, version.checks, version.reference, version.params, "v2")
    newer = rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
                     anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                     confirm=True, log_dir=logs)
    run = run_row(conn, paused)
    assert run["status"] == "cancelled" and run["error"] == f"superseded by run {newer}"
    assert len(fake.creates) == 1


def test_paused_v1_run_skips_tickets_rereviewed_under_v2(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    save_version(conn, version.checks, version.reference, version.params, "v2")
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(102)]),
             anthropic_client=client_for(fake), images_loader=lambda c, log: [], confirm=True,
             limit=1, log_dir=logs)
    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[-1]] == ["t102"]


def test_error_under_current_version_is_not_repaid_by_normal_run(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])                 # ok under v1
    save_version(conn, version.checks, version.reference, version.params, "v2")
    failing = FakeBatches()
    failing.invalid = {"t101": 2}
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
             anthropic_client=client_for(failing), images_loader=lambda c, log: [],
             log_dir=logs)                                             # error under v2, kept as history
    assert current_reviews(conn)[0]["rubric_version_id"] == version.version_id
    # Even with no current review left, the error under the run's version is not paid for again.
    conn.execute("UPDATE reviews SET is_current = 0")
    conn.commit()

    fake = FakeBatches()
    third = go(conn, logs, fake, [make_contact(101)])
    assert fake.creates == [] and run_row(conn, third)["contact_count"] == 0

    rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
             anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert len(fake.creates) == 1  # an explicit re-review does retry it


def test_first_review_error_is_not_repaid_by_normal_run(conn, version, logs):
    failing = FakeBatches()
    failing.invalid = {"t101": 2}
    go(conn, logs, failing, [make_contact(101)])
    assert current_reviews(conn)[0]["status"] == "error"
    fake = FakeBatches()
    go(conn, logs, fake, [make_contact(101)])
    assert fake.creates == []


def test_normal_run_skips_ticket_reviewed_under_older_version(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])                 # reviewed under v1
    save_version(conn, version.checks, version.reference, version.params, "v2")
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert fake.creates == []
    assert run_row(conn, run_id)["contact_count"] == 0
    [review] = current_reviews(conn)
    assert review["rubric_version_id"] == version.version_id         # past results are not re-scored


def test_build_services_disables_sdk_retries(conn, version, logs):
    """Retries are off for create only: the production client keeps the default for reads, and the
    pipeline creates every batch (first and retry) through with_options(max_retries=0)."""
    from work_assistant import credentials

    credentials.set_secret("anthropic_api_key", "sk-ant-key")
    services = cli.build_services(conn, need_zendesk=False)
    assert services.anthropic_client.max_retries == anthropic.DEFAULT_MAX_RETRIES
    assert services.anthropic_client.with_options(max_retries=0).max_retries == 0

    fake = FakeBatches()
    fake.invalid = {"t101": 1}
    go(conn, logs, fake, [make_contact(101)])
    creates = [options for name, options in fake.calls if name == "create"]
    reads = [options for name, options in fake.calls if name != "create"]
    assert creates == [{"max_retries": 0}, {"max_retries": 0}]
    assert reads and all(options == {} for options in reads)


# -- fix round 3: remaining re-submission paths ----------------------------------------------------

def test_db_error_after_accepted_create_keeps_tickets_held(conn, version, logs, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pipeline, "_record_submitted", locked)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "submission_unknown" and run["error"] == UNRECORDED
    monkeypatch.undo()

    again = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, again)["contact_count"] == 0
    assert len(fake.creates) == 1


def test_db_error_while_marking_unknown_still_ends_unknown(conn, version, logs, monkeypatch):
    real = pipeline._mark_unknown
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_mark_unknown", flaky)
    fake = FakeBatches()
    fake.create_error = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, run_id)["status"] == "submission_unknown"
    assert len(calls) == 2


def test_submit_marks_clear_4xx_failed_itself(conn, version, logs, monkeypatch):
    monkeypatch.setattr(pipeline, "_record_failure", lambda *args, **kwargs: None)
    fake = FakeBatches()
    fake.create_error = anthropic.AuthenticationError("bad key", response=api_response(401), body=None)
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and run["error"] == ANTHROPIC_KEY_REJECTED


def test_non_api_error_from_create_is_unknown(conn, version, logs):
    fake = FakeBatches()
    fake.create_error = RuntimeError("socket closed mid-upload")
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, run_id)["status"] == "submission_unknown"


def test_confirm_skips_tickets_held_by_unknown_rereview(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    paused = go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102)])
    timing_out = FakeBatches()
    timing_out.create_error = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    held_by = rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(102)]),
                       anthropic_client=client_for(timing_out), images_loader=lambda c, log: [],
                       confirm=True, limit=1, log_dir=logs)
    assert run_row(conn, held_by)["status"] == "submission_unknown"

    fake = FakeBatches()
    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]  # 101 is not paid for again
    text = (logs / f"run-{paused}.log").read_text(encoding="utf-8")
    assert f"1 tickets excluded: held by run(s) {held_by}" in text


def test_confirm_skips_tickets_held_by_submitted_rereview(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    paused = go(conn, logs, FakeBatches(), [make_contact(101)])
    stalled = FakeBatches()
    stalled.retrieve_error = Killed()
    with pytest.raises(Killed):
        rereview(conn, WINDOW, fetcher=fetching([make_contact(101)]),
                 anthropic_client=client_for(stalled), images_loader=lambda c, log: [],
                 confirm=True, log_dir=logs)
    held_by = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    assert run_row(conn, held_by)["status"] == "submitted"

    fake = FakeBatches()
    confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert fake.creates == []

    stalled.retrieve_error = None
    resume_runs(conn, anthropic_client=client_for(stalled), log_dir=logs)
    assert run_row(conn, held_by)["status"] == "complete"
    assert [(r["ticket_id"], r["run_id"]) for r in current_reviews(conn)] == [(101, held_by)]


def test_two_confirms_on_the_same_paused_run_create_once(conn, version, logs, tmp_path):
    set_setting(conn, "budget_limit_usd", 0.01)
    paused = go(conn, logs, FakeBatches(), [make_contact(101)])
    fake = FakeBatches()
    other = connect(tmp_path / "work-assistant.db")  # a second process, confirming at the same moment
    state = {"raced": False}

    def loader(contact, log):
        if not state["raced"]:
            state["raced"] = True
            confirm_run(other, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [],
                        log_dir=logs)
        return []

    try:
        confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=loader, log_dir=logs)
    finally:
        other.close()
    assert len(fake.creates) == 1
    assert run_row(conn, paused)["status"] == "complete"
    text = (logs / f"run-{paused}.log").read_text(encoding="utf-8")
    assert "not submitted; its status changed in another process first" in text


# -- Task 13a: callbacks, merges and Not counted ----------------------------------------------------


def merged_contact(ticket_id):
    from work_assistant.models import MergeInfo

    target_comments = (
        Comment(comment_id=9901, created_at="2026-10-05T10:20:00Z", author_id=7, author_name="Sam Agent",
                author_role="agent", public=False, kind="triage_macro", text="Issue Details: x\nImpact & Priority: y",
                image_urls=("https://example.invalid/t.png",)),
    )
    info = MergeInfo(ticket_id=3990, subject="Receipt printer offline", location="LOC-2201",
                     created_at="2026-10-04T14:00:00Z", comments=target_comments)
    return dataclasses.replace(make_contact(ticket_id, image_urls=("https://example.invalid/own.png",)),
                               callback_minutes=3.0, merged_into=info, summary_email_at=None)


def test_not_counted_is_stored(conn, version, logs):
    fake = FakeBatches()
    reason = '[00:12] Customer: "my problem seems to have resolved itself"'
    fake.not_counted["t102"] = reason
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    reviews = {r["ticket_id"]: r for r in current_reviews(conn)}
    assert (reviews[101]["counted"], reviews[101]["not_counted_reason"]) == (1, None)
    assert (reviews[102]["counted"], reviews[102]["not_counted_reason"]) == (0, reason)
    # The review and its checks are still stored in full.
    assert len(checks_of(conn, reviews[102]["review_id"])) == 23
    assert "resolved itself" not in (logs / f"run-{run_id}.log").read_text(encoding="utf-8")


def test_code_only_review_counts(conn, version, logs):
    checks = tuple(c for c in version.checks if c.timed is not None)
    v2 = save_version(conn, checks, version.reference, version.params, "timed only")
    go(conn, logs, FakeBatches(), [make_contact(101)])
    [review] = current_reviews(conn)
    assert review["rubric_version_id"] == v2.version_id
    assert (review["counted"], review["not_counted_reason"]) == (1, None)


def test_snapshot_round_trips_callback_and_merge():
    contact = merged_contact(101)
    assert contact_from_snapshot(pipeline.contact_snapshot(contact)) == contact
    answered = dataclasses.replace(contact, answered_by_user_id=900)
    assert contact_from_snapshot(pipeline.contact_snapshot(answered)).answered_by_user_id == 900


def test_old_snapshot_without_new_fields_loads():
    data = json.loads(pipeline.contact_snapshot(make_contact(101)))
    del data["callback_minutes"], data["merged_into"], data["answered_by_user_id"]
    restored = contact_from_snapshot(json.dumps(data))
    assert restored == make_contact(101)
    assert restored.callback_minutes is None and restored.merged_into is None and restored.answered_by_user_id is None


def test_image_urls_include_merged_comments_after_own():
    assert pipeline.image_urls(merged_contact(101)) == [
        "https://example.invalid/own.png", "https://example.invalid/t.png",
    ]


def test_merged_contact_request_carries_merge_and_callback(conn, version, logs):
    fake = FakeBatches()
    go(conn, logs, fake, [merged_contact(101)])
    [request] = fake.creates[0]
    facts, ticket = (b["text"] for b in request["params"]["messages"][0]["content"][:2])
    assert "started 3.0 minutes after the call ended" in facts
    assert "ticket 3990" in facts
    assert "<merged_comment " in ticket
    assert "pr-email-sent" in schema_check_ids(request)  # left to the model after a merge


# -- final review: stored facts, fresh re-review data, errored-review report -----------------------


def contact_row(conn, ticket_id):
    return conn.execute("SELECT * FROM contacts WHERE ticket_id = ?", (ticket_id,)).fetchone()


def changed(ticket_id):
    """The same call fetched again later, with different facts."""
    return dataclasses.replace(make_contact(ticket_id), agent_email="other@example.com", attribution_ok=False,
                               callback_minutes=4.0)


def test_normal_run_keeps_stored_facts_of_reviewed_contact(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])
    conn.execute("UPDATE contacts SET fetched_at = '2026-10-01T00:00:00Z'")
    conn.commit()
    before = contact_row(conn, 101)

    go(conn, logs, FakeBatches(), [changed(101), changed(102)])
    after = contact_row(conn, 101)
    assert after["attribution_ok"] == before["attribution_ok"] == 1
    assert after["agent_email"] == "sam@example.com"
    assert after["snapshot"] == before["snapshot"]
    assert after["fetched_at"] != "2026-10-01T00:00:00Z"  # only the fetch time moves
    new = contact_row(conn, 102)                           # a contact with no review is stored as fetched
    assert new["attribution_ok"] == 0 and contact_from_snapshot(new["snapshot"]) == changed(102)


def test_rereview_uses_fresh_ticket_data(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102)])
    fake = FakeBatches()
    windows = []

    def fetcher(window, log):
        windows.append(window)
        return [changed(101), changed(102)]

    rereview(conn, WINDOW, fetcher=fetcher, anthropic_client=client_for(fake), images_loader=lambda c, log: [],
             limit=1, log_dir=logs)
    assert windows == [WINDOW]
    [request] = fake.creates[0]
    assert request["custom_id"] == "t101"
    facts = request["params"]["messages"][0]["content"][0]["text"]
    assert "started 4.0 minutes after the call ended" in facts   # data the old snapshot lacked

    refreshed = contact_row(conn, 101)                          # re-reviewed: its stored facts are refreshed
    assert contact_from_snapshot(refreshed["snapshot"]) == changed(101)
    assert refreshed["attribution_ok"] == 0 and refreshed["agent_email"] == "other@example.com"
    untouched = contact_row(conn, 102)                          # not re-reviewed (limit): kept as reviewed
    assert contact_from_snapshot(untouched["snapshot"]) == make_contact(102)
    assert untouched["attribution_ok"] == 1


def test_rereview_paused_then_confirmed_uses_fresh_data(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = rereview(conn, WINDOW, fetcher=fetching([changed(101)]), anthropic_client=client_for(fake),
                      images_loader=lambda c, log: [], log_dir=logs)
    assert run_row(conn, run_id)["status"] == "awaiting_confirmation"
    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    facts = fake.creates[0][0]["params"]["messages"][0]["content"][0]["text"]
    assert "started 4.0 minutes after the call ended" in facts


def test_rereview_of_ticket_no_longer_in_zendesk_uses_stored_snapshot(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102)])
    before = contact_row(conn, 101)
    fake = FakeBatches()
    run_id = rereview(conn, WINDOW, fetcher=fetching([make_contact(102)]), anthropic_client=client_for(fake),
                      images_loader=lambda c, log: [], log_dir=logs)
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t102"]
    assert run_row(conn, run_id)["contact_count"] == 2
    assert contact_row(conn, 101)["snapshot"] == before["snapshot"]
    assert contact_row(conn, 101)["fetched_at"] == before["fetched_at"]
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "ticket 101: no longer returned by Zendesk; using its stored data" in text


def test_rereview_includes_newly_fetched_call_in_window(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])
    fake = FakeBatches()
    rereview(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(103)]),
             anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t103"]
    assert contact_from_snapshot(contact_row(conn, 103)["snapshot"]) == make_contact(103)


def test_cli_report_counts_errored_reviews(conn, version, logs, tmp_path, capsys):
    fake = FakeBatches()
    fake.invalid = {"t101": 2, "t102": 2}
    code = cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db",
                    services=services_for(fake, logs, [make_contact(101), make_contact(102), make_contact(103)]))
    out = capsys.readouterr().out
    assert code == 0
    assert "2 calls could not be reviewed (see work-assistant show --run 1)" in out

    fake = FakeBatches()
    fake.invalid = {"t104": 2}
    cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db",
             services=services_for(fake, logs, [make_contact(104)]))
    assert "1 call could not be reviewed (see work-assistant show --run 2)" in capsys.readouterr().out


def test_cli_report_is_silent_without_errored_reviews(conn, version, logs, tmp_path, capsys):
    cli.main(["run", "--last-week"], db_path=tmp_path / "work-assistant.db",
             services=services_for(FakeBatches(), logs, [make_contact(101)]))
    assert "could not be reviewed" not in capsys.readouterr().out


# -- estimate without running (Task 14: the app's Run now shows this first) ------------------------


def test_estimate_window_matches_the_run_estimate_and_stores_nothing(conn, version, logs):
    contacts = [make_contact(101, image_urls=("https://acme.zendesk.com/a.png",)), make_contact(202)]
    estimate = pipeline.estimate_window(conn, WINDOW, fetcher=fetching(contacts), rereview=False)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert estimate.contacts == 2 and estimate.excluded_purged == 0
    run_id = go(conn, logs, FakeBatches(), contacts, images=lambda c, log: [(b"x", "image/png")] * len(
        pipeline.image_urls(c)))
    assert estimate.estimate_usd == run_row(conn, run_id)["estimate_usd"]


def test_estimate_window_skips_reviewed_and_held_tickets(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101)])
    estimate = pipeline.estimate_window(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(202)]),
                                        rereview=False)
    assert estimate.contacts == 1
    rereview_estimate = pipeline.estimate_window(
        conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(202)]), rereview=True)
    assert rereview_estimate.contacts == 2


def test_estimate_window_rereview_excludes_purged(conn, version, logs):
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(202)])
    conn.execute("UPDATE contacts SET purged_at = '2026-10-16T00:00:00Z', snapshot = '{}' WHERE ticket_id = 101")
    conn.commit()
    estimate = pipeline.estimate_window(conn, WINDOW, fetcher=fetching([make_contact(101), make_contact(202)]),
                                        rereview=True)
    assert (estimate.contacts, estimate.excluded_purged) == (1, 1)


def test_run_records_its_process_id(conn, version, logs):
    import os

    run_id = go(conn, logs, FakeBatches(), [make_contact(101)])
    assert run_row(conn, run_id)["pid"] == os.getpid()


# -- the driving process (Task 14 fix: never collect a batch a live process is still polling) -------

OTHER_PID = 424242


def _interrupted_submitted(conn, logs, fake):
    fake.retrieve_error = Killed()
    with pytest.raises(Killed):
        go(conn, logs, fake, [make_contact(101)])
    fake.retrieve_error = None
    return conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]


def test_submit_records_the_driving_process(conn, version, logs):
    import os

    run_id = _interrupted_submitted(conn, logs, FakeBatches())
    assert run_row(conn, run_id)["pid"] == os.getpid()


def test_resume_skips_a_run_another_live_process_is_collecting(conn, version, logs, monkeypatch):
    fake = FakeBatches()
    run_id = _interrupted_submitted(conn, logs, fake)
    conn.execute("UPDATE runs SET pid = ? WHERE run_id = ?", (OTHER_PID, run_id))
    conn.commit()
    monkeypatch.setattr(pipeline, "pid_alive", lambda pid: pid == OTHER_PID)
    client = client_for(fake)
    calls_before = len(fake.calls)
    assert resume_runs(conn, anthropic_client=client, log_dir=logs) == []
    assert len(fake.calls) == calls_before  # its batch was not touched
    assert (run_row(conn, run_id)["status"], run_row(conn, run_id)["pid"]) == ("submitted", OTHER_PID)
    assert "still collecting it" in (logs / f"run-{run_id}.log").read_text(encoding="utf-8")


def test_resume_takes_a_run_whose_process_died_and_records_itself(conn, version, logs, monkeypatch):
    import os

    fake = FakeBatches()
    run_id = _interrupted_submitted(conn, logs, fake)
    conn.execute("UPDATE runs SET pid = ? WHERE run_id = ?", (OTHER_PID, run_id))
    conn.commit()
    monkeypatch.setattr(pipeline, "pid_alive", lambda pid: False)
    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [run_id]
    assert (run_row(conn, run_id)["status"], run_row(conn, run_id)["pid"]) == ("complete", os.getpid())


def test_settle_leaves_a_live_process_submitting(conn, version, logs, monkeypatch):
    run_id = _interrupted_submitted(conn, logs, FakeBatches())
    conn.execute("UPDATE runs SET status = 'submitting', batch_id = NULL, pid = ? WHERE run_id = ?",
                 (OTHER_PID, run_id))
    conn.commit()
    monkeypatch.setattr(pipeline, "pid_alive", lambda pid: pid == OTHER_PID)
    assert resume_runs(conn, anthropic_client=client_for(FakeBatches()), log_dir=logs) == []
    assert run_row(conn, run_id)["status"] == "submitting"
    monkeypatch.setattr(pipeline, "pid_alive", lambda pid: False)
    assert resume_runs(conn, anthropic_client=client_for(FakeBatches()), log_dir=logs) == [run_id]
    assert run_row(conn, run_id)["status"] == "submission_unknown"


def test_confirm_records_the_confirming_process(conn, version, logs):
    import os

    set_setting(conn, "budget_limit_usd", 0.0001)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, run_id)["status"] == "awaiting_confirmation"
    conn.execute("UPDATE runs SET pid = ? WHERE run_id = ?", (OTHER_PID, run_id))
    conn.commit()
    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert (run_row(conn, run_id)["status"], run_row(conn, run_id)["pid"]) == ("complete", os.getpid())


# -- dropped calls are exempt from the 10-minute rule ---------------------------------------------

DROPPED_EVIDENCE = "Dropped call: the 10-minute rule doesn't apply; the callback is checked instead."


def run_with_dropped(conn, logs, answer, **contact_kwargs):
    fake = FakeBatches()
    fake.dropped["t101"] = answer
    go(conn, logs, fake, [make_contact(101, duration_seconds=720, **contact_kwargs)])
    review = current_reviews(conn)[0]
    return checks_of(conn, review["review_id"])


def test_dropped_call_over_ten_minutes_is_na(conn, version, logs):
    checks = run_with_dropped(conn, logs, "yes")
    assert checks["pr-call-length"]["outcome"] == "na"
    assert checks["pr-call-length"]["decided_by"] == "code"
    assert checks["pr-call-length"]["evidence"] == DROPPED_EVIDENCE


def test_not_dropped_call_over_ten_minutes_stays_missed(conn, version, logs):
    checks = run_with_dropped(conn, logs, "no")
    assert checks["pr-call-length"]["outcome"] == "missed"
    assert checks["pr-call-length"]["decided_by"] == "code"
    assert checks["pr-call-length"]["evidence"] == "Call lasted 12.0 min (limit 10)."


def test_unclear_drop_keeps_the_code_decision(conn, version, logs):
    checks = run_with_dropped(conn, logs, "unclear")
    assert checks["pr-call-length"]["outcome"] == "missed"
    assert checks["pr-call-length"]["evidence"] == "Call lasted 12.0 min (limit 10)."


def test_dropped_answer_does_not_touch_a_model_decided_call_length(conn, version, logs):
    fake = FakeBatches()
    fake.dropped["t101"] = "yes"
    go(conn, logs, fake, [make_contact(101, priority="urgent", duration_seconds=720)])
    checks = checks_of(conn, current_reviews(conn)[0]["review_id"])
    assert checks["pr-call-length"]["decided_by"] == "model"
    assert checks["pr-call-length"]["evidence"] == "model evidence"


@pytest.mark.parametrize("answer", ["yes", "no", "unclear"])
def test_call_dropped_is_stored(conn, version, logs, answer):
    run_with_dropped(conn, logs, answer)
    assert current_reviews(conn)[0]["call_dropped"] == answer


def test_call_dropped_not_recorded_without_a_judgment(conn, version, logs):
    fake = FakeBatches()
    fake.invalid["t101"] = 2   # both attempts fail: a review error, so the model judged nothing
    go(conn, logs, fake, [make_contact(101)])
    review = current_reviews(conn)[0]
    assert (review["status"], review["call_dropped"]) == ("error", None)


# -- the reviewing model (Task 3, T-033) -----------------------------------------------------------


def test_run_records_model_and_effort(conn, version, logs, monkeypatch):
    set_setting(conn, "model", SONNET)
    set_setting(conn, "effort", "high")
    seen = {}
    real = pipeline.estimate_cost

    def spy(system_chars, contact_chars, image_counts, model_id, output_tokens):
        seen.update(model=model_id, output=output_tokens)
        return real(system_chars, contact_chars, image_counts, model_id, output_tokens)

    monkeypatch.setattr(pipeline, "estimate_cost", spy)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    run = run_row(conn, run_id)
    assert (run["model"], run["effort"]) == (SONNET, "high")
    assert seen == {"model": SONNET, "output": 10000}      # Sonnet's default until it has 10 reviews
    assert all(r["params"]["model"] == SONNET and r["params"]["output_config"]["effort"] == "high"
               for r in fake.creates[0])
    assert run["actual_usd"] == pytest.approx(2 * actual_cost(USAGE, SONNET))
    reviews = current_reviews(conn)
    assert [(r["model"], r["effort"], r["output_tokens"]) for r in reviews] == [(SONNET, "high", 500)] * 2


def test_estimate_learns_output_tokens_per_model(conn, version, logs, monkeypatch):
    go(conn, logs, FakeBatches(), [make_contact(100 + i) for i in range(10)])   # 10 ok reviews at 500 tokens
    seen = []
    real = pipeline.estimate_cost

    def spy(system_chars, contact_chars, image_counts, model_id, output_tokens):
        seen.append((model_id, output_tokens))
        return real(system_chars, contact_chars, image_counts, model_id, output_tokens)

    monkeypatch.setattr(pipeline, "estimate_cost", spy)
    go(conn, logs, FakeBatches(), [make_contact(201)])
    pipeline.estimate_window(conn, WINDOW, fetcher=fetching([make_contact(204)]), rereview=False)
    set_setting(conn, "effort", "max")
    go(conn, logs, FakeBatches(), [make_contact(205)])
    pipeline.estimate_window(conn, WINDOW, fetcher=fetching([make_contact(206)]), rereview=False)
    set_setting(conn, "effort", "medium")
    set_setting(conn, "model", SONNET)
    go(conn, logs, FakeBatches(), [make_contact(202)])
    pipeline.estimate_window(conn, WINDOW, fetcher=fetching([make_contact(203)]), rereview=False)
    # 500 x 1.25 once Opus has 10 at medium; at max nothing is measured yet, so Opus's default.
    assert seen == [(OPUS, 625), (OPUS, 625), (OPUS, 6000), (OPUS, 6000), (SONNET, 10000), (SONNET, 10000)]


def test_confirm_uses_run_model_not_current_setting(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert run_row(conn, run_id)["status"] == "awaiting_confirmation"
    set_setting(conn, "model", SONNET)
    set_setting(conn, "effort", "max")

    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert all(r["params"]["model"] == OPUS and r["params"]["output_config"]["effort"] == "medium"
               for r in fake.creates[0])
    assert run["actual_usd"] == pytest.approx(2 * ONE_RESULT_COST)
    assert {(r["model"], r["effort"]) for r in current_reviews(conn)} == {(OPUS, "medium")}


def test_resume_uses_run_model_not_current_setting(conn, version, logs):
    fake = FakeBatches()
    run_id = _interrupted_submitted(conn, logs, fake)
    set_setting(conn, "model", SONNET)
    set_setting(conn, "effort", "max")

    assert resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs) == [run_id]
    run = run_row(conn, run_id)
    assert run["status"] == "complete"
    assert run["actual_usd"] == pytest.approx(ONE_RESULT_COST)    # costed at Opus rates
    [review] = current_reviews(conn)
    assert (review["model"], review["effort"], review["output_tokens"]) == (OPUS, "medium", 500)


def test_run_stored_before_models_confirms_on_opus_at_the_current_effort(conn, version, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = go(conn, logs, fake, [make_contact(101)])
    conn.execute("UPDATE runs SET model = NULL, effort = NULL WHERE run_id = ?", (run_id,))
    conn.commit()
    set_setting(conn, "model", SONNET)
    set_setting(conn, "effort", "high")

    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    [request] = fake.creates[0]
    assert (request["params"]["model"], request["params"]["output_config"]["effort"]) == (OPUS, "high")
    run = run_row(conn, run_id)
    assert (run["model"], run["effort"]) == (OPUS, "high")      # recorded, so a resume would match
    assert (current_reviews(conn)[0]["model"], current_reviews(conn)[0]["effort"]) == (OPUS, "high")


def test_run_submitted_before_models_resumes_on_opus_with_effort_not_recorded(conn, version, logs):
    fake = FakeBatches()
    run_id = _interrupted_submitted(conn, logs, fake)
    conn.execute("UPDATE runs SET model = NULL, effort = NULL WHERE run_id = ?", (run_id,))
    conn.commit()
    set_setting(conn, "model", SONNET)

    resume_runs(conn, anthropic_client=client_for(fake), log_dir=logs)
    assert run_row(conn, run_id)["actual_usd"] == pytest.approx(ONE_RESULT_COST)
    [review] = current_reviews(conn)
    assert (review["model"], review["effort"]) == (OPUS, None)   # its requests were built before effort was kept


# -- cancelling a run waiting for your OK (final review) ---------------------------------------------


def test_cancel_paused_run_releases_its_tickets(conn, version, logs):
    """A run the budget guard paused can be cancelled: nothing was sent, it becomes `cancelled`, and the
    tickets it held are free for the next run."""
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    paused = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert run_row(conn, paused)["status"] == "awaiting_confirmation"
    set_setting(conn, "budget_limit_usd", 1000)
    blocked = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert run_row(conn, blocked)["contact_count"] == 0, "the paused run holds its tickets"

    pipeline.cancel_run(conn, paused, log_dir=logs)
    run = run_row(conn, paused)
    assert (run["status"], run["error"], run["batch_id"]) == ("cancelled", pipeline.CANCELLED_BY_OWNER, None)
    assert run["finished_at"] is not None
    assert pipeline.CANCELLED_BY_OWNER == "Cancelled by you before anything was sent."
    assert pipeline.CANCELLED_BY_OWNER in (logs / f"run-{paused}.log").read_text(encoding="utf-8")
    assert fake.creates == []
    assert pipeline._held_tickets(conn, 0, pipeline.HOLDING_STATUSES) == {}

    again = go(conn, logs, fake, [make_contact(101), make_contact(102)])
    assert (run_row(conn, again)["status"], run_row(conn, again)["contact_count"]) == ("complete", 2)
    assert sorted(r["custom_id"] for r in fake.creates[0]) == ["t101", "t102"]
    with pytest.raises(ValueError, match="not awaiting confirmation"):
        confirm_run(conn, paused, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)


@pytest.mark.parametrize("status", ["fetching", "submitting", "submitted", "submission_unknown", "complete",
                                    "failed", "cancelled"])
def test_only_a_run_waiting_for_your_ok_can_be_cancelled(conn, version, logs, status):
    from factories import add_run

    run_id = add_run(conn, status=status, error="kept")
    with pytest.raises(ValueError, match=r"^Only a run waiting for your OK can be cancelled"):
        pipeline.cancel_run(conn, run_id, log_dir=logs)
    assert (run_row(conn, run_id)["status"], run_row(conn, run_id)["error"]) == (status, "kept")
    with pytest.raises(ValueError, match="does not exist"):
        pipeline.cancel_run(conn, run_id + 1, log_dir=logs)


def test_cli_cancel_run(conn, version, logs, tmp_path, capsys):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    services = services_for(fake, logs, [make_contact(101)])
    db_path = tmp_path / "work-assistant.db"
    assert cli.main(["run", "--last-week"], db_path=db_path, services=services) == 0
    assert "work-assistant cancel-run 1" in capsys.readouterr().out

    assert cli.main(["cancel-run", "1"], db_path=db_path, services=services) == 0
    assert capsys.readouterr().out == ("Run 1 cancelled. Nothing was sent or charged, and its calls are free for "
                                       "later runs.\n")
    assert run_row(conn, 1)["status"] == "cancelled" and fake.creates == []
    with pytest.raises(SystemExit, match="Only a run waiting for your OK can be cancelled"):
        cli.main(["cancel-run", "1"], db_path=db_path, services=services)
    with pytest.raises(SystemExit, match="does not exist"):
        cli.main(["cancel-run", "9"], db_path=db_path, services=services)


# -- Fable 5.1 and data retention (final review) ----------------------------------------------------


def test_fable_batch_rejected_as_bad_request_says_why(conn, version, logs):
    """A 400 from batches.create proves no batch was created, so the run fails (its tickets are free). On
    Fable 5.1 the run's message adds the likely reason: its 30-day data retention."""
    bad = anthropic.BadRequestError("zero data retention: org-secret-detail", response=api_response(400), body=None)
    set_setting(conn, "model", "claude-fable-5-1")
    fake = FakeBatches()
    fake.create_error = bad
    run_id = go(conn, logs, fake, [make_contact(101)])
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and run["finished_at"] is not None
    assert run["error"] == pipeline.FABLE_BATCH_REJECTED
    assert run["error"].endswith("Claude Fable 5.1 needs 30-day data retention, which your Anthropic organization "
                                 "may not allow.")
    assert "org-secret-detail" not in run["error"], "the exception's own text never reaches the run"

    set_setting(conn, "model", SONNET)
    other = go(conn, logs, fake, [make_contact(101)])
    assert run_row(conn, other)["error"] == "BadRequestError", "other models keep the plain class name"


# -- Review again: chosen calls by ticket id (T-045) -----------------------------------------------


def by_ids(contacts, seen=None):
    """A fetcher for chosen tickets: returns the given contacts among the ids asked for, recording each ask."""
    def fetch(ticket_ids, log):
        ids = list(ticket_ids)
        if seen is not None:
            seen.append(ids)
        return [c for c in contacts if c.ticket_id in ids]
    return fetch


def again(conn, logs, fake, ticket_ids, fetcher, **kwargs):
    return pipeline.rereview_tickets(conn, ticket_ids, fetcher_for_ids=fetcher, anthropic_client=client_for(fake),
                                     images_loader=lambda c, log: [], log_dir=logs, **kwargs)


THURSDAY = "2026-10-08T15:00:00Z"


@pytest.fixture
def three_calls(conn, version, logs):
    """Calls 101 and 102 on Mon Oct 5 and 103 on Thu Oct 8 (Chicago), reviewed by run 1."""
    set_setting(conn, "timezone", CHI)
    go(conn, logs, FakeBatches(), [make_contact(101), make_contact(102),
                                   make_contact(103, call_started_at=THURSDAY)])
    return conn


def test_rereview_tickets_fetches_only_those_and_replaces_the_shown_result(three_calls, conn, logs):
    old = {r["ticket_id"]: r for r in current_reviews(conn)}
    conn.execute("UPDATE check_results SET override_outcome = 'missed', override_note = 'disagree', "
                 "override_at = '2026-10-12T00:00:00Z' WHERE review_id = ? AND check_id = 'sc06'",
                 (old[101]["review_id"],))
    conn.commit()
    seen = []
    fake = FakeBatches()
    run_id = again(conn, logs, fake, [101], by_ids([changed(101), changed(102)], seen))

    assert seen == [[101]], "only the chosen call is read from Zendesk"
    run = run_row(conn, run_id)
    assert (run["kind"], run["status"], run["contact_count"]) == ("rereview", "complete", 1)
    assert run["rubric_version_id"] == current_version(conn).version_id and run["model"] == OPUS
    assert [r["custom_id"] for r in fake.creates[0]] == ["t101"]
    new = {r["ticket_id"]: r for r in current_reviews(conn)}
    assert new[101]["run_id"] == run_id and new[101]["review_id"] != old[101]["review_id"]
    assert new[102]["review_id"] == old[102]["review_id"] and new[103]["review_id"] == old[103]["review_id"]
    checks = checks_of(conn, new[101]["review_id"])
    assert checks["sc06"]["needs_recheck"] == 1 and checks["sc06"]["override_outcome"] is None
    assert checks_of(conn, old[101]["review_id"])["sc06"]["override_outcome"] == "missed", "kept for reference"
    assert contact_from_snapshot(contact_row(conn, 101)["snapshot"]) == changed(101), "fresh data stored"
    assert contact_from_snapshot(contact_row(conn, 102)["snapshot"]) == make_contact(102)


def test_rereview_tickets_window_spans_the_chosen_days(three_calls, conn, logs):
    run_id = again(conn, logs, FakeBatches(), [103, 101],
                   by_ids([make_contact(101), make_contact(103, call_started_at=THURSDAY)]))
    run = run_row(conn, run_id)
    # Local midnight of Mon Oct 5 to the midnight after Thu Oct 8, in Chicago (UTC-5).
    assert (run["window_start"], run["window_end"]) == ("2026-10-05T05:00:00Z", "2026-10-09T05:00:00Z")


def test_rereview_tickets_skips_purged_and_held_calls(three_calls, conn, logs):
    conn.execute("UPDATE contacts SET purged_at = '2026-10-12T00:00:00Z' WHERE ticket_id = 101")
    holder = conn.execute(
        "INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, created_at, batch_id) "
        "VALUES ('manual', 'a', 'b', 1, 'submitted', 'c', 'batch_x')").lastrowid
    conn.execute("INSERT INTO run_contacts (run_id, ticket_id) VALUES (?, 102)", (holder,))
    conn.commit()
    seen = []
    fake = FakeBatches()
    run_id = again(conn, logs, fake, [101, 102, 103], by_ids([make_contact(103, call_started_at=THURSDAY)], seen))
    assert seen == [[103]], "erased and held calls are not read again"
    run = run_row(conn, run_id)
    assert (run["contact_count"], run["excluded_purged"]) == (1, 1)
    assert [r["custom_id"] for r in fake.creates[0]] == ["t103"]
    text = (logs / f"run-{run_id}.log").read_text(encoding="utf-8")
    assert "1 tickets excluded: held by run(s) 2" in text and "1 purged calls excluded" in text

    estimate = pipeline.estimate_tickets(conn, [101, 102, 103],
                                         fetcher_for_ids=by_ids([make_contact(103, call_started_at=THURSDAY)]))
    assert (estimate.contacts, estimate.excluded_purged, estimate.excluded_held) == (1, 1, 1)


def test_rereview_tickets_goes_through_the_budget_guard(three_calls, conn, logs):
    set_setting(conn, "budget_limit_usd", 0.01)
    fake = FakeBatches()
    run_id = again(conn, logs, fake, [101, 102], by_ids([make_contact(101), make_contact(102)]))
    assert run_row(conn, run_id)["status"] == "awaiting_confirmation" and fake.creates == []
    confirm_run(conn, run_id, anthropic_client=client_for(fake), images_loader=lambda c, log: [], log_dir=logs)
    assert run_row(conn, run_id)["status"] == "complete" and len(fake.creates[0]) == 2


def test_rereview_tickets_uses_stored_data_when_zendesk_no_longer_returns_the_call(three_calls, conn, logs):
    fake = FakeBatches()
    run_id = again(conn, logs, fake, [102], by_ids([]))
    assert [r["custom_id"] for r in fake.creates[0]] == ["t102"]
    assert "ticket 102: no longer returned by Zendesk; using its stored data" in \
        (logs / f"run-{run_id}.log").read_text(encoding="utf-8")


def test_rereview_tickets_needs_a_stored_call(three_calls, conn, logs):
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    with pytest.raises(ValueError, match="None of these calls"):
        again(conn, logs, FakeBatches(), [999], by_ids([]))
    with pytest.raises(ValueError, match="None of these calls"):
        pipeline.estimate_tickets(conn, [], fetcher_for_ids=by_ids([]))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs, "no run is created"


def test_estimate_tickets_matches_the_run_and_stores_nothing(three_calls, conn, logs):
    later = make_contact(103, call_started_at=THURSDAY)
    estimate = pipeline.estimate_tickets(conn, [101, 103], fetcher_for_ids=by_ids([changed(101), later]))
    assert estimate.contacts == 2 and estimate.excluded_purged == 0 and estimate.excluded_held == 0
    assert estimate.estimate_usd > 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert contact_from_snapshot(contact_row(conn, 101)["snapshot"]) == make_contact(101), "nothing stored"
    run_id = again(conn, logs, FakeBatches(), [101, 103], by_ids([changed(101), later]))
    assert run_row(conn, run_id)["estimate_usd"] == pytest.approx(estimate.estimate_usd)


def ids_services(fake, logs, contacts, seen=None):
    return cli.Services(anthropic_client=client_for(fake), ids_fetcher=by_ids(contacts, seen),
                        images_loader=lambda c, log: [], log_dir=logs)


def test_cli_rereview_tickets(three_calls, conn, logs, tmp_path, capsys):
    seen = []
    fake = FakeBatches()
    code = cli.main(["run", "--rereview", "--tickets", "103,101"], db_path=tmp_path / "work-assistant.db",
                    services=ids_services(fake, logs, [make_contact(101), make_contact(103, call_started_at=THURSDAY)],
                                          seen))
    assert code == 0
    assert seen == [[101, 103]]
    assert "Run 2: complete. Contacts: 2." in capsys.readouterr().out
    assert run_row(conn, 2)["kind"] == "rereview"


def test_cli_rereview_tickets_file_is_read_then_deleted(three_calls, conn, logs, tmp_path):
    ids_file = logs / "review-again-x.txt"
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text("101\n102\n", encoding="utf-8")
    seen = []
    cli.main(["run", "--rereview", "--tickets-file", str(ids_file)], db_path=tmp_path / "work-assistant.db",
             services=ids_services(FakeBatches(), logs, [make_contact(101), make_contact(102)], seen))
    assert seen == [[101, 102]]
    assert not ids_file.exists()


@pytest.mark.parametrize("argv, message", [
    (["run", "--tickets", "101"], "--tickets needs --rereview"),
    (["run", "--rereview", "--tickets", "101", "--last-week"], "takes no period"),
    (["run", "--rereview", "--tickets", "1x"], "ticket numbers"),
    (["run", "--rereview", "--tickets", "101", "--tickets-file", "f"], "not both"),
])
def test_cli_rereview_tickets_rejects_bad_arguments(three_calls, logs, tmp_path, argv, message):
    with pytest.raises(SystemExit, match=message):
        cli.main(argv, db_path=tmp_path / "work-assistant.db", services=ids_services(FakeBatches(), logs, []))
