"""The concrete run log: timestamped lines with every secret replaced by [REDACTED]."""
import re

from work_assistant.runlog import RunLog, log_path


def test_runlog_redacts(tmp_path):
    path = tmp_path / "logs" / "run-1.log"
    log = RunLog(path, ["tok-XYZ"])
    log.info("token tok-XYZ")
    text = path.read_text(encoding="utf-8")
    assert "token [REDACTED]" in text
    assert "tok-XYZ" not in text


def test_runlog_lines_are_timestamped_with_level(tmp_path):
    path = tmp_path / "run-2.log"
    log = RunLog(path, [])
    log.info("first")
    log.warn("second")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO first", lines[0])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z WARN second", lines[1])


def test_runlog_ignores_missing_secrets_and_redacts_overlaps(tmp_path):
    path = tmp_path / "run-3.log"
    log = RunLog(path, [None, "", "abc", "abcdef"])
    log.warn("key abcdef and abc")
    text = path.read_text(encoding="utf-8")
    assert "key [REDACTED] and [REDACTED]" in text
    assert "abc" not in text.replace("[REDACTED]", "")


def test_runlog_redacts_multiline_and_appends(tmp_path):
    path = tmp_path / "run-4.log"
    RunLog(path, ["s3cret"]).info("one s3cret\ntwo")
    RunLog(path, ["s3cret"]).info("three")
    text = path.read_text(encoding="utf-8")
    assert "s3cret" not in text
    assert len(text.splitlines()) == 2  # an embedded newline cannot forge a second log line


def test_log_path():
    from pathlib import Path

    assert log_path(Path("logs"), 7) == Path("logs") / "run-7.log"
