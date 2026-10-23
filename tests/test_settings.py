from datetime import date

import pytest

from work_assistant import settings
from work_assistant.oneonone.cycle import Schedule
from work_assistant.settings import DEFAULTS, get_setting, get_timezone, set_setting, team_emails, validate_changes


def test_defaults(conn):
    assert get_setting(conn, "budget_limit_usd") == 40.0
    assert get_setting(conn, "problem_threshold") == 1
    assert get_setting(conn, "effort") == "medium"
    assert get_setting(conn, "retention_days") == 90
    assert get_setting(conn, "team") == []


def test_show_call_qa_defaults_on_and_must_be_a_real_boolean(conn):
    assert get_setting(conn, "show_call_qa") is True
    assert validate_changes({"show_call_qa": False}) == ({"show_call_qa": False}, {})
    for bad in (0, 1, "false", None, "yes"):
        cleaned, problems = validate_changes({"show_call_qa": bad})
        assert problems == {"show_call_qa": "Show Call QA must be on or off"} and cleaned == {}


OWNER_SCHEDULE = {
    "desmond@example.com": {"date": "2026-10-20", "weeks": 2},
    "saahil@example.com": {"date": "2026-10-21", "weeks": 2},
    "ruben@example.com": {"date": "2026-10-22", "weeks": 2},
    "zane@example.com": {"date": "2026-10-28", "weeks": 2},
}


def test_schedule_setting_validation(conn):
    assert get_setting(conn, "oneonone_schedule") == {}
    assert validate_changes({"oneonone_schedule": OWNER_SCHEDULE}) == (
        {"oneonone_schedule": OWNER_SCHEDULE}, {})
    # Keys are lowercased and stripped; dates are stored in the one ISO form; a whole 2.0 is 2.
    cleaned, problems = validate_changes({"oneonone_schedule": {
        " Desmond@Example.COM ": {"date": " 20261020 ", "weeks": 2.0}}})
    assert problems == {}
    assert cleaned == {"oneonone_schedule": {"desmond@example.com": {"date": "2026-10-20", "weeks": 2}}}
    assert validate_changes({"oneonone_schedule": {}}) == ({"oneonone_schedule": {}}, {})
    bad_values = [
        "not a map", ["desmond@example.com"], None, 7,                          # not an object
        {"desmond@example.com": "2026-10-20"},                                  # entry not an object
        {"not-an-email": {"date": "2026-10-20", "weeks": 2}},                  # bad email
        {"desmond@example.com": {"date": "10/20/2026", "weeks": 2}},            # bad date
        {"desmond@example.com": {"date": "2026-13-01", "weeks": 2}},
        {"desmond@example.com": {"weeks": 2}},                                  # no date
        {"desmond@example.com": {"date": "2026-10-20", "weeks": 0}},            # repeat out of 1-8
        {"desmond@example.com": {"date": "2026-10-20", "weeks": 9}},
        {"desmond@example.com": {"date": "2026-10-20", "weeks": "two"}},        # not a number
        {"desmond@example.com": {"date": "2026-10-20", "weeks": 2.5}},          # not whole
        {"desmond@example.com": {"date": "2026-10-20", "weeks": True}},
        {"desmond@example.com": {"date": "2026-10-20"}},                        # no repeat
    ]
    for bad in bad_values:
        cleaned, problems = validate_changes({"oneonone_schedule": bad})
        assert cleaned == {}, bad
        assert set(problems) == {"oneonone_schedule"}, bad
        assert problems["oneonone_schedule"], bad
    _, problems = validate_changes({"oneonone_schedule": {
        "desmond@example.com": {"date": "2026-10-20", "weeks": 9}}})
    assert problems == {"oneonone_schedule":
                        "The repeat for desmond@example.com must be a whole number of weeks from 1 to 8"}
    _, problems = validate_changes({"oneonone_schedule": {
        "desmond@example.com": {"date": "10/20/2026", "weeks": 2}}})
    assert problems == {"oneonone_schedule":
                        "Enter the 1:1 date for desmond@example.com as a date, like 2026-10-20"}


def test_schedules_only_for_team(conn):
    set_setting(conn, "team", ["Desmond@example.com", "saahil@example.com", "zane@example.com"])
    set_setting(conn, "oneonone_schedule", {
        **OWNER_SCHEDULE, "gone@example.com": {"date": "2026-10-20", "weeks": 4}})
    assert settings.schedules(conn) == {   # ruben and gone are not on the team; desmond matches lowercased
        "desmond@example.com": Schedule(date(2026, 10, 20), 2),
        "saahil@example.com": Schedule(date(2026, 10, 21), 2),
        "zane@example.com": Schedule(date(2026, 10, 28), 2),
    }
    set_setting(conn, "oneonone_schedule", {})
    assert settings.schedules(conn) == {}


def test_default_list_is_not_shared(conn):
    get_setting(conn, "team").append("x@x.com")
    assert DEFAULTS["team"] == []
    assert get_setting(conn, "team") == []


def test_set_and_get_roundtrip(conn):
    set_setting(conn, "zendesk_subdomain", "acme")
    set_setting(conn, "budget_limit_usd", 12.5)
    set_setting(conn, "budget_limit_usd", 15.0)  # overwrite
    assert get_setting(conn, "zendesk_subdomain") == "acme"
    assert get_setting(conn, "budget_limit_usd") == 15.0


def test_unknown_key_rejected(conn):
    with pytest.raises(KeyError):
        get_setting(conn, "nope")
    with pytest.raises(KeyError):
        set_setting(conn, "nope", 1)


def test_team_emails_normalized(conn):
    set_setting(conn, "team", [" A@x.com ", "b@X.com"])
    assert team_emails(conn) == ["a@x.com", "b@x.com"]


def test_timezone_falls_back_to_local_zone(conn, monkeypatch):
    monkeypatch.setattr(settings.tzlocal, "get_localzone_name", lambda: "Pacific/Auckland")
    assert get_timezone(conn) == "Pacific/Auckland"
    set_setting(conn, "timezone", "America/Chicago")
    assert get_timezone(conn) == "America/Chicago"


def test_model_setting_defaults_to_opus(conn):
    assert get_setting(conn, "model") == "claude-opus-5-5"


def test_model_setting_validation():
    cleaned, problems = validate_changes({"model": "claude-sonnet-5-5"})
    assert cleaned == {"model": "claude-sonnet-5-5"} and problems == {}
    for bad in ("claude-haiku-5-5", "Claude Opus 5.5", None, ["claude-opus-5-5"]):
        cleaned, problems = validate_changes({"model": bad})
        assert cleaned == {}
        assert problems["model"] == (
            "Model must be one of: Claude Opus 5.5, Claude Sonnet 5.5, Claude Fable 5.1.")
