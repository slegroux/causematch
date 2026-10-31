"""Sign-in with an outside provider: Google, Apple, or the fake that stands in for either
in development and tests (CAUSEMATCH_FAKE_OAUTH=1).

Authorization code flow with `state` and `nonce`, and PKCE (S256) for Google. Apple ignores
PKCE: its proof is the client secret, a JWT signed with our key. /auth/{provider}/start
keeps all three in a short-lived signed cookie (oauth_cookie, never the session); the
callback reads it and asks the browser to delete it. Nothing server-side records a used
state: a client that keeps the cookie can present it again for 10 minutes, and what stops
a replay is the provider's one-time code (the fake's is one-time too). The ID token is
verified here, against the provider's published keys: signature, iss, aud, exp and nonce.

fasthtml.oauth is not used: it derives the redirect URI from the request's Host header,
sends no state, PKCE or nonce, and never verifies the ID token.
"""

import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from itsdangerous import BadData, URLSafeTimedSerializer

from app import photos
from app.auth import session_secret
from app.config import apple_oauth, fakes_allowed, google_oauth, is_production

TIMEOUT = 10  # seconds, for every call to the provider
LABELS = {
    "apple": "Apple",
    "google": "Google",
}  # the providers, in the login page's order
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
    hosted_domain: str = ""  # Google's hd: set only for a Workspace account


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


# Why the flow was started: a volunteer signing in (/login), a coordinator or admin
# signing in (/org/login), or a signed-in volunteer connecting a provider (Me)
INTENTS = ("volunteer", "staff", "link")


@dataclass(frozen=True)
class Flow:
    nonce: str
    verifier: str
    intent: str
    uid: int | None  # link only: the volunteer who pressed Connect
    sv: int | None  # link only: their session version then


def seal(provider, state, nonce, verifier, intent="volunteer", uid=None, sv=None):
    flow = {"p": provider, "s": state, "n": nonce, "v": verifier, "i": intent}
    if intent == "link":
        flow |= {"u": uid, "sv": sv}
    return _serializer().dumps(flow)


def unseal(value, provider, state):
    """The Flow in the cookie when it is ours, under 10 minutes old, for this provider and
    this state; else AuthError. Not single use on its own: no used state is recorded, so
    the same cookie and state pass again until they expire. The callback's cookie deletion
    (in a cooperating browser) and the provider's one-time code make a callback work once."""
    if not value or not state:
        raise AuthError("missing")
    try:
        flow = _serializer().loads(value, max_age=COOKIE_MAX_AGE)
    except BadData:
        raise AuthError("bad or expired cookie") from None
    if not isinstance(flow, dict) or flow.get("p") != provider:
        raise AuthError("wrong provider")
    # bytes: compare_digest refuses a str with non-ASCII characters (a 500, not a 400)
    if not hmac.compare_digest(str(flow.get("s", "")).encode(), state.encode()):
        raise AuthError("state mismatch")
    intent, uid, sv = flow.get("i"), flow.get("u"), flow.get("sv")
    if intent not in INTENTS:
        raise AuthError("bad intent")
    if intent == "link" and not (type(uid) is int and type(sv) is int):
        raise AuthError("bad link")
    if intent != "link":
        uid = sv = None
    return Flow(str(flow.get("n", "")), str(flow.get("v", "")), intent, uid, sv)


# --- Google ------------------------------------------------------------------------------

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
# Google's signing keys, cached (they rotate every few weeks); tests replace it
JWKS = jwt.PyJWKClient(
    "https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, timeout=TIMEOUT
)


def verify_id_token(token, client_id, nonce, jwks=None, issuers=GOOGLE_ISSUERS):
    """The ID token's claims once its signature (RS256, a key from the provider's JWKS:
    Google's by default), iss, aud, exp and nonce check out; AuthError otherwise,
    Unavailable when the keys can't be fetched."""
    try:
        key = (jwks or JWKS).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuers,
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
        hosted_domain=str(c.get("hd") or ""),
    )


GOOGLE_HOSTED = ("gmail.com", "googlemail.com")
APPLE_HOSTED = ("icloud.com", "me.com", "mac.com")


def authoritative(claims):
    """True when the provider hosts this mailbox, so a verified email means the person
    controls it now, as the emailed link proves: Gmail, a Google Workspace account whose
    hd is the email's domain, or an iCloud address. Elsewhere a provider keeps an address
    verified long after the mailbox has moved on to someone else."""
    domain = claims.email.rpartition("@")[2].lower()
    if claims.provider == "google":
        hd = claims.hosted_domain.lower()
        return domain in GOOGLE_HOSTED or bool(hd and hd == domain)
    if claims.provider == "apple":
        return domain in APPLE_HOSTED
    return False


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


# --- Apple -------------------------------------------------------------------------------

APPLE_AUTHORIZE = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN = "https://appleid.apple.com/auth/token"
APPLE_ISSUER = "https://appleid.apple.com"  # also the client secret's audience
APPLE_ISSUERS = [APPLE_ISSUER]
CLIENT_SECRET_TTL = (
    24 * 3600
)  # seconds; Apple allows up to 6 months, one is minted per call
# Apple's signing keys, cached as Google's are; tests replace it
APPLE_JWKS = jwt.PyJWKClient(
    "https://appleid.apple.com/auth/keys", cache_keys=True, timeout=TIMEOUT
)


class Apple:
    name = "apple"

    def __init__(self, client_id, team_id, key_id, private_key_pem):
        """RuntimeError (main.py refuses to start) unless the key is an EC P-256 private
        key, as the .p8 file Apple issues is. The error never quotes the key."""
        self.client_id, self.team_id, self.key_id = client_id, team_id, key_id
        try:
            key = serialization.load_pem_private_key(private_key_pem.encode(), None)
        except (ValueError, TypeError):
            key = None
        p256 = isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(
            key.curve, ec.SECP256R1
        )
        if not p256:
            raise RuntimeError(
                "APPLE_PRIVATE_KEY must be Apple's .p8 key (EC P-256 PEM)"
            )
        self._key = key

    def authorize_url(self, redirect_uri, state, nonce, code_challenge):
        """Apple's consent page; it posts back cross-site (form_post). No PKCE."""
        return f"{APPLE_AUTHORIZE}?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "form_post",
                "scope": "name email",
                "state": state,
                "nonce": nonce,
            }
        )

    def client_secret(self, now=None):
        """The ES256 JWT Apple takes as the client secret, valid CLIENT_SECRET_TTL."""
        now = int(now or time.time())
        return jwt.encode(
            {
                "iss": self.team_id,
                "sub": self.client_id,
                "aud": APPLE_ISSUER,
                "iat": now,
                "exp": now + CLIENT_SECRET_TTL,
            },
            self._key,
            algorithm="ES256",
            headers={"kid": self.key_id},
        )

    def finish(self, code, verifier, redirect_uri, nonce):
        """Exchange the code (the verifier is unused) and verify the ID token: Claims."""
        try:
            r = httpx.post(
                APPLE_TOKEN,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret(),
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            token = r.json()["id_token"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise Unavailable(type(e).__name__) from None
        claims = verify_id_token(
            token, self.client_id, nonce, APPLE_JWKS, APPLE_ISSUERS
        )
        return _claims(self.name, claims)


def apple_names(raw):
    """(given, family) from the `user` field Apple posts on first consent only, JSON like
    {"name": {"firstName": .., "lastName": ..}, "email": ..}; ("", "") for anything else.
    The field isn't signed: it only ever names the person signing in themselves."""
    if len(raw) > 2000:  # far past any name; deep nesting would raise RecursionError
        return "", ""
    try:
        name = json.loads(raw).get("name")
        given, family = name.get("firstName"), name.get("lastName")
    except (ValueError, AttributeError, TypeError, RecursionError):
        return "", ""
    return (
        given if isinstance(given, str) else "",
        family if isinstance(family, str) else "",
    )


# --- the fake ----------------------------------------------------------------------------


class Fake:
    """Stands in for Google or Apple, driving the real start and callback: its authorize
    URL is a local consent page (app/routes/oauth.py) whose form mints a one-time code
    bound to the flow's nonce and, for Google, its PKCE challenge. Nothing leaves the app."""

    def __init__(self, name):
        self.name = name  # it serves this provider's routes
        self.pkce = name != "apple"  # as the real one: Apple has none
        self._codes = {}  # code -> (Claims, nonce, code_challenge)
        self._lock = threading.Lock()

    def authorize_url(self, redirect_uri, state, nonce, code_challenge):
        flow = {"provider": self.name, "state": state, "nonce": nonce}
        if self.pkce:
            flow["code_challenge"] = code_challenge
        return "/auth/fake/consent?" + urlencode(flow)

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
        if self.pkce and not hmac.compare_digest(challenge(verifier), code_challenge):
            raise AuthError("PKCE mismatch")
        if not nonce or not hmac.compare_digest(bound_nonce, nonce):
            raise AuthError("nonce mismatch")
        return claims


_fakes = {name: Fake(name) for name in LABELS}


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


def get_provider(name, env=os.environ):
    """The provider behind a sign-in button: the fake, the real one, or None (no button,
    and its routes answer 404) for one not configured or not known."""
    if name not in LABELS:
        return None
    if fake_enabled(env):
        return _fakes[name]
    if name == "google" and (creds := google_oauth(env)):
        return Google(*creds)
    if name == "apple" and (creds := apple_oauth(env)):
        return Apple(*creds)
    return None


def enabled_providers(env=os.environ):
    """[(name, label)] of the configured providers, in the login page's order."""
    return [(n, label) for n, label in LABELS.items() if get_provider(n, env)]


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
