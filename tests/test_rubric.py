import dataclasses
import json
import re
from pathlib import Path

import pytest

from work_assistant.db import connect, init_db
from work_assistant.models import DEFAULT_REFERENCE
from work_assistant.rubric import (
    current_version,
    ensure_seeded,
    get_version,
    list_versions,
    new_check_id,
    save_version,
    scorecard_total,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def _by_id(v):
    return {c.check_id: c for c in v.checks}


def test_seed_scorecard_totals_100(conn):
    v = ensure_seeded(conn)
    assert scorecard_total(v) == 100
    numbers = sorted(c.number for c in v.checks if c.kind == "scorecard")
    assert numbers == list(range(1, 17))


def test_seed_process_rules(conn):
    v = ensure_seeded(conn)
    rules = {c.check_id: c for c in v.checks if c.kind == "process"}
    assert set(rules) == {
        "pr-triage-macro",
        "pr-call-length",
        "pr-scope",
        "pr-email-sent",
        "pr-email-qualities",
        "pr-dropped-callback",
        "pr-merge-correct",
    }
    assert rules["pr-call-length"].timed == "call_length"
    assert rules["pr-email-sent"].timed == "email_deadline"
    for cid in ("pr-triage-macro", "pr-scope", "pr-email-qualities", "pr-dropped-callback", "pr-merge-correct"):
        assert rules[cid].timed is None
    assert all(r.points == 0 and r.number is None for r in rules.values())


def test_seed_applies_when(conn):
    checks = _by_id(ensure_seeded(conn))
    assert checks["sc11"].applies_when == "Only when the issue was not resolved on the call"
    assert checks["sc12"].applies_when == (
        "Only when the issue was resolved on the call but the root cause is unknown"
    )
    assert checks["sc13"].applies_when == "Only when the issue was resolved on the call"
    assert checks["pr-dropped-callback"].applies_when == (
        "Only when the transcript shows the call disconnected before the issue was handled"
    )
    assert checks["pr-merge-correct"].applies_when == "Only when this call's ticket was merged into another ticket"
    assert checks["pr-call-length"].applies_when == "Always, except dropped calls"
    assert checks["pr-call-length"].definition.endswith(
        "This rule doesn't apply to dropped calls: when a call disconnects before the issue is "
        "handled, only the callback is checked (see the dropped-call rule)."
    )
    special = ("sc11", "sc12", "sc13", "pr-dropped-callback", "pr-merge-correct", "pr-call-length")
    others = [c for cid, c in checks.items() if cid not in special]
    assert all(c.applies_when == "Always" for c in others)


def test_seed_text_has_no_meta_annotations(conn):
    for c in ensure_seeded(conn).checks:
        text = c.summary + c.definition
        assert "**" not in text
        assert "builder's call" not in text.lower()
        assert "owner's standard" not in text.lower()
        assert "owner's definition" not in text.lower()


def test_seed_key_content(conn):
    checks = _by_id(ensure_seeded(conn))
    assert "phonetic spelling" in checks["sc02"].definition
    assert "confirming the last 4 digits is enough" in checks["sc02"].definition
    macro = checks["pr-triage-macro"].definition
    assert "Station/Product Affected" in macro and "When Did It Begin?" in macro
    assert "image" in macro
    assert "must match what was actually discussed" in checks["sc16"].definition
    assert "The standard footer is not required." in checks["pr-email-qualities"].definition
    assert checks["pr-email-qualities"].definition.count("\n- ") == 5


def test_seed_calibration_rules(conn):
    checks = _by_id(ensure_seeded(conn))
    assert [c.check_id for c in checks.values() if c.kind == "process"][-2:] == [
        "pr-dropped-callback", "pr-merge-correct",
    ]
    macro = checks["pr-triage-macro"].definition
    assert "Only the triage macro format counts." in macro
    assert '"Incident details / Troubleshooting steps / Solution"' in macro
    callback = checks["pr-dropped-callback"]
    assert callback.summary == "Dropped call: called back right away"
    assert "on the same ticket" in callback.definition and "within 5 minutes" in callback.definition
    assert "continuation of the same contact" in callback.definition
    assert "No callback, a late callback, or a callback on a different ticket is a miss." in callback.definition
    merge = checks["pr-merge-correct"]
    assert merge.summary == "Merged into the correct ticket"
    assert "same location (Location ID) and the same issue" in merge.definition
    assert "The caller can be a different person at that location." in merge.definition
    assert "A wrong merge is a miss." in merge.definition
    assert "A dropped call stays on its own ticket" in merge.definition
    assert all(c.points == 0 and not c.auto_fail and c.enabled for c in (callback, merge))


def test_seed_once(conn):
    ensure_seeded(conn)
    ensure_seeded(conn)
    assert conn.execute("SELECT COUNT(*) FROM rubric_versions").fetchone()[0] == 1


def test_seed_reference_documents(conn):
    ref = ensure_seeded(conn).reference
    assert set(ref) == set(DEFAULT_REFERENCE)
    assert ref["Priority definitions"].startswith("Urgent")
    assert "The site is not operational." in ref["Priority definitions"]
    assert "Third-party barcode scanners" in ref["Unsupported products"]
    assert "admin portal" in ref["Not authorized"]
    assert "local IT" in ref["Redirect procedure"]
    assert all(t != "NOT PROVIDED YET" for t in ref.values())


def test_reference_is_open_list(conn):
    v1 = ensure_seeded(conn)
    ref = {k: t for k, t in v1.reference.items() if k != "Not authorized"}
    ref["Escalation contacts"] = "Call the on-call lead."
    v2 = save_version(conn, v1.checks, ref, v1.params, "edit reference")
    assert set(get_version(conn, v2.version_id).reference) == set(ref)
    assert set(get_version(conn, 1).reference) == set(DEFAULT_REFERENCE)


def test_save_version_keeps_ids(conn):
    v1 = ensure_seeded(conn)
    edited = tuple(
        dataclasses.replace(c, definition="New wording") if c.check_id == "sc06" else c
        for c in v1.checks
    )
    v2 = save_version(conn, edited, v1.reference, v1.params, "reword sc06")
    assert v2.version_id > v1.version_id
    assert _by_id(v2)["sc06"].definition == "New wording"
    assert _by_id(get_version(conn, 1))["sc06"].definition != "New wording"
    assert current_version(conn).version_id == v2.version_id
    assert [r[0] for r in list_versions(conn)] == [1, v2.version_id]
    assert list_versions(conn)[1][2] == "reword sc06"


def test_roundtrip_types(conn):
    v = ensure_seeded(conn)
    again = get_version(conn, v.version_id)
    assert again == v
    assert isinstance(again.checks, tuple)
    assert isinstance(again.params.exempt_priorities, tuple)


def test_retire_by_omission(conn):
    v1 = ensure_seeded(conn)
    kept = tuple(c for c in v1.checks if c.check_id != "sc14")
    v2 = save_version(conn, kept, v1.reference, v1.params, "retire sc14")
    assert scorecard_total(v2) == 95
    assert "sc14" in _by_id(get_version(conn, 1))


def test_new_check_id_unique():
    ids = {new_check_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(re.fullmatch(r"c-[0-9a-f]{12}", i) for i in ids)


def _store_with_hours(conn, version, hours):
    """Store `version` as a pre-minutes row would hold it: its deadline under the old key, in hours."""
    snapshot = json.loads(conn.execute("SELECT snapshot FROM rubric_versions WHERE version_id = ?",
                                       (version.version_id,)).fetchone()[0])
    del snapshot["params"]["email_deadline_minutes"]
    snapshot["params"]["email_deadline_hours"] = hours
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    cur = conn.execute("INSERT INTO rubric_versions (created_at, note, snapshot) VALUES (?, ?, ?)",
                       ("2026-10-01T00:00:00+00:00", "old hours row", raw))
    conn.commit()
    return cur.lastrowid, raw


def test_old_hours_versions_load_as_minutes(conn):
    """Review Focus 4: a version stored before minutes holds `email_deadline_hours`. It loads as minutes
    (24 h -> 1440 min) without its stored row being rewritten, and judges every call as it did before."""
    from work_assistant.models import FirstContact
    from work_assistant.timing import timed_facts

    seed = ensure_seeded(conn)
    day = save_version(conn, seed.checks, seed.reference,
                       dataclasses.replace(seed.params, email_deadline_minutes=1440), "24 hours in minutes")
    old_id, raw = _store_with_hours(conn, seed, 24)
    old = get_version(conn, old_id)
    assert old.params.email_deadline_minutes == 1440
    assert old.params == day.params
    stored = conn.execute("SELECT snapshot FROM rubric_versions WHERE version_id = ?", (old_id,)).fetchone()[0]
    assert stored == raw, "loading never rewrites a stored version"

    def call(email_at):
        return FirstContact(ticket_id=1, channel="voice", subject="s", priority="normal", tags=(), fields={},
                            call_started_at="2026-10-05T10:00:00Z", call_duration_seconds=420, agent_user_id=1,
                            agent_email="a@example.com", agent_name="Agent", attribution_ok=True,
                            summary_email_at=email_at, comments=())

    for email_at, outcome in (("2026-10-05T12:25:00Z", "met"), ("2026-10-06T10:07:00Z", "met"),
                              ("2026-10-06T10:07:01Z", "missed"), (None, "missed")):
        before, after = timed_facts(call(email_at), old), timed_facts(call(email_at), day)
        assert before.decided["pr-email-sent"] == after.decided["pr-email-sent"] == outcome, email_at
        assert before.evidence == after.evidence


def test_seed_defaults_to_30_minutes(conn):
    v = ensure_seeded(conn)
    assert v.params.email_deadline_minutes == 30
    checks = _by_id(v)
    email = checks["pr-email-sent"].definition
    assert "within 30 minutes of the call ending" in email
    assert "24 hours" not in email
    # The merged-into ticket's comments are gathered over the fixed collection window, not the deadline.
    merge = checks["pr-merge-correct"].definition
    assert "through 24 hours after the call ends" in merge
    assert "summary-email deadline" not in merge
