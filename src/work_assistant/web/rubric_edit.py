"""The Rubric & rules screen's write side: turning the page's edited rubric into a new version.

The page sends the whole edited rubric (`checks`, `reference`, `params`) plus a `note`, and
optionally the `base_version_id` it was editing. `parse_edit` validates it against the current
version and the checks known from history, and returns what `rubric.save_version` needs:

- A check with a `check_id` keeps it; the id must be known (current, or retired in an earlier
  version, which restores it). A check without one is new and gets `rubric.new_check_id()`.
- Checks left out of `checks` are retired as of the new version (never erased).
- Scorecard items are numbered 1..n in the order sent (display order, not identity); process rules
  have no number. Any `number` sent is ignored.
- Process rules have 0 points and are never auto-fail (only scorecard items affect the score).
- `timed` can't be set on a new check (a new kind of timed rule needs code), and an existing check's
  `timed` value can't be changed or removed. Leaving `timed` out keeps the stored value.

Every error message is shown to the owner as is.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from work_assistant import rubric
from work_assistant.models import CheckDef, RubricVersion, TimedParams

KINDS = ("scorecard", "process")
ALWAYS = "Always"
MAX_POINTS = 100          # one scorecard item's most points
NOTE_MAX_CHARS = 500      # a version note's longest text
TIMED_NEEDS_CODE = "timed rules need code"
DEADLINE_MAX_MINUTES = 24 * 60   # the summary email deadline's longest setting

TOP_FIELDS = ("checks", "reference", "params", "note", "base_version_id")
TOP_REQUIRED = ("checks", "reference", "params", "note")
CHECK_FIELDS = ("check_id", "kind", "number", "summary", "definition", "points", "auto_fail", "applies_when",
                "enabled", "timed")
CHECK_REQUIRED = ("kind", "summary", "definition", "points", "auto_fail", "enabled")
PARAM_FIELDS = ("call_limit_minutes", "exempt_priorities", "exempt_tags", "email_deadline_minutes")
PARAM_LABELS = {
    "call_limit_minutes": "The call length limit (minutes)",
    "email_deadline_minutes": "The summary email deadline (minutes)",
    "exempt_priorities": "The exempt priorities",
    "exempt_tags": "The exempt tags",
}


class EditError(ValueError):
    """The edit can't be saved; the message says why (HTTP 400)."""


class StaleEdit(EditError):
    """The rubric changed since the page loaded it (HTTP 409)."""


@dataclass(frozen=True)
class Edit:
    checks: tuple[CheckDef, ...]
    reference: dict[str, str]
    params: TimedParams
    note: str


# -- reading ------------------------------------------------------------------------------------------


def version_json(version: RubricVersion) -> dict[str, Any]:
    """A rubric version as the page reads it."""
    return {
        "version_id": version.version_id,
        "created_at": version.created_at,
        "note": version.note,
        "checks": [dataclasses.asdict(c) for c in version.checks],
        "reference": dict(version.reference),
        "params": {
            "call_limit_minutes": version.params.call_limit_minutes,
            "exempt_priorities": list(version.params.exempt_priorities),
            "exempt_tags": list(version.params.exempt_tags),
            "email_deadline_minutes": version.params.email_deadline_minutes,
        },
        "scorecard_total": rubric.scorecard_total(version),
    }


def known_checks(conn: sqlite3.Connection) -> dict[str, CheckDef]:
    """Every check id any version has had, with its latest definition."""
    known: dict[str, CheckDef] = {}
    for version_id, _created, _note in rubric.list_versions(conn):
        for check in rubric.get_version(conn, version_id).checks:
            known[check.check_id] = check
    return known


# -- validating ---------------------------------------------------------------------------------------


def _unknown(fields: dict, allowed: tuple[str, ...], what: str) -> None:
    unknown = sorted(key for key in fields if key not in allowed)
    if unknown:
        raise EditError(f"Unknown {what}: {', '.join(unknown)}.")


def _missing(fields: dict, required: tuple[str, ...], what: str) -> None:
    missing = [key for key in required if key not in fields]
    if missing:
        raise EditError(f"{what} is missing: {', '.join(missing)}.")


def _text(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditError(message)
    return value.strip()


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _note(value: Any) -> str:
    note = _text(value, "Add a note that says what changed in this version.")
    if len(note) > NOTE_MAX_CHARS:
        raise EditError(f"The version note can be up to {NOTE_MAX_CHARS} characters.")
    return note


def _check(raw: Any, position: int, known: dict[str, CheckDef], new_id: Callable[[], str]) -> CheckDef:
    if not isinstance(raw, dict):
        raise EditError(f"Check {position} must be an object.")
    check_id = raw.get("check_id")
    stored = None
    if check_id is not None:
        if not isinstance(check_id, str) or check_id not in known:
            raise EditError(f"Unknown check id {check_id}. Leave check_id out for a new check.")
        stored = known[check_id]
    label = f"Check {check_id}" if stored else f"New check {position}"
    _unknown(raw, CHECK_FIELDS, f"fields on {label.lower()}")
    _missing(raw, CHECK_REQUIRED, label)

    kind = raw["kind"]
    if kind not in KINDS:
        raise EditError(f"{label}: kind must be scorecard or process.")
    if stored is not None and kind != stored.kind:
        raise EditError(f"{label} can't move between the scorecard and the process rules (kind). Retire it and "
                        "add a new one instead.")
    if stored is None:
        if raw.get("timed") is not None:
            raise EditError(TIMED_NEEDS_CODE)
        timed = None
    else:
        if "timed" in raw and raw["timed"] != stored.timed:
            raise EditError(f"{label}: its timed setting can't be changed or removed.")
        timed = stored.timed

    summary = _text(raw["summary"], f"{label}: the summary can't be empty.")
    definition = _text(raw["definition"], f"{label}: the definition can't be empty.")
    points = raw["points"]
    if not _whole(points) or not 0 <= points <= MAX_POINTS:
        raise EditError(f"{label}: points must be a whole number from 0 to {MAX_POINTS}.")
    if kind == "process" and points != 0:
        raise EditError(f"{label}: process rules have no points (points must be 0).")
    for field, name in (("auto_fail", "Auto-fail"), ("enabled", "Enabled")):
        if not isinstance(raw[field], bool):
            raise EditError(f"{label}: {name} must be true or false.")
    if kind == "process" and raw["auto_fail"]:
        raise EditError(f"{label}: process rules can't be auto-fail; only scorecard items affect the score.")
    applies_when = raw.get("applies_when", ALWAYS)
    if applies_when is None:
        applies_when = ALWAYS
    if not isinstance(applies_when, str):
        raise EditError(f"{label}: applies when must be text.")
    return CheckDef(
        check_id=check_id if stored else new_id(),
        kind=kind,
        number=None,
        summary=summary,
        definition=definition,
        points=points,
        auto_fail=raw["auto_fail"],
        applies_when=applies_when.strip() or ALWAYS,
        enabled=raw["enabled"],
        timed=timed,
    )


def _checks(raw: Any, known: dict[str, CheckDef], new_id: Callable[[], str]) -> tuple[CheckDef, ...]:
    if not isinstance(raw, list):
        raise EditError("Checks must be a list.")
    seen: set[str] = set()
    for item in raw:
        check_id = item.get("check_id") if isinstance(item, dict) else None
        if isinstance(check_id, str):
            if check_id in seen:
                raise EditError(f"Check {check_id} appears twice.")
            seen.add(check_id)
    checks = [_check(item, i, known, new_id) for i, item in enumerate(raw, start=1)]
    scorecard = [dataclasses.replace(c, number=n)
                 for n, c in enumerate((c for c in checks if c.kind == "scorecard"), start=1)]
    process = [c for c in checks if c.kind == "process"]
    if not any(c.enabled and c.points > 0 for c in scorecard):
        raise EditError("The scorecard needs at least one enabled item with points.")
    return tuple(scorecard + process)


def _reference(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise EditError("Reference documents must be an object of title to text.")
    reference: dict[str, str] = {}
    seen: dict[str, str] = {}
    for title, text in raw.items():
        clean = title.strip()
        if not clean:
            raise EditError("Every reference document needs a title.")
        if clean.casefold() in seen:
            raise EditError(f'Two reference documents are titled "{clean}". Titles must be different.')
        if not isinstance(text, str):
            raise EditError(f'The reference document "{clean}" must have its text as plain text.')
        seen[clean.casefold()] = clean
        reference[clean] = text.strip()
    return reference


def _words(raw: Any, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise EditError(f"{PARAM_LABELS[field]} must be a list of text.")
    words: list[str] = []
    for item in raw:
        word = item.strip().lower()
        if word and word not in words:
            words.append(word)
    return tuple(words)


def _params(raw: Any) -> TimedParams:
    if not isinstance(raw, dict):
        raise EditError("The timed rule settings must be an object.")
    _unknown(raw, PARAM_FIELDS, "timed rule settings")
    _missing(raw, PARAM_FIELDS, "The timed rule settings")
    if not _whole(raw["call_limit_minutes"]) or raw["call_limit_minutes"] < 1:
        raise EditError(f"{PARAM_LABELS['call_limit_minutes']} must be a whole number of 1 or more.")
    deadline = raw["email_deadline_minutes"]
    if not _whole(deadline) or not 1 <= deadline <= DEADLINE_MAX_MINUTES:
        raise EditError(f"{PARAM_LABELS['email_deadline_minutes']} must be a whole number from 1 to "
                        f"{DEADLINE_MAX_MINUTES}.")
    return TimedParams(
        call_limit_minutes=raw["call_limit_minutes"],
        exempt_priorities=_words(raw["exempt_priorities"], "exempt_priorities"),
        exempt_tags=_words(raw["exempt_tags"], "exempt_tags"),
        email_deadline_minutes=deadline,
    )


def parse_edit(body: Any, current: RubricVersion, known: dict[str, CheckDef],
               new_id: Callable[[], str] = rubric.new_check_id) -> Edit:
    """Validate the page's edited rubric against the current version. Raises `EditError` (or
    `StaleEdit` when `base_version_id` is not the current version)."""
    if not isinstance(body, dict):
        raise EditError("Send the edited rubric as an object with checks, reference, params and note.")
    _unknown(body, TOP_FIELDS, "fields")
    note = _note(body.get("note"))
    _missing(body, TOP_REQUIRED, "The edited rubric")
    base = body.get("base_version_id")
    if base is not None and base != current.version_id:
        raise StaleEdit(f"The rubric was changed elsewhere since this page loaded it (it is now version "
                        f"{current.version_id}). Reload the page to see that version, then make your changes again.")
    edit = Edit(checks=_checks(body["checks"], known, new_id), reference=_reference(body["reference"]),
                params=_params(body["params"]), note=note)
    if (edit.checks, edit.reference, edit.params) == (current.checks, current.reference, current.params):
        raise EditError(f"Nothing changed since version {current.version_id}, so no new version was saved.")
    return edit
