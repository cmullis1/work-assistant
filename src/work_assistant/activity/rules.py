"""Pure day rules: one tech's day in, counts, longest in-shift gap, state and flags out."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from work_assistant import timeutil
from work_assistant.activity.model import Action, DayFacts, Flag, ShiftVersion, SignIn, Thresholds


def _local_instant(day: date, minutes: int, zone: ZoneInfo) -> datetime:
    """UTC instant of `minutes` after local midnight on `day` (wall-clock, so DST-safe)."""
    wall = datetime.combine(day, time(0)) + timedelta(minutes=minutes)
    return wall.replace(tzinfo=zone).astimezone(timezone.utc)


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def day_state(day: date, acted: bool, signed_in: bool, shift: ShiftVersion | None, off: str | None, tz: str,
              today: date, *, synced_from: datetime | None, synced_through: datetime | None) -> str:
    """The state `day_facts` gives a day, which depends only on whether the tech acted (`acted`: any action on the
    day) and signed in on it, not on what each action was: what counts days by their state alone (1:1 prep's PTO and
    sick days) needs no more."""
    zone = ZoneInfo(tz)
    weekday = (day.weekday() + 1) % 7  # Sunday = 0
    if day > today:
        return "future"
    if shift is None or not shift.active:
        return "untracked"
    midnight = _local_instant(day, 0, zone)
    if synced_from is None or synced_through is None or midnight < synced_from or synced_through <= midnight:
        return "unsynced"
    if weekday not in shift.work_days:
        return "off" if acted else "rest"
    if off == "pto":
        return "pto"
    if off == "sick":
        return "missed"
    if synced_through < _local_instant(day, shift.end_min, zone):   # the shift hadn't ended when the sync ran
        if acted:
            return "worked"
        started = _local_instant(day, shift.start_min, zone)
        return "future" if day == today and synced_through <= started else "unsynced"
    if not acted:
        return "zero" if signed_in else "missed"
    return "worked"


def day_facts(
    day: date,
    actions: list[Action],
    signins: list[SignIn],
    shift: ShiftVersion | None,
    off: str | None,
    th: Thresholds,
    tz: str,
    today: date,
    *,
    synced_from: datetime | None,
    synced_through: datetime | None,
    solved_at: Sequence[datetime] = (),
) -> DayFacts:
    """Judge one day as far as the stored data reaches.

    `synced_from` is the earliest instant the stored activity covers and `synced_through` the instant the newest
    complete sync was current to (both None before any sync). A day no sync has reached (before `synced_from`, or
    from `synced_through` on, or with no sync at all) is "unsynced" and has no flags. A shift that had not ended
    when the last sync ran is judged only on what had happened by then: no missed shift, no "signed in, no
    activity" and no early finish; a late start only once the late limit had passed with nothing recorded in the
    shift; gaps only between recorded actions. With no action yet it is "future" (today, the sync came before the
    shift started) or "unsynced". `shift` is kept only on a day the tech was due to work: not a rest day, PTO, or
    a day off the team. A marked sick day (`off` "sick") keeps its shift hours, and nothing recorded on it counts
    as in-shift. `solved` is how many of `solved_at`, the instants tickets were solved while assigned to the tech,
    fall inside the day, midnight to midnight on `tz`; a status change to solved counts for nothing. The state is
    `day_state`'s.
    """
    zone = ZoneInfo(tz)
    acts = sorted(actions, key=lambda a: a.at)
    first = acts[0].at if acts else None
    last = acts[-1].at if acts else None
    signin = min((s.at for s in signins), default=None)
    day_start, day_end = timeutil.day_window(day, tz)

    counts = dict(
        tickets=len({a.ticket_id for a in acts}),
        replies=sum(a.kind == "reply" for a in acts),
        notes=sum(a.kind == "note" for a in acts),
        calls=sum(a.kind == "call" for a in acts),
        solved=sum(day_start <= at < day_end for at in solved_at),
    )

    def build(state, gap_min=0, gap_start=None, flags=()):
        return DayFacts(
            day=day,
            state=state,
            signin=signin,
            first=first,
            last=last,
            gap_min=gap_min,
            gap_start=gap_start,
            flags=tuple(flags),
            shift=shift if scheduled else None,
            in_shift=in_shift,
            first_in_shift=first_inside,
            off=off,
            **counts,
        )

    active = shift is not None and shift.active
    weekday = (day.weekday() + 1) % 7  # Sunday = 0
    scheduled = active and weekday in shift.work_days and off != "pto"
    if scheduled:
        start = _local_instant(day, shift.start_min, zone)
        end = _local_instant(day, shift.end_min, zone)
        inside = [a.at for a in acts if start <= a.at <= end]
        in_shift = len(inside)
        first_inside = inside[0] if inside else None
    else:
        in_shift = 0
        first_inside = None

    state = day_state(day, bool(acts), signin is not None, shift, off, tz, today, synced_from=synced_from,
                      synced_through=synced_through)
    if state in ("future", "untracked", "unsynced", "off", "rest", "pto"):
        return build(state)
    if off == "sick":
        return replace(build("missed", flags=[Flag("missed", None)]), in_shift=0, first_in_shift=None)
    if state == "missed":
        return build("missed", flags=[Flag("missed", None)])
    if state == "zero":
        return build("zero", flags=[Flag("no_activity", None)])

    if synced_through < end:
        return _in_progress(build, acts, start, end, synced_through, th)

    flags = []

    first_in_shift = next((a.at for a in acts if a.at >= start), None)
    if first_in_shift is not None:
        late = _minutes(first_in_shift - start)
        if late >= th.late_min:
            flags.append(Flag("late_start", late))

    early = _minutes(end - last)
    if early >= th.early_min:
        flags.append(Flag("early_finish", early))

    gap_min, gap_start = _longest_gap(acts, start, end)
    if gap_min >= th.gap_min:
        flags.append(Flag("long_gap", gap_min))

    return build("worked", gap_min, gap_start, flags)


def _in_progress(build, acts: list[Action], start: datetime, end: datetime, synced_through: datetime,
                 th: Thresholds) -> DayFacts:
    """A worked shift (`acts` holds an action) that had not ended when the last sync ran, judged on what had happened
    by then."""
    flags = []
    first_in_shift = next((a.at for a in acts if a.at >= start), None)
    if first_in_shift is not None:
        late = _minutes(first_in_shift - start)
        if late >= th.late_min:
            flags.append(Flag("late_start", late))
    elif _minutes(synced_through - start) >= th.late_min:
        flags.append(Flag("late_start", None))   # nothing in the shift yet: late by at least the limit
    gap_min, gap_start = _longest_gap(acts, start, end)
    if gap_min >= th.gap_min:
        flags.append(Flag("long_gap", gap_min))
    return build("worked", gap_min, gap_start, flags)


def _longest_gap(acts: list[Action], start: datetime, end: datetime) -> tuple[int, datetime | None]:
    """The longest stretch between two consecutive recorded actions within the shift, and when it began."""
    inside = [a.at for a in acts if start <= a.at <= end]
    gap_min, gap_start = 0, None
    for before, after in zip(inside, inside[1:]):
        gap = _minutes(after - before)
        if gap > gap_min:
            gap_min, gap_start = gap, before
    return gap_min, gap_start
