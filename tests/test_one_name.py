"""The project has one name, Work Assistant (Work Assistant name design, plan Task 4).

Every file git tracks is read as text and checked for the old names: Ticket QA in any spelling, the server's old
`teamact` user, its `team-activity` folders, units (and the systemctl and journalctl commands and unit descriptions
that name them), kit, update and export names, and the old `team_activity` edition. The dated records (specs,
plans, mockups, handovers and Storybloq's tickets) keep the names they were written with, and the one-time move
scripts must name what they move, so those paths are in ALLOWED. The Team activity feature keeps its name and is not
matched.

Only the repository's own files are read. Nothing here opens a database or looks outside the repository.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

OLD_NAME = re.compile(
    r"ticket(?:%20|[ _-])?qa|teamact|/(opt|etc|var/lib)/team-activity|team-activity[a-z-]*\.(service|timer)"
    r"|team-activity-(kit|update|export)|edition\s*=\s*\"team_activity\"|--edition[= ]team_activity"
    r"|-u team-activity\b|Description=Team activity|systemctl (status|restart|start|stop) team-activity\b",
    re.IGNORECASE)
ALLOWED = ("docs/superpowers/specs/*", "docs/superpowers/plans/*", ".claude-project/handovers/*", ".story/*",
           "tests/test_one_name.py",
           # The one-time PC move (plan Task 5) names the folder, database, keys, task and shortcut it moves.
           "scripts/move_to_work_assistant.py", "tests/test_move_to_work_assistant.py",
           "docs/move-to-work-assistant.md",
           # The one-time server rename (plan Task 6) names the folders, user, database and units it copies from.
           "deploy/linux/rename-server.sh", "tests/test_rename_server.py", "docs/hosted/rename-to-work-assistant.md",
           # The dated design mockups are records too: each shows the app as it was named on its day.
           "docs/superpowers/mockups/*")

ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [name for name in listed.decode("utf-8").split("\0") if name]


def _allowed(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in ALLOWED)


def _text(path: str) -> str | None:
    """The file's text, or None for a binary file (or one deleted but not yet committed)."""
    try:
        data = (ROOT / path).read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_allowed_covers_the_dated_records_at_any_depth():
    assert _allowed("docs/superpowers/specs/2026-10-16-ticket-qa-reviewer-design.md")
    assert _allowed("docs/superpowers/plans/2026-10-21-work-assistant-name-and-github-updates.md")
    assert _allowed(".claude-project/handovers/2026-10-21-session.md")
    assert _allowed(".story/tickets/T-001.json")
    assert _allowed(".story/handovers/2026-10-21-01.md")
    assert _allowed("docs/superpowers/mockups/2026-10-16-qa-app-screens.html")
    assert _allowed("scripts/move_to_work_assistant.py")
    assert _allowed("deploy/linux/rename-server.sh")
    assert not _allowed("deploy/linux/work-assistant-update.sh")
    assert not _allowed("scripts/inspect_zendesk.py")
    assert not _allowed("docs/hosted/operator-guide.md")
    assert not _allowed(".claude-project/context.md")
    assert not _allowed("README.md")


def test_the_old_names_are_matched_and_the_feature_name_is_not():
    for old in ("Ticket QA", "Ticket%20QA", "TicketQA", "ticketqa", "ticket-qa", "ticket_qa", "teamact", "/opt/team-activity",
                "/var/lib/team-activity", "team-activity.service", "team-activity-sync.timer", "team-activity-kit",
                "team-activity-export.db", 'edition = "team_activity"', "--edition team_activity",
                "--edition=team_activity", "journalctl -u team-activity -n 50", "journalctl -u team-activity-sync",
                "Description=Team activity web app", "sudo systemctl status team-activity",
                "systemctl restart team-activity", "systemctl start team-activity", "systemctl stop team-activity"):
        assert OLD_NAME.search(old), old
    for kept in ("Team activity", "show_team_activity", "tests/test_web_team_activity.py", "team-activity.yaml",
                 "set-show-team-activity", "--edition hosted", "--edition=team", "Description=Work Assistant",
                 "journalctl -u work-assistant", "systemctl status work-assistant", "team-activity-heatmap",
                 "the Team activity screen", "systemctl status team-activityx"):
        assert not OLD_NAME.search(kept), kept


def test_no_tracked_file_uses_an_old_name():
    found = []
    for path in _tracked_files():
        if _allowed(path):
            continue
        text = _text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD_NAME.search(line):
                found.append(f"{path}:{number}: {line.strip()[:160]}")
    assert not found, "Old names are still in use:\n" + "\n".join(found)
