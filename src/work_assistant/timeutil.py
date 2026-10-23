"""Day and week windows (local midnight to local midnight; weeks run Sunday to Saturday), DST-safe."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _sunday_on_or_before(day: date) -> date:
    return day - timedelta(days=(day.weekday() + 1) % 7)  # Monday is 0, Sunday is 6


def _local_midnight_utc(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)


def week_window(day: date, tz: str) -> tuple[datetime, datetime]:
    """The Sunday-start week containing `day`, as aware UTC datetimes (end exclusive)."""
    zone = ZoneInfo(tz)
    sunday = _sunday_on_or_before(day)
    return _local_midnight_utc(sunday, zone), _local_midnight_utc(sunday + timedelta(days=7), zone)


def previous_week(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The full Sunday-start week before the one containing `now` (an aware datetime)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(ZoneInfo(tz)).date()
    return week_window(today - timedelta(days=7), tz)


def midnight(day: date, tz: str) -> datetime:
    """Local midnight starting `day`, as an aware UTC datetime: where `day_window` starts."""
    return _local_midnight_utc(day, ZoneInfo(tz))


def day_window(day: date, tz: str) -> tuple[datetime, datetime]:
    """The local calendar day `day`, midnight to midnight, as aware UTC datetimes (end exclusive). A DST
    change makes it 23 or 25 hours long."""
    return date_range_window(day, day, tz)


def previous_day(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The whole local day before the one containing `now` (an aware datetime): yesterday."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return day_window(now.astimezone(ZoneInfo(tz)).date() - timedelta(days=1), tz)


def missed_days(last_end: datetime | None, now: datetime, tz: str,
                max_age_days: int) -> tuple[list[tuple[datetime, datetime]], int]:
    """The complete days a scheduled run reviews, oldest first, and how many older ones it skips.

    Every complete local day ending after `last_end` (the end of the latest period already covered,
    a day or a week), up to yesterday. With no `last_end`, just yesterday. Days that start more than
    `max_age_days` before `now` are left out and counted, since their calls are past the retention
    period; yesterday itself is always kept.
    """
    latest = previous_day(now, tz)
    if last_end is None:
        return [latest], 0
    days = []
    day = last_end.astimezone(ZoneInfo(tz)).date()
    window = day_window(day, tz)
    while window[1] <= latest[1]:
        if window[1] > last_end:
            days.append(window)
        day += timedelta(days=1)
        window = day_window(day, tz)
    cutoff = now - timedelta(days=max_age_days)
    kept = [d for d in days if d[0] >= cutoff or d == latest]
    return kept, len(days) - len(kept)


def week_start(day: date) -> date:
    """The Sunday that starts the week containing `day`."""
    return _sunday_on_or_before(day)


def local_date(ts_utc: str, tz: str) -> date:
    """The local calendar day of the ISO UTC timestamp."""
    return datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).date()


def window_days(start_utc: str, end_utc: str, tz: str) -> dict[str, str]:
    """A stored run window (ISO UTC, the end exclusive) as its `first_day` and `last_day`, local ISO dates:
    the pair the run history and the model tests list show."""
    end = datetime.fromisoformat(end_utc.replace("Z", "+00:00")) - timedelta(microseconds=1)
    return {"first_day": local_date(start_utc, tz).isoformat(),
            "last_day": end.astimezone(ZoneInfo(tz)).date().isoformat()}


def week_start_local(ts_utc: str, tz: str) -> date:
    """The Sunday that starts the local week containing the ISO UTC timestamp."""
    return _sunday_on_or_before(local_date(ts_utc, tz))


def date_range_window(first: date, last: date, tz: str) -> tuple[datetime, datetime]:
    """Local midnight starting `first` to local midnight after `last` (both days included), in UTC."""
    if last < first:
        raise ValueError("the last day is before the first day")
    zone = ZoneInfo(tz)
    return _local_midnight_utc(first, zone), _local_midnight_utc(last + timedelta(days=1), zone)
