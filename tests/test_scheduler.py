"""The daily Windows Task Scheduler entry. Every test uses a fake runner; real schtasks is never called."""
import codecs
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from work_assistant import cli, scheduler

PYTHON = Path(r"C:\Tools & Co\venv\Scripts\python.exe")
PROJECT = Path(r"D:\Projects\Work Assistant")
NS_TASK = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

QUERY_OUTPUT = """
Folder: \\
HostName:                             EXAMPLE-PC
TaskName:                             \\Work Assistant Daily Review
Next Run Time:                        10/26/2026 6:00:00 AM
Status:                               Ready
Logon Mode:                           Interactive only
Last Run Time:                        10/19/2026 6:00:01 AM
Last Result:                          0
Author:                               EXAMPLE-PC\\owner
Task To Run:                          "C:\\venv\\Scripts\\python.exe" -m work_assistant.cli run --scheduled
Start In:                             D:\\Projects\\Work Assistant
Scheduled Task State:                 Enabled
Schedule Type:                        Daily
Start Time:                           6:00:00 AM
Days:                                 Every 1 day(s)
"""


class FakeRunner:
    """Records each schtasks call and answers from a queue of (returncode, stdout, stderr). Once the queue is
    empty, a query answers "not found" and anything else succeeds."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.files = {}

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if "/XML" in args:
            path = Path(args[args.index("/XML") + 1])
            self.files[str(path)] = path.read_bytes()
        if self.answers:
            code, out, err = self.answers.pop(0)
        else:
            code, out, err = NOT_FOUND if args[1] == "/Query" else (0, "", "")
        return NS(returncode=code, stdout=out, stderr=err)


DAILY = "Work Assistant Daily Review"
NOT_FOUND = (1, "", "ERROR: The system cannot find the file specified.")


def query_args():
    return ["schtasks", "/Query", "/TN", DAILY, "/V", "/FO", "LIST"]


def exists_args(name):
    return ["schtasks", "/Query", "/TN", name]


def delete_args(name):
    return ["schtasks", "/Delete", "/TN", name, "/F"]


# -- the brief's tests ----------------------------------------------------------------------------------


PYTHONW = PYTHON.with_name("pythonw.exe")


def test_task_xml_daily():
    xml = scheduler.task_xml(PYTHONW, PROJECT)
    for piece in ("<ScheduleByDay>", "<DaysInterval>1</DaysInterval>", "T06:00:00",
                  "<StartWhenAvailable>true</StartWhenAvailable>",
                  "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
                  "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", "PT26H"):
        assert piece in xml, piece
    for gone in ("ScheduleByWeek", "<Monday />", "--last-week"):
        assert gone not in xml, gone
    exec_ = ET.fromstring(xml.encode("utf-16")).find(f"{NS_TASK}Actions/{NS_TASK}Exec")
    assert exec_.find(f"{NS_TASK}Command").text == f'"{PYTHONW}"'
    assert exec_.find(f"{NS_TASK}Arguments").text == "-m work_assistant.cli run --scheduled"


def test_task_xml_keeps_every_other_setting():
    """Only the trigger changed with the switch to daily runs: the settings and the principal are as before."""
    root = ET.fromstring(scheduler.task_xml(PYTHONW, PROJECT).encode("utf-16"))
    settings = {child.tag.removeprefix(NS_TASK): (child.text or "").strip()
                for child in root.find(f"{NS_TASK}Settings")}
    assert settings == {
        "MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false", "AllowHardTerminate": "true", "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false", "IdleSettings": "", "AllowStartOnDemand": "true", "Enabled": "true",
        "Hidden": "false", "RunOnlyIfIdle": "false", "WakeToRun": "false", "ExecutionTimeLimit": "PT26H",
        "Priority": "7"}
    principal = root.find(f"{NS_TASK}Principals/{NS_TASK}Principal")
    assert [(c.tag.removeprefix(NS_TASK), c.text) for c in principal] == [
        ("LogonType", "InteractiveToken"), ("RunLevel", "LeastPrivilege")]
    [trigger] = root.find(f"{NS_TASK}Triggers")
    assert trigger.tag == f"{NS_TASK}CalendarTrigger"
    assert trigger.find(f"{NS_TASK}Enabled").text == "true"


def test_install_command():
    runner = FakeRunner((0, "SUCCESS: The scheduled task \"Work Assistant Daily Review\" has successfully been created.", ""))
    scheduler.install(runner)
    [create] = runner.calls
    assert create[:5] == ["schtasks", "/Create", "/TN", DAILY, "/XML"]
    assert create[6:] == ["/F"] and len(create) == 7
    path = create[5]
    data = runner.files[path]
    assert data.startswith(codecs.BOM_UTF16_LE), "schtasks needs UTF-16 with a byte order mark"
    assert "work_assistant.cli run --scheduled</Arguments>" in data.decode("utf-16")
    assert not Path(path).exists(), "the temporary XML file is removed afterward"


def test_task_names():
    assert scheduler.TASK_NAME == DAILY


def test_status_parse():
    runner = FakeRunner((0, QUERY_OUTPUT, ""))
    assert scheduler.status(runner) == {
        "installed": True,
        "next_run": "10/26/2026 6:00:00 AM",
        "last_run": "10/19/2026 6:00:01 AM",
        "last_result": "Succeeded",
    }
    assert runner.calls == [query_args()]


def test_status_query_failure_is_an_error():
    # Only "not found" means not installed; any other failure is reported, never read as "not installed".
    with pytest.raises(scheduler.SchedulerError, match="Access is denied"):
        scheduler.status(FakeRunner((1, "", "ERROR: Access is denied.")))


def test_installed_tells_not_found_from_other_failures():
    assert scheduler._installed(FakeRunner(NOT_FOUND), DAILY) is False
    assert scheduler._installed(FakeRunner((0, "", "")), DAILY) is True
    with pytest.raises(scheduler.SchedulerError, match="Access is denied"):
        scheduler._installed(FakeRunner((1, "", "ERROR: Access is denied.")), DAILY)


def test_remove_query_failure_is_an_error():
    runner = FakeRunner((1, "", "ERROR: Access is denied."))
    with pytest.raises(scheduler.SchedulerError, match="Access is denied"):
        scheduler.remove(runner)
    assert [c[1] for c in runner.calls] == ["/Query"]


def test_status_not_installed():
    runner = FakeRunner(NOT_FOUND)
    assert scheduler.status(runner) == {"installed": False, "next_run": None, "last_run": None, "last_result": None}
    assert runner.calls == [query_args()]


# -- the task definition --------------------------------------------------------------------------------


def test_xml_action_runs_venv_python_in_project_dir():
    root = ET.fromstring(scheduler.task_xml(PYTHON, PROJECT).encode("utf-16"))  # well formed, & escaped
    exec_ = root.find(f"{NS_TASK}Actions/{NS_TASK}Exec")
    assert exec_.find(f"{NS_TASK}Command").text == f'"{PYTHON}"'
    assert exec_.find(f"{NS_TASK}Arguments").text == "-m work_assistant.cli run --scheduled"
    assert exec_.find(f"{NS_TASK}WorkingDirectory").text == str(PROJECT)


def test_xml_runs_only_when_logged_on_without_a_password():
    xml = scheduler.task_xml(PYTHON, PROJECT)
    root = ET.fromstring(xml.encode("utf-16"))
    principal = root.find(f"{NS_TASK}Principals/{NS_TASK}Principal")
    assert principal.find(f"{NS_TASK}LogonType").text == "InteractiveToken"
    assert "Password" not in xml


@pytest.mark.parametrize("now, boundary", [
    (datetime(2026, 10, 17, 11, 48), "2026-10-18T06:00:00"),  # after 06:00: tomorrow
    (datetime(2026, 10, 19, 5, 59), "2026-10-19T06:00:00"),   # before 06:00: today
    (datetime(2026, 10, 19, 6, 0), "2026-10-20T06:00:00"),    # at 06:00: tomorrow
    (datetime(2026, 10, 31, 23, 30), "2026-11-01T06:00:00"),  # the last day of a month
])
def test_xml_starts_at_the_next_0600(now, boundary):
    # A start in the past would let "run as soon as possible after a missed start" fire right after install.
    root = ET.fromstring(scheduler.task_xml(PYTHON, PROJECT, now=now).encode("utf-16"))
    assert root.find(f"{NS_TASK}Triggers/{NS_TASK}CalendarTrigger/{NS_TASK}StartBoundary").text == boundary


def test_install_uses_this_python_and_the_project_dir(monkeypatch):
    monkeypatch.setattr(scheduler.sys, "executable", str(PYTHON))
    runner = FakeRunner()
    scheduler.install(runner)
    xml = runner.files[runner.calls[0][5]].decode("utf-16")
    assert f'"{PYTHON}"'.replace("&", "&amp;") in xml
    assert f"<WorkingDirectory>{scheduler.PROJECT_DIR}</WorkingDirectory>" in xml
    assert (scheduler.PROJECT_DIR / "pyproject.toml").is_file()


def test_install_failure_raises_with_schtasks_message():
    runner = FakeRunner((1, "", "ERROR: Access is denied.\n"))
    with pytest.raises(scheduler.SchedulerError, match="Access is denied"):
        scheduler.install(runner)
    assert not Path(runner.calls[0][5]).exists()


# -- status details -------------------------------------------------------------------------------------


def test_status_never_run():
    output = QUERY_OUTPUT.replace("10/19/2026 6:00:01 AM", "11/30/1999 12:00:00 AM").replace(
        "Last Result:                          0", "Last Result:                          267011")
    assert scheduler.status(FakeRunner((0, output, ""))) == {
        "installed": True, "next_run": "10/26/2026 6:00:00 AM", "last_run": None, "last_result": "Has not run yet"}


@pytest.mark.parametrize("code, text", [("1", "Error code 1"), ("-2147024894", "Error code -2147024894"),
                                        ("0x1", "Error code 1"), ("267009", "Running now"),
                                        ("garbled", "Error code garbled")])
def test_status_last_result_codes(code, text):
    output = QUERY_OUTPUT.replace("Last Result:                          0", f"Last Result:                          {code}")
    assert scheduler.status(FakeRunner((0, output, "")))["last_result"] == text


def test_status_missing_and_na_fields_are_none():
    output = "\n".join(line for line in QUERY_OUTPUT.splitlines()
                       if not line.startswith(("Last Run Time", "Last Result")))
    output = output.replace("10/26/2026 6:00:00 AM", "N/A")
    assert scheduler.status(FakeRunner((0, output, ""))) == {
        "installed": True, "next_run": None, "last_run": None, "last_result": None}


def test_status_of_empty_output_does_not_crash():
    assert scheduler.status(FakeRunner((0, "", "")))["installed"] is True


# -- remove, and a PC without schtasks ------------------------------------------------------------------


def test_remove_command():
    runner = FakeRunner((0, "", ""), (0, "SUCCESS: deleted", ""))
    assert scheduler.remove(runner) is True
    assert runner.calls == [exists_args(DAILY), delete_args(DAILY)]


def test_remove_when_not_installed():
    runner = FakeRunner(NOT_FOUND)
    assert scheduler.remove(runner) is False
    assert runner.calls == [exists_args(DAILY)]


@pytest.mark.parametrize("call", [scheduler.install, scheduler.remove, scheduler.status])
def test_missing_schtasks_is_unavailable(call):
    def runner(args, **kwargs):
        raise FileNotFoundError(2, "No such file", "schtasks")

    with pytest.raises(scheduler.SchedulerUnavailable, match="Windows Task Scheduler"):
        call(runner)


def test_tests_never_reach_real_schtasks():
    """The autouse guard in conftest stands in for the default runner."""
    with pytest.raises(scheduler.SchedulerUnavailable):
        scheduler.status()


# -- the CLI: work-assistant schedule install|remove|status ---------------------------------------------------


@pytest.fixture
def use_runner(monkeypatch):
    def install_runner(runner):
        monkeypatch.setattr(scheduler, "_system_runner", runner)
        return runner
    return install_runner


def test_cli_schedule_install_prints_next_run(use_runner, tmp_path, capsys):
    runner = use_runner(FakeRunner((0, "SUCCESS", ""), (0, QUERY_OUTPUT, "")))
    assert cli.main(["schedule", "install"], db_path=tmp_path / "work-assistant.db") == 0
    assert [c[1] for c in runner.calls] == ["/Create", "/Query"]  # + the status's query
    out = capsys.readouterr().out
    assert ("Installed the daily schedule: every day at 06:00 it reviews the previous day's calls, and any days "
            "it missed.") in out
    assert "Next run: 10/26/2026 6:00:00 AM." in out
    assert not (tmp_path / "work-assistant.db").exists(), "schedule commands never open the database"


@pytest.mark.parametrize("error", [scheduler.SchedulerError("schtasks did not answer within 5 seconds."),
                                   scheduler.SchedulerUnavailable(scheduler.UNAVAILABLE)])
def test_cli_schedule_install_succeeds_when_the_status_check_fails(use_runner, monkeypatch, tmp_path, capsys,
                                                                   error):
    runner = use_runner(FakeRunner((0, "SUCCESS", "")))

    def failing_status(runner=None):
        raise error

    monkeypatch.setattr(scheduler, "status", failing_status)
    assert cli.main(["schedule", "install"], db_path=tmp_path / "work-assistant.db") == 0
    assert [c[1] for c in runner.calls] == ["/Create"]
    out = capsys.readouterr().out
    assert "Installed the daily schedule: every day at 06:00" in out
    assert "Couldn't read the next run time just now; run `work-assistant schedule status` later." in out


def test_cli_schedule_install_failure(use_runner, tmp_path, capsys):
    use_runner(FakeRunner((1, "", "ERROR: Access is denied.\n")))
    assert cli.main(["schedule", "install"], db_path=tmp_path / "work-assistant.db") == 1
    assert "Could not install the daily schedule: ERROR: Access is denied." in capsys.readouterr().out


def test_cli_schedule_remove(use_runner, tmp_path, capsys):
    use_runner(FakeRunner((0, "", ""), (0, "SUCCESS", "")))
    assert cli.main(["schedule", "remove"], db_path=tmp_path / "work-assistant.db") == 0
    assert "Removed the daily schedule." in capsys.readouterr().out


def test_cli_schedule_remove_when_not_installed(use_runner, tmp_path, capsys):
    use_runner(FakeRunner(NOT_FOUND))
    assert cli.main(["schedule", "remove"], db_path=tmp_path / "work-assistant.db") == 0
    assert "The daily schedule is not installed, so there is nothing to remove." in capsys.readouterr().out


def test_schedule_status_text(use_runner, tmp_path, capsys):
    use_runner(FakeRunner((0, QUERY_OUTPUT, "")))
    assert cli.main(["schedule", "status"], db_path=tmp_path / "work-assistant.db") == 0
    assert capsys.readouterr().out.splitlines() == [
        "The daily schedule is installed. Every day at 06:00 it reviews the previous day's calls, and any days it "
        "missed.",
        "Next run: 10/26/2026 6:00:00 AM. Last run: 10/19/2026 6:00:01 AM. Last result: Succeeded."]


def test_cli_schedule_status_not_installed(use_runner, tmp_path, capsys):
    use_runner(FakeRunner(NOT_FOUND))
    assert cli.main(["schedule", "status"], db_path=tmp_path / "work-assistant.db") == 0
    assert ("The daily schedule is not installed. To install it, run: work-assistant schedule install"
            in capsys.readouterr().out)


@pytest.mark.parametrize("action", ["install", "remove", "status"])
def test_cli_schedule_without_task_scheduler(tmp_path, capsys, action):
    # The conftest guard stands in for a PC without schtasks.
    assert cli.main(["schedule", action], db_path=tmp_path / "work-assistant.db") == 1
    assert "Windows Task Scheduler" in capsys.readouterr().out


# -- no console window: the task runs pythonw -------------------------------------------------------------


def test_install_uses_pythonw_when_present(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(scheduler.sys, "executable", str(tmp_path / "python.exe"))
    runner = FakeRunner()
    scheduler.install(runner)
    xml = runner.files[runner.calls[0][5]].decode("utf-16")
    assert f'<Command>"{tmp_path / "pythonw.exe"}"</Command>' in xml


def test_install_falls_back_to_python_without_pythonw(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(scheduler.sys, "executable", str(tmp_path / "python.exe"))
    runner = FakeRunner()
    scheduler.install(runner)
    xml = runner.files[runner.calls[0][5]].decode("utf-16")
    assert f'<Command>"{tmp_path / "python.exe"}"</Command>' in xml


def test_schtasks_calls_time_out_quickly():
    seen = {}

    def runner(args, **kwargs):
        seen.update(kwargs)
        return NS(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")

    scheduler.status(runner)
    assert seen["timeout"] <= 5
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
