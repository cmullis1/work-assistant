from datetime import date

import keyring
import keyring.backend
import pytest

from work_assistant.db import connect, init_db


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Keyring backend that keeps secrets in a dict, never touching Credential Manager."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring():
    """Install an in-memory keyring around every test, restoring the previous backend afterward.

    Autouse so no test can ever reach the real Windows Credential Manager. Tests that need the
    backend (e.g. to inspect `.store`) can still request it by name.
    """
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_real_schtasks(monkeypatch):
    """Stand in for the real Task Scheduler around every test, as if this PC had no schtasks.

    Autouse so no test can ever install, remove or query the real daily task. Tests that need
    schtasks answers pass a fake runner, or replace `_system_runner` themselves.
    """
    from work_assistant import scheduler

    def blocked(args, **kwargs):
        raise FileNotFoundError(2, "real schtasks is blocked in tests", args[0])

    monkeypatch.setattr(scheduler, "_system_runner", blocked)


@pytest.fixture(autouse=True)
def no_real_powershell(monkeypatch):
    """Stand in for PowerShell around every test, as if this PC had none.

    Autouse so no test can ever write a shortcut to the real desktop. Tests that need PowerShell
    answers pass a fake runner, or replace `_system_runner` themselves.
    """
    from work_assistant import desktop

    def blocked(args, **kwargs):
        raise FileNotFoundError(2, "real PowerShell is blocked in tests", args[0])

    monkeypatch.setattr(desktop, "_system_runner", blocked)


@pytest.fixture(autouse=True)
def no_real_app_server(monkeypatch):
    """Block what `serve` and `stop` do to the real PC around every test: checking the app's port,
    an HTTP request to it, running a real server, and opening a browser. The owner's own app may be
    on port 8765.

    Autouse so no test can ever stop, query or collide with the owner's running app. Tests that need
    them replace `cli._port_free`, `cli._http`, `cli._run_server` or `cli._open_browser` with fakes.
    """
    from work_assistant import cli

    def blocked(*args, **kwargs):
        raise RuntimeError("real port checks, HTTP, servers and browsers are blocked in tests")

    for name in ("_port_free", "_http", "_run_server", "_open_browser"):
        monkeypatch.setattr(cli, name, blocked)


@pytest.fixture(autouse=True)
def no_real_certificate_reads(monkeypatch):
    """Block reading Cloudflare's certificate address around every test, as if it couldn't be reached.

    Autouse so no test ever reaches Cloudflare. Tests that need certificates give the Verifier a `fetch` that
    answers with test keys (tests/test_access.py), or replace `access.fetch_certs` with one.
    """
    from work_assistant import access

    def blocked(url):
        raise RuntimeError("real certificate reads are blocked in tests")

    monkeypatch.setattr(access, "fetch_certs", blocked)


@pytest.fixture(autouse=True)
def no_leftover_server_config():
    """Undo what a server config applies to the whole process (`server_config.apply`: the secrets file and the
    pinned time zone) after every test, so one test's hosted copy never reaches the next.

    Autouse because `create_app(config=...)` and the CLI's `--config` apply it as a side effect."""
    yield
    from work_assistant import credentials, settings

    credentials.use_file(None)
    settings.pin_timezone(None)


FIXED_TODAY = date(2026, 10, 17)   # a Saturday: last week is Oct 4-10, the synthetic data's usual week


def pytest_configure(config):
    config.addinivalue_line("markers", "real_today: leave the web app's real `_today` in place (no pin)")


@pytest.fixture(autouse=True)
def fixed_today(request, monkeypatch):
    """Pin the web app's "today" (`web.app._today`) to FIXED_TODAY around every test.

    Autouse because the default periods (Last week, This week so far) follow today's date, and the
    synthetic calls sit on fixed dates: without the pin, tests of the default views would change result
    as the real calendar moves on. Tests that need another day replace `_today` themselves; a test of
    `_today` itself opts out with `@pytest.mark.real_today`.
    """
    if request.node.get_closest_marker("real_today"):
        return None
    from work_assistant.web import app as webapp

    monkeypatch.setattr(webapp, "_today", lambda tz: FIXED_TODAY)
    return FIXED_TODAY


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "work-assistant.db")
    init_db(connection)
    yield connection
    connection.close()
