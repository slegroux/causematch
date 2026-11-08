import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import coordinator_for, link_sign_in, sign_in, volunteer
from starlette.testclient import TestClient

from app import data, notify, oauth, security
from app.auth import DEV_SECRET, normalize_phone, session_secret
from app.config import require_volunteer_sign_in
from app.db import identities, login_tokens, profiles, rate_events
from app.routes.auth import SENT
from main import app

TWILIO = {
    "TWILIO_ACCOUNT_SID": "a",
    "TWILIO_AUTH_TOKEN": "b",
    "TWILIO_FROM_NUMBER": "+1",
}


def test_signed_out_deck_redirects_to_login(anon):
    r = anon.get("/deck", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_is_public(anon):
    assert anon.get("/login").status_code == 200


def send_link(client, email, **kw):
    return client.post("/login/send", data={"email": email}, **kw)


def verify(client, token):
    return client.post("/login/verify", data={"token": token}, follow_redirects=False)


def link_token(email):
    """The token of the last link emailed to this address, or None."""
    for to, _, body in reversed(notify.get_emailer().sent):
        if to == email:
            return re.search(r"token=([\w-]+)", body).group(1)
    return None


def email_volunteers(email):
    return identities(
        where="provider = ? AND subject = ?", where_args=[data.EMAIL_SIGN_IN, email]
    )


def test_every_address_gets_the_same_answer_and_only_the_right_link(anon):
    sign_in(TestClient(app), "3105550001")  # signed up by link: v13105550001@...
    known = "v13105550001@example.org"
    sign_in(TestClient(app), "3105550002")
    suspended = "v13105550002@example.org"
    profiles.update(
        id=email_volunteers(suspended)[0].user_id, suspended_at="2026-01-01"
    )
    _, staff = coordinator_for(0)
    pages = [
        send_link(anon, e)
        for e in (known, "New@Example.org", suspended, staff, "nobody@example.org")
    ]
    assert {r.status_code for r in pages} == {200}
    assert len({r.text for r in pages}) == 1 and SENT in pages[0].text
    sent = {to: (subject, body) for to, subject, body in notify.get_emailer().sent}
    assert sent[known][0] == "Your Causematch sign-in link"
    assert sent["new@example.org"][0] == "Finish signing up for Causematch"
    assert "/login/verify?token=" in sent["new@example.org"][1]
    assert suspended not in sent  # a suspended account costs no email
    assert "/org/login/verify?token=" in sent[staff][1]  # staff get their own link


def test_a_sign_up_link_creates_the_account_only_when_used(anon):
    email = "new@example.org"
    send_link(anon, "NEW@example.org")
    token = link_token(email)
    before = profiles.count
    assert anon.get(f"/login/verify?token={token}").status_code == 200
    assert profiles.count == before and email_volunteers(email) == []
    r = verify(anon, token)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    (ident,) = email_volunteers(email)
    p = profiles[ident.user_id]
    assert p.role == "volunteer" and p.email == email and p.phone is None
    assert profiles.count == before + 1
    again = TestClient(app)  # the same address later: the same account
    send_link(again, email)
    assert verify(again, link_token(email)).status_code == 303
    assert [i.user_id for i in email_volunteers(email)] == [p.id]


def test_a_link_works_once_and_expires(anon):
    send_link(anon, "once@example.org")
    token = link_token("once@example.org")
    assert verify(anon, token).status_code == 303
    other = TestClient(app)
    r = verify(other, token)
    assert r.status_code == 200 and "expired or was already used" in r.text
    assert other.get("/deck", follow_redirects=False).headers["location"] == "/login"
    send_link(other, "late@example.org")
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds")
    for t in login_tokens():
        login_tokens.update(token_hash=t.token_hash, expires_at=past)
    r = verify(other, link_token("late@example.org"))
    assert r.status_code == 200 and "expired" in r.text
    assert email_volunteers("late@example.org") == []


def test_the_token_is_stored_hashed_for_15_minutes(anon):
    send_link(anon, "hash@example.org")
    token = link_token("hash@example.org")
    (t,) = login_tokens()
    assert token not in str(t) and len(t.token_hash) == 64 and t.user_id is None
    left = datetime.fromisoformat(t.expires_at) - datetime.now(UTC)
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)
    assert security.redact_query(f"/login/verify?token={token}") == (
        "/login/verify?token=[redacted]"
    )


def test_get_and_head_do_not_use_the_link_up(anon):
    send_link(anon, "scan@example.org")
    token = link_token("scan@example.org")
    for method in (anon.get, anon.head, anon.get):
        r = method(f"/login/verify?token={token}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login/verify"
    page = anon.get(f"/login/verify?token={token}")
    assert token not in page.text and 'action="/login/verify"' in page.text
    assert [t.used_at for t in login_tokens()] == [None]
    r = anon.post(
        "/login/verify", follow_redirects=False
    )  # the button: from the session
    assert r.status_code == 303 and login_tokens()[0].used_at


def test_a_staff_link_and_a_volunteer_link_only_work_on_their_own_page(anon):
    _, staff = coordinator_for(0)
    anon.post("/org/login/send", data={"email": staff})
    staff_token = link_token(staff)
    send_link(anon, "vol@example.org")
    vol_token = link_token("vol@example.org")
    assert "expired" in verify(anon, staff_token).text
    org = anon.post("/org/login/verify", data={"token": vol_token})
    assert "expired" in org.text
    assert [t.used_at for t in login_tokens()] == [None, None]  # neither used up
    assert verify(anon, vol_token).headers["location"] == "/onboarding"


def at(ip):
    return TestClient(app, client=(ip, 1))


def test_links_are_limited_per_mailbox_and_address_then_per_mailbox_a_day():
    """5 an hour per mailbox from one address, whoever holds it (a +tag or Gmail's dots
    are the same mailbox); someone asking elsewhere doesn't hold the owner's off; 20 a
    day per mailbox in all."""
    c = at("198.51.100.1")
    variants = ("a.b@gmail.com", "ab@gmail.com", "ab+1@gmail.com", "AB@googlemail.com")
    codes = [
        send_link(c, e).status_code
        for e in (*variants, "a.b+x@gmail.com", "ab@gmail.com")
    ]
    assert codes == [200] * 5 + [429]
    assert send_link(at("203.0.113.5"), "ab@gmail.com").status_code == 200
    for i in range(14):  # 20 today in all
        assert send_link(at(f"192.0.2.{i + 1}"), "ab@gmail.com").status_code == 200
    assert send_link(at("192.0.2.200"), "ab@gmail.com").status_code == 429


def test_ipv6_link_requests_share_a_slash_48():
    codes = [
        send_link(at(f"2001:db8:cc:{i:x}::1"), f"n{i}@example.org").status_code
        for i in range(41)
    ]
    assert codes == [200] * 40 + [429]  # each /64 alone may ask for 20


def test_sign_up_links_stop_at_the_app_wide_ceiling_but_sign_ins_still_go(monkeypatch):
    monkeypatch.setattr(security, "SIGNUP_ALL", (("signup-all-hour", 2, 3600),))
    sign_in(TestClient(app), "3105550010")  # an account: v13105550010@example.org
    pages = [send_link(at(f"192.0.2.{i}"), f"new{i}@example.org") for i in range(3)]
    assert {r.status_code for r in pages} == {200} and len({r.text for r in pages}) == 1
    assert len(notify.get_emailer().sent) == 2  # the third sign-up link isn't sent
    send_link(at("192.0.2.9"), "v13105550010@example.org")
    assert notify.get_emailer().sent[-1][0] == "v13105550010@example.org"


def test_a_suspended_google_volunteers_address_gets_no_link_live_or_deleted(anon):
    claims = oauth.Claims("google", "g-1", "bob@gmail.com", True, "Bob", "", "")
    uid = data.volunteer_for_identity(claims)
    profiles.update(id=uid, suspended_at="2026-01-01T00:00:00")
    before = profiles.count
    for e in ("bob@gmail.com", "b.o.b+x@gmail.com"):
        assert SENT in send_link(anon, e).text
    data.delete_account(uid)  # suspended: its verified address's hash is kept too
    assert SENT in send_link(anon, "bob@gmail.com").text
    assert notify.get_emailer().sent == [] and profiles.count == before


def test_variants_of_a_suspended_mailbox_get_nothing_and_odd_shapes_are_refused():
    c = at("203.0.113.7")
    uid = link_sign_in(c, "mal@gmail.com")
    profiles.update(id=uid, suspended_at="2026-01-01T00:00:00")
    notify.get_emailer().sent.clear()
    for v in ("mal+2@gmail.com", "m.al@gmail.com", "MAL@googlemail.com"):
        assert SENT in send_link(c, v).text
    for bad in (
        "mal@gmail.com.",
        "<mal@gmail.com>",
        '"mal"@gmail.com',
        "x<mal@gmail.com>",
    ):
        assert "Enter your email address" in send_link(c, bad).text
    assert notify.get_emailer().sent == []
    send_link(c, "Other.Name+tag@Example.org")  # mail goes to the address as typed
    assert notify.get_emailer().sent[-1][0] == "other.name+tag@example.org"


def test_the_confirm_page_names_the_account_and_warns_about_a_link_from_elsewhere():
    def page(client, path):
        return client.get(path).text.replace("&#x27;", "'")

    attacker = TestClient(app)
    send_link(attacker, "attacker@example.org")
    token = link_token("attacker@example.org")
    mine = page(attacker, f"/login/verify?token={token}")
    assert "Sign in as attacker@example.org" in mine
    assert "didn't ask for this link" not in mine and "another account" not in mine
    victim = volunteer()  # signed in, and never asked for this link
    theirs = page(victim, f"/login/verify?token={token}")
    assert "Sign in as attacker@example.org" in theirs
    assert "didn't ask for this link" in theirs and "another account" in theirs
    _, staff = coordinator_for(0)  # the same on the nonprofit sign-in
    TestClient(app).post("/org/login/send", data={"email": staff})
    org = page(victim, f"/org/login/verify?token={link_token(staff)}")
    assert f"Sign in as {staff}" in org and "didn't ask for this link" in org
    peeked = [
        t for t in login_tokens() if t.user_id or t.email == "attacker@example.org"
    ]
    assert [t.used_at for t in peeked] == [None, None]  # peeking used nothing up
    cookie = json.dumps(session_of(attacker))  # signed, not encrypted: no address in it
    assert "attacker" not in cookie and "example.org" not in cookie


def test_the_asked_for_mark_lasts_a_links_lifetime(monkeypatch):
    import app.routes.auth as auth_routes

    c = TestClient(app)
    send_link(c, "late@example.org")
    token = link_token("late@example.org")
    later = Later(data.TOKEN_MINUTES * 60 + 1)
    monkeypatch.setattr(auth_routes, "time", later)
    page = c.get(f"/login/verify?token={token}").text.replace("&#x27;", "'")
    assert "didn't ask for this link" in page


def test_a_plus_led_address_keeps_its_own_mailbox():
    from app.forms import abuse_key

    assert abuse_key("+x@example.com") == "+x@example.com"
    assert abuse_key("+x@example.com") != abuse_key("+y@example.com")
    assert abuse_key("a+b@corp.com") == "a@corp.com"


def test_a_deleted_account_signs_up_anew_unless_it_was_suspended(anon):
    send_link(anon, "gone@example.org")
    verify(anon, link_token("gone@example.org"))
    (old,) = email_volunteers("gone@example.org")
    data.delete_account(old.user_id)
    notify.get_emailer().sent.clear()
    send_link(anon, "gone@example.org")  # nothing kept: a plain sign-up again
    assert verify(anon, link_token("gone@example.org")).status_code == 303
    (new,) = email_volunteers("gone@example.org")
    assert new.user_id != old.user_id
    profiles.update(id=new.user_id, suspended_at="2026-01-01")
    data.delete_account(new.user_id)  # suspended: its address's hash is kept
    notify.get_emailer().sent.clear()
    r = send_link(anon, "gone@example.org")
    assert SENT in r.text and notify.get_emailer().sent == []


def test_bad_addresses_are_refused_without_a_lookup(anon):
    for bad in ("not-an-email", "\u212aaren@example.org", "a" * 250 + "@x.org"):
        r = send_link(anon, bad)
        assert r.status_code == 200 and "Enter your email address" in r.text
    assert notify.get_emailer().sent == [] and rate_events.count == 0
    for t in ("", "x", "a" * 5000):
        assert "expired" in verify(anon, t).text


def test_sign_in_rotates_the_session(anon):
    old = volunteer()
    anon.cookies.update(old.cookies)
    assert anon.get("/deck").status_code == 200
    send_link(anon, "rotate@example.org")
    r = verify(anon, link_token("rotate@example.org"))
    assert f"Max-Age={30 * 24 * 3600}" in r.headers["set-cookie"]
    uid = email_volunteers("rotate@example.org")[0].user_id
    assert session_of(anon)["uid"] == uid != old.uid


def test_the_login_page_offers_the_link_and_no_phone(anon):
    page = anon.get("/login").text
    assert 'action="/login/send"' in page and 'name="email"' in page
    assert "Or get an emailed link" in page and "Continue with Google" in page
    assert 'name="phone"' not in page and "Text me a code" not in page


def test_sign_out_everywhere_revokes_other_sessions(user):
    assert user.get("/deck").status_code == 200
    (p,) = profiles(where="phone = ?", where_args=[user.phone])
    data.sign_out_everywhere(p.id)
    assert user.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_logout_is_post_only(user):
    assert user.get("/logout", follow_redirects=False).status_code == 405


def test_logout_signs_out(user):
    assert user.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert user.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_session_secret_fails_closed():
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        session_secret({})  # no environment declared
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        session_secret({"CAUSEMATCH_ENV": "production"})
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        session_secret({"K_SERVICE": "causematch", "CAUSEMATCH_ENV": "development"})
    with pytest.raises(RuntimeError, match="at least 32"):  # too short for production
        session_secret({"CAUSEMATCH_ENV": "production", "SESSION_SECRET": "x" * 31})
    good = "x" * 32
    assert (
        session_secret({"CAUSEMATCH_ENV": "production", "SESSION_SECRET": good}) == good
    )
    assert session_secret({"CAUSEMATCH_ENV": "development"}) == DEV_SECRET
    assert session_secret({"CAUSEMATCH_ENV": "test"}) == DEV_SECRET


def test_fake_sms_only_when_development_is_declared():
    for env in (
        {"CAUSEMATCH_FAKE_SMS": "1"},
        {"CAUSEMATCH_FAKE_SMS": "1", "CAUSEMATCH_ENV": "production"},
        {"CAUSEMATCH_FAKE_SMS": "1", "CAUSEMATCH_ENV": "development", "K_SERVICE": "x"},
    ):
        with pytest.raises(RuntimeError, match="CAUSEMATCH_ENV"):
            notify.make_verifier(env)
    ok = notify.make_verifier(
        {"CAUSEMATCH_FAKE_SMS": "1", "CAUSEMATCH_ENV": "development"}
    )
    assert isinstance(ok, notify.FakeVerifier)


def test_production_starts_with_email_alone_and_twilio_verify_is_optional():
    resend = {"EMAIL_API_KEY": "k", "EMAIL_FROM": "a@causematch.test"}
    google = {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "s" * 32}
    for prod in ({"CAUSEMATCH_ENV": "production"}, {"K_SERVICE": "causematch"}):
        for way_in in (resend, google):
            env = {**prod, **way_in}
            assert notify.make_verifier(env) is None and notify.make_sms(env) is None
            require_volunteer_sign_in(env)  # each alone is a way in
        with pytest.raises(RuntimeError, match="No volunteer sign-in"):
            require_volunteer_sign_in(prod)
        with pytest.raises(
            RuntimeError, match="No volunteer sign-in"
        ):  # never a way in
            require_volunteer_sign_in({**prod, **TWILIO, "TWILIO_VERIFY_SID": "c"})
        with pytest.raises(RuntimeError, match="half"):
            notify.make_sms({**prod, "TWILIO_ACCOUNT_SID": "a"})
    sms = {"CAUSEMATCH_ENV": "production", **TWILIO}
    assert notify.make_verifier(sms) is None  # texts without Verify: no add-a-number
    assert isinstance(notify.make_sms(sms), notify.TwilioSMS)
    full = {**sms, "TWILIO_VERIFY_SID": "c"}
    assert isinstance(notify.make_verifier(full), notify.TwilioVerifier)
    with pytest.raises(RuntimeError, match="half"):
        notify.make_verifier({"CAUSEMATCH_ENV": "production", "TWILIO_VERIFY_SID": "c"})


def test_without_twilio_verify_no_number_can_be_added_and_texts_turn_to_email(
    anon, monkeypatch
):
    vol = volunteer()
    profiles.update(id=vol.uid, email="sam@example.com")
    monkeypatch.setattr(notify, "_verifier", None)
    monkeypatch.setattr(notify, "_sms", None)
    assert "Mobile number" not in vol.get("/me/profile").text
    assert vol.post("/me/phone/send", data={"phone": "3105550123"}).status_code == 404
    assert vol.post("/me/phone/verify", data={"code": "000000"}).status_code == 404
    assert notify.channel(vol.uid) == "email"  # a number on file, but no texts


@pytest.mark.parametrize(
    "raw", ["3105550123", "(310) 555-0123", "+1 310-555-0123", "+13105550123"]
)
def test_phone_normalizes_to_e164(raw):
    assert normalize_phone(raw) == "+13105550123"


def test_unicode_digits_rejected():
    assert normalize_phone("٣١٠٥٥٥٠١٢٣٤") is None
    assert normalize_phone("0105550100") is None  # NANP: no area code starts 0 or 1


def test_fake_verifier_only_accepts_a_phone_it_sent_to():
    v = notify.FakeVerifier()
    assert not v.check("+13105550001", "000000")
    v.send("+13105550001")
    assert v.check("+13105550001", "000000")
    assert not v.check("+13105550002", "000000")
    assert not v.check("+13105550001", "111111")


def test_undeclared_environment_refuses_to_start():
    import pytest

    from app.config import require_env

    for env in (
        {},
        {"CAUSEMATCH_ENV": ""},
        {"CAUSEMATCH_ENV": "staging"},
        {"SESSION_SECRET": "x"},
    ):
        with pytest.raises(RuntimeError):
            require_env(env)
    for env in (
        {"CAUSEMATCH_ENV": "development"},
        {"CAUSEMATCH_ENV": "test"},
        {"CAUSEMATCH_ENV": "production"},
        {"K_SERVICE": "s"},
    ):
        require_env(env)


# --- the number Me's add-a-number texted a code to doesn't outlive the code ----------------


class Later:
    """app.auth's clock, moved forward."""

    def __init__(self, seconds):
        self.seconds = seconds

    def time(self):
        import time

        return time.time() + self.seconds


def session_of(client):
    import base64
    import json

    from app.auth import session_cookie

    raw = client.cookies.get(session_cookie(), "")
    payload = raw.split(".")[0] if raw else ""
    return json.loads(base64.b64decode(payload + "==")) if payload else {}


def test_the_pending_number_expires_after_15_minutes(user, monkeypatch):
    import app.auth as auth_module

    profiles.update(id=data.get_or_create_volunteer(user.phone), phone=None)
    user.post("/me/phone/send", data={"phone": "3105550160"})
    assert session_of(user)["me_pending_phone"] == "+13105550160"
    monkeypatch.setattr(auth_module, "time", Later(15 * 60 + 1))
    r = user.post("/me/phone/verify", data={"code": "000000"})
    assert r.status_code == 400 and "Enter your number to get a code." in r.text
    assert "me_pending_phone" not in session_of(user)


@pytest.mark.parametrize("env", [{}, {"CAUSEMATCH_ENV": "production"}])
def test_outside_a_demo_there_are_no_demo_buttons_and_their_posts_are_refused(
    anon, monkeypatch, env
):
    """Without config.demo_matches (production can't have it: it refuses to start)."""
    secret = "s" * 32
    for k, v in {**env, "PHONE_HASH_KEY": secret, "TASKS_TOKEN": secret,
                 "CONTACT_EMAIL": "x@causematch.test"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    for page in ("/login", "/org/login"):
        assert "Try it as" not in anon.get(page).text
    for path in ("/login/demo", "/org/login/demo"):
        r = anon.post(path, follow_redirects=False)
        assert r.status_code == 403 and "uid" not in session_of(anon), path
