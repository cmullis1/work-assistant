"""Submit review requests through the Message Batches API and collect the strict-JSON results.

The model is chosen per run (`reviewer.catalog`); every model gets the same request shape.

`client` is an `anthropic.Anthropic` in production; tests pass any object exposing `.messages.batches`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from work_assistant.costs import actual_cost
from work_assistant.models import Outcome
from work_assistant.reviewer.catalog import get_model
from work_assistant.reviewer.schema import OutputInvalid, output_schema, parse_output

_FINAL_ERROR_TYPES = ("invalid_request_error", "invalid_request")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass
class ReviewOutcome:
    ok: bool
    outcomes: dict[str, tuple[Outcome, str]]
    resolved_on_call: str | None
    error: str | None
    cost_usd: float
    retryable: bool
    counted: bool = True                    # False when the reviewer marked the call Not counted
    not_counted_reason: str | None = None
    call_dropped: str | None = None         # "yes" / "no" / "unclear" from the reviewer; None when it gave none
    output_tokens: int | None = None        # usage.output_tokens; None when the result reported none


def build_request(custom_id: str, system: list[dict], content: list[dict], check_ids: list[str], effort: str,
                  model_id: str) -> Request:
    """One review request, with the model's output limit. No thinking, fallbacks, temperature or betas are sent."""
    params = MessageCreateParamsNonStreaming(
        model=model_id,
        max_tokens=get_model(model_id).max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": output_schema(check_ids)}},
    )
    return Request(custom_id=custom_id, params=params)


def submit(client: Any, requests: list[Request]) -> str:
    """Create the batch and return its id.

    Create is never retried by the SDK: a retry after a timeout could create a second, paid batch.
    The caller treats an ambiguous failure as an unknown submission instead. Reads (retrieve,
    results) keep the client's normal retries.
    """
    return client.with_options(max_retries=0).messages.batches.create(requests=requests).id


def wait(client: Any, batch_id: str, sleep: Callable[[float], None] = time.sleep,
         poll_seconds: int = 60, max_hours: float = 24) -> None:
    """Poll until the batch has ended. Raises TimeoutError once max_hours of polling has elapsed."""
    waited = 0
    limit = max_hours * 3600
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        if waited >= limit:
            raise TimeoutError(f"batch {batch_id} did not end within {max_hours} hours")
        sleep(poll_seconds)
        waited += poll_seconds


def _failure(error: str, retryable: bool, cost: float = 0.0, output_tokens: int | None = None) -> ReviewOutcome:
    return ReviewOutcome(ok=False, outcomes={}, resolved_on_call=None, error=error, cost_usd=cost, retryable=retryable,
                         output_tokens=output_tokens)


def _usage(message: Any) -> dict[str, int | None]:
    """The message's token counts; a count it did not report is None."""
    usage = getattr(message, "usage", None)
    return {key: getattr(usage, key, None) for key in _USAGE_KEYS}


def _text(message: Any) -> str:
    """Join every text block; thinking and other block types are ignored."""
    return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")


def _error_type(error: Any) -> str:
    """The real error type. result.error is an ErrorResponse (type "error"); the type is on its inner .error."""
    inner = getattr(error, "error", None)
    return getattr(inner, "type", None) or getattr(error, "type", None) or "unknown"


def _review(result: Any, check_ids: list[str], model_id: str) -> ReviewOutcome:
    kind = result.type
    if kind == "succeeded":
        message = result.message
        usage = _usage(message)
        spent = {"cost": actual_cost(usage, model_id), "output_tokens": usage["output_tokens"]}
        if message.stop_reason == "refusal":
            # Stored as is; the app shows it in plain English (web.queries).
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unknown"
            return _failure(f"refused: {category}", retryable=False, **spent)
        if message.stop_reason == "max_tokens":
            return _failure("output cut off at max_tokens", retryable=True, **spent)
        try:
            parsed = parse_output(_text(message), check_ids)
        except OutputInvalid as exc:
            return _failure(f"invalid output: {exc}", retryable=True, **spent)
        return ReviewOutcome(ok=True, outcomes=parsed.outcomes, resolved_on_call=parsed.resolved_on_call,
                             error=None, cost_usd=spent["cost"], retryable=False, counted=parsed.counted,
                             not_counted_reason=parsed.not_counted_reason,
                             call_dropped=parsed.call_dropped, output_tokens=spent["output_tokens"])
    if kind == "errored":
        error_type = _error_type(getattr(result, "error", None))
        return _failure(f"request errored: {error_type}", retryable=error_type not in _FINAL_ERROR_TYPES)
    return _failure(f"request {kind}", retryable=True)


def collect(client: Any, batch_id: str, expected: dict[str, list[str]], model_id: str) -> dict[str, ReviewOutcome]:
    """Map every expected custom_id to a ReviewOutcome, costed at `model_id`'s rates (the model the
    batch was submitted with). Results are matched by custom_id, never position."""
    out: dict[str, ReviewOutcome] = {}
    for item in client.messages.batches.results(batch_id):
        check_ids = expected.get(item.custom_id)
        if check_ids is None:
            continue
        out[item.custom_id] = _review(item.result, check_ids, model_id)
    for custom_id in expected:
        out.setdefault(custom_id, _failure("no result returned for request", retryable=True))
    return out
