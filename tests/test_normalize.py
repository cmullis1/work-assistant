import copy
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from work_assistant.models import MergeInfo
from work_assistant.zendesk.normalize import (
    COLLECTION_WINDOW_HOURS,
    EXCLUDED_FIELD_TITLE_SUBSTRINGS,
    build_contact,
    classify_comment,
    in_window,
    is_inbound_voice,
    merge_info,
    merge_target_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "zendesk"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ticket():
    return load("ticket_inbound.json")


@pytest.fixture
def comments():
    return load("comments_inbound.json")["comments"]


@pytest.fixture
def users():
    return {u["id"]: u for u in load("users_inbound.json")["users"]}


@pytest.fixture
def field_titles():
    return {f["id"]: f["title"] for f in load("ticket_fields.json")["ticket_fields"]}


TEAM = {900: "dana.whitfield@example.com", 901: "priya.raman@example.com"}


def build(ticket, comments, users, field_titles, team=None):
    return build_contact(ticket, comments, users, TEAM if team is None else team, field_titles)


def test_inbound_voice_only():
    assert is_inbound_voice(load("ticket_inbound.json")) is True
    assert is_inbound_voice(load("ticket_outbound.json")) is False
    assert is_inbound_voice(load("ticket_email.json")) is False


def test_voicemail_is_not_inbound():
    assert load("ticket_voicemail.json")["via"]["source"]["rel"] == "voicemail"
    assert is_inbound_voice(load("ticket_voicemail.json")) is False


@pytest.mark.parametrize("via", [None, {}, {"channel": "voice"}, {"channel": "voice", "source": None}])
def test_inbound_voice_tolerates_missing_via_parts(via):
    assert is_inbound_voice({"via": via}) is False


def test_build_contact_core(ticket, comments, users, field_titles):
    contact = build(ticket, comments, users, field_titles)
    assert contact is not None
    assert contact.ticket_id == 4101
    assert contact.channel == "voice"
    assert contact.subject == "Checkout page shows a blank banner"
    assert contact.priority == "normal"
    assert contact.tags == ("voice", "web_checkout")
    assert contact.call_duration_seconds == 420
    assert contact.call_started_at == "2026-10-12T18:41:50Z"
    assert contact.agent_user_id == 900
    assert contact.agent_email == "dana.whitfield@example.com"
    assert contact.agent_name == "Dana Whitfield"
    assert contact.attribution_ok is True
    assert contact.summary_email_at == "2026-10-12T21:00:00Z"
    assert [c.kind for c in contact.comments] == [
        "other", "voice", "transcript", "call_summary", "triage_macro", "summary_email",
    ]
    assert 5007 not in [c.comment_id for c in contact.comments]


def test_build_contact_text_and_roles(ticket, comments, users, field_titles):
    contact = build(ticket, comments, users, field_titles)
    by_id = {c.comment_id: c for c in contact.comments}
    assert by_id[5002].text.startswith("Call from Morgan Ellis answered by Dana Whitfield")
    assert by_id[5003].text.startswith("Call transcript:")
    assert by_id[5003].author_role == "system"
    assert by_id[5004].author_role == "system"
    assert by_id[5001].author_role == "system"
    assert by_id[5005].author_role == "agent"
    assert by_id[5005].public is False
    assert by_id[5006].public is True


def test_images_only_from_image_attachments(ticket, comments, users, field_titles):
    contact = build(ticket, comments, users, field_titles)
    all_urls = [url for c in contact.comments for url in c.image_urls]
    assert all_urls == ["https://example.invalid/attachments/6001/banner.png"]


def test_images_capped_at_four_by_comment_order(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5005:
            comment["attachments"] = [
                {"content_url": f"https://example.invalid/a/{n}.png", "content_type": "image/png"}
                for n in range(3)
            ]
        if comment["id"] == 5006:
            comment["attachments"] = [
                {"content_url": f"https://example.invalid/b/{n}.jpg", "content_type": "image/jpeg"}
                for n in range(3)
            ]
    contact = build(ticket, comments, users, field_titles)
    urls = [url for c in contact.comments for url in c.image_urls]
    assert urls == [
        "https://example.invalid/a/0.png",
        "https://example.invalid/a/1.png",
        "https://example.invalid/a/2.png",
        "https://example.invalid/b/0.jpg",
    ]


def test_fields_drop_empty_and_show_values_unchanged(ticket, comments, users, field_titles):
    contact = build(ticket, comments, users, field_titles)
    assert contact.fields == {
        "Initial Agent Resolution": "low_solved_no_assistance",
        "Product": "Widget Pro",
    }


def test_password_field_never_reaches_contact(ticket, comments, users, field_titles):
    assert field_titles[1003] == "Remote Access Password"
    contact = build(ticket, comments, users, field_titles)
    assert "Remote Access Password" not in contact.fields
    assert "synthetic-pass-7731" not in repr(contact)
    assert "synthetic-pass-7731" not in contact.fields.values()


@pytest.mark.parametrize("title", ["Remote Access Password", "wifi PASSWORD hint", "password", "Admin Passwords"])
def test_password_exclusion_is_case_insensitive_substring(ticket, comments, users, field_titles, title):
    field_titles = dict(field_titles)
    field_titles[1005] = title
    contact = build(ticket, comments, users, field_titles)
    assert title not in contact.fields
    assert "Widget Pro" not in repr(contact)


def test_excluded_title_substrings_is_a_module_tuple():
    assert EXCLUDED_FIELD_TITLE_SUBSTRINGS == ("password",)


def test_field_with_unknown_id_is_skipped(ticket, comments, users, field_titles):
    ticket = copy.deepcopy(ticket)
    ticket["custom_fields"].append({"id": 9999, "value": "orphan"})
    contact = build(ticket, comments, users, field_titles)
    assert "orphan" not in repr(contact)


def test_attribution_mismatch(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5005:
            comment["author_id"] = 901
    contact = build(ticket, comments, users, field_titles)
    assert contact.attribution_ok is False
    assert contact.agent_user_id == 900


def test_unanswered_call_skipped(ticket, comments, users, field_titles):
    comments = [c for c in copy.deepcopy(comments) if c["id"] != 5005]
    for comment in comments:
        if comment["type"] == "VoiceComment":
            comment["data"]["answered_by_id"] = None
    assert build(ticket, comments, users, field_titles) is None


def test_not_on_team_returns_none(ticket, comments, users, field_titles):
    assert build(ticket, comments, users, field_titles, team={902: "someone.else@example.com"}) is None


def test_macro_author_on_team_flags_attribution(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5005:
            comment["author_id"] = 901
    contact = build(ticket, comments, users, field_titles, team={901: "priya.raman@example.com"})
    assert contact is not None
    assert contact.agent_user_id == 901
    assert contact.agent_email == "priya.raman@example.com"
    assert contact.agent_name == "Priya Raman"
    assert contact.attribution_ok is False


def test_no_email_cutoff(ticket, comments, users, field_titles):
    comments = [c for c in copy.deepcopy(comments) if c["id"] != 5006]
    # Call ends 2026-10-12T18:48:50Z, so the 24h cutoff is 2026-10-13T18:48:50Z.
    comments.append(
        {"id": 5008, "type": "Comment", "author_id": 700, "public": True,
         "created_at": "2026-10-13T18:48:50Z", "body": "exactly at the cutoff", "attachments": []}
    )
    comments.append(
        {"id": 5009, "type": "Comment", "author_id": 700, "public": True,
         "created_at": "2026-10-13T18:48:51Z", "body": "one second late", "attachments": []}
    )
    contact = build(ticket, comments, users, field_titles)
    assert contact.summary_email_at is None
    ids = [c.comment_id for c in contact.comments]
    assert ids == [5001, 5002, 5003, 5004, 5005, 5007, 5008]
    assert all(c.kind != "summary_email" for c in contact.comments)


def customer_note(comment_id, created_at):
    return {"id": comment_id, "type": "Comment", "author_id": 700, "public": True, "created_at": created_at,
            "body": "customer follow-up", "attachments": []}


def test_collection_window_independent_of_deadline(ticket, comments, users, field_titles, target):
    """Later comments are gathered over a fixed 24 hours after the call, whatever the email deadline (a
    30-minute deadline would otherwise hide a triage macro added an hour later)."""
    assert COLLECTION_WINDOW_HOURS == 24
    assert not any("deadline" in name for name in inspect.signature(build_contact).parameters)
    assert not any("deadline" in name for name in inspect.signature(merge_info).parameters)
    # The call ends 2026-10-12T18:48:50Z. No summary email: 5006 (the email) and 5007 (Priya's reply) are dropped.
    own = [c for c in copy.deepcopy(comments) if c["id"] not in (5006, 5007)]
    own += [customer_note(5010, "2026-10-12T20:48:50Z"),   # 2 hours after the call
            customer_note(5011, "2026-10-13T19:48:50Z")]   # 25 hours after
    contact = build(ticket, own, users, field_titles)
    ids = [c.comment_id for c in contact.comments]
    assert 5010 in ids and 5011 not in ids

    target_ticket, target_comments = target
    target_comments = target_comments + [agent_note(7010, "Two hours on", created_at="2026-10-12T20:48:50Z"),
                                         agent_note(7011, "A day on", created_at="2026-10-13T19:48:50Z")]
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    merged_ids = [c.comment_id for c in info.comments]
    assert 7010 in merged_ids and 7011 not in merged_ids


def test_summary_email_must_be_after_call_end(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    comments.append(
        {"id": 5000, "type": "Comment", "author_id": 900, "public": True,
         "created_at": "2026-10-12T18:45:00Z", "body": "mid-call public note", "attachments": []}
    )
    contact = build(ticket, comments, users, field_titles)
    by_id = {c.comment_id: c for c in contact.comments}
    assert by_id[5000].kind == "other"
    assert by_id[5006].kind == "summary_email"
    assert contact.summary_email_at == "2026-10-12T21:00:00Z"


def test_reply_from_a_different_agent_is_not_the_summary_email(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5006:
            comment["author_id"] = 901
    contact = build(ticket, comments, users, field_titles)
    assert contact.summary_email_at is None
    assert all(c.kind != "summary_email" for c in contact.comments)


def test_voice_comment_without_started_at_uses_created_at(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["type"] == "VoiceComment":
            del comment["data"]["started_at"]
    contact = build(ticket, comments, users, field_titles)
    assert contact.call_started_at == "2026-10-12T18:48:50Z"


def test_earliest_voice_comment_supplies_call_facts(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    comments.append(
        {"id": 5020, "type": "VoiceComment", "author_id": 700, "public": False,
         "created_at": "2026-10-12T20:00:00Z", "body": "second call",
         "data": {"answered_by_id": 901, "call_duration": 60, "started_at": "2026-10-12T19:59:00Z"},
         "attachments": []}
    )
    contact = build(ticket, comments, users, field_titles)
    assert contact.agent_user_id == 900
    assert contact.call_duration_seconds == 420


@pytest.mark.parametrize(
    "started_at",
    [
        "2026-10-12T18:41:50Z",
        "2026-10-12T20:41:50+02:00",
        "2026-10-12T18:41:50.123Z",
        "2026-10-12 18:41:50 +0000",
    ],
)
def test_started_at_formats_normalize_to_utc(ticket, comments, users, field_titles, started_at):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["type"] == "VoiceComment":
            comment["data"]["started_at"] = started_at
    contact = build(ticket, comments, users, field_titles)
    assert contact.call_started_at == "2026-10-12T18:41:50Z"


@pytest.mark.parametrize("started_at", ["garbage", 12345])
def test_unparseable_started_at_raises(ticket, comments, users, field_titles, started_at):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["type"] == "VoiceComment":
            comment["data"]["started_at"] = started_at
    with pytest.raises((ValueError, TypeError)):
        build(ticket, comments, users, field_titles)


@pytest.mark.parametrize("missing", ["absent", None])
def test_missing_call_duration_is_malformed_not_zero(ticket, comments, users, field_titles, missing):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["type"] == "VoiceComment":
            if missing == "absent":
                del comment["data"]["call_duration"]
            else:
                comment["data"]["call_duration"] = None
    with pytest.raises(ValueError):
        build(ticket, comments, users, field_titles)


def test_zero_call_duration_is_kept(ticket, comments, users, field_titles):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["type"] == "VoiceComment":
            comment["data"]["call_duration"] = 0
    assert build(ticket, comments, users, field_titles).call_duration_seconds == 0


def test_unanswered_call_without_duration_is_skipped_quietly(ticket, comments, users, field_titles):
    comments = [c for c in copy.deepcopy(comments) if c["id"] != 5005]
    for comment in comments:
        if comment["type"] == "VoiceComment":
            comment["data"]["answered_by_id"] = None
            del comment["data"]["call_duration"]
    assert build(ticket, comments, users, field_titles) is None


def test_ticket_without_voice_comment_returns_none(ticket, comments, users, field_titles):
    comments = [c for c in comments if c["type"] != "VoiceComment"]
    assert build(ticket, comments, users, field_titles) is None


def test_non_inbound_ticket_returns_none(comments, users, field_titles):
    assert build(load("ticket_outbound.json"), comments, users, field_titles) is None
    assert build(load("ticket_voicemail.json"), comments, users, field_titles) is None


def test_window_edges():
    start = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    assert in_window({"created_at": "2026-10-11T00:00:00Z"}, (start, end)) is True
    assert in_window({"created_at": "2026-10-17T23:59:59Z"}, (start, end)) is True
    assert in_window({"created_at": "2026-10-18T00:00:00Z"}, (start, end)) is False
    assert in_window({"created_at": "2026-10-10T23:59:59Z"}, (start, end)) is False


# -- classify_comment ---------------------------------------------------------------------


def raw_comment(**overrides):
    base = {"id": 1, "type": "Comment", "author_id": 900, "public": False,
            "created_at": "2026-10-12T19:00:00Z", "body": "hello", "attachments": []}
    base.update(overrides)
    return base


AGENT = {"id": 900, "name": "Dana Whitfield", "role": "agent"}
ADMIN = {"id": 901, "name": "Priya Raman", "role": "admin"}
CUSTOMER = {"id": 700, "name": "Morgan Ellis", "role": "end-user"}
SYSTEM_USER = {"id": 801, "name": "Call Assistant", "role": "system"}


def test_author_roles():
    assert classify_comment(raw_comment(), AGENT).author_role == "agent"
    assert classify_comment(raw_comment(author_id=901), ADMIN).author_role == "agent"
    assert classify_comment(raw_comment(author_id=700), CUSTOMER).author_role == "end-user"


def test_system_author_as_missing_user():
    comment = classify_comment(raw_comment(author_id=555, body="Call transcript:\nhi"), None)
    assert comment.author_role == "system"
    assert comment.kind == "transcript"


def test_system_author_as_id_minus_one():
    comment = classify_comment(raw_comment(author_id=-1, body="Call summary:\nok"), {})
    assert comment.author_role == "system"
    assert comment.kind == "call_summary"
    comment = classify_comment(raw_comment(author_id=-1), AGENT)
    assert comment.author_role == "system"


def test_system_author_as_system_role_user():
    comment = classify_comment(raw_comment(author_id=801, body="Call transcript:\nhi"), SYSTEM_USER)
    assert comment.author_role == "system"
    assert comment.kind == "transcript"


def test_voice_comment_uses_body_and_is_voice():
    raw = raw_comment(type="VoiceComment", body="Call recording note", author_id=700,
                      data={"answered_by_id": 900})
    comment = classify_comment(raw, CUSTOMER)
    assert comment.kind == "voice"
    assert comment.text == "Call recording note"


def test_text_prefers_plain_body_falling_back_to_body():
    assert classify_comment(raw_comment(plain_body="plain", body="body"), AGENT).text == "plain"
    assert classify_comment(raw_comment(plain_body=None, body="body"), AGENT).text == "body"
    assert classify_comment(raw_comment(plain_body="", body="body"), AGENT).text == "body"


def test_triage_macro_requires_internal_agent_note_with_both_markers():
    text = "Issue Details: x\nImpact & Priority: y"
    assert classify_comment(raw_comment(body=text), AGENT).kind == "triage_macro"
    assert classify_comment(raw_comment(body="Issue Details: only"), AGENT).kind == "other"
    assert classify_comment(raw_comment(body=text, public=True), AGENT).kind == "other"
    assert classify_comment(raw_comment(body=text, author_id=700), CUSTOMER).kind == "other"


def test_attachments_without_urls_or_non_images_are_ignored():
    raw = raw_comment(attachments=[
        {"content_url": "https://example.invalid/x.png", "content_type": "image/png"},
        {"content_url": "https://example.invalid/x.pdf", "content_type": "application/pdf"},
        {"content_type": "image/png"},
    ])
    assert classify_comment(raw, AGENT).image_urls == ("https://example.invalid/x.png",)


# -- dropped-call callbacks (spec §7.2 rule 6) --------------------------------------------------------


def outbound_call(comment_id=5101, started_at="2026-10-12T18:51:50Z", created_at="2026-10-12T18:55:00Z",
                  rel="outbound"):
    """A synthetic VoiceComment for a later call on the same ticket."""
    return {
        "id": comment_id,
        "type": "VoiceComment",
        "author_id": 700,
        "public": False,
        "created_at": created_at,
        "body": "Outbound call to Morgan Ellis by Dana Whitfield. Duration 2 minutes.",
        "data": {"call_id": 88002, "answered_by_id": 900, "call_duration": 120, "started_at": started_at},
        "attachments": [],
        "via": {"channel": "voice", "source": {"rel": rel}},
    }


def system_comment(comment_id, created_at, body):
    return {"id": comment_id, "type": "Comment", "author_id": -1, "public": False, "created_at": created_at,
            "body": body, "plain_body": body, "attachments": [], "via": {"channel": "voice"}}


def test_callback_gap_computed(ticket, comments, users, field_titles):
    # The call ran 18:41:50 + 420 s = 18:48:50; the outbound call started 3 minutes later.
    contact = build(ticket, comments + [outbound_call()], users, field_titles)
    assert contact.callback_minutes == 3.0


def test_callback_gap_rounds_up_to_tenths(ticket, comments, users, field_titles):
    contact = build(ticket, comments + [outbound_call(started_at="2026-10-12T18:53:51Z")], users, field_titles)
    assert contact.callback_minutes == 5.1  # 5 min 1 s never shows as 5.0


def test_callback_none_without_outbound_call(ticket, comments, users, field_titles):
    assert build(ticket, comments, users, field_titles).callback_minutes is None
    later_inbound = outbound_call(rel="inbound")
    assert build(ticket, comments + [later_inbound], users, field_titles).callback_minutes is None


def test_outbound_callback_kept_in_comments(ticket, comments, users, field_titles):
    extra = [
        outbound_call(),
        system_comment(5102, "2026-10-12T18:56:00Z", "Call transcript:\n[00:01] Agent: Sorry, we got cut off."),
    ]
    contact = build(ticket, comments + extra, users, field_titles)
    by_id = {c.comment_id: c for c in contact.comments}
    assert by_id[5101].kind == "voice"
    assert by_id[5102].kind == "transcript"


def test_callback_after_summary_email_still_kept_with_its_transcript(ticket, comments, users, field_titles):
    # The summary email went out at 21:00; a callback at 22:00 is still continuation material.
    extra = [
        outbound_call(started_at="2026-10-12T22:00:00Z", created_at="2026-10-12T22:03:00Z"),
        system_comment(5102, "2026-10-12T22:04:00Z", "Call transcript:\n[00:01] Agent: Calling you back."),
        system_comment(5103, "2026-10-12T22:04:30Z", "Call summary:\nAgent called back."),
    ]
    contact = build(ticket, comments + extra, users, field_titles)
    ids = [c.comment_id for c in contact.comments]
    assert 5101 in ids and 5102 in ids and 5103 in ids
    assert 5007 not in ids  # the next-day reply stays outside the agent's part
    assert contact.summary_email_at == "2026-10-12T21:00:00Z"
    assert contact.callback_minutes == 191.2


def test_callback_after_the_collection_window_is_measured_but_not_kept(ticket, comments, users, field_titles):
    late = outbound_call(started_at="2026-10-14T09:00:00Z", created_at="2026-10-14T09:03:00Z")
    contact = build(ticket, comments + [late], users, field_titles)
    assert contact.callback_minutes is not None and contact.callback_minutes > 24 * 60
    assert 5101 not in [c.comment_id for c in contact.comments]


# -- merges (spec §7.2 rule 7) ------------------------------------------------------------------------


def agent_note(comment_id, body, created_at="2026-10-12T18:52:00Z", author_id=900):
    return {"id": comment_id, "type": "Comment", "author_id": author_id, "public": False, "created_at": created_at,
            "body": body, "plain_body": body, "attachments": [], "via": {"channel": "web"}}


def test_merge_detected_via_comment_text(ticket, comments, users):
    merged = comments + [agent_note(5201, 'This request was closed and merged into request #3990 "Receipt printer".')]
    assert merge_target_id(ticket, merged, users) == 3990
    merged = comments + [agent_note(5201, "Merged into ticket #3990.")]
    assert merge_target_id(ticket, merged, users) == 3990
    merged = comments + [agent_note(5201, "MERGED INTO #3990")]
    assert merge_target_id(ticket, merged, users) == 3990


def test_merge_detected_via_tag(ticket, comments, users):
    tagged = {**ticket, "tags": ["voice", "closed_by_merge"]}
    with_note = comments + [agent_note(5201, "Same site already has an open ticket, merged with #3990.")]
    assert merge_target_id(tagged, with_note, users) == 3990
    # Without the tag, that free-text wording is not taken as a merge.
    assert merge_target_id(ticket, with_note, users) is None


def test_no_merge(ticket, comments, users):
    assert merge_target_id(ticket, comments, users) is None
    tagged_without_number = {**ticket, "tags": ["closed_by_merge"]}
    assert merge_target_id(tagged_without_number, comments, users) is None


def test_customer_text_never_triggers_a_merge_fetch(ticket, comments, users):
    from_customer = comments + [agent_note(5201, "Please see merged into request #3990", author_id=700)]
    assert merge_target_id(ticket, from_customer, users) is None


def test_transcript_words_never_trigger_a_merge_fetch(ticket, comments, users):
    # Transcripts and call summaries are system comments, but they carry what the customer said.
    tagged = {**ticket, "tags": ["closed_by_merge"]}
    spoken = comments + [
        system_comment(5202, "2026-10-12T18:50:00Z", "Call transcript:\n[00:40] Customer: it was merged into ticket #3990"),
        system_comment(5203, "2026-10-12T18:50:10Z", "Call summary:\nCustomer says it was merged into #3990."),
    ]
    assert merge_target_id(tagged, spoken, users) is None


def public_note(comment_id, body, created_at):
    return {**agent_note(comment_id, body, created_at=created_at), "public": True, "via": {"channel": "email"}}


def test_public_merge_note_is_never_the_summary_email(ticket, comments, users, field_titles):
    note = public_note(5201, "This request was closed and merged into request #3990.", "2026-10-12T18:52:00Z")
    contact = build(ticket, comments + [note], users, field_titles)
    assert contact.summary_email_at == "2026-10-12T21:00:00Z"  # the real email, not the merge note
    kinds = {c.comment_id: c.kind for c in contact.comments}
    assert kinds[5201] == "other" and kinds[5006] == "summary_email"


def test_public_merge_note_alone_leaves_no_summary_email(ticket, comments, users, field_titles):
    only_note = [c for c in comments if c["id"] != 5006] + [
        public_note(5201, "Merged into ticket #3990", "2026-10-12T18:52:00Z")
    ]
    assert build(ticket, only_note, users, field_titles).summary_email_at is None


def test_merge_target_notice_is_never_the_summary_email(ticket, comments, users, field_titles, target):
    contact = build(ticket, comments, users, field_titles)
    target_ticket, target_comments = target
    notice = public_note(7010, "Request #4101 \"Checkout page\" was closed and merged into this request.",
                         "2026-10-12T18:52:00Z")
    info = merge_info(contact, target_ticket, target_comments + [notice], users, field_titles)
    kinds = {c.comment_id: c.kind for c in info.comments}
    assert kinds[7010] == "other" and kinds[7003] == "summary_email"


def test_merge_into_itself_is_ignored(ticket, comments, users):
    own = comments + [agent_note(5201, f"merged into request #{ticket['id']}")]
    assert merge_target_id(ticket, own, users) is None


@pytest.fixture
def target():
    return load("ticket_merge_target.json"), load("comments_merge_target.json")["comments"]


def test_merge_info_from_target(ticket, comments, users, field_titles, target):
    contact = build(ticket, comments, users, field_titles)
    target_ticket, target_comments = target
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    assert isinstance(info, MergeInfo)
    assert info.ticket_id == 3990
    assert info.subject == "Receipt printer offline at Example Store"
    assert info.location == "LOC-2201"  # the target's "Location ID" field
    assert info.created_at == "2026-10-12T14:00:00Z"
    # From this call's start (18:41:50) through call end + 24 h (2026-10-13T18:48:50).
    assert [c.comment_id for c in info.comments] == [7002, 7003]
    assert [c.kind for c in info.comments] == ["triage_macro", "summary_email"]
    assert info.comments[0].image_urls == ("https://example.invalid/attachments/6101/printer.png",)


def test_merge_info_email_by_the_answerer_when_the_macro_author_stands_in(ticket, comments, users, field_titles,
                                                                         target):
    """Dana (900) answered but isn't on the team, so the macro's author Priya (901) stands in as the agent. As
    on the call's own ticket, an email on the merged-into ticket from either of them is the summary email."""
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5005:
            comment["author_id"] = 901
    contact = build(ticket, comments, users, field_titles, team={901: "priya.raman@example.com"})
    assert (contact.agent_user_id, contact.answered_by_user_id) == (901, 900)
    target_ticket, target_comments = target
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    assert {c.comment_id: c.kind for c in info.comments}[7003] == "summary_email"  # Dana's email


def test_merge_info_email_by_someone_else_is_not_the_summary_email(ticket, comments, users, field_titles, target):
    # Dana (900) answered and wrote the macro; Priya's (901) public reply on the target is not Dana's email.
    contact = build(ticket, comments, users, field_titles)
    assert (contact.agent_user_id, contact.answered_by_user_id) == (900, 900)
    target_ticket, target_comments = target
    target_comments = [c for c in copy.deepcopy(target_comments) if c["id"] != 7003]
    target_comments.append({**public_note(7010, "Priya here, following up.", "2026-10-12T19:40:00Z"),
                            "author_id": 901})
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    assert all(c.kind != "summary_email" for c in info.comments)


def test_merge_info_excludes_password_fields(ticket, comments, users, field_titles, target):
    contact = build(ticket, comments, users, field_titles)
    target_ticket, target_comments = target
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    assert "synthetic-target-pass-5521" not in repr(info)
    # Even a password-titled field named like the location field is dropped.
    sneaky = {**field_titles, 1006: "Location ID password"}
    assert merge_info(contact, target_ticket, target_comments, users, sneaky).location is None


def test_merge_info_images_share_the_contact_cap(ticket, comments, users, field_titles, target):
    comments = copy.deepcopy(comments)
    for comment in comments:
        if comment["id"] == 5005:
            comment["attachments"] = [
                {"content_url": f"https://example.invalid/a/{n}.png", "content_type": "image/png"} for n in range(4)
            ]
    contact = build(ticket, comments, users, field_titles)
    target_ticket, target_comments = target
    info = merge_info(contact, target_ticket, target_comments, users, field_titles)
    assert all(c.image_urls == () for c in info.comments)


def test_merge_info_location_missing(ticket, comments, users, field_titles, target):
    contact = build(ticket, comments, users, field_titles)
    target_ticket, target_comments = target
    no_location = {**target_ticket, "custom_fields": []}
    assert merge_info(contact, no_location, target_comments, users, field_titles).location is None
