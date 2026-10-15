from datetime import UTC, datetime, timedelta

from conftest import coordinator_for, org_sign_in
from starlette.testclient import TestClient

from app import notify
from app.db import login_tokens
from main import app

SENT = "If that address belongs to a coordinator, a sign-in link is on its way"


def request_link(client, email, **kw):
    return client.post("/org/login/send", data={"email": email}, **kw)


def verify(client, token):
    return client.post(
        "/org/login/verify", data={"token": token}, follow_redirects=False
    )


def token_of(body):
    return body.split("token=")[1].split()[0]


def test_unknown_email_gets_same_message_and_no_email(anon):
    coordinator_for(0, "known@example.org")
    known = request_link(anon, "known@example.org")
    unknown = request_link(anon, "nobody@example.org")
    assert SENT in known.text and SENT in unknown.text
    assert known.status_code == unknown.status_code == 200
    assert [m[0] for m in notify.get_emailer().sent] == ["known@example.org"]


def test_volunteer_email_does_not_get_a_link(user):
    from app.db import profiles

    profiles.update(
        id=profiles(where="role = 'volunteer'")[0].id, email="vol@example.org"
    )
    r = request_link(TestClient(app), "vol@example.org")
    assert SENT in r.text
    assert notify.get_emailer().sent == []


def test_token_works_once(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    token = token_of(notify.get_emailer().sent[-1][2])
    first = verify(anon, token)
    assert first.status_code == 303 and first.headers["location"] == "/org"
    assert anon.get("/org").status_code == 200
    other = TestClient(app)
    second = verify(other, token)
    assert second.status_code == 200 and "expired or was already used" in second.text
    assert other.get("/org", follow_redirects=False).status_code == 303


def test_expired_token_fails(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    token = token_of(notify.get_emailer().sent[-1][2])
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds")
    for t in login_tokens():
        login_tokens.update(token_hash=t.token_hash, expires_at=past)
    r = verify(anon, token)
    assert r.status_code == 200 and "expired" in r.text
    assert anon.get("/org", follow_redirects=False).status_code == 303


def test_token_has_fifteen_minute_expiry(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    (t,) = login_tokens()
    left = datetime.fromisoformat(t.expires_at) - datetime.now(UTC)
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)


def test_token_is_stored_hashed(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    token = token_of(notify.get_emailer().sent[-1][2])
    (t,) = login_tokens()
    assert token not in str(t) and t.token_hash != token and len(t.token_hash) == 64
    assert len(token) >= 43  # 32 random bytes, urlsafe


def test_new_link_invalidates_the_old_one(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    old = token_of(notify.get_emailer().sent[-1][2])
    request_link(anon, email)
    r = verify(anon, old)
    assert r.status_code == 200 and "expired or was already used" in r.text
    assert anon.get("/org", follow_redirects=False).status_code == 303
    assert verify(anon, token_of(notify.get_emailer().sent[-1][2])).status_code == 303


def test_garbage_tokens_fail(anon):
    for t in ("", "x", "a" * 5000, "%00"):
        r = verify(anon, t)
        assert r.status_code == 200 and "expired" in r.text


def test_link_uses_public_base_url_not_host_header(anon):
    _, email = coordinator_for(0)
    request_link(
        anon,
        email,
        headers={"host": "evil.example", "x-forwarded-host": "evil.example"},
    )
    body = notify.get_emailer().sent[-1][2]
    assert "https://causematch.test/org/login/verify?token=" in body
    assert "evil.example" not in body


def test_sign_in_clears_old_session_and_uses_session_version(coord):
    from app import data
    from app.db import profiles

    uid = profiles(where="role = 'coordinator'")[0].id
    assert coord.get("/org").status_code == 200
    data.sign_out_everywhere(uid)
    assert coord.get("/org", follow_redirects=False).headers["location"] == "/org/login"


def test_signed_out_org_redirects_to_org_login(anon):
    r = anon.get("/org", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org/login"
    assert anon.get("/org/login").status_code == 200


def test_bad_email_shape_is_rejected_without_a_lookup(anon):
    r = request_link(anon, "not-an-email")
    assert "Enter your email address" in r.text
    assert notify.get_emailer().sent == []


def test_fake_email_refused_outside_dev():
    import pytest

    with pytest.raises(RuntimeError):
        notify.make_emailer(
            {"CAUSEMATCH_FAKE_EMAIL": "1", "CAUSEMATCH_ENV": "production"}
        )
    with pytest.raises(RuntimeError):
        notify.make_emailer(
            {"CAUSEMATCH_FAKE_EMAIL": "1", "K_SERVICE": "x", "CAUSEMATCH_ENV": "test"}
        )
    with pytest.raises(RuntimeError):
        notify.make_emailer({"CAUSEMATCH_ENV": "production"})
    with pytest.raises(RuntimeError):
        notify.make_emailer({"CAUSEMATCH_ENV": "development"})
    assert isinstance(
        notify.make_emailer(
            {
                "EMAIL_API_KEY": "k",
                "EMAIL_FROM": "a@b.org",
                "CAUSEMATCH_ENV": "production",
            }
        ),
        notify.ResendEmailer,
    )


def test_public_base_url_rules():
    import pytest

    from app.config import public_base_url

    with pytest.raises(RuntimeError):
        public_base_url({"CAUSEMATCH_ENV": "production"})
    with pytest.raises(RuntimeError):
        public_base_url(
            {"CAUSEMATCH_ENV": "production", "PUBLIC_BASE_URL": "http://x.org"}
        )
    assert (
        public_base_url(
            {"PUBLIC_BASE_URL": "https://x.org/", "CAUSEMATCH_ENV": "production"}
        )
        == "https://x.org"
    )
    assert public_base_url({"CAUSEMATCH_ENV": "development"}).startswith(
        "http://localhost"
    )


def test_org_sign_in_helper_roundtrip():
    _, email = coordinator_for(1)
    c = TestClient(app)
    org_sign_in(c, email)
    assert c.get("/org").status_code == 200


def test_get_and_head_do_not_consume_the_token(anon):
    _, email = coordinator_for(0)
    request_link(anon, email)
    token = token_of(notify.get_emailer().sent[-1][2])
    for method in (anon.get, anon.head, anon.get):
        r = method(f"/org/login/verify?token={token}", follow_redirects=False)
        assert r.status_code == 200
    assert 'name="token"' in anon.get(f"/org/login/verify?token={token}").text
    assert [t.used_at for t in login_tokens()] == [None]
    assert (
        anon.get("/org", follow_redirects=False).status_code == 303
    )  # still signed out
    assert verify(anon, token).status_code == 303
    assert login_tokens()[0].used_at
    assert verify(TestClient(app), token).status_code == 200


def test_multipart_email_is_400_not_500(anon):
    r = anon.post("/org/login/send", files={"email": ("a.txt", b"x@y.org")})
    assert r.status_code == 400
    r = anon.post("/org/login/verify", files={"token": ("a.txt", b"x")})
    assert r.status_code == 400


def test_membership_removed_between_send_and_verify_is_refused(anon):
    from app.db import org_members

    _, email = coordinator_for(0)
    request_link(anon, email)
    token = token_of(notify.get_emailer().sent[-1][2])
    org_members.delete_where()
    r = verify(anon, token)
    assert r.status_code == 200 and "expired or was already used" in r.text
    assert anon.get("/org", follow_redirects=False).status_code == 303


def test_link_is_sent_after_the_response_and_a_send_failure_changes_nothing(
    anon, monkeypatch
):
    _, email = coordinator_for(0)

    def boom(*a):
        raise RuntimeError("emailer down")

    monkeypatch.setattr(notify, "send_login_link", boom)
    known = request_link(anon, email)
    unknown = request_link(anon, "nobody@example.org")
    assert known.status_code == unknown.status_code == 200
    assert SENT in known.text and known.text == unknown.text
