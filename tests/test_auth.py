import pytest

from app import notify
from app.auth import normalize_phone, session_secret
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
    r = anon.post(
        "/login/verify",
        data={"phone": "3105550003", "code": "123456"},
        follow_redirects=False,
    )
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
    from starlette.testclient import TestClient

    from main import app

    sign_in(anon, "3105550004")
    other = TestClient(app)
    sign_in(other, "3105550004")
    assert anon.get("/onboarding", follow_redirects=False).status_code == 200
    assert other.get("/onboarding", follow_redirects=False).status_code == 200


def test_logout_signs_out(user):
    assert user.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert user.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_production_without_session_secret_refuses_to_start():
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        session_secret({"CAUSEMATCH_ENV": "production"})
    assert (
        session_secret({"CAUSEMATCH_ENV": "production", "SESSION_SECRET": "x"}) == "x"
    )
    assert session_secret({})  # development default


def test_production_without_twilio_refuses_to_start():
    with pytest.raises(RuntimeError, match="Twilio"):
        notify.make_verifier({"CAUSEMATCH_ENV": "production"})
    with pytest.raises(RuntimeError, match="production"):
        notify.make_verifier(
            {"CAUSEMATCH_ENV": "production", "CAUSEMATCH_FAKE_SMS": "1"}
        )
    cfg = {
        "TWILIO_ACCOUNT_SID": "a",
        "TWILIO_AUTH_TOKEN": "b",
        "TWILIO_VERIFY_SID": "c",
    }
    assert isinstance(
        notify.make_verifier({"CAUSEMATCH_ENV": "production", **cfg}),
        notify.TwilioVerifier,
    )
