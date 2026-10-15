import pytest
from conftest import sign_in

VOLUNTEER_PAGES = ["/", "/deck", "/onboarding"]
ORG_PAGES = ["/org", "/org/openings/new"]


@pytest.mark.parametrize("path", ORG_PAGES)
def test_volunteer_has_no_org_access(user, path):
    r = user.get(path, follow_redirects=False)
    assert r.status_code == 403
    assert "Your openings" not in r.text and "Post an opening" not in r.text


def test_volunteer_cannot_post_or_close(user):
    assert (
        user.post(
            "/org/openings/new", data={"title": "x"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert user.post("/org/openings/1/close", follow_redirects=False).status_code == 403


def test_not_yet_onboarded_volunteer_has_no_org_access(new_user):
    assert new_user.get("/org", follow_redirects=False).status_code == 403


@pytest.mark.parametrize("path", VOLUNTEER_PAGES)
def test_coordinator_has_no_volunteer_pages(coord, path):
    r = coord.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


def test_coordinator_onboarding_post_is_refused(coord):
    r = coord.post("/onboarding", data={"first_name": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


def test_coordinator_without_membership_gets_nothing(coord):
    from app.db import org_members

    org_members.delete_where()
    assert coord.get("/org", follow_redirects=False).status_code == 403


def test_admin_role_has_neither_side(anon):
    from app.db import profiles

    sign_in(anon)
    profiles.update(
        id=profiles(where="phone = ?", where_args=[anon.phone])[0].id, role="admin"
    )
    assert anon.get("/deck", follow_redirects=False).status_code == 403
    assert anon.get("/org", follow_redirects=False).status_code == 403


def test_logout_works_for_every_role(coord, user):
    assert coord.post("/logout", follow_redirects=False).status_code == 303
    assert coord.get("/org", follow_redirects=False).headers["location"] == "/org/login"
    assert user.post("/logout", follow_redirects=False).status_code == 303
    assert user.get("/deck", follow_redirects=False).headers["location"] == "/login"


def test_phone_sign_in_does_not_hand_a_volunteer_a_coordinator_session(anon):
    sign_in(anon)
    assert anon.get("/org", follow_redirects=False).status_code == 403
