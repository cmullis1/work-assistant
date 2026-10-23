"""The server config file (hosted Team activity design §6, plan Task 1): loading and checking it with
`server_config.load`, and the CLI commands that take `--config` (serve, set-secret, activity sync).

Nothing here binds a port, starts a server, or reads or writes a real secret: secrets go to files under tmp_path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_access import Certs, token
from work_assistant import access, cli, credentials, server_config, settings
from work_assistant.db import connect, init_db
from work_assistant.server_config import ConfigError, ServerConfig

HOSTED = {
    "edition": "hosted",
    "public_host": "activity.example.com",
    "db_path": "data/hosted.db",
    "log_dir": "data/logs",
    "secrets_file": "secrets.json",
    "admin_email": "Owner@Example.com",
    "access_team_domain": "example.cloudflareaccess.com",
    "access_aud": "aud-tag-for-tests",
    "timezone": "America/Chicago",
}
REQUIRED_FOR_HOSTED = ("public_host", "admin_email", "access_team_domain", "access_aud", "timezone")
SIGN_IN_HEADER = "Cf-Access-Jwt-Assertion"


def toml_value(value) -> str:
    return json.dumps(value) if isinstance(value, str) else str(value).lower() if isinstance(value, bool) \
        else str(value)


def write_config(folder: Path, values: dict, name: str = "config.toml") -> Path:
    path = folder / name
    path.write_text("".join(f"{key} = {toml_value(value)}\n" for key, value in values.items()), encoding="utf-8")
    return path


def refusal(folder: Path, values: dict) -> str:
    """The ConfigError's message for these values, without the config file's path (a test folder's name can hold
    a key's name)."""
    path = write_config(folder, values)
    with pytest.raises(ConfigError) as refused:
        server_config.load(path)
    return str(refused.value).replace(str(path), "CONFIG")


# -- loading ----------------------------------------------------------------------------------------------


def test_load_hosted_config_requires_access_and_timezone(tmp_path):
    for key in REQUIRED_FOR_HOSTED:
        message = refusal(tmp_path, {k: v for k, v in HOSTED.items() if k != key})
        assert message == f"The config file CONFIG is missing {key}.", f"the message names the missing {key}"


def test_load_hosted_config_reads_every_value(tmp_path):
    config = server_config.load(write_config(tmp_path, HOSTED))
    assert config == ServerConfig(
        edition="hosted", public_host="activity.example.com", bind_host="127.0.0.1", port=8770,
        db_path=tmp_path / "data" / "hosted.db", log_dir=tmp_path / "data" / "logs",
        secrets_file=tmp_path / "secrets.json", admin_email="owner@example.com",
        access_team_domain="example.cloudflareaccess.com", access_aud="aud-tag-for-tests", timezone="America/Chicago")


def test_absolute_paths_stay_as_written_and_port_and_bind_host_can_be_set(tmp_path):
    folder = tmp_path / "elsewhere"
    values = {**HOSTED, "db_path": str(folder / "a.db"), "log_dir": str(folder / "logs"),
              "secrets_file": str(folder / "s.json"), "port": 8771, "bind_host": "127.0.0.2"}
    config = server_config.load(write_config(tmp_path, values))
    assert (config.db_path, config.log_dir, config.secrets_file) == (
        folder / "a.db", folder / "logs", folder / "s.json")
    assert (config.port, config.bind_host) == (8771, "127.0.0.2")


def test_bad_timezone_is_refused(tmp_path):
    message = refusal(tmp_path, {**HOSTED, "timezone": "America/Atlantis"})
    assert message == ("In the config file CONFIG: timezone 'America/Atlantis' is not a known time zone. Use a name "
                       "such as America/Chicago.")


def test_a_folder_of_zones_is_refused_in_plain_words(tmp_path):
    """"America" is a folder of zones, not a zone: on Windows opening it fails with an OSError."""
    message = refusal(tmp_path, {**HOSTED, "timezone": "America"})
    assert message == ("In the config file CONFIG: timezone 'America' is not a known time zone. Use a name "
                       "such as America/Chicago.")


def test_a_zone_that_cant_be_opened_is_refused_in_plain_words(tmp_path, monkeypatch):
    def unreadable(name):
        raise IsADirectoryError(21, "Is a directory", name)

    monkeypatch.setattr(server_config, "ZoneInfo", unreadable)
    message = refusal(tmp_path, {**HOSTED, "timezone": "America"})
    assert message == ("In the config file CONFIG: timezone 'America' is not a known time zone. Use a name "
                       "such as America/Chicago.")


def test_full_edition_needs_only_the_edition_and_its_files(tmp_path):
    config = server_config.load(write_config(tmp_path, {"edition": "full", "db_path": "q.db", "log_dir": "logs"}))
    assert config.edition == "full" and config.public_host is None and config.timezone is None
    assert config.secrets_file is None and config.port == 8770


def test_a_public_host_is_refused_outside_the_hosted_edition(tmp_path):
    """Final review F5: only the hosted edition checks Cloudflare's sign-in. A full edition on a public host
    would answer anyone, Call QA and its paid runs included, so its config may not name one."""
    message = refusal(tmp_path, {"edition": "full", "public_host": "activity.example.com", "db_path": "q.db",
                                 "log_dir": "logs"})
    assert message == ('In the config file CONFIG: public_host is only for edition = "hosted", which checks who '
                       'is signed in. The full edition has no sign-in, so it answers only on this computer: remove '
                       'public_host, or set edition = "hosted".')
    assert server_config.load(write_config(tmp_path, HOSTED)).public_host == "activity.example.com"


def test_port_8000_is_refused_as_install_sh_refuses_it(tmp_path):
    """Final review F6 (plan Global Constraints: never port 8000): the loader refuses the Zendesk Bridge's port too,
    so a config edited after install can't take it."""
    for edition in (HOSTED, {"edition": "full", "db_path": "q.db", "log_dir": "logs"}):
        message = refusal(tmp_path, {**edition, "port": 8000})
        assert message == ("In the config file CONFIG: port 8000 is used by another service on this server (the "
                           "Zendesk Bridge). Set port = 8770.")
    assert server_config.load(write_config(tmp_path, {**HOSTED, "port": 8001})).port == 8001


@pytest.mark.parametrize("key", ["edition", "db_path", "log_dir"])
def test_every_edition_needs_its_edition_database_and_log_folder(tmp_path, key):
    values = {k: v for k, v in {"edition": "full", "db_path": "q.db", "log_dir": "logs"}.items() if k != key}
    assert key in refusal(tmp_path, values)


@pytest.mark.parametrize("key, value", [
    ("edition", "call_qa"),
    ("public_host", "https://activity.example.com"),
    ("public_host", "activity.example.com/path"),
    ("public_host", ""),
    ("port", 0),
    ("port", 70000),
    ("port", "8770"),
    ("admin_email", "not an email"),
    ("access_team_domain", "https://example.cloudflareaccess.com"),
    ("access_aud", ""),
    ("timezone", 5),
    ("db_path", ""),
    ("secrets_file", 3),
])
def test_bad_values_are_refused_naming_the_key(tmp_path, key, value):
    assert key in refusal(tmp_path, {**HOSTED, key: value})


def test_the_old_edition_name_is_refused(tmp_path):
    """The hosted edition was called team_activity before the Work Assistant rename (T-120). The old name has no
    alias: a config that still says it is refused, naming the editions there are."""
    message = refusal(tmp_path, {**HOSTED, "edition": "team_activity"})
    assert message == "In the config file CONFIG: edition must be one of: full, hosted."


def test_an_unknown_key_is_refused(tmp_path):
    assert "unknown keys: secret_file." in refusal(tmp_path, {**HOSTED, "secret_file": "x.json"})


def test_a_missing_or_broken_file_is_refused_in_plain_words(tmp_path):
    with pytest.raises(ConfigError, match="doesn't exist"):
        server_config.load(tmp_path / "nowhere.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("edition = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="isn't valid TOML"):
        server_config.load(broken)


# -- the CLI ----------------------------------------------------------------------------------------------


@pytest.fixture
def hosted(tmp_path):
    """A hosted config whose database (with Zendesk settings), logs and secrets live under tmp_path."""
    path = write_config(tmp_path, HOSTED)
    conn = connect(tmp_path / "data" / "hosted.db")
    init_db(conn)
    settings.set_setting(conn, "zendesk_subdomain", "acme")
    settings.set_setting(conn, "zendesk_email", "owner@example.com")
    conn.close()
    return path


def test_set_secret_saves_the_token_in_the_config_secrets_file(hosted, tmp_path, monkeypatch, capsys,
                                                               memory_keyring):
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or "  tok-XYZ \n")
    assert cli.main(["set-secret", "zendesk_token", "--config", str(hosted)]) == 0
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {"zendesk_token": "tok-XYZ"}
    assert memory_keyring.store == {}, "nothing is written to the keyring"
    assert prompts and "hidden" in prompts[0]
    out = capsys.readouterr()
    assert "tok-XYZ" not in out.out + out.err
    assert "Saved" in out.out


def test_set_secret_takes_only_the_zendesk_token(hosted, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-ABC")
    with pytest.raises(SystemExit) as refused:
        cli.main(["set-secret", "anthropic_api_key", "--config", str(hosted)])
    assert refused.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["set-secret", "zendesk_token"])   # the config is required
    capsys.readouterr()


def test_set_secret_refuses_a_blank_token(hosted, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")
    with pytest.raises(SystemExit) as refused:
        cli.main(["set-secret", "zendesk_token", "--config", str(hosted)])
    assert "blank" in str(refused.value.code)
    assert not (tmp_path / "secrets.json").exists()


def test_a_bad_config_stops_the_command_with_its_message(tmp_path, capsys):
    path = write_config(tmp_path, {**HOSTED, "timezone": "Nowhere/Land"})
    with pytest.raises(SystemExit) as stopped:
        cli.main(["set-secret", "zendesk_token", "--config", str(path)])
    assert "timezone" in str(stopped.value.code)


def test_activity_sync_uses_the_config_database_log_folder_and_secrets(hosted, tmp_path, monkeypatch):
    from work_assistant.activity import sync as activity_sync
    from work_assistant.zendesk import client as zendesk_client

    (tmp_path / "secrets.json").write_text(json.dumps({"zendesk_token": "file-token"}), encoding="utf-8")
    built, synced = [], []

    class FakeClient:
        def __init__(self, subdomain, email, token):
            built.append((subdomain, email, token))

        def close(self):
            pass

    def fake_sync(conn, client, *, kind, now, log, logs_dir, alive):
        synced.append((conn.execute("PRAGMA database_list").fetchone()["file"], kind, logs_dir))
        return {"status": "failed", "synced_through": None, "pages": 0, "actions": 0, "error": "stop"}

    monkeypatch.setattr(zendesk_client, "ZendeskClient", FakeClient)
    monkeypatch.setattr(activity_sync, "sync", fake_sync)
    assert cli.main(["activity", "sync", "--kind", "refresh", "--config", str(hosted)]) == 1
    assert built == [("acme", "owner@example.com", "file-token")], "the token comes from the secrets file"
    [(database, kind, logs_dir)] = synced
    assert Path(database) == tmp_path / "data" / "hosted.db"
    assert kind == "refresh" and logs_dir == tmp_path / "data" / "logs"


def test_activity_sync_with_a_config_points_to_set_secret_when_zendesk_is_not_set_up(hosted, capsys):
    assert cli.main(["activity", "sync", "--config", str(hosted)]) == 1
    out = capsys.readouterr().out
    assert f"work-assistant set-secret zendesk_token --config {hosted}" in out
    assert "work-assistant setup" not in out, "setup would ask for an Anthropic key and save to the keyring"


def test_db_and_config_cannot_be_combined(hosted, tmp_path, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["--db", str(tmp_path / "other.db"), "activity", "sync", "--config", str(hosted)])
    assert refused.value.code == 2
    capsys.readouterr()


@pytest.fixture
def uvicorn_logging():
    yield
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()


def test_serve_with_a_config_listens_where_it_says_and_runs_its_edition(hosted, tmp_path, monkeypatch, capsys,
                                                                          uvicorn_logging):
    (tmp_path / "secrets.json").write_text(json.dumps({"zendesk_token": "file-token"}), encoding="utf-8")
    checked, servers = [], []
    certs = Certs()
    monkeypatch.setattr(cli, "_port_free", lambda host, port: checked.append((host, port)) or True)
    monkeypatch.setattr(cli, "_run_server", servers.append)
    monkeypatch.setattr(access, "fetch_certs", certs)
    assert cli.main(["serve", "--config", str(hosted), "--no-browser"]) == 0
    assert checked == [("127.0.0.1", 8770)]
    [server] = servers
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8770)
    assert "http://127.0.0.1:8770/" in capsys.readouterr().out
    assert (tmp_path / "data" / "logs" / "app.log").exists(), "the app log goes to the config's log folder"
    with TestClient(server.config.app, base_url="http://127.0.0.1:8770") as client:
        assert client.get("/api/settings").status_code == 403, "the config's Access values are checked"
        client.headers[SIGN_IN_HEADER] = token("owner@example.com")
        body = client.get("/api/settings", headers={"Host": "activity.example.com"}).json()
        assert client.get("/api/runs").status_code == 404
    assert certs.urls == ["https://example.cloudflareaccess.com/cdn-cgi/access/certs"]
    assert body["edition"] == "hosted"
    assert body["credentials"] == {"zendesk_token": True}, "secrets come from the config's file"
    assert body["timezone_effective"] == "America/Chicago"


def test_serve_with_a_config_finds_itself_already_running_by_its_health_check(hosted, monkeypatch, capsys):
    """Every other address of the hosted copy needs a sign-in, so serve's already-running check reads `/healthz`:
    a 403 must not read as another program holding the port."""
    calls, servers = [], []

    def http(method, url, timeout):
        calls.append((method, url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(cli, "_port_free", lambda host, port: False)
    monkeypatch.setattr(cli, "_http", http)
    monkeypatch.setattr(cli, "_run_server", servers.append)
    assert cli.main(["serve", "--config", str(hosted), "--no-browser"]) == 0
    assert calls == [("GET", "http://127.0.0.1:8770/healthz")]
    assert servers == []
    assert "already running at http://127.0.0.1:8770/" in capsys.readouterr().out


@pytest.mark.parametrize("status, body", [(403, {"detail": "Sign in through the company link."}),
                                          (404, {"detail": "Not Found"}), (200, {"ok": True, "other": 1})])
def test_serve_with_a_config_when_another_program_has_the_port(hosted, monkeypatch, capsys, status, body):
    monkeypatch.setattr(cli, "_port_free", lambda host, port: False)
    monkeypatch.setattr(cli, "_http", lambda method, url, timeout: httpx.Response(
        status, json=body, request=httpx.Request(method, url)))
    monkeypatch.setattr(cli, "_run_server", lambda server: pytest.fail("no server may start"))
    assert cli.main(["serve", "--config", str(hosted), "--no-browser"]) == 1
    assert "Port 8770 is in use by another program" in capsys.readouterr().out


def test_the_hosted_app_points_to_set_secret_when_zendesk_is_not_set_up(hosted, monkeypatch, uvicorn_logging):
    """Refresh and adding a tech, before the token is saved: the message names `set-secret --config`, never
    `work-assistant setup` (which would ask for an Anthropic key and save to the keyring)."""
    servers = []
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    monkeypatch.setattr(cli, "_run_server", servers.append)
    monkeypatch.setattr(access, "fetch_certs", Certs())
    assert cli.main(["serve", "--config", str(hosted), "--no-browser"]) == 0
    client = TestClient(servers[0].config.app, base_url="http://127.0.0.1:8770",
                        headers={SIGN_IN_HEADER: token("owner@example.com")})
    refresh = client.post("/api/activity/sync", json={"kind": "refresh"})
    # The hosted copy adds a tech through the Techs panel's route; its settings save refuses `team` (ruling S7).
    team = client.post("/api/team", json={"name": "New Tech", "email": "new@example.com"})
    for response in (refresh, team):
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert f"`work-assistant set-secret zendesk_token --config {hosted}`" in detail
        assert "work-assistant setup" not in detail


def test_zendesk_not_set_up_names_set_secret_only_with_a_config(tmp_path):
    from work_assistant.web.app import ZENDESK_NOT_SET_UP, Unavailable, subprocess_run_starter, zendesk_factory_for

    path = tmp_path / "q.db"
    conn = connect(path)
    init_db(conn)
    conn.close()
    config_path = tmp_path / "config.toml"
    sync = {"command": "activity-sync", "kind": "refresh"}
    for config, expected in ((config_path, f"`work-assistant set-secret zendesk_token --config {config_path}`"),
                             (None, ZENDESK_NOT_SET_UP)):
        for attempt in (lambda: subprocess_run_starter(path, tmp_path / "logs", config)(sync),
                        lambda: zendesk_factory_for(path, config)()):
            with pytest.raises(Unavailable) as raised:
                attempt()
            assert expected in str(raised.value)
    assert "work-assistant setup" in ZENDESK_NOT_SET_UP, "the PC's message is unchanged"


@pytest.mark.parametrize("extra", [["--demo"], ["--port", "8811"]])
def test_serve_config_refuses_the_demo_and_another_port(hosted, extra, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--config", str(hosted), *extra])
    assert refused.value.code == 2
    capsys.readouterr()


def test_the_app_starts_a_sync_with_the_same_config(tmp_path):
    from work_assistant.web.app import cli_args

    config_path = tmp_path / "config.toml"
    args = cli_args({"command": "activity-sync", "kind": "refresh"}, tmp_path / "hosted.db", config_path)
    assert args[1:] == ["-m", "work_assistant.cli", "activity", "sync", "--kind", "refresh", "--config", str(config_path)]
    assert cli_args({"command": "activity-sync", "kind": "refresh"}, tmp_path / "q.db")[1:] == [
        "-m", "work_assistant.cli", "--db", str(tmp_path / "q.db"), "activity", "sync", "--kind", "refresh"]
    assert cli.build_parser().parse_args(args[3:]).config == config_path
