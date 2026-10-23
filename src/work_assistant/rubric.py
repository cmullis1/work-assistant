"""Versioned rubric store. Every save inserts a new immutable snapshot row."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Iterable, Mapping

from work_assistant.models import CheckDef, RubricVersion, TimedParams
from work_assistant.seed_rubric import default_checks, default_reference

SEED_NOTE = "Default rubric"


def new_check_id() -> str:
    """A fresh permanent check id. Ids are never reused."""
    return "c-" + uuid.uuid4().hex[:12]


def scorecard_total(v: RubricVersion) -> int:
    """Total points of the enabled scorecard checks in this version."""
    return sum(c.points for c in v.checks if c.kind == "scorecard" and c.enabled)


def _serialize(
    checks: Iterable[CheckDef], reference: Mapping[str, str], params: TimedParams
) -> str:
    return json.dumps(
        {
            "checks": [dataclasses.asdict(c) for c in checks],
            "reference": dict(reference),
            "params": dataclasses.asdict(params),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _deadline_minutes(params: dict) -> int:
    """The summary-email deadline in minutes. Versions saved before minutes hold it in whole hours, under
    `email_deadline_hours`; they are read as minutes so they keep their meaning, and are never rewritten."""
    if "email_deadline_minutes" in params:
        return params["email_deadline_minutes"]
    return params["email_deadline_hours"] * 60


def _deserialize(row: sqlite3.Row) -> RubricVersion:
    data = json.loads(row["snapshot"])
    p = data["params"]
    params = TimedParams(
        call_limit_minutes=p["call_limit_minutes"],
        exempt_priorities=tuple(p["exempt_priorities"]),
        exempt_tags=tuple(p["exempt_tags"]),
        email_deadline_minutes=_deadline_minutes(p),
    )
    return RubricVersion(
        version_id=row["version_id"],
        created_at=row["created_at"],
        note=row["note"],
        checks=tuple(CheckDef(**c) for c in data["checks"]),
        reference=dict(data["reference"]),
        params=params,
    )


def save_version(
    conn: sqlite3.Connection,
    checks: Iterable[CheckDef],
    reference: Mapping[str, str],
    params: TimedParams,
    note: str,
) -> RubricVersion:
    """Insert a new rubric version and return it. Existing versions are never modified."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO rubric_versions (created_at, note, snapshot) VALUES (?, ?, ?)",
        (created_at, note, _serialize(checks, reference, params)),
    )
    conn.commit()
    return get_version(conn, cur.lastrowid)


def get_version(conn: sqlite3.Connection, version_id: int) -> RubricVersion:
    row = conn.execute(
        "SELECT version_id, created_at, note, snapshot FROM rubric_versions WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"No rubric version {version_id}")
    return _deserialize(row)


def current_version(conn: sqlite3.Connection) -> RubricVersion:
    row = conn.execute(
        "SELECT version_id, created_at, note, snapshot FROM rubric_versions "
        "ORDER BY version_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("No rubric version exists yet; call ensure_seeded first")
    return _deserialize(row)


def list_versions(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """(version_id, created_at, note) for every version, oldest first."""
    rows = conn.execute(
        "SELECT version_id, created_at, note FROM rubric_versions ORDER BY version_id"
    ).fetchall()
    return [(r["version_id"], r["created_at"], r["note"]) for r in rows]


def ensure_seeded(conn: sqlite3.Connection) -> RubricVersion:
    """Create version 1 from the defaults if no version exists; return the current version."""
    count = conn.execute("SELECT COUNT(*) FROM rubric_versions").fetchone()[0]
    if count == 0:
        save_version(conn, default_checks(), default_reference(), TimedParams(), SEED_NOTE)
    return current_version(conn)
