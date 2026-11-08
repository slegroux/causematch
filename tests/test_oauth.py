import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from conftest import (
    FORM,
    coordinator_for,
    jpeg,
    link_sign_in,
    orgs,
    pantry_opening,
    volunteer,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from app import data, notify, oauth, security
from app.db import Identity, blocks, identities, photos, profiles, retained_identities
from app.routes import oauth as oauth_routes
from app.routes.auth import SENT as LINK_SENT
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
    c = TestClient(app)  # on a preview the fake signs in no admin, and nobody off the
    r, _ = google_sign_in(c, STAFF, email=email, hd=hd)  # demo's domains
    assert NO_ACCOUNT in r.text and signed_out(c)


@pytest.mark.parametrize(
    "email, signs_in",
    [("lee@demo.causematch.test", True), ("coord0@example.org", False)],
)
def test_on_a_preview_the_fake_signs_in_demo_coordinators_only(
    anon, monkeypatch, email, signs_in
):
    coordinator_for(0, email)
    monkeypatch.setattr(oauth_routes, "is_preview", lambda: True)
    r, _ = google_sign_in(anon, STAFF, email=email, hd=email.rpartition("@")[2])
    assert (r.status_code == 303 and r.headers["location"] == "/org") is signs_in
    assert (anon.get("/org", follow_redirects=False).status_code == 200) is signs_in


def test_a_fake_code_expires_with_its_flow(monkeypatch):
    fake = oauth.Fake("google")
    claims = oauth.Claims("google", "g-late", EMAIL, True, "", "", "")
    code = fake.mint(claims, "n", oauth.challenge("v"))
    assert fake.finish(fake.mint(claims, "n", oauth.challenge("v")), "v", "", "n")
    monkeypatch.setattr(oauth, "COOKIE_MAX_AGE", -1)  # its 10 minutes are up
    with pytest.raises(oauth.AuthError, match="expired"):
        fake.finish(code, "v", "", "n")


def test_the_fake_consent_page_asks_for_a_subject_of_ones_own(anon):
    page = anon.get("/auth/fake/consent", params={"provider": "google", **start(anon)})
    (sub,) = re.findall(r'<input[^>]*name="sub"[^>]*>', page.text)
    assert 'value=""' in sub and "required" in sub and "fake-1" not in page.text


def test_coordinator_sign_in_with_an_unknown_email_creates_nothing(anon):
    before = counts()
    r, _ = google_sign_in(anon, STAFF, email="nobody@example.org", hd="example.org")
    assert r.status_code == 200
    assert "No nonprofit account uses this email" in r.text
    assert 'href="/org/claim"' in r.text
    assert counts() == before
    assert anon.get("/org", follow_redirects=False).headers["location"] == "/org/login"


def test_coordinator_and_volunteer_intents_never_cross(anon):
    _, email = coordinator_for(0)
    before, mails = counts(), len(notify.get_emailer().sent)
    r, _ = google_sign_in(anon, email=email, sub="g-coord")  # the volunteer sign-in
    # one address, one role: the emailed link's answer for it, and no volunteer
    assert r.status_code == 200 and LINK_SENT in r.text
    assert counts() == before and signed_out(anon)
    assert len(notify.get_emailer().sent) == mails + 1
    to, _, body = notify.get_emailer().sent[-1]
    assert to == email and "/org/login/verify?token=" in body  # the staff link
    assert anon.get("/org", follow_redirects=False).headers["location"] == "/org/login"

    google_sign_in(TestClient(app), sub="g-vol")  # a volunteer's Google account
    staff = TestClient(app)
    # a volunteer's email, as staff, from a Workspace account that hosts it
    r, _ = google_sign_in(staff, STAFF, sub="g-vol", hd="example.com")
    assert r.status_code == 200 and "Claim your nonprofit" in r.text
    assert signed_out(staff)
    assert staff.get("/org", follow_redirects=False).headers["location"] == "/org/login"


# --- Me: connect and disconnect -----------------------------------------------------------


def test_link_connects_google_to_an_email_volunteer_but_not_someone_elses(anon):
    vol = volunteer()
    r = connect(vol)
    assert r.headers["location"] == "/me/profile?notice=connected"
    assert data.identities_of(vol.uid)["google"] == EMAIL
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
    assert list(data.identities_of(vol.uid)) == ["email"]  # the emailed link is left
    assert vol.get("/deck").status_code == 200  # nobody is signed out

    google_sign_in(anon, sub="g-only")  # Google is the only way in
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


def test_signing_in_again_or_connecting_never_renames_the_volunteer(anon):
    """The provider's name is read once, when the account is created: a later sign-in or
    a connect that carries another name leaves the profile as it is, so name_changed_at
    (which refuses hours confirmed before a rename) stays unset."""
    claims = oauth.Claims("google", "g-name", EMAIL, True, "Robin", "Lee", "")
    uid = data.volunteer_for_identity(claims)
    assert profiles[uid].display_name == "Robin" and profiles[uid].last_name is None
    data.save_profile_details(
        uid, first_name="Robin", last_name="Lee", pronouns="", bio="", interests=[]
    )  # the last name is added by hand: a provider name is never the last name
    assert profiles[uid].name_changed_at is None  # first fill-in
    other = oauth.Claims("google", "g-name", EMAIL, True, "Mallory", "Mal", "")
    assert data.volunteer_for_identity(other) == uid
    p = profiles[uid]
    assert (p.display_name, p.last_name, p.name_changed_at) == ("Robin", "Lee", None)
    phone_user = volunteer("Sam")
    connect(phone_user, sub="g-connect")
    p = profiles[phone_user.uid]
    assert (p.display_name, p.last_name, p.name_changed_at) == ("Sam", None, None)


# --- a suspended mailbox stays suspended whichever way in comes next ----------------------


@pytest.mark.parametrize("deleted", [False, True])
def test_a_suspended_emailed_link_account_cannot_come_back_through_google(deleted):
    uid = link_sign_in(TestClient(app), "mal@example.org")
    profiles.update(id=uid, suspended_at="2026-01-01T00:00:00")
    if deleted:
        data.delete_account(uid)  # suspended: its address's hash is kept
    c = TestClient(app)
    r, _ = google_sign_in(c, sub="g-mal", email="mal+g@example.org")  # the same mailbox
    assert r.status_code == 200 and "can't sign in" in r.text
    assert signed_out(c)
    vol = volunteer()  # and no one can connect a Google account with that address
    flow = start(vol, "/me/identity/google/connect", "POST")
    r = vol.get(consent(vol, flow, sub="g-mal2", email="mal@example.org"),
                follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/me/profile?error=refused-google"


def test_a_deleted_emailed_link_accounts_org_block_follows_it_to_google(anon):
    uid = link_sign_in(TestClient(app), "kit@example.org")
    profiles.update(id=uid, display_name="Kit", onboarded_at="2026-01-01T00:00:00")
    coord, _ = coordinator_for(0)
    data.block_volunteer(coord, data.swipe(uid, pantry_opening().id, True).id)
    data.delete_account(uid)
    r, _ = google_sign_in(anon, sub="g-kit", email="kit@example.org")
    assert r.status_code == 303
    (i,) = identities(where="subject = ?", where_args=["g-kit"])
    assert i.user_id != uid and blocks(
        where="blocked_user_id = ?", where_args=[i.user_id]
    )


# --- one address, one account: the emailed link and Google or Apple join ------------------

GMAIL = (
    "robin.lee@gmail.com"  # a mailbox Google hosts, so its verified address is proof
)


def owner(sub):
    (i,) = identities(where="subject = ?", where_args=[sub])
    return i.user_id


def uid_of(client):
    """The profile this browser's session is signed in to (signed, not encrypted)."""
    import base64

    from app.auth import session_cookie

    payload = client.cookies.get(session_cookie(), "").split(".")[0]
    return json.loads(base64.b64decode(payload + "==")).get("uid")


def sent_subject(email):
    return [s for to, s, _ in notify.get_emailer().sent if to == email][-1]


def test_an_emailed_link_to_a_google_volunteers_address_signs_in_to_that_account(anon):
    google_sign_in(anon, sub="g-robin", email=GMAIL)
    uid, before = owner("g-robin"), profiles.count
    c = TestClient(app)
    assert link_sign_in(c, GMAIL) == uid  # the emailed link's identity is attached
    assert sent_subject(GMAIL) == "Your Causematch sign-in link"  # not a sign-up
    assert profiles.count == before and uid_of(c) == uid
    assert data.identities_of(uid) == {"google": GMAIL, "email": GMAIL}
    again = TestClient(app)  # Google again: still the one account
    google_sign_in(again, sub="g-robin", email=GMAIL)
    assert uid_of(again) == uid and profiles.count == before


def test_google_or_apple_with_an_emailed_link_volunteers_address_joins_it(anon):
    uid = link_sign_in(TestClient(app), GMAIL)
    coord, _ = coordinator_for(0)
    data._add_block(blocker_id=coord, blocked_user_id=uid, blocked_org_id=orgs()[0].id)
    before = profiles.count
    r, _ = google_sign_in(anon, sub="g-robin", email=GMAIL.upper())  # as Google sent it
    assert r.status_code == 303 and uid_of(anon) == uid
    assert owner("g-robin") == uid and profiles.count == before
    assert blocks.count_where("blocked_user_id = ?", [uid]) == 1  # the one profile's
    kim = link_sign_in(TestClient(app), "kim@icloud.com")  # Apple hosts iCloud mail
    claims = oauth.Claims("apple", "a-kim", "kim@icloud.com", True, "Kim", "", "")
    assert data.volunteer_for_identity(claims) == kim
    assert data.identities_of(kim)["apple"] == "kim@icloud.com"


@pytest.mark.parametrize(
    "case", ["unverified", "not hosted", "typed in Me", "plus tag", "preview fake"]
)
def test_only_the_exact_proven_address_joins(anon, monkeypatch, case):
    """An unverified provider email, one whose provider doesn't host the mailbox (it
    may be stale), the profile's own typed email, a +tag of the address, and the fake
    on a preview (any typed email reads as verified there) never join an account."""
    email = "robin@example.org" if case == "not hosted" else GMAIL
    if case == "typed in Me":  # someone else's account, with this address typed in
        uid = link_sign_in(TestClient(app), "mallory@example.org")
        profiles.update(id=uid, email=GMAIL)
        assert link_sign_in(TestClient(app), GMAIL) != uid  # nor does the link join it
        assert sent_subject(GMAIL) == "Finish signing up for Causematch"
    else:
        uid = link_sign_in(TestClient(app), email)
    if case == "preview fake":
        monkeypatch.setattr(oauth_routes, "is_preview", lambda: True)
    used = GMAIL.replace("@", "+v@") if case == "plus tag" else email
    r, _ = google_sign_in(
        anon, sub="g-other", email=used, verified=case != "unverified"
    )
    if case == "unverified":  # refused, as before: nothing is created or attached
        assert r.status_code == 400 and signed_out(anon)
        assert identities.count_where("provider = ?", ["google"]) == 0
        return
    assert r.status_code == 303 and uid_of(anon) != uid  # an account of its own
    assert "google" not in data.identities_of(uid)
    if case == "preview fake":  # and nothing can join it later either
        (i,) = identities(where="provider = ?", where_args=["google"])
        assert i.authoritative == 0


@pytest.mark.parametrize("variant", ["coord0@example.org", "coord0+vol@example.org"])
def test_a_staff_address_never_signs_a_volunteer_in_or_up(anon, variant):
    """By Google (the exact address or a +tag: the emailed link's answer, and only the
    exact one gets the staff link), by the emailed link (+tag: nothing is sent) and by
    Me's Connect. A volunteer whose Google account's address became staff's is refused."""
    google_sign_in(TestClient(app), sub="g-before", email="coord0@gmail.com")
    coordinator_for(0)
    coordinator_for(1, "coord0@gmail.com")  # that volunteer's address, now staff's
    vol = volunteer()
    before, mails = counts(), len(notify.get_emailer().sent)
    r, _ = google_sign_in(anon, sub="g-staff", email=variant)
    assert r.status_code == 200 and LINK_SENT in r.text and signed_out(anon)
    exact = "+" not in variant
    assert len(notify.get_emailer().sent) == mails + exact
    anon.post("/login/send", data={"email": "coord0+vol@example.org"})
    assert len(notify.get_emailer().sent) == mails + exact  # a +tag gets nothing
    c = TestClient(app)
    r, _ = google_sign_in(c, sub="g-before", email="coord0@gmail.com")
    assert r.status_code == 200 and LINK_SENT in r.text and signed_out(c)
    flow = start(vol, "/me/identity/google/connect", "POST")
    callback = consent(vol, flow, sub="g-connect", email=variant.upper())
    r = vol.get(callback, follow_redirects=False)
    assert r.headers["location"] == "/me/profile?error=refused-google"
    assert counts() == before  # no profile and no identity


def test_a_suspended_account_or_mailbox_is_never_joined_by_a_new_way_in(anon):
    uid = link_sign_in(TestClient(app), GMAIL)
    profiles.update(id=uid, suspended_at="2026-01-01T00:00:00")
    r, _ = google_sign_in(anon, sub="g-robin", email=GMAIL)
    assert r.status_code == 200 and "can't sign in" in r.text and signed_out(anon)
    assert "google" not in data.identities_of(uid)  # nothing attached to it

    google_sign_in(TestClient(app), sub="g-sam", email="sam@gmail.com")
    sam = owner("g-sam")  # live, while another account on the mailbox is suspended
    gone = link_sign_in(TestClient(app), "s.a.m+x@gmail.com")
    profiles.update(id=gone, suspended_at="2026-01-01T00:00:00")
    mails = len(notify.get_emailer().sent)
    TestClient(app).post("/login/send", data={"email": "sam@gmail.com"})
    assert len(notify.get_emailer().sent) == mails  # no link to join Sam with
    claims = oauth.Claims("email", "sam@gmail.com", "sam@gmail.com", True, "", "", "")
    assert data.get_profile(data.volunteer_for_identity(claims)) is None
    assert data.identities_of(sam) == {"google": "sam@gmail.com"}


def test_two_accounts_on_one_address_are_left_apart_and_logged(anon, caplog):
    """Accounts made before joining existed: each way in keeps its own, nothing is
    attached to either, and the operator gets a warning naming the profile ids."""
    first = link_sign_in(TestClient(app), GMAIL)
    second = data.get_or_create_volunteer("+13105559999")
    identities.insert(  # the duplicate a Google sign-in used to make
        Identity(
            user_id=second,
            provider="google",
            subject="g-dup",
            email=GMAIL,
            created_at="2026-01-01T00:00:00",
            authoritative=1,
        )
    )
    with caplog.at_level("WARNING", logger="causematch"):
        assert link_sign_in(TestClient(app), GMAIL) == first
        r, _ = google_sign_in(anon, sub="g-dup", email=GMAIL)
        assert r.status_code == 303 and uid_of(anon) == second
        claims = oauth.Claims("google", "g-new", GMAIL, True, "", "", "")
        third = data.volunteer_for_identity(claims)  # joins neither
    assert third not in (first, second)
    assert data.identities_of(first) == {"email": GMAIL}
    assert data.identities_of(second) == {"google": GMAIL}
    shared = [m for m in caplog.messages if "share a sign-in address" in m]
    assert len(shared) == 3 and all(f"{first},{second}" in m for m in shared)
    assert not any("robin" in m for m in shared)  # ids only, never the address
