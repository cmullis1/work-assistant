"""Import from Teams Shifts (Shifts import design, 2026-10-21-shift-finder-design.md). Task 1: the reader turns an
export workbook into rows, and the planner turns rows into each roster tech's dated regions, week-by-week shift
entries, Started and leave, every day on the tech's own region clock. It reads the database and writes nothing.
Task 2: the applier writes a plan, each tech in one transaction, and the same files applied again change nothing.
Task 4: leave is read by word from a row's label and its notes, as the owner's exports write it; half days are work
days, a timed Time Off entry covers each day it overlaps by six hours or more on the tech's clock, and the labels read
as working shifts that the import doesn't know are listed.

The workbooks are synthetic, built here in the export's own layout (sheet "Shifts" and sheet "Time Off", dates
M/D/YYYY, times HH:MM on the app's zone, America/Chicago); never real files."""
from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from work_assistant import names, regions
from work_assistant.activity import store
from work_assistant.activity.model import ShiftVersion, TimeOff
from work_assistant.settings import set_setting
from work_assistant.shifts_import import apply_import, plan_import, preview_json, read_workbook

CHICAGO = "America/Chicago"
SHIFTS_HEADER = ["Member", "Work Email", "Group", "Start Date", "Start Time", "End Date", "End Time", "Theme Color",
                 "Custom Label", "Unpaid Break (minutes)", "Notes", "Shared"]
TIME_OFF_HEADER = ["Member", "Work Email", "Start Date", "Start Time", "End Date", "End Time", "Time Off Reason",
                   "Theme Color", "Notes", "Shared"]
NASA, EMEA, APAC = "NASA Team", "EMEA Team", "APAC Team"

PAT = ("Pat Nasa", "pat.nasa@example.com")
EMMA = ("Emma Emea", "emma.emea@example.com")
ANDY = ("Andy Apac", "andy.apac@example.com")
SAM = ("Sam Swap", "sam.swap@example.com")
LEE = ("Lee Lead", "lee.lead@example.com")       # a team lead: not on the roster
KIM = ("Kim Gone", "kim.gone@example.com")       # left the team: not on the roster

OCT_4, OCT_11, OCT_18 = date(2026, 10, 4), date(2026, 10, 11), date(2026, 10, 18)   # Sundays
MON_FRI = frozenset({1, 2, 3, 4, 5})


def _written(day: date) -> str:
    return f"{day.month}/{day.day}/{day.year}"


def shift(tech, group, day, start, end, label="", note=""):
    """One Shifts row as the export writes it; an end at or before the start is on the next day."""
    member, email = tech
    end_day = day + timedelta(days=1) if end <= start else day
    return [member, email, group, _written(day), start, _written(end_day), end, "1. White", label, 0, note, "Shared"]


def week(tech, group, sunday, start, end, rest=(0, 6)):
    """A tech's Sunday-to-Saturday week as the export writes it: one row a day, `Rest Day` on the `rest` days."""
    return [shift(tech, group, sunday + timedelta(days=i), start, end, "Rest Day" if i in rest else "")
            for i in range(7)]


def time_off(tech, first, start, end_day, end, reason):
    member, email = tech
    return [member, email, _written(first), start, _written(end_day), end, reason, "1. White", "", "Shared"]


def workbook(shifts=(), offs=(), sheets=("Shifts", "Time Off")) -> bytes:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title in sheets:
        sheet = book.create_sheet(title)
        if title == "Shifts":
            sheet.append(SHIFTS_HEADER)
            for row in shifts:
                sheet.append(row)
        elif title == "Time Off":
            sheet.append(TIME_OFF_HEADER)
            for row in offs:
                sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def planned(conn, shifts=(), offs=()):
    rows, off_rows = read_workbook(workbook(shifts, offs), "schedule.xlsx", CHICAGO)
    return plan_import(conn, rows, off_rows)


def tech_plan(plan, tech):
    return next(t for t in plan.techs if t.email == tech[1])


def version(tech, since, days, start_min, end_min):
    return ShiftVersion(tech[1], since, frozenset(days), start_min, end_min, True)


@pytest.fixture
def team(conn):
    """The app on Chicago's clock, with Pat, Emma, Andy and Sam on the roster (Lee and Kim are not)."""
    set_setting(conn, "timezone", CHICAGO)
    set_setting(conn, "team", [tech[1] for tech in (PAT, EMMA, ANDY, SAM)])
    return conn


def test_a_nasa_week(team):
    plan = planned(team, week(PAT, NASA, OCT_4, "08:00", "17:00"))
    pat = tech_plan(plan, PAT)
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020),), "one entry from that Sunday, Dallas 8 to 5"
    assert pat.regions == ((OCT_4, "NASA"),)
    assert (plan.first, plan.last) == (OCT_4, date(2026, 10, 10))
    assert pat.started is None and pat.replace_before
    assert pat.pto == pat.sick == pat.problems == plan.unrecognised == ()


def test_the_first_week_starts_at_the_first_imported_date(team):
    """A file that begins on a Wednesday: its first week's entry is dated that Wednesday, not the Sunday before
    (those days aren't in the files); the next week's is dated its Sunday."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00")[3:] + week(PAT, NASA, OCT_11, "08:00", "17:00")
    pat = tech_plan(planned(team, rows), PAT)
    assert pat.entries == (version(PAT, date(2026, 10, 7), {3, 4, 5}, 480, 1020),
                           version(PAT, OCT_11, MON_FRI, 480, 1020))


def test_an_emea_shift_is_dubai_9_to_6(team):
    plan = planned(team, week(EMMA, EMEA, OCT_4, "00:00", "09:00"))
    emma = tech_plan(plan, EMMA)
    assert emma.entries == (version(EMMA, OCT_4, MON_FRI, 540, 1080),), "Chicago 00:00-09:00 is Dubai 9 to 6"
    assert emma.regions == ((OCT_4, "EMEA"),)
    assert regions.REGIONS["EMEA"] == "Asia/Dubai"


def test_an_apac_shift_lands_on_its_manila_day(team):
    book = workbook([shift(ANDY, APAC, date(2026, 10, 3), "15:00", "00:00", "Rest Day"),   # Manila Sun Oct 4
                     shift(ANDY, APAC, date(2026, 10, 7), "15:00", "00:00")])               # Manila Thu Oct 8
    rows, offs = read_workbook(book, "schedule.xlsx", CHICAGO)
    worked = rows[1]
    assert worked.start == datetime(2026, 10, 7, 15, 0, tzinfo=ZoneInfo(CHICAGO))
    assert worked.end == datetime(2026, 10, 8, 0, 0, tzinfo=ZoneInfo(CHICAGO))
    manila = ZoneInfo("Asia/Manila")
    assert worked.start.astimezone(manila).replace(tzinfo=None) == datetime(2026, 10, 8, 4, 0)
    andy = tech_plan(plan_import(team, rows, offs), ANDY)
    assert andy.entries == (version(ANDY, OCT_4, {4}, 240, 780),), "Thursday (4) on Manila's clock, not Wednesday"
    assert andy.regions == ((OCT_4, "APAC"),)


def test_rest_day_and_rest_only_week(team):
    rows = (week(PAT, NASA, OCT_4, "08:00", "17:00", rest=(0, 2, 6))     # Tuesday is a Rest Day
            + week(PAT, NASA, OCT_11, "08:00", "17:00", rest=range(7))  # only Rest Day rows
            + week(PAT, NASA, OCT_18, "08:00", "17:00"))
    pat = tech_plan(planned(team, rows), PAT)
    assert pat.entries == (version(PAT, OCT_4, {1, 3, 4, 5}, 480, 1020),
                           version(PAT, OCT_11, set(), 480, 1020),
                           version(PAT, OCT_18, MON_FRI, 480, 1020))
    assert pat.pto == pat.sick == (), "a rest day is neither leave nor a missed day"


def test_annual_leave_on_a_wednesday_is_no_shift_change(team):
    """Ruling 12: a weekday with leave is a scheduled work day of its week's pattern, so a Mon-Fri week with Annual
    Leave on Wednesday is Mon-Fri, and nothing changes against the weeks around it."""
    middle = week(PAT, NASA, OCT_11, "08:00", "17:00")
    middle[3] = shift(PAT, NASA, date(2026, 10, 14), "08:00", "17:00", "Annual Leave")
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + middle + week(PAT, NASA, OCT_18, "08:00", "17:00")
    pat = tech_plan(planned(team, rows), PAT)
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020),)
    assert pat.pto == (date(2026, 10, 14),)


def test_time_off_sick_leave_on_a_tuesday_is_no_shift_change(team):
    """Ruling 12, from the Time Off sheet: Tuesday has no Shifts row, only a Sick Leave entry, and still counts."""
    middle = week(PAT, NASA, OCT_11, "08:00", "17:00")
    del middle[2]   # Tuesday Oct 13
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + middle + week(PAT, NASA, OCT_18, "08:00", "17:00")
    offs = [time_off(PAT, date(2026, 10, 13), "00:00", date(2026, 10, 14), "00:00", "Sick Leave")]
    pat = tech_plan(planned(team, rows, offs), PAT)
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020),)
    assert pat.sick == (date(2026, 10, 13),)


def on_leave(tech, sunday, rest=(0, 6)):
    """A Sunday-to-Saturday week of Annual Leave, with Rest Day rows on the `rest` days."""
    return [shift(tech, NASA, sunday + timedelta(days=i), "08:00", "17:00", "Rest Day" if i in rest else
                  "Annual Leave") for i in range(7)]


def test_a_week_of_only_leave(team):
    """Ruling 12: a week whose only non-rest days are leave takes the week before's hours, with its leave days as its
    work days; with no week before, the next working week's. A Rest Day stays out of its week even when a leave
    entry spans it. A tech on leave in the files' first week was on the schedule then: no Started."""
    rows = (week(PAT, NASA, OCT_4, "09:00", "18:00") + on_leave(PAT, OCT_11) + week(PAT, NASA, OCT_18, "10:00", "19:00")
            + on_leave(SAM, OCT_4, rest=(0, 5, 6)) + week(SAM, NASA, OCT_11, "10:00", "19:00"))
    offs = [time_off(PAT, date(2026, 10, 12), "00:00", date(2026, 10, 18), "00:00", "Annual Leave")]   # to Saturday
    plan = planned(team, rows, offs)
    assert tech_plan(plan, PAT).entries == (version(PAT, OCT_4, MON_FRI, 540, 1080),
                                            version(PAT, OCT_18, MON_FRI, 600, 1140))
    sam = tech_plan(plan, SAM)
    assert sam.entries == (version(SAM, OCT_4, {1, 2, 3, 4}, 600, 1140), version(SAM, OCT_11, MON_FRI, 600, 1140)), \
        "Sam's first week, Mon-Thu on leave, at his next working week's hours"
    assert sam.started is None


def test_leave_labels_and_time_off_reasons(team):
    rows = [shift(PAT, NASA, date(2026, 10, 5), "08:00", "17:00"),
            shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00", "Annual Leave"),
            shift(PAT, NASA, date(2026, 10, 9), "13:00", "17:00", " annual leave "),   # a shorter shift: still leave
            shift(PAT, NASA, date(2026, 10, 12), "08:00", "17:00", "Public Holiday"),
            shift(PAT, NASA, date(2026, 10, 13), "08:00", "17:00", "Sick Leave"),
            shift(PAT, NASA, date(2026, 10, 14), "08:00", "17:00", "Work From Home")]   # a working shift
    offs = [time_off(PAT, date(2026, 10, 7), "00:00", date(2026, 10, 9), "00:00", "Sick Leave"),   # Wed and Thu
            time_off(PAT, date(2026, 10, 15), "08:00", date(2026, 10, 15), "17:00", "Vacation"),  # timed: a full day
            time_off(PAT, date(2026, 10, 16), "00:00", date(2026, 10, 17), "00:00", "Rest Day"),
            time_off(PAT, date(2026, 10, 19), "00:00", date(2026, 10, 20), "00:00", "Work From Home")]
    shift_rows, off_rows = read_workbook(workbook(rows, offs), "schedule.xlsx", CHICAGO)
    assert [(o.kind, o.first, o.last) for o in off_rows] == [
        ("sick", date(2026, 10, 7), date(2026, 10, 8)),
        ("pto", date(2026, 10, 15), date(2026, 10, 15)),
        ("rest", date(2026, 10, 16), date(2026, 10, 16))], "Work From Home isn't time off"
    plan = plan_import(team, shift_rows, off_rows)
    pat = tech_plan(plan, PAT)
    assert pat.pto == (date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 15))
    assert pat.sick == (date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 13))
    assert pat.entries[-1].work_days == frozenset({1, 2, 3}), \
        "Monday's and Tuesday's leave are work days (Ruling 12), Work From Home is a working shift, and Thursday's " \
        "leave is after the imported dates"
    assert plan.unrecognised == (("Work From Home", 1),)


def days_from(first: date, count: int) -> list[date]:
    return [first + timedelta(days=i) for i in range(count)]


def read_as(plan, tech, days: list[date]) -> list[str | None]:
    """What the plan makes each of `days` for the tech: "pto", "sick", or None (not leave)."""
    found = tech_plan(plan, tech)
    return ["pto" if day in found.pto else "sick" if day in found.sick else None for day in days]


def test_leave_words_in_labels(team):
    """Leave is read from a label by word, whatever else it says; a day with a sick word is Sick."""
    labels = {"Special Leave": "pto", "PTO": "pto", "Holiday": "pto", "Eid Holiday": "pto",
              "Holiday: Day in Lieu for Eid": "pto", "In lieu of June 15 PH": "pto", "On leave": "pto",
              "out sick": "sick", "Sick Leave": "sick", "SL": "sick"}
    days = days_from(date(2026, 10, 5), len(labels))
    plan = planned(team, [shift(PAT, NASA, day, "08:00", "17:00", label) for day, label in zip(days, labels)])
    assert dict(zip(labels, read_as(plan, PAT, days))) == labels
    assert plan.unrecognised == ()


def test_leave_in_notes(team):
    """Leave written only in the Notes column counts as in the label; a note with no leave word changes nothing."""
    rows = [shift(EMMA, EMEA, date(2026, 10, 5), "00:00", "09:00", "", " PTO "),
            shift(EMMA, EMEA, date(2026, 10, 6), "00:00", "09:00", "", "Sick Leave"),
            shift(EMMA, EMEA, date(2026, 10, 7), "00:00", "09:00", "9AM - 6PM DXB", "Chats 8am - 1pm")]
    shift_rows, off_rows = read_workbook(workbook(rows), "schedule.xlsx", CHICAGO)
    assert [(r.label, r.note) for r in shift_rows] == [("", "PTO"), ("", "Sick Leave"),
                                                       ("9AM - 6PM DXB", "Chats 8am - 1pm")]
    plan = plan_import(team, shift_rows, off_rows)
    assert read_as(plan, EMMA, days_from(date(2026, 10, 5), 3)) == ["pto", "sick", None]
    assert tech_plan(plan, EMMA).entries == (version(EMMA, date(2026, 10, 5), {1, 2, 3}, 540, 1080),)
    assert plan.unrecognised == ()


def test_leave_in_notes_under_a_time_label(team):
    """Ruling 17: a label that starts with a time keeps only its own words from making the day leave; a leave word in
    its notes still does. Only a half day is a work day whatever the notes say."""
    rows = [shift(EMMA, EMEA, date(2026, 10, 5), "00:00", "09:00", "9AM - 6PM DXB", "PTO"),
            shift(EMMA, EMEA, date(2026, 10, 6), "00:00", "09:00", "9AM - 12PM", "Half day in lieu for Sunday duty"),
            shift(EMMA, EMEA, date(2026, 10, 7), "00:00", "09:00", "9AM - 6PM DXB", "Chats 8am - 1pm"),
            shift(EMMA, EMEA, date(2026, 10, 8), "00:00", "09:00", "730AM - 230PM (Ramadan)", "Sick Leave"),
            shift(EMMA, EMEA, date(2026, 10, 9), "00:00", "09:00", "11AM - 6PM (Annual Leave)")]
    plan = planned(team, rows)
    assert read_as(plan, EMMA, days_from(date(2026, 10, 5), 5)) == ["pto", None, None, "sick", None]
    assert plan.unrecognised == ()


def test_half_at_a_word_start(team):
    """F3: "half" counts at a word's start ("Halfday", "Half-day", "half day"), not inside a word: "on behalf of" is
    no half day, so its leave word counts and, as a label, it is one the import doesn't know."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00")
    halves = {date(2026, 10, 5): ("Halfday", ""), date(2026, 10, 6): ("Half-day", ""),
              date(2026, 10, 7): ("half day", ""), date(2026, 10, 8): ("Annual Leave", "Approved on behalf of Lee"),
              date(2026, 10, 9): ("Cover on behalf of Sam", "")}
    for day, (label, note) in halves.items():
        rows[(day - OCT_4).days] = shift(PAT, NASA, day, "08:00", "17:00", label, note)
    plan = planned(team, rows)
    assert read_as(plan, PAT, days_from(date(2026, 10, 5), 5)) == [None, None, None, "pto", None]
    assert plan.unrecognised == (("Cover on behalf of Sam", 1),)


def test_half_days_are_work_days(team):
    """A label or note with "half" at a word's start is a working shift, never leave, whatever leave word it also
    has; its weekday is a work day of its week."""
    halves = [("Half day", ""), ("On Half-day Leave", ""), ("Half Day SL", ""), ("Half Day Sick Leave", ""),
              ("", "half day - Leave"), ("", "Halfday: 2PM - 6PM DXB"), ("9AM - 12PM (Half Day)", "")]
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "08:00", "17:00")
    days = days_from(date(2026, 10, 5), 5) + days_from(date(2026, 10, 12), 2)   # Mon-Fri, then Mon and Tue
    for day, (label, note) in zip(days, halves):
        rows[(day - OCT_4).days] = shift(PAT, NASA, day, "08:00", "13:00", label, note)
    plan = planned(team, rows)
    pat = tech_plan(plan, PAT)
    assert pat.pto == pat.sick == ()
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 780), version(PAT, OCT_11, MON_FRI, 480, 1020)), \
        "a half day is a working shift at its own hours, its weekday a work day"
    assert plan.unrecognised == (), "a half day is a label the import knows"


def test_time_labels_and_clients_are_work(team):
    """A label that starts with a time, a client's name or anything else with no leave word is a working shift. The
    labels the import doesn't know (not blank, a time, a half day, a rest day or leave) are listed, most common
    first, so the owner can check them."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "08:00", "17:00")
    labels = {date(2026, 10, 5): "11AM - 6PM (Ramadan)", date(2026, 10, 6): "6PM-3AM PH time",
              date(2026, 10, 7): "Contoso PH", date(2026, 10, 8): "Training", date(2026, 10, 12): "Training",
              date(2026, 10, 13): "Laptop setup"}   # "pto" inside a word is no leave word
    for day, label in labels.items():
        rows[(day - OCT_4).days] = shift(PAT, NASA, day, "08:00", "17:00", label)
    plan = planned(team, rows)
    pat = tech_plan(plan, PAT)
    assert pat.pto == pat.sick == ()
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020),)
    assert plan.unrecognised == (("Training", 2), ("Contoso PH", 1), ("Laptop setup", 1))


def test_time_labels_without_a_colon(team):
    """F4: a label that starts with a time written without a colon ("730AM - 230PM (Ramadan)") starts with a time, as
    "9AM - 6PM DXB" does, so it isn't listed; a client's name or "Training" still is."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "08:00", "17:00")
    labels = {date(2026, 10, 5): "730AM - 230PM (Ramadan)", date(2026, 10, 6): "1130PM-830AM",
              date(2026, 10, 7): "9AM - 6PM DXB", date(2026, 10, 8): "11AM - 6PM (Ramadan)",
              date(2026, 10, 9): "6PM-3AM PH time", date(2026, 10, 12): "8AM-5PM(PH) / 4AM-1PM(DXB)",
              date(2026, 10, 13): "Contoso PH", date(2026, 10, 14): "Training"}
    for day, label in labels.items():
        rows[(day - OCT_4).days] = shift(PAT, NASA, day, "08:00", "17:00", label)
    plan = planned(team, rows)
    pat = tech_plan(plan, PAT)
    assert pat.pto == pat.sick == ()
    assert plan.unrecognised == (("Contoso PH", 1), ("Training", 1))


def test_only_the_imported_rows_labels_are_listed(team):
    """The list holds the labels of the rows the import reads as working shifts: not those of people skipped, of
    rows outside the three groups, or of a row whose notes make it leave."""
    rows = [shift(PAT, NASA, date(2026, 10, 5), "08:00", "17:00", "Training", "PTO"),
            shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00", "Annual Leave", "half day"),
            shift(PAT, "Leads Team", date(2026, 10, 7), "08:00", "17:00", "Leads sync"),
            shift(LEE, NASA, date(2026, 10, 5), "08:00", "17:00", "Interviews")]
    plan = planned(team, rows)
    assert read_as(plan, PAT, days_from(date(2026, 10, 5), 2)) == ["pto", None]
    assert plan.unrecognised == ()


def test_timed_time_off(team):
    """A timed Time Off entry of six hours or more on one day of the tech's clock is a full day of leave: Andy's APAC
    Annual Leave from Chicago Mon Mar 2 15:00 to midnight is his Manila Tue Mar 3, 04:00 to 13:00. A shorter one is a
    half day, a work day: nothing. An all-day one keeps its written dates."""
    rows = week(ANDY, APAC, date(2026, 2, 28), "15:00", "00:00") + week(PAT, NASA, date(2026, 3, 1), "08:00", "17:00")
    offs = [time_off(ANDY, date(2026, 3, 2), "15:00", date(2026, 3, 3), "00:00", "Annual Leave"),
            time_off(ANDY, date(2026, 3, 5), "00:00", date(2026, 3, 6), "00:00", "Annual Leave"),
            time_off(PAT, date(2026, 3, 3), "08:00", date(2026, 3, 3), "17:00", "Annual Leave"),
            time_off(PAT, date(2026, 3, 4), "08:00", date(2026, 3, 4), "14:00", "Annual Leave"),   # six hours
            time_off(PAT, date(2026, 3, 5), "13:00", date(2026, 3, 5), "17:00", "Sick Leave")]    # four hours
    shift_rows, off_rows = read_workbook(workbook(rows, offs), "schedule.xlsx", CHICAGO)
    assert [(o.email, o.kind) for o in off_rows] == [(ANDY[1], "pto"), (ANDY[1], "pto"), (PAT[1], "pto"),
                                                     (PAT[1], "pto")], "four hours is no time off"
    plan = plan_import(team, shift_rows, off_rows)
    assert tech_plan(plan, ANDY).pto == (date(2026, 3, 3), date(2026, 3, 5))
    pat = tech_plan(plan, PAT)
    assert (pat.pto, pat.sick) == ((date(2026, 3, 3), date(2026, 3, 4)), ())


def test_timed_time_off_over_several_days(team):
    """Ruling 16: a timed Time Off entry covers each day, on the tech's clock, that it overlaps by six hours or more.
    Pat's NASA Annual Leave from Mon 09:00 to Wed 18:00 is Mon, Tue and Wed; his Sick Leave from Thu 20:00 to Sat 03:00
    is only Friday (four hours on Thursday, three on Saturday). Andy's APAC leave from Chicago Mon 15:00 to Wed 00:00
    is Manila's Tue 04:00 to Wed 13:00, so his Tue and Wed; his 9-hour one from Chicago Thu 15:00 is his Manila Fri."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(ANDY, APAC, date(2026, 10, 3), "15:00", "00:00")
    offs = [time_off(PAT, date(2026, 10, 5), "09:00", date(2026, 10, 7), "18:00", "Annual Leave"),
            time_off(PAT, date(2026, 10, 8), "20:00", date(2026, 10, 10), "03:00", "Sick Leave"),
            time_off(ANDY, date(2026, 10, 5), "15:00", date(2026, 10, 7), "00:00", "Annual Leave"),
            time_off(ANDY, date(2026, 10, 8), "15:00", date(2026, 10, 9), "00:00", "Annual Leave")]
    shift_rows, off_rows = read_workbook(workbook(rows, offs), "schedule.xlsx", CHICAGO)
    chicago = ZoneInfo(CHICAGO)
    assert [(o.first, o.last, o.start, o.end) for o in off_rows[:2]] == [
        (date(2026, 10, 5), date(2026, 10, 7), datetime(2026, 10, 5, 9, 0, tzinfo=chicago),
         datetime(2026, 10, 7, 18, 0, tzinfo=chicago)),
        (date(2026, 10, 8), date(2026, 10, 10), datetime(2026, 10, 8, 20, 0, tzinfo=chicago),
         datetime(2026, 10, 10, 3, 0, tzinfo=chicago))], "a timed entry keeps its start and end"
    plan = plan_import(team, shift_rows, off_rows)
    pat = tech_plan(plan, PAT)
    assert pat.pto == (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7))
    assert pat.sick == (date(2026, 10, 9),)
    assert tech_plan(plan, ANDY).pto == (date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 9))


def test_timed_time_off_on_the_day_a_region_begins(team):
    """F3: a timed Time Off entry's day and region come from its start on the tech's clock, not from its Chicago
    date. Sam moves from NASA to APAC on Thursday Oct 8, his first Manila shift; his leave starting Chicago Wednesday
    15:00 is Manila Thursday 04:00, after his APAC days began, so it is his Oct 8."""
    rows = [shift(SAM, NASA, date(2026, 10, 5), "08:00", "17:00"),
            shift(SAM, NASA, date(2026, 10, 6), "08:00", "17:00"),
            shift(SAM, APAC, date(2026, 10, 7), "15:00", "00:00"),    # Manila Thu Oct 8 04:00
            shift(SAM, APAC, date(2026, 10, 8), "15:00", "00:00")]
    offs = [time_off(SAM, date(2026, 10, 7), "15:00", date(2026, 10, 8), "00:00", "Annual Leave")]
    sam = tech_plan(planned(team, rows, offs), SAM)
    assert sam.regions == ((date(2026, 10, 5), "NASA"), (date(2026, 10, 8), "APAC"))
    assert sam.pto == (date(2026, 10, 8),)


def test_region_swap(team):
    rows = [shift(SAM, APAC, date(2026, 3, 29), "15:00", "00:00"),   # Manila Mon Mar 30
            shift(SAM, APAC, date(2026, 3, 30), "15:00", "00:00"),   # Manila Tue Mar 31
            shift(SAM, NASA, date(2026, 4, 1), "08:00", "17:00"),
            shift(SAM, NASA, date(2026, 4, 2), "08:00", "17:00")]
    plan = planned(team, rows)
    assert plan.first == date(2026, 3, 30)
    assert tech_plan(plan, SAM).regions == ((date(2026, 3, 30), "APAC"), (date(2026, 4, 1), "NASA"))


def test_regions_outside_the_dates_are_kept(team):
    """Region changes before and after the imported dates stay; those inside them are replaced."""
    regions.set_regions(team, PAT[1], [(date(2025, 1, 1), "EMEA"), (date(2026, 10, 6), "APAC"),
                                       (date(2026, 12, 1), "EMEA")])
    pat = tech_plan(planned(team, week(PAT, NASA, OCT_4, "08:00", "17:00")), PAT)
    assert pat.regions == ((date(2025, 1, 1), "EMEA"), (OCT_4, "NASA"), (date(2026, 12, 1), "EMEA"))
    assert regions.region_history(team, PAT[1]) == [(date(2025, 1, 1), "EMEA"), (date(2026, 10, 6), "APAC"),
                                                    (date(2026, 12, 1), "EMEA")], "planning writes nothing"


def test_only_roster_techs(team):
    open_shift = ["", None, NASA, _written(date(2026, 10, 5)), "08:00", _written(date(2026, 10, 5)), "17:00",
                  "1. White", "", 0, "", "Shared"]
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + [shift(LEE, NASA, date(2026, 10, 5), "08:00", "17:00"),
                                                        open_shift]
    offs = [time_off(KIM, date(2026, 10, 6), "00:00", date(2026, 10, 7), "00:00", "Annual Leave")]
    shift_rows, off_rows = read_workbook(workbook(rows, offs), "schedule.xlsx", CHICAGO)
    assert len(shift_rows) == 8, "an open shift (no Work Email) is skipped"
    plan = plan_import(team, shift_rows, off_rows)
    assert plan.skipped == ("Kim Gone", "Lee Lead")
    assert [t.email for t in plan.techs] == [PAT[1]]


def test_started_only_for_a_late_joiner(team):
    rows = [shift(PAT, NASA, date(2025, 9, 14), "08:00", "17:00", "Rest Day"),   # the files' first week: Sep 14
            shift(PAT, NASA, date(2025, 9, 15), "08:00", "17:00"),
            shift(EMMA, EMEA, date(2025, 9, 17), "00:00", "09:00"),               # later that week: not late
            shift(PAT, NASA, date(2026, 2, 2), "08:00", "17:00"),
            shift(SAM, NASA, date(2026, 2, 2), "08:00", "17:00")]
    plan = planned(team, rows)
    sam = tech_plan(plan, SAM)
    assert sam.started == date(2026, 2, 2)
    assert sam.entries == (version(SAM, date(2026, 2, 2), {1}, 480, 1020),)
    assert tech_plan(plan, PAT).started is None
    assert tech_plan(plan, EMMA).started is None


def test_before_the_schedule(team):
    """One pattern (a placeholder) before the imported dates is replaced by the first imported pattern from that
    entry's date; none, from the first imported date; two or more patterns are kept, and the preview says so."""
    store.add_shift_version(team, version(PAT, date(2025, 10, 17), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_shift_version(team, version(SAM, date(2025, 6, 1), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_shift_version(team, version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080))
    rows = (week(PAT, NASA, OCT_4, "08:00", "17:00") + week(SAM, NASA, OCT_4, "08:00", "17:00")
            + week(EMMA, EMEA, OCT_4, "00:00", "09:00"))
    plan = planned(team, rows)
    pat, sam, emma = tech_plan(plan, PAT), tech_plan(plan, SAM), tech_plan(plan, EMMA)
    assert pat.replace_before and pat.entries == (version(PAT, date(2025, 10, 17), MON_FRI, 480, 1020),)
    assert emma.replace_before and emma.entries == (version(EMMA, OCT_4, MON_FRI, 540, 1080),)
    assert not sam.replace_before and sam.entries == (version(SAM, OCT_4, MON_FRI, 480, 1020),)
    assert len(sam.problems) == 1
    assert "2025-06-01" in sam.problems[0] and "2026-03-01" in sam.problems[0]
    assert "Sun-Thu 08:00-17:00" in sam.problems[0] and "Mon-Fri 09:00-18:00" in sam.problems[0]
    assert len(store.shift_versions(team, SAM[1])) == 2, "planning writes nothing"


def test_a_kept_history_keeps_its_started(team):
    """A tech whose recorded history is kept has no Started set, even when the files show them first a week
    late: a Started would delete that history."""
    store.add_shift_version(team, version(SAM, date(2025, 6, 1), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_shift_version(team, version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080))
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(SAM, NASA, OCT_11, "08:00", "17:00")
    sam = tech_plan(planned(team, rows), SAM)
    assert not sam.replace_before and sam.started is None
    assert sam.entries == (version(SAM, OCT_11, MON_FRI, 480, 1020),)


PLACEHOLDER_FROM = date(2025, 6, 1)
NOV_6 = date(2026, 11, 6)   # a Friday


def placeholder(tech):
    """One hand-entered pattern, Sun-Thu 8 to 5, from June 1, 2025."""
    return version(tech, PLACEHOLDER_FROM, {0, 1, 2, 3, 4}, 480, 1020)


def activity_from(conn, window_start="2025-10-22T05:00:00Z"):
    """A completed sync whose stored Team activity begins at `window_start` (midnight Oct 22, 2025 in Chicago)."""
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, finished_at, synced_through, window_start)"
                 " VALUES ('daily', 'complete', ?, ?, ?, ?)", ("2026-11-20T11:00:00Z",) * 3 + (window_start,))
    conn.commit()


def test_a_later_months_import_keeps_the_history_before_it(team):
    """Ruling 18: files whose first date (Friday Nov 6, 2026) is after the first day the stored Team activity covers
    (Oct 22, 2025) don't reach back. Every entry before them stays as it is, one pattern or two, and no problem says
    so; no Started is set; the first imported entry is dated at the first imported date."""
    activity_from(team)
    store.add_shift_version(team, placeholder(PAT))
    store.add_shift_version(team, placeholder(SAM))
    store.add_shift_version(team, version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080))
    rows = [shift(PAT, NASA, NOV_6, "08:00", "17:00"),
            shift(PAT, NASA, date(2026, 11, 7), "08:00", "17:00", "Rest Day"),
            *week(PAT, NASA, date(2026, 11, 8), "08:00", "17:00"),
            *week(SAM, NASA, date(2026, 11, 8), "08:00", "17:00")]
    plan, _ = applied(team, rows)
    pat, sam = tech_plan(plan, PAT), tech_plan(plan, SAM)
    assert plan.first == NOV_6
    assert not pat.replace_before and pat.started is None and pat.problems == ()
    assert pat.entries == (version(PAT, NOV_6, {5}, 480, 1020), version(PAT, date(2026, 11, 8), MON_FRI, 480, 1020))
    assert store.shift_versions(team, PAT[1]) == [placeholder(PAT), *pat.entries], "the entry before is untouched"
    assert not sam.replace_before and sam.started is None and sam.problems == ()
    assert store.shift_versions(team, SAM[1]) == [placeholder(SAM), version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080),
                                                  version(SAM, date(2026, 11, 8), MON_FRI, 480, 1020)]


def test_an_import_that_reaches_back_replaces_the_history_before_it(team):
    """Ruling 18: files from Oct 1, 2025, on or before the first day the stored Team activity covers (Oct 22, 2025),
    reach back: the one pattern before them is replaced, from where it began."""
    activity_from(team)
    store.add_shift_version(team, placeholder(PAT))
    rows = (week(PAT, NASA, date(2025, 9, 28), "08:00", "17:00")[3:]   # from Wednesday Oct 1
            + week(PAT, NASA, date(2025, 10, 5), "08:00", "17:00"))
    pat = tech_plan(planned(team, rows), PAT)
    assert pat.replace_before and pat.started is None and pat.problems == ()
    assert pat.entries[0] == version(PAT, PLACEHOLDER_FROM, MON_FRI, 480, 1020)


def test_a_late_joiner_in_a_later_months_file_gets_no_started(team):
    """Ruling 18: only an import that reaches back sets Started. Sam's first shift is a week after the files' first
    week: with no activity stored the files reach back and he is a late joiner; with activity stored from Oct 22,
    2025, these November files don't, and his Started stays as it is."""
    rows = week(PAT, NASA, date(2026, 11, 8), "08:00", "17:00") + week(SAM, NASA, date(2026, 11, 15), "08:00", "17:00")
    assert tech_plan(planned(team, rows), SAM).started == date(2026, 11, 16)
    activity_from(team)
    sam = tech_plan(planned(team, rows), SAM)
    assert sam.started is None and not sam.replace_before
    assert sam.entries == (version(SAM, date(2026, 11, 15), MON_FRI, 480, 1020),)


def test_the_pattern_dated_back_is_the_first_whole_weeks(team):
    """Files that begin on Wednesday Oct 1, 2025 cut their first week: a Mon-Fri tech's days before the schedule are
    Mon-Fri, the first whole imported week's (the first week whose Sunday is on or after Oct 1), not that cut week's
    Wed-Fri. The cut week keeps its own entry for its own dates, Wed to Sat. With no whole week in the files, the cut
    week's pattern is the one dated back."""
    store.add_shift_version(team, placeholder(PAT))
    cut = week(PAT, NASA, date(2025, 9, 28), "08:00", "17:00")[3:]   # Wed Oct 1 to Sat Oct 4
    only_cut = tech_plan(planned(team, cut), PAT)
    assert only_cut.entries == (version(PAT, PLACEHOLDER_FROM, {3, 4, 5}, 480, 1020),)
    plan, _ = applied(team, cut + week(PAT, NASA, date(2025, 10, 5), "08:00", "17:00"))
    pat = tech_plan(plan, PAT)
    assert pat.replace_before
    assert pat.entries == (version(PAT, PLACEHOLDER_FROM, MON_FRI, 480, 1020),
                           version(PAT, date(2025, 10, 1), {3, 4, 5}, 480, 1020),
                           version(PAT, date(2025, 10, 5), MON_FRI, 480, 1020))
    assert store.shift_versions(team, PAT[1]) == list(pat.entries)


def test_a_shift_crossing_midnight_is_a_problem(team):
    """A shift that crosses midnight on the tech's clock is reported, and its week is left as the week before."""
    middle = week(PAT, NASA, OCT_11, "09:00", "18:00")
    middle[5] = shift(PAT, NASA, date(2026, 10, 16), "20:00", "04:00")
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + middle + week(PAT, NASA, OCT_18, "10:00", "19:00")
    pat = tech_plan(planned(team, rows), PAT)
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020), version(PAT, OCT_18, MON_FRI, 600, 1140))
    assert len(pat.problems) == 1 and "2026-10-16" in pat.problems[0] and "midnight" in pat.problems[0]


def test_rows_outside_the_three_groups_are_problems(team):
    rows = (week(PAT, NASA, OCT_4, "08:00", "17:00") + [shift(PAT, "Leads Team", date(2026, 10, 12), "08:00", "17:00")]
            + [shift(EMMA, "Leads Team", date(2026, 10, 5), "08:00", "17:00")])
    plan = planned(team, rows)
    pat = tech_plan(plan, PAT)
    assert pat.entries == (version(PAT, OCT_4, MON_FRI, 480, 1020),), "the Leads Team row is skipped"
    assert len(pat.problems) == 1 and "Leads Team" in pat.problems[0]
    assert [t.email for t in plan.techs] == [PAT[1]]
    assert len(plan.problems) == 1 and "Emma Emea" in plan.problems[0]


def test_techs_by_region_then_name(team):
    """Techs are listed by region (NASA, EMEA, APAC) then name; a tech's name is the app's, else the file's."""
    names.store(team, SAM[1], "Alex Swap")
    rows = (week(ANDY, APAC, date(2026, 10, 3), "15:00", "00:00") + week(EMMA, EMEA, OCT_4, "00:00", "09:00")
            + week(PAT, NASA, OCT_4, "08:00", "17:00") + week(SAM, NASA, OCT_4, "08:00", "17:00"))
    plan = planned(team, rows)
    assert [(t.name, t.regions[-1][1]) for t in plan.techs] == [
        ("Alex Swap", "NASA"), ("Pat Nasa", "NASA"), ("Emma Emea", "EMEA"), ("Andy Apac", "APAC")]


def test_not_a_shifts_export(team):
    with pytest.raises(ValueError) as caught:
        read_workbook(workbook(sheets=("Time Off",)), "october.xlsx", CHICAGO)
    assert str(caught.value) == "october.xlsx isn't a Teams Shifts export (no Shifts sheet)."
    with pytest.raises(ValueError) as caught:
        read_workbook(b"not a workbook", "notes.txt", CHICAGO)
    assert str(caught.value) == "notes.txt isn't a Teams Shifts export (no Shifts sheet)."


def test_dates_and_times_as_excel_values_read_the_same(team):
    """A workbook that holds real Excel dates and times reads as one holding the export's text."""
    as_text = shift(PAT, NASA, date(2026, 10, 5), "08:00", "17:00")
    as_values = list(as_text)
    as_values[3:7] = [datetime(2026, 10, 5), time(8, 0), datetime(2026, 10, 5), time(17, 0)]
    assert (read_workbook(workbook([as_values]), "a.xlsx", CHICAGO)
            == read_workbook(workbook([as_text]), "a.xlsx", CHICAGO))


def test_every_row_is_read_whatever_size_the_sheet_claims(team):
    """A workbook whose sheet claims to be smaller than it is (a wrong dimension tag) still gives every row."""
    source = zipfile.ZipFile(io.BytesIO(workbook(week(PAT, NASA, OCT_4, "08:00", "17:00"))))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                content, count = re.subn(rb'<dimension ref="[^"]*"', b'<dimension ref="A1:L2"', content)
                assert count == 1
            target.writestr(item, content)
    rows, _ = read_workbook(buffer.getvalue(), "schedule.xlsx", CHICAGO)
    assert len(rows) == 7


def test_preview_json_shape(team):
    rows = [shift(ANDY, APAC, date(2026, 10, 3), "15:00", "00:00", "Rest Day"),
            shift(ANDY, APAC, date(2026, 10, 7), "15:00", "00:00", "Training"),
            shift(LEE, NASA, date(2026, 10, 5), "08:00", "17:00")]
    offs = [time_off(ANDY, date(2026, 10, 9), "00:00", date(2026, 10, 10), "00:00", "Sick Leave")]
    got = preview_json(planned(team, rows, offs))
    json.dumps(got)
    assert set(got) == {"first", "last", "techs", "skipped", "problems", "unrecognised"}
    assert (got["first"], got["last"], got["skipped"], got["problems"]) == ("2026-10-04", "2026-10-08",
                                                                            ["Lee Lead"], [])
    assert got["unrecognised"] == [{"label": "Training", "count": 1}]
    andy = got["techs"][0]
    assert set(andy) == {"email", "name", "regions", "changes", "started", "replace_before", "pto", "sick",
                         "problems"}
    assert andy["regions"] == [{"since": "2026-10-04", "region": "APAC"}]
    assert andy["changes"] == [{"since": "2026-10-04", "days": [4], "start": "04:00", "end": "13:00"}]
    assert (andy["email"], andy["name"], andy["started"], andy["replace_before"]) == (ANDY[1], "Andy Apac", None,
                                                                                      True)
    assert (andy["pto"], andy["sick"], andy["problems"]) == ([], ["2026-10-09"], [])


# -- applying a plan (Task 2) -------------------------------------------------------------------------------------


def applied(conn, shifts=(), offs=()):
    """Plan the rows and write the plan, as Apply does: the plan and what `apply_import` answered."""
    plan = planned(conn, shifts, offs)
    return plan, apply_import(conn, plan)


def imported_time_off(conn, tech) -> list[tuple]:
    return [(o.first, o.last, o.kind) for o in store.time_off(conn, tech[1]) if o.note == "Teams Shifts"]


def tables(conn) -> dict:
    """Every table an import writes, row for row as stored: time off with its ids, and the ids used so far."""
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in ("shift_versions", "time_off", "tech_regions", "sqlite_sequence")}


def test_apply_writes_the_plan(team):
    """Each tech's region history, shift entries, Started and leave, as planned: Pat's placeholder gives way to his
    first imported pattern, Andy (a late joiner) starts on his first Manila shift, and leave becomes Time off entries
    noted "Teams Shifts", consecutive days of one kind as one."""
    store.add_shift_version(team, version(PAT, date(2025, 10, 17), {0, 1, 2, 3, 4}, 480, 1020))
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "08:00", "17:00")
    rows[2] = shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00", "Annual Leave")
    rows += week(ANDY, APAC, date(2026, 10, 10), "15:00", "00:00")   # Manila Sun Oct 11 to Sat Oct 17
    offs = [time_off(PAT, date(2026, 10, 14), "00:00", date(2026, 10, 16), "00:00", "Sick Leave")]
    plan, written = applied(team, rows, offs)
    assert tech_plan(plan, ANDY).started == date(2026, 10, 12)
    assert written == {"techs": 2, "entries": 2, "time_off": 2}
    assert regions.region_history(team, PAT[1]) == [(OCT_4, "NASA")]
    assert regions.region_history(team, ANDY[1]) == [(OCT_11, "APAC")]
    assert store.shift_versions(team, PAT[1]) == [version(PAT, date(2025, 10, 17), MON_FRI, 480, 1020)]
    assert store.shift_versions(team, ANDY[1]) == [version(ANDY, date(2026, 10, 12), MON_FRI, 240, 780)]
    assert imported_time_off(team, PAT) == [(date(2026, 10, 6), date(2026, 10, 6), "pto"),
                                            (date(2026, 10, 14), date(2026, 10, 15), "sick")]
    assert store.time_off(team, ANDY[1]) == []


def test_apply_keeps_the_history_before_and_the_entries_after(team):
    """A recorded history (two patterns) before the imported dates and an entry after them stay; an entry inside them
    is replaced."""
    store.add_shift_version(team, version(SAM, date(2025, 6, 1), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_shift_version(team, version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080))
    store.add_shift_version(team, version(SAM, date(2026, 10, 8), {2, 3}, 600, 1140))   # inside: replaced
    store.add_shift_version(team, version(SAM, date(2026, 12, 6), {1, 2, 3}, 600, 1140))   # after: kept
    applied(team, week(SAM, NASA, OCT_4, "08:00", "17:00") + week(SAM, NASA, OCT_11, "08:00", "17:00"))
    assert store.shift_versions(team, SAM[1]) == [
        version(SAM, date(2025, 6, 1), {0, 1, 2, 3, 4}, 480, 1020), version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080),
        version(SAM, OCT_4, MON_FRI, 480, 1020), version(SAM, date(2026, 12, 6), {1, 2, 3}, 600, 1140)]


def test_a_leads_own_time_off_survives_two_applies(team):
    """A lead's own Time off entry inside the imported dates is never changed or removed, and a leave day it covers
    gets no entry of the import's (Review Focus 3)."""
    own_id = store.add_time_off(team, PAT[1], date(2026, 10, 6), date(2026, 10, 7), "dentist", "pto")
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00")
    rows[3] = shift(PAT, NASA, date(2026, 10, 7), "08:00", "17:00", "Annual Leave")
    rows[4] = shift(PAT, NASA, date(2026, 10, 8), "08:00", "17:00", "Annual Leave")
    for _ in range(2):
        applied(team, rows)
    assert store.time_off(team, PAT[1]) == [
        TimeOff(own_id, PAT[1], date(2026, 10, 6), date(2026, 10, 7), "dentist", "pto"),
        TimeOff(own_id + 1, PAT[1], date(2026, 10, 8), date(2026, 10, 8), "Teams Shifts", "pto")]


def test_applying_the_same_files_twice_changes_nothing(team):
    """The same files applied again leave every table as the first apply left it, row for row and id for id (Review
    Focus 4): a kept history, a replaced placeholder, a late joiner, a region swap, leave running past the imported
    dates, timed leave on the tech's clock, a lead's own entry and an earlier import's entry outside the dates."""
    store.add_shift_version(team, version(SAM, date(2025, 6, 1), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_shift_version(team, version(SAM, date(2026, 3, 1), MON_FRI, 540, 1080))
    store.add_shift_version(team, version(PAT, date(2025, 10, 17), {0, 1, 2, 3, 4}, 480, 1020))
    store.add_time_off(team, PAT[1], date(2026, 10, 13), date(2026, 10, 13), "dentist", "pto")
    store.add_time_off(team, PAT[1], date(2026, 9, 1), date(2026, 9, 2), "Teams Shifts", "pto")
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "09:00", "18:00")
    rows[2] = shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00", "Annual Leave")
    rows += (week(SAM, NASA, OCT_4, "08:00", "17:00") + week(SAM, APAC, date(2026, 10, 10), "15:00", "00:00")
             + week(ANDY, APAC, date(2026, 10, 10), "15:00", "00:00"))
    offs = [time_off(PAT, date(2026, 10, 15), "00:00", date(2026, 10, 21), "00:00", "Annual Leave"),
            time_off(ANDY, date(2026, 10, 13), "15:00", date(2026, 10, 14), "00:00", "Sick Leave")]
    first_plan, _ = applied(team, rows, offs)
    once = tables(team)
    assert imported_time_off(team, PAT) == [(date(2026, 9, 1), date(2026, 9, 2), "pto"),
                                            (date(2026, 10, 6), date(2026, 10, 6), "pto"),
                                            (date(2026, 10, 15), date(2026, 10, 20), "pto")]
    assert imported_time_off(team, ANDY) == [(date(2026, 10, 14), date(2026, 10, 14), "sick")], "Manila's Oct 14"
    second_plan, _ = applied(team, rows, offs)
    assert tables(team) == once
    assert preview_json(second_plan) == preview_json(first_plan)


def test_a_reimport_replaces_its_own_time_off_on_its_dates(team):
    """An import replaces an earlier import's Time off entries on its dates, so leave moved in Shifts moves here too.
    An earlier import's days outside the dates stay, and so does every other entry."""
    outside = store.add_time_off(team, PAT[1], date(2026, 9, 1), date(2026, 9, 2), "Teams Shifts", "pto")
    store.add_time_off(team, PAT[1], date(2026, 10, 1), date(2026, 10, 5), "Teams Shifts", "sick")   # into the dates
    lead = store.add_time_off(team, PAT[1], date(2026, 10, 9), date(2026, 10, 9), "dentist", "pto")
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(PAT, NASA, OCT_11, "08:00", "17:00")
    rows[2] = shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00", "Annual Leave")
    applied(team, rows)
    assert imported_time_off(team, PAT) == [(date(2026, 9, 1), date(2026, 9, 2), "pto"),
                                            (date(2026, 10, 1), date(2026, 10, 3), "sick"),
                                            (date(2026, 10, 6), date(2026, 10, 6), "pto")]
    rows[2] = shift(PAT, NASA, date(2026, 10, 6), "08:00", "17:00")
    rows[9] = shift(PAT, NASA, date(2026, 10, 13), "08:00", "17:00", "Annual Leave")   # moved to the next Tuesday
    applied(team, rows)
    assert imported_time_off(team, PAT) == [(date(2026, 9, 1), date(2026, 9, 2), "pto"),
                                            (date(2026, 10, 1), date(2026, 10, 3), "sick"),
                                            (date(2026, 10, 13), date(2026, 10, 13), "pto")]
    kept = store.time_off(team, PAT[1])
    assert kept[0].id == outside and [o.id for o in kept if o.note == "dentist"] == [lead]


def test_each_tech_is_one_transaction(team, monkeypatch):
    """A tech's writes are committed together: a failure while writing Andy's leave leaves nothing of his written,
    and Pat, written before him, is whole."""
    rows = week(PAT, NASA, OCT_4, "08:00", "17:00") + week(ANDY, APAC, date(2026, 10, 3), "15:00", "00:00")
    offs = [time_off(ANDY, date(2026, 10, 7), "00:00", date(2026, 10, 8), "00:00", "Sick Leave")]
    plan = planned(team, rows, offs)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "add_time_off", broken)
    with pytest.raises(sqlite3.OperationalError):
        apply_import(team, plan)
    assert not team.in_transaction, "what is left is committed"
    assert store.shift_versions(team, PAT[1]) == [version(PAT, OCT_4, MON_FRI, 480, 1020)]
    assert regions.region_history(team, PAT[1]) == [(OCT_4, "NASA")]
    assert store.shift_versions(team, ANDY[1]) == [] and regions.region_history(team, ANDY[1]) == []
