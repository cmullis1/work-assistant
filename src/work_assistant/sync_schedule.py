"""When the hosted copy's scheduled sync runs, so the page can say when the next update comes instead of offering a
Refresh button (the owner, 2026-10-22): every hour, on the hour, around the clock every day (the owner, 2026-10-22),
as deploy/linux/work-assistant-sync.timer says (tests/test_sync_schedule.py checks the two agree). The timer keeps
UTC's hours, so no night skips a run or runs one twice when the clocks change; they are the same hours in every zone
a whole number of hours from UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def next_sync(now: datetime) -> datetime:
    """The first scheduled sync after `now`: the next hour on UTC's clock, as an aware UTC instant."""
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return hour + timedelta(hours=1)
