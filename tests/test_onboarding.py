import json

import pytest

from app import data
from app.db import causes, places, profiles
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
    new_user.post("/onboarding", data={**FORM, "place1_minutes": "0"})
    assert profile_of(new_user).radius_km == 60
    (home,) = places(where="user_id = ?", where_args=[profile_of(new_user).id])
    assert home.max_minutes == 0


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
        "/onboarding", data={**FORM, "place1_where": "10001"}, follow_redirects=False
    )
    assert "only in Los Angeles for now" in r.text
    assert not profile_of(new_user).onboarded_at


def test_inactive_cause_rejected_if_posted(new_user):
    r = new_user.post(
        "/onboarding",
        data={
            **{k: v for k, v in FORM.items() if not k.startswith("cause_")},
            "cause_homelessness-services": "love",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert not profile_of(new_user).onboarded_at


def test_unknown_cause_rejected(new_user):
    r = new_user.post(
        "/onboarding",
        data={**FORM, "cause_food-insecurity": "adore"},
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


def test_a_day_grid_slot_is_accepted_and_shown(new_user):
    page = new_user.get("/onboarding").text
    assert 'name="availability" value="sat_evening"' in page and "Evening" in page
    r = new_user.post(
        "/onboarding",
        data={**FORM, "availability": ["sat_evening"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert json.loads(profile_of(new_user).availability) == ["sat_evening"]


def test_onboarding_saves_home_ratings_and_days(new_user):
    r = new_user.post(
        "/onboarding",
        data={**FORM, "cause_environment-parks": "open", "cause_animal-shelters": "no"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/deck"
    p = profile_of(new_user)
    assert json.loads(p.cause_prefs) == {
        "food-insecurity": "love",
        "environment-parks": "open",
        "animal-shelters": "no",
    }
    assert json.loads(p.causes) == ["food-insecurity", "environment-parks"]
    assert json.loads(p.availability) == ["sat_morning", "sun_morning"]
    (home,) = places(where="user_id = ?", where_args=[p.id])
    assert (home.label, home.area, home.mode, home.max_minutes) == (
        "Home",
        "90012",
        "car",
        30,
    )
    assert (p.lat, p.lon) == (home.lat, home.lon)
    page = new_user.get("/me/preferences").text  # the same answers, editable in Me
    assert 'name="place1_where" value="90012"' in page
    assert 'name="cause_animal-shelters" value="no" checked' in page


def test_onboarding_takes_a_neighborhood_or_a_rounded_device_location(new_user):
    new_user.post("/onboarding", data={**FORM, "place1_where": "mar vista"})
    (home,) = places(where="user_id = ?", where_args=[profile_of(new_user).id])
    assert home.area == "Mar Vista"


def test_onboarding_rounds_a_device_location(new_user):
    r = new_user.post(
        "/onboarding",
        data={
            **FORM,
            "place1_where": "My location",
            "place1_lat": "34.056789",
            "place1_lon": "-118.234561",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    (home,) = places(where="user_id = ?", where_args=[profile_of(new_user).id])
    assert (home.lat, home.lon, home.area) == (34.06, -118.23, "My location")


def test_an_onboarding_error_echoes_only_a_rounded_point(new_user):
    r = new_user.post(
        "/onboarding",
        data={
            **FORM,
            "place1_where": "My location",
            "place1_lat": "34.056789",
            "place1_lon": "-118.234561",
            "consent": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200 and "agree to the privacy policy" in r.text
    assert "056789" not in r.text and "234561" not in r.text
    assert 'value="34.06"' in r.text


@pytest.mark.parametrize(
    "where, message",
    [
        ("9001", "Enter a five-digit ZIP code, like 90012."),
        ("900123", "Enter a five-digit ZIP code, like 90012."),
        ("", "Tell us where you'll help from"),
        ("Atlantis", "couldn't find"),
    ],
)
def test_a_bad_place_is_refused_with_a_clear_message(new_user, where, message):
    r = new_user.post(
        "/onboarding", data={**FORM, "place1_where": where}, follow_redirects=False
    )
    assert r.status_code == 200 and message in r.text
    assert not profile_of(new_user).onboarded_at


def test_onboarding_allows_location_and_has_no_inline_script(new_user):
    r = new_user.get("/onboarding")
    assert "geolocation=(self)" in r.headers["permissions-policy"]
    assert '<script src="/static/geo.js" defer>' in r.text
    assert "<script>" not in r.text and " style=" not in r.text


def test_privacy_page_lists_places_and_preferences_as_private(anon):
    page = anon.get("/privacy").text
    assert "places" in page and "never an address" in page
    assert "Only you see your preferences" in page


def test_onboarding_once_done_cannot_be_posted_again(user):
    """Later changes go through /me/preferences; a second POST changes nothing."""
    before = profile_of(user)
    r = user.post(
        "/onboarding",
        data={**FORM, "first_name": "Mallory", "neighborhood": "Elsewhere"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    after = profile_of(user)
    assert (after.display_name, after.neighborhood, after.onboarded_at) == (
        before.display_name,
        before.neighborhood,
        before.onboarded_at,
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("first_name", "Sa\u200bm"),  # zero-width space
        ("first_name", "\u3164"),  # Hangul filler: looks blank
        ("neighborhood", "Echo\u202ePark"),  # right-to-left override
        ("other_causes", "owls\u0007"),  # a control character
        ("place1_where", "Mar\u2028Vista"),  # line separator
    ],
)
def test_hidden_characters_are_refused(new_user, field, value):
    r = new_user.post(
        "/onboarding", data={**FORM, field: value}, follow_redirects=False
    )
    assert r.status_code == 200 and "hidden or special characters" in r.text
    assert not profile_of(new_user).onboarded_at


def test_accented_and_non_latin_names_are_fine(new_user):
    r = new_user.post(
        "/onboarding",
        data={**FORM, "first_name": "José 李", "neighborhood": "Boyle Heights"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert profile_of(new_user).display_name == "José 李"
