"""The hosted copy's scheduled sync, as the page says when the next update comes (sync_schedule)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from work_assistant import sync_schedule

CHICAGO = ZoneInfo("America/Chicago")
TIMER = Path(__file__).resolve().parent.parent / "deploy" / "linux" / "work-assistant-sync.timer"


def local(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=CHICAGO)


@pytest.mark.parametrize("now, expected", [
    (local(2026, 10, 22, 11, 30), local(2026, 10, 22, 12, 0)),    # Thursday mid-morning: the next hour
    (local(2026, 10, 22, 12, 0), local(2026, 10, 22, 13, 0)),     # on the hour: the one after
    (local(2026, 10, 22, 18, 5), local(2026, 10, 22, 19, 0)),     # the evening: still every hour
    (local(2026, 10, 22, 23, 40), local(2026, 10, 23, 0, 0)),     # late at night: midnight
    (local(2026, 10, 25, 3, 15), local(2026, 10, 25, 4, 0)),      # Sunday small hours: every hour too
    (local(2027, 3, 14, 1, 30), local(2027, 3, 14, 3, 0)),        # the clocks go forward: 2 AM doesn't happen
])
def test_next_sync(now, expected):
    got = sync_schedule.next_sync(now.astimezone(timezone.utc))
    assert got == expected.astimezone(timezone.utc)
    assert got.tzinfo == timezone.utc


def test_the_night_the_clocks_go_back_has_both_1_am_syncs():
    first = datetime(2027, 11, 7, 1, 0, tzinfo=CHICAGO)            # 1 AM before the clocks go back
    second = datetime(2027, 11, 7, 1, 0, tzinfo=CHICAGO).replace(fold=1)
    assert sync_schedule.next_sync(first.astimezone(timezone.utc)) == second.astimezone(timezone.utc)


@pytest.mark.skipif(not TIMER.exists(), reason="the server's deploy kit is not in this copy of the repo")
def test_the_schedule_matches_the_servers_timer():
    """The page's "next update" comes from sync_schedule (the next hour on UTC's clock); the server runs the sync
    from the timer file, every hour on the hour, UTC."""
    lines = [line.split("=", 1)[1] for line in TIMER.read_text(encoding="utf-8").splitlines()
             if line.startswith("OnCalendar=")]
    assert lines == ["*-*-* *:00:00 UTC"]
