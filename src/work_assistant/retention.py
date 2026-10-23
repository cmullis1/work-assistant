"""Customer-data retention: erase the content of calls older than the retention period.

What is erased: the stored contact snapshot (transcript, summary, macro, emails, subject, fields),
the evidence text on every check result of every review of the call, and the Not counted reason
and review error text of any of those reviews (either can quote the customer). What is kept: the
contact's identifying columns, every review, outcome, override and score, so trends survive.

Team activity follows the same period: ticket subjects and the values of field edits are erased, and
every activity action row is kept (`activity.store.purge_activity`). So do the satisfaction ratings collected for
1:1 Prep and Trends: a rating's reason and the customer's comment are erased, and its score and times are kept, so
no count changes (`activity.collected.purge_ratings`). The other collected rows (call legs, calls, solved tickets,
aging lists) hold no customer text.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_assistant.activity import collected, store

EMPTY_SNAPSHOT = "{}"


def _iso(moment: datetime) -> str:
    """The same format as contacts.call_started_at, so the cutoff compares as a string."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_placeholder(retention_days: int) -> str:
    return f"Removed after {retention_days} days"


def purge_expired(conn: sqlite3.Connection, now: datetime, retention_days: int, logs_dir: Path) -> int:
    """Erase customer content older than `retention_days` and return how many calls were purged.

    Safe to repeat: a purged call is skipped. The database is VACUUMed (and its write-ahead log
    truncated) after the commit when anything was purged, so erased text does not linger in the file.
    """
    cutoff = now - timedelta(days=retention_days)
    expired = [
        r["ticket_id"]
        for r in conn.execute(
            "SELECT ticket_id FROM contacts WHERE purged_at IS NULL AND call_started_at < ?", (_iso(cutoff),)
        )
    ]
    activity_erased = store.purge_activity(conn, cutoff)
    ratings_erased = collected.purge_ratings(conn, cutoff)
    if expired:
        marks = ",".join("?" for _ in expired)
        conn.execute(
            f"UPDATE check_results SET evidence = ? WHERE review_id IN "
            f"(SELECT review_id FROM reviews WHERE ticket_id IN ({marks}))",
            (evidence_placeholder(retention_days), *expired),
        )
        conn.execute(
            f"UPDATE reviews SET not_counted_reason = ? "
            f"WHERE not_counted_reason IS NOT NULL AND ticket_id IN ({marks})",
            (evidence_placeholder(retention_days), *expired),
        )
        conn.execute(
            f"UPDATE reviews SET error = ? WHERE error IS NOT NULL AND ticket_id IN ({marks})",
            (evidence_placeholder(retention_days), *expired),
        )
        conn.execute(
            f"UPDATE contacts SET snapshot = ?, purged_at = ? WHERE ticket_id IN ({marks})",
            (EMPTY_SNAPSHOT, _iso(now), *expired),
        )
        conn.commit()
    if expired or activity_erased or ratings_erased:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    _delete_old_logs(Path(logs_dir), cutoff)
    return len(expired)


def _delete_old_logs(logs_dir: Path, cutoff: datetime) -> None:
    if not logs_dir.is_dir():
        return
    limit = cutoff.timestamp()
    for path in logs_dir.glob("run-*.log"):
        if path.stat().st_mtime < limit:
            path.unlink()
