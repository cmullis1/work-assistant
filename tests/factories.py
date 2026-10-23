"""Small builders for web tests: rows written straight into a test database (synthetic data only).

The richer synthetic-data builder (contacts, reviews, rubric versions) lives in `work_assistant.demo`, so
`work-assistant serve --demo` never imports the tests.
"""
from __future__ import annotations

import sqlite3

from work_assistant.rubric import ensure_seeded


def add_run(conn: sqlite3.Connection, *, status: str = "complete", kind: str = "manual",
            window: tuple[str, str] = ("2026-10-04T05:00:00Z", "2026-10-11T05:00:00Z"),
            estimate_usd: float | None = None, actual_usd: float | None = None, error: str | None = None,
            created_at: str = "2026-10-12T11:00:00Z", finished_at: str | None = None,
            pid: int | None = None, contact_count: int | None = None) -> int:
    """Insert one run row and return its id."""
    version = ensure_seeded(conn)
    cur = conn.execute(
        "INSERT INTO runs (kind, window_start, window_end, rubric_version_id, status, estimate_usd, actual_usd, "
        "contact_count, error, created_at, finished_at, pid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, window[0], window[1], version.version_id, status, estimate_usd, actual_usd, contact_count, error,
         created_at, finished_at, pid),
    )
    conn.commit()
    return cur.lastrowid
