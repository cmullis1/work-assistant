from datetime import datetime, timedelta, timezone

import httpx
import pytest
from collect_fakes import CollectFake

from work_assistant.activity import store
from work_assistant.activity import sync as activity_sync
from work_assistant.activity.model import SignIn
from work_assistant.settings import set_setting
from work_assistant.zendesk.client import ZendeskClient, ZendeskError

UTC = timezone.utc
NOW = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
PAT, SAM = "pat@example.com", "sam@example.com"
PAT_ID, SAM_ID = 77, 88
EVERY_KIND_READ = {"legs": None, "calls": None, "ratings": None, "tickets": None}   # a sync's "collected" when all read


def ts(moment):
    return int(moment.timestamp())


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def event(event_id, ticket_id, at, author_id=PAT_ID):
    """A ticket event holding one public reply by `author_id`."""
    return {
        "id": event_id, "ticket_id": ticket_id, "timestamp": ts(at), "created_at": stamp(at),
        "updater_id": author_id, "via": "Web form",
        "child_events": [{"id": event_id * 10 + 1, "event_type": "Comment", "author_id": author_id, "public": True}],
    }


def page(events, end_time, end_of_stream=False):
    return {"ticket_events": events, "end_time": ts(end_time), "end_of_stream": end_of_stream}


class FakeClient(CollectFake):
    """Stands in for ZendeskClient. `pages` are served in order, skipping those at or before `start`. The collection's
    reads are `CollectFake`'s, given by keyword (`legs`, `calls`, `ticket_export`, `ratings`, `comments`)."""

    def __init__(self, pages=(), users=None, logins=None, tickets=None, open_assigned=None, fail_on_page=None,
                 **collect):
        super().__init__(**collect)
        self.pages = list(pages)
        self.users = {PAT: PAT_ID} if users is None else users
        self.login_rows = logins or {}
        self.tickets = tickets  # id -> subject; None means every id has the subject "Subject <id>"
        self.open_rows = open_assigned or {}
        self.fail_on_page = fail_on_page
        self.starts: list[int] = []
        self.served: list[int] = []
        self.login_calls: list[tuple] = []
        self.ticket_calls: list[list[int]] = []
        self.closed = False

    def ticket_events(self, start):
        self.starts.append(start)
        for number, one in enumerate(self.pages, 1):
            if one["end_time"] <= start:
                continue
            if number == self.fail_on_page:
                raise ZendeskError("Zendesk returned HTTP 429 for /api/v2/incremental/ticket_events.json after 5 retries")
            self.served.append(number)
            yield one
            if one["end_of_stream"]:
                return

    def users_by_email(self, emails):
        return {e: {"id": self.users[e]} for e in emails if e in self.users}

    def logins(self, user_id, start, end):
        self.login_calls.append((user_id, start, end))
        return list(self.login_rows.get(user_id, []))

    def tickets_many(self, ids):
        self.ticket_calls.append(list(ids))
        if self.tickets is None:
            return {i: {"id": i, "subject": f"Subject {i}"} for i in ids}
        return {i: {"id": i, "subject": self.tickets[i]} for i in ids if i in self.tickets}

    def open_assigned(self, user_id):
        return list(self.open_rows.get(user_id, []))

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def team(conn):
    set_setting(conn, "team", [PAT])
    set_setting(conn, "retention_days", 90)


def run(conn, client, tmp_path, now=NOW, kind="refresh", lines=None, **kwargs):
    return activity_sync.sync(conn, client, kind=kind, now=now,
                              log=(lines.append if lines is not None else (lambda line: None)),
                              logs_dir=tmp_path / "logs", **kwargs)


def action_count(conn):
    return conn.execute("SELECT COUNT(*) FROM activity_actions").fetchone()[0]


def three_pages():
    base = NOW - timedelta(days=3)
    return [
        page([event(1, 5, base)], base + timedelta(hours=1)),
        page([event(2, 5, base + timedelta(hours=2))], base + timedelta(hours=3)),
        page([event(3, 6, base + timedelta(hours=4))], base + timedelta(hours=5), end_of_stream=True),
    ]


# -- what a sync stores ---------------------------------------------------------------------------


def test_sync_stores_the_teams_actions_and_reports_them(conn, tmp_path):
    result = run(conn, FakeClient(three_pages()), tmp_path)
    assert result == {"status": "complete", "synced_through": NOW, "pages": 3, "actions": 3, "error": None,
                      "collected": EVERY_KIND_READ}
    assert action_count(conn) == 3
    row = store.latest_sync(conn)
    assert (row["status"], row["kind"], row["pages"]) == ("complete", "refresh", 3)
    assert store.synced_through(conn) == NOW


def test_sync_ignores_events_by_people_outside_the_team(conn, tmp_path):
    base = NOW - timedelta(hours=5)
    pages = [page([event(1, 5, base, author_id=SAM_ID), event(2, 5, base)], base, end_of_stream=True)]
    run(conn, FakeClient(pages), tmp_path)
    assert [a.event_id for a in store.actions_between(conn, PAT, base - timedelta(days=1), NOW)] == [2]
    assert action_count(conn) == 1


def test_sync_is_idempotent(conn, tmp_path):
    first = run(conn, FakeClient(three_pages(), logins={PAT_ID: [{"created_at": stamp(NOW - timedelta(hours=9))}]}),
                tmp_path)
    again = run(conn, FakeClient(three_pages(), logins={PAT_ID: [{"created_at": stamp(NOW - timedelta(hours=9))}]}),
                tmp_path, now=NOW + timedelta(minutes=5))
    assert first["actions"] == 3 and again["actions"] == 0
    assert action_count(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM activity_signins").fetchone()[0] == 1


def test_overlapping_page_boundaries_do_not_double_count(conn, tmp_path):
    """Zendesk may hand the same event on two pages; the second copy adds nothing."""
    base = NOW - timedelta(days=1)
    shared = event(1, 5, base)
    pages = [page([shared], base), page([shared, event(2, 5, base + timedelta(hours=1))],
                                        base + timedelta(hours=2), end_of_stream=True)]
    run(conn, FakeClient(pages), tmp_path)
    assert action_count(conn) == 2


def test_sync_filters_by_event_timestamp(conn, tmp_path):
    """A page can carry older backfilled events; only those inside [start, now) are stored."""
    feb = datetime(2026, 2, 3, 9, 0, tzinfo=UTC)
    inside = NOW - timedelta(days=2)
    later = NOW + timedelta(minutes=1)
    pages = [page([event(1, 5, feb), event(2, 5, inside), event(3, 5, later)], inside, end_of_stream=True)]
    run(conn, FakeClient(pages), tmp_path)
    stored = conn.execute("SELECT event_id FROM activity_actions").fetchall()
    assert [r["event_id"] for r in stored] == [2]


def test_backfill_starts_at_retention_window(conn, tmp_path):
    set_setting(conn, "retention_days", 90)
    client = FakeClient(three_pages())
    run(conn, client, tmp_path, kind="backfill")
    assert client.starts == [ts(NOW - timedelta(days=90))]
    assert store.latest_sync(conn)["history_from"] is None


def test_the_first_complete_sync_marks_where_the_stored_data_begins(conn, tmp_path):
    """Final review F1: days are judged only from where the stored data begins (the first complete sync's window)."""
    assert store.synced_from(conn) is None
    run(conn, FakeClient(three_pages()), tmp_path, kind="backfill")
    assert store.latest_sync(conn)["window_start"] == stamp(NOW - timedelta(days=90))
    assert store.synced_from(conn) == NOW - timedelta(days=90)
    run(conn, FakeClient([page([], NOW + timedelta(days=1), end_of_stream=True)]), tmp_path,
        now=NOW + timedelta(days=1))
    assert store.synced_from(conn) == NOW - timedelta(days=90), "a later sync carries on; the start stays"
    assert store.synced_through(conn) == NOW + timedelta(days=1)


def test_sync_stops_at_the_first_page_that_reaches_now(conn, tmp_path):
    base = NOW - timedelta(hours=3)
    pages = [page([event(1, 5, base)], base), page([event(2, 5, base + timedelta(hours=1))], NOW),
             page([event(3, 5, NOW - timedelta(seconds=1))], NOW + timedelta(hours=1), end_of_stream=True)]
    client = FakeClient(pages)
    result = run(conn, client, tmp_path)
    assert client.served == [1, 2]
    assert result["pages"] == 2 and action_count(conn) == 2
    assert conn.execute("SELECT cursor_ts FROM activity_sync").fetchone()[0] == ts(NOW)


# -- interrupted syncs resume ---------------------------------------------------------------------


def test_sync_resumes_after_crash(conn, tmp_path):
    pages = three_pages() + [page([event(4, 7, NOW - timedelta(hours=1))], NOW - timedelta(minutes=30),
                                  end_of_stream=True)]
    pages[2]["end_of_stream"] = False
    crashing = FakeClient(pages, fail_on_page=3)
    result = run(conn, crashing, tmp_path, kind="backfill")
    assert result["status"] == "failed" and "429" in result["error"]
    row = store.latest_sync(conn)
    assert row["status"] == "failed" and "429" in row["error"]
    assert row["cursor_ts"] == pages[1]["end_time"], "the position is the last page that was fully stored"
    assert action_count(conn) == 2
    assert store.synced_through(conn) is None

    resumed = FakeClient(pages)
    result = run(conn, resumed, tmp_path, now=NOW + timedelta(minutes=1), kind="backfill")
    assert result["status"] == "complete"
    assert resumed.starts == [pages[1]["end_time"]]
    assert resumed.served == [3, 4]
    assert action_count(conn) == 4, "pages 3 and up are stored once, pages 1 and 2 not again"


def test_cursor_only_advances_after_a_pages_rows_are_stored(conn, tmp_path, monkeypatch):
    pages = three_pages()
    real = store.insert_actions
    seen = []

    def spy(c, rows):
        seen.append(c.execute("SELECT cursor_ts FROM activity_sync").fetchone()[0])
        return real(c, rows)

    monkeypatch.setattr(store, "insert_actions", spy)
    run(conn, FakeClient(pages), tmp_path)
    # While page N's rows were being stored, the saved position was still page N-1's end.
    assert seen == [None, pages[0]["end_time"], pages[1]["end_time"]]


def test_a_failure_after_the_events_keeps_the_event_position_but_retries_the_rest(conn, tmp_path):
    class BreaksAtTickets(FakeClient):
        def tickets_many(self, ids):
            raise ZendeskError("Zendesk returned HTTP 500 for /api/v2/tickets/show_many.json after 5 retries")

    logins = {PAT_ID: [{"created_at": stamp(NOW - timedelta(hours=2))}]}
    broken = BreaksAtTickets(three_pages(), logins=logins)
    assert run(conn, broken, tmp_path)["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM activity_tickets").fetchone()[0] == 0

    healthy = FakeClient(three_pages(), logins=logins)
    run(conn, healthy, tmp_path, now=NOW + timedelta(minutes=1))
    assert store.subjects(conn, [5, 6]) == {5: "Subject 5", 6: "Subject 6"}
    # Sign-ins are asked for again from the last complete sync, which is none, so from the window start.
    assert healthy.login_calls[0][1] == NOW + timedelta(minutes=1) - timedelta(days=90)


# -- sign-ins, subjects and the open snapshot -----------------------------------------------------


def test_signins_are_stored_and_next_sync_asks_from_the_last_complete_one(conn, tmp_path):
    first = FakeClient([], logins={PAT_ID: [{"created_at": "2026-10-19T08:15:00Z", "actor_id": PAT_ID}]})
    run(conn, first, tmp_path)
    assert first.login_calls == [(PAT_ID, NOW - timedelta(days=90), NOW)]
    assert store.signins_between(conn, PAT, NOW - timedelta(days=1), NOW) == [
        SignIn(PAT, datetime(2026, 10, 19, 8, 15, tzinfo=UTC))]

    second = FakeClient([])
    later = NOW + timedelta(hours=2)
    run(conn, second, tmp_path, now=later)
    assert second.login_calls == [(PAT_ID, NOW, later)]


def test_missing_ticket_subject_kept_as_none(conn, tmp_path):
    base = NOW - timedelta(hours=4)
    pages = [page([event(1, 7, base), event(2, 8, base)], base, end_of_stream=True)]
    client = FakeClient(pages, tickets={8: "Visible"})
    assert run(conn, client, tmp_path)["status"] == "complete"
    assert action_count(conn) == 2, "the action still counts"
    assert store.subjects(conn, [7, 8]) == {7: None, 8: "Visible"}

    # Zendesk is not asked about ticket 7 again.
    again = FakeClient(pages, tickets={8: "Visible"})
    run(conn, again, tmp_path, now=NOW + timedelta(minutes=1))
    assert again.ticket_calls == []


def test_subjects_are_fetched_only_for_tickets_not_yet_known(conn, tmp_path):
    first = FakeClient(three_pages())
    run(conn, first, tmp_path)
    assert sorted(first.ticket_calls[0]) == [5, 6]

    base = NOW - timedelta(minutes=30)
    more = FakeClient(three_pages() + [page([event(9, 9, base)], base + timedelta(minutes=1), end_of_stream=True)])
    more.pages[2]["end_of_stream"] = False  # so the new page 4 follows the old three, which the cursor skips
    run(conn, more, tmp_path, now=NOW + timedelta(minutes=1))
    assert more.ticket_calls == [[9]]


def test_open_snapshot_is_replaced_per_tech(conn, tmp_path):
    set_setting(conn, "team", [PAT, SAM])
    users = {PAT: PAT_ID, SAM: SAM_ID}
    first = FakeClient([], users=users, open_assigned={
        PAT_ID: [{"id": 5, "status": "open"}, {"id": 6, "status": "pending"}], SAM_ID: [{"id": 9, "status": "open"}]})
    run(conn, first, tmp_path)
    second = FakeClient([], users=users, open_assigned={PAT_ID: [{"id": 6, "status": "open"}],
                                                        SAM_ID: [{"id": 9, "status": "open"}]})
    later = NOW + timedelta(hours=1)
    run(conn, second, tmp_path, now=later)
    rows = conn.execute("SELECT email, ticket_id, status, captured_at FROM activity_open ORDER BY email, ticket_id")
    assert [tuple(r) for r in rows] == [
        (PAT, 6, "open", stamp(later)), (SAM, 9, "open", stamp(later))]


# -- team members Zendesk does not know ---------------------------------------------------------


def test_tech_not_found_in_zendesk_is_skipped(conn, tmp_path):
    gone = "gone@example.com"
    set_setting(conn, "team", [PAT, gone])
    from work_assistant.activity.model import Action
    old = Action(gone, 4, NOW - timedelta(days=30), "reply", None, None, 1, 1)
    store.insert_actions(conn, [old])
    conn.execute("INSERT INTO activity_open (email, captured_at, ticket_id, status) VALUES (?, 'x', 4, 'open')", (gone,))
    conn.commit()

    lines: list[str] = []
    result = run(conn, FakeClient(three_pages()), tmp_path, lines=lines)

    assert result["status"] == "complete"
    assert f"Not found in Zendesk: {gone}" in lines
    assert store.actions_between(conn, gone, NOW - timedelta(days=60), NOW) == [old], "past rows are untouched"
    assert conn.execute("SELECT COUNT(*) FROM activity_open WHERE email = ?", (gone,)).fetchone()[0] == 1
    assert action_count(conn) == 4


def test_team_with_nobody_in_zendesk_completes_without_reading_events(conn, tmp_path):
    client = FakeClient(three_pages(), users={})
    lines: list[str] = []
    result = run(conn, client, tmp_path, lines=lines)
    assert result["status"] == "complete" and result["pages"] == 0
    assert client.starts == []
    assert conn.execute("SELECT cursor_ts FROM activity_sync").fetchone()[0] is None


# -- one sync at a time ---------------------------------------------------------------------------


def running_row(conn, pid, started=NOW - timedelta(minutes=10)):
    conn.execute("INSERT INTO activity_sync (kind, status, started_at, pid) VALUES ('refresh', 'running', ?, ?)",
                 (stamp(started), pid))
    conn.commit()


def test_a_dead_running_sync_is_marked_interrupted(conn, tmp_path):
    running_row(conn, 4242)
    result = run(conn, FakeClient([]), tmp_path, alive=lambda pid: False)
    assert result["status"] == "complete"
    old = conn.execute("SELECT status, error FROM activity_sync ORDER BY id").fetchone()
    assert (old["status"], old["error"]) == ("failed", "interrupted")


def test_a_live_running_sync_blocks_a_new_one(conn, tmp_path):
    running_row(conn, 4242)
    client = FakeClient(three_pages())
    with pytest.raises(activity_sync.SyncBusy):
        run(conn, client, tmp_path, alive=lambda pid: True)
    assert client.starts == []
    assert conn.execute("SELECT COUNT(*) FROM activity_sync").fetchone()[0] == 1


def test_a_running_row_without_a_process_or_long_stale_is_dead(conn, tmp_path):
    running_row(conn, None)
    running_row(conn, 4242, started=NOW - timedelta(days=2))
    run(conn, FakeClient([]), tmp_path, alive=lambda pid: True)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM activity_sync ORDER BY id")]
    assert statuses == ["failed", "failed", "complete"]


# -- history fill -----------------------------------------------------------------------------------

LATER = NOW + timedelta(hours=1)
COVERED = NOW - timedelta(days=90)    # where the first (90-day) sync began
WIDE = LATER - timedelta(days=365)    # the window start once retention is 365 days


def first_sync(conn, tmp_path):
    """A complete 90-day sync at NOW, then the owner raises retention to 365 days."""
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    set_setting(conn, "retention_days", 365)


def history_pages():
    return [
        page([event(10, 49, NOW - timedelta(days=400)), event(11, 50, NOW - timedelta(days=300))],
             NOW - timedelta(days=200)),
        page([event(12, 51, NOW - timedelta(days=150)), event(13, 52, NOW - timedelta(days=80))],
             NOW - timedelta(days=85)),
        page([event(14, 53, NOW - timedelta(days=60))], NOW - timedelta(days=50)),
        page([event(15, 54, NOW + timedelta(minutes=30))], LATER + timedelta(hours=1), end_of_stream=True),
    ]


def stored_events(conn):
    return {r[0] for r in conn.execute("SELECT event_id FROM activity_actions")}


def test_backfill_fills_the_gap_back_to_a_longer_retention_window(conn, tmp_path):
    first_sync(conn, tmp_path)
    client = FakeClient(history_pages())
    result = run(conn, client, tmp_path, now=LATER, kind="backfill")
    assert result["status"] == "complete"
    assert client.starts == [ts(WIDE), ts(NOW)], "the gap from the new window start, then on from the old cursor"
    assert stored_events(conn) == {11, 12, 15}, "10 is before the window, 13 and 14 are the forward range's"
    assert result["pages"] == 3 and result["actions"] == 3
    assert (PAT_ID, WIDE, COVERED) in client.login_calls
    row = store.latest_sync(conn)
    assert row["history_from"] == stamp(WIDE) and row["history_cursor_ts"] == ts(COVERED)
    assert store.synced_from(conn) == WIDE


def test_daily_and_refresh_syncs_never_read_history(conn, tmp_path):
    first_sync(conn, tmp_path)
    for kind, now in (("refresh", LATER), ("daily", LATER + timedelta(minutes=1))):
        client = FakeClient(history_pages())
        run(conn, client, tmp_path, now=now, kind=kind)
        assert client.starts[0] >= ts(NOW), kind
    assert not {11, 12} & stored_events(conn)
    assert store.synced_from(conn) == COVERED


def test_a_failed_fill_keeps_the_coverage_and_resumes_where_it_stopped(conn, tmp_path):
    first_sync(conn, tmp_path)
    result = run(conn, FakeClient(history_pages(), fail_on_page=2), tmp_path, now=LATER, kind="backfill")
    assert result["status"] == "failed"
    assert store.synced_from(conn) == COVERED, "an unfinished fill never makes unread days look synced"
    row = store.latest_sync(conn)
    assert row["history_cursor_ts"] == ts(NOW - timedelta(days=200)) and row["history_from"] is None
    assert stored_events(conn) == {11}

    again = LATER + timedelta(minutes=1)
    resumed = FakeClient(history_pages())
    assert run(conn, resumed, tmp_path, now=again, kind="backfill")["status"] == "complete"
    assert resumed.starts == [ts(NOW - timedelta(days=200)), ts(NOW)]
    assert resumed.served == [2, 4]
    assert stored_events(conn) == {11, 12, 15}
    assert store.synced_from(conn) == again - timedelta(days=365)


def test_a_fill_that_failed_at_the_signins_resumes_at_the_coverage_start(conn, tmp_path):
    """Every page of the gap was read, so the retry starts at the end of the gap instead of re-reading it."""
    class BreaksAtOlderSignins(FakeClient):
        def logins(self, user_id, start, end):
            if start < COVERED:
                raise ZendeskError("Zendesk returned HTTP 500 for /api/v2/users/77/logins.json after 5 retries")
            return super().logins(user_id, start, end)

    first_sync(conn, tmp_path)
    result = run(conn, BreaksAtOlderSignins(history_pages()), tmp_path, now=LATER, kind="backfill")
    assert result["status"] == "failed"
    assert store.synced_from(conn) == COVERED
    row = store.latest_sync(conn)
    assert row["history_cursor_ts"] == ts(COVERED) and row["history_from"] is None

    again = LATER + timedelta(minutes=1)
    client = FakeClient(history_pages())
    assert run(conn, client, tmp_path, now=again, kind="backfill")["status"] == "complete"
    assert client.starts[0] == ts(COVERED)
    assert store.synced_from(conn) == again - timedelta(days=365)


def test_no_fill_when_a_tech_is_not_found_in_zendesk(conn, tmp_path):
    """The coverage moves back for everyone at once, so it waits until every tech's older activity can be read."""
    first_sync(conn, tmp_path)
    set_setting(conn, "team", [PAT, SAM])
    client = FakeClient(history_pages(), users={PAT: PAT_ID})
    lines: list[str] = []
    result = run(conn, client, tmp_path, now=LATER, kind="backfill", lines=lines)
    assert result["status"] == "complete"
    assert client.starts == [ts(NOW)], "only the forward read"
    assert store.latest_sync(conn)["history_from"] is None
    assert store.synced_from(conn) == COVERED
    assert "Older team activity not read: not every tech was found in Zendesk." in lines


def test_a_retention_increase_after_a_failed_fill_starts_over(conn, tmp_path):
    first_sync(conn, tmp_path)
    run(conn, FakeClient(history_pages(), fail_on_page=2), tmp_path, now=LATER, kind="backfill")
    set_setting(conn, "retention_days", 400)
    again = LATER + timedelta(minutes=1)
    client = FakeClient(history_pages())
    run(conn, client, tmp_path, now=again, kind="backfill")
    assert client.starts[0] == ts(again - timedelta(days=400))
    assert store.synced_from(conn) == again - timedelta(days=400)


def test_a_completed_fill_moves_the_coverage_back_even_if_the_rest_fails(conn, tmp_path):
    class BreaksAtTickets(FakeClient):
        def tickets_many(self, ids):
            raise ZendeskError("Zendesk returned HTTP 500 for /api/v2/tickets/show_many.json after 5 retries")

    first_sync(conn, tmp_path)
    result = run(conn, BreaksAtTickets(history_pages()), tmp_path, now=LATER, kind="backfill")
    assert result["status"] == "failed"
    assert store.synced_from(conn) == WIDE


def test_no_fill_when_the_window_does_not_reach_before_the_coverage(conn, tmp_path):
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    client = FakeClient(history_pages())
    run(conn, client, tmp_path, now=LATER, kind="backfill")
    assert client.starts == [ts(NOW)]
    assert store.latest_sync(conn)["history_from"] is None
    assert store.synced_from(conn) == COVERED


# -- read-only, retention ---------------------------------------------------------------------------


def read_only_zendesk(methods: list[str]) -> ZendeskClient:
    """A real client on a stand-in Zendesk that answers every read a sync makes, the collection's too, recording each
    request's method; any other request fails its read."""
    base = NOW - timedelta(hours=3)
    answers = {
        "/api/v2/users/search.json": {"users": [{"id": PAT_ID, "email": PAT}]},
        "/api/v2/incremental/ticket_events.json": page([event(1, 5, base)], base, end_of_stream=True),
        "/api/v2/audit_logs.json": {"audit_logs": [{"created_at": stamp(base), "actor_id": PAT_ID}]},
        "/api/v2/tickets/show_many.json": {"tickets": [{"id": 5, "subject": "Hello"}]},
        "/api/v2/search.json": {"results": [{"id": 5, "status": "open",
                                             "created_at": stamp(NOW - timedelta(days=10))}]},
        "/api/v2/channels/voice/stats/incremental/legs.json": {"legs": []},
        "/api/v2/channels/voice/stats/incremental/calls.json": {"calls": []},
        "/api/v2/satisfaction_ratings.json": {"satisfaction_ratings": []},
        "/api/v2/incremental/tickets/cursor.json": {"tickets": [], "metric_sets": [], "after_cursor": "abc",
                                                    "end_of_stream": True},
        "/api/v2/tickets/5/comments.json": {"comments": [{"author_id": PAT_ID, "public": True,
                                                          "created_at": stamp(base)}]},
    }

    def handler(request):
        methods.append(request.method)
        assert request.method == "GET", f"{request.method} {request.url.path}"
        assert request.url.path in answers, f"unexpected request {request.url.path}"
        return httpx.Response(200, json=answers[request.url.path])

    return ZendeskClient("acme", "owner@example.com", "tok", transport=httpx.MockTransport(handler))


def test_sync_never_writes(conn, tmp_path):
    methods: list[str] = []
    result = run(conn, read_only_zendesk(methods), tmp_path)
    assert result["status"] == "complete"
    assert result["collected"] == EVERY_KIND_READ
    assert methods and set(methods) == {"GET"}
    assert action_count(conn) == 1
    assert store.subjects(conn, [5]) == {5: "Hello"}
    assert conn.execute("SELECT status FROM activity_open").fetchone()[0] == "open"
    assert conn.execute("SELECT COUNT(*) FROM aging_items").fetchone()[0] == 1


def test_history_fill_never_writes(conn, tmp_path):
    methods: list[str] = []
    client = read_only_zendesk(methods)
    assert run(conn, client, tmp_path)["status"] == "complete"
    set_setting(conn, "retention_days", 365)
    result = run(conn, client, tmp_path, now=LATER, kind="backfill")
    assert result["status"] == "complete" and result["collected"] == EVERY_KIND_READ
    assert methods and set(methods) == {"GET"}
    assert store.synced_from(conn) == WIDE


def test_sync_ends_with_the_retention_purge(conn, tmp_path):
    set_setting(conn, "retention_days", 30)
    old = NOW - timedelta(days=45)
    from work_assistant.activity.model import Action
    store.insert_actions(conn, [Action(PAT, 3, old, "fields", "a", "b", 1, 1)])
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject) VALUES (3, 'Secret')")
    conn.commit()
    run(conn, FakeClient([]), tmp_path)
    assert store.subjects(conn, [3]) == {3: None}
    assert action_count(conn) == 1


def test_a_purged_ticket_with_a_new_action_gets_its_subject_back(conn, tmp_path):
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject, purged) VALUES (5, NULL, 1)")
    conn.commit()
    client = FakeClient(three_pages())
    run(conn, client, tmp_path)
    assert store.subjects(conn, [5]) == {5: "Subject 5"}
    assert conn.execute("SELECT purged FROM activity_tickets WHERE ticket_id = 5").fetchone()[0] == 0


# -- subjects of open tickets (Ruling 10) -------------------------------------------------------


def test_open_snapshot_stores_each_tickets_subject(conn, tmp_path):
    client = FakeClient([], open_assigned={PAT_ID: [
        {"id": 5, "status": "open", "subject": "Printer jam"}, {"id": 6, "status": "pending"}]})
    run(conn, client, tmp_path)
    assert store.subjects(conn, [5, 6]) == {5: "Printer jam"} | {6: None}
    assert conn.execute("SELECT COUNT(*) FROM activity_tickets WHERE ticket_id = 6").fetchone()[0] == 0, (
        "a ticket with no subject in the search result gets no row, so the subject fetch still asks for it")


def test_open_snapshot_never_overwrites_a_purged_row(conn, tmp_path):
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject, purged) VALUES (5, NULL, 1)")
    conn.commit()
    run(conn, FakeClient([], open_assigned={PAT_ID: [{"id": 5, "status": "open", "subject": "Back again"}]}),
        tmp_path)
    row = conn.execute("SELECT subject, purged FROM activity_tickets WHERE ticket_id = 5").fetchone()
    assert (row["subject"], row["purged"]) == (None, 1)


def test_open_snapshot_updates_a_changed_subject_and_keeps_one_it_is_not_given(conn, tmp_path):
    conn.execute("INSERT INTO activity_tickets (ticket_id, subject) VALUES (5, 'Old'), (6, 'Kept')")
    conn.commit()
    run(conn, FakeClient([], open_assigned={PAT_ID: [{"id": 5, "status": "open", "subject": "New"},
                                                     {"id": 6, "status": "open"}]}), tmp_path)
    assert store.subjects(conn, [5, 6]) == {5: "New", 6: "Kept"}


# -- reading back to a date ------------------------------------------------------------------------------


def test_a_backfill_from_a_date_reads_back_to_it_without_a_longer_retention(conn, tmp_path):
    """Whole-month periods need the days just before a year's window: `reach_back` reads them once, and retention
    stays as it is (its purge still erases their content; the counts stay)."""
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    back = NOW - timedelta(days=120)
    pages = [page([event(20, 60, NOW - timedelta(days=130)), event(21, 61, NOW - timedelta(days=100))],
                  NOW - timedelta(days=95)),
             page([event(22, 62, NOW - timedelta(days=92))], NOW - timedelta(days=89)),
             page([event(23, 63, NOW + timedelta(minutes=30))], LATER + timedelta(hours=1), end_of_stream=True)]
    client = FakeClient(pages)
    result = run(conn, client, tmp_path, now=LATER, kind="backfill", reach_back=back)
    assert result["status"] == "complete"
    assert client.starts == [ts(back), ts(NOW)], "the older stretch from the date, then on from the cursor"
    assert stored_events(conn) == {21, 22, 23}, "20 is before the date"
    assert store.synced_from(conn) == back
    assert settings_retention(conn) == 90


def test_reach_back_is_only_for_a_backfill_and_only_when_it_is_earlier(conn, tmp_path):
    run(conn, FakeClient([page([], NOW, end_of_stream=True)]), tmp_path)
    client = FakeClient(history_pages())
    run(conn, client, tmp_path, now=LATER, kind="refresh", reach_back=NOW - timedelta(days=120))
    assert client.starts == [ts(NOW)]
    later = FakeClient(history_pages())
    run(conn, later, tmp_path, now=LATER + timedelta(minutes=1), kind="backfill", reach_back=NOW - timedelta(days=10))
    assert later.starts[0] >= ts(NOW), "a date inside the stored history reads nothing older"
    assert store.synced_from(conn) == COVERED


def settings_retention(conn):
    from work_assistant.settings import get_setting
    return get_setting(conn, "retention_days")
