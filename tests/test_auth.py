import pytest
from starlette.testclient import TestClient

from app import data, notify
from app.auth import DEV_SECRET, normalize_phone, session_secret
from app.db import profiles
from main import app
from tests.conftest import sign_in


def test_signed_out_deck_redirects_to_login(anon):
    r = anon.get("/deck", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_is_public(anon):
    assert anon.get("/login").status_code == 200


def test_code_flow_signs_in(anon):
    r = sign_in(anon, "3105550001")
    assert r.headers["location"] == "/onboarding"
    assert anon.get("/onboarding").status_code == 200


def test_code_is_sent_through_the_verifier(anon):
    anon.post("/login/send", data={"phone": "(310) 555-0002"})
    assert ("+13105550002", "000000") in notify.get_verifier().sent


def test_wrong_code_rejected(anon):
    anon.post("/login/send", data={"phone": "3105550003"})
    r = anon.post("/login/verify", data={"code": "123456"}, follow_redirects=False)
    assert r.status_code == 200
    assert "didn't work" in r.text
    assert anon.get("/deck", follow_redirects=False).headers["location"] == "/login"


@pytest.mark.parametrize(
    "bad", ["", "12345", "abcdefghij", "+44 20 7946 0958", "0105550100", "31055501234"]
)
def test_bad_phone_rejected(anon, bad):
    r = anon.post("/login/send", data={"phone": bad})
    assert r.status_code == 200
    assert "US mobile number" in r.text


@pytest.mark.parametrize(
    "raw", ["3105550123", "(310) 555-0123", "+1 310-555-0123", "+13105550123"]
)
def test_phone_normalizes_to_e164(raw):
    assert normalize_phone(raw) == "+13105550123"


def test_same_phone_signs_into_same_profile(anon):
    other = TestClient(app)
    sign_in(anon, "3105550004")
    sign_in(other, "3105550004")
    rows = profiles(where="phone = ?", where_args=["+13105550004"])
    assert len(rows) == 1
    assert anon.get("/onboarding").status_code == 200
    assert other.get("/onboarding").status_code == 200


def test_verify_uses_only_the_phone_the_code_was_sent_to(anon):
    anon.post("/login/send", data={"phone": "3105550005"})
    r = anon.post(
        "/login/verify",
        data={"phone": "3105550006", "code": "000000"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert profiles(where="phone = ?", where_args=["+13105550006"]) == []
    assert len(profiles(where="phone = ?", where_args=["+13105550005"])) == 1


def test_verify_without_send_rejected(anon):
    r = anon.post(
        "/login/verify",
        data={"phone": "3105550007", "code": "000000"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert profiles(where="phone = ?", where_args=["+13105550007"]) == []


def test_fake_verifier_only_accepts_a_phone_it_sent_to():
    v = notify.FakeVerifier()
    assert not v.check("+13105550001", "000000")
    v.send("+13105550001")
    assert v.check("+13105550001", "000000")
    assert not v.check("+13105550002", "000000")
    assert not v.check("+13105550001", "111111")


def test_junk_code_rejected(anon):
    anon.post("/login/send", data={"phone": "3105550008"})
    r = anon.post("/login/verify", data={"code": "abc"})
    assert "6-digit" in r.text


def test_unicode_digits_rejected():
    assert normalize_phone("٣١٠٥٥٥٠١٢٣٤") is None
    assert normalize_phone("1 310 555 0123") == "+13105550123"


def test_soft_deleted_profile_frees_the_number(anon):
    sign_in(anon, "3105550009")
    (old,) = profiles(where="phone = ?", where_args=["+13105550009"])
    profiles.update(id=old.id, deleted_at="2026-10-15T00:00:00")
    again = TestClient(app)
    sign_in(again, "3105550009")
    live = profiles(
        where="phone = ? AND deleted_at IS NULL", where_args=["+13105550009"]
    )
    assert len(live) == 1 and live[0].id != old.id
    assert anon.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_sign_out_everywhere_revokes_other_sessions(user):
    assert user.get("/deck").status_code == 200
    (p,) = profiles(where="phone = ?", where_args=[user.phone])
    data.sign_out_everywhere(p.id)
    assert user.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_session_cookie_lasts_30_days(anon):
    anon.post("/login/send", data={"phone": "3105550010"})
    r = anon.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert f"Max-Age={30 * 24 * 3600}" in r.headers["set-cookie"]


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
    assert (
        session_secret({"CAUSEMATCH_ENV": "production", "SESSION_SECRET": "x"}) == "x"
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


def test_production_without_twilio_refuses_to_start():
    with pytest.raises(RuntimeError, match="Twilio"):
        notify.make_verifier({"CAUSEMATCH_ENV": "production"})
    with pytest.raises(RuntimeError, match="Twilio"):
        notify.make_verifier({"K_SERVICE": "causematch"})
    cfg = {
        "TWILIO_ACCOUNT_SID": "a",
        "TWILIO_AUTH_TOKEN": "b",
        "TWILIO_VERIFY_SID": "c",
    }
    assert isinstance(
        notify.make_verifier({"CAUSEMATCH_ENV": "production", **cfg}),
        notify.TwilioVerifier,
    )


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
