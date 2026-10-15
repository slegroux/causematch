import json

from app import data
from app.db import causes, profiles
from tests.conftest import FORM


def profile_of(client):
    (p,) = profiles(where="phone = ?", where_args=[client.phone])
    return p


def test_new_user_cannot_reach_deck_until_onboarded(new_user):
    r = new_user.get("/deck", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_onboarding_form_lists_active_causes_only(new_user):
    html = new_user.get("/onboarding").text
    assert "Food insecurity" in html
    assert "Homelessness services" not in html
    assert 'name="viewport"' in html


def test_completed_onboarding_stores_profile_and_reaches_deck(new_user):
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    p = profile_of(new_user)
    assert p.neighborhood == "Chinatown"
    assert p.display_name == "Sam"
    assert p.radius_km == 16
    assert json.loads(p.causes) == ["food-insecurity"]
    assert 33.9 < p.lat < 34.2 and -118.4 < p.lon < -118.1
    assert p.adult_attested and p.onboarded_at and p.heard_from == "public"
    assert new_user.get("/deck").status_code == 200


def test_travel_time_maps_to_radius(new_user):
    new_user.post("/onboarding", data={**FORM, "travel": "any"})
    assert profile_of(new_user).radius_km == 60


def test_under_18_blocked(new_user):
    form = {k: v for k, v in FORM.items() if k != "adult"}
    r = new_user.post("/onboarding", data=form, follow_redirects=False)
    assert r.status_code == 200
    assert "Causematch is for adults 18+" in r.text
    assert not profile_of(new_user).onboarded_at
    assert (
        new_user.get("/deck", follow_redirects=False).headers["location"]
        == "/onboarding"
    )


def test_zip_outside_la_rejected(new_user):
    r = new_user.post(
        "/onboarding", data={**FORM, "zip": "10001"}, follow_redirects=False
    )
    assert "only in Los Angeles for now" in r.text
    assert not profile_of(new_user).onboarded_at


def test_inactive_cause_rejected_if_posted(new_user):
    r = new_user.post(
        "/onboarding",
        data={**FORM, "causes": ["homelessness-services"]},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert not profile_of(new_user).onboarded_at


def test_unknown_cause_rejected(new_user):
    r = new_user.post(
        "/onboarding",
        data={**FORM, "causes": ["food-insecurity", "nope"]},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert not profile_of(new_user).onboarded_at


def test_consent_required(new_user):
    form = {k: v for k, v in FORM.items() if k != "consent"}
    r = new_user.post("/onboarding", data=form, follow_redirects=False)
    assert r.status_code == 200
    assert not profile_of(new_user).onboarded_at


def test_newly_activated_cause_is_offered(new_user):
    causes.update(slug="homelessness-services", active=True)
    try:
        assert "Homelessness services" in new_user.get("/onboarding").text
        assert [c.slug for c in data.active_causes()][-1] == "homelessness-services"
    finally:
        causes.update(slug="homelessness-services", active=False)


def test_onboarded_user_login_goes_to_deck(user):
    assert (
        user.get("/onboarding", follow_redirects=False).headers["location"] == "/deck"
    )


def test_email_is_optional_and_stored_when_given(new_user):
    new_user.post("/onboarding", data={**FORM, "email": "sam@example.com"})
    assert profile_of(new_user).email == "sam@example.com"


def test_onboarding_without_email_succeeds(new_user):
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    assert not profile_of(new_user).email


def test_malformed_email_rejected(new_user):
    r = new_user.post(
        "/onboarding", data={**FORM, "email": "nope"}, follow_redirects=False
    )
    assert r.status_code == 200 and "email" in r.text
    assert not profile_of(new_user).onboarded_at
