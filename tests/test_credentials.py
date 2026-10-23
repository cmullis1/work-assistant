import pytest

from work_assistant.cli import main
from work_assistant.credentials import get_secret, secret_status, set_secret
from work_assistant.db import connect
from work_assistant.settings import get_setting


def test_secret_roundtrip_and_status(memory_keyring):
    assert secret_status() == {"zendesk_token": False, "anthropic_api_key": False}
    set_secret("zendesk_token", "s3cret")
    assert get_secret("zendesk_token") == "s3cret"
    assert get_secret("anthropic_api_key") is None
    assert secret_status() == {"zendesk_token": True, "anthropic_api_key": False}
    assert memory_keyring.store == {("work-assistant", "zendesk_token"): "s3cret"}


def test_unknown_secret_name_rejected(memory_keyring):
    with pytest.raises(ValueError):
        set_secret("other", "x")
    with pytest.raises(ValueError):
        get_secret("other")


def test_empty_secret_rejected(memory_keyring):
    with pytest.raises(ValueError):
        set_secret("zendesk_token", "")


def _fake_prompts(monkeypatch, inputs, secrets):
    inputs, secrets = iter(inputs), iter(secrets)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(secrets))


def test_setup_saves_settings_and_secrets(memory_keyring, monkeypatch, tmp_path):
    db = tmp_path / "work-assistant.db"
    _fake_prompts(monkeypatch, [" acme ", " Boss@Acme.com "], ["tok-XYZ", "sk-ant-ABC"])
    assert main(["setup"], db_path=db) == 0
    conn = connect(db)
    try:
        assert get_setting(conn, "zendesk_subdomain") == "acme"
        assert get_setting(conn, "zendesk_email") == "Boss@Acme.com"
    finally:
        conn.close()
    assert get_secret("zendesk_token") == "tok-XYZ"
    assert get_secret("anthropic_api_key") == "sk-ant-ABC"


def test_setup_never_prints_secret(memory_keyring, monkeypatch, tmp_path, capsys):
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["tok-XYZ", "sk-ant-ABC"])
    main(["setup"], db_path=tmp_path / "work-assistant.db")
    captured = capsys.readouterr()
    assert "Saved." in captured.out
    for stream in (captured.out, captured.err):
        assert "tok-XYZ" not in stream
        assert "sk-ant-ABC" not in stream


def test_setup_blank_secret_keeps_existing(memory_keyring, monkeypatch, tmp_path):
    set_secret("zendesk_token", "old-token")
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["", "sk-ant-ABC"])
    assert main(["setup"], db_path=tmp_path / "work-assistant.db") == 0
    assert get_secret("zendesk_token") == "old-token"
    assert get_secret("anthropic_api_key") == "sk-ant-ABC"


def test_setup_blank_secret_with_nothing_stored_fails(memory_keyring, monkeypatch, tmp_path):
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["", "sk-ant-ABC"])
    with pytest.raises(SystemExit):
        main(["setup"], db_path=tmp_path / "work-assistant.db")
    assert secret_status() == {"zendesk_token": False, "anthropic_api_key": False}


def test_setup_blank_subdomain_fails(memory_keyring, monkeypatch, tmp_path):
    _fake_prompts(monkeypatch, ["  ", "boss@acme.com"], ["a", "b"])
    with pytest.raises(SystemExit):
        main(["setup"], db_path=tmp_path / "work-assistant.db")


def test_setup_uses_default_db_path(memory_keyring, monkeypatch, tmp_path):
    db = tmp_path / "default.db"
    monkeypatch.setattr("work_assistant.db.DB_PATH", db)
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["a", "b"])
    assert main(["setup"]) == 0
    assert db.exists()


def test_memory_keyring_is_active_without_requesting_it():
    import keyring

    assert type(keyring.get_keyring()).__name__ == "InMemoryKeyring"


def test_set_secret_strips_value(memory_keyring):
    set_secret("zendesk_token", "  tok \r\n")
    assert get_secret("zendesk_token") == "tok"


@pytest.mark.parametrize("blank", ["", " ", "\t\r\n  "])
def test_set_secret_rejects_whitespace_only(memory_keyring, blank):
    with pytest.raises(ValueError, match="empty or whitespace"):
        set_secret("zendesk_token", blank)
    assert secret_status()["zendesk_token"] is False


def test_setup_strips_secrets(memory_keyring, monkeypatch, tmp_path):
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["  tok-XYZ\r\n", "\tsk-ant-ABC "])
    assert main(["setup"], db_path=tmp_path / "work-assistant.db") == 0
    assert get_secret("zendesk_token") == "tok-XYZ"
    assert get_secret("anthropic_api_key") == "sk-ant-ABC"


def test_setup_rejects_whitespace_only_secret(memory_keyring, monkeypatch, tmp_path):
    set_secret("zendesk_token", "old-token")
    _fake_prompts(monkeypatch, ["acme", "boss@acme.com"], ["   ", "sk-ant-ABC"])
    with pytest.raises(SystemExit) as exc:
        main(["setup"], db_path=tmp_path / "work-assistant.db")
    assert "whitespace" in str(exc.value)
    assert get_secret("zendesk_token") == "old-token"
    assert get_secret("anthropic_api_key") is None


@pytest.mark.parametrize(
    "typed",
    ["acme", "ACME", "acme.zendesk.com", "https://acme.zendesk.com", "https://acme.zendesk.com/",
     " HTTPS://Acme.Zendesk.com/ ", "http://acme.zendesk.com"],
)
def test_setup_normalizes_subdomain(memory_keyring, monkeypatch, tmp_path, typed):
    db = tmp_path / "work-assistant.db"
    _fake_prompts(monkeypatch, [typed, "boss@acme.com"], ["a", "b"])
    assert main(["setup"], db_path=db) == 0
    conn = connect(db)
    try:
        assert get_setting(conn, "zendesk_subdomain") == "acme"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "typed",
    ["acme corp", "acme_corp", "acme/path", "https://acme.zendesk.com/agent", "acme.example.com",
     "ac!me", "https://", ".zendesk.com", "-"],
)
def test_setup_rejects_bad_subdomain(memory_keyring, monkeypatch, tmp_path, typed):
    _fake_prompts(monkeypatch, [typed, "boss@acme.com"], ["a", "b"])
    with pytest.raises(SystemExit) as exc:
        main(["setup"], db_path=tmp_path / "work-assistant.db")
    assert "subdomain" in str(exc.value).lower()
    assert secret_status() == {"zendesk_token": False, "anthropic_api_key": False}


# -- the secrets file (the hosted copy: a config's `secrets_file`) ------------------------------------------


def test_file_secrets_round_trip_and_mode(tmp_path, memory_keyring):
    import json
    import os
    import stat
    import sys

    from work_assistant import credentials

    path = tmp_path / "secrets.json"
    credentials.use_file(path)
    assert secret_status() == {"zendesk_token": False, "anthropic_api_key": False}, "no file yet: nothing is set"
    path.write_text("", encoding="utf-8")
    assert get_secret("zendesk_token") is None, "the install script leaves the file empty"
    set_secret("zendesk_token", "  tok-1 \n")
    assert get_secret("zendesk_token") == "tok-1"
    assert json.loads(path.read_text(encoding="utf-8")) == {"zendesk_token": "tok-1"}
    set_secret("zendesk_token", "tok-2")
    assert get_secret("zendesk_token") == "tok-2"
    assert secret_status() == {"zendesk_token": True, "anthropic_api_key": False}
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "readable only by the service user"
    with pytest.raises(ValueError, match="empty or whitespace"):
        set_secret("zendesk_token", "  ")
    with pytest.raises(ValueError):
        set_secret("other", "x")
    assert memory_keyring.store == {}
    credentials.use_file(None)
    assert get_secret("zendesk_token") is None, "back to the keyring, which holds nothing"


def test_file_secrets_never_touch_keyring(tmp_path, monkeypatch):
    import keyring

    from work_assistant import credentials

    def refuse(*args, **kwargs):
        raise AssertionError("the secrets file must never reach the keyring")

    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, refuse)
    credentials.use_file(tmp_path / "secrets.json")
    set_secret("zendesk_token", "tok")
    assert get_secret("zendesk_token") == "tok"
    assert secret_status()["zendesk_token"] is True


def test_a_broken_secrets_file_is_refused_without_quoting_it(tmp_path):
    from work_assistant import credentials

    path = tmp_path / "secrets.json"
    path.write_text('["tok-in-a-list"]', encoding="utf-8")
    credentials.use_file(path)
    with pytest.raises(ValueError) as refused:
        get_secret("zendesk_token")
    assert "tok-in-a-list" not in str(refused.value) and str(path) in str(refused.value)
    path.write_text("{not json tok-XYZ", encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        get_secret("zendesk_token")
    assert "tok-XYZ" not in str(refused.value)
