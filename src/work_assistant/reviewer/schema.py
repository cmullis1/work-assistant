"""The reviewer's strict JSON output schema and the validation of what comes back."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, get_args

from work_assistant.models import Outcome

OUTCOMES: tuple[str, ...] = get_args(Outcome)
RESOLVED_VALUES: tuple[str, ...] = ("yes", "no", "unclear")
COUNTED_VALUES: tuple[str, ...] = ("yes", "no")
DROPPED_VALUES: tuple[str, ...] = ("yes", "no", "unclear")


class OutputInvalid(ValueError):
    """The reviewer's output is not valid JSON or does not cover exactly the requested checks."""


@dataclass(frozen=True)
class ReviewerOutput:
    outcomes: dict[str, tuple[Outcome, str]]  # check_id -> (outcome, evidence), in requested order
    resolved_on_call: str
    counted: bool                     # False when the reviewer marks the call Not counted (spec §7.3)
    not_counted_reason: str | None    # set only when counted is False
    call_dropped: str = "unclear"     # "yes" / "no" / "unclear"; the default keeps older callers working


def output_schema(check_ids: list[str]) -> dict:
    """JSON schema for structured output: one entry per check id, resolved_on_call, call_dropped, and Not counted.

    not_counted_reason is required too (structured output needs every property required); it is
    empty when counted is yes.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks", "resolved_on_call", "call_dropped", "counted", "not_counted_reason"],
        "properties": {
            "resolved_on_call": {"type": "string", "enum": list(RESOLVED_VALUES)},
            "call_dropped": {"type": "string", "enum": list(DROPPED_VALUES)},
            "counted": {"type": "string", "enum": list(COUNTED_VALUES)},
            "not_counted_reason": {"type": "string"},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["check_id", "outcome", "evidence"],
                    "properties": {
                        "check_id": {"type": "string", "enum": list(check_ids)},
                        "outcome": {"type": "string", "enum": list(OUTCOMES)},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }


def _entry(item: Any, index: int) -> tuple[str, Outcome, str]:
    if not isinstance(item, dict):
        raise OutputInvalid(f"checks[{index}] is not an object")
    missing = [k for k in ("check_id", "outcome", "evidence") if k not in item]
    if missing:
        raise OutputInvalid(f"checks[{index}] is missing {', '.join(missing)}")
    check_id, outcome, evidence = item["check_id"], item["outcome"], item["evidence"]
    if not isinstance(check_id, str):
        raise OutputInvalid(f"checks[{index}].check_id is not a string")
    if outcome not in OUTCOMES:
        raise OutputInvalid(f"check {check_id} has invalid outcome {outcome!r}")
    if not isinstance(evidence, str):
        raise OutputInvalid(f"check {check_id} evidence is not a string")
    return check_id, outcome, evidence


def parse_output(text: str, check_ids: list[str]) -> ReviewerOutput:
    """Validate the reviewer's JSON.

    The outcomes cover exactly `check_ids`, in that order. Raises OutputInvalid on bad JSON, a wrong
    structure, an invalid resolved_on_call, call_dropped or counted, a missing, duplicate or unknown
    check id, or a Not counted call without a reason.
    A reason sent with a counted call is dropped.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OutputInvalid(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OutputInvalid("top level is not an object")

    resolved = data.get("resolved_on_call")
    if resolved not in RESOLVED_VALUES:
        raise OutputInvalid(f"invalid resolved_on_call {resolved!r}")
    dropped = data.get("call_dropped")
    if dropped not in DROPPED_VALUES:
        raise OutputInvalid(f"invalid call_dropped {dropped!r}")
    counted = data.get("counted")
    if counted not in COUNTED_VALUES:
        raise OutputInvalid(f"invalid counted {counted!r}")
    reason = data.get("not_counted_reason")
    if not isinstance(reason, str):
        raise OutputInvalid("not_counted_reason is not a string")
    if counted == "no" and not reason.strip():
        raise OutputInvalid("not_counted_reason is empty for a Not counted call")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise OutputInvalid("checks is not a list")

    expected = set(check_ids)
    found: dict[str, tuple[Outcome, str]] = {}
    for index, item in enumerate(checks):
        check_id, outcome, evidence = _entry(item, index)
        if check_id not in expected:
            raise OutputInvalid(f"unknown check id {check_id}")
        if check_id in found:
            raise OutputInvalid(f"duplicate check id {check_id}")
        found[check_id] = (outcome, evidence)

    missing = [cid for cid in check_ids if cid not in found]
    if missing:
        raise OutputInvalid(f"missing check ids: {', '.join(missing)}")
    return ReviewerOutput(
        outcomes={cid: found[cid] for cid in check_ids},
        resolved_on_call=resolved,
        counted=counted == "yes",
        not_counted_reason=reason.strip() if counted == "no" else None,
        call_dropped=dropped,
    )
