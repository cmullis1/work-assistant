"""Read-only Zendesk client.

The client is physically unable to write: every request passes through a hook that raises
``ReadOnlyViolation`` for any method other than GET before anything is sent.

Exception messages carry only the status code and the request path (no query string, no body),
because response bodies and query strings can contain customer data.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

MAX_RETRIES = 5
DEFAULT_RETRY_WAIT_SECONDS = 10.0
MAX_RETRY_WAIT_SECONDS = 120.0
# A full page of a Zendesk Talk incremental export; a shorter page is the end of the export.
TALK_PAGE_SIZE = 1000

# httpx logs every request URL (including query strings that hold customer emails) at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ZendeskError(Exception):
    """Base class for Zendesk client failures."""


class ZendeskAuthError(ZendeskError):
    """Zendesk rejected the credentials (HTTP 401)."""


class ZendeskNotFound(ZendeskError):
    """The resource is missing or not visible to this user (HTTP 403 or 404)."""


class ReadOnlyViolation(ZendeskError):
    """A non-GET request was attempted; the client never writes to Zendesk."""


@dataclass(frozen=True)
class TicketPage:
    """One page of the incremental ticket export with its metric sets.

    ``after_cursor`` is where the next read starts, ``end_of_stream`` whether this page reached the present.
    """

    tickets: list[dict]
    metric_sets: list[dict]
    after_cursor: str | None
    end_of_stream: bool


def _refuse_non_get(request: httpx.Request) -> None:
    if request.method != "GET":
        raise ReadOnlyViolation(f"Refusing {request.method} request: this client is read-only")


def _aware(moment: datetime) -> datetime:
    """A naive datetime is taken as UTC (``timestamp()`` would otherwise read it as local time)."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _start_time(url: str) -> int | None:
    """The integer ``start_time`` query value of a ``next_page`` link, or None if it has none."""
    try:
        return int(httpx.URL(url).params["start_time"])
    except (KeyError, ValueError):
        return None


def _retry_wait(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            wait = float(raw)
        except ValueError:
            return DEFAULT_RETRY_WAIT_SECONDS
        if math.isfinite(wait):
            return min(max(0.0, wait), MAX_RETRY_WAIT_SECONDS)
    return DEFAULT_RETRY_WAIT_SECONDS


class ZendeskClient:
    def __init__(
        self,
        subdomain: str,
        email: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = f"https://{subdomain}.zendesk.com"
        self._host = f"{subdomain}.zendesk.com"
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self._base_url,
            auth=httpx.BasicAuth(f"{email}/token", token),
            transport=transport,
            timeout=30.0,
            event_hooks={"request": [_refuse_non_get]},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ZendeskClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- low level -----------------------------------------------------------------------

    def _is_own_host(self, url: str) -> bool:
        parsed = httpx.URL(url)
        if not parsed.host:
            return True  # relative path, resolved against the https base URL
        return parsed.scheme == "https" and parsed.host == self._host and parsed.port in (None, 443)

    def _request(
        self,
        path_or_url: str,
        params: dict | None = None,
        *,
        with_auth: bool = True,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        shown_path = httpx.URL(path_or_url).path  # never the query string
        auth = httpx.USE_CLIENT_DEFAULT if with_auth else None
        attempt = 0
        while True:
            try:
                response = self._http.get(
                    path_or_url, params=params, auth=auth, follow_redirects=follow_redirects
                )
            except httpx.TransportError:
                if attempt >= MAX_RETRIES:
                    # `from None`: the transport's own text can carry the URL with its query string.
                    raise ZendeskError(
                        f"Zendesk could not be reached for {shown_path} after {MAX_RETRIES} retries"
                    ) from None
                attempt += 1
                self._sleep(DEFAULT_RETRY_WAIT_SECONDS)
                continue
            status = response.status_code
            if status == 401:
                raise ZendeskAuthError(f"Zendesk rejected the credentials (HTTP 401) for {shown_path}")
            if status in (403, 404):
                raise ZendeskNotFound(f"Zendesk returned HTTP {status} for {shown_path}")
            if status == 429 or status >= 500:
                if attempt >= MAX_RETRIES:
                    raise ZendeskError(
                        f"Zendesk returned HTTP {status} for {shown_path} after {MAX_RETRIES} retries"
                    )
                attempt += 1
                self._sleep(_retry_wait(response))
                continue
            if not 200 <= status < 300:
                raise ZendeskError(f"Zendesk returned HTTP {status} for {shown_path}")
            return response

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET a Zendesk API path (or a same-host absolute URL such as a ``next_page`` link)."""
        if not self._is_own_host(path):
            raise ZendeskError(f"Refusing to send Zendesk credentials to another host for {httpx.URL(path).path}")
        return self._request(path, params).json()

    # -- endpoints -----------------------------------------------------------------------

    def incremental_tickets(self, start: datetime) -> Iterator[dict]:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        params: dict = {"start_time": int(start.timestamp())}
        while True:
            page = self.get("/api/v2/incremental/tickets/cursor.json", params)
            yield from page.get("tickets", [])
            cursor = page.get("after_cursor")
            if page.get("end_of_stream") or not cursor:
                return
            params = {"cursor": cursor}

    def ticket_pages(self, *, start: datetime | None = None, cursor: str | None = None) -> Iterator[TicketPage]:
        """Pages of the incremental ticket export with each ticket's metric sets, from ``start`` or ``cursor``.

        Exactly one of them is given: ``start`` for a first read, ``cursor`` (a saved ``after_cursor``) to resume.
        Reading stops after the page that has ``end_of_stream`` or no ``after_cursor``.
        """
        if (start is None) == (cursor is None):
            raise ValueError("ticket_pages needs exactly one of start or cursor")
        params: dict = {"include": "metric_sets"}
        if cursor is None:
            params["start_time"] = int(_aware(start).timestamp())
        else:
            params["cursor"] = cursor
        while True:
            page = self.get("/api/v2/incremental/tickets/cursor.json", params)
            after_cursor = page.get("after_cursor")
            end_of_stream = bool(page.get("end_of_stream"))
            yield TicketPage(
                tickets=page.get("tickets") or [],
                metric_sets=page.get("metric_sets") or [],
                after_cursor=after_cursor,
                end_of_stream=end_of_stream,
            )
            if end_of_stream or not after_cursor:
                return
            params = {"cursor": after_cursor, "include": "metric_sets"}

    def ticket(self, ticket_id: int) -> dict:
        """One ticket by id (used for a merged-into ticket)."""
        return self.get(f"/api/v2/tickets/{int(ticket_id)}.json")["ticket"]

    def ticket_comments(self, ticket_id: int) -> tuple[list[dict], dict[int, dict]]:
        comments: list[dict] = []
        users: dict[int, dict] = {}
        path: str | None = f"/api/v2/tickets/{ticket_id}/comments.json"
        params: dict | None = {"include": "users"}
        while path:
            page = self.get(path, params)
            comments.extend(page.get("comments", []))
            for user in page.get("users", []):
                users[user["id"]] = user
            path = page.get("next_page")
            params = None  # next_page already carries its query string
        return comments, users

    def ticket_fields(self) -> dict[int, dict]:
        fields: dict[int, dict] = {}
        path: str | None = "/api/v2/ticket_fields.json"
        while path:
            page = self.get(path)
            for field in page.get("ticket_fields", []):
                fields[field["id"]] = field
            path = page.get("next_page")
        return fields

    def ticket_events(self, start: int) -> Iterator[dict]:
        """Pages of department-wide ticket events from ``start`` (epoch seconds), oldest first.

        The caller decides when to stop; the stream ends at ``end_of_stream``.
        """
        path: str | None = "/api/v2/incremental/ticket_events.json"
        params: dict | None = {"start_time": int(start), "include": "comment_events"}
        while path:
            page = self.get(path, params)
            yield page
            if page.get("end_of_stream"):
                return
            path = page.get("next_page")
            params = None  # next_page already carries its query string

    def logins(self, user_id: int, start: datetime, end: datetime) -> list[dict]:
        """Sign-ins (audit log action ``login``) by one user between two instants."""

        def stamp(moment: datetime) -> str:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows: list[dict] = []
        path: str | None = "/api/v2/audit_logs.json"
        params: dict | None = {
            "filter[actor_id]": int(user_id),
            "filter[action]": "login",
            "filter[created_at][]": [stamp(start), stamp(end)],
        }
        while path:
            page = self.get(path, params)
            rows.extend(page.get("audit_logs", []))
            path = page.get("next_page")
            params = None
        return rows

    def tickets_many(self, ids: list[int]) -> dict[int, dict]:
        """Tickets by id, 100 per request. Ids Zendesk won't return are absent."""
        found: dict[int, dict] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            page = self.get("/api/v2/tickets/show_many.json", {"ids": ",".join(str(int(t)) for t in chunk)})
            for ticket in page.get("tickets", []):
                found[ticket["id"]] = ticket
        return found

    def open_assigned(self, user_id: int) -> list[dict]:
        """Tickets assigned to a user that are not yet solved."""
        tickets: list[dict] = []
        path: str | None = "/api/v2/search.json"
        params: dict | None = {"query": f"type:ticket assignee:{int(user_id)} status<solved"}
        while path:
            page = self.get(path, params)
            tickets.extend(page.get("results", []))
            path = page.get("next_page")
            params = None
        return tickets

    def talk_pages(
        self, kind: Literal["legs", "calls"], start: datetime
    ) -> Iterator[tuple[list[dict], datetime | None]]:
        """Pages of a Zendesk Talk incremental export (call legs or calls) from ``start``, each with the
        instant its next page starts from (that page's ``start_time``), or None on the last page.

        Once a read reaches the present, Zendesk sends no ``end_of_stream``: it repeats the last row with
        the same ``next_page`` forever. So the last page is the first one that has ``end_of_stream``, no
        rows, fewer than ``TALK_PAGE_SIZE`` rows, no ``next_page``, or a ``next_page`` whose ``start_time``
        is missing or not later than the ``start_time`` that produced the page.
        """
        path = f"/api/v2/channels/voice/stats/incremental/{kind}.json"
        requested_start = int(_aware(start).timestamp())
        params: dict | None = {"start_time": requested_start}
        while True:
            page = self.get(path, params)
            rows = page.get(kind) or []
            next_path = page.get("next_page")
            next_start = _start_time(next_path) if next_path else None
            if (
                page.get("end_of_stream")
                or len(rows) < TALK_PAGE_SIZE
                or next_start is None
                or next_start <= requested_start  # the read would not move forward
            ):
                yield rows, None
                return
            yield rows, datetime.fromtimestamp(next_start, timezone.utc)
            path, requested_start = next_path, next_start
            params = None  # next_page already carries its query string

    def talk_export(self, kind: Literal["legs", "calls"], start: datetime, stop_after: datetime) -> Iterator[dict]:
        """Rows of a Zendesk Talk incremental export (call legs or calls) from ``start``.

        The export is ordered by ``updated_at``, so it runs on past any window. Reading follows
        ``talk_pages``' stop rules, and also stops after the first page (its rows still yielded) whose
        oldest ``updated_at`` is later than ``stop_after`` (over the rows that have one).
        """
        stop_after = _aware(stop_after)
        for rows, next_start in self.talk_pages(kind, start):
            yield from rows
            if next_start is None:
                return  # the last page
            stamps = [datetime.fromisoformat(row["updated_at"]) for row in rows if row.get("updated_at")]
            if stamps and min(stamps) > stop_after:
                return

    def satisfaction_ratings(self, start: datetime, end: datetime) -> Iterator[dict]:
        """Received (good or bad) satisfaction ratings in Zendesk's ``start_time``..``end_time`` window."""
        path: str | None = "/api/v2/satisfaction_ratings.json"
        params: dict | None = {
            "score": "received",
            "start_time": int(_aware(start).timestamp()),
            "end_time": int(_aware(end).timestamp()),
        }
        while path:
            page = self.get(path, params)
            yield from page.get("satisfaction_ratings", [])
            path = page.get("next_page")
            params = None

    def search_count(self, query: str) -> int:
        """How many results a Zendesk search query matches, without reading them."""
        return int(self.get("/api/v2/search/count.json", {"query": query})["count"])

    def users_by_email(self, emails: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for email in emails:
            page = self.get("/api/v2/users/search.json", {"query": f"email:{email}"})
            candidates = page.get("users", [])
            exact = [u for u in candidates if (u.get("email") or "").lower() == email.lower()]
            if exact:
                found[email] = exact[0]
        return found

    def download(self, url: str) -> tuple[bytes, str]:
        """Fetch bytes and content type. Credentials go only to this Zendesk host."""
        response = self._request(url, with_auth=self._is_own_host(url), follow_redirects=True)
        return response.content, response.headers.get("Content-Type", "application/octet-stream")
