"""Cloudflare Access sign-in (hosted Team activity design §3): the hosted copy trusts a request only when it carries
a token Cloudflare Access signed for this app, and the signed-in person is that token's email.

`Verifier` checks a token's RS256 signature against the keys the Cloudflare team publishes at
`https://{team_domain}/cdn-cgi/access/certs`, and its audience (the Access application's AUD tag), expiry and issuer.
The keys are read once and kept. When Cloudflare rotates them, a token names a key ID that isn't kept yet, and the
keys are read again once for it, at most once every REREAD_SECONDS: a made-up key ID can't make the app read them
over and over. The read happens outside the lock, so a slow Cloudflare never holds up tokens with a kept key. A
token with a new key ID that arrives while a read is in flight waits for that read (READ_WAIT_SECONDS at most) and is
then let in or refused by what it found, rather than refused before the new keys are known. The team domain and AUD
tag are config values, not secrets; this module never sees a password or key of anyone's.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, NoReturn

import httpx
import jwt

LEEWAY_SECONDS = 60          # how far the VM's clock and Cloudflare's may differ
CERTS_TIMEOUT_SECONDS = 10
REREAD_SECONDS = 5           # the certificates are read at most once in this long
READ_WAIT_SECONDS = CERTS_TIMEOUT_SECONDS + 5   # the longest a token waits on a read in flight; then Unreachable


class Denied(Exception):
    """The request carries no token, or one Cloudflare didn't sign for this app or that has expired. The message
    says why, in a few words, for the log; it never quotes the token."""


class Unreachable(Exception):
    """Cloudflare's certificate address couldn't be read, so no token can be checked."""


def read_certs(url: str) -> dict:
    """Read the certificate address (the JSON key set)."""
    response = httpx.get(url, timeout=CERTS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


# What a Verifier without its own `fetch` reads the certificates with. Tests replace it, so none reaches Cloudflare.
fetch_certs: Callable[[str], dict] = read_certs


class Verifier:
    """Checks Cloudflare Access tokens for one Access application: `email(token)` returns the signed-in person's
    lowercased email, or raises Denied, or Unreachable when the certificates can't be read. Safe to share between
    threads. `clock` (seconds, only ever compared) times the reads; tests pass their own."""

    def __init__(self, team_domain: str, aud: str, fetch: Callable[[str], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.issuer = f"https://{team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.aud = aud
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, Any] = {}       # key ID -> public key, as last read
        self._read_at: float | None = None    # when the last read started
        self._read_error: str | None = None   # why the last read failed; None once one succeeds
        self._reading: threading.Event | None = None   # the read in flight, set when it finishes; None when none is
        self._lock = threading.Lock()

    def email(self, token: str) -> str:
        if not token:
            raise Denied("no sign-in token")
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise Denied("not a sign-in token") from None
        if not isinstance(kid, str) or not kid:
            raise Denied("the token names no signing key")
        key = self._key(kid)
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                                leeway=LEEWAY_SECONDS, options={"require": ["exp", "iss", "aud"]})
        except jwt.ExpiredSignatureError:
            raise Denied("the sign-in has expired") from None
        except jwt.InvalidAudienceError:
            raise Denied("the token has the wrong audience (another Access application)") from None
        except jwt.InvalidIssuerError:
            raise Denied("the token has the wrong issuer (another Cloudflare team)") from None
        except jwt.InvalidSignatureError:
            raise Denied("the signature doesn't match Cloudflare's key") from None
        except jwt.MissingRequiredClaimError as exc:
            raise Denied(f"the token has no {exc.claim} claim") from None
        except jwt.InvalidAlgorithmError:
            raise Denied("the token isn't signed with RS256 (wrong algorithm)") from None
        except jwt.PyJWTError as exc:
            raise Denied(f"the token isn't valid ({type(exc).__name__})") from None
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise Denied("the token carries no email")
        return email.strip().lower()

    def _key(self, kid: str) -> Any:
        """The public key with this ID: from the kept keys, or after reading the certificates once more. While a
        read is in flight, an unknown key ID waits for it (READ_WAIT_SECONDS at most, then Unreachable) and is
        judged by what it found. Otherwise, within REREAD_SECONDS of the last read nothing is read: an unknown key ID
        is refused (Denied), or, if that read failed, Unreachable again. Only the bookkeeping holds the lock, never
        the network read or the wait."""
        with self._lock:
            if kid in self._keys:
                return self._keys[kid]
            in_flight = self._reading
            if in_flight is None:
                now = self._clock()
                if self._read_at is not None and now - self._read_at < REREAD_SECONDS:
                    self._refuse()
                self._read_at = now
                self._reading = done = threading.Event()
        if in_flight is not None:
            if not in_flight.wait(READ_WAIT_SECONDS):
                raise Unreachable(f"Cloudflare's certificates at {self.certs_url} took over {READ_WAIT_SECONDS} "
                                  "seconds to read")
            with self._lock:
                return self._keys[kid] if kid in self._keys else self._refuse()
        try:
            keys = self._read()
        except Unreachable as exc:
            with self._lock:
                self._read_error = str(exc)   # the keys read before stay
            raise
        else:
            with self._lock:
                self._keys, self._read_error = keys, None
        finally:
            with self._lock:
                self._reading = None
            done.set()
        if kid not in keys:
            raise Denied("the token's signing key isn't one of Cloudflare's")
        return keys[kid]

    def _refuse(self) -> NoReturn:
        """An unknown key ID once the last read is known (the caller holds the lock): Unreachable if that read
        failed, else Denied."""
        if self._read_error is not None:
            raise Unreachable(self._read_error)
        raise Denied("the token's signing key isn't one of Cloudflare's")

    def _read(self) -> dict[str, Any]:
        fetch = self._fetch if self._fetch is not None else fetch_certs
        try:
            listed = fetch(self.certs_url)["keys"]
            keys = {jwk["kid"]: jwt.PyJWK(jwk, algorithm="RS256").key
                    for jwk in listed if jwk.get("kty") == "RSA" and jwk.get("kid")}
        except Exception as exc:
            raise Unreachable(f"Cloudflare's certificates at {self.certs_url} couldn't be read "
                              f"({type(exc).__name__})") from None
        if not keys:
            raise Unreachable(f"Cloudflare's certificates at {self.certs_url} list no signing keys")
        return keys
