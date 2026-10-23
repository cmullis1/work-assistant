"""Checking Cloudflare Access sign-in tokens (hosted Team activity design §3, plan Task 2): the signature against the
team's certificates, the audience, the expiry and the issuer, and re-reading the certificates when Cloudflare rotates
its keys (Review Focus 2).

Tokens are signed here with RSA keys made for the tests, and the certificates come from a stand-in `fetch` that
answers with those keys: nothing here reaches Cloudflare. Other test files import the helpers below.
"""
from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from work_assistant import access

DOMAIN = "example.cloudflareaccess.com"
AUD = "aud-tag-for-tests"
ISSUER = f"https://{DOMAIN}"
CERTS_URL = f"https://{DOMAIN}/cdn-cgi/access/certs"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY_A, KEY_B, KEY_OTHER = new_key(), new_key(), new_key()


def jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict:
    """The public half of `private_key` as Cloudflare lists it at the certificate address."""
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**data, "kid": kid, "alg": "RS256", "use": "sig"}


def jwks(*keys: tuple[rsa.RSAPrivateKey, str]) -> dict:
    return {"keys": [jwk(key, kid) for key, kid in keys]}


CERTS_A = jwks((KEY_A, "key-a"))


def token(email: str | None = "pat@example.com", *, key: rsa.RSAPrivateKey = KEY_A, kid: str | None = "key-a",
          **claims) -> str:
    """A token as Cloudflare Access signs it. A claim given as None is left out."""
    now = int(time.time())
    payload = {"aud": [AUD], "email": email, "exp": now + 3600, "iat": now, "nbf": now, "iss": ISSUER,
               "sub": "7335d417-61da-459d-899c-0a01c76a2f94", "type": "app", **claims}
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, algorithm="RS256", headers=headers)


class Certs:
    """A stand-in for reading the certificate address: each read answers with the next answer (the last one
    repeats), a JWKS or an exception to raise. Every address read is recorded."""

    def __init__(self, *answers):
        self.answers = list(answers) or [CERTS_A]
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    """A stand-in for the Verifier's clock: it moves only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = access.REREAD_SECONDS) -> None:
        self.now += seconds


def verifier(certs: Certs | None = None, clock: Clock | None = None) -> access.Verifier:
    return access.Verifier(DOMAIN, AUD, fetch=certs if certs is not None else Certs(),
                           clock=clock if clock is not None else Clock())


def denied(check: access.Verifier, value: str) -> str:
    with pytest.raises(access.Denied) as refused:
        check.email(value)
    return str(refused.value)


# -- a good token -------------------------------------------------------------------------------------------


def test_valid_token_gives_lowercased_email():
    certs = Certs()
    check = verifier(certs)
    assert check.email(token("Pat.Smith@Example.COM")) == "pat.smith@example.com"
    assert certs.urls == [CERTS_URL], "the team's own certificate address"
    assert check.email(token("lee@example.com")) == "lee@example.com"
    assert certs.urls == [CERTS_URL], "the keys are cached: read once"


def test_expiry_allows_a_minute_of_clock_difference():
    check = verifier()
    assert check.email(token(exp=int(time.time()) - 30)) == "pat@example.com"


# -- refused tokens: one test per case ------------------------------------------------------------------------


def test_wrong_audience_denied():
    assert "audience" in denied(verifier(), token(aud=["another-access-app"]))


def test_expired_denied():
    assert "expired" in denied(verifier(), token(exp=int(time.time()) - 61))


def test_wrong_issuer_denied():
    assert "issuer" in denied(verifier(), token(iss="https://other.cloudflareaccess.com"))


def test_bad_signature_denied():
    """Signed with another key under a key ID Cloudflare does list."""
    assert "signature" in denied(verifier(), token(key=KEY_OTHER))


def test_missing_token_denied():
    certs = Certs()
    assert denied(verifier(certs), "") == "no sign-in token"
    assert certs.urls == [], "nothing to check, so the certificates aren't read"


def test_garbage_denied():
    assert denied(verifier(), "not-a-token") == "not a sign-in token"


def test_token_without_a_key_id_denied():
    assert denied(verifier(), token(kid=None)) == "the token names no signing key"


def test_token_without_an_email_denied():
    assert denied(verifier(), token(email=None)) == "the token carries no email"


def test_token_without_an_expiry_denied():
    assert "exp" in denied(verifier(), token(exp=None))


def test_token_signed_with_a_shared_secret_denied():
    """Only RS256: a token "signed" with HS256 (for example with the public key as the secret) is refused."""
    forged = jwt.encode({"aud": [AUD], "email": "pat@example.com", "exp": int(time.time()) + 3600, "iss": ISSUER},
                        "a-shared-secret-long-enough-for-hs256", algorithm="HS256", headers={"kid": "key-a"})
    assert "algorithm" in denied(verifier(), forged)


# -- Cloudflare rotates its keys (Review Focus 2) -------------------------------------------------------------


def test_unknown_kid_refetches_once_then_accepts():
    certs = Certs(CERTS_A, jwks((KEY_A, "key-a"), (KEY_B, "key-b")))
    clock = Clock()
    check = verifier(certs, clock)
    assert check.email(token()) == "pat@example.com"
    assert len(certs.urls) == 1
    clock.advance()
    assert check.email(token("lee@example.com", key=KEY_B, kid="key-b")) == "lee@example.com"
    assert len(certs.urls) == 2, "an unknown key ID re-reads the certificates once"
    assert check.email(token(key=KEY_B, kid="key-b")) == "pat@example.com"
    assert check.email(token()) == "pat@example.com"
    assert len(certs.urls) == 2, "the new keys are cached"


def test_unknown_kid_after_refetch_denied():
    certs = Certs()
    clock = Clock()
    check = verifier(certs, clock)
    assert check.email(token()) == "pat@example.com"
    clock.advance()
    assert denied(check, token(key=KEY_B, kid="key-b")) == "the token's signing key isn't one of Cloudflare's"
    assert len(certs.urls) == 2, "one re-read, then refused"
    clock.advance()
    denied(check, token(key=KEY_B, kid="key-b"))
    assert len(certs.urls) == 3, "each such token, once the last read is old enough, gets exactly one re-read"
    assert check.email(token()) == "pat@example.com"
    assert len(certs.urls) == 3


def test_rotated_out_key_is_dropped():
    certs = Certs(CERTS_A, jwks((KEY_B, "key-b")))
    clock = Clock()
    check = verifier(certs, clock)
    check.email(token())
    clock.advance()
    check.email(token(key=KEY_B, kid="key-b"))
    assert denied(check, token()) == "the token's signing key isn't one of Cloudflare's", \
        "a key Cloudflare no longer lists stops working"


# -- the certificates can't be read ---------------------------------------------------------------------------


def test_certificate_fetch_failure_is_unreachable():
    certs = Certs(httpx.ConnectError("no route to host"), CERTS_A)
    clock = Clock()
    check = verifier(certs, clock)
    with pytest.raises(access.Unreachable) as unreachable:
        check.email(token())
    assert CERTS_URL in str(unreachable.value) and "ConnectError" in str(unreachable.value)
    with pytest.raises(access.Unreachable) as again:
        check.email(token())
    assert str(again.value) == str(unreachable.value) and len(certs.urls) == 1, \
        "within the wait the failure is given again, without a read"
    clock.advance()
    assert check.email(token()) == "pat@example.com", "after it, the next request reads them again"


@pytest.mark.parametrize("body", [{"keys": []}, {"public_cert": "x"}, ["keys"], {"keys": [{"kty": "RSA"}]}])
def test_certificates_without_usable_keys_are_unreachable(body):
    with pytest.raises(access.Unreachable):
        verifier(Certs(body)).email(token())


def test_failed_reread_keeps_the_cached_keys():
    certs = Certs(CERTS_A, httpx.ReadTimeout("timed out"))
    clock = Clock()
    check = verifier(certs, clock)
    check.email(token())
    clock.advance()
    with pytest.raises(access.Unreachable):
        check.email(token(key=KEY_B, kid="key-b"))
    assert check.email(token()) == "pat@example.com", "the keys read before still work"
    assert len(certs.urls) == 2


def test_unknown_kids_reread_at_most_once_per_wait():
    """R7: made-up key IDs can't make the app read the certificates again and again. Within REREAD_SECONDS of a
    read, an unknown key ID is refused without a read; known keys keep working throughout."""
    certs = Certs()
    clock = Clock()
    check = verifier(certs, clock)
    assert check.email(token()) == "pat@example.com"
    for made_up in ("made-up-1", "made-up-2", "made-up-3"):
        assert denied(check, token(key=KEY_B, kid=made_up)) == "the token's signing key isn't one of Cloudflare's"
    assert len(certs.urls) == 1, "no read within the wait"
    clock.advance(access.REREAD_SECONDS - 0.5)
    denied(check, token(key=KEY_B, kid="made-up-4"))
    assert len(certs.urls) == 1
    clock.advance(0.5)
    denied(check, token(key=KEY_B, kid="made-up-5"))
    assert len(certs.urls) == 2, "once the wait is over, one more read"
    denied(check, token(key=KEY_B, kid="made-up-6"))
    assert len(certs.urls) == 2
    assert check.email(token()) == "pat@example.com"


class SlowCerts:
    """Certificates that answer CERTS_A at once the first time, then block each later read until `release` is set
    (`reading` says one has started) and answer `then`, a JWKS or an exception to raise. Reads are counted."""

    def __init__(self, then=None):
        import threading

        self.reading, self.release = threading.Event(), threading.Event()
        self.then = then if then is not None else jwks((KEY_A, "key-a"), (KEY_B, "key-b"), (KEY_OTHER, "key-c"))
        self.reads = 0

    def __call__(self, url):
        self.reads += 1
        if self.reads == 1:
            return CERTS_A
        self.reading.set()
        self.release.wait(10)
        if isinstance(self.then, Exception):
            raise self.then
        return self.then


def in_thread(check_one):
    """Start `check_one()` in a thread. Returns the thread and a dict that gets "value": what it gave, or the
    exception it raised."""
    import threading

    answer = {}

    def run():
        try:
            answer["value"] = check_one()
        except Exception as exc:   # noqa: BLE001 - handed back to the test
            answer["value"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, answer


def at_once(check_one):
    """What `check_one()` gives (or raises, as an exception object), or "held up" if it takes over a second."""
    thread, answer = in_thread(check_one)
    thread.join(1)
    return answer.get("value", "held up")


def rotating(certs: SlowCerts) -> access.Verifier:
    """A verifier that has read CERTS_A, with its wait between reads over: the next unknown key ID reads again."""
    clock = Clock()
    check = access.Verifier(DOMAIN, AUD, fetch=certs, clock=clock)
    assert check.email(token()) == "pat@example.com"
    clock.advance()
    return check


def test_a_slow_reread_holds_up_no_token_with_a_kept_key():
    """R7: the certificates are read outside the lock. While one request waits on a slow Cloudflare, a token with a
    kept key is checked at once. Another unknown key ID waits for that read (R9), starts none of its own, and is
    refused once the read shows it isn't one of Cloudflare's."""
    certs = SlowCerts()
    check = rotating(certs)
    first, rotated = in_thread(lambda: check.email(token(kid="key-b", key=KEY_B)))
    assert certs.reading.wait(10), "the re-read has started"
    made_up, refused = in_thread(lambda: check.email(token(key=KEY_B, kid="made-up")))
    try:
        assert at_once(lambda: check.email(token("lee@example.com"))) == "lee@example.com", "a kept key doesn't wait"
        made_up.join(0.3)
        assert "value" not in refused, "the made-up key ID waits for the read in flight"
    finally:
        certs.release.set()
        first.join(10)
        made_up.join(10)
    assert rotated == {"value": "pat@example.com"}, "the re-read, once done, lets the rotated key in"
    assert isinstance(refused["value"], access.Denied), refused
    assert str(refused["value"]) == "the token's signing key isn't one of Cloudflare's"
    assert certs.reads == 2, "one re-read for both"


def test_a_new_key_arriving_during_a_read_waits_for_it():
    """R9: a second request whose key is new too, arriving while the first one's read is in flight, waits for that
    read and is let in, instead of being refused; nothing is read twice."""
    certs = SlowCerts()
    check = rotating(certs)
    first, one = in_thread(lambda: check.email(token("lee@example.com", kid="key-b", key=KEY_B)))
    assert certs.reading.wait(10)
    second, two = in_thread(lambda: check.email(token("kim@example.com", kid="key-c", key=KEY_OTHER)))
    second.join(0.3)
    assert "value" not in two, "it waits rather than being refused"
    certs.release.set()
    first.join(10)
    second.join(10)
    assert (one, two) == ({"value": "lee@example.com"}, {"value": "kim@example.com"})
    assert certs.reads == 2
    assert check.email(token(kid="key-c", key=KEY_OTHER)) == "pat@example.com" and certs.reads == 2, "kept"


def test_a_failed_read_in_flight_is_unreachable_for_those_waiting():
    """Review Focus 2 holds for a waiting request: if the read it waits on fails, the answer is Unreachable (503),
    never a sign-in."""
    certs = SlowCerts(then=httpx.ConnectError("no route to host"))
    check = rotating(certs)
    first, one = in_thread(lambda: check.email(token(kid="key-b", key=KEY_B)))
    assert certs.reading.wait(10)
    second, two = in_thread(lambda: check.email(token(kid="key-c", key=KEY_OTHER)))
    second.join(0.3)
    certs.release.set()
    first.join(10)
    second.join(10)
    assert isinstance(one["value"], access.Unreachable) and isinstance(two["value"], access.Unreachable)
    assert str(two["value"]) == str(one["value"]) and "ConnectError" in str(two["value"])
    assert certs.reads == 2
    assert check.email(token()) == "pat@example.com", "the keys read before still work"


def test_the_wait_on_a_read_in_flight_is_bounded(monkeypatch):
    """A request waits on a read in flight for READ_WAIT_SECONDS at most; past that it is answered Unreachable
    (503), never let in. New reads still start at most once every REREAD_SECONDS."""
    monkeypatch.setattr(access, "READ_WAIT_SECONDS", 0.2)
    certs = SlowCerts()
    check = rotating(certs)
    first, one = in_thread(lambda: check.email(token(kid="key-b", key=KEY_B)))
    assert certs.reading.wait(10)
    try:
        waited = at_once(lambda: check.email(token(kid="key-c", key=KEY_OTHER)))
        assert isinstance(waited, access.Unreachable), waited
        assert certs.reads == 2, "giving up starts no read of its own"
    finally:
        certs.release.set()
        first.join(10)
    assert one == {"value": "pat@example.com"}
    assert check.email(token(kid="key-c", key=KEY_OTHER)) == "pat@example.com", "the read, once done, is kept"
    assert certs.reads == 2


def test_default_fetch_reads_the_address_with_a_timeout(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return httpx.Response(200, json=CERTS_A, request=httpx.Request("GET", url))

    monkeypatch.setattr(access, "fetch_certs", access.read_certs)   # undo the tests' blocker for this test
    monkeypatch.setattr(access.httpx, "get", fake_get)
    assert access.Verifier(DOMAIN, AUD).email(token()) == "pat@example.com"
    assert calls == [(CERTS_URL, access.CERTS_TIMEOUT_SECONDS)]


def test_certificate_reads_are_blocked_in_tests():
    with pytest.raises(access.Unreachable):
        access.Verifier(DOMAIN, AUD).email(token())
