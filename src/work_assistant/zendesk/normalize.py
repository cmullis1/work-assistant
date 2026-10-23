"""Raw Zendesk JSON -> FirstContact.

This is the only module that knows Zendesk field names.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from work_assistant.models import Comment, FirstContact, MergeInfo

# Custom fields whose title contains any of these (case-insensitive) are dropped before anything
# is stored or sent to the reviewer. Add entries here to exclude more sensitive fields.
EXCLUDED_FIELD_TITLE_SUBSTRINGS: tuple[str, ...] = ("password",)

MAX_IMAGES_PER_CONTACT = 4

# Later comments, callbacks and the merged-into ticket's comments are gathered up to this long after the
# call ends. It is fixed, not the summary-email deadline: a short deadline would otherwise hide a triage
# macro or callback that came later, and change scores.
COLLECTION_WINDOW_HOURS = 24

_TRANSCRIPT_PREFIX = "Call transcript:"
_SUMMARY_PREFIX = "Call summary:"
_MACRO_MARKERS = ("Issue Details", "Impact & Priority")
_AGENT_ROLES = ("agent", "admin")

# Merges (spec §7.2 rule 7). Zendesk tags a ticket closed by a merge and adds a comment naming the
# target; agents can reword that comment, so with the tag any "#number" in a merge note counts.
_MERGE_TAG = "closed_by_merge"
_MERGED_INTO = re.compile(r"merged into (?:request |ticket )?#(\d+)", re.IGNORECASE)
_TICKET_NUMBER = re.compile(r"#(\d+)")
# The notes a merge leaves on the source ("...merged into request #N") and on the target ("Request
# #N ... was closed and merged into this request"). Either can be public, but neither is ever the
# summary email.
_MERGE_NOTE = re.compile(
    r"merged into (?:request |ticket )?#\d+|was closed and merged into this request", re.IGNORECASE
)
LOCATION_FIELD_TITLE = "Location ID"


# -- time helpers -------------------------------------------------------------------------


def _parse_time(value: str) -> datetime:
    """Parse a Zendesk timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ceil_minutes(delta: timedelta) -> float:
    """Minutes rounded UP to one decimal place, so a gap never displays as shorter than it was."""
    tenth = timedelta(seconds=6)
    return -((-delta) // tenth) / 10


# -- ticket-level predicates --------------------------------------------------------------


def is_inbound_voice(ticket: dict) -> bool:
    """True for tickets created by an inbound phone call (not outbound calls, voicemails or other channels)."""
    via = ticket.get("via") or {}
    source = via.get("source") or {}
    return via.get("channel") == "voice" and source.get("rel") == "inbound"


def in_window(ticket: dict, window: tuple[datetime, datetime]) -> bool:
    """True when the ticket was created in [start, end)."""
    start, end = window
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    created = _parse_time(ticket["created_at"])
    return start <= created < end


# -- comments -----------------------------------------------------------------------------


def _is_system(raw: dict, author: dict | None) -> bool:
    return not author or author.get("role") == "system" or raw.get("author_id") == -1


def _author_role(raw: dict, author: dict | None) -> str:
    if _is_system(raw, author):
        return "system"
    if author.get("role") in _AGENT_ROLES:
        return "agent"
    return "end-user"


def _comment_text(raw: dict) -> str:
    if raw.get("type") == "VoiceComment":
        return raw.get("body") or ""
    return raw.get("plain_body") or raw.get("body") or ""


def _image_urls(raw: dict) -> tuple[str, ...]:
    urls = []
    for attachment in raw.get("attachments") or []:
        content_type = attachment.get("content_type") or ""
        url = attachment.get("content_url")
        if content_type.startswith("image/") and url:
            urls.append(url)
    return tuple(urls)


def classify_comment(raw: dict, author: dict | None) -> Comment:
    """Turn one raw comment into a Comment.

    A summary email can only be recognised with the whole call in view, so it is assigned
    later by ``build_contact``; here such a comment is ``other``.
    """
    role = _author_role(raw, author)
    public = bool(raw.get("public", False))
    text = _comment_text(raw)
    stripped = text.lstrip()
    if raw.get("type") == "VoiceComment":
        kind = "voice"
    elif stripped.startswith(_TRANSCRIPT_PREFIX):
        kind = "transcript"
    elif stripped.startswith(_SUMMARY_PREFIX):
        kind = "call_summary"
    elif not public and role == "agent" and all(marker in text for marker in _MACRO_MARKERS):
        kind = "triage_macro"
    else:
        kind = "other"
    name = (author or {}).get("name") or ("System" if role == "system" else "")
    return Comment(
        comment_id=raw["id"],
        created_at=raw["created_at"],
        author_id=raw.get("author_id", -1),
        author_name=name,
        author_role=role,
        public=public,
        kind=kind,
        text=text,
        image_urls=_image_urls(raw),
    )


# -- custom fields ------------------------------------------------------------------------


def _is_excluded_title(title: str) -> bool:
    lowered = title.lower()
    return any(part in lowered for part in EXCLUDED_FIELD_TITLE_SUBSTRINGS)


def _display_value(value) -> str | None:
    if value is None or value is False or value == "" or value == []:
        return None
    if value is True:
        return "true"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _fields(ticket: dict, field_titles: dict[int, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for entry in ticket.get("custom_fields") or []:
        title = field_titles.get(entry.get("id"))
        if not title or _is_excluded_title(title):
            continue
        shown = _display_value(entry.get("value"))
        if shown is not None:
            fields[title] = shown
    return fields


# -- the first contact --------------------------------------------------------------------


def _summary_email_index(classified: list[Comment], senders: set[int | None], call_end: datetime) -> int | None:
    """The summary email: the first public agent reply after the call ended by one of `senders`, the
    answering agent and, when the triage macro's author stands in as the agent, that author too. A
    merge note is never it."""
    return next(
        (
            i
            for i, c in enumerate(classified)
            if c.kind == "other"
            and c.public
            and c.author_role == "agent"
            and c.author_id in senders
            and _parse_time(c.created_at) > call_end
            and not _MERGE_NOTE.search(c.text)
        ),
        None,
    )


def build_contact(
    ticket: dict,
    comments: list[dict],
    users: dict[int, dict],
    team: dict[int, str],
    field_titles: dict[int, str],
) -> FirstContact | None:
    """The agent's part of an inbound call, or None when the ticket does not qualify.

    ``team`` maps Zendesk user id to email. Window filtering happens in ``fetch_contacts``.
    """
    if not is_inbound_voice(ticket):
        return None

    ordered = sorted(
        enumerate(comments), key=lambda pair: (_parse_time(pair[1]["created_at"]), pair[0])
    )
    raw_by_id = {raw["id"]: raw for _, raw in ordered}
    classified = [classify_comment(raw, users.get(raw.get("author_id"))) for _, raw in ordered]

    voice_index = next((i for i, c in enumerate(classified) if c.kind == "voice"), None)
    if voice_index is None:
        return None
    voice_raw = raw_by_id[classified[voice_index].comment_id]
    data = voice_raw.get("data") or {}

    answered_by = data.get("answered_by_id")
    macro = next((c for c in classified if c.kind == "triage_macro"), None)
    if answered_by in team:
        agent_id = answered_by
        attribution_ok = macro is None or macro.author_id == agent_id
    elif macro is not None and macro.author_id in team:
        agent_id = macro.author_id
        attribution_ok = False
    else:
        return None

    started = _parse_time(data.get("started_at") or voice_raw["created_at"])
    duration = _call_duration(data)
    call_end = started + timedelta(seconds=duration)
    cutoff = call_end + timedelta(hours=COLLECTION_WINDOW_HOURS)
    callback_index, callback_start = _callback(classified, raw_by_id, voice_index)

    email_index = _summary_email_index(classified, {agent_id, answered_by}, call_end)
    if email_index is not None:
        classified[email_index] = replace(classified[email_index], kind="summary_email")
        end = email_index + 1
        summary_email_at = classified[email_index].created_at
    else:
        # Comments are in time order, so the ones within the collection window are a prefix.
        end = next((i for i, c in enumerate(classified) if _parse_time(c.created_at) > cutoff), len(classified))
        summary_email_at = None
    # A callback is a continuation of the contact, so it and its transcript stay in the agent's part
    # even when it came after the summary email, provided it started within the collection window.
    if callback_index is not None and callback_start <= cutoff:
        end = max(end, _callback_tail(classified, callback_index) + 1)

    kept = _cap_images(classified[:end])

    agent_user = users.get(agent_id) or {}
    return FirstContact(
        ticket_id=ticket["id"],
        channel="voice",
        subject=ticket.get("subject") or "",
        priority=ticket.get("priority"),
        tags=tuple(ticket.get("tags") or ()),
        fields=_fields(ticket, field_titles),
        call_started_at=_iso_utc(started),
        call_duration_seconds=duration,
        agent_user_id=agent_id,
        answered_by_user_id=answered_by,
        agent_email=team.get(agent_id) or agent_user.get("email") or "",
        agent_name=agent_user.get("name") or (data.get("answered_by_name") if agent_id == answered_by else "") or "",
        attribution_ok=attribution_ok,
        summary_email_at=summary_email_at,
        comments=tuple(kept),
        callback_minutes=(
            _ceil_minutes(max(callback_start - call_end, timedelta(0))) if callback_start is not None else None
        ),
    )


def _is_outbound(raw: dict) -> bool:
    via = raw.get("via") or {}
    return (via.get("source") or {}).get("rel") == "outbound"


def _callback(
    classified: list[Comment], raw_by_id: dict[int, dict], voice_index: int
) -> tuple[int | None, datetime | None]:
    """Index and start time of the first outbound call on the ticket after the answered call."""
    for index in range(voice_index + 1, len(classified)):
        if classified[index].kind != "voice":
            continue
        raw = raw_by_id[classified[index].comment_id]
        if _is_outbound(raw):
            data = raw.get("data") or {}
            return index, _parse_time(data.get("started_at") or raw["created_at"])
    return None, None


def _callback_tail(classified: list[Comment], callback_index: int) -> int:
    """Index of the last of the callback's own transcript and call summary (or the callback itself)."""
    tail = callback_index
    seen: set[str] = set()
    for index in range(callback_index + 1, len(classified)):
        kind = classified[index].kind
        if kind == "voice":
            break  # the next call's material is not this callback's
        if kind in ("transcript", "call_summary") and kind not in seen:
            seen.add(kind)
            tail = index
    return tail


# -- merges -------------------------------------------------------------------------------


def merge_target_id(ticket: dict, comments: list[dict], users: dict[int, dict]) -> int | None:
    """The ticket this one was merged into, or None.

    Only agent and system notes are read: a customer's words must never make the fetcher pull in
    another ticket, so customer comments, call records, transcripts and call summaries are skipped.
    A merge into the ticket itself is ignored.
    """
    own_id = ticket.get("id")
    notes = (classify_comment(raw, users.get(raw.get("author_id"))) for raw in comments)
    texts = [
        note.text
        for note in notes
        if note.author_role != "end-user" and note.kind not in ("voice", "transcript", "call_summary")
    ]
    found = [int(m.group(1)) for text in texts for m in _MERGED_INTO.finditer(text)]
    if _MERGE_TAG in (ticket.get("tags") or ()):
        found += [
            int(m.group(1)) for text in texts if "merge" in text.lower() for m in _TICKET_NUMBER.finditer(text)
        ]
    return next((target for target in found if target != own_id), None)


def merge_info(
    contact: FirstContact,
    target: dict,
    comments: list[dict],
    users: dict[int, dict],
    field_titles: dict[int, str],
) -> MergeInfo:
    """The merged-into ticket as the reviewer sees it.

    Its comments run from this call's start through call end + the collection window. The first
    public reply after the call ends by the people who may send the call's summary email (as on the
    call's own ticket) is marked as the summary email. Images share the contact's cap, after the
    contact's own. Password-titled fields are dropped as for any ticket.
    """
    start = _parse_time(contact.call_started_at)
    call_end = start + timedelta(seconds=contact.call_duration_seconds)
    cutoff = call_end + timedelta(hours=COLLECTION_WINDOW_HOURS)
    ordered = sorted(enumerate(comments), key=lambda pair: (_parse_time(pair[1]["created_at"]), pair[0]))
    classified = [
        classify_comment(raw, users.get(raw.get("author_id")))
        for _, raw in ordered
        if start <= _parse_time(raw["created_at"]) <= cutoff
    ]
    email_index = _summary_email_index(classified, {contact.agent_user_id, contact.answered_by_user_id}, call_end)
    if email_index is not None:
        classified[email_index] = replace(classified[email_index], kind="summary_email")
    used = sum(len(c.image_urls) for c in contact.comments)
    return MergeInfo(
        ticket_id=target["id"],
        subject=target.get("subject") or "",
        location=_fields(target, field_titles).get(LOCATION_FIELD_TITLE),
        created_at=_iso_utc(_parse_time(target["created_at"])),
        comments=tuple(_cap_images(classified, MAX_IMAGES_PER_CONTACT - used)),
    )


def _call_duration(data: dict) -> int:
    """Seconds, from an answered call's data. A missing duration is malformed, not zero: a silent 0
    would pass the call-length rule automatically."""
    raw = data.get("call_duration")
    if raw is None or isinstance(raw, bool):
        raise ValueError("call_duration missing")
    return int(raw)


def _cap_images(comments: list[Comment], limit: int = MAX_IMAGES_PER_CONTACT) -> list[Comment]:
    remaining = max(limit, 0)
    capped = []
    for comment in comments:
        if comment.image_urls:
            urls = comment.image_urls[:remaining]
            remaining -= len(urls)
            if len(urls) != len(comment.image_urls):
                comment = replace(comment, image_urls=urls)
        capped.append(comment)
    return capped
