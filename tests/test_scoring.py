import dataclasses

import pytest

from work_assistant.models import CheckDef
from work_assistant.rubric import ensure_seeded, save_version
from work_assistant.scoring import effective, score, verdict


@pytest.fixture
def version(conn):
    return ensure_seeded(conn)


def all_met(version):
    return {c.check_id: "met" for c in version.checks if c.enabled}


def test_all_met_100(version):
    assert score(version, all_met(version)) == (100, 100, 100.0)


def test_na_and_unsure_earn(version):
    o = all_met(version)
    o["sc11"] = "na"
    o["sc06"] = "unsure"
    assert score(version, o) == (100, 100, 100.0)


def test_miss_costs_points(version):
    o = all_met(version)
    o["sc06"] = "missed"
    o["sc15"] = "missed"
    assert score(version, o) == (90, 100, 90.0)


def test_auto_fail_zero(version):
    checks = tuple(
        dataclasses.replace(c, auto_fail=True) if c.check_id == "sc01" else c
        for c in version.checks
    )
    v = dataclasses.replace(version, checks=checks)
    o = all_met(v)
    o["sc01"] = "missed"
    assert score(v, o) == (0, 100, 0.0)


def test_percent_with_105_total(version):
    extra = CheckDef(
        check_id="sc-extra", kind="scorecard", number=17, summary="Extra", definition="Extra",
        points=5, auto_fail=False, applies_when="Always", enabled=True,
    )
    v = dataclasses.replace(version, checks=version.checks + (extra,))
    o = all_met(v)
    o["sc-extra"] = "missed"
    assert score(v, o) == (100, 105, 95.2)


def test_missing_outcome_earns_nothing_and_is_not_a_miss(version):
    o = all_met(version)
    del o["sc06"]
    assert score(version, o) == (95, 100, 95.0)
    assert verdict(o, True, True, 1) == "good"


def test_disabled_check_ignored(version):
    checks = tuple(
        dataclasses.replace(c, enabled=False) if c.check_id == "sc06" else c
        for c in version.checks
    )
    v = dataclasses.replace(version, checks=checks)
    o = all_met(v)
    o["sc06"] = "missed"
    assert score(v, o) == (95, 95, 100.0)


def test_verdicts(version):
    o = all_met(version)
    assert verdict(o, True, True, 1) == "good"

    process = {**o, "pr-x": "missed"}
    assert verdict(process, True, True, 1) == "problem"

    assert verdict({**o, "sc03": "unsure"}, True, True, 1) == "needs_review"
    assert verdict(o, False, True, 1) == "needs_review"
    # A review that did not finish has its own verdict (T-045), whatever its measured checks say.
    assert verdict(o, True, False, 1) == "review_failed"
    assert verdict({}, True, False, 1) == "review_failed"
    assert verdict(process, True, False, 1) == "review_failed"
    assert verdict(o, False, False, 1) == "review_failed"

    one_miss = {**o, "sc06": "missed"}
    assert verdict(one_miss, True, True, 2) == "good"
    assert verdict({**one_miss, "sc15": "missed"}, True, True, 2) == "problem"


def test_override_wins():
    assert effective("missed", "met") == "met"
    assert effective("missed", None) == "missed"
