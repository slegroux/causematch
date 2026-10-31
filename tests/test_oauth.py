import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from conftest import FORM, coordinator_for, jpeg, orgs, volunteer
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from app import data, notify, oauth, security
from app.db import blocks, identities, photos, profiles, retained_identities
from app.routes import oauth as oauth_routes
from main import app

EMAIL = "robin@example.com"


def start(client, path="/auth/google/start", method="GET"):
    """GET /auth/google/start (or another start, such as Me's Connect POST): the flow's
    state, nonce and PKCE challenge, from the fake's authorize URL (the cookie holds the
    same flow)."""
    r = client.request(method, path, follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    return {k: q[k][0] for k in ("state", "nonce", "code_challenge")}


def consent(client, flow, sub="g-1", verified=True, picture="", email=EMAIL, hd=""):
    """The fake's consent form, as the browser posts it: the callback URL it sends to."""
    form = {
        **flow,
        "sub": sub,
        "email": email,
        "given_name": "Robin",
        "family_name": "Lee",
        "picture_url": picture,
        "hd": hd,
        **({"email_verified": "1"} if verified else {}),
    }
    r = client.post("/auth/fake/consent", data=form, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def google_sign_in(client, path="/auth/google/start", **kw):
    callback = consent(client, start(client, path), **kw)
    return client.get(callback, follow_redirects=False), callback


def connect(client, sub="g-link"):
    """Me's Connect Google, through the fake: the callback's response."""
    flow = start(client, "/me/identity/google/connect", "POST")
    return client.get(consent(client, flow, sub=sub), follow_redirects=False)


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


# --- coordinators and admins (?for=org): the verified email is the key ------------------

STAFF = "/auth/google/start?for=org"
NO_ACCOUNT = "No nonprofit account uses this email"
SENT = "If that address belongs to a coordinator, a sign-in link is on its way"


def counts():
    return profiles.count, identities.count


@pytest.mark.parametrize(
    "who, email, hd",
    [
        ("coordinator", "coord0@example.org", "example.org"),  # Workspace: Google's own
        ("admin", "boss@gmail.com", ""),
        ("coordinator", "coord0@example.org", ""),  # Google doesn't host this mailbox
    ],
)
def test_staff_sign_in_needs_the_provider_to_host_the_mailbox(
    anon, monkeypatch, who, email, hd
):
    if who == "coordinator":
        coordinator_for(0, email)
    else:
        data.add_admin(email, "Boss Person")
    assert STAFF in anon.get("/org/login").text
    before, mails = counts(), len(notify.get_emailer().sent)
    r, _ = google_sign_in(anon, STAFF, email=email.upper(), hd=hd)  # lowercased
    assert counts() == before  # a staff sign-in creates no profile and no identity
    if not hd and not email.endswith("@gmail.com"):  # the emailed link's answer instead
        assert r.status_code == 200 and SENT in r.text
        assert len(notify.get_emailer().sent) == mails + 1
        assert (
            anon.get("/org", follow_redirects=False).headers["location"] == "/org/login"
        )
        return
    lands = "/org" if who == "coordinator" else "/admin"
    assert r.status_code == 303 and r.headers["location"] == lands
    assert anon.get(lands).status_code == 200
    monkeypatch.setattr(oauth_routes, "is_preview", lambda: True)
    c = TestClient(app)  # on a preview the fake signs in coordinators, never an admin
    r, _ = google_sign_in(c, STAFF, email=email, hd=hd)
    assert (NO_ACCOUNT in r.text and signed_out(c)) is (who == "admin")


def test_coordinator_sign_in_with_an_unknown_email_creates_nothing(anon):
    before = counts()
    r, _ = google_sign_in(anon, STAFF, email="nobody@example.org", hd="example.org")
    assert r.status_code == 200
    assert "No nonprofit account uses this email" in r.text
    assert 'href="/org/claim"' in r.text
    assert counts() == before
    assert anon.get("/org", follow_redirects=False).headers["location"] == "/org/login"


def test_coordinator_and_volunteer_intents_never_cross(anon):
    coord_uid, email = coordinator_for(0)
    r, _ = google_sign_in(anon, email=email, sub="g-coord")  # the volunteer sign-in
    assert r.headers["location"] == "/onboarding"
    (i,) = identities(where="subject = ?", where_args=["g-coord"])
    assert i.user_id != coord_uid and profiles[i.user_id].role == "volunteer"
    assert anon.get("/org", follow_redirects=False).status_code == 403

    staff = TestClient(app)
    # a volunteer's email, as staff, from a Workspace account that hosts it
    r, _ = google_sign_in(staff, STAFF, sub="g-coord", hd="example.com")
    assert r.status_code == 200 and "Claim your nonprofit" in r.text
    assert signed_out(staff)
    assert staff.get("/org", follow_redirects=False).headers["location"] == "/org/login"


# --- Me: connect and disconnect -----------------------------------------------------------


def test_link_connects_google_to_a_phone_volunteer_but_not_someone_elses(anon):
    vol = volunteer()
    r = connect(vol)
    assert r.headers["location"] == "/me/profile?notice=connected"
    assert data.identities_of(vol.uid) == {"google": EMAIL}
    assert f"Connected as {EMAIL}" in vol.get("/me/profile").text
    google_sign_in(anon, sub="g-theirs")  # another volunteer's Google account
    r = connect(volunteer(), sub="g-theirs")
    assert r.headers["location"] == "/me/profile?error=taken-google"
    page = vol.get(r.headers["location"]).text
    assert "already connected to another Causematch account" in page
    assert identities.count_where("subject = ?", ["g-theirs"]) == 1


def test_link_with_a_flow_sealed_for_another_uid_is_400():
    me, them = volunteer(), volunteer()
    state, nonce, verifier = oauth.new_flow()
    me.cookies.set("cm_oauth", oauth.seal("google", state, nonce, verifier, "link",
                                          them.uid, 1))  # fmt: skip
    flow = {"state": state, "nonce": nonce, "code_challenge": oauth.challenge(verifier)}
    r = me.get(consent(me, flow, sub="g-forged"), follow_redirects=False)
    assert r.status_code == 400
    assert identities.count_where("subject = ?", ["g-forged"]) == 0


def test_unlink_never_removes_the_last_way_in(anon):
    vol = volunteer()
    connect(vol)
    r = vol.post("/me/identity/google/disconnect", follow_redirects=False)
    assert r.headers["location"] == "/me/profile?notice=disconnected"
    assert data.identities_of(vol.uid) == {}
    assert vol.get("/deck").status_code == 200  # nobody is signed out

    google_sign_in(anon, sub="g-only")  # no phone: Google is the only way in
    anon.post("/onboarding", data=FORM)
    r = anon.post("/me/identity/google/disconnect", follow_redirects=False)
    assert r.headers["location"] == "/me/profile?error=last"
    assert "Keep at least one way to sign in" in anon.get(r.headers["location"]).text
    assert identities.count_where("subject = ?", ["g-only"]) == 1


def test_staff_sign_in_refuses_a_non_ascii_email_before_lowercasing(anon):
    coordinator_for(0, "karen@example.org")
    kelvin = "Karen@example.org"  # Python lowercases the Kelvin sign to "k"
    before, mails = counts(), len(notify.get_emailer().sent)
    r, _ = google_sign_in(anon, STAFF, email=kelvin, hd="example.org")
    assert r.status_code == 200 and NO_ACCOUNT in r.text
    assert counts() == before and len(notify.get_emailer().sent) == mails
    assert anon.get("/org", follow_redirects=False).headers["location"] == "/org/login"


def test_junk_callbacks_do_not_use_up_a_shared_address_budget(anon):
    for _ in range(security.OAUTH_PER_IP[0][1] + 5):  # more than the limit allows
        r = anon.get("/auth/google/callback?state=x&code=y", follow_redirects=False)
        assert r.status_code == 400  # refused on the missing flow cookie, never 429
    r, _ = google_sign_in(anon)  # a real flow from the same address still works
    assert r.status_code == 303


def test_disconnect_signs_other_sessions_out_but_not_this_one(anon):
    vol = volunteer()
    connect(vol)
    other = TestClient(app)  # the same account, signed in on another device
    other.cookies.update(vol.cookies)
    assert other.get("/deck", follow_redirects=False).status_code == 200
    vol.post("/me/identity/google/disconnect", follow_redirects=False)
    assert vol.get("/deck", follow_redirects=False).status_code == 200
    assert other.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_disconnecting_does_not_shed_a_block(anon):
    vol = volunteer()
    connect(vol, sub="g-blocked")
    coordinator_uid, _ = coordinator_for(0)
    data._add_block(
        blocker_id=coordinator_uid, blocked_user_id=vol.uid, blocked_org_id=orgs()[0].id
    )
    vol.post("/me/identity/google/disconnect", follow_redirects=False)
    assert len(retained_identities()) == 1  # a hash, never the subject
    google_sign_in(anon, sub="g-blocked")  # a new profile, same Google account
    (i,) = identities(where="subject = ?", where_args=["g-blocked"])
    assert i.user_id != vol.uid
    assert blocks.count_where("blocked_user_id = ?", [i.user_id]) == 1


def test_disconnecting_with_nothing_against_the_account_keeps_no_hash(anon):
    vol = volunteer()
    connect(vol)
    vol.post("/me/identity/google/disconnect", follow_redirects=False)
    assert retained_identities() == []
