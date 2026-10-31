"""Sign-in with an outside provider: Google, or the fake that stands in for it in
development and tests (CAUSEMATCH_FAKE_OAUTH=1).

Authorization code flow with PKCE (S256), `state` and `nonce`. /auth/google/start keeps
all three in a short-lived signed cookie (oauth_cookie, never the session); the callback
reads and deletes it, so a callback URL works once. The ID token is verified here, against
Google's published keys: signature, iss, aud, exp and nonce.

fasthtml.oauth is not used: it derives the redirect URI from the request's Host header,
sends no state, PKCE or nonce, and never verifies the ID token.
"""

import base64
import functools
import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from itsdangerous import BadData, URLSafeTimedSerializer

from app import photos
from app.auth import session_secret
from app.config import fakes_allowed, google_oauth, is_production

TIMEOUT = 10  # seconds, for every call to the provider
COOKIE_MAX_AGE = 10 * 60  # seconds from start to callback
PICTURE_HOST = ".googleusercontent.com"  # where Google serves profile pictures


class AuthError(Exception):
    """A check failed (state, nonce, signature, a used code, an unverified email): answer
    400 and sign nobody in. str(e) is safe to show."""


class Unavailable(Exception):
    """The provider couldn't be reached or answered with an error: try again later."""


@dataclass(frozen=True)
class Claims:
    provider: str
    subject: str
    email: str
    email_verified: bool
    given_name: str
    family_name: str
    picture_url: str  # Google only; imported on opt-in (fetch_picture)


# --- the oauth cookie --------------------------------------------------------------------


def oauth_cookie(env=os.environ):
    """The flow cookie's name: __Host- in production, as app/auth.py session_cookie."""
    return "__Host-cm_oauth" if is_production(env) else "cm_oauth"


@functools.cache
def _serializer():
    return URLSafeTimedSerializer(session_secret(os.environ), salt="oauth")


def new_flow():
    """(state, nonce, PKCE verifier), each 256 random bits."""
    return tuple(secrets.token_urlsafe(32) for _ in range(3))


def challenge(verifier):
    """PKCE S256: base64url(SHA-256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def seal(provider, state, nonce, verifier):
    return _serializer().dumps({"p": provider, "s": state, "n": nonce, "v": verifier})


def unseal(value, provider, state):
    """(nonce, verifier) from the cookie when it is ours, under 10 minutes old, for this
    provider and this state; else AuthError."""
    if not value or not state:
        raise AuthError("missing")
    try:
        flow = _serializer().loads(value, max_age=COOKIE_MAX_AGE)
    except BadData:
        raise AuthError("bad or expired cookie") from None
    if not isinstance(flow, dict) or flow.get("p") != provider:
        raise AuthError("wrong provider")
    if not hmac.compare_digest(str(flow.get("s", "")), state):
        raise AuthError("state mismatch")
    return str(flow.get("n", "")), str(flow.get("v", ""))


# --- Google ------------------------------------------------------------------------------

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
# Google's signing keys, cached (they rotate every few weeks); tests replace it
JWKS = jwt.PyJWKClient(
    "https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, timeout=TIMEOUT
)


def verify_id_token(token, client_id, nonce, jwks=None):
    """The ID token's claims once its signature (RS256, a key from Google's JWKS), iss,
    aud, exp and nonce check out; AuthError otherwise, Unavailable when the keys can't be
    fetched."""
    try:
        key = (jwks or JWKS).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=GOOGLE_ISSUERS,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWKClientConnectionError:
        raise Unavailable("keys") from None
    except jwt.PyJWTError as e:
        raise AuthError(type(e).__name__) from None
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AuthError("nonce mismatch")
    return claims


def _claims(provider, c):
    verified = c.get("email_verified")
    return Claims(
        provider=provider,
        subject=str(c.get("sub") or ""),
        email=str(c.get("email") or ""),
        email_verified=verified is True or verified == "true",
        given_name=str(c.get("given_name") or ""),
        family_name=str(c.get("family_name") or ""),
        picture_url=str(c.get("picture") or ""),
    )


class Google:
    name = "google"

    def __init__(self, client_id, secret):
        self.client_id, self.secret = client_id, secret

    def authorize_url(self, redirect_uri, state, nonce, code_challenge):
        return f"{GOOGLE_AUTHORIZE}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )

    def finish(self, code, verifier, redirect_uri, nonce):
        """Exchange the code (with the PKCE verifier) and verify the ID token: Claims."""
        try:
            r = httpx.post(
                GOOGLE_TOKEN,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            token = r.json()["id_token"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise Unavailable(type(e).__name__) from None
        return _claims(self.name, verify_id_token(token, self.client_id, nonce))


# --- the fake ----------------------------------------------------------------------------


class Fake:
    """Stands in for Google, driving the real start and callback: its authorize URL is a
    local consent page (app/routes/oauth.py) whose form mints a one-time code bound to the
    flow's nonce and PKCE challenge. Nothing leaves the app."""

    name = "google"  # it serves Google's routes

    def __init__(self):
        self._codes = {}  # code -> (Claims, nonce, code_challenge)
        self._lock = threading.Lock()

    def authorize_url(self, redirect_uri, state, nonce, code_challenge):
        return "/auth/fake/consent?" + urlencode(
            {"state": state, "nonce": nonce, "code_challenge": code_challenge}
        )

    def mint(self, claims, nonce, code_challenge):
        code = secrets.token_urlsafe(16)
        with self._lock:
            if len(self._codes) >= 1000:  # unused codes of a long dev session
                self._codes.pop(next(iter(self._codes)))
            self._codes[code] = (claims, nonce, code_challenge)
        return code

    def finish(self, code, verifier, redirect_uri, nonce):
        with self._lock:
            found = self._codes.pop(code, None)  # once
        if found is None:
            raise AuthError("unknown or used code")
        claims, bound_nonce, code_challenge = found
        if not hmac.compare_digest(challenge(verifier), code_challenge):
            raise AuthError("PKCE mismatch")
        if not nonce or not hmac.compare_digest(bound_nonce, nonce):
            raise AuthError("nonce mismatch")
        return claims


_fake = Fake()


def fake_enabled(env=os.environ):
    """CAUSEMATCH_FAKE_OAUTH=1: development, test and preview only, as app/notify.py's fakes;
    RuntimeError anywhere else, so production with it set refuses to start (main.py)."""
    if env.get("CAUSEMATCH_FAKE_OAUTH") != "1":
        return False
    if not fakes_allowed(env):
        raise RuntimeError(
            "CAUSEMATCH_FAKE_OAUTH=1 needs CAUSEMATCH_ENV=development, test or preview"
        )
    return True


def get_google(env=os.environ):
    """The provider behind the Google button: the fake, Google, or None (no button)."""
    if fake_enabled(env):
        return _fake
    if creds := google_oauth(env):
        return Google(*creds)
    return None


# --- the profile picture -----------------------------------------------------------------


def fetch_picture(url):
    """The bytes of a Google profile picture, at most photos.MAX_UPLOAD of them. Only https
    on a googleusercontent.com host, no redirects followed. photos.BadPhoto for any other
    URL or a picture too large; httpx.HTTPError when it can't be fetched."""
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host.endswith(PICTURE_HOST) or u.username:
        raise photos.BadPhoto("not a Google picture URL")
    with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=False) as r:
        r.raise_for_status()
        raw = bytearray()
        for chunk in r.iter_bytes():
            raw += chunk
            if len(raw) > photos.MAX_UPLOAD:
                raise photos.BadPhoto("too large")
    return bytes(raw)
