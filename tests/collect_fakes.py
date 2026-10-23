"""A stand-in for the reads the hourly collection makes (`activity.collect`): the Talk exports, the incremental ticket
export, satisfaction ratings and ticket comments, canned and recorded. `test_activity_sync.FakeClient` builds on it, so
every sync test's client answers them (by default with nothing to collect)."""
from __future__ import annotations

from work_assistant.activity.store import parse_iso
from work_assistant.zendesk.client import TicketPage


class CollectFake:
    """`legs` and `calls` are each Talk export's pages as (rows, next start or None), and `ticket_export` the ticket
    export's TicketPages: served in order whatever the start, and an Exception in a page's place is raised when that
    page is reached. `ratings` are served when made inside the window asked for (both ends included); an Exception
    there is raised. `comments` are each ticket's comments by id; an Exception in their place is raised. Every read is
    recorded in `talk_reads`, `ticket_reads`, `rating_windows` and `comment_reads`."""

    def __init__(self, *, legs=None, calls=None, ticket_export=None, ratings=(), comments=None):
        self.talk = {"legs": [([], None)] if legs is None else list(legs),
                     "calls": [([], None)] if calls is None else list(calls)}
        self.ticket_export = ([TicketPage([], [], "cursor-0", True)] if ticket_export is None
                              else list(ticket_export))
        self.ratings = ratings
        self.comments = comments or {}
        self.talk_reads: list[tuple] = []
        self.ticket_reads: list[dict] = []
        self.rating_windows: list[tuple] = []
        self.comment_reads: list[int] = []

    def talk_pages(self, kind, start):
        self.talk_reads.append((kind, start))
        for one in self.talk[kind]:
            if isinstance(one, Exception):
                raise one
            yield one

    def ticket_pages(self, *, start=None, cursor=None):
        self.ticket_reads.append({"start": start, "cursor": cursor})
        for one in self.ticket_export:
            if isinstance(one, Exception):
                raise one
            yield one

    def satisfaction_ratings(self, start, end):
        self.rating_windows.append((start, end))
        if isinstance(self.ratings, Exception):
            raise self.ratings
        for row in self.ratings:
            if start <= parse_iso(row["created_at"]) <= end:
                yield row

    def ticket_comments(self, ticket_id):
        self.comment_reads.append(ticket_id)
        found = self.comments.get(ticket_id, [])
        if isinstance(found, Exception):
            raise found
        return list(found), {}
