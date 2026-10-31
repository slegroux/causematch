import html
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.testclient import TestClient

from app import oauth
from app.config import apple_oauth
from app.db import identities, profiles
from main import app

APPLE = "https://appleid.apple.com"
CLIENT_ID = "org.causematch.web"
RELAY = "x7k2@privaterelay.appleid.com"
CROSS_SITE = {"Origin": APPLE}
HTTPS = "https://testserver"  # so the client sends back the Secure flow cookie
ATTR = re.compile(r"""(\w+)=(["'])(.*?)\2""")  # either quote: a JSON value gets '


def signed_out(client):
    return client.get("/deck", follow_redirects=False).headers["location"] == "/login"


@pytest.fixture
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def provider_with(key):
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    env = {
        "APPLE_CLIENT_ID": CLIENT_ID,
        "APPLE_TEAM_ID": "TEAM123456",
        "APPLE_KEY_ID": "KEY1234567",
        "APPLE_PRIVATE_KEY": pem.replace("\n", "\\n"),  # as Secret Manager may hold it
    }
    return oauth.Apple(*apple_oauth(env))


def test_client_secret_is_an_es256_jwt_for_apple(ec_key):
    secret = provider_with(ec_key).client_secret()
    assert jwt.get_unverified_header(secret)["kid"] == "KEY1234567"
    c = jwt.decode(secret, ec_key.public_key(), algorithms=["ES256"], audience=APPLE)
    assert (c["iss"], c["sub"]) == ("TEAM123456", CLIENT_ID)
    assert 0 < c["exp"] - c["iat"] <= 180 * 24 * 3600  # Apple's limit is six months
    assert apple_oauth({}) is None  # none set: no Apple button
    with pytest.raises(RuntimeError, match="APPLE_KEY_ID.*APPLE_PRIVATE_KEY"):
        apple_oauth({"APPLE_CLIENT_ID": CLIENT_ID, "APPLE_TEAM_ID": "TEAM123456"})


def test_token_exchange_verifies_the_id_token_against_apples_keys(ec_key, monkeypatch):
    signer, other = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
                     for _ in range(2))  # fmt: skip
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signer.public_key()))
    keys = jwt.PyJWKClient("https://keys.invalid/apple")
    monkeypatch.setattr(keys, "fetch_data", lambda: {"keys": [{**jwk, "kid": "a1"}]})
    monkeypatch.setattr(oauth, "APPLE_JWKS", keys)
    now = int(time.time())
    good = {"iss": APPLE, "aud": CLIENT_ID, "sub": "001.abc", "iat": now,
            "exp": now + 600, "nonce": "n1", "email": RELAY,
            "email_verified": "true"}  # fmt: skip
    sent = {}

    def answer(token):
        def post(url, data, timeout):
            sent.update(url=url, **data)
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: {"id_token": token}})()  # fmt: skip

        monkeypatch.setattr(oauth.httpx, "post", post)

    def token(key=signer, **claims):
        return jwt.encode({**good, **claims}, key, "RS256", headers={"kid": "a1"})

    apple = provider_with(ec_key)
    answer(token())
    claims = apple.finish("code-1", "unused", "https://x/cb", "n1")
    assert (claims.provider, claims.subject, claims.email) == (
        "apple",
        "001.abc",
        RELAY,
    )
    assert claims.email_verified  # Apple says "true", a string
    assert sent["url"] == oauth.APPLE_TOKEN and "code_verifier" not in sent
    jwt.decode(sent["client_secret"], ec_key.public_key(), ["ES256"], audience=APPLE)
    for bad in (
        token(aud="someone-else"),
        token(iat=now - 7200, exp=now - 3600),
        token(iss="https://accounts.google.com"),
        token(key=other),
    ):
        answer(bad)
        with pytest.raises(oauth.AuthError):
            apple.finish("code-1", "unused", "https://x/cb", "n1")


def apple_form(client):
    """Start, then the fake's consent: the form Apple would post back cross-site."""
    r = client.get("/auth/apple/start", follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "samesite=none" in cookie and "secure" in cookie
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "code_challenge" not in q  # Apple has no PKCE
    consent = {
        "provider": "apple",
        "state": q["state"][0],
        "nonce": q["nonce"][0],
        "sub": "001.robin",
        "email": RELAY,
        "email_verified": "1",
        "given_name": "Robin",
        "family_name": "Lee",
    }
    page = client.post("/auth/fake/consent", data=consent).text
    fields = {}
    for tag in re.findall(r"<input[^>]*>", page):
        attrs = {k: html.unescape(v) for k, _, v in ATTR.findall(tag)}
        if "name" in attrs:
            fields[attrs["name"]] = attrs.get("value", "")
    assert '"firstName": "Robin"' in fields["user"]
    return {k: fields[k] for k in ("code", "state", "user")}


def callback(client, form):
    """Apple's cross-site form_post: it comes from Apple's origin, not ours."""
    return client.post(
        "/auth/apple/callback", data=form, headers=CROSS_SITE, follow_redirects=False
    )


def test_cross_site_form_post_signs_in_only_with_the_flow_cookie():
    c = TestClient(app, base_url=HTTPS)
    form = apple_form(c)
    stranger = TestClient(app, base_url=HTTPS)
    assert callback(stranger, form).status_code == 400 and signed_out(stranger)
    assert profiles.count_where("email = ?", [RELAY]) == 0
    r = callback(c, form)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    (p,) = profiles(where="email = ?", where_args=[RELAY])
    assert (p.display_name, p.phone) == ("Robin", None)
    assert identities.count_where("provider = 'apple' AND subject = '001.robin'") == 1
    assert callback(c, form).status_code == 400  # the cookie went with the first one


def test_other_cross_site_posts_are_still_refused():
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/login/send", data={"phone": "3105550131"}, headers=CROSS_SITE)
    assert r.status_code == 403


def test_bad_user_field_means_no_names():
    for raw in ("not json", "[]", '{"name": "Robin"}', '{"name": {"firstName": 7}}'):
        assert oauth.apple_names(raw) == ("", "")
    assert oauth.apple_names('{"name": {"firstName": "Ana"}}') == ("Ana", "")
