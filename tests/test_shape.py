import importlib.util
import json
from pathlib import Path

import pytest

from work_assistant import credentials, db, settings
from work_assistant.zendesk.shape import KEEP_PATHS, path_report, shape_of


def test_shape_hides_content():
    raw = {"subject": "Elston Electric", "via": {"channel": "voice"}, "id": 5}
    assert shape_of(raw) == {"subject": "str", "via": {"channel": "voice"}, "id": "int"}


def test_shape_lists():
    raw = {"comments": [{"type": "VoiceComment", "body": "hi"}, {}]}
    assert shape_of(raw) == {"comments": {"len": 2, "item": {"type": "VoiceComment", "body": "str"}}}


def test_shape_scalar_types_and_empty_list():
    raw = {"a": True, "b": None, "c": 1.5, "d": 7, "e": []}
    assert shape_of(raw) == {"a": "bool", "b": "null", "c": "float", "d": "int", "e": {"len": 0}}


def test_shape_keep_paths_are_configurable():
    raw = {"via": {"channel": "voice"}, "subject": "Elston"}
    assert shape_of(raw, frozenset()) == {"via": {"channel": "str"}, "subject": "str"}
    assert shape_of(raw, frozenset({"subject"})) == {"via": {"channel": "str"}, "subject": "Elston"}
    assert "via.channel" in KEEP_PATHS and "users[].role" in KEEP_PATHS


def test_shape_keeps_enum_by_full_path_and_list_notation():
    raw = {
        "priority": "urgent",
        "via": {"channel": "voice", "source": {"rel": "inbound"}},
        "comments": [
            {"type": "VoiceComment", "via": {"channel": "voice"}, "attachments": [{"content_type": "audio/mpeg"}]}
        ],
        "users": [{"role": "end-user"}],
    }
    out = shape_of(raw)
    assert out["priority"] == "urgent"
    assert out["via"] == {"channel": "voice", "source": {"rel": "inbound"}}
    assert out["comments"]["item"]["type"] == "VoiceComment"
    assert out["comments"]["item"]["via"] == {"channel": "voice"}
    assert out["comments"]["item"]["attachments"]["item"] == {"content_type": "audio/mpeg"}
    assert out["users"]["item"] == {"role": "end-user"}


def test_shape_never_keeps_long_spaced_or_nested_values_at_kept_paths():
    raw = {"status": "x" * 200, "type": {"body": "Elston Electric"}, "role": ["Elston Electric"], "priority": "has space"}
    out = shape_of(raw)
    assert out["status"] == "str"
    assert out["type"] == {"body": "str"}
    assert out["role"] == {"len": 1, "item": "str"}
    assert out["priority"] == "str"


LEAK = "Owes 4200 - collections"


def test_shape_does_not_keep_enum_named_keys_under_custom_containers():
    raw = {
        "custom_fields": [{"id": 1, "value": LEAK}],
        "users": [{"role": "end-user", "user_fields": {"status": LEAK, "type": "VIP-Elston"}}],
        "comments": [{"type": "Comment", "metadata": {"custom": {"status": LEAK, "role": "VIP-Elston"}}}],
        "tags": ["vip-elston"],
        "status": "open",
    }
    out = shape_of(raw)
    assert out["users"]["item"]["role"] == "end-user"
    assert out["users"]["item"]["user_fields"] == {"status": "str", "type": "str"}
    assert out["comments"]["item"]["metadata"] == {"custom": {"status": "str", "role": "str"}}
    assert out["custom_fields"]["item"] == {"id": "int", "value": "str"}
    assert out["tags"] == {"len": 1, "item": "str"}
    assert out["status"] == "open"
    text = json.dumps(out)
    assert LEAK not in text and "VIP-Elston" not in text and "vip-elston" not in text


def test_shape_reported_leak_case_from_review():
    raw = {"users": [{"role": "end-user", "user_fields": {"status": "Owes 4200 - collections", "type": "VIP Elston Electric"}}]}
    assert shape_of(raw) == {
        "users": {"len": 1, "item": {"role": "end-user", "user_fields": {"status": "str", "type": "str"}}}
    }


def test_path_report_all_present():
    tickets = [{"via": {"channel": "voice", "source": {"rel": "inbound"}}}]
    comments = [
        {"type": "Comment"},
        {"type": "VoiceComment", "data": {"answered_by_id": 1, "call_duration": 30, "started_at": "t"}},
    ]
    assert path_report(tickets, comments) == {
        "via.source.rel": True,
        'comments[].type == "VoiceComment"': True,
        "comments[].data.answered_by_id": True,
        "comments[].data.call_duration": True,
        "comments[].data.started_at": True,
    }


def test_path_report_all_absent():
    tickets = [{"via": {"channel": "voice", "source": {}}}, {"via": None}]
    comments = [{"type": "Comment", "data": None}, {"type": "VoiceComment", "data": {"recording_url": "u"}}]
    report = path_report(tickets, comments)
    assert report["via.source.rel"] is False
    assert report['comments[].type == "VoiceComment"'] is True
    assert report["comments[].data.answered_by_id"] is False
    assert report["comments[].data.call_duration"] is False
    assert report["comments[].data.started_at"] is False


def test_path_report_ignores_data_on_non_voice_comments():
    comments = [
        {"type": "Comment", "data": {"answered_by_id": 1, "call_duration": 2, "started_at": "t"}},
        {"type": "VoiceComment", "data": {"recording_url": "u"}},
    ]
    report = path_report([], comments)
    assert report['comments[].type == "VoiceComment"'] is True
    assert report["comments[].data.answered_by_id"] is False
    assert report["comments[].data.call_duration"] is False
    assert report["comments[].data.started_at"] is False


def test_path_report_empty_inputs():
    assert not any(path_report([], []).values())


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inspect_zendesk.py"
needs_script = pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/inspect_zendesk.py is not in this copy of the repo")


def _load_script():
    spec = importlib.util.spec_from_file_location("inspect_zendesk", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _configured_db(tmp_path):
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    settings.set_setting(conn, "zendesk_subdomain", "acme")
    settings.set_setting(conn, "zendesk_email", "boss@acme.com")
    conn.close()
    credentials.set_secret("zendesk_token", "TOKEN-VALUE")
    return db_path


@needs_script
def test_inspect_script_prints_structure_only(memory_keyring, monkeypatch, tmp_path, capsys):
    db_path = _configured_db(tmp_path)
    secret_text = "CUSTOMER-SECRET-TEXT"
    voice = {
        "id": 11, "subject": secret_text, "priority": "urgent", "tags": [secret_text],
        "custom_fields": [{"id": 5, "value": secret_text}],
        "via": {"channel": "voice", "source": {"rel": "inbound", "from": {"phone": secret_text}}},
    }
    pages = {
        "/api/v2/incremental/tickets/cursor.json": {
            "tickets": [{"id": 1, "via": {"channel": "email"}}, voice], "end_of_stream": True,
        },
        "/api/v2/tickets/11/comments.json": {
            "comments": [{"id": 1, "type": "VoiceComment", "body": secret_text,
                          "data": {"answered_by_id": 9, "call_duration": 60, "started_at": secret_text},
                          "metadata": {"custom": {"status": secret_text, "type": secret_text}}}],
            "users": [{"id": 9, "name": secret_text, "email": secret_text, "role": "agent",
                       "user_fields": {"status": secret_text, "type": secret_text}}],
        },
        "/api/v2/ticket_fields.json": {
            "ticket_fields": [{"title": "Site", "type": "text", "description": secret_text}],
        },
    }
    seen = []

    def fake_get(url, **kwargs):
        seen.append((url, kwargs))
        path = url.split("?")[0].replace("https://acme.zendesk.com", "")
        if path not in pages:
            raise AssertionError(f"unexpected URL {url}")
        return _Resp(pages[path])

    script = _load_script()
    monkeypatch.setattr(script.httpx, "get", fake_get)
    assert script.main(db_path=db_path) == 0

    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert secret_text not in stream
        assert "TOKEN-VALUE" not in stream
        assert "boss@acme.com" not in stream
    assert "Site" in out.out and "text" in out.out
    assert '"urgent"' in out.out and '"inbound"' in out.out
    assert [u.split("?")[0] for u, _ in seen] == [
        "https://acme.zendesk.com/api/v2/incremental/tickets/cursor.json",
        "https://acme.zendesk.com/api/v2/tickets/11/comments.json",
        "https://acme.zendesk.com/api/v2/ticket_fields.json",
    ]
    assert all(kw["auth"] == ("boss@acme.com/token", "TOKEN-VALUE") for _, kw in seen)
    json.loads(out.out)  # the whole output is one JSON document


@needs_script
def test_inspect_script_failure_prints_status_only(memory_keyring, monkeypatch, tmp_path, capsys):
    db_path = _configured_db(tmp_path)
    script = _load_script()
    monkeypatch.setattr(script.httpx, "get", lambda url, **kw: _Resp({"error": "CUSTOMER-SECRET-TEXT"}, 401))
    assert script.main(db_path=db_path) == 1
    out = capsys.readouterr()
    assert "401" in out.err
    assert "CUSTOMER-SECRET-TEXT" not in out.out + out.err
    assert "TOKEN-VALUE" not in out.out + out.err


@needs_script
def test_inspect_script_requires_setup(memory_keyring, tmp_path, capsys):
    script = _load_script()
    assert script.main(db_path=tmp_path / "empty.db") == 1
    assert "work-assistant setup" in capsys.readouterr().err


@needs_script
def test_inspect_script_unexpected_exception_prints_class_name_only(memory_keyring, monkeypatch, tmp_path, capsys):
    db_path = _configured_db(tmp_path)

    class _BadJson(_Resp):
        def json(self):
            raise ValueError("CUSTOMER-SECRET-TEXT in body")

    script = _load_script()
    monkeypatch.setattr(script.httpx, "get", lambda url, **kw: _BadJson({}, 200))
    assert script.main(db_path=db_path) == 1
    out = capsys.readouterr()
    assert "Failed: ValueError" in out.err
    assert "CUSTOMER-SECRET-TEXT" not in out.out + out.err
    assert "TOKEN-VALUE" not in out.out + out.err
