from datetime import datetime, timezone

from work_assistant.activity.classify import classify_event
from work_assistant.activity.model import Action

TEAM = {10: "tech@example.com", 11: "other@example.com"}
AT = datetime(2026, 10, 15, 14, 3, 22, tzinfo=timezone.utc)


def event(children, updater_id=10, via="Web form", id=500):
    return {
        "id": id,
        "ticket_id": 9001,
        "created_at": "2026-10-15T14:03:22Z",
        "updater_id": updater_id,
        "via": via,
        "child_events": children,
    }


def action(kind, child_id, old=None, new=None, email="tech@example.com"):
    return Action(email=email, ticket_id=9001, at=AT, kind=kind, old=old, new=new, event_id=500, child_id=child_id)


def test_rule_child_inside_agent_event_is_dropped():
    children = [
        {"id": 1, "event_type": "Change", "custom_ticket_fields": {"1": "x"}, "via": "Rule"},
        {"id": 2, "event_type": "Change", "tags": ["a"], "via": "Rule"},
        {"id": 3, "event_type": "Change", "status": "pending", "previous_value": "open", "via": "Web form"},
    ]
    assert classify_event(event(children), TEAM) == [action("status", 3, "open", "pending")]


def test_public_and_private_comments():
    children = [
        {"id": 1, "event_type": "Comment", "author_id": 10, "public": True, "via": "Web form"},
        {"id": 2, "event_type": "Comment", "author_id": 10, "public": False, "via": "Web form"},
        {"id": 3, "event_type": "Comment", "author_id": 99, "public": True, "via": "Mail"},  # customer
        {"id": 4, "event_type": "Comment", "author_id": 11, "public": True, "via": "Web form"},
    ]
    assert classify_event(event(children, updater_id=99), TEAM) == [
        action("reply", 1),
        action("note", 2),
        action("reply", 4, email="other@example.com"),
    ]


def test_voice_comment_is_call():
    children = [
        {"id": 1, "event_type": "VoiceApiComment", "author_id": 11, "via": "voice"},
        {"id": 2, "event_type": "VoiceApiComment", "author_id": None, "via": "voice"},
        {"id": 3, "event_type": "VoiceApiComment", "author_id": 99, "via": "voice"},
    ]
    assert classify_event(event(children), TEAM) == [
        action("call", 1, email="other@example.com"),
        action("call", 2),
    ]


def test_voice_comment_without_author_by_non_team_updater_is_ignored():
    children = [{"id": 1, "event_type": "VoiceApiComment", "via": "voice"}]
    assert classify_event(event(children, updater_id=-1), TEAM) == []


def test_merge_event_is_one_merge_action():
    children = [
        {"id": 1, "event_type": "Comment", "author_id": 10, "public": False, "via": "system"},
        {"id": 2, "event_type": "Comment", "author_id": 10, "public": True, "via": {"channel": "merge"}},
        {"id": 3, "event_type": "Change", "status": "closed", "previous_value": "open", "via": "Merge"},
    ]
    assert classify_event(event(children, via="Merge"), TEAM) == [action("merge", 0)]


def test_merge_event_by_non_team_updater_is_ignored():
    assert classify_event(event([], updater_id=99, via="Merge"), TEAM) == []


def test_system_event_ignored():
    children = [{"id": 1, "event_type": "Change", "status": "open", "previous_value": "new", "via": "Rule"}]
    assert classify_event(event(children, updater_id=-1, via="Rule"), TEAM) == []
    # a non-Rule child under a system updater is still nobody's action
    children = [{"id": 1, "event_type": "Change", "status": "open", "previous_value": "new", "via": "Web form"}]
    assert classify_event(event(children, updater_id=-1), TEAM) == []


def test_child_via_dict_or_string():
    children = [
        {"id": 1, "event_type": "Change", "priority": "high", "previous_value": "normal", "via": {"channel": "Rule"}},
        {"id": 2, "event_type": "Change", "priority": "high", "previous_value": "normal", "via": {"channel": "Web form"}},
        {"id": 3, "event_type": "Change", "group_id": 7, "previous_value": 5, "via": "Web form"},
        {"id": 4, "event_type": "Change", "assignee_id": 10, "previous_value": None, "via": {"channel": "Web form", "source": {}}},
        {"id": 5, "event_type": "Change", "priority": "low", "previous_value": "normal"},  # no via at all
    ]
    assert classify_event(event(children), TEAM) == [
        action("priority", 2, "normal", "high"),
        action("group", 3, "5", "7"),
        action("assignee", 4, None, "tech@example.com"),
        action("priority", 5, "normal", "low"),
    ]


def test_custom_fields_become_fields_without_values():
    children = [{"id": 1, "event_type": "Change", "custom_ticket_fields": {"12": "a"}, "previous_value": "b", "via": "Web form"}]
    assert classify_event(event(children), TEAM) == [action("fields", 1)]


def test_tags_and_unknown_changes_are_not_actions():
    children = [
        {"id": 1, "event_type": "Change", "tags": ["x"], "via": "Web form"},
        {"id": 2, "event_type": "Change", "subject": "new subject", "via": "Web form"},
        {"id": 3, "event_type": "Notification", "via": "Web form"},
    ]
    assert classify_event(event(children), TEAM) == []


def test_create_with_status_is_created():
    children = [
        {"id": 1, "event_type": "Create", "subject": "hi", "via": "Web form"},
        {"id": 2, "event_type": "Create", "status": "new", "via": "Web form"},
    ]
    assert classify_event(event(children), TEAM) == [action("created", 2)]


def test_event_without_children_or_with_non_team_updater():
    assert classify_event(event([]), TEAM) == []
    children = [{"id": 1, "event_type": "Change", "status": "solved", "previous_value": "open", "via": "Web form"}]
    assert classify_event(event(children, updater_id=99), TEAM) == []


def test_assignee_change_to_a_team_member_is_stored_as_their_email():
    children = [{"id": 1, "event_type": "Change", "assignee_id": 11, "previous_value": None, "via": "Web form"}]
    assert classify_event(event(children), TEAM) == [action("assignee", 1, None, "other@example.com")]
    children = [{"id": 2, "event_type": "Change", "assignee_id": "10", "previous_value": "11", "via": "Web form"}]
    assert classify_event(event(children), TEAM) == [action("assignee", 2, "other@example.com", "tech@example.com")]


def test_assignee_change_to_someone_outside_the_team_keeps_the_id():
    children = [{"id": 1, "event_type": "Change", "assignee_id": 777, "previous_value": 11, "via": "Web form"}]
    assert classify_event(event(children), TEAM) == [action("assignee", 1, "other@example.com", "777")]
