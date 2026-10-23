"""Turn one Zendesk ticket event into the actions the team did in it.

An event is one save. Its `updater_id` is who saved, but triggers that fire during the save
are recorded inside the same event, so each child event's own `via` decides whether it was
the tech's doing ("Rule" means a trigger).
"""

from datetime import datetime, timezone

from work_assistant.activity.model import Action

# The key that holds the new value in a `Change` child, and the action it becomes.
_CHANGE_KEYS = (
    ("status", "status"),
    ("assignee_id", "assignee"),
    ("group_id", "group"),
    ("priority", "priority"),
)


def _channel(via) -> str:
    """A child's `via` is a string or a dict with a `channel`."""
    if isinstance(via, dict):
        via = via.get("channel")
    return via if isinstance(via, str) else ""


def _text(value) -> str | None:
    return None if value is None else str(value)


def _person(value, team: dict[int, str]) -> str | None:
    """An assignee as the record names them: a team member's email, else the Zendesk id as text."""
    if value is None:
        return None
    try:
        email = team.get(int(value))
    except (TypeError, ValueError):
        email = None
    return email or str(value)


def _when(event: dict) -> datetime:
    return datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)


def classify_event(event: dict, team: dict[int, str]) -> list[Action]:
    """The team's actions in one ticket event. `team` maps a Zendesk user id to an email."""
    updater = team.get(event.get("updater_id"))
    ticket_id = event["ticket_id"]
    event_id = event["id"]
    at = _when(event)

    def make(email: str, kind: str, child_id: int, old: str | None = None, new: str | None = None) -> Action:
        return Action(email=email, ticket_id=ticket_id, at=at, kind=kind, old=old, new=new, event_id=event_id, child_id=child_id)

    if _channel(event.get("via")) == "Merge":
        return [make(updater, "merge", 0)] if updater else []

    actions: list[Action] = []
    for child in event.get("child_events") or []:
        child_id = child["id"]
        kind = child.get("event_type")
        if kind == "Comment":
            author = team.get(child.get("author_id"))
            if author:
                actions.append(make(author, "reply" if child.get("public") else "note", child_id))
        elif kind == "VoiceApiComment":
            author_id = child.get("author_id")
            author = team.get(author_id) if author_id is not None else updater
            if author:
                actions.append(make(author, "call", child_id))
        elif kind == "Change":
            if updater is None or _channel(child.get("via")) == "Rule":
                continue
            for key, action_kind in _CHANGE_KEYS:
                if key in child:
                    old, new = child.get("previous_value"), child[key]
                    if action_kind == "assignee":
                        old, new = _person(old, team), _person(new, team)
                    else:
                        old, new = _text(old), _text(new)
                    actions.append(make(updater, action_kind, child_id, old, new))
                    break
            else:
                if "custom_ticket_fields" in child:
                    actions.append(make(updater, "fields", child_id))
        elif kind == "Create":
            if updater is not None and "status" in child:
                actions.append(make(updater, "created", child_id))
    return actions
