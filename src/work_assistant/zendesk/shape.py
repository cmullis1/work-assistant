"""Structure-only views of Zendesk JSON, so the owner can share API shapes without customer content."""
from __future__ import annotations

import re
from typing import Any

# Full paths (dict keys joined by ".", list items shown as "[]") whose scalar values are short enums,
# never free text. Matching is by whole path, not key name, so owner- or integration-defined keys
# such as users[].user_fields.status or comments[].metadata.custom.type are never kept.
# The unprefixed entries ("type", "data.direction", ...) cover a single ticket or comment shaped on its own.
KEEP_PATHS: frozenset[str] = frozenset(
    {
        "via.channel",
        "via.source.rel",
        "type",
        "priority",
        "status",
        "role",
        "direction",
        "data.direction",
        "comments[].type",
        "comments[].via.channel",
        "comments[].via.source.rel",
        "comments[].data.direction",
        "comments[].attachments[].content_type",
        "attachments[].content_type",
        "users[].role",
    }
)

# Second layer: a kept value must also look like an enum token (no spaces, short).
_ENUM_VALUE = re.compile(r"[A-Za-z0-9_./+-]{1,40}")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def shape_of(obj: Any, keep_paths: frozenset[str] = KEEP_PATHS, _path: str = "") -> Any:
    """Replace every scalar with its type name, keeping a value only when its full path is in
    `keep_paths` and it is a short enum-like string.

    Lists become {"len": n, "item": shape_of(first_item)} (an empty list has no "item").
    """
    if isinstance(obj, dict):
        return {
            key: shape_of(value, keep_paths, f"{_path}.{key}" if _path else str(key))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        result: dict[str, Any] = {"len": len(obj)}
        if obj:
            result["item"] = shape_of(obj[0], keep_paths, f"{_path}[]")
        return result
    if _path in keep_paths and isinstance(obj, str) and _ENUM_VALUE.fullmatch(obj):
        return obj
    return _type_name(obj)


PATH_VIA_SOURCE_REL = "via.source.rel"
PATH_VOICE_COMMENT = 'comments[].type == "VoiceComment"'
PATH_ANSWERED_BY = "comments[].data.answered_by_id"
PATH_CALL_DURATION = "comments[].data.call_duration"
PATH_STARTED_AT = "comments[].data.started_at"


def _data_has(comments: list[dict], key: str) -> bool:
    """Whether any VoiceComment's data has `key`. Other comment types are ignored."""
    return any(
        isinstance(c.get("data"), dict) and key in c["data"]
        for c in comments
        if isinstance(c, dict) and c.get("type") == "VoiceComment"
    )


def path_report(tickets: list[dict], comments: list[dict]) -> dict[str, bool]:
    """Whether each field path Task 6 depends on exists in the sampled voice tickets and comments.

    Returns presence flags only, never values.
    """
    via_has_rel = False
    for ticket in tickets:
        via = ticket.get("via") if isinstance(ticket, dict) else None
        source = via.get("source") if isinstance(via, dict) else None
        if isinstance(source, dict) and "rel" in source:
            via_has_rel = True
            break
    return {
        PATH_VIA_SOURCE_REL: via_has_rel,
        PATH_VOICE_COMMENT: any(isinstance(c, dict) and c.get("type") == "VoiceComment" for c in comments),
        PATH_ANSWERED_BY: _data_has(comments, "answered_by_id"),
        PATH_CALL_DURATION: _data_has(comments, "call_duration"),
        PATH_STARTED_AT: _data_has(comments, "started_at"),
    }
