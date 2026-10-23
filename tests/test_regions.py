"""Each tech's region (Regions design, plan Task 1): NASA, EMEA or APAC, whose clock judges the tech's days. A region
is dated, as a shift is (the owner, 2026-10-21: one tech served APAC, then NASA): the region in force on a day is the
latest change on or before it, the earliest change's before any. A tech with no region stored shows as NASA and is
judged on the app's own zone, so a database with no regions answers exactly as before; a tech set to NASA is on
Dallas time (America/Chicago)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from work_assistant import regions
from work_assistant.settings import set_setting

UTC = timezone.utc
PAT = "pat.tech@example.com"
SAM = "sam.tech@example.com"
JUN_1, OCT_11, OCT_12 = date(2026, 6, 1), date(2026, 10, 11), date(2026, 10, 12)


def test_the_three_regions_their_clocks_and_cities():
    assert regions.REGIONS == {"NASA": "America/Chicago", "EMEA": "Asia/Dubai", "APAC": "Asia/Manila"}
    assert regions.ORDER == ("NASA", "EMEA", "APAC")
    assert regions.CITIES == {"NASA": "Dallas", "EMEA": "Dubai", "APAC": "Manila"}
    assert regions.DEFAULT == "NASA"


def test_unset_is_nasa_on_the_app_zone(conn):
    set_setting(conn, "timezone", "UTC")
    assert regions.region_of(conn, PAT) == "NASA" and regions.zone_of(conn, PAT) == "UTC"
    assert regions.region_of(conn, PAT, OCT_12) == "NASA" and regions.zone_of(conn, PAT, OCT_12) == "UTC"
    assert regions.region_history(conn, PAT) == []


def test_a_set_region_has_its_own_zone(conn):
    set_setting(conn, "timezone", "UTC")
    regions.set_regions(conn, PAT, [(JUN_1, "APAC")])
    assert regions.region_of(conn, PAT) == "APAC" and regions.zone_of(conn, PAT) == "Asia/Manila"
    regions.set_regions(conn, PAT, [(JUN_1, "NASA")])
    assert regions.zone_of(conn, PAT) == "America/Chicago", "a tech set to NASA is on Dallas time, not the app's zone"


def test_a_swap_is_dated(conn):
    """Served APAC, then NASA from Oct 12: each day is judged on the region in force on it."""
    regions.set_regions(conn, PAT, [(OCT_12, "NASA"), (JUN_1, "APAC")])
    assert regions.region_history(conn, PAT) == [(JUN_1, "APAC"), (OCT_12, "NASA")], "oldest first"
    assert regions.region_of(conn, PAT, OCT_11) == "APAC" and regions.zone_of(conn, PAT, OCT_11) == "Asia/Manila"
    assert regions.region_of(conn, PAT, OCT_12) == "NASA" and regions.zone_of(conn, PAT, OCT_12) == "America/Chicago"
    assert regions.region_of(conn, PAT, date(2026, 5, 1)) == "APAC", "before the earliest change, the earliest's region"
    assert regions.region_of(conn, PAT) == "NASA" and regions.zone_of(conn, PAT) == "America/Chicago", \
        "the current region is the latest change's"


def test_repeats_merge_keeping_the_earlier_date(conn):
    regions.set_regions(conn, PAT, [(JUN_1, "APAC"), (date(2026, 8, 3), "APAC"), (OCT_12, "NASA"),
                                    (date(2026, 11, 2), "NASA")])
    assert regions.region_history(conn, PAT) == [(JUN_1, "APAC"), (OCT_12, "NASA")]


@pytest.mark.parametrize("history, words", [
    ([(JUN_1, "LATAM")], "The region must be NASA, EMEA or APAC."),
    ([(JUN_1, ["APAC"])], "The region must be NASA, EMEA or APAC."),
    ([(JUN_1, "APAC"), (JUN_1, "EMEA")], "Each region change needs its own date."),
])
def test_a_bad_history_is_refused_and_changes_nothing(conn, history, words):
    regions.set_regions(conn, PAT, [(OCT_12, "EMEA")])
    with pytest.raises(ValueError) as refused:
        regions.set_regions(conn, PAT, history)
    assert str(refused.value) == words
    assert regions.region_history(conn, PAT) == [(OCT_12, "EMEA")]


def test_an_empty_history_is_back_to_unset(conn):
    set_setting(conn, "timezone", "UTC")
    regions.set_regions(conn, PAT, [(JUN_1, "APAC")])
    regions.set_regions(conn, PAT, [])
    assert regions.region_history(conn, PAT) == []
    assert regions.region_of(conn, PAT) == "NASA" and regions.zone_of(conn, PAT) == "UTC"


def test_regions_of_gives_each_techs_current_region(conn):
    regions.set_regions(conn, PAT, [(JUN_1, "APAC"), (OCT_12, "EMEA")])
    assert regions.regions_of(conn, [PAT, SAM]) == {PAT: "EMEA", SAM: "NASA"}
    assert regions.regions_of(conn, []) == {}


def test_the_current_region_is_the_one_in_force_today(conn, monkeypatch):
    """A change dated after today (an import can write one) isn't current until its day comes: a tech's current
    region is the one in force on the app's today, in `regions_of`, `region_of`, `zone_of` and `today_for`; with no
    change on or before today, the earliest change's. Today is the caller's (the web app's) or, with none, the app's
    by the real clock (`app_today`)."""
    set_setting(conn, "timezone", "America/Chicago")
    today, nov_2 = date(2026, 10, 22), date(2026, 11, 2)
    regions.set_regions(conn, PAT, [(JUN_1, "APAC"), (nov_2, "NASA")])
    regions.set_regions(conn, SAM, [(nov_2, "EMEA"), (date(2026, 12, 7), "APAC")])
    assert regions.regions_of(conn, [PAT, SAM], today) == {PAT: "APAC", SAM: "EMEA"}
    assert regions.regions_of(conn, [PAT, SAM], nov_2) == {PAT: "NASA", SAM: "EMEA"}
    now = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)      # 15:00 Oct 22 in Dallas, 04:00 Oct 23 in Manila
    assert regions.today_for(conn, PAT, now, today) == date(2026, 10, 23), "still on Manila's clock"
    monkeypatch.setattr(regions, "app_today", lambda conn: today)
    assert regions.regions_of(conn, [PAT, SAM]) == {PAT: "APAC", SAM: "EMEA"}
    assert regions.region_of(conn, PAT) == "APAC" and regions.zone_of(conn, PAT) == "Asia/Manila"
    assert regions.region_of(conn, SAM) == "EMEA" and regions.zone_of(conn, SAM) == "Asia/Dubai"


def test_today_for_is_the_techs_local_day(conn):
    regions.set_regions(conn, PAT, [(JUN_1, "APAC")])
    now = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)      # 04:00 Oct 22 in Manila
    assert regions.today_for(conn, PAT, now, date(2026, 10, 21)) == date(2026, 10, 22)
    assert regions.today_for(conn, PAT, None, date(2026, 10, 21)) == date(2026, 10, 21)
