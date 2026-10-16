import json

import pytest
from conftest import FORM, close_all_openings, make_opening

from app import data
from app.db import causes, places, profiles


def profile_of(client):
    (p,) = profiles(where="phone = ?", where_args=[client.phone])
    return p


def test_new_user_cannot_reach_deck_until_onboarded(new_user):
    r = new_user.get("/deck", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_onboarding_form_lists_active_causes_only(homelessness_off, new_user):
    html = new_user.get("/onboarding").text
    assert "Food insecurity" in html
    assert "Homelessness services" not in html
    assert 'name="viewport"' in html


def test_completed_onboarding_stores_profile_and_reaches_deck(new_user):
    r = new_user.post("/onboarding", data=FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    p = profile_of(new_user)
    assert p.display_name == "Sam"
    assert p.radius_km == 16
    assert json.loads(p.causes) == ["food-insecurity"]
    assert 33.9 < p.lat < 34.2 and -118.4 < p.lon < -118.1
    assert p.adult_attested and p.onboarded_at and p.heard_from == ""
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
    assert "only in Los Angeles County for now" in r.text
    assert not profile_of(new_user).onboarded_at


def test_inactive_cause_rejected_if_posted(homelessness_off, new_user):
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


def test_newly_activated_cause_is_offered(homelessness_off, new_user):
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
    assert json.loads(p.availability) == []  # set later in Me
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
    assert "Only you see these:" in page


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
        ("place1_where", "Mar\u2028Vista"),  # line separator
        ("first_name", "Sam\u0301\u0301\u0301\u0301"),  # 4 combining marks in a row
        ("first_name", "Sa\u200dm"),  # a zero-width joiner, in a one-line answer
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


@pytest.mark.parametrize("name", ["Nguy\u1ec5n", "Nguye\u0302\u0303n"])  # composed, not
def test_vietnamese_names_are_fine_composed_or_not(new_user, name):
    r = new_user.post(
        "/onboarding", data={**FORM, "first_name": name}, follow_redirects=False
    )
    assert r.status_code == 303
    assert profile_of(new_user).display_name == name


def test_privacy_page_says_exactly_what_is_shared_and_what_is_private(anon):
    from html import unescape

    from app.choices import SHARED_WITH_NONPROFIT

    page = unescape(anon.get("/privacy").text)
    private = page.split("Only you see these:")[1].split(". They decide")[0]
    shared = page.split('A nonprofit sees only what "Who sees what" lists')[1]
    shared = shared.split("(you can change it")[0]
    assert SHARED_WITH_NONPROFIT in page
    # each thing the consent shares is named as shared, and none of it as private
    for item, words in (
        ("first name", "first name"),
        ("causes", "causes you love or are open to"),
        ("availability", "general summary of when you're free"),
        ("how often", "how often you'd like to help"),
        ("languages", "languages you speak"),
        ("neighborhood", "neighborhood you show nonprofits"),
        ("photo", "your photo"),
        ("pronouns", "pronouns, bio and interests"),
    ):
        assert item in SHARED_WITH_NONPROFIT
        assert words in shared, item
        assert item not in private, item
    for kept in ("places", "device", "rated", "specific dates", "skills"):
        assert kept in private, kept


def test_a_new_volunteer_sees_what_their_home_reaches(new_user):
    """End to end, with nothing changed after onboarding: Home is 90012 by car within
    30 minutes (16 km), so a shift nearby shows and one across the county doesn't."""
    from app.geo import neighborhood_latlon

    new_user.post("/onboarding", data=FORM)
    close_all_openings()
    for title, area in (("near", "Chinatown"), ("far", "Santa Monica")):
        lat, lon = neighborhood_latlon(area)[1]
        make_opening(
            title, causes=["food-insecurity"], lat=lat, lon=lon, neighborhood=area,
            starts_at="2099-01-03T09:00",
        )  # fmt: skip
    (card,) = data.deck(profile_of(new_user).id, 5)
    assert card["title"] == "near" and card["travel"].endswith("by car from Home")
    assert "near" in new_user.get("/deck").text


@pytest.mark.parametrize(
    "where, area",
    [("９００１２", "90012"), ("90012-1234", "90012"), (" 90012 ", "90012")],
)
def test_zip_forms_people_type_are_read(new_user, where, area):
    r = new_user.post(
        "/onboarding", data={**FORM, "place1_where": where}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    (home,) = places(where="user_id = ?", where_args=[profile_of(new_user).id])
    assert home.area == area


@pytest.mark.parametrize(
    "where, message",
    [
        ("²³", "Enter a five-digit ZIP code"),
        ("9001-2", "Enter a five-digit ZIP code"),
        ("x" * 81, "too long"),
    ],
)
def test_odd_places_get_a_clear_message(new_user, where, message):
    r = new_user.post(
        "/onboarding", data={**FORM, "place1_where": where}, follow_redirects=False
    )
    assert r.status_code == 200 and message in r.text


def two_cards(client):
    return [c["id"] for c in data.deck(profile_of(client).id, 2)]


def test_the_short_form_has_only_what_the_first_card_needs(new_user):
    page = new_user.get("/onboarding").text
    for gone in ("last_name", "email", "neighborhood", "availability", "heard_from",
                 "volunteered_last_year", "other_causes"):  # fmt: skip
        assert f'name="{gone}"' not in page
    assert 'name="adult"' in page and 'name="consent"' in page
    assert 'href="/privacy"' in page and "I agree to the" in page


def test_the_short_form_saves_and_the_deck_has_a_card_without_availability(new_user):
    new_user.post("/onboarding", data=FORM)
    p = profile_of(new_user)
    assert json.loads(p.availability) == [] and p.heard_from == ""
    assert new_user.get("/deck").text.count("data-swipe-card") >= 1


def test_the_after_first_like_card_shows_once_and_can_be_skipped(new_user):
    new_user.post("/onboarding", data=FORM)
    like = {"opening_id": two_cards(new_user)[0], "liked": "1"}
    r = new_user.post("/swipe", data=like, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/more"
    assert "How did you hear" in new_user.get("/onboarding/more").text
    r = new_user.post("/onboarding/more", data={"skip": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    assert profile_of(new_user).heard_from == "skipped"
    r = new_user.post(
        "/swipe",
        data={**like, "opening_id": two_cards(new_user)[1]},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/deck"  # not asked again
    assert new_user.get("/onboarding/more", follow_redirects=False).status_code == 303


def test_the_after_first_like_card_saves_its_answers(new_user):
    new_user.post("/onboarding", data=FORM)
    new_user.post("/swipe", data={"opening_id": two_cards(new_user)[1], "liked": "1"})
    new_user.post(
        "/onboarding/more",
        data={"heard_from": "public", "volunteered_last_year": "yes"},
    )
    p = profile_of(new_user)
    assert (p.heard_from, p.volunteered_last_year) == ("public", "yes")
    assert (
        new_user.post("/onboarding/more", data={"heard_from": "x"}).status_code == 200
    )
