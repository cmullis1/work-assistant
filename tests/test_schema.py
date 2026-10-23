import json

import pytest

from work_assistant.reviewer.schema import OutputInvalid, output_schema, parse_output

IDS = ["sc01", "sc02", "pr-scope"]


def payload(checks, resolved="yes", counted="yes", reason="", dropped="no"):
    return json.dumps({"checks": checks, "resolved_on_call": resolved, "call_dropped": dropped,
                       "counted": counted, "not_counted_reason": reason})


def entry(check_id, outcome="met", evidence="ok"):
    return {"check_id": check_id, "outcome": outcome, "evidence": evidence}


def test_output_schema_shape():
    assert output_schema(IDS) == {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks", "resolved_on_call", "call_dropped", "counted", "not_counted_reason"],
        "properties": {
            "resolved_on_call": {"type": "string", "enum": ["yes", "no", "unclear"]},
            "call_dropped": {"type": "string", "enum": ["yes", "no", "unclear"]},
            "counted": {"type": "string", "enum": ["yes", "no"]},
            "not_counted_reason": {"type": "string"},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["check_id", "outcome", "evidence"],
                    "properties": {
                        "check_id": {"type": "string", "enum": IDS},
                        "outcome": {"type": "string", "enum": ["met", "missed", "na", "unsure"]},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }


def test_output_schema_does_not_alias_caller_list():
    ids = list(IDS)
    schema = output_schema(ids)
    ids.append("sc99")
    assert schema["properties"]["checks"]["items"]["properties"]["check_id"]["enum"] == IDS


def test_parse_ok():
    text = payload(
        [
            entry("pr-scope", "unsure", "Reference material not provided."),
            entry("sc01", "met", '[00:03] Agent: "Thank you for calling…"'),
            entry("sc02", "missed", "No phonetic read-back anywhere in the transcript."),
        ],
        resolved="no",
    )
    parsed = parse_output(text, IDS)
    outcomes = parsed.outcomes
    assert parsed.resolved_on_call == "no"
    assert parsed.counted is True
    assert parsed.not_counted_reason is None
    assert outcomes == {
        "sc01": ("met", '[00:03] Agent: "Thank you for calling…"'),
        "sc02": ("missed", "No phonetic read-back anywhere in the transcript."),
        "pr-scope": ("unsure", "Reference material not provided."),
    }
    assert list(outcomes) == IDS


def test_parse_missing_id_raises():
    with pytest.raises(OutputInvalid, match="pr-scope"):
        parse_output(payload([entry("sc01"), entry("sc02")]), IDS)


def test_parse_duplicate_raises():
    with pytest.raises(OutputInvalid, match="sc01"):
        parse_output(payload([entry("sc01"), entry("sc01"), entry("sc02"), entry("pr-scope")]), IDS)


def test_parse_unknown_raises():
    with pytest.raises(OutputInvalid, match="sc99"):
        parse_output(
            payload([entry("sc01"), entry("sc02"), entry("pr-scope"), entry("sc99")]), IDS
        )


def test_parse_bad_json_raises():
    with pytest.raises(OutputInvalid):
        parse_output('{"checks": [', IDS)


@pytest.mark.parametrize(
    "text",
    [
        json.dumps([]),
        json.dumps({"checks": []}),
        json.dumps({"checks": "x", "resolved_on_call": "yes", "call_dropped": "no", "counted": "yes", "not_counted_reason": ""}),
        payload([entry("sc01"), entry("sc02"), entry("pr-scope")], resolved="maybe"),
        payload([entry("sc01", outcome="pass"), entry("sc02"), entry("pr-scope")]),
        payload([entry("sc01", evidence=5), entry("sc02"), entry("pr-scope")]),
        payload([{"check_id": "sc01", "outcome": "met"}, entry("sc02"), entry("pr-scope")]),
        payload(["sc01", entry("sc02"), entry("pr-scope")]),
        json.dumps({"checks": [entry("sc01"), entry("sc02"), entry("pr-scope")], "resolved_on_call": "yes",
                    "call_dropped": "no"}),
        payload([entry("sc01"), entry("sc02"), entry("pr-scope")], counted="maybe"),
        payload([entry("sc01"), entry("sc02"), entry("pr-scope")], reason=None),
        json.dumps({"checks": [entry("sc01"), entry("sc02"), entry("pr-scope")], "resolved_on_call": "yes",
                    "call_dropped": "no", "counted": "yes"}),
        json.dumps({"checks": [entry("sc01"), entry("sc02"), entry("pr-scope")], "resolved_on_call": "yes",
                    "counted": "yes", "not_counted_reason": ""}),
        payload([entry("sc01"), entry("sc02"), entry("pr-scope")], dropped="maybe"),
    ],
    ids=[
        "top-level-list", "no-resolved", "checks-not-list", "bad-resolved",
        "bad-outcome", "evidence-not-string", "missing-evidence-key", "entry-not-object",
        "no-counted", "bad-counted", "reason-not-string", "no-reason", "no-call-dropped", "bad-call-dropped",
    ],
)
def test_parse_malformed_structure_raises(text):
    with pytest.raises(OutputInvalid):
        parse_output(text, IDS)


def test_schema_requires_counted():
    schema = output_schema(IDS)
    assert "counted" in schema["required"] and "not_counted_reason" in schema["required"]
    assert schema["properties"]["counted"] == {"type": "string", "enum": ["yes", "no"]}


def test_parse_not_counted():
    text = payload([entry("sc01"), entry("sc02"), entry("pr-scope")], counted="no",
                   reason='[00:12] Customer: "my problem seems to have resolved itself"')
    parsed = parse_output(text, IDS)
    assert parsed.counted is False
    assert parsed.not_counted_reason == '[00:12] Customer: "my problem seems to have resolved itself"'


def test_parse_not_counted_needs_a_reason():
    with pytest.raises(OutputInvalid, match="not_counted_reason"):
        parse_output(payload([entry("sc01"), entry("sc02"), entry("pr-scope")], counted="no", reason="  "), IDS)


def test_parse_counted_ignores_a_stray_reason():
    parsed = parse_output(payload([entry("sc01"), entry("sc02"), entry("pr-scope")], reason="leftover"), IDS)
    assert parsed.counted is True and parsed.not_counted_reason is None


def test_schema_requires_call_dropped():
    schema = output_schema(IDS)
    assert "call_dropped" in schema["required"]
    assert schema["properties"]["call_dropped"] == {"type": "string", "enum": ["yes", "no", "unclear"]}


@pytest.mark.parametrize("answer", ["yes", "no", "unclear"])
def test_parse_call_dropped(answer):
    parsed = parse_output(payload([entry("sc01"), entry("sc02"), entry("pr-scope")], dropped=answer), IDS)
    assert parsed.call_dropped == answer
