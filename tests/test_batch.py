"""Batch submission and collection, against a fake client. No network."""
import json
from types import SimpleNamespace as NS

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, Usage
from anthropic.types.messages import (
    MessageBatchCanceledResult,
    MessageBatchErroredResult,
    MessageBatchExpiredResult,
    MessageBatchIndividualResponse,
    MessageBatchSucceededResult,
)
from anthropic.types.refusal_stop_details import RefusalStopDetails
from anthropic.types.shared import APIErrorObject, ErrorResponse, InvalidRequestError, OverloadedError

from work_assistant.costs import actual_cost
from work_assistant.reviewer.batch import ReviewOutcome, build_request, collect, submit, wait

IDS = ["c1", "c2"]
OPUS, SONNET, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"


class FakeBatches:
    def __init__(self, statuses=("ended",), results=()):
        self.statuses = list(statuses)
        self._results = list(results)
        self.created = None
        self.retrieved = 0

    def create(self, requests):
        self.created = requests
        return NS(id="batch_1")

    def retrieve(self, batch_id):
        self.retrieved += 1
        return NS(processing_status=self.statuses.pop(0))

    def results(self, batch_id):
        return iter(self._results)


class FakeClient:
    """Mimics anthropic.Anthropic's `with_options`; records the options create was called through."""

    def __init__(self, batches, options=None):
        self.options = dict(options or {})
        self._batches = batches
        self.messages = NS(batches=batches)

    def with_options(self, **options):
        merged = {**self.options, **options}
        self._batches.create_options = merged
        return FakeClient(self._batches, merged)


def fake_client(**kwargs):
    batches = FakeBatches(**kwargs)
    return FakeClient(batches), batches


def good_text(counted="yes", reason=""):
    return json.dumps({
        "resolved_on_call": "yes",
        "call_dropped": "no",
        "counted": counted,
        "not_counted_reason": reason,
        "checks": [
            {"check_id": "c1", "outcome": "met", "evidence": "said hello"},
            {"check_id": "c2", "outcome": "na", "evidence": "not needed"},
        ],
    })


def usage(**kw):
    base = dict(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=None)
    base.update(kw)
    return Usage.model_construct(**base)


def succeeded(custom_id, text=None, stop_reason="end_turn", category=None, with_thinking=True, use=None):
    """A succeeded result built from the SDK's own models."""
    content = []
    if with_thinking:
        content.append(ThinkingBlock.model_construct(type="thinking", thinking="hmm", signature="sig"))
    content.append(TextBlock.model_construct(type="text", text=good_text() if text is None else text, citations=None))
    details = RefusalStopDetails.model_construct(type="refusal", category=category, explanation=None) if category else None
    message = Message.model_construct(
        id="msg_1", type="message", role="assistant", model="claude-opus-5-5", content=content,
        stop_reason=stop_reason, stop_details=details, stop_sequence=None,
        usage=use if use is not None else usage(),
    )
    return MessageBatchIndividualResponse.model_construct(
        custom_id=custom_id, result=MessageBatchSucceededResult.model_construct(type="succeeded", message=message))


def errored(custom_id, inner):
    """An errored result in the real shape: result.error is an ErrorResponse whose .error holds the type."""
    return MessageBatchIndividualResponse.model_construct(
        custom_id=custom_id,
        result=MessageBatchErroredResult.model_construct(
            type="errored", error=ErrorResponse.model_construct(type="error", request_id="req_1", error=inner)),
    )


def ended(custom_id, kind):
    cls = MessageBatchExpiredResult if kind == "expired" else MessageBatchCanceledResult
    return MessageBatchIndividualResponse.model_construct(custom_id=custom_id, result=cls.model_construct(type=kind))


def test_build_request_params():
    req = build_request("t1", [{"type": "text", "text": "sys"}], [{"type": "text", "text": "hi"}], IDS, "medium", OPUS)
    assert req["custom_id"] == "t1"
    params = req["params"]
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 16000
    assert params["output_config"]["effort"] == "medium"
    fmt = params["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["checks"]["items"]["properties"]["check_id"]["enum"] == IDS
    assert "thinking" not in params
    assert "fallbacks" not in params
    assert params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_submit_returns_batch_id():
    client, batches = fake_client()
    assert submit(client, ["r1", "r2"]) == "batch_1"
    assert batches.created == ["r1", "r2"]
    assert batches.create_options == {"max_retries": 0}


def _counting_sdk_client():
    """A real anthropic client over a mock transport that always answers 500 (no network)."""
    import anthropic
    import httpx2  # the HTTP library the anthropic SDK is built on

    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx2.Response(500, headers={"retry-after-ms": "1"},
                               json={"type": "error", "error": {"type": "api_error", "message": "boom"}})

    client = anthropic.Anthropic(api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return client, calls


def test_submit_never_retries_create_but_reads_keep_retries():
    import anthropic

    client, calls = _counting_sdk_client()
    assert client.max_retries == anthropic.DEFAULT_MAX_RETRIES > 0
    with pytest.raises(anthropic.InternalServerError):
        submit(client, [])
    assert calls == ["/v1/messages/batches"]  # one attempt: a retried create could pay twice

    calls.clear()
    with pytest.raises(anthropic.InternalServerError):
        wait(client, "b1", sleep=lambda s: None)
    assert len(calls) == 1 + anthropic.DEFAULT_MAX_RETRIES  # retrieve keeps the default retries


def test_collect_success():
    client, _ = fake_client(results=[succeeded("t1")])
    out = collect(client, "batch_1", {"t1": IDS}, OPUS)
    r = out["t1"]
    assert r.ok and r.error is None and not r.retryable
    assert r.outcomes == {"c1": ("met", "said hello"), "c2": ("na", "not needed")}
    assert r.resolved_on_call == "yes"
    assert r.counted is True and r.not_counted_reason is None
    assert r.cost_usd == pytest.approx(actual_cost(
        {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0},
        OPUS))
    assert r.cost_usd > 0
    assert r.output_tokens == 500


def test_build_request_uses_model():
    req = build_request("t1", [{"type": "text", "text": "sys"}], [{"type": "text", "text": "hi"}], IDS, "high",
                        SONNET)
    params = req["params"]
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"]["effort"] == "high"
    assert set(params) == {"model", "max_tokens", "system", "messages", "output_config"}


def test_collect_costs_at_the_model_rates():
    client, _ = fake_client(results=[succeeded("t1")])
    r = collect(client, "batch_1", {"t1": IDS}, FABLE)["t1"]
    assert r.cost_usd == pytest.approx(actual_cost(
        {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000}, FABLE))


def test_collect_failures_keep_output_tokens_when_usage_present():
    client, _ = fake_client(results=[succeeded("t1", text="not json", use=usage(output_tokens=700))])
    assert collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"].output_tokens == 700
    client, _ = fake_client(results=[])
    assert collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"].output_tokens is None


def test_collect_ok_without_usage_records_no_output_tokens():
    """A result with no usage records its output tokens as unknown, never 0, so the learned average per
    call (costs.output_tokens_for) is not pulled down by counts nobody measured."""
    item = succeeded("t1")
    item.result.message.usage = None
    client, _ = fake_client(results=[item])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert r.ok and r.output_tokens is None and r.cost_usd == 0.0
    measured = succeeded("t2", use=usage(output_tokens=0))
    client, _ = fake_client(results=[measured])
    assert collect(client, "batch_1", {"t2": IDS}, OPUS)["t2"].output_tokens == 0   # a reported 0 is kept


def test_build_request_uses_the_model_max_tokens():
    """Fable 5.1 always thinks, so it gets more room before its output is cut off."""
    def max_tokens(model_id):
        return build_request("t1", [{"type": "text", "text": "sys"}], [{"type": "text", "text": "hi"}], IDS,
                             "high", model_id)["params"]["max_tokens"]

    assert (max_tokens(OPUS), max_tokens(SONNET), max_tokens(FABLE)) == (16000, 16000, 32000)


def test_collect_not_counted():
    reason = '[00:10] Customer: "it fixed itself, I do not need help anymore"'
    client, _ = fake_client(results=[succeeded("t1", text=good_text(counted="no", reason=reason))])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert r.ok
    assert r.counted is False
    assert r.not_counted_reason == reason


def test_collect_refusal():
    client, _ = fake_client(results=[succeeded("t1", text="", stop_reason="refusal", category="cyber")])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and not r.retryable
    assert r.error == "refused: cyber"
    assert r.outcomes == {} and r.resolved_on_call is None


def test_collect_max_tokens_retryable():
    client, _ = fake_client(results=[succeeded("t1", text="{", stop_reason="max_tokens")])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and r.retryable
    assert "max_tokens" in r.error


def test_collect_invalid_json_retryable():
    client, _ = fake_client(results=[succeeded("t1", text="not json")])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and r.retryable
    assert r.error.startswith("invalid output:")
    assert r.cost_usd > 0


@pytest.mark.parametrize("kind", ["expired", "canceled"])
def test_collect_expired_retryable(kind):
    client, _ = fake_client(results=[ended("t1", kind)])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and r.retryable and kind in r.error
    assert r.cost_usd == 0


def test_collect_errored_invalid_request_not_retryable():
    inner = InvalidRequestError.model_construct(type="invalid_request_error", message="bad")
    client, _ = fake_client(results=[errored("t1", inner)])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and not r.retryable
    assert "invalid_request_error" in r.error


@pytest.mark.parametrize("inner", [
    APIErrorObject.model_construct(type="api_error", message="boom"),
    OverloadedError.model_construct(type="overloaded_error", message="busy"),
])
def test_collect_errored_server_side_retryable(inner):
    client, _ = fake_client(results=[errored("t1", inner)])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and r.retryable
    assert inner.type in r.error


def test_collect_errored_flat_shape_fallback():
    res = NS(custom_id="t1", result=NS(type="errored", error=NS(type="invalid_request")))
    client, _ = fake_client(results=[res])
    r = collect(client, "batch_1", {"t1": IDS}, OPUS)["t1"]
    assert not r.ok and not r.retryable and "invalid_request" in r.error


def test_collect_out_of_order():
    other = ["x1"]
    text2 = json.dumps({"resolved_on_call": "no", "call_dropped": "yes", "counted": "yes", "not_counted_reason": "",
                        "checks": [{"check_id": "x1", "outcome": "missed", "evidence": "e"}]})
    results = [succeeded("t2", text=text2), succeeded("t1")]
    client, _ = fake_client(results=results)
    out = collect(client, "batch_1", {"t1": IDS, "t2": other}, OPUS)
    assert out["t1"].outcomes["c1"] == ("met", "said hello")
    assert out["t2"].outcomes == {"x1": ("missed", "e")}
    assert out["t2"].resolved_on_call == "no"
    assert out["t1"].call_dropped == "no" and out["t2"].call_dropped == "yes"


def test_collect_missing_result_is_retryable_error():
    client, _ = fake_client(results=[succeeded("t1")])
    out = collect(client, "batch_1", {"t1": IDS, "t2": IDS}, OPUS)
    assert out["t1"].ok
    assert not out["t2"].ok and out["t2"].retryable and "no result" in out["t2"].error


def test_collect_ignores_unexpected_ids():
    client, _ = fake_client(results=[succeeded("zzz")])
    assert collect(client, "batch_1", {}, OPUS) == {}


def test_review_outcome_is_dataclass():
    r = ReviewOutcome(ok=False, outcomes={}, resolved_on_call=None, error="e", cost_usd=0.0, retryable=True)
    assert r.error == "e"


def test_wait_polls_until_ended():
    client, batches = fake_client(statuses=["in_progress", "in_progress", "ended"])
    sleeps = []
    wait(client, "batch_1", sleep=sleeps.append)
    assert sleeps == [60, 60]
    assert batches.retrieved == 3


def test_wait_times_out():
    client, _ = fake_client(statuses=["in_progress"] * 100)
    with pytest.raises(TimeoutError):
        wait(client, "batch_1", sleep=lambda s: None, poll_seconds=3600, max_hours=2)
