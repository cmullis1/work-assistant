"""The synthetic demo database behind `work-assistant serve --demo`."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from work_assistant import demo, periods, settings, timeutil
from work_assistant.activity import trends
from work_assistant.db import connect
from work_assistant.settings import get_setting


def test_build_demo_db(tmp_path):
    path = tmp_path / "demo.db"
    demo.build_demo_db(path)
    conn = connect(path)
    try:
        team = get_setting(conn, "team")
        assert 4 <= len(team) <= 6
        assert all(email.endswith("@example.com") for email in team)
        agents = {r[0] for r in conn.execute("SELECT DISTINCT agent_email FROM contacts")}
        assert agents == set(team)

        statuses = [r[0] for r in conn.execute("SELECT status FROM runs ORDER BY run_id")]
        assert statuses.count("complete") >= 8
        assert statuses[-1] == "complete"  # no failure banner on a fresh demo

        weeks = conn.execute("SELECT COUNT(DISTINCT window_start) FROM runs WHERE kind = 'scheduled'").fetchone()[0]
        assert weeks == 8
        versions = conn.execute("SELECT COUNT(DISTINCT rubric_version_id) FROM reviews").fetchone()[0]
        assert versions >= 2
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE counted = 0").fetchone()[0] >= 2
        assert conn.execute("SELECT COUNT(*) FROM check_results WHERE override_outcome IS NOT NULL").fetchone()[0] >= 3
        assert conn.execute("SELECT COUNT(*) FROM check_results WHERE outcome = 'missed'").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE is_current = 1").fetchone()[0] >= 8 * 4 * 3
        assert conn.execute("SELECT SUM(actual_usd) FROM runs").fetchone()[0] > 0
    finally:
        conn.close()


def test_each_module_level_name_is_defined_once():
    """A constant defined twice at the module's top level leaves the first definition dead and the reader unsure
    which one holds (MON_FRI was, for the regions and the Shifts export)."""
    import ast
    from collections import Counter
    from pathlib import Path

    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    names = Counter(target.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                    if isinstance(target, ast.Name))
    assert [name for name, count in names.items() if count > 1] == []


def test_build_demo_db_replaces_an_existing_file(tmp_path):
    path = tmp_path / "demo.db"
    demo.build_demo_db(path)
    demo.build_demo_db(path)
    conn = connect(path)
    try:
        assert conn.execute("SELECT COUNT(DISTINCT window_start) FROM runs WHERE kind = 'scheduled'").fetchone()[0] == 8
    finally:
        conn.close()


def test_demo_has_a_finished_model_test(tmp_path):
    """The demo shows a finished Sonnet model test of about 8 calls, with checks that differ from the shown
    reviews and at least one owner override on a shown review it was compared with, so the comparison views
    have something to show."""
    from work_assistant.web import compare

    path = tmp_path / "demo.db"
    demo.build_demo_db(path)
    conn = connect(path)
    try:
        tests = compare.test_list(conn)
        assert len(tests) == 1
        test = tests[0]
        assert (test["model_label"], test["status"]) == ("Claude Sonnet 5.5", "complete")
        assert 6 <= test["calls"] <= 10 and test["cost_usd"] > 0
        report = compare.report(conn, test["run_id"])
        assert report["summary"]["calls"] == test["calls"]
        assert any(c["differing_checks"] for c in report["calls"]), "some checks differ"
        assert report["overrides"]["checks"] >= 1, "an override on the shown side"
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE is_current = 1 AND run_id = ?",
                            (test["run_id"],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_demo_activity_has_every_cell_kind(tmp_path):
    """Eyes needs every kind of day in the demo: worked, signed in with no activity, missed, PTO, a worked
    rest day, a rest day, and a long gap, with the special days recent enough for Last week / Last month."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from work_assistant import settings
    from work_assistant.activity import report
    from work_assistant.periods import Period

    path = tmp_path / "demo.db"
    demo.build_demo_db(path)
    conn = connect(path)
    try:
        today = datetime.now(ZoneInfo(settings.get_timezone(conn))).date()
        start = today - timedelta(weeks=13) + timedelta(days=1)
        period = Period(start=start, end=today, preset="custom", label="13 weeks", so_far=False)
        view = report.range_view(conn, period, None, today=today)
        assert len(view["rows"]) == len(demo.AGENTS)
        cells = [c for row in view["rows"] for c in row["cells"]]
        assert {"worked", "zero", "missed", "pto", "off", "rest"} <= {c["state"] for c in cells}
        cutoff = (today - timedelta(days=14)).isoformat()
        assert {"zero", "missed", "pto", "off"} <= {c["state"] for c in cells if c["day"] >= cutoff}
        assert any(f["kind"] == "long_gap" and c["day"] >= cutoff for c in cells for f in c["flags"])
        assert view["synced"]["at"] is not None and view["synced"]["status"] == "complete"
        assert all(t["email"].endswith("@example.com") for t in view["techs"])
        worked = [c for c in cells if c["state"] == "worked"]
        assert worked and all(c["shift"] == {"start": "08:00", "end": "17:00"} for c in worked)
    finally:
        conn.close()


def test_demo_activity_is_deterministic(tmp_path):
    rows = []
    for name in ("a.db", "b.db"):
        demo.build_demo_db(tmp_path / name)
        conn = connect(tmp_path / name)
        try:
            rows.append([tuple(r) for r in conn.execute(
                "SELECT email, ticket_id, at, kind, old, new FROM activity_actions ORDER BY event_id")])
        finally:
            conn.close()
    assert rows[0] and rows[0] == rows[1]


def test_a_demo_built_as_of_a_day_is_dated_from_it(tmp_path):
    """Final review F5 (and T6 F2): the sync, the open-ticket snapshot, the overrides and the rubric versions are
    dated from the build's `now`, never the real clock, and the sync covers the demo's first day onward."""
    from datetime import date, datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    from work_assistant import settings
    from work_assistant.activity import report, store
    from work_assistant.periods import Period

    now = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)      # noon in Chicago, a Monday
    path = tmp_path / "demo.db"
    demo.build_demo_db(path, now=now)
    conn = connect(path)
    try:
        zone = ZoneInfo(settings.get_timezone(conn))
        sync = store.latest_sync(conn)
        assert (sync["started_at"], sync["synced_through"]) == ("2026-10-19T17:00:00Z", "2026-10-19T17:00:00Z")
        today = now.astimezone(zone).date()
        first_day = today - timedelta(days=demo.ACTIVITY_WEEKS * 7 - 1)
        history_day = today - timedelta(days=demo.HISTORY_WEEKS * 7 - 1)   # the year before it (seed_history)
        assert sync["window_start"] == store.iso(datetime.combine(history_day, time(0), zone))
        assert {r[0] for r in conn.execute("SELECT captured_at FROM activity_open")} == {"2026-10-19T17:00:00Z"}
        overrides = {r[0][:10] for r in conn.execute(
            "SELECT override_at FROM check_results WHERE override_at IS NOT NULL")}
        assert overrides <= {"2026-10-17", "2026-10-18"}
        versions = [r[0] for r in conn.execute("SELECT created_at FROM rubric_versions ORDER BY version_id")]
        assert "2026-08-01" < versions[0] < versions[1] < "2026-10-19", "within the demo's weeks"
        assert settings.get_setting(conn, "retention_days") == 90
        purged = conn.execute("SELECT COUNT(*) FROM contacts WHERE purged_at IS NOT NULL").fetchone()[0]
        assert purged == 0
        view = report.range_view(conn, Period(start=first_day, end=today, preset="custom", label="13 weeks",
                                              so_far=False), None, today=today)
        cells = [c for row in view["rows"] for c in row["cells"]]
        assert "unsynced" not in {c["state"] for c in cells}, "the sync covers every demo day"
        todays = [c for c in cells if c["day"] == today.isoformat()]
        assert todays and not any(f["kind"] in ("early_finish", "missed", "no_activity")
                                  for c in todays for f in c["flags"]), "today is judged only up to noon"
    finally:
        conn.close()


NOW = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)      # noon on Monday Oct 19 in Chicago


def _trends(conn, today=date(2026, 10, 19)):
    """Last 8 weeks against the 8 before, as the Trends screen opens."""
    facts = {"today": today, "last_reviewed": today, "latest_week_with_data": None}
    period = periods.resolve("last_8_weeks", **facts)
    return trends.trends_view(conn, period, periods.comparison(period, "previous"), today=today)


def test_demo_has_17_weeks_of_activity(tmp_path):
    """Last 8 weeks and the 8 before it are both covered, with a few days to spare."""
    assert demo.ACTIVITY_WEEKS == 17
    demo.build_demo_db(tmp_path / "demo.db", now=NOW)
    conn = connect(tmp_path / "demo.db")
    try:
        tz = settings.get_timezone(conn)
        days = {timeutil.local_date(r[0], tz) for r in conn.execute("SELECT at FROM activity_actions")}
        today = NOW.astimezone(ZoneInfo(tz)).date()
        assert min(days) <= today - timedelta(days=17 * 7 - 1) + timedelta(days=6)
        assert max(days) == today
        body = _trends(conn, today)
        assert body["coverage_from"] is None and len(body["steps"]) == 16
        tickets = next(m for m in body["measures"] if m["key"] == "tickets_per_shift")
        assert all(v is not None for v in tickets["team"]["steps"]), "every one of the 16 weeks has shifts"
    finally:
        conn.close()


def test_demo_has_schedules_and_counts_preps(tmp_path):
    """Built as of Tue Oct 20, 2026 (`--demo-date 2026-10-20`): each invented tech but one has their own 1:1 schedule,
    on different weekdays (two on Wednesdays, a week apart). Nothing is stored: 1:1 Prep counts every scheduled tech's
    latest period from the demo's collected data with no failure, and each tech's past periods go back to where the
    collected data begins."""
    from work_assistant.activity import store as activity_store
    from work_assistant.oneonone import report

    now = demo.as_of(date(2026, 10, 20))
    today = date(2026, 10, 20)
    demo.build_demo_db(tmp_path / "demo.db", now=now)
    conn = connect(tmp_path / "demo.db")
    try:
        tz = settings.get_timezone(conn)
        emails = [email for _, email, _ in demo.AGENTS]
        schedules = settings.schedules(conn)
        assert {e: (s.date.isoformat(), s.weeks) for e, s in schedules.items()} == {
            emails[0]: ("2026-10-20", 2), emails[1]: ("2026-10-21", 2), emails[2]: ("2026-10-22", 2),
            emails[3]: ("2026-10-28", 2), emails[4]: ("2026-10-16", 2)}
        assert emails[5] not in schedules, "one tech has no 1:1 date set"
        days_of_cycle = [(s.date - date(2026, 10, 19)).days % 14 for s in schedules.values()]
        assert len(set(days_of_cycle)) == 5 and len({s.date.weekday() for s in schedules.values()}) == 4

        view = report.latest_view(conn, today=today, now=now)
        assert [(r["state"], r["day"], r["today"], r["soon"]) for r in view["rows"]] == [
            ("counted", "2026-10-20", True, None), ("counted", "2026-10-07", False, "Tomorrow"),
            ("counted", "2026-10-08", False, "In 2 days"), ("counted", "2026-10-14", False, None),
            ("counted", "2026-10-16", False, None), ("no_schedule", None, False, None)]
        assert [r["name"] for r in view["rows"]] == [name for name, _, _ in demo.AGENTS]
        assert view["synced"] == activity_store.iso(activity_store.synced_through(conn))
        for row in view["rows"][:5]:
            failed = {key: cell["reason"] for key, cell in row["cells"].items() if cell["text"] is None and cell["reason"]}
            assert failed == {}, (row["email"], failed)
            assert row["cells"]["calls"]["value"] > 0 and row["cells"]["solved"]["value"] > 0

        first = activity_store.synced_from(conn).astimezone(ZoneInfo(tz)).date()
        for email in schedules:
            past = report.tech_view(conn, email, None, today=today, now=now)["past"]
            starts = [date.fromisoformat(p["day"]) - timedelta(days=14) for p in past]
            assert len(past) >= 7 and min(starts) >= first > min(starts) - timedelta(days=14), email
    finally:
        conn.close()


def test_demo_collects_what_1_1_prep_counts(tmp_path):
    """Collected calls design: the demo invents what the hourly sync collects (Talk legs and calls, ratings, solved
    tickets and each tech's daily aging list), so 1:1 Prep counts the demo's numbers the way it counts real ones
    (`live.count_preps`). Built as of Tue Oct 20, 2026 (`--demo-date 2026-10-20`): every scheduled tech's latest 1:1 is
    counted with no dash, Avery's (today) has about her usual two weeks of calls, the queue is department-wide, calls
    carry their ticket's priority (some non-urgent ones run over 10 minutes), ratings carry reasons and comments, and
    aging tickets their last reply. Calls fall on the days Team activity has the tech working; a period that starts
    before the collected data has the reason in place of its numbers. Everything is invented."""
    from work_assistant.activity import collected
    from work_assistant.activity import store as activity_store
    from work_assistant.oneonone import attendance, cycle, live

    now = demo.as_of(date(2026, 10, 20))
    today = date(2026, 10, 20)
    demo.build_demo_db(tmp_path / "demo.db", now=now)
    conn = connect(tmp_path / "demo.db")
    try:
        tz = settings.get_timezone(conn)
        emails = [email for _, email, _ in demo.AGENTS]
        assert collected.agents(conn) == {email: 900001 + n for n, email in enumerate(emails)}
        first = activity_store.synced_from(conn)
        states = [collected.state(conn, kind) for kind in collected.KINDS]
        assert [(s.covered_from, s.through) for s in states] == [(first, now)] * len(collected.KINDS), \
            "every kind collected from the activity's first day to now"

        schedules = settings.schedules(conn)
        wanted = [(email, cycle.latest_day(schedules[email], today)) for email in emails if email in schedules]
        assert len(wanted) == 5
        counted = live.count_preps(conn, wanted, now=now)
        assert [c.tech.email for c in sorted(counted, key=lambda c: emails.index(c.tech.email))] == emails[:5]
        over = ratings = reasons = comments = replies = never = 0
        for prep in counted:
            tech, period = prep.tech, prep.period
            assert prep.failures == {} and tech.failures == {}, tech.email
            assert tech.calls > 0 and tech.solved > 0 and tech.aging is not None
            assert 100 * tech.calls / prep.queue < 10, "the queue is department-wide"
            answered = [i for i in tech.items if i.kind == "answered"]
            assert len(answered) == tech.calls and sum(i.talk_seconds for i in answered) == tech.talk_seconds
            assert all(3 * 60 <= i.talk_seconds <= 12 * 60 for i in answered), "talk 3 to 12 minutes"
            assert all(i.priority in ("low", "normal", "high", "urgent") for i in answered)
            over += sum(i.talk_seconds > 10 * 60 and i.priority != "urgent" for i in answered)
            rated = [i for i in tech.items if i.kind == "rating"]
            assert (sum(i.score == "good" for i in rated), sum(i.score == "bad" for i in rated)) == \
                (tech.ratings_good, tech.ratings_bad)
            ratings += len(rated)
            reasons += sum(i.reason is not None for i in rated)
            comments += sum(i.comment is not None for i in rated)
            aging = [i for i in tech.items if i.kind == "aging"]
            assert tech.aging == len(aging)
            for i in aging:
                assert timeutil.local_date(i.at, tz) <= period.day - timedelta(days=7), "aging: open over a week"
                if i.last_reply is None:
                    never += 1
                    continue
                replies += 1
                assert timeutil.local_date(i.at, tz) <= timeutil.local_date(i.last_reply, tz) < period.day
            out = attendance.attendance(conn, tech.email, period, today=today, now=now)
            assert (tech.pto, tech.sick) == (len(out.pto), len(out.sick))
            work_days = demo.SUN_THU if emails.index(tech.email) < 2 else demo.TUE_SAT
            call_days = {timeutil.local_date(i.at, tz) for i in tech.items
                         if i.kind in ("answered", "declined", "missed")}
            assert call_days and all((d.weekday() + 1) % 7 in work_days for d in call_days), "on their work days"
            assert not call_days & set(out.pto + out.sick), "and none on a day out"
        assert over > 0, "some non-urgent calls run over 10 minutes"
        assert ratings and reasons and comments and comments < ratings, "ratings with and without comments"
        assert replies > never > 0, "aging tickets with a last reply, and some never replied to"

        avery = next(c.tech for c in counted if c.tech.email == emails[0])
        usual = demo.PREP_PROFILES[0][0]
        assert abs(avery.calls - usual) <= 0.2 * usual, (avery.calls, usual)

        oldest = cycle.latest_day(schedules[emails[0]], today) - timedelta(days=14 * 30)   # before the demo's data
        before = live.count_preps(conn, [(emails[0], oldest)], now=now)[0]
        assert before.tech.calls is None and before.tech.failures["calls"] == live.NOT_COLLECTED["calls"]
        assert before.tech.failures["solved"] == live.NOT_COLLECTED["solved"]
        assert before.tech.failures["aging"].startswith("Not recorded then")
    finally:
        conn.close()


def test_demo_collected_data_is_deterministic(tmp_path):
    tables = ("talk_legs", "talk_calls", "sat_ratings", "ticket_states", "aging_items")
    rows = []
    for name in ("a.db", "b.db"):
        demo.build_demo_db(tmp_path / name, now=NOW)
        conn = connect(tmp_path / name)
        try:
            rows.append({t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3")] for t in tables})
        finally:
            conn.close()
    assert all(rows[0][t] for t in tables) and rows[0] == rows[1]


def test_every_demo_tech_answers_calls_on_their_own_clock(tmp_path):
    """Each tech of the hosted demo, the techs outside NASA too (each with their own Zendesk id), answers calls on the
    days Team activity has them working, on their own clock (`regions.day_zones`), and in every week they worked: the
    calls Trends counts per shift. Mara Santos (APAC) takes hers 09:00 to 18:00 Manila, and her 1:1 numbers are
    counted on Manila's clock, with her own queue."""
    from work_assistant import regions
    from work_assistant.activity import collected, report
    from work_assistant.oneonone import live

    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        agents = collected.agents(conn)
        everyone = [email for _, email, _ in demo.AGENTS] + [tech[1] for tech in demo.REGION_TECHS]
        assert sorted(agents) == sorted(everyone) and len(set(agents.values())) == len(everyone)
        app_today = NOW.astimezone(DALLAS).date()
        first = app_today - timedelta(days=demo.ACTIVITY_WEEKS * 7 - 1)
        legs, calls = collected.talk_between(conn, datetime.combine(first, datetime.min.time(), DALLAS) -
                                             timedelta(days=1), NOW + timedelta(days=1))
        for email in everyone:
            zone_on = regions.day_zones(conn, email)

            def day_of(at):
                near = [at.date() + timedelta(days=n) for n in (-1, 0, 1)]
                return max(d for d in near if at.astimezone(ZoneInfo(zone_on(d))).date() == d)

            answered = [leg for leg in legs if leg.agent_id == agents[email] and leg.type == "agent"
                        and leg.status == "completed" and leg.talk > 0 and calls[leg.call_id].direction == "inbound"]
            days = [d for d in (day_of(leg.created) for leg in answered) if d >= first]   # the history's are earlier
            facts = report.tech_day_facts(conn, email, first, app_today + timedelta(days=1), today=app_today, now=NOW)
            worked = {f.day for f in facts if f.state in ("worked", "off")}
            assert answered and set(days) <= worked, email
            weeks = {timeutil.week_start(d) for d in worked if d < timeutil.week_start(app_today)}
            assert weeks <= {timeutil.week_start(d) for d in days}, f"{email}: calls in every week they worked"
            assert all(leg.created <= NOW for leg in answered)
        mara = [leg.created.astimezone(MANILA) for leg in legs if leg.agent_id == agents[MARA]]
        assert mara and all(9 <= at.hour < 18 for at in mara), "09:00 to 18:00 Manila"

        prep = live.count_preps(conn, [(MARA, date(2026, 10, 13))], now=NOW)[0]
        assert prep.tech.calls > 0 and prep.tech.queue > prep.tech.calls and prep.queue is None, "her own queue"
        assert prep.failures == {} and prep.tech.failures == {}
    finally:
        conn.close()


def test_trends_has_calls_per_shift_for_every_demo_tech(tmp_path):
    """In the hosted demo, Trends' Calls per shift over the last 8 weeks has a value for every demo tech, those outside
    NASA too, and in every week they worked a shift."""
    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        body = _trends(conn)
        by_key = {m["key"]: {t["email"]: t for t in m["techs"]} for m in body["measures"]}
        everyone = [email for _, email, _ in demo.AGENTS] + [tech[1] for tech in demo.REGION_TECHS]
        assert sorted(by_key["calls_per_shift"]) == sorted(everyone)
        weeks = [n for n, step in enumerate(body["steps"]) if step["in_period"]]
        assert len(weeks) == 8
        for email in everyone:
            calls, tickets = by_key["calls_per_shift"][email], by_key["tickets_per_shift"][email]
            assert calls["now"] is not None and calls["now"] > 0, email
            worked = [n for n in weeks if tickets["steps"][n] is not None]
            assert worked and all(calls["steps"][n] is not None for n in worked), email
    finally:
        conn.close()


def test_demo_has_a_slipping_and_an_improving_tech(tmp_path):
    demo.build_demo_db(tmp_path / "demo.db", now=NOW)
    conn = connect(tmp_path / "demo.db")
    try:
        body = _trends(conn)
        by_key = {m["key"]: {t["email"]: t for t in m["techs"]} for m in body["measures"]}
        jordan, taylor = "jordan.pike@example.com", "taylor.brooks@example.com"
        assert by_key["tickets_per_shift"][jordan]["change"] < 0
        assert by_key["late_start_min"][jordan]["change"] > 0
        assert by_key["tickets_per_shift"][taylor]["change"] > 0
        assert by_key["late_start_min"][taylor]["change"] < 0
        # One missed shift for Jordan, in the 4th week back (Sep 20-26).
        missed = by_key["missed_shifts"][jordan]
        assert missed["now"] == 1 and missed["steps"][12] == 1 and sum(v for v in missed["steps"][8:]) == 1
        # The slip is steady, not a cliff: no week is absurd.
        weekly = by_key["tickets_per_shift"][jordan]["steps"]
        assert min(v for v in weekly if v is not None) > 3
        assert by_key["late_start_min"][jordan]["steps"][-1] < 90
        assert max(v for v in by_key["longest_gap_min"][jordan]["steps"][8:] if v is not None) < 120
    finally:
        conn.close()


def test_hosted_demo_has_a_team_lead_with_three_techs(tmp_path):
    """The hosted Eyes scenarios (`serve --demo --edition hosted`, plan Task 6) need a TL: added by name, seeing
    three of the demo's techs (one slipping, one with a sick day, one with PTO). The PC's demo has no people."""
    from fastapi.testclient import TestClient

    from work_assistant import people
    from work_assistant.server_config import ServerConfig
    from work_assistant.web.app import create_app

    path = tmp_path / "demo.db"
    demo.build_demo_db(path, now=NOW)
    conn = connect(path)
    try:
        assert people.everyone(conn) == [], "the PC's demo adds nobody"
    finally:
        conn.close()

    demo.seed_team_lead(path, "team.lead@example.com")
    name, email = demo.DEMO_TL
    jordan, sam, taylor = "jordan.pike@example.com", "sam.okafor@example.com", "taylor.brooks@example.com"
    assert (name, email) == ("Jamie Rivera", "jamie.rivera@example.com")
    conn = connect(path)
    try:
        assert people.everyone(conn)[1] == {"email": email, "role": "tl", "name": name,
                                            "techs": [jordan, sam, taylor]}
        assert people.visible_techs(conn, email) == [jordan, sam, taylor], "all three are on the roster"
    finally:
        conn.close()

    def signed_in(who):
        config = ServerConfig(edition="hosted", port=8811, db_path=path, log_dir=tmp_path / "logs",
                              admin_email="team.lead@example.com", timezone="America/Chicago")
        return TestClient(create_app(path, log_dir=tmp_path / "logs", demo=True, port=8811, now=NOW, config=config,
                                     demo_identity=who), base_url="http://127.0.0.1:8811")

    with signed_in(email) as client:
        rows = client.get("/api/activity", params={"period": "last_8_weeks"}).json()["rows"]
        assert [r["email"] for r in rows] == [jordan, sam, taylor]
        cells = [c for r in rows for c in r["cells"]]
        assert {"missed", "pto"} <= {c["state"] for c in cells}, "a sick day and a PTO day to show"
        assert sorted(t["email"] for t in client.get("/api/activity/trends").json()["techs"]) == [jordan, sam, taylor]
        assert sorted(client.get("/api/settings").json()["team"]) == [jordan, sam, taylor]
    with signed_in("team.lead@example.com") as client:
        listed = client.get("/api/people").json()["people"]
        assert [(p["email"], p["role"]) for p in listed][:2] == [("team.lead@example.com", "admin"), (email, "tl")]


def test_demo_seeds_admin_team(tmp_path):
    """Hosted Team design §3: the demo's admin, named Alex Kim since Managers (so a manager's screens name every team
    lead), has their own team (Avery Stone, Morgan Reyes, Riley Chen, as in the mockups, and since Regions the four
    techs outside NASA, demo.REGION_TECHS), and sees only it; Jamie Rivera's is Jordan Pike, Sam Okafor and Taylor
    Brooks."""
    from fastapi.testclient import TestClient

    from work_assistant import people
    from work_assistant.server_config import ServerConfig
    from work_assistant.web.app import create_app

    admin = "team.lead@example.com"
    path = tmp_path / "demo.db"
    demo.build_demo_db(path, now=NOW)
    demo.seed_team_lead(path, admin, now=NOW)
    avery, morgan, riley = "avery.stone@example.com", "morgan.reyes@example.com", "riley.chen@example.com"
    own = [avery, LENA, MARA, morgan, OMAR, PAOLO, riley]   # by email, and by name too
    conn = connect(path)
    try:
        assert people.everyone(conn)[0] == {"email": admin, "role": "admin", "name": "Alex Kim", "techs": own}
        assert people.everyone(conn)[1]["techs"] == ["jordan.pike@example.com", "sam.okafor@example.com",
                                                     "taylor.brooks@example.com"]
    finally:
        conn.close()
    config = ServerConfig(edition="hosted", port=8811, db_path=path, log_dir=tmp_path / "logs",
                          admin_email=admin, timezone="America/Chicago")
    with TestClient(create_app(path, log_dir=tmp_path / "logs", demo=True, port=8811, now=NOW, config=config,
                               demo_identity=admin), base_url="http://127.0.0.1:8811") as client:
        assert [r["email"] for r in client.get("/api/activity").json()["rows"]] == own, \
            "the admin sees their own team only, by name"
        assert sorted(client.get("/api/settings").json()["team"]) == own
        assert {r["email"]: r["leads"] for r in client.get("/api/people").json()["roster"]}[avery] == [admin]


# -- the hosted demo's regions ---------------------------------------------------------------------------------

DEMO_ADMIN = "team.lead@example.com"
LENA, OMAR = "lena.fischer@example.com", "omar.haddad@example.com"      # EMEA
MARA, PAOLO = "mara.santos@example.com", "paolo.dizon@example.com"      # APAC; Paolo moved to NASA
MANILA, DALLAS, DUBAI = ZoneInfo("Asia/Manila"), ZoneInfo("America/Chicago"), ZoneInfo("Asia/Dubai")
TUE_SAT, MON_FRI = {2, 3, 4, 5, 6}, {1, 2, 3, 4, 5}                    # Sunday = 0


def _hosted_demo(tmp_path):
    """The hosted demo's database as `serve --demo --demo-date 2026-10-19 --edition hosted` builds it."""
    path = tmp_path / "demo.db"
    demo.build_demo_db(path, now=NOW)
    demo.seed_team_lead(path, DEMO_ADMIN, now=NOW)
    return path


def _signed_in(path, tmp_path, who):
    from fastapi.testclient import TestClient

    from work_assistant.server_config import ServerConfig
    from work_assistant.web.app import create_app

    config = ServerConfig(edition="hosted", port=8811, db_path=path, log_dir=tmp_path / "logs",
                          admin_email=DEMO_ADMIN, timezone="America/Chicago")
    return TestClient(create_app(path, log_dir=tmp_path / "logs", demo=True, port=8811, now=NOW, config=config,
                                 demo_identity=who), base_url="http://127.0.0.1:8811")


def _weekday(day):
    return (day.weekday() + 1) % 7   # Sunday = 0, as shifts count


def test_the_hosted_demos_admin_sees_techs_in_three_regions(tmp_path):
    """Regions design: the hosted demo's admin leads techs in NASA, EMEA and APAC, so their Tech picker groups by
    region (Eyes: hosted-regions.yaml), and a region narrows Activity to its techs. Paolo Dizon moved from APAC to NASA,
    so he is under NASA. Jamie Rivera's techs are all NASA: one region, the plain list. The PC's demo has no regions."""
    path = tmp_path / "pc.db"
    demo.build_demo_db(path, now=NOW)
    conn = connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tech_regions").fetchone()[0] == 0
        assert len(settings.team_emails(conn)) == len(demo.AGENTS), "the PC's demo has only its six"
    finally:
        conn.close()

    path = _hosted_demo(tmp_path)
    with _signed_in(path, tmp_path, DEMO_ADMIN) as client:
        picker = client.get("/api/activity", params={"period": "last_week"}).json()["picker"]
        grouped: dict[str, list[str]] = {}
        for tech in picker:
            grouped.setdefault(tech["region"], []).append(tech["name"])
        assert grouped == {"NASA": ["Avery Stone", "Morgan Reyes", "Paolo Dizon", "Riley Chen"],
                           "EMEA": ["Lena Fischer", "Omar Haddad"], "APAC": ["Mara Santos"]}
        for region, names in (("APAC", ["Mara Santos"]), ("EMEA", ["Lena Fischer", "Omar Haddad"])):
            rows = client.get("/api/activity", params={"period": "last_week", "region": region}).json()["rows"]
            assert [r["name"] for r in rows] == names
        trends = client.get("/api/activity/trends", params={"tech": MARA}).json()
        assert trends["team"] == DEMO_ADMIN and len(trends["techs"]) == 7, "Mara is compared with the admin's team"
    with _signed_in(path, tmp_path, demo.DEMO_TL[1]) as client:
        picker = client.get("/api/activity", params={"period": "last_week"}).json()["picker"]
        assert [(t["name"], t["region"]) for t in picker] == [
            ("Jordan Pike", "NASA"), ("Sam Okafor", "NASA"), ("Taylor Brooks", "NASA")]


def test_the_hosted_demo_has_a_manager_and_a_lead_for_each_other_region(tmp_path):
    """Managers design, "The demo": the admin is named Alex Kim; Leila Haddad leads the EMEA techs and Paolo Ramos the
    APAC ones (Paolo Dizon moved on to NASA), each sharing them with the admin's team, which stays as Regions made it;
    and Parker Lane manages NASA and APAC. Signed in as Parker (`--demo-user parker.lane@example.com`), the demo shows
    every tech in those regions, whoever's team, and the team leads with any of them: Leila's are all EMEA, so she
    isn't one."""
    from work_assistant import people

    assert demo.DEMO_ADMIN_NAME == "Alex Kim"
    assert demo.DEMO_MANAGER == ("Parker Lane", "parker.lane@example.com", ("NASA", "APAC"))
    assert demo.DEMO_REGION_LEADS == {"EMEA": ("Leila Haddad", "leila.haddad@example.com"),
                                      "APAC": ("Paolo Ramos", "paolo.ramos@example.com")}
    leila, ramos, parker = "leila.haddad@example.com", "paolo.ramos@example.com", "parker.lane@example.com"
    avery, morgan, riley = "avery.stone@example.com", "morgan.reyes@example.com", "riley.chen@example.com"
    jordan, sam, taylor = "jordan.pike@example.com", "sam.okafor@example.com", "taylor.brooks@example.com"
    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        assert [(p["email"], p["role"], p["name"], p["techs"]) for p in people.everyone(conn)] == [
            (DEMO_ADMIN, "admin", "Alex Kim", [avery, LENA, MARA, morgan, OMAR, PAOLO, riley]),
            (demo.DEMO_TL[1], "tl", "Jamie Rivera", [jordan, sam, taylor]),
            (leila, "tl", "Leila Haddad", [LENA, OMAR]),
            (ramos, "tl", "Paolo Ramos", [MARA, PAOLO]),
            (parker, "manager", "Parker Lane", []),
        ]
        assert people.manager_regions(conn, parker) == ["NASA", "APAC"]
        assert people.leads_of(conn, MARA) == [DEMO_ADMIN, ramos], "a tech on two teams"
    finally:
        conn.close()

    nasa_apac = [avery, jordan, riley, sam, taylor, morgan, MARA, PAOLO]   # the roster's order
    with _signed_in(path, tmp_path, parker) as client:
        me = client.get("/api/me").json()
        assert (me["role"], me["name"], me["regions"]) == ("manager", "Parker Lane", ["NASA", "APAC"])
        rows = client.get("/api/activity", params={"period": "last_week"}).json()["rows"]
        assert sorted(r["email"] for r in rows) == sorted(nasa_apac), "every tech in NASA and APAC, no EMEA"
        listed = client.get("/api/people").json()["people"]
        assert [(p["name"], p["techs"]) for p in listed] == [
            ("Alex Kim", [avery, MARA, morgan, PAOLO, riley]), ("Jamie Rivera", [jordan, sam, taylor]),
            ("Paolo Ramos", [MARA, PAOLO])], "the leads with techs in NASA or APAC, each with only those"
        trends = client.get("/api/activity/trends", params={"team": ramos}).json()
        assert sorted(t["email"] for t in trends["techs"]) == [MARA, PAOLO]
    for lead, theirs in ((leila, [LENA, OMAR]), (ramos, [MARA, PAOLO])):
        with _signed_in(path, tmp_path, lead) as client:
            assert [r["email"] for r in client.get("/api/activity").json()["rows"]] == theirs


def test_an_apac_demo_techs_busiest_hours_are_9_to_6_manila(tmp_path):
    """Mara Santos (APAC) works Tuesday to Saturday, 09:00 to 18:00 Manila: about 8 PM to 5 AM in Dallas, so each of
    her days on her own clock is whole, never split across two of the app's days (the case that started Regions).
    Built as of noon Monday Oct 19 in Dallas, it is already 1 AM Tuesday Oct 20 in Manila: her today, still to come."""
    from collections import Counter

    from work_assistant.activity import report, store

    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        ats = [store.parse_iso(r[0]) for r in conn.execute("SELECT at FROM activity_actions WHERE email = ?", (MARA,))]
        hours = Counter(at.astimezone(MANILA).hour for at in ats)
        assert len(ats) > 500 and set(hours) <= set(range(9, 18)), "every action 09:00-18:00 Manila"
        assert {at.astimezone(DALLAS).hour for at in ats} <= {20, 21, 22, 23, 0, 1, 2, 3, 4}
        signins = [store.parse_iso(r[0]).astimezone(MANILA) for r in conn.execute(
            "SELECT at FROM activity_signins WHERE email = ?", (MARA,))]
        assert signins and all(8 * 60 + 45 <= s.hour * 60 + s.minute <= 9 * 60 + 10 for s in signins)

        started = date(2026, 6, 24)
        facts = report.tech_day_facts(conn, MARA, started, date(2026, 10, 20), today=date(2026, 10, 19), now=NOW)
        worked = [f for f in facts if f.state == "worked"]
        assert len(worked) > 80
        assert all(_weekday(f.day) in TUE_SAT for f in worked), "her work days on her clock"
        assert all(f.first.astimezone(MANILA).date() == f.day == f.last.astimezone(MANILA).date() for f in worked)
        assert all((f.shift.start_min, f.shift.end_min) == (9 * 60, 18 * 60) for f in worked)
        assert {f.state for f in facts[:-1]} == {"worked", "rest"}, "no worked rest day, missed day or unsynced day"
        assert (facts[-1].day, facts[-1].state) == (date(2026, 10, 20), "future")
    finally:
        conn.close()


def test_a_demo_tech_who_moved_from_apac_to_nasa(tmp_path):
    """Paolo Dizon served APAC, then NASA from Monday Sep 28 (a dated swap): his days before it are on
    Manila's clock and from it on Dallas's, each whole on its own day, and his today is Dallas's."""
    from work_assistant import regions
    from work_assistant.activity import report

    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        moved = date(2026, 9, 28)
        assert regions.region_history(conn, PAOLO) == [(date(2026, 6, 24), "APAC"), (moved, "NASA")]
        assert regions.region_of(conn, PAOLO) == "NASA"
        facts = report.tech_day_facts(conn, PAOLO, date(2026, 6, 24), date(2026, 10, 19), today=date(2026, 10, 19),
                                      now=NOW)
        worked = [f for f in facts if f.state == "worked"]
        before, after = [f for f in worked if f.day < moved], [f for f in worked if f.day >= moved]
        assert len(before) > 60 and len(after) == 16, "Sep 28 to Oct 16 and Monday Oct 19 so far"
        assert all(_weekday(f.day) in MON_FRI for f in worked)
        for days, zone in ((before, MANILA), (after, DALLAS)):
            for f in days:
                first, last = f.first.astimezone(zone), f.last.astimezone(zone)
                assert first.date() == f.day == last.date() and 9 <= first.hour and last.hour < 18
        assert {f.state for f in facts} == {"worked", "rest"}, "nothing lost or doubled at the move"
        assert facts[-1].day == date(2026, 10, 19) and facts[-1].last.astimezone(DALLAS).hour < 12, "noon in Dallas"
    finally:
        conn.close()


def test_the_hosted_demos_emea_techs_work_on_dubai_time(tmp_path):
    """Lena Fischer and Omar Haddad (EMEA) work Monday to Friday, 09:00 to 18:00 Dubai: midnight to 9 AM in Dallas
    in summer time. At noon Monday Oct 19 in Dallas it is 9 PM in Dubai, so their Monday is whole."""
    from work_assistant.activity import report

    path = _hosted_demo(tmp_path)
    conn = connect(path)
    try:
        for email in (LENA, OMAR):
            facts = report.tech_day_facts(conn, email, date(2026, 6, 24), date(2026, 10, 19),
                                          today=date(2026, 10, 19), now=NOW)
            worked = [f for f in facts if f.state == "worked"]
            assert len(worked) > 70 and all(_weekday(f.day) in MON_FRI for f in worked)
            assert all(f.first.astimezone(DUBAI).date() == f.day and 9 <= f.first.astimezone(DUBAI).hour
                       and f.last.astimezone(DUBAI).hour < 18 for f in worked)
            assert {f.state for f in facts} == {"worked", "rest"}
            assert facts[-1].state == "worked" and facts[-1].last.astimezone(DUBAI).hour >= 16, "Monday is done"
    finally:
        conn.close()


def test_hosted_demo_serves_an_invented_teams_shifts_export(tmp_path):
    """Import from Teams Shifts (the hosted admin's Settings) needs export files to choose: the hosted demo serves an
    invented one, built in memory, for trying the panel and for the hosted-admin Eyes scenario. Its preview has a bit
    of everything the panel shows: leave written only in the notes, a sick day, a half day that is no sick day, a
    shift change, EMEA and APAC clocks, a move from APAC to NASA, a tech whose first shift is a week in, a shift that
    crosses midnight, a day off entered with times, a label read as a working shift and a team lead skipped. The
    export begins after the demo's stored activity, so it sets no Started and keeps the entries before it (Ruling 18).
    The PC's demo has no import, so no export."""
    import base64

    from fastapi.testclient import TestClient

    from work_assistant.server_config import ServerConfig
    from work_assistant.web.app import create_app

    admin = "team.lead@example.com"
    path = tmp_path / "demo.db"
    demo.build_demo_db(path, now=NOW)
    demo.seed_team_lead(path, admin, now=NOW)
    config = ServerConfig(edition="hosted", port=8811, db_path=path, log_dir=tmp_path / "logs",
                          admin_email=admin, timezone="America/Chicago")
    with TestClient(create_app(path, log_dir=tmp_path / "logs", demo=True, port=8811, now=NOW, config=config,
                               demo_identity=admin), base_url="http://127.0.0.1:8811") as client:
        got = client.get("/demo/shifts-export.xlsx")
        assert got.status_code == 200
        assert got.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert 'filename="demo-shifts-export.xlsx"' in got.headers["content-disposition"]
        files = {"files": [{"name": demo.SHIFTS_EXPORT_NAME, "data": base64.b64encode(got.content).decode("ascii")}]}
        answer = client.post("/api/shifts-import/preview", json=files)
    assert answer.status_code == 200, answer.text
    preview = answer.json()
    assert (preview["first"], preview["last"]) == ("2026-10-04", "2026-10-31")
    assert [t["name"] for t in preview["techs"]] == ["Avery Stone", "Jordan Pike", "Morgan Reyes", "Sam Okafor",
                                                    "Taylor Brooks", "Riley Chen"], "by region, then name"
    tech = {t["name"]: t for t in preview["techs"]}

    def changes(name):
        return [(c["days"], c["start"], c["end"]) for c in tech[name]["changes"]]

    assert tech["Avery Stone"]["pto"] == ["2026-10-14"] and changes("Avery Stone") == [([0, 1, 2, 3, 4], "08:00", "17:00")]
    assert changes("Jordan Pike") == [([0, 1, 2, 3, 4], "08:00", "17:00"), ([1, 2, 3, 4, 5], "09:00", "18:00")]
    assert tech["Jordan Pike"]["changes"][1]["since"] == "2026-10-18" and tech["Jordan Pike"]["sick"] == ["2026-10-20"]
    assert tech["Riley Chen"]["regions"] == [{"since": "2026-10-04", "region": "EMEA"}]
    assert changes("Riley Chen") == [([2, 3, 4, 5, 6], "09:00", "18:00")], "Dubai 9 to 6"
    assert tech["Sam Okafor"]["regions"] == [{"since": "2026-10-04", "region": "APAC"},
                                             {"since": "2026-10-18", "region": "NASA"}]
    assert changes("Sam Okafor") == [([1, 2, 3, 4, 5], "08:00", "17:00"), ([2, 3, 4, 5, 6], "08:00", "17:00")], \
        "Manila 8 to 5, then Dallas 8 to 5"
    assert changes("Morgan Reyes") == [([2, 3, 4, 5, 6], "10:00", "19:00")]
    assert tech["Morgan Reyes"]["changes"][0]["since"] == "2026-10-11" and tech["Morgan Reyes"]["started"] is None, \
        "the export begins after the demo's stored activity, so it doesn't reach back (Ruling 18): no Started"
    assert not any(t["replace_before"] for t in preview["techs"]), "and keeps every entry before its dates"
    taylor = tech["Taylor Brooks"]
    assert taylor["pto"] == ["2026-10-29"], "a day off entered with times, 8 to 5, is a full day"
    assert taylor["problems"] == ["The 20:00-04:00 shift on 2026-10-23 crosses midnight on Dallas time, so its week "
                                  "keeps the week before's shift."]
    assert (preview["skipped"], preview["problems"]) == (["Jamie Rivera"], []), "the team lead isn't on the roster"
    assert preview["unrecognised"] == [{"label": "Training", "count": 2}]

    with TestClient(create_app(path, log_dir=tmp_path / "logs", demo=True, port=8811, now=NOW),
                    base_url="http://127.0.0.1:8811") as client:
        assert client.get("/demo/shifts-export.xlsx").status_code == 404, "the PC's demo has no import"


def test_demo_has_a_year_of_history_before_its_17_weeks(tmp_path):
    """The owner, 2026-10-22: the demo goes back HISTORY_WEEKS (a year), so 1:1 Prep's Compare of 6 months and a year,
    and Trends' Last year, show data. The older weeks have shifts, actions and collected calls and ratings, and the
    sync and the collection start with them; 1:1 Prep then has 13 1:1s for 6 months and 25 for a year."""
    from work_assistant.activity import collected
    from work_assistant.activity import store as activity_store
    from work_assistant.oneonone import report as prep_report

    assert demo.HISTORY_WEEKS == 53
    demo.build_demo_db(tmp_path / "demo.db", now=NOW)
    conn = connect(tmp_path / "demo.db")
    try:
        zone = ZoneInfo(settings.get_timezone(conn))
        today = NOW.astimezone(zone).date()
        start = today - timedelta(days=demo.HISTORY_WEEKS * 7 - 1)
        activity_first = today - timedelta(days=demo.ACTIVITY_WEEKS * 7 - 1)
        began = datetime.combine(start, datetime.min.time(), zone)
        assert activity_store.synced_from(conn) == began
        assert all(collected.state(conn, kind).covered_from == began for kind in collected.KINDS)
        older = activity_store.iso(datetime.combine(activity_first, datetime.min.time(), zone))
        assert conn.execute("SELECT COUNT(*) FROM activity_actions WHERE at < ?", (older,)).fetchone()[0] > 1000
        assert conn.execute("SELECT COUNT(*) FROM talk_legs WHERE created < ?", (older,)).fetchone()[0] > 1000
        assert conn.execute("SELECT COUNT(*) FROM sat_ratings WHERE created < ?", (older,)).fetchone()[0] > 10
        tech = demo.AGENTS[0][1]
        assert len(prep_report.trends_view(conn, tech, 13, today=today, now=NOW)["days"]) == 13
        assert len(prep_report.trends_view(conn, tech, 26, today=today, now=NOW)["days"]) >= 25
    finally:
        conn.close()
