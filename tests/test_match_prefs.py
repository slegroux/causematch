"""The deck under a volunteer's preferences: places (and, as they land, when and work)."""

import json

import pytest

from app import data, geo, match
from app.db import Opening, now, openings, orgs, places, profiles

DOWNTOWN = geo.neighborhood_latlon("Downtown")[1]
WEST_LA = geo.neighborhood_latlon("West Los Angeles")[1]
PASADENA = geo.neighborhood_latlon("Pasadena")[1]
BASE = {
    "cause_food-insecurity": "love",
    "availability": ["weekend_morning"],
    "frequency": "weekly",
}


def uid_of(client):
    return data.get_or_create_volunteer(client.phone)


def add(title, where, starts_at="2099-01-03T09:00", **kw):
    lat, lon = where
    fields = {
        "org_id": orgs()[0].id,
        "title": title,
        "about": "",
        "causes": '["food-insecurity"]',
        "skills": "[]",
        "lat": lat,
        "lon": lon,
        "neighborhood": "Somewhere",
        "remote": False,
        "starts_at": starts_at,
        "recurrence": "one_time",
        "slots": 3,
        "status": "open",
        "created_at": now(),
    }
    fields.update(kw)
    return openings.insert(Opening(**fields)).id


def place_fields(n, label, where, mode="car", minutes="15", lat="", lon=""):
    return {
        f"place{n}_label": label,
        f"place{n}_where": where,
        f"place{n}_mode": mode,
        f"place{n}_minutes": minutes,
        f"place{n}_lat": lat,
        f"place{n}_lon": lon,
    }


@pytest.fixture
def bare(user):
    for o in openings():
        openings.update(id=o.id, status="closed")
    return user


def save(client, *place_forms, **extra):
    form = {**BASE, **extra}
    for p in place_forms:
        form.update(p)
    return client.post("/me/preferences", data=form, follow_redirects=False)


def cards(client):
    return {c["title"]: c for c in data.deck(uid_of(client), 50)}


# --- places --------------------------------------------------------------------------------


def test_place_near_work_but_far_from_home_is_shown_from_work(bare):
    r = save(
        bare,
        place_fields(1, "Home", "West Los Angeles"),
        place_fields(2, "Work", "downtown"),  # any case
    )
    assert r.status_code == 303, r.text
    add("by the office", DOWNTOWN)
    add("by home", WEST_LA)
    add("pasadena", PASADENA)  # ~ 14 km from Downtown, past 15 min by car
    got = cards(bare)
    assert set(got) == {"by the office", "by home"}
    assert got["by the office"]["travel"].endswith("by car from Work")
    assert got["by home"]["travel"].endswith("by car from Home")
    top = data.deck(uid_of(bare), 1)[0]["travel"]
    assert f"About {top}" in bare.get("/deck").text  # the card says it


def test_place_the_quickest_place_labels_the_card(bare):
    # Work is nearer but on foot; Home is farther but by car, so quicker
    save(
        bare,
        place_fields(1, "Home", "Koreatown", minutes="0"),
        place_fields(2, "Work", "Chinatown", mode="walk", minutes="0"),
    )
    add("downtown", DOWNTOWN)
    travel = cards(bare)["downtown"]["travel"]
    assert travel.endswith("by car from Home"), travel


def test_place_a_device_point_is_stored_with_two_decimals(bare):
    r = save(
        bare,
        place_fields(
            1, "Home", "My location", lat="34.056789", lon="-118.234561", minutes="0"
        ),
    )
    assert r.status_code == 303, r.text
    (p,) = places(where="user_id = ?", where_args=[uid_of(bare)])
    assert (p.lat, p.lon) == (34.06, -118.23)
    assert round(p.lat, 2) == p.lat and round(p.lon, 2) == p.lon
    assert p.area == "My location"
    profile = profiles[uid_of(bare)]
    assert (profile.lat, profile.lon) == (34.06, -118.23)
    page = bare.get("/me/preferences").text
    assert "34.06" not in page and "118.23" not in page  # never sent back to the page
    assert 'value="My location"' in page


def test_place_unchanged_text_keeps_the_saved_point(bare):
    save(bare, place_fields(1, "Home", "My location", lat="34.1", lon="-118.3"))
    save(bare, place_fields(1, "Home", "My location", minutes="45"))  # no new point
    (p,) = places(where="user_id = ?", where_args=[uid_of(bare)])
    assert (p.lat, p.lon, p.max_minutes) == (34.1, -118.3, 45)


@pytest.mark.parametrize(
    "lat, lon", [("40.71", "-74.01"), ("nan", "-118.2"), ("x", "y"), ("1e400", "0")]
)
def test_place_a_device_point_outside_la_is_refused(bare, lat, lon):
    r = save(bare, place_fields(1, "Home", "My location", lat=lat, lon=lon))
    assert r.status_code == 200 and "isn't in Los Angeles County" in r.text
    assert places(where="user_id = ?", where_args=[uid_of(bare)]) == []


def test_place_a_zip_and_a_neighborhood_resolve(bare):
    save(bare, place_fields(1, "Home", "90066"), place_fields(2, "Gym", "silverlake"))
    home, gym = places(where="user_id = ?", where_args=[uid_of(bare)], order_by="id")
    assert (home.area, (home.lat, home.lon)) == ("90066", geo.zip_latlon("90066"))
    assert (gym.label, gym.area) == ("Gym", "Silverlake")


def test_place_an_unknown_name_saves_nothing(bare):
    r = save(bare, place_fields(1, "Home", "Atlantis"))
    assert r.status_code == 200 and "couldn't find “Atlantis”" in r.text
    assert 'value="Atlantis"' in r.text  # what they typed stays in the field
    assert places(where="user_id = ?", where_args=[uid_of(bare)]) == []


@pytest.mark.parametrize(
    "over",
    [
        {"place1_mode": "teleport"},
        {"place1_minutes": "20"},
        {"place1_minutes": "soon"},
    ],
)
def test_place_an_unknown_mode_or_time_saves_nothing(bare, over):
    r = save(bare, {**place_fields(1, "Home", "Venice"), **over})
    assert r.status_code == 200 and "wasn't recognized" in r.text
    assert places(where="user_id = ?", where_args=[uid_of(bare)]) == []


def test_place_the_main_place_is_required_and_names_are_short(bare):
    r = save(bare, place_fields(1, "Home", ""), place_fields(2, "Work", "Venice"))
    assert "Tell us your main place" in r.text
    r = save(bare, place_fields(1, "x" * 21, "Venice"))
    assert "up to 20 characters" in r.text
    assert places(where="user_id = ?", where_args=[uid_of(bare)]) == []


def test_place_emptying_the_second_place_removes_it(bare):
    save(bare, place_fields(1, "Home", "Venice"), place_fields(2, "Work", "Downtown"))
    save(bare, place_fields(1, "Home", "Venice"), place_fields(2, "Work", ""))
    assert [
        p.label for p in places(where="user_id = ?", where_args=[uid_of(bare)])
    ] == ["Home"]


def test_place_no_more_than_two(bare):
    save(
        bare,
        place_fields(1, "Home", "Venice"),
        place_fields(2, "Work", "Downtown"),
        place_fields(3, "Gym", "Palms"),
    )
    assert len(places(where="user_id = ?", where_args=[uid_of(bare)])) == 2


def test_place_a_profile_from_sign_up_shows_its_home(bare):
    """No saved place yet: the sign-up ZIP is Home, by car, at the sign-up travel time."""
    profiles.update(id=uid_of(bare), radius_km=16)
    page = bare.get("/me/preferences").text
    assert 'name="place1_where" value="Chinatown"' in page
    assert 'name="place1_mode" value="car" checked' in page
    assert 'name="place1_minutes" value="30" checked' in page
    add("near", DOWNTOWN)
    assert cards(bare)["near"]["travel"].endswith("by car from Home")


def test_place_rows_go_with_the_account(bare):
    save(bare, place_fields(1, "Home", "Venice"))
    data.delete_account(uid_of(bare))
    assert places() == []


def test_place_geolocation_is_allowed_on_the_preferences_page_only(bare):
    policy = lambda path: bare.get(path).headers["permissions-policy"]
    assert "geolocation=(self)" in policy("/me/preferences")
    assert "geolocation=()" in policy("/deck")
    assert "geolocation=()" in policy("/profile")


def test_place_page_lists_neighborhoods_and_loads_geo_js(bare):
    page = bare.get("/me/preferences").text
    assert '<datalist id="la-areas">' in page and '<option value="Silverlake">' in page
    assert '<script src="/static/geo.js" defer>' in page
    assert bare.get("/static/geo.js").status_code == 200


def test_place_minutes_by_mode():
    assert match.minutes(16, "car") == 30
    assert match.minutes(7, "transit") == 30
    assert match.minutes(0.05, "walk") == 1  # never "0 min"
    assert match.reach_km("bike", 30) == pytest.approx(6.5)
    assert match.reach_km("walk", 0) is None


def test_place_best_place_skips_places_out_of_reach():
    home = {"lat": 34.0, "lon": -118.4, "mode": "car", "reach_km": 5}
    work = {"lat": 34.05, "lon": -118.25, "mode": "walk", "reach_km": None}
    p, km, minutes = match.best_place([home, work], 34.05, -118.25)
    assert p is work and km == 0 and minutes == 1
    assert match.best_place([home], 34.05, -118.25) is None


def test_place_neighborhood_names_are_colloquial():
    names = set(geo.neighborhoods())
    assert {"Silverlake", "Santa Monica", "Venice", "Altadena", "Downtown"} <= names
    assert not [n for n in names if n.startswith(("City of", "Los Angeles - "))]
    assert len(names) == len(geo.neighborhoods()) > 300  # unique


def test_place_cards_never_carry_coordinates(bare):
    save(bare, place_fields(1, "Home", "Venice", minutes="0"))
    add("anywhere", DOWNTOWN)
    for c in cards(bare).values():
        assert not {"lat", "lon"} & set(c)
        assert json.dumps(c).count("34.") == 0
