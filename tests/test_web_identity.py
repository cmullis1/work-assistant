"""The sign-in check in the app (hosted Team activity design §3, plan Task 2): in the hosted edition every
request but `/healthz` and the static files must carry a token Cloudflare Access signed, read from the
`Cf-Access-Jwt-Assertion` header or else the `CF_Authorization` cookie; the full edition has no such check; the demo
signs in as a fixed person without a token; and the page shows a refused first read plainly.

Tokens are signed with the test keys in test_access, and the certificates come from a stand-in: nothing here reaches
Cloudflare."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from test_access import AUD, CERTS_A, DOMAIN, KEY_B, Certs, Clock, jwks, token
from test_compare import api_for
from test_web_edition import HELPERS, LOCAL, PAT, PUBLIC, db_path, hosted_config  # noqa: F401  (fixture)
from test_web_overview import STATIC, node_prelude, run_node
from test_web_test_view import PAGE_JS, answer, page_responses, run_page
from work_assistant import access, cli, people
from work_assistant.db import connect
from work_assistant.server_config import ServerConfig
from work_assistant.web.app import create_app

SIGN_IN = {"detail": "Sign in through the company link."}
ADMIN = "owner@example.com"
HEADER = "Cf-Access-Jwt-Assertion"
COOKIE = "CF_Authorization"


def with_whoami(app):
    """The app with one extra route that answers who the request was signed in as (`request.state.email`)."""

    @app.get("/api/test-whoami")
    def whoami(request: Request) -> dict:
        return {"email": request.state.email}

    return app


@pytest.fixture
def certs():
    return Certs()


@pytest.fixture
def client_hosted(db_path, tmp_path, certs):  # noqa: F811
    app = create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 7,
                     log_dir=tmp_path / "logs", startup_work=False,
                     access_verifier=access.Verifier(DOMAIN, AUD, fetch=certs))
    return TestClient(with_whoami(app), base_url=LOCAL)


@pytest.fixture
def client_full(db_path, tmp_path):  # noqa: F811
    app = create_app(db_path, run_starter=lambda spec: 1, log_dir=tmp_path / "logs", startup_work=False)
    return TestClient(with_whoami(app), base_url="http://127.0.0.1:8765")


def signed(email: str = ADMIN, **kwargs) -> dict:
    return {HEADER: token(email, **kwargs)}


def added(path, *emails: str) -> None:
    """Add `emails` as team leads, as the admin would: the app answers anyone it doesn't know "not added" (plan
    Task 3, tests/test_web_scoping.py), and these tests are about who a request is signed in as."""
    conn = connect(path)
    for email in emails:
        people.add_person(conn, email, "tl")
    conn.close()


# -- the hosted edition ----------------------------------------------------------------------------


def test_middleware_403_without_token_even_from_localhost(client_hosted, certs):
    for host in ("127.0.0.1:8770", "localhost:8770", PUBLIC):
        for path in ("/api/settings", "/api/activity", "/", "/api/test-whoami"):
            response = client_hosted.get(path, headers={"Host": host})
            assert (response.status_code, response.json()) == (403, SIGN_IN), (host, path)
    refused = client_hosted.put("/api/settings", json={"activity_gap_minutes": 100})
    assert (refused.status_code, refused.json()) == (403, SIGN_IN)
    assert client_hosted.get("/api/runs").status_code == 403, "a Call QA address gives nothing away without a sign-in"
    assert certs.urls == [], "no token, nothing to check"
    for bad in ("not-a-token", token(ADMIN, aud=["another-app"]), token(ADMIN, key=KEY_B)):
        assert client_hosted.get("/api/settings", headers={HEADER: bad}).status_code == 403
    assert client_hosted.get("/api/settings", headers=signed()).json()["activity_gap_minutes"] == 120, "nothing changed"


def test_middleware_accepts_cookie_or_header(client_hosted, db_path):  # noqa: F811
    added(db_path, PAT, "lee@example.com")
    by_header = client_hosted.get("/api/test-whoami", headers=signed("Pat@Example.com"))
    assert (by_header.status_code, by_header.json()) == (200, {"email": PAT})
    client_hosted.cookies.set(COOKIE, token("lee@example.com"))
    by_cookie = client_hosted.get("/api/test-whoami")
    assert (by_cookie.status_code, by_cookie.json()) == (200, {"email": "lee@example.com"})
    assert client_hosted.get("/api/test-whoami", headers=signed()).json() == {"email": ADMIN}, "the header comes first"
    assert client_hosted.get("/api/test-whoami", headers={HEADER: "not-a-token"}).status_code == 403, \
        "a bad header isn't saved by a good cookie"
    assert client_hosted.get("/").status_code == 200, "the page itself, with the cookie"
    changed = client_hosted.put("/api/settings", json={"activity_gap_minutes": 100}, headers=signed())
    assert changed.status_code == 200 and changed.json()["activity_gap_minutes"] == 100
    assert client_hosted.get("/api/runs", headers=signed()).status_code == 404, "signed in: Call QA still doesn't exist"


def test_middleware_503_when_certs_unreachable(db_path, tmp_path):  # noqa: F811
    """Review Focus 2: if Cloudflare's certificate address can't be reached, the app answers 503, never 200."""
    certs = Certs(httpx.ConnectError("no route to host"))
    clock = Clock()
    app = create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 7,
                     log_dir=tmp_path / "logs", startup_work=False,
                     access_verifier=access.Verifier(DOMAIN, AUD, fetch=certs, clock=clock))
    client = TestClient(with_whoami(app), base_url=LOCAL)
    for path in ("/api/settings", "/api/test-whoami", "/"):
        response = client.get(path, headers=signed())
        assert response.status_code == 503, path
        assert response.json() == {"detail": "The sign-in check couldn't reach Cloudflare. Try again in a few "
                                              "minutes."}
        clock.advance()
    certs.answers = [CERTS_A]
    assert client.get("/api/test-whoami", headers=signed()).json() == {"email": ADMIN}, "back once they answer"
    clock.advance()
    certs.answers = [httpx.ReadTimeout("timed out")]
    assert client.get("/api/settings", headers=signed(key=KEY_B, kid="key-b")).status_code == 503, \
        "a rotated key that can't be checked is 503 too"
    assert client.get("/api/settings", headers=signed()).status_code == 200, "known keys keep working"


def test_unknown_kid_rereads_the_certificates_through_the_app(db_path, tmp_path):  # noqa: F811
    """Review Focus 2 end to end: Cloudflare rotates its keys, the app re-reads once and lets the person in."""
    certs = Certs(CERTS_A, jwks((KEY_B, "key-b")))
    clock = Clock()
    app = create_app(db_path, config=hosted_config(tmp_path), run_starter=lambda spec: 7,
                     log_dir=tmp_path / "logs", startup_work=False,
                     access_verifier=access.Verifier(DOMAIN, AUD, fetch=certs, clock=clock))
    client = TestClient(with_whoami(app), base_url=LOCAL)
    assert client.get("/api/test-whoami", headers=signed()).status_code == 200
    clock.advance()
    assert client.get("/api/test-whoami", headers=signed(key=KEY_B, kid="key-b")).json() == {"email": ADMIN}
    assert len(certs.urls) == 2


def test_healthz_and_static_need_no_token(client_hosted, certs):
    assert client_hosted.get("/healthz").json() == {"ok": True}
    for path in ("/static/app.js", "/static/app.css"):
        assert client_hosted.get(path).status_code == 200, path
    assert certs.urls == []


def test_the_app_builds_its_verifier_from_the_config(db_path, tmp_path, monkeypatch):  # noqa: F811
    built = []

    class Recorded(access.Verifier):
        def __init__(self, team_domain, aud, fetch=None):
            built.append((team_domain, aud, fetch))
            super().__init__(team_domain, aud, fetch=Certs())

    monkeypatch.setattr(access, "Verifier", Recorded)
    app = create_app(db_path, config=hosted_config(tmp_path, access_team_domain="team.cloudflareaccess.com",
                                                          access_aud="the-aud-tag"),
                     run_starter=lambda spec: 7, log_dir=tmp_path / "logs", startup_work=False)
    assert built == [("team.cloudflareaccess.com", "the-aud-tag", None)]
    assert TestClient(app, base_url=LOCAL).get("/api/settings").status_code == 403


# -- the full edition ---------------------------------------------------------------------------------------


def test_full_edition_has_no_identity_check(client_full, db_path, tmp_path, monkeypatch):  # noqa: F811
    for headers in ({}, {HEADER: "not-a-token"}):
        response = client_full.get("/api/test-whoami", headers=headers)
        assert (response.status_code, response.json()) == (200, {"email": None})
        assert client_full.get("/api/settings", headers=headers).status_code == 200
        assert client_full.get("/", headers=headers).status_code == 200
    monkeypatch.setattr(access, "Verifier", None)   # building one would fail: neither app below builds one
    full = hosted_config(tmp_path, edition="full", port=8771)
    client = TestClient(with_whoami(create_app(db_path, config=full, run_starter=lambda spec: 1,
                                               log_dir=tmp_path / "logs", startup_work=False)),
                        base_url="http://127.0.0.1:8771")
    assert client.get("/api/test-whoami").json() == {"email": None}, "a full-edition config has no sign-in either"
    create_app(db_path, run_starter=lambda spec: 1, log_dir=tmp_path / "logs", startup_work=False)


# -- the demo's fixed identity ------------------------------------------------------------------------------


def demo_config(tmp_path) -> ServerConfig:
    return ServerConfig(edition="hosted", db_path=tmp_path / "work-assistant.db", log_dir=tmp_path / "logs",
                        admin_email="team.lead@example.com", timezone="America/Chicago", port=8811)


def test_demo_identity_needs_no_token_and_no_verifier(db_path, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(access, "Verifier", None)
    app = create_app(db_path, config=demo_config(tmp_path), demo=True, demo_identity="tl@example.com",
                     run_starter=lambda spec: 7, log_dir=tmp_path / "logs", startup_work=False)
    added(db_path, "tl@example.com")
    client = TestClient(with_whoami(app), base_url="http://127.0.0.1:8811")
    assert client.get("/api/test-whoami").json() == {"email": "tl@example.com"}
    assert client.get("/api/test-whoami", headers={HEADER: token("other@example.com")}).json() == \
        {"email": "tl@example.com"}, "a token changes nothing in the demo"
    assert client.get("/api/runs").status_code == 404
    assert client.get("/api/settings", headers={"Host": PUBLIC}).status_code == 403, "local hosts only"


def test_demo_identity_only_in_the_demo(db_path, tmp_path):  # noqa: F811
    with pytest.raises(ValueError):
        create_app(db_path, config=hosted_config(tmp_path), demo_identity="tl@example.com",
                   run_starter=lambda spec: 7, log_dir=tmp_path / "logs", startup_work=False)


def serve(workdir, *extra):
    return cli.main(["--db", str(workdir / "work-assistant.db"), "serve", *extra])


@pytest.fixture
def demo_serve(tmp_path, monkeypatch):
    """`serve --demo` in a scratch folder with a stand-in demo builder and server; returns the servers run."""
    import logging

    from work_assistant import demo

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_port_free", lambda host, p: True)
    monkeypatch.setattr(cli, "DEMO_DB_PATH", tmp_path / "demo.db")
    monkeypatch.setattr(cli, "DEMO_LOG_DIR", tmp_path / "demo-logs")
    monkeypatch.setattr(demo, "build_demo_db", lambda path, *, log_dir, now=None: demo.db.init_db(demo.db.connect(path)))
    servers = []
    monkeypatch.setattr(cli, "_run_server", servers.append)
    yield servers
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()


@pytest.mark.parametrize("extra", [
    ["--demo-user", "tl@example.com"],
    ["--edition", "hosted"],
    ["--edition", "full"],
    ["--edition", "hosted", "--demo-user", "tl@example.com"],
    ["--demo", "--demo-user", "tl@example.com"],                       # the full edition has no sign-in
    ["--demo", "--edition", "full", "--demo-user", "tl@example.com"],
    ["--demo", "--edition", "hosted", "--demo-user", "not an email"],
    ["--demo", "--edition", "everything"],
])
def test_demo_user_only_with_demo(tmp_path, extra, capsys):
    with pytest.raises(SystemExit) as refused:
        serve(tmp_path, *extra)
    assert refused.value.code == 2
    capsys.readouterr()


def test_demo_user_refused_with_a_config(tmp_path, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--config", str(tmp_path / "config.toml"), "--edition", "hosted"])
    assert refused.value.code == 2
    assert "only go with --demo" in capsys.readouterr().err


def test_serve_demo_hosted_signs_in_as_the_demo_user(tmp_path, demo_serve, monkeypatch, capsys):
    monkeypatch.setattr(access, "Verifier", None)   # the demo never builds a verifier
    assert serve(tmp_path, "--demo", "--edition", "hosted", "--port", "8811", "--no-browser") == 0
    [server] = demo_serve
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8811)
    with TestClient(with_whoami(server.config.app), base_url="http://127.0.0.1:8811") as client:
        assert client.get("/api/test-whoami").json() == {"email": "team.lead@example.com"}, "the default demo user"
        body = client.get("/api/settings").json()
        assert client.get("/api/runs").status_code == 404
        assert client.get("/api/settings", headers={"Host": PUBLIC}).status_code == 403, "no public host"
    assert (body["edition"], body["demo"], body["timezone_effective"]) == ("hosted", True, "America/Chicago")
    assert "signed in as team.lead@example.com" in capsys.readouterr().out


def test_serve_demo_hosted_with_another_demo_user(tmp_path, demo_serve, monkeypatch):
    monkeypatch.setattr(access, "Verifier", None)
    assert serve(tmp_path, "--demo", "--edition", "hosted", "--demo-user", "TL@Example.com", "--port", "8811",
                 "--no-browser") == 0
    added(tmp_path / "demo.db", "tl@example.com")
    with TestClient(with_whoami(demo_serve[0].config.app), base_url="http://127.0.0.1:8811") as client:
        assert client.get("/api/test-whoami").json() == {"email": "tl@example.com"}


def test_serve_demo_hosted_adds_the_demo_team_lead(tmp_path, demo_serve, monkeypatch):
    """The edition's demo has its TL (demo.DEMO_TL, plan Task 6's Eyes scenarios), who is let in. (The PC's demo adds
    nobody: tests/test_demo.py.)"""
    from work_assistant import demo

    monkeypatch.setattr(access, "Verifier", None)
    tl = demo.DEMO_TL[1]
    assert serve(tmp_path, "--demo", "--demo-date", "2026-10-19", "--edition", "hosted", "--demo-user", tl,
                 "--port", "8811", "--no-browser") == 0
    conn = connect(tmp_path / "demo.db")
    try:
        assert people.role(conn, tl) == "tl"
    finally:
        conn.close()
    with TestClient(with_whoami(demo_serve[0].config.app), base_url="http://127.0.0.1:8811") as client:
        assert client.get("/api/me").json()["role"] == "tl", "signed in and known"


def test_serve_demo_hosted_signs_in_as_the_demo_manager(tmp_path, demo_serve, monkeypatch):
    """The edition's demo has its manager too (demo.DEMO_MANAGER, Managers design "The demo"): with `--demo-user
    parker.lane@example.com` it is signed in as Parker Lane, a manager of NASA and APAC (Eyes: hosted-manager.yaml)."""
    from work_assistant import demo

    monkeypatch.setattr(access, "Verifier", None)
    name, email, picked = demo.DEMO_MANAGER
    assert serve(tmp_path, "--demo", "--demo-date", "2026-10-19", "--edition", "hosted", "--demo-user", email,
                 "--port", "8811", "--no-browser") == 0
    with TestClient(with_whoami(demo_serve[0].config.app), base_url="http://127.0.0.1:8811") as client:
        me = client.get("/api/me").json()
    assert (me["email"], me["role"], me["name"], me["regions"]) == (email, "manager", name, list(picked))
    assert (name, email, picked) == ("Parker Lane", "parker.lane@example.com", ("NASA", "APAC"))


def test_serve_demo_full_edition_is_the_usual_demo(tmp_path, demo_serve):
    assert serve(tmp_path, "--demo", "--edition", "full", "--port", "8811", "--no-browser") == 0
    with TestClient(with_whoami(demo_serve[0].config.app), base_url="http://127.0.0.1:8811") as client:
        assert client.get("/api/test-whoami").json() == {"email": None}
        assert client.get("/api/runs").status_code == 200
        assert client.get("/api/settings").json()["edition"] == "full"


# -- the page -------------------------------------------------------------------------------------------------


def first_read_fails(tmp_path, settings_answer):
    responses = page_responses(api_for(tmp_path), "/api/activity")
    responses["GET /api/settings"] = settings_answer
    return run_page("", responses, HELPERS + """
run(async function () {
  var out = { hash: location.hash, shown: shownLinks(), statusReads: statusReads(),
              error: all(screenEl, function (e) { return e.classList.contains("error"); }).map(function (e) {
                return { text: e.textContent, role: e.getAttribute("role") }; }),
              reads: requests.map(function (r) { return r.url; }) };
  await go("#/overview");
  out.after = { screen: screenEl.textContent, statusReads: statusReads(), reads: requests.length };
  return out;
});
""")


def test_page_shows_the_sign_in_refusal_plainly(tmp_path):
    out = first_read_fails(tmp_path, answer(SIGN_IN, 403))
    assert out["error"] == [{"text": "Sign in through the company link.", "role": "alert"}]
    assert out["hash"] == "", "not sent to the Call QA overview"
    assert out["shown"] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"], "Call QA stays hidden"
    assert out["statusReads"] == 0 and out["reads"] == ["/api/settings"], "no /api/status polling, no other reads"
    assert out["after"] == {"screen": "Sign in through the company link.", "statusReads": 0, "reads": 1}


def test_page_shows_a_failed_first_read_instead_of_the_full_edition(tmp_path):
    out = first_read_fails(tmp_path, answer({"detail": "Something broke."}, 500))
    assert out["error"] == [{"text": "Something broke.", "role": "alert"}]
    assert out["hash"] == "" and out["statusReads"] == 0
    assert out["shown"] == ["Team", "Activity", "Trends", "1:1 Prep", "Settings"]


def test_page_shows_an_unreachable_app(tmp_path):
    """The first read never gets an answer (the fetch itself fails, as when the app or the network is down)."""
    unreachable = """
var standInFetch = globalThis.fetch;
globalThis.fetch = function (url, options) {
  if (url === "/api/settings") { requests.push({ url: url }); return Promise.reject(new TypeError("Failed to fetch")); }
  return standInFetch(url, options);
};
"""
    assert PAGE_JS.count("require(APP_JS);") == 1
    script = (node_prelude() + 'var START_HASH = "";\n'
              + f"var RESPONSES = {json.dumps(page_responses(api_for(tmp_path)))};\n"
              + f"var APP_JS = {json.dumps(str(STATIC / 'app.js'))};\n"
              + PAGE_JS.replace("require(APP_JS);", unreachable + "require(APP_JS);") + HELPERS + """
run(async function () {
  return { error: all(screenEl, function (e) { return e.classList.contains("error"); })
             .map(function (e) { return e.textContent; }),
           shown: shownLinks(), statusReads: statusReads() };
});
""")
    out = run_node(script)
    assert out == {"error": ["The app couldn't be reached. Reload the page to try again."],
                   "shown": ["Team", "Activity", "Trends", "1:1 Prep", "Settings"], "statusReads": 0}
