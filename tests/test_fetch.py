import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from work_assistant.zendesk.client import ZendeskAuthError, ZendeskClient
from work_assistant.zendesk.fetch import fetch_contacts, fetch_ticket, fetch_tickets

FIXTURES = Path(__file__).parent / "fixtures" / "zendesk"
WINDOW = (datetime(2026, 10, 11, tzinfo=timezone.utc), datetime(2026, 10, 18, tzinfo=timezone.utc))
TEAM_EMAILS = ["dana.whitfield@example.com", "priya.raman@example.com"]


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeLog:
    def __init__(self):
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg):
        self.infos.append(msg)

    def warn(self, msg):
        self.warns.append(msg)

    @property
    def lines(self):
        return self.infos + self.warns


def make_client(tickets, comment_pages=None, missing=(), unauthorized=(), targets=None, requested=None):
    """A real ZendeskClient over a MockTransport that serves the synthetic fixtures.

    `targets` maps ticket id to a ticket served by GET /api/v2/tickets/{id}.json (merge targets);
    `requested`, when given, collects every request path.
    """
    comment_pages = comment_pages or {}
    targets = targets or {}
    users = load("users_inbound.json")["users"]
    fields = load("ticket_fields.json")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if requested is not None:
            requested.append(path)
        if path == "/api/v2/incremental/tickets/cursor.json":
            return httpx.Response(200, json={"tickets": tickets, "end_of_stream": True, "after_cursor": "c"})
        if path == "/api/v2/ticket_fields.json":
            return httpx.Response(200, json=fields)
        if path == "/api/v2/users/search.json":
            query = request.url.params["query"]
            email = query.removeprefix("email:")
            return httpx.Response(200, json={"users": [u for u in users if u["email"] == email]})
        if path.startswith("/api/v2/tickets/") and path.endswith("/comments.json"):
            ticket_id = int(path.split("/")[4])
            if ticket_id in unauthorized:
                return httpx.Response(401, json={"error": "nope"})
            if ticket_id in missing:
                return httpx.Response(404, json={"error": "RecordNotFound"})
            page = comment_pages.get(ticket_id) or load("comments_inbound.json")["comments"]
            return httpx.Response(200, json={"comments": page, "users": users, "next_page": None})
        if path.startswith("/api/v2/tickets/") and path.endswith(".json"):
            ticket_id = int(path.split("/")[4].removesuffix(".json"))
            if ticket_id in missing or ticket_id not in targets:
                return httpx.Response(404, json={"error": "RecordNotFound"})
            return httpx.Response(200, json={"ticket": targets[ticket_id]})
        return httpx.Response(500, json={"error": "unexpected request"})

    return ZendeskClient("acme", "owner@example.com", "tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def second_ticket(ticket_id=4201):
    ticket = copy.deepcopy(load("ticket_inbound.json"))
    ticket["id"] = ticket_id
    ticket["created_at"] = "2026-10-14T15:00:00Z"
    return ticket


def test_fetch_returns_qualifying_contacts_only():
    tickets = [
        load("ticket_inbound.json"),
        load("ticket_outbound.json"),
        load("ticket_voicemail.json"),
        load("ticket_email.json"),
    ]
    log = FakeLog()
    contacts = fetch_contacts(make_client(tickets), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4101]
    assert contacts[0].agent_email == "dana.whitfield@example.com"
    assert contacts[0].fields["Initial Agent Resolution"] == "low_solved_no_assistance"
    assert "Remote Access Password" not in contacts[0].fields
    assert "found 1 qualifying calls" in log.infos


def test_fetch_skips_deleted_ticket():
    tickets = [load("ticket_inbound.json"), second_ticket(4201)]
    log = FakeLog()
    contacts = fetch_contacts(make_client(tickets, missing={4101}), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4201]
    skipped = [line for line in log.lines if "skipped ticket" in line]
    assert len(skipped) == 1
    assert "4101" in skipped[0]
    assert "found 1 qualifying calls" in log.infos


def test_fetch_empty_week():
    log = FakeLog()
    assert fetch_contacts(make_client([]), WINDOW, TEAM_EMAILS, log) == []
    assert "found 0 qualifying calls" in log.infos


def test_fetch_excludes_tickets_outside_window_edges():
    at_start = second_ticket(4301)
    at_start["created_at"] = "2026-10-11T00:00:00Z"
    at_end = second_ticket(4302)
    at_end["created_at"] = "2026-10-18T00:00:00Z"
    before = second_ticket(4303)
    before["created_at"] = "2026-10-10T23:59:59Z"
    log = FakeLog()
    contacts = fetch_contacts(make_client([at_start, at_end, before]), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4301]


def test_fetch_skips_tickets_not_answered_by_team():
    log = FakeLog()
    contacts = fetch_contacts(make_client([load("ticket_inbound.json")]), WINDOW, ["priya.raman@example.com"], log)
    # Dana (900) answered and wrote the macro, so with only Priya on the team nobody qualifies.
    assert contacts == []
    assert "found 0 qualifying calls" in log.infos


def test_fetch_dedupes_repeated_tickets():
    ticket = load("ticket_inbound.json")
    log = FakeLog()
    contacts = fetch_contacts(make_client([ticket, copy.deepcopy(ticket)]), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4101]


def test_fetch_lets_auth_errors_through():
    with pytest.raises(ZendeskAuthError):
        fetch_contacts(make_client([load("ticket_inbound.json")], unauthorized={4101}), WINDOW, TEAM_EMAILS, FakeLog())


def test_fetch_logs_contain_no_customer_content():
    tickets = [load("ticket_inbound.json"), second_ticket(4201)]
    log = FakeLog()
    fetch_contacts(make_client(tickets, missing={4201}), WINDOW, TEAM_EMAILS, log)
    joined = "\n".join(log.lines)
    for secret in ("blank banner", "Morgan", "555", "dana.whitfield", "priya.raman", "synthetic-pass"):
        assert secret not in joined


def comments_with(mutator):
    comments = copy.deepcopy(load("comments_inbound.json")["comments"])
    mutator(comments)
    return comments


def set_voice(key, value):
    def mutate(comments):
        for comment in comments:
            if comment["type"] == "VoiceComment":
                if value is DELETE:
                    del comment["data"][key]
                else:
                    comment["data"][key] = value
    return mutate


DELETE = object()


def test_fetch_skips_malformed_ticket_and_keeps_the_rest():
    tickets = [load("ticket_inbound.json"), second_ticket(4201)]
    pages = {4101: comments_with(set_voice("started_at", "garbage"))}
    log = FakeLog()
    contacts = fetch_contacts(make_client(tickets, comment_pages=pages), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4201]
    assert "skipped ticket 4101: ValueError" in log.warns
    assert "found 1 qualifying calls" in log.infos
    joined = "\n".join(log.lines)
    for secret in ("garbage", "blank banner", "Morgan", "555", "dana.whitfield"):
        assert secret not in joined


@pytest.mark.parametrize(
    "mutator",
    [
        set_voice("call_duration", DELETE),
        set_voice("call_duration", "not-a-number"),
        lambda comments: comments[0].pop("id"),
        lambda comments: comments[0].pop("created_at"),
    ],
)
def test_fetch_skips_other_malformed_comment_shapes(mutator):
    tickets = [load("ticket_inbound.json"), second_ticket(4201)]
    pages = {4101: comments_with(mutator)}
    log = FakeLog()
    contacts = fetch_contacts(make_client(tickets, comment_pages=pages), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4201]
    assert any(line.startswith("skipped ticket 4101: ") for line in log.warns)


def test_fetch_skips_ticket_with_unparseable_created_at():
    bad = second_ticket(4202)
    bad["created_at"] = "garbage"
    log = FakeLog()
    contacts = fetch_contacts(make_client([bad, load("ticket_inbound.json")]), WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4101]
    assert "skipped ticket 4202: ValueError" in log.warns


# -- merges (spec §7.2 rule 7) ------------------------------------------------------------------------


MERGE_NOTE = {
    "id": 5201, "type": "Comment", "author_id": 900, "public": False, "created_at": "2026-10-12T18:52:00Z",
    "body": "This request was closed and merged into request #3990.",
    "plain_body": "This request was closed and merged into request #3990.",
    "attachments": [], "via": {"channel": "web"},
}


def merged_setup():
    ticket = load("ticket_inbound.json")
    ticket["tags"] = ["voice", "closed_by_merge"]
    pages = {
        4101: load("comments_inbound.json")["comments"] + [MERGE_NOTE],
        3990: load("comments_merge_target.json")["comments"],
    }
    return ticket, pages, {3990: load("ticket_merge_target.json")}


def test_fetch_attaches_merge_target():
    ticket, pages, targets = merged_setup()
    requested = []
    log = FakeLog()
    client = make_client([ticket], comment_pages=pages, targets=targets, requested=requested)
    [contact] = fetch_contacts(client, WINDOW, TEAM_EMAILS, log)
    assert "/api/v2/tickets/3990.json" in requested
    assert "/api/v2/tickets/3990/comments.json" in requested
    info = contact.merged_into
    assert info.ticket_id == 3990
    assert info.location == "LOC-2201"
    assert info.subject == "Receipt printer offline at Example Store"
    assert [c.comment_id for c in info.comments] == [7002, 7003]
    assert "synthetic-target-pass" not in repr(contact)
    joined = "\n".join(log.lines)
    for secret in ("Receipt printer", "LOC-2201", "Morgan", "synthetic-target-pass"):
        assert secret not in joined


def test_fetch_without_merge_makes_no_extra_requests():
    requested = []
    [contact] = fetch_contacts(make_client([load("ticket_inbound.json")], requested=requested),
                               WINDOW, TEAM_EMAILS, FakeLog())
    assert contact.merged_into is None
    assert not [p for p in requested if p.startswith("/api/v2/tickets/") and p.count("/") == 4]


def test_fetch_skips_ticket_whose_merge_target_is_missing():
    ticket, pages, _ = merged_setup()
    log = FakeLog()
    contacts = fetch_contacts(make_client([ticket, second_ticket(4201)], comment_pages=pages, targets={}),
                              WINDOW, TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4201]
    assert "skipped ticket 4101: ZendeskNotFound" in log.warns


def test_fetch_tickets_reads_only_those_tickets():
    """T-045: Review again reads the chosen calls by id (GETs only), never the incremental export."""
    tickets = {4101: load("ticket_inbound.json"), 4201: second_ticket(4201)}
    requested = []
    log = FakeLog()
    client = make_client([], targets=tickets, requested=requested)
    contacts = fetch_tickets(client, [4101], TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4101]
    assert contacts[0].agent_email == "dana.whitfield@example.com"
    assert "Remote Access Password" not in contacts[0].fields
    assert "/api/v2/incremental/tickets/cursor.json" not in requested
    assert "/api/v2/tickets/4101.json" in requested and "/api/v2/tickets/4101/comments.json" in requested
    assert not [p for p in requested if "4201" in p]
    assert "found 1 of 1 calls" in log.infos


def test_fetch_tickets_skips_missing_and_non_qualifying_tickets():
    outbound = load("ticket_outbound.json")
    tickets = {4101: load("ticket_inbound.json"), outbound["id"]: outbound}
    log = FakeLog()
    contacts = fetch_tickets(make_client([], targets=tickets), [4999, outbound["id"], 4101], TEAM_EMAILS, log)
    assert [c.ticket_id for c in contacts] == [4101]
    assert "skipped ticket 4999: ZendeskNotFound" in log.warns
    assert "found 1 of 3 calls" in log.infos


def test_fetch_ticket_attaches_merge_target():
    ticket, pages, targets = merged_setup()
    client = make_client([], comment_pages=pages, targets={**targets, 4101: ticket})
    field_titles = {i: f.get("title") or "" for i, f in client.ticket_fields().items()}
    team = {user["id"]: email for email, user in client.users_by_email(TEAM_EMAILS).items()}
    contact = fetch_ticket(client, 4101, team, field_titles)
    assert contact.ticket_id == 4101 and contact.merged_into.ticket_id == 3990


def test_fetch_tickets_lets_auth_errors_through():
    with pytest.raises(ZendeskAuthError):
        fetch_tickets(make_client([], targets={4101: load("ticket_inbound.json")}, unauthorized={4101}), [4101],
                      TEAM_EMAILS, FakeLog())


def test_fetch_lets_auth_errors_on_merge_target_through():
    ticket, pages, targets = merged_setup()
    with pytest.raises(ZendeskAuthError):
        fetch_contacts(make_client([ticket], comment_pages=pages, targets=targets, unauthorized={3990}),
                       WINDOW, TEAM_EMAILS, FakeLog())
