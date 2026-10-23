import base64
from datetime import datetime, timezone

import httpx
import pytest

from work_assistant.zendesk.client import (
    MAX_RETRIES,
    TALK_PAGE_SIZE,
    ReadOnlyViolation,
    TicketPage,
    ZendeskAuthError,
    ZendeskClient,
    ZendeskError,
    ZendeskNotFound,
)


def make_client(handler, sleeps=None):
    calls = {"n": 0}

    def counting(request):
        calls["n"] += 1
        return handler(request)

    client = ZendeskClient(
        "acme",
        "agent@example.com",
        "tok",
        transport=httpx.MockTransport(counting),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )
    return client, calls


def test_refuses_post():
    client, calls = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ReadOnlyViolation):
        client._http.post("https://x.zendesk.com/api/v2/tickets.json")
    assert calls["n"] == 0


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_refuses_other_writes(method):
    client, calls = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ReadOnlyViolation):
        getattr(client._http, method)("https://x.zendesk.com/api/v2/tickets/1.json")
    assert calls["n"] == 0


def test_uses_basic_auth_and_base_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"ok": True})

    client, _ = make_client(handler)
    assert client.get("/api/v2/ping.json") == {"ok": True}
    assert seen["url"] == "https://acme.zendesk.com/api/v2/ping.json"
    expected = base64.b64encode(b"agent@example.com/token:tok").decode()
    assert seen["auth"] == f"Basic {expected}"


def test_retry_after_honored():
    responses = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={"ok": 1})]
    sleeps = []
    client, _ = make_client(lambda r: responses.pop(0), sleeps)
    assert client.get("/api/v2/x.json") == {"ok": 1}
    assert sleeps == [3.0]


def test_retries_5xx_with_default_wait_then_gives_up():
    sleeps = []
    client, calls = make_client(lambda r: httpx.Response(503, text="customer secret body"), sleeps)
    with pytest.raises(ZendeskError) as exc:
        client.get("/api/v2/x.json", params={"q": "email:a@b.com"})
    assert calls["n"] == 6  # first try + 5 retries
    assert sleeps == [10.0] * 5
    message = str(exc.value)
    assert "503" in message and "/api/v2/x.json" in message
    assert "customer secret body" not in message and "a@b.com" not in message


def test_404_raises_not_found():
    client, _ = make_client(lambda r: httpx.Response(404, text="private detail"))
    with pytest.raises(ZendeskNotFound) as exc:
        client.get("/api/v2/tickets/9.json")
    assert "private detail" not in str(exc.value)


def test_403_raises_not_found():
    client, _ = make_client(lambda r: httpx.Response(403))
    with pytest.raises(ZendeskNotFound):
        client.get("/api/v2/tickets/9.json")


def test_401_raises_auth_error():
    client, calls = make_client(lambda r: httpx.Response(401, text="bad token"))
    with pytest.raises(ZendeskAuthError) as exc:
        client.get("/api/v2/tickets.json")
    assert calls["n"] == 1
    assert "bad token" not in str(exc.value)
    assert issubclass(ZendeskAuthError, ZendeskError)


def test_other_client_error_is_zendesk_error():
    client, calls = make_client(lambda r: httpx.Response(422, text="x"))
    with pytest.raises(ZendeskError):
        client.get("/api/v2/x.json")
    assert calls["n"] == 1


def test_get_refuses_foreign_host_so_credentials_stay_home():
    client, calls = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ZendeskError):
        client.get("https://evil.example.com/api/v2/x.json")
    assert calls["n"] == 0


def test_incremental_pagination():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={"tickets": [{"id": 1}, {"id": 2}], "after_cursor": "c2", "end_of_stream": False},
            )
        assert request.url.params["cursor"] == "c2"
        return httpx.Response(200, json={"tickets": [{"id": 3}], "after_cursor": "c3", "end_of_stream": True})

    client, _ = make_client(handler)
    start = datetime(2026, 1, 4, tzinfo=timezone.utc)
    assert [t["id"] for t in client.incremental_tickets(start)] == [1, 2, 3]
    assert seen[0]["start_time"] == str(int(start.timestamp()))


TICKETS_CURSOR = "/api/v2/incremental/tickets/cursor.json"
TICKETS_START = datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)  # epoch 1792558800


def ticket_export(*pages):
    """A ticket export handler that serves ``pages`` in order and records each request."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    return handler, seen


def test_ticket_pages_sends_include_and_cursor():
    first = {
        "tickets": [{"id": 1, "status": "solved"}],
        "metric_sets": [{"ticket_id": 1, "solved_at": "2026-10-21T06:00:00Z"}],
        "after_cursor": "abc",
        "end_of_stream": False,
    }
    second = {
        "tickets": [{"id": 2, "status": "open"}],
        "metric_sets": [{"ticket_id": 2, "solved_at": None}],
        "after_cursor": "def",
        "end_of_stream": True,
    }
    never_read = {"tickets": [{"id": 3}], "metric_sets": [], "after_cursor": "ghi", "end_of_stream": True}
    handler, seen = ticket_export(first, second, never_read)
    client, calls = make_client(handler)
    pages = list(client.ticket_pages(start=TICKETS_START))
    assert pages == [
        TicketPage(tickets=first["tickets"], metric_sets=first["metric_sets"], after_cursor="abc", end_of_stream=False),
        TicketPage(tickets=second["tickets"], metric_sets=second["metric_sets"], after_cursor="def", end_of_stream=True),
    ]
    assert calls["n"] == 2
    assert [r.url.path for r in seen] == [TICKETS_CURSOR, TICKETS_CURSOR]
    assert dict(seen[0].url.params) == {"start_time": "1792558800", "include": "metric_sets"}
    assert dict(seen[1].url.params) == {"cursor": "abc", "include": "metric_sets"}


def test_ticket_pages_resumes_from_cursor():
    handler, seen = ticket_export({"tickets": [], "metric_sets": [], "after_cursor": "xyz2", "end_of_stream": True})
    client, calls = make_client(handler)
    pages = list(client.ticket_pages(cursor="xyz"))
    assert pages == [TicketPage(tickets=[], metric_sets=[], after_cursor="xyz2", end_of_stream=True)]
    assert calls["n"] == 1
    assert dict(seen[0].url.params) == {"cursor": "xyz", "include": "metric_sets"}


def test_ticket_pages_stops_without_after_cursor():
    handler, _ = ticket_export({"tickets": [{"id": 1}], "metric_sets": [], "end_of_stream": False})
    client, calls = make_client(handler)
    pages = list(client.ticket_pages(start=TICKETS_START))
    assert pages == [TicketPage(tickets=[{"id": 1}], metric_sets=[], after_cursor=None, end_of_stream=False)]
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "kwargs", [{}, {"start": TICKETS_START, "cursor": "abc"}], ids=["neither", "both"]
)
def test_ticket_pages_needs_one_of_start_or_cursor(kwargs):
    client, calls = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        list(client.ticket_pages(**kwargs))
    assert calls["n"] == 0


def test_comments_include_users():
    def handler(request):
        assert request.url.path == "/api/v2/tickets/77/comments.json"
        assert request.url.params["include"] == "users"
        return httpx.Response(
            200,
            json={
                "comments": [{"id": 1, "author_id": 10}, {"id": 2, "author_id": 11}],
                "users": [{"id": 10, "name": "Ann"}, {"id": 11, "name": "Bob"}],
                "next_page": None,
            },
        )

    client, _ = make_client(handler)
    comments, users = client.ticket_comments(77)
    assert [c["id"] for c in comments] == [1, 2]
    assert users == {10: {"id": 10, "name": "Ann"}, 11: {"id": 11, "name": "Bob"}}


def test_comments_follow_next_page():
    def handler(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200, json={"comments": [{"id": 2}], "users": [{"id": 11, "name": "Bob"}], "next_page": None}
            )
        return httpx.Response(
            200,
            json={
                "comments": [{"id": 1}],
                "users": [{"id": 10, "name": "Ann"}],
                "next_page": "https://acme.zendesk.com/api/v2/tickets/5/comments.json?include=users&page=2",
            },
        )

    client, _ = make_client(handler)
    comments, users = client.ticket_comments(5)
    assert [c["id"] for c in comments] == [1, 2]
    assert set(users) == {10, 11}


def test_ticket_fields_keyed_by_id():
    client, _ = make_client(
        lambda r: httpx.Response(
            200, json={"ticket_fields": [{"id": 5, "title": "A"}, {"id": 6, "title": "B"}], "next_page": None}
        )
    )
    assert client.ticket_fields() == {5: {"id": 5, "title": "A"}, 6: {"id": 6, "title": "B"}}


def test_users_by_email_one_call_per_email():
    queries = []

    def handler(request):
        q = request.url.params["query"]
        queries.append(q)
        assert request.url.path == "/api/v2/users/search.json"
        if q == "email:a@x.com":
            return httpx.Response(200, json={"users": [{"id": 1, "email": "a@x.com"}]})
        return httpx.Response(200, json={"users": []})

    client, _ = make_client(handler)
    result = client.users_by_email(["a@x.com", "none@x.com"])
    assert queries == ["email:a@x.com", "email:none@x.com"]
    assert result == {"a@x.com": {"id": 1, "email": "a@x.com"}}


def test_download_follows_redirects_without_leaking_credentials():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "acme.zendesk.com":
            return httpx.Response(302, headers={"Location": "https://files.example.com/blob"})
        return httpx.Response(200, content=b"PNGDATA", headers={"Content-Type": "image/png"})

    client, _ = make_client(handler)
    data, ctype = client.download("https://acme.zendesk.com/attachments/token/abc/?name=a.png")
    assert (data, ctype) == (b"PNGDATA", "image/png")
    assert seen[0][1] is not None
    assert seen[1][0] == "https://files.example.com/blob" and seen[1][1] is None


def test_download_from_foreign_host_sends_no_credentials():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=b"x", headers={"Content-Type": "image/jpeg"})

    client, _ = make_client(handler)
    assert client.download("https://p1.zdusercontent.com/attachment/1") == (b"x", "image/jpeg")
    assert seen == [None]


def test_httpx_info_logging_does_not_leak_query(caplog):
    import logging

    client, _ = make_client(lambda r: httpx.Response(200, json={"users": []}))
    with caplog.at_level(logging.INFO):
        client.get("/api/v2/users/search.json", {"query": "email:a@x.com"})
    assert not [r for r in caplog.records if "query=" in r.getMessage()]


@pytest.mark.parametrize("header,expected", [("inf", 10.0), ("nan", 10.0), ("1e9", 120.0), ("-5", 0.0)])
def test_retry_after_is_finite_and_capped(header, expected):
    responses = [httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200, json={})]
    sleeps = []
    client, _ = make_client(lambda r: responses.pop(0), sleeps)
    client.get("/api/v2/x.json")
    assert sleeps == [expected]


@pytest.mark.parametrize(
    "url",
    ["http://acme.zendesk.com/api/v2/x.json", "https://acme.zendesk.com:8443/api/v2/x.json"],
)
def test_get_refuses_insecure_or_odd_port_own_host(url):
    client, calls = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ZendeskError):
        client.get(url)
    assert calls["n"] == 0


@pytest.mark.parametrize(
    "url",
    ["http://acme.zendesk.com/attachments/1", "https://acme.zendesk.com:8443/attachments/1"],
)
def test_download_insecure_or_odd_port_own_host_sends_no_credentials(url):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=b"x", headers={"Content-Type": "image/png"})

    client, _ = make_client(handler)
    assert client.download(url) == (b"x", "image/png")
    assert seen == [None]


def test_get_accepts_https_default_port_absolute_url():
    client, calls = make_client(lambda r: httpx.Response(200, json={"ok": 1}))
    assert client.get("https://acme.zendesk.com:443/api/v2/x.json") == {"ok": 1}


def test_users_by_email_ignores_non_exact_matches():
    client, _ = make_client(lambda r: httpx.Response(200, json={"users": [{"id": 9, "email": "other@x.com"}]}))
    assert client.users_by_email(["a@x.com"]) == {}


def test_users_by_email_exact_match_is_case_insensitive():
    client, _ = make_client(
        lambda r: httpx.Response(200, json={"users": [{"id": 9, "email": "other@x.com"}, {"id": 1, "email": "A@X.com"}]})
    )
    assert client.users_by_email(["a@x.com"]) == {"a@x.com": {"id": 1, "email": "A@X.com"}}


def test_ticket_by_id():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/tickets/3990.json"
        return httpx.Response(200, json={"ticket": {"id": 3990, "subject": "s"}})

    client, calls = make_client(handler)
    assert client.ticket(3990) == {"id": 3990, "subject": "s"}
    assert calls["n"] == 1


def test_ticket_by_id_missing_raises_not_found():
    client, _ = make_client(lambda request: httpx.Response(404, json={"error": "RecordNotFound"}))
    with pytest.raises(ZendeskNotFound):
        client.ticket(3990)


# -- team activity read methods ---------------------------------------------------------------


def test_ticket_events_follows_next_page_until_end_of_stream():
    def handler(request):
        if request.url.params["start_time"] == "1000":
            assert request.url.params["include"] == "comment_events"
            return httpx.Response(
                200,
                json={
                    "ticket_events": [{"id": 1}],
                    "next_page": "https://acme.zendesk.com/api/v2/incremental/ticket_events.json?start_time=2000&include=comment_events",
                    "end_of_stream": False,
                },
            )
        return httpx.Response(
            200,
            json={"ticket_events": [{"id": 2}], "next_page": "https://acme.zendesk.com/never", "end_of_stream": True},
        )

    client, calls = make_client(handler)
    pages = list(client.ticket_events(1000))
    assert [[e["id"] for e in p["ticket_events"]] for p in pages] == [[1], [2]]
    assert calls["n"] == 2


def test_ticket_events_stops_without_next_page():
    client, calls = make_client(lambda r: httpx.Response(200, json={"ticket_events": [], "end_of_stream": False}))
    assert len(list(client.ticket_events(5))) == 1
    assert calls["n"] == 1


def test_logins_sends_filters():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "audit_logs": [{"id": 1, "created_at": "2026-10-15T14:00:00Z"}],
                    "next_page": "https://acme.zendesk.com/api/v2/audit_logs.json?page=2",
                },
            )
        return httpx.Response(
            200, json={"audit_logs": [{"id": 2, "created_at": "2026-10-15T20:00:00Z"}], "next_page": None}
        )

    client, _ = make_client(handler)
    start = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)
    rows = client.logins(77, start, end)
    assert [r["id"] for r in rows] == [1, 2]
    params = seen[0].url.params
    assert params["filter[actor_id]"] == "77"
    assert params["filter[action]"] == "login"
    assert params.get_list("filter[created_at][]") == ["2026-10-15T05:00:00Z", "2026-10-16T05:00:00Z"]
    assert seen[0].url.path == "/api/v2/audit_logs.json"


def test_tickets_many_chunks_by_100():
    seen = []

    def handler(request):
        seen.append(request.url.params["ids"].split(","))
        ids = [int(i) for i in seen[-1] if int(i) != 3]  # ticket 3 is not returned
        return httpx.Response(200, json={"tickets": [{"id": i, "subject": f"s{i}"} for i in ids]})

    client, calls = make_client(handler)
    out = client.tickets_many(list(range(1, 251)))
    assert calls["n"] == 3
    assert [len(c) for c in seen] == [100, 100, 50]
    assert len(out) == 249 and 3 not in out
    assert out[250]["subject"] == "s250"


def test_tickets_many_empty_makes_no_request():
    client, calls = make_client(lambda r: httpx.Response(200, json={"tickets": []}))
    assert client.tickets_many([]) == {}
    assert calls["n"] == 0


def test_open_assigned_query():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={"results": [{"id": 1}], "next_page": "https://acme.zendesk.com/api/v2/search.json?page=2"},
            )
        return httpx.Response(200, json={"results": [{"id": 2}], "next_page": None})

    client, _ = make_client(handler)
    assert [t["id"] for t in client.open_assigned(42)] == [1, 2]
    assert seen[0].url.path == "/api/v2/search.json"
    assert seen[0].url.params["query"] == "type:ticket assignee:42 status<solved"


def test_new_methods_are_get_only():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"ticket_events": [], "audit_logs": [], "tickets": [], "results": []})

    client, _ = make_client(handler)
    list(client.ticket_events(1))
    client.logins(1, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))
    client.tickets_many([1])
    client.open_assigned(1)
    assert methods == ["GET"] * 4


# -- 1:1 prep read methods --------------------------------------------------------------------

TALK_LEGS = "/api/v2/channels/voice/stats/incremental/legs.json"
PREP_START = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)  # epoch 1791262800
PREP_STOP_AFTER = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)
TALK_REQUEST_CAP = 10  # a Talk read still going after this many requests has looped


def _legs(first_id, count, updated_at="2026-10-07T06:00:00Z"):
    return [{"id": first_id + i, "updated_at": updated_at} for i in range(count)]


def _talk_next(start_time):
    return f"https://acme.zendesk.com{TALK_LEGS}?start_time={start_time}"


def talk_pages(*pages):
    """A Talk export handler that serves ``pages`` in order, then the last one again forever (as Zendesk does
    once a read reaches the present). Past TALK_REQUEST_CAP requests it fails, so a read that never stops
    fails fast instead of hanging the suite."""
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) > TALK_REQUEST_CAP:
            raise AssertionError(f"talk_export was still reading after {TALK_REQUEST_CAP} requests")
        return httpx.Response(200, json=pages[min(len(seen), len(pages)) - 1])

    return handler, seen


def test_talk_export_pages_until_end_of_stream():
    first = _legs(1, TALK_PAGE_SIZE)
    last = _legs(1001, TALK_PAGE_SIZE, "2026-10-08T06:00:00Z")
    handler, seen = talk_pages(
        {"legs": first, "next_page": _talk_next(1791400000), "end_of_stream": False},
        # full, not past stop_after, with a next_page that moves on: only end_of_stream ends the read
        {"legs": last, "next_page": _talk_next(1791500000), "end_of_stream": True},
    )
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first + last
    assert calls["n"] == 2
    assert [r.url.path for r in seen] == [TALK_LEGS, TALK_LEGS]
    assert seen[0].url.params["start_time"] == str(int(PREP_START.timestamp()))
    assert seen[1].url.params.get_list("start_time") == ["1791400000"]  # next_page carries its own query


def test_talk_export_stops_once_a_page_is_past_stop_after():
    # oldest is before stop_after (the newest being past it doesn't matter)
    first = _legs(1, TALK_PAGE_SIZE - 1, "2026-10-23T06:00:00Z") + [{"id": 1000, "updated_at": "2026-10-22T23:00:00Z"}]
    # oldest is one second past stop_after: yielded, then reading stops (the page is full and its
    # next_page moves on, so only the stop_after check ends the read)
    second = [{"id": 1001, "updated_at": "2026-10-23T05:00:01Z"}] + _legs(1002, TALK_PAGE_SIZE - 1, "2026-10-23T07:00:00Z")
    handler, _ = talk_pages(
        {"legs": first, "next_page": _talk_next(1792700000), "end_of_stream": False},
        {"legs": second, "next_page": _talk_next(1792800000), "end_of_stream": False},
        {"legs": _legs(2001, TALK_PAGE_SIZE, "2026-10-24T00:00:00Z"), "next_page": None, "end_of_stream": True},
    )
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first + second
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "first_page",
    [
        {"legs": [], "next_page": _talk_next(1791400000), "end_of_stream": False},
        # full, so only the missing next_page ends the read
        {"legs": _legs(1, TALK_PAGE_SIZE), "next_page": None, "end_of_stream": False},
    ],
    ids=["empty page", "no next_page"],
)
def test_talk_export_stops_on_empty_page_or_missing_next_page(first_page):
    handler, _ = talk_pages(first_page)
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first_page["legs"]
    assert calls["n"] == 1


def test_talk_export_stops_on_a_short_page():
    # Shaped like a real read that reached the present (2026-10-20): Zendesk sent no end_of_stream, only a
    # short page, and after it the short page's last row again and again with the same next_page.
    first = _legs(1, TALK_PAGE_SIZE, "2026-10-06T11:09:38Z")
    second = _legs(999, 582, "2026-10-15T15:45:20Z")  # starts with the first page's last two rows
    handler, seen = talk_pages(
        {"legs": first, "next_page": _talk_next(1792079120), "end_of_stream": None, "count": 1000},
        {"legs": second, "next_page": _talk_next(1792526404), "end_of_stream": None, "count": 582},
        {"legs": second[-1:], "next_page": _talk_next(1792526404), "end_of_stream": None, "count": 1},
    )
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first + second
    assert calls["n"] == 2
    assert seen[1].url.params["start_time"] == "1792079120"


@pytest.mark.parametrize(
    "stuck_next_page",
    [_talk_next(1792079120), _talk_next(1791262800), f"https://acme.zendesk.com{TALK_LEGS}"],
    ids=["repeats", "goes back", "no start_time"],
)
def test_talk_export_stops_when_next_page_does_not_advance(stuck_next_page):
    first = _legs(1, TALK_PAGE_SIZE, "2026-10-06T11:09:38Z")
    second = _legs(1001, TALK_PAGE_SIZE, "2026-10-15T15:45:20Z")
    handler, seen = talk_pages(
        {"legs": first, "next_page": _talk_next(1792079120), "end_of_stream": None},
        # full, not past stop_after, no end_of_stream: only the next_page that doesn't move on ends the read
        {"legs": second, "next_page": stuck_next_page, "end_of_stream": None},
    )
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first + second
    assert calls["n"] == 2
    assert seen[1].url.params["start_time"] == "1792079120"


def test_talk_export_stop_check_skips_rows_without_updated_at():
    # No row on the first page has an updated_at: the stop_after check is skipped there, not crashed on.
    first = [{"id": i} for i in range(1, 501)] + [{"id": i, "updated_at": None} for i in range(501, 1001)]
    # On the second page every row that has one is past stop_after: yielded, then reading stops.
    second = [{"id": 1001}] + _legs(1002, TALK_PAGE_SIZE - 1, "2026-10-24T00:00:00Z")
    handler, _ = talk_pages(
        {"legs": first, "next_page": _talk_next(1791400000), "end_of_stream": False},
        {"legs": second, "next_page": _talk_next(1791500000), "end_of_stream": False},
        {"legs": _legs(2001, TALK_PAGE_SIZE, "2026-10-25T00:00:00Z"), "next_page": _talk_next(1791600000)},
    )
    client, calls = make_client(handler)
    rows = list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    assert rows == first + second
    assert calls["n"] == 2


def test_talk_export_reads_calls_key_for_calls():
    def handler(request):
        assert request.url.path == "/api/v2/channels/voice/stats/incremental/calls.json"
        return httpx.Response(
            200,
            json={
                "calls": [{"id": 7, "direction": "inbound", "updated_at": "2026-10-07T06:00:00Z"}],
                "legs": [{"id": 99, "updated_at": "2026-10-07T06:00:00Z"}],
                "next_page": None,
                "end_of_stream": True,
            },
        )

    client, _ = make_client(handler)
    assert [r["id"] for r in client.talk_export("calls", PREP_START, PREP_STOP_AFTER)] == [7]


def test_talk_pages_yields_next_start():
    first = _legs(1, TALK_PAGE_SIZE)
    second = _legs(1001, TALK_PAGE_SIZE, "2026-10-08T06:00:00Z")
    last = _legs(2001, 12, "2026-10-09T06:00:00Z")
    handler, seen = talk_pages(
        {"legs": first, "next_page": _talk_next(1791400000), "end_of_stream": None},
        {"legs": second, "next_page": _talk_next(1791500000), "end_of_stream": None},
        # short, so the last page, though its next_page moves on (as at the present)
        {"legs": last, "next_page": _talk_next(1791600000), "end_of_stream": None},
    )
    client, calls = make_client(handler)
    pages = list(client.talk_pages("legs", PREP_START))
    assert [rows for rows, _ in pages] == [first, second, last]
    assert [next_start for _, next_start in pages] == [
        datetime(2026, 10, 7, 19, 6, 40, tzinfo=timezone.utc),  # 1791400000
        datetime(2026, 10, 8, 22, 53, 20, tzinfo=timezone.utc),  # 1791500000
        None,
    ]
    assert all(next_start.tzinfo is not None for _, next_start in pages[:2])
    assert calls["n"] == 3
    assert seen[0].url.params["start_time"] == str(int(PREP_START.timestamp()))
    assert seen[2].url.params.get_list("start_time") == ["1791500000"]  # next_page carries its own query


@pytest.mark.parametrize(
    "page",
    [
        {"legs": _legs(1, TALK_PAGE_SIZE), "next_page": _talk_next(1791400000), "end_of_stream": True},
        {"legs": [], "next_page": _talk_next(1791400000), "end_of_stream": None},
        {"legs": _legs(1, TALK_PAGE_SIZE), "next_page": None, "end_of_stream": None},
        {"legs": _legs(1, TALK_PAGE_SIZE), "next_page": _talk_next(1791262800), "end_of_stream": None},
        {"legs": _legs(1, TALK_PAGE_SIZE), "next_page": f"https://acme.zendesk.com{TALK_LEGS}", "end_of_stream": None},
    ],
    ids=["end_of_stream", "empty page", "no next_page", "next_page does not move on", "no start_time"],
)
def test_talk_pages_last_page_has_no_next_start(page):
    handler, _ = talk_pages(page)
    client, calls = make_client(handler)
    assert list(client.talk_pages("legs", PREP_START)) == [(page["legs"], None)]
    assert calls["n"] == 1


def test_satisfaction_ratings_reads_received_scores_in_window():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/api/v2/satisfaction_ratings.json"
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "satisfaction_ratings": [{"id": 1, "score": "good", "assignee_id": 10}],
                    "next_page": "https://acme.zendesk.com/api/v2/satisfaction_ratings.json?page=2",
                },
            )
        return httpx.Response(
            200, json={"satisfaction_ratings": [{"id": 2, "score": "bad", "assignee_id": 11}], "next_page": None}
        )

    client, calls = make_client(handler)
    end = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
    rows = list(client.satisfaction_ratings(PREP_START, end))
    assert [(r["id"], r["score"], r["assignee_id"]) for r in rows] == [(1, "good", 10), (2, "bad", 11)]
    assert calls["n"] == 2
    params = seen[0].url.params
    assert params["score"] == "received"
    assert params["start_time"] == str(int(PREP_START.timestamp()))
    assert params["end_time"] == str(int(end.timestamp()))
    assert seen[1].url.params["page"] == "2"
    assert seen[1].url.params.get_list("score") == []  # next_page carries its own query


def test_search_count_returns_count():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"count": 34})

    client, calls = make_client(handler)
    query = "type:ticket assignee:42 solved>=2026-10-06 solved<=2026-10-19"
    assert client.search_count(query) == 34
    assert calls["n"] == 1
    assert seen[0].url.path == "/api/v2/search/count.json"
    assert seen[0].url.params["query"] == query


def test_prep_reads_are_get_only():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(
            200, json={"legs": [], "calls": [], "satisfaction_ratings": [], "count": 0, "end_of_stream": True}
        )

    client, _ = make_client(handler)
    list(client.talk_export("legs", PREP_START, PREP_STOP_AFTER))
    list(client.talk_export("calls", PREP_START, PREP_STOP_AFTER))
    list(client.satisfaction_ratings(PREP_START, PREP_STOP_AFTER))
    client.search_count("type:ticket")
    assert methods == ["GET"] * 4


def test_transport_error_is_retried_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) <= 2:
            raise httpx.ConnectError("[WinError 10054] An existing connection was forcibly closed")
        return httpx.Response(200, json={"ok": 1})

    sleeps = []
    client, calls = make_client(handler, sleeps)
    assert client.get("/api/v2/x.json") == {"ok": 1}
    assert calls["n"] == 3
    assert sleeps == [10.0, 10.0]


def test_transport_error_gives_up_after_max_retries():
    def handler(request):
        raise httpx.ConnectError("connection to https://acme.zendesk.com/api/v2/x.json?query=email:a@b.com failed")

    sleeps = []
    client, calls = make_client(handler, sleeps)
    with pytest.raises(ZendeskError) as exc:
        client.get("/api/v2/x.json", params={"query": "email:a@b.com"})
    assert calls["n"] == MAX_RETRIES + 1
    assert sleeps == [10.0] * MAX_RETRIES
    assert str(exc.value) == f"Zendesk could not be reached for /api/v2/x.json after {MAX_RETRIES} retries"
    # The transport's own text can hold the query string, so it is not chained onto the error.
    assert exc.value.__suppress_context__ and exc.value.__cause__ is None
