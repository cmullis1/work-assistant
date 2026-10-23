"""Fetch Zendesk tickets (a window of them, or chosen ones by id) and normalize them into first contacts."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from work_assistant.models import FirstContact
from work_assistant.runlog import LogSink
from work_assistant.zendesk.client import ZendeskClient, ZendeskNotFound
from work_assistant.zendesk.normalize import build_contact, in_window, is_inbound_voice, merge_info, merge_target_id

# A ticket whose data is missing (deleted, or not visible) or malformed is skipped and logged by class name
# only: the message can echo customer-supplied or malformed input.
SKIPPED = (ZendeskNotFound, ValueError, KeyError, TypeError)


def _team_and_fields(client: ZendeskClient, team_emails: list[str], log: LogSink) -> tuple[dict, dict]:
    """The team's Zendesk user ids (id -> email) and every ticket field's title (id -> title)."""
    found = client.users_by_email(team_emails)
    team = {user["id"]: email for email, user in found.items()}
    log.info(f"matched {len(team)} of {len(team_emails)} team members in Zendesk")
    field_titles = {field_id: field.get("title") or "" for field_id, field in client.ticket_fields().items()}
    return team, field_titles


def _first_contact(client: ZendeskClient, ticket: dict, team: dict, field_titles: dict) -> FirstContact | None:
    """The first contact of an inbound voice ticket: its comments, and the merged-into ticket and its
    comments when it was merged. None when the call doesn't qualify (see `build_contact`)."""
    ticket_id = ticket["id"]
    comments, users = client.ticket_comments(ticket_id)
    contact = build_contact(ticket, comments, users, team, field_titles)
    target_id = merge_target_id(ticket, comments, users) if contact is not None else None
    if target_id is not None:
        target = client.ticket(target_id)
        target_comments, target_users = client.ticket_comments(target_id)
        contact = replace(contact, merged_into=merge_info(
            contact, target, target_comments, target_users, field_titles))
    return contact


def fetch_ticket(client: ZendeskClient, ticket_id: int, team: dict, field_titles: dict) -> FirstContact | None:
    """One ticket's first contact, read by id (GETs only); None when it isn't a qualifying inbound call.

    `team` maps the team's Zendesk user ids to their emails and `field_titles` ticket field ids to titles,
    as `fetch_tickets` reads them once for all its tickets. Missing or malformed data raises (see SKIPPED).
    """
    ticket = client.ticket(ticket_id)
    if not is_inbound_voice(ticket):
        return None
    return _first_contact(client, ticket, team, field_titles)


def fetch_tickets(client: ZendeskClient, ticket_ids: Iterable[int], team_emails: list[str],
                  log: LogSink) -> list[FirstContact]:
    """Fresh first contacts for the chosen tickets (Review again), in the order given, each read by id.

    A ticket that is missing, malformed or no longer a qualifying call is left out (missing and malformed
    ones are logged); the caller decides what to do about it. Rejected credentials and network failures
    propagate. Log lines carry only ticket ids, counts and error class names, never customer content.
    """
    ids = list(dict.fromkeys(int(t) for t in ticket_ids))
    team, field_titles = _team_and_fields(client, team_emails, log)
    contacts: list[FirstContact] = []
    for ticket_id in ids:
        try:
            contact = fetch_ticket(client, ticket_id, team, field_titles)
        except SKIPPED as exc:
            log.warn(f"skipped ticket {ticket_id}: {type(exc).__name__}")
            continue
        if contact is not None:
            contacts.append(contact)
    log.info(f"found {len(contacts)} of {len(ids)} calls")
    return contacts


def fetch_contacts(
    client: ZendeskClient,
    window: tuple[datetime, datetime],
    team_emails: list[str],
    log: LogSink,
) -> list[FirstContact]:
    """Qualifying first contacts for tickets created in ``window`` (start inclusive, end exclusive).

    A contact whose ticket was merged also gets the merged-into ticket and its comments. A ticket
    whose data (its own or its merge target's) is missing or malformed is skipped and logged.

    Log lines carry only ticket ids, counts and error class names, never customer content.
    """
    team, field_titles = _team_and_fields(client, team_emails, log)
    contacts: list[FirstContact] = []
    seen: set[int] = set()
    for ticket in client.incremental_tickets(window[0]):
        ticket_id = ticket.get("id")
        if ticket_id in seen or not is_inbound_voice(ticket):
            continue
        try:
            if not in_window(ticket, window):
                continue
            seen.add(ticket_id)
            contact = _first_contact(client, ticket, team, field_titles)
        except SKIPPED as exc:
            log.warn(f"skipped ticket {ticket_id}: {type(exc).__name__}")
            continue
        if contact is not None:
            contacts.append(contact)

    log.info(f"found {len(contacts)} qualifying calls")
    return contacts
