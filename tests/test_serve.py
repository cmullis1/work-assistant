"""`work-assistant serve` and `work-assistant stop`: the already-running check, the app log, and quitting.

Every test fakes the port check, the HTTP request and the server; nothing binds, listens on, or talks
to the real port 8765.
"""
import logging
import socket
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from work_assistant import cli
from work_assistant.web.app import create_app

URL = "http://127.0.0.1:8765/"
CONFIG = {"host": "127.0.0.1", "port": 8765}
SETTINGS_BODY = {"retention_days": 90, "credentials": {"zendesk_token": True}, "demo": False}


class FakeHttp:
    """Answers each request from a queue: a (status, json body) pair, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, timeout):
        self.calls.append((method, url))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        request = httpx.Request(method, url)
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)


def refused():
    return httpx.ConnectError("[WinError 10061] No connection could be made")


class FakePort:
    """Answers whether the port is free from a queue of booleans; the last answer repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def port(monkeypatch, *answers):
    fake = FakePort(*answers)
    monkeypatch.setattr(cli, "_port_free", fake)
    return fake


class FakeServerRun:
    """Stands in for running the uvicorn server, recording the servers it was asked to run."""

    def __init__(self, during=None):
        self.servers = []
        self.during = during

    def __call__(self, server):
        self.servers.append(server)
        if self.during is not None:
            self.during(server)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run in a scratch folder, so logs/app.log lands there, and undo uvicorn's logging setup afterward."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(cli, "_open_browser", urls.append)
    return urls


@pytest.fixture
def run(monkeypatch):
    fake = FakeServerRun()
    monkeypatch.setattr(cli, "_run_server", fake)
    return fake


def app_log(workdir):
    return (workdir / "logs" / "app.log").read_text(encoding="utf-8")


def serve(workdir, *extra):
    return cli.main(["--db", str(workdir / "work-assistant.db"), "serve", *extra])


# -- the already-running check ----------------------------------------------------------------------------


def test_probe_free_port_needs_no_request(monkeypatch):
    """A free port is found by binding it, at once: Windows takes ~2 s to refuse a connection."""
    fake = port(monkeypatch, True)
    http = FakeHttp()
    monkeypatch.setattr(cli, "_http", http)
    assert cli.probe_app(CONFIG) == "free"
    assert fake.calls == [("127.0.0.1", 8765)]
    assert http.calls == []


def test_probe_finds_the_app(monkeypatch):
    port(monkeypatch, False)
    http = FakeHttp((200, SETTINGS_BODY))
    monkeypatch.setattr(cli, "_http", http)
    assert cli.probe_app(CONFIG) == "app"
    assert http.calls == [("GET", "http://127.0.0.1:8765/api/settings")]


def test_probe_port_released_between_checks(monkeypatch):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp(refused()))
    assert cli.probe_app(CONFIG) == "free"


@pytest.mark.parametrize("answer", [
    (404, {"detail": "Not Found"}),           # another web app
    (200, {"something": "else"}),             # JSON, but not Work Assistant's settings
    (200, "<html>hello</html>"),              # not JSON at all
    (200, ["retention_days", "credentials"]),  # JSON, not an object
    httpx.ReadTimeout("timed out"),           # something that never answers
])
def test_probe_other_program(monkeypatch, answer):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp(answer))
    assert cli.probe_app(CONFIG) == "other"


def test_port_free_on_a_scratch_port():
    """The real bind check, on a port the OS picks: free when nothing holds it, taken while a listener does."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        taken = listener.getsockname()[1]
        assert cli.port_free("127.0.0.1", taken) is False
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        unused = spare.getsockname()[1]
    assert cli.port_free("127.0.0.1", unused) is True


def test_serve_when_already_running_opens_the_browser_and_exits(monkeypatch, workdir, opened, run, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, SETTINGS_BODY)))
    assert serve(workdir) == 0
    assert opened == [URL]
    assert run.servers == [], "no second server tries the busy port"
    assert not (workdir / "work-assistant.db").exists(), "the database is not even opened"
    assert f"Work Assistant is already running at {URL}" in capsys.readouterr().out
    assert f"Work Assistant is already running at {URL}" in app_log(workdir)


def test_serve_no_browser_when_already_running(monkeypatch, workdir, opened, run):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, SETTINGS_BODY)))
    assert serve(workdir, "--no-browser") == 0
    assert opened == [] and run.servers == []


def test_probe_finds_the_demo(monkeypatch):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, {**SETTINGS_BODY, "demo": True})))
    assert cli.probe_app(CONFIG) == "demo"


def test_serve_demo_refuses_when_the_real_app_is_running(monkeypatch, workdir, opened, run, capsys):
    """Opening the real app for someone who asked for the demo could get real data changed by mistake."""
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, SETTINGS_BODY)))
    assert serve(workdir, "--demo") == 1
    assert opened == [] and run.servers == []
    assert "with your real data, so the demo can't start" in capsys.readouterr().out


def test_serve_opens_the_running_demo(monkeypatch, workdir, opened, run, capsys):
    """The shortcut clicked while the demo runs opens the demo (it shows a Demo data badge) and says so."""
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, {**SETTINGS_BODY, "demo": True})))
    assert serve(workdir) == 0
    assert opened == [URL] and run.servers == []
    out = capsys.readouterr().out
    assert "The Work Assistant demo is already running" in out
    assert "stop the demo with: work-assistant stop" in out


HOSTED_DEMO_BODY = {**SETTINGS_BODY, "demo": True, "edition": "hosted"}


def test_probe_finds_the_hosted_demo(monkeypatch):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, HOSTED_DEMO_BODY)))
    assert cli.probe_app(CONFIG) == "hosted demo"


def test_serve_opens_the_running_hosted_demo_and_says_to_press_ctrl_c(monkeypatch, workdir, opened, run,
                                                                             capsys):
    """R7: the hosted demo has no quit address (/api/quit is 404 there), so `work-assistant stop` can't stop it:
    the hint says to press Ctrl+C in its window instead."""
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, HOSTED_DEMO_BODY)))
    assert serve(workdir) == 0
    assert opened == [URL] and run.servers == []
    out = capsys.readouterr().out
    assert "The Work Assistant demo is already running" in out
    assert "stop the demo by pressing Ctrl+C in the window it runs in, then open the app again" in out
    assert "work-assistant stop" not in out


def test_stop_the_hosted_demo_says_to_press_ctrl_c(monkeypatch, no_sleep, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((404, {"detail": "Not found."}), (200, HOSTED_DEMO_BODY)))
    assert cli.main(["stop"]) == 1
    assert "The hosted demo can't be stopped this way. Press Ctrl+C in the window it runs in." in \
        capsys.readouterr().out


def test_serve_demo_when_the_demo_is_running(monkeypatch, workdir, opened, run):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, {**SETTINGS_BODY, "demo": True})))
    assert serve(workdir, "--demo") == 0
    assert opened == [URL] and run.servers == []


def test_serve_when_another_program_has_the_port(monkeypatch, workdir, opened, run, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((404, {"detail": "Not Found"})))
    assert serve(workdir) == 1
    assert run.servers == [] and opened == []
    assert "Port 8765 is in use by another program" in capsys.readouterr().out


def test_serve_starts_the_server_when_the_port_is_free(monkeypatch, workdir, opened, run, capsys):
    port(monkeypatch, True)
    assert serve(workdir, "--no-browser") == 0
    [server] = run.servers
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8765)
    out = capsys.readouterr().out
    assert f"Work Assistant is running at {URL}" in out
    assert "work-assistant stop" in out
    assert out.rstrip().endswith("Work Assistant stopped."), "the log records when the server has stopped"


# -- serve without a console (pythonw.exe, as the desktop shortcut runs it) --------------------------------


def test_serve_without_a_console_logs_to_app_log(monkeypatch, workdir, opened):
    """Under pythonw sys.stdout and sys.stderr are None. Uvicorn's own logging setup reads
    sys.stdout.isatty(), which crashed there; everything now goes to logs/app.log instead."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    port(monkeypatch, True)

    def during(server):
        logging.getLogger("uvicorn.error").warning("a warning from the server")

    run = FakeServerRun(during)
    monkeypatch.setattr(cli, "_run_server", run)
    assert serve(workdir, "--no-browser") == 0
    assert len(run.servers) == 1, "the real uvicorn Config was built without crashing"
    assert sys.stdout is None and sys.stderr is None, "the console streams are restored afterward"
    log = app_log(workdir)
    assert log.splitlines()[0].endswith(": app started ===")
    assert f"Work Assistant is running at {URL}" in log
    assert "a warning from the server" in log


def test_serve_without_a_console_when_already_running(monkeypatch, workdir, opened, run):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, SETTINGS_BODY)))
    assert serve(workdir) == 0
    assert opened == [URL]
    assert "already running" in app_log(workdir)


def test_serve_logs_unexpected_errors_without_their_message(monkeypatch, workdir, opened):
    port(monkeypatch, True)
    secret = "customer text" + " that must not reach the log"  # not on the raising line, which is logged

    def boom(server):
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_run_server", boom)
    with pytest.raises(RuntimeError):
        serve(workdir, "--no-browser")
    log = app_log(workdir)
    assert "The app stopped with an unexpected error: RuntimeError" in log
    assert "customer text" not in log


def test_quit_request_stops_the_server(monkeypatch, workdir, opened):
    """The app's quit hook asks uvicorn to shut down gracefully."""
    port(monkeypatch, True)

    def during(server):
        client = TestClient(server.config.app, base_url="http://127.0.0.1:8765")
        assert client.post("/api/quit").json() == {"stopping": True}
        assert server.should_exit is True

    run = FakeServerRun(during)
    monkeypatch.setattr(cli, "_run_server", run)
    assert serve(workdir, "--no-browser") == 0
    assert len(run.servers) == 1


# -- guards ------------------------------------------------------------------------------------------------


def test_real_port_http_server_and_browser_are_blocked_in_tests():
    """The conftest guards: no test can bind or reach the real port 8765, run a server, or open a browser."""
    for blocked in (lambda: cli.probe_app(CONFIG), lambda: cli._http("GET", URL, 1),
                    lambda: cli._run_server(object()), lambda: cli._open_browser(URL)):
        with pytest.raises(RuntimeError, match="blocked in tests"):
            blocked()


# -- the quit endpoint -------------------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "work-assistant.db"


def test_quit_endpoint_calls_the_hook(db_path, tmp_path):
    calls = []
    app = create_app(db_path, log_dir=tmp_path / "logs", startup_work=False, on_quit=lambda: calls.append(1))
    response = TestClient(app, base_url="http://127.0.0.1:8765").post("/api/quit")
    assert response.status_code == 200
    assert calls == [1]


def test_quit_endpoint_refuses_other_pages(db_path, tmp_path):
    calls = []
    app = create_app(db_path, log_dir=tmp_path / "logs", startup_work=False, on_quit=lambda: calls.append(1))
    response = TestClient(app, base_url="http://127.0.0.1:8765").post(
        "/api/quit", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert calls == []


def test_quit_endpoint_without_a_hook(db_path, tmp_path):
    app = create_app(db_path, log_dir=tmp_path / "logs", startup_work=False)
    response = TestClient(app, base_url="http://127.0.0.1:8765").post("/api/quit")
    assert response.status_code == 503


# -- stop -------------------------------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(cli, "_sleep", waits.append)
    return waits


def test_stop_asks_the_app_to_quit_and_waits_until_it_has(monkeypatch, no_sleep, capsys):
    port(monkeypatch, False, False, True)  # running; still holding the port; let go
    http = FakeHttp((200, {"stopping": True}))
    monkeypatch.setattr(cli, "_http", http)
    assert cli.main(["stop"]) == 0
    assert http.calls == [("POST", "http://127.0.0.1:8765/api/quit")]
    assert len(no_sleep) == 1
    assert "Work Assistant has stopped." in capsys.readouterr().out


def test_stop_when_not_running(monkeypatch, no_sleep, capsys):
    port(monkeypatch, True)
    http = FakeHttp()
    monkeypatch.setattr(cli, "_http", http)
    assert cli.main(["stop"]) == 0
    assert http.calls == []
    assert "Work Assistant is not running." in capsys.readouterr().out


def test_stop_when_the_port_is_released_before_the_request(monkeypatch, no_sleep, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp(refused()))
    assert cli.main(["stop"]) == 0
    assert "Work Assistant is not running." in capsys.readouterr().out


@pytest.mark.parametrize("answers", [
    [(404, {"detail": "Not Found"}), (404, {"detail": "Not Found"})],  # refused, and not Work Assistant either
    [httpx.ReadTimeout("timed out")],                                  # never answers
])
def test_stop_when_another_program_has_the_port(monkeypatch, no_sleep, capsys, answers):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp(*answers))
    assert cli.main(["stop"]) == 1
    assert "did not accept the request to stop" in capsys.readouterr().out


def test_stop_an_app_from_before_stop_existed(monkeypatch, no_sleep, capsys):
    """An app started with older code has no quit request (404) but is still Work Assistant."""
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((404, {"detail": "Not Found"}), (200, SETTINGS_BODY)))
    assert cli.main(["stop"]) == 1
    assert "started before it could be stopped this way. Close the window it runs in" in capsys.readouterr().out


def test_stop_when_the_app_keeps_running(monkeypatch, no_sleep, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((200, {"stopping": True})))
    assert cli.main(["stop"]) == 1
    assert len(no_sleep) == cli.STOP_WAIT_CHECKS
    assert "still running" in capsys.readouterr().out


# -- serve --port ------------------------------------------------------------------------------------------


def test_serve_port_option(monkeypatch, workdir, opened, run, capsys):
    """`--port N` serves on N and checks only N, so the demo runs beside the owner's real app on 8765."""
    monkeypatch.setattr(cli, "_port_free", lambda host, p: p != 8765)  # the real app holds 8765
    assert serve(workdir, "--port", "8811", "--no-browser") == 0
    server = run.servers[0]
    assert server.config.port == 8811 and server.config.host == "127.0.0.1"
    assert "http://127.0.0.1:8811/" in capsys.readouterr().out


def test_serve_demo_on_another_port_ignores_the_real_app(monkeypatch, workdir, opened, run):
    from work_assistant import demo

    monkeypatch.setattr(cli, "_port_free", lambda host, p: p != 8765)
    monkeypatch.setattr(cli, "_http", FakeHttp())  # any request would fail: nothing may be probed
    monkeypatch.setattr(cli, "DEMO_DB_PATH", workdir / "demo.db")
    monkeypatch.setattr(cli, "DEMO_LOG_DIR", workdir / "demo-logs")
    monkeypatch.setattr(demo, "build_demo_db",
                        lambda path, *, log_dir, now=None: demo.db.init_db(demo.db.connect(path)))
    assert serve(workdir, "--demo", "--port", "8811", "--no-browser") == 0
    assert run.servers[0].config.port == 8811


def test_serve_demo_date_builds_the_demo_as_of_that_day_and_pins_the_app_to_it(monkeypatch, workdir, opened, run):
    """Final review F5: `--demo-date` dates the demo and the app's today, so Eyes baselines do not drift."""
    from datetime import date, datetime, timezone

    from work_assistant import demo

    built = []

    def build(path, *, log_dir, now=None):
        built.append(now)
        demo.db.init_db(demo.db.connect(path))

    monkeypatch.setattr(cli, "_port_free", lambda host, p: p != 8765)
    monkeypatch.setattr(cli, "_http", FakeHttp())
    monkeypatch.setattr(cli, "DEMO_DB_PATH", workdir / "demo.db")
    monkeypatch.setattr(cli, "DEMO_LOG_DIR", workdir / "demo-logs")
    monkeypatch.setattr(demo, "build_demo_db", build)
    monkeypatch.setattr(demo.tzlocal, "get_localzone_name", lambda: "America/Chicago")
    assert serve(workdir, "--demo", "--demo-date", "2026-10-19", "--port", "8811", "--no-browser") == 0
    assert built == [datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)], "noon on that day, this PC's zone"
    app = run.servers[0].config.app
    with TestClient(app, base_url="http://127.0.0.1:8811") as client:
        body = client.get("/api/activity", params={"period": "last_week"}).json()
    assert body["today"] == "2026-10-19", "the app's today is the demo's day, not the real one"
    assert body["period"]["start"] == "2026-10-11" and body["period"]["end"] == "2026-10-17"
    assert demo.as_of(date(2026, 10, 19)) == built[0]


def test_serve_demo_date_needs_the_demo(workdir, capsys):
    with pytest.raises(SystemExit) as stopped:
        serve(workdir, "--demo-date", "2026-10-19")
    assert stopped.value.code == 2 and "--demo-date only goes with --demo" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        serve(workdir, "--demo", "--demo-date", "Oct 19")


def test_serve_port_taken_is_refused(monkeypatch, workdir, opened, run, capsys):
    port(monkeypatch, False)
    monkeypatch.setattr(cli, "_http", FakeHttp((404, {"detail": "Not Found"})))
    assert serve(workdir, "--port", "8811") == 1
    assert run.servers == [] and "Port 8811 is in use" in capsys.readouterr().out


def test_serve_config_default_port_and_override():
    assert cli.serve_config() == {"host": "127.0.0.1", "port": 8765}
    assert cli.serve_config(9001)["port"] == 9001
