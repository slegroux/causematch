import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from conftest import FORM, jpeg
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from app import oauth
from app.db import photos, profiles
from main import app

EMAIL = "robin@example.com"


def start(client):
    """GET /auth/google/start: the flow's state, nonce and PKCE challenge, from the fake's
    authorize URL (the cookie holds the same flow)."""
    r = client.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    return {k: q[k][0] for k in ("state", "nonce", "code_challenge")}


def consent(client, flow, sub="g-1", verified=True, picture=""):
    """The fake's consent form, as the browser posts it: the callback URL it sends to."""
    form = {
        **flow,
        "sub": sub,
        "email": EMAIL,
        "given_name": "Robin",
        "family_name": "Lee",
        "picture_url": picture,
        **({"email_verified": "1"} if verified else {}),
    }
    r = client.post("/auth/fake/consent", data=form, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def google_sign_in(client, **kw):
    callback = consent(client, start(client), **kw)
    return client.get(callback, follow_redirects=False), callback


def signed_out(client):
    return client.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_new_identity_onboards_then_signs_back_in_to_the_deck(anon):
    assert "Continue with Google" in anon.get("/login").text
    r, _ = google_sign_in(anon)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    assert 'value="Robin"' in anon.get("/onboarding").text
    (p,) = profiles(where="email = ?", where_args=[EMAIL])
    assert p.phone is None and p.role == "volunteer"
    r = anon.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    assert profiles[p.id].email == EMAIL  # onboarding keeps the verified address

    again = TestClient(app)
    r, _ = google_sign_in(again)
    assert r.headers["location"] == "/deck"
    assert again.get("/deck").status_code == 200
    assert profiles.count_where("email = ?", [EMAIL]) == 1


def _wrong_state(c):
    return c.get(consent(c, {**start(c), "state": "forged"}), follow_redirects=False)


def _replayed(c):
    """The same callback URL twice: the cookie is gone, and with the cookie copied the
    code is used up."""
    flow = start(c)
    jar = {"cm_oauth": c.cookies["cm_oauth"]}
    callback = consent(c, flow)
    assert c.get(callback, follow_redirects=False).status_code == 303
    assert "cm_oauth" not in c.cookies  # deleted by the callback: single use
    assert c.get(callback, follow_redirects=False).status_code == 400
    thief = TestClient(app, cookies=jar)
    r = thief.get(callback, follow_redirects=False)
    assert signed_out(thief)
    return r


def _nonce_mismatch(c):
    first = start(c)
    second = start(c)  # the cookie now holds the second flow
    callback = consent(c, {**second, "nonce": first["nonce"]})
    return c.get(callback, follow_redirects=False)


def _unverified(c):
    r, _ = google_sign_in(c, verified=False)
    assert "isn't verified" in r.text
    return r


@pytest.mark.parametrize(
    "attempt", [_wrong_state, _replayed, _nonce_mismatch, _unverified]
)
def test_failed_checks_refuse_with_400_and_no_session(attempt):
    c = TestClient(app)
    assert attempt(c).status_code == 400
    if attempt is not _replayed:  # that one's own client did sign in, once
        assert signed_out(c)
        assert profiles.count_where("email = ?", [EMAIL]) == 0


def test_id_token_verified_against_the_jwks(monkeypatch):
    key, other = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
                  for _ in range(2))  # fmt: skip
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    keys = jwt.PyJWKClient("https://keys.invalid/certs")
    monkeypatch.setattr(keys, "fetch_data", lambda: {"keys": [{**jwk, "kid": "k1"}]})
    monkeypatch.setattr(oauth, "JWKS", keys)
    now = int(time.time())
    good = {"iss": "https://accounts.google.com", "aud": "cid", "sub": "123",
            "iat": now, "exp": now + 600, "nonce": "n1"}  # fmt: skip

    def token(signer=key, **claims):
        return jwt.encode({**good, **claims}, signer, "RS256", headers={"kid": "k1"})

    assert oauth.verify_id_token(token(), "cid", "n1")["sub"] == "123"
    for bad, nonce in (
        (token(aud="someone-else"), "n1"),
        (token(iat=now - 7200, exp=now - 3600), "n1"),
        (token(), "n2"),
        (token(signer=other), "n1"),
    ):
        with pytest.raises(oauth.AuthError):
            oauth.verify_id_token(bad, "cid", nonce)


@pytest.mark.parametrize("opt_in", [True, False])
def test_google_photo_is_imported_only_when_ticked(anon, monkeypatch, opt_in):
    monkeypatch.setattr(oauth, "fetch_picture", lambda url: jpeg())
    google_sign_in(anon, picture="https://lh3.googleusercontent.com/a/robin")
    assert "Use my Google photo" in anon.get("/onboarding").text
    form = {**FORM, **({"google_photo": "1"} if opt_in else {})}
    r = anon.post("/onboarding", data=form, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    (p,) = profiles(where="email = ?", where_args=[EMAIL])
    assert bool(p.photo_key) is opt_in
    assert photos.count_where("owner_id = ?", [p.id]) == int(opt_in)


def test_no_provider_means_no_button_and_404(anon, monkeypatch):
    for k in ("CAUSEMATCH_FAKE_OAUTH", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert "Continue with Google" not in anon.get("/login").text
    for path in ("/auth/google/start", "/auth/google/callback", "/auth/fake/consent"):
        assert anon.get(path, follow_redirects=False).status_code == 404
