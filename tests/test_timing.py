import dataclasses

import pytest

from work_assistant.models import FirstContact, TimedParams
from work_assistant.rubric import ensure_seeded, save_version
from work_assistant.timing import format_minutes, timed_facts


def contact(
    duration=420,
    priority="normal",
    tags=(),
    started="2026-10-05T10:00:00Z",
    email_at="2026-10-05T12:25:00Z",
):
    return FirstContact(
        ticket_id=1,
        channel="voice",
        subject="s",
        priority=priority,
        tags=tuple(tags),
        fields={},
        call_started_at=started,
        call_duration_seconds=duration,
        agent_user_id=1,
        agent_email="a@example.com",
        agent_name="Agent",
        attribution_ok=True,
        summary_email_at=email_at,
        comments=(),
    )


@pytest.fixture
def version(conn):
    return ensure_seeded(conn)


def test_short_normal_call_met(version):
    f = timed_facts(contact(duration=420), version)
    assert f.call_minutes == 7.0
    assert f.exempt is False
    assert f.decided["pr-call-length"] == "met"
    assert f.evidence["pr-call-length"] == "Call lasted 7.0 min (limit 10)."


def test_long_normal_call_missed(version):
    f = timed_facts(contact(duration=720), version)
    assert f.decided["pr-call-length"] == "missed"
    assert f.evidence["pr-call-length"] == "Call lasted 12.0 min (limit 10)."


def test_urgent_not_decided(version):
    f = timed_facts(contact(duration=1500, priority="urgent"), version)
    assert "pr-call-length" not in f.decided
    assert "pr-call-length" not in f.evidence
    assert f.exempt is True
    assert f.call_minutes == 25.0


def test_urgent_priority_is_case_insensitive(version):
    f = timed_facts(contact(duration=1500, priority="Urgent"), version)
    assert f.exempt is True
    assert "pr-call-length" not in f.decided


def test_exempt_tag(conn, version):
    v = save_version(
        conn, version.checks, version.reference,
        TimedParams(exempt_tags=("site_down",)), "tag exempt",
    )
    f = timed_facts(contact(duration=1500, tags=("site_down", "other")), v)
    assert f.exempt is True
    assert "pr-call-length" not in f.decided
    f2 = timed_facts(contact(duration=1500, tags=("other",)), v)
    assert f2.exempt is False
    assert f2.decided["pr-call-length"] == "missed"


def day_version(conn, version):
    """The seed rubric with the old 24-hour (1440-minute) deadline."""
    return save_version(conn, version.checks, version.reference,
                        dataclasses.replace(version.params, email_deadline_minutes=1440), "24 hours")


def test_email_deadline_in_minutes(version):
    # The seed deadline is 30 minutes. The call ends at 10:07.
    assert version.params.email_deadline_minutes == 30
    f = timed_facts(contact(email_at="2026-10-05T10:19:00Z"), version)  # 12 min
    assert f.email_sent is True
    assert f.minutes_to_email == 12
    assert f.decided["pr-email-sent"] == "met"
    assert f.evidence["pr-email-sent"].startswith(
        "Summary email sent 12 min after the call ended (limit 30 min)")
    f = timed_facts(contact(email_at="2026-10-05T10:52:00Z"), version)  # 45 min
    assert f.minutes_to_email == 45
    assert f.decided["pr-email-sent"] == "missed"
    assert f.evidence["pr-email-sent"].startswith(
        "Summary email sent 45 min after the call ended (limit 30 min)")
    f = timed_facts(contact(email_at=None), version)
    assert f.email_sent is False
    assert f.minutes_to_email is None
    assert f.decided["pr-email-sent"] == "missed"
    assert f.evidence["pr-email-sent"] == "No summary email found."


def test_email_evidence_in_hours_and_minutes(conn, version):
    v = day_version(conn, version)
    f = timed_facts(contact(), v)  # call ends 10:07, email 12:25 -> 2 h 18 min
    assert f.minutes_to_email == 138
    assert f.decided["pr-email-sent"] == "met"
    assert f.evidence["pr-email-sent"] == (
        "Summary email sent 2 h 18 min after the call ended (limit 24 h)."
    )
    f = timed_facts(contact(email_at="2026-10-06T16:07:00Z"), v)  # 30 h
    assert f.minutes_to_email == 1800
    assert f.decided["pr-email-sent"] == "missed"
    assert f.evidence["pr-email-sent"] == (
        "Summary email sent 30 h after the call ended (limit 24 h)."
    )


def test_email_exactly_at_deadline_met(version):
    f = timed_facts(contact(email_at="2026-10-05T10:37:00Z"), version)  # 30 min
    assert f.decided["pr-email-sent"] == "met"
    assert f.evidence["pr-email-sent"].startswith("Summary email sent 30 min after the call ended (limit 30 min)")


def test_email_one_second_inside_the_deadline_met(version):
    # 29 min 59 s against the 30-minute limit: met, and the rounded-up figure reads "30 min", never over it.
    f = timed_facts(contact(email_at="2026-10-05T10:36:59Z"), version)
    assert version.params.email_deadline_minutes == 30
    assert f.decided["pr-email-sent"] == "met"
    assert f.minutes_to_email == 30
    assert f.evidence["pr-email-sent"].startswith("Summary email sent 30 min after the call ended (limit 30 min)")


def test_email_one_second_late_evidence_consistent(version):
    # Rounded up to whole minutes, so the shown figure never contradicts the miss.
    f = timed_facts(contact(email_at="2026-10-05T10:37:01Z"), version)  # 30 min 1 s
    assert f.decided["pr-email-sent"] == "missed"
    assert f.minutes_to_email == 31
    assert f.evidence["pr-email-sent"].startswith("Summary email sent 31 min after the call ended (limit 30 min)")


def test_format_minutes():
    assert format_minutes(30) == "30 min"
    assert format_minutes(60) == "1 h"
    assert format_minutes(90) == "1 h 30 min"
    assert format_minutes(1440) == "24 h"


def test_disabled_timed_check_not_decided(conn, version):
    checks = tuple(
        dataclasses.replace(c, enabled=False) if c.timed else c for c in version.checks
    )
    v = save_version(conn, checks, version.reference, version.params, "disabled")
    f = timed_facts(contact(duration=720, email_at=None), v)
    assert f.decided == {}
    assert f.evidence == {}
    assert f.call_minutes == 12.0
    assert f.email_sent is False


def test_absent_timed_check_not_decided(conn, version):
    checks = tuple(c for c in version.checks if c.timed != "call_length")
    v = save_version(conn, checks, version.reference, version.params, "absent")
    f = timed_facts(contact(duration=720), v)
    assert "pr-call-length" not in f.decided
    assert "pr-email-sent" in f.decided


def test_boundary_601s_evidence_consistent(version):
    f = timed_facts(contact(duration=601), version)
    assert f.decided["pr-call-length"] == "missed"
    assert f.evidence["pr-call-length"] == "Call lasted 10.1 min (limit 10)."
    assert f.call_minutes == 10.1


def test_boundary_600s_met(version):
    f = timed_facts(contact(duration=600), version)
    assert f.decided["pr-call-length"] == "met"
    assert f.evidence["pr-call-length"] == "Call lasted 10.0 min (limit 10)."


def merged(c, comments=()):
    from work_assistant.models import MergeInfo

    return dataclasses.replace(c, merged_into=MergeInfo(ticket_id=3990, subject="s", location="L-1",
                                                        created_at="2026-10-04T00:00:00Z", comments=comments))


def merged_email(created_at):
    from work_assistant.models import Comment

    return Comment(comment_id=72, created_at=created_at, author_id=1, author_name="Agent", author_role="agent",
                   public=True, kind="summary_email", text="Hi, thanks for calling.", image_urls=())


def test_merged_call_without_its_own_email_leaves_email_rule_to_the_model(version):
    # After a merge the summary email is normally on the merged-into ticket (spec §7.2 rule 7), and
    # whether the merge was correct is the reviewer's call, so the code must not record a miss.
    f = timed_facts(merged(contact(email_at=None)), version)
    assert "pr-email-sent" not in f.decided
    assert "pr-email-sent" not in f.evidence
    assert f.email_sent is False
    assert f.decided["pr-call-length"] == "met"


def test_merged_call_with_its_own_email_is_still_decided(version):
    f = timed_facts(merged(contact(email_at="2026-10-05T10:19:00Z")), version)
    assert f.decided["pr-email-sent"] == "met"


def test_merged_into_email_gap_is_measured(version):
    # The call ends at 10:07. The merged-into ticket's email came 2 hours later, past the 30-minute deadline.
    # The reviewer still judges the rule, since whether the merge was correct is its call, but from the gap.
    f = timed_facts(merged(contact(email_at=None), comments=(merged_email("2026-10-05T12:07:00Z"),)), version)
    assert f.merged_email_minutes == 120
    assert "pr-email-sent" not in f.decided
    f = timed_facts(merged(contact(email_at=None)), version)
    assert f.merged_email_minutes is None
    assert timed_facts(contact(), version).merged_email_minutes is None
